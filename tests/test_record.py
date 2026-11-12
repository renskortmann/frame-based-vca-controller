"""``vcactl record``: input-only recording of every channel, and input-only backend starts."""

import json
import sys

import numpy as np
import pytest

from vcactl.cli import main
from vcactl.config import load_channels, load_settings
from vcactl.daq.base import DaqFault
from vcactl.daq.sim import SimulatedDaq
from vcactl.config import load_shaker
from vcactl.record import InputStream, record_settings, run_record
from vcactl.safety import AbortError


def test_cli_record(tmp_path):
    assert main(["record", "--sim", "--yes", "--seconds", "1.0", "--daq", "usb4431",
                 "--channels", "cantilever_driven", "--log-dir", str(tmp_path), "--seed", "1"]) == 0
    run = next(tmp_path.iterdir())
    meta = json.loads((run / "meta.json").read_text())
    raw = meta["raw"]
    assert raw["columns"] == ["drive_point_g", "force_N", "base_g"]
    assert raw["units"] == ["g", "N", "g"] and raw["fs_hz"] == 51200
    x = np.fromfile(run / "raw.f32", dtype=np.float32).reshape(-1, 3)
    assert len(x) == 51200 and meta["recorded_s"] == pytest.approx(1.0)
    # no drive: only sensor noise (50 uV) in each channel's unit
    np.testing.assert_allclose(np.std(x, axis=0), 50e-6 / np.array([0.972, 0.022, 1.048]),
                               rtol=0.05)
    assert (run / "status.csv").read_text().splitlines()[0] == \
        "t_s,drive_point_g_rms,force_N_rms,base_g_rms"


def test_cli_record_default_channel(tmp_path):
    assert main(["record", "--sim", "--yes", "--seconds", "0.3", "--daq", "usb4431",
                 "--log-dir", str(tmp_path)]) == 0
    meta = json.loads((next(tmp_path.iterdir()) / "meta.json").read_text())
    assert meta["raw"]["columns"] == ["control_g"]


def test_sim_input_only_refuses_writes():
    st = record_settings(load_settings(daq="usb4431"))
    backend = SimulatedDaq(st, load_shaker("bk4809"), seed=1)
    backend.start(None, 10.0)
    assert backend.read(100).shape == (100,)
    with pytest.raises(DaqFault, match="input-only"):
        backend.write(np.zeros(10))


class Loud:
    """A backend whose force channel reads 9.9 V."""

    def start(self, prefill, ai_range_v):
        assert prefill is None

    def read(self, n):
        x = np.zeros((n, 3))
        x[5, 1] = 9.9
        return x

    def stop(self):
        self.stopped = True


def test_overload_stops_the_recording():
    st = record_settings(load_channels("cantilever_driven", load_settings(daq="usb4431")))
    backend = Loud()
    res = run_record(st, backend, None, 1.0)
    assert not res.completed and "AI overload on channel 'force'" in res.reason
    assert backend.stopped


def test_input_stream_scales_to_units():
    st = record_settings(load_channels("cantilever_driven", load_settings(daq="usb4431")))

    class One:
        def read(self, n):
            return np.ones((n, 3)) * 0.1

    s = InputStream(st, One(), 8)
    np.testing.assert_allclose(s.read()[0], [0.1 / 0.972, 0.1 / 0.022, 0.1 / 1.048])
    assert [c.range_v for c in s.channels] == [10.0, 10.0, 10.0]


def test_nidaq_input_only(tmp_path, monkeypatch):
    from test_channels import FakeNi

    fake = FakeNi()
    for name, mod in fake.module().items():
        monkeypatch.setitem(sys.modules, name, mod)
    from vcactl.daq import nidaq
    monkeypatch.setattr(nidaq, "SETTLE_HOLD_S", 0.2)
    st = record_settings(load_channels("cantilever_driven", load_settings(daq="usb4431")))
    backend = nidaq.NiDaq(st, 512)
    backend.start(None, 10.0)
    assert backend.ao_task is None
    assert [t.name for t in fake.tasks] == ["vcactl_settle", "vcactl_ai"]
    with pytest.raises(DaqFault, match="input-only"):
        backend.write(np.zeros(4))
    assert backend.read(4).shape == (4, 3)
    backend.stop()


def test_uldaq_input_only(monkeypatch):
    import test_ul_backend as tul
    from conftest import pin_rates

    monkeypatch.setattr(tul.ul, "AI_BUFFER_S", 2.0)
    dev = tul.FakeDevice()
    tul.install(monkeypatch, ("01", dev))
    st = pin_rates(load_settings(daq="dt9837a"), tul.FS, 1, 2 * tul.BLOCK)
    backend = tul.ul.UlDaq(st, tul.BLOCK)
    backend.start(None, 10.0)
    try:
        assert not dev.ao_running and dev.a_out_values == [0.0]     # AO held at 0 V
        assert backend.read(tul.BLOCK).shape == (tul.BLOCK,)
        with pytest.raises(DaqFault, match="input-only"):
            backend.write(np.zeros(4))
    finally:
        backend.stop()
