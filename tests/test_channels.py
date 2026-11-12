"""Channel sets (``--channels``): loading and validation, the recorded channels in the controller,
the simulator and the backends (NiDaq against a fake nidaqmx, UlDaq against the fake uldaq)."""

import dataclasses
import enum
import json
import sys
import types

import numpy as np
import pytest

from conftest import PROFILES, pin_rates
from vcactl.cli import main
from vcactl.config import ConfigError, load_channels, load_settings, load_shaker
from vcactl.controller import Controller
from vcactl.daq.sim import RESPONSE_FRACTION, SimulatedDaq
from vcactl.dsp.resample import Decimator, design_lowpass
from vcactl.logger import RunLogger
from vcactl.profile import G, load_profile
from vcactl.sine import SINE_STATUS_FIELDS, SineController


def write_channels(tmp_path, body: str, name: str = "chans.toml"):
    path = tmp_path / name
    path.write_text(body)
    return str(path)


CONTROL = '[[channel]]\nname = "drive_point"\nphysical = "ai0"\nsensor = "pcb352b"\nrole = "control"\n'
FORCE = '[[channel]]\nname = "force"\nphysical = "ai1"\nsensor = "bk8230_001"\nrole = "force"\n'


def driven_settings():
    return load_channels("cantilever_driven", load_settings(daq="usb4431"))


# ---------------------------------------------------------------------- loading and validation
def test_cantilever_driven_channels():
    st = driven_settings()
    assert [c.name for c in st.channels] == ["drive_point", "force", "base"]
    assert [c.physical for c in st.channels] == ["ai0", "ai1", "ai3"]
    assert st.has_control and st.daq.ai_channel == "ai0" and st.sensor.name.startswith("PCB 352B")
    force, base = st.extra_channels
    assert force.unit == "N" and force.mv_per_unit == 22.0 and force.range_v == 10.0
    assert force.iepe_current_ma == 2.1 and force.coupling == "AC"
    assert base.unit == "g" and base.mv_per_unit == 1048.0


def test_tap_channels_have_no_control():
    st = load_channels("cantilever_tap_shaker", load_settings(daq="usb4431"))
    assert not st.has_control
    assert [c.role for c in st.channels] == ["hammer", "response", "response", "response"]
    assert [c.mv_per_unit for c in st.channels[1:]] == [980.0, 1054.0, 1048.0]


def test_default_settings_have_only_the_control_channel():
    st = load_settings(daq="usb4431")
    assert st.extra_channels == () and [c.role for c in st.channels] == ["control"]


@pytest.mark.parametrize("body, message", [
    (CONTROL + FORCE.replace('"ai1"', '"ai0"'), "physical AI channel may be used once"),
    (CONTROL + FORCE.replace('"force"\nphysical', '"drive_point"\nphysical'), "names must be distinct"),
    (CONTROL + "".join(FORCE.replace("ai1", f"ai{i}").replace('"force"\nphysical', f'"f{i}"\nphysical')
                       for i in range(1, 5)), "has 4"),
    (CONTROL + FORCE.replace('"bk8230_001"', '"pcb352b"'), "needs a force sensor"),
    (CONTROL.replace("pcb352b", "bk8230_001"), "must be an accelerometer"),
    (CONTROL + FORCE + 'range_v = 3.0\n', "range_v must be"),
    (CONTROL + FORCE + 'coupling = "DC"\n', "coupling must be AC"),
    (CONTROL + FORCE.replace('role = "force"', 'role = "speed"'), "role must be one of"),
    (CONTROL + CONTROL.replace("ai0", "ai1").replace('"drive_point"', '"other"'), "only one channel"),
    (CONTROL + 'range_v = 10.0\n', "leave range_v out"),
    ('name = "x"\n', "at least one"),
])
def test_channel_errors(tmp_path, body, message):
    # TOML keys after a table header belong to it, so the last entry gets the extra key
    with pytest.raises(ConfigError, match=message):
        load_channels(write_channels(tmp_path, body), load_settings(daq="usb4431"))


def test_iepe_compliance_per_channel(tmp_path):
    sensor = tmp_path / "hot.toml"
    sensor.write_text('name = "hot"\nunit = "N"\nsensitivity_mv_per_n = 50.0\nrange_n_pk = 200.0\n'
                      'iepe_min_ma = 2.0\niepe_max_ma = 20.0\niepe_bias_max_v = 12.0\n')
    body = CONTROL + FORCE.replace('"bk8230_001"', f'"{sensor}"')
    with pytest.raises(ConfigError, match="compliance"):
        load_channels(write_channels(tmp_path, body), load_settings(daq="usb4431"))


def test_sensor_option_conflicts_with_a_control_channel(tmp_path):
    with pytest.raises(ConfigError, match="leave out --sensor"):
        load_channels(write_channels(tmp_path, CONTROL), load_settings(daq="usb4431"),
                      sensor_given=True)


def test_cli_check_with_channels(capsys):
    assert main(["check", "--shaker", "bk4809", "--daq", "usb4431", "--channels",
                 "cantilever_driven", "--profile", "sine_sweep", "--level", "-12"]) == 0
    out = capsys.readouterr().out
    assert "force        ai1   force    B&K 8230-001, 22 mV/N" in out


def test_cli_rejects_channels_without_control(capsys):
    assert main(["check", "--shaker", "bk4809", "--daq", "usb4431", "--channels",
                 "cantilever_tap_beam", "--profile", "low_g"]) == 2
    assert "no control channel" in capsys.readouterr().err


# ---------------------------------------------------------------------- DSP
def test_decimator_2d_equals_per_channel():
    taps = design_lowpass(100000, 4)
    rng = np.random.default_rng(3)
    x = rng.standard_normal((4096, 3))
    d2 = Decimator(taps, 4)
    ds = [Decimator(taps, 4) for _ in range(3)]
    for blk in np.split(x, 4):
        y2 = d2.process(blk)
        for c in range(3):
            np.testing.assert_allclose(y2[:, c], ds[c].process(blk[:, c]), atol=1e-12)


# ---------------------------------------------------------------------- controller + simulator
def _driven(rates=True):
    st = driven_settings()
    return pin_rates(st, 51200, 1, 8192) if rates else st


def test_random_run_records_every_channel(tmp_path):
    st = _driven()
    shaker = load_shaker("bk4809")
    profile = load_profile(PROFILES / "low_g.toml")
    backend = SimulatedDaq(st, shaker, seed=1)
    logger = RunLogger(tmp_path, "rec")
    ctl = Controller(st, shaker, profile, backend, duration_s=2.0, seed=2, logger=logger,
                     record=True, target_level_db=-12)
    res = ctl.run()
    logger.close()
    assert res.completed, res.reason
    raw = np.fromfile(logger.dir / "raw.f32", dtype=np.float32).reshape(-1, 4)
    drive, accel, force, base = raw.T
    k = slice(len(raw) // 2, None)
    payload = st.safety.payload_kg
    assert np.std(force[k]) == pytest.approx(payload * G * np.std(accel[k]), rel=0.02)
    assert np.std(base[k]) == pytest.approx(RESPONSE_FRACTION * np.std(accel[k]), rel=0.05)


def test_force_limit_aborts(tmp_path):
    st = _driven()
    st = dataclasses.replace(st, safety=dataclasses.replace(st.safety, force_limit_n=0.05))
    shaker = load_shaker("bk4809")
    profile = load_profile(PROFILES / "low_g.toml")
    res = Controller(st, shaker, profile, SimulatedDaq(st, shaker, seed=1), duration_s=5.0,
                     seed=2, target_level_db=-6).run()
    assert not res.completed and "force limit" in res.reason


def test_overload_on_a_recorded_channel_aborts(tmp_path):
    # 100 V/N, IEPE off (the compliance check would reject it): 10 V at 0.1 N
    body = CONTROL + FORCE + 'sensitivity = 100000.0\niepe_current_ma = 0.0\ncoupling = "DC"\n'
    st = load_channels(write_channels(tmp_path, body), load_settings(daq="usb4431"))
    st = pin_rates(st, 51200, 1, 8192)
    shaker = load_shaker("bk4809")
    profile = load_profile(PROFILES / "low_g.toml")
    res = Controller(st, shaker, profile, SimulatedDaq(st, shaker, seed=1), duration_s=5.0,
                     seed=2, target_level_db=0).run()
    assert not res.completed and "on channel 'force'" in res.reason


def test_ringdown_csv_has_every_channel(tmp_path):
    st = _driven()
    shaker = load_shaker("bk4809")
    path = tmp_path / "ring.toml"
    path.write_text('type = "stepped_sine"\nbreakpoints = [{ f_hz = 120.0, accel_g = 0.5 }]\n'
                    'frequencies_hz = [120.0]\nlevels_db = [-12.0]\nsettle_s = 0.5\n'
                    'dwell_s = 0.5\nringdown_s = 0.2\n')
    profile = load_profile(path)
    logger = RunLogger(tmp_path, "ring", SINE_STATUS_FIELDS)
    ctl = SineController(st, shaker, profile, SimulatedDaq(st, shaker, seed=1), seed=2,
                         logger=logger)
    res = ctl.run()
    logger.close()
    assert res.completed, res.reason
    cols = ctl.last_ringdown
    assert list(cols) == ["t_s", "response_g", "force_N", "base_g"]
    head = (logger.dir / f"ringdown_{ctl.step_label}.csv").read_text().splitlines()[0]
    assert head == "t_s,response_g,force_N,base_g"
    pre = cols["t_s"] < 0
    assert np.std(cols["force_N"][pre]) == pytest.approx(
        st.safety.payload_kg * G * np.std(cols["response_g"][pre]), rel=0.05)


def test_cli_run_meta_lists_the_columns(tmp_path):
    assert main(["run", "--sim", "--record", "--yes", "--seed", "1", "--log-dir", str(tmp_path),
                 "--shaker", "bk4809", "--daq", "usb4431", "--channels", "cantilever_driven",
                 "--profile", "low_g", "--duration", "1", "--level", "-12"]) == 0
    run = next(tmp_path.iterdir())
    meta = json.loads((run / "meta.json").read_text())
    assert meta["raw"]["columns"] == ["drive_v", "response_g", "force_N", "base_g"]
    assert meta["raw"]["units"] == ["V", "g", "N", "g"]
    assert meta["raw"]["sensitivity_mv_per_unit"] == [None, 972.0, 22.0, 1048.0]
    size = (run / "raw.f32").stat().st_size
    assert size % (4 * 4) == 0 and size > 0


# ---------------------------------------------------------------------- NiDaq, fake nidaqmx
class FakeNi:
    """Records the AI channels and their configuration; AI row r holds the channel number of
    the r-th channel of the task."""

    def __init__(self):
        self.tasks = []

    def module(self):
        fake = self
        m = types.ModuleType("nidaqmx")

        class Enum(enum.Enum):
            pass

        const = types.ModuleType("nidaqmx.constants")
        const.AcquisitionType = enum.Enum("AcquisitionType", "CONTINUOUS FINITE")
        const.RegenerationMode = enum.Enum("RegenerationMode", "DONT_ALLOW_REGENERATION")
        const.TaskMode = enum.Enum("TaskMode", "TASK_RESERVE")
        const.TerminalConfiguration = enum.Enum("TerminalConfiguration",
                                                "DIFF RSE NRSE PSEUDO_DIFF DEFAULT")
        const.Coupling = enum.Enum("Coupling", "AC DC")
        const.ExcitationSource = enum.Enum("ExcitationSource", "INTERNAL NONE")
        const.ExcitationVoltageOrCurrent = enum.Enum("ExcitationVoltageOrCurrent", "USE_CURRENT")

        class Chan(types.SimpleNamespace):
            pass

        class Channels:
            def __init__(self, task):
                self.task = task

            def add_ai_voltage_chan(self, name, terminal_config=None, min_val=0, max_val=0):
                ch = Chan(name=name, range=max_val, ai_coupling=None, ai_excit_src=None,
                          ai_excit_val=None)
                self.task.chans.append(ch)
                return ch

            add_ao_voltage_chan = add_ai_voltage_chan

        class Task:
            def __init__(self, name=""):
                self.name, self.chans = name, []
                self.ai_channels = self.ao_channels = Channels(self)
                self.timing = types.SimpleNamespace(
                    cfg_samp_clk_timing=self._timing, samp_clk_rate=0.0, ai_conv_rate=250000.0)
                self.in_stream = types.SimpleNamespace(task=self)
                self.out_stream = types.SimpleNamespace()
                self.trig = None
                self.triggers = types.SimpleNamespace(start_trigger=types.SimpleNamespace(
                    cfg_dig_edge_start_trig=lambda src: setattr(self, "trig", src)))
                self.export_signals = types.SimpleNamespace(start_trig_output_term=None)
                fake.tasks.append(self)

            def _timing(self, rate, **kw):
                self.timing.samp_clk_rate = rate

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, number_of_samples_per_channel, timeout=0):
                return [[0.0] * number_of_samples_per_channel for _ in self.chans]

            def write(self, data, auto_start=False):
                pass

            def wait_until_done(self, timeout=0):
                pass

            start = stop = close = control = lambda self, *a: None

        m.Task = Task
        m.constants = const
        m.errors = types.ModuleType("nidaqmx.errors")
        m.errors.DaqError = type("DaqError", (Exception,), {})
        m.errors.DaqWarning = type("DaqWarning", (Warning,), {})
        readers = types.ModuleType("nidaqmx.stream_readers")

        class MultiReader:
            def __init__(self, stream):
                self.task = stream.task

            def read_many_sample(self, buf, number_of_samples_per_channel, timeout=0):
                task = self.task
                if task.name == "vcactl_aux":        # read by the aux thread: pace it
                    __import__("time").sleep(0.01)
                for r, ch in enumerate(task.chans):
                    buf[r] = int(ch.name.rsplit("ai", 1)[1])

        readers.AnalogMultiChannelReader = readers.AnalogSingleChannelReader = MultiReader
        writers = types.ModuleType("nidaqmx.stream_writers")
        writers.AnalogSingleChannelWriter = lambda stream, auto_start=False: types.SimpleNamespace(
            write_many_sample=lambda data, timeout=0: None)
        mods = {"nidaqmx": m, "nidaqmx.constants": const, "nidaqmx.errors": m.errors,
                "nidaqmx.stream_readers": readers, "nidaqmx.stream_writers": writers}
        return mods


def test_nidaq_configures_each_channel(tmp_path, monkeypatch):
    fake = FakeNi()
    for name, mod in fake.module().items():
        monkeypatch.setitem(sys.modules, name, mod)
    from vcactl.daq import nidaq
    monkeypatch.setattr(nidaq, "SETTLE_HOLD_S", 0.2)
    body = (CONTROL.replace("ai0", "ai2") + FORCE.replace("ai1", "ai0")
            + '[[channel]]\nname = "loose"\nphysical = "ai1"\nsensor = "pcb356b18"\n'
              'iepe_current_ma = 0.0\ncoupling = "DC"\n')
    st = pin_rates(load_channels(write_channels(tmp_path, body), load_settings(daq="usb4431")),
                   51200, 1, 8192)
    backend = nidaq.NiDaq(st, 4096)
    backend.start(np.zeros(8192), 10.0)
    settle, ai = fake.tasks[0], fake.tasks[1]
    assert settle.name == "vcactl_settle" and [c.name for c in settle.chans] == ["Dev1/ai0", "Dev1/ai2"]
    assert [c.name for c in ai.chans] == ["Dev1/ai0", "Dev1/ai1", "Dev1/ai2"]   # ascending
    by = {c.name: c for c in ai.chans}
    assert by["Dev1/ai0"].ai_excit_val == pytest.approx(0.0021)
    assert by["Dev1/ai2"].ai_excit_val == pytest.approx(0.0021)
    assert by["Dev1/ai1"].ai_excit_src.name == "NONE" and by["Dev1/ai1"].ai_coupling.name == "DC"
    assert by["Dev1/ai0"].ai_coupling.name == "AC"
    y = backend.read(16)
    assert y.shape == (16, 3)
    np.testing.assert_array_equal(y[0], [2, 0, 1])      # control (ai2), force (ai0), loose (ai1)
    assert backend.settle_s > 0
    backend.stop()


# ---------------------------------------------------------------------- UlDaq, fake uldaq
def test_uldaq_recorded_channel(tmp_path, monkeypatch):
    import test_ul_backend as tul

    body = CONTROL.replace("ai0", "ai1") + FORCE.replace("ai1", "ai0")
    st = load_channels(write_channels(tmp_path, body), load_settings(daq="dt9837a"))
    st = pin_rates(st, tul.FS, 1, 2 * tul.BLOCK)
    monkeypatch.setattr(tul.ul, "PAD_S", 0.1)
    monkeypatch.setattr(tul.ul, "AI_BUFFER_S", 2.0)
    backend, dev = tul.started(st, monkeypatch)
    rng = np.random.default_rng(1)
    stream, got = [np.zeros(4 * tul.BLOCK)], []
    try:
        for _ in range(8):
            x = 0.1 * rng.standard_normal(tul.BLOCK)
            backend.write(x)
            stream.append(x)
            got.append(backend.read(tul.BLOCK))
    finally:
        backend.stop()
    x = np.concatenate(stream)[:8 * tul.BLOCK]
    y = np.concatenate(got)
    assert y.shape == (8 * tul.BLOCK, 2)
    # the fake returns its "control" signal on channel 0 and the AO on other channels:
    np.testing.assert_allclose(y[:, 0], x, atol=1e-12)              # control channel ai1
    np.testing.assert_allclose(y[3:, 1], 0.5 * x[:-3], atol=1e-12)  # force channel ai0
    assert dev.iepe == {0: "on", 1: "on"} and dev.coupling == {0: "ac", 1: "ac"}
