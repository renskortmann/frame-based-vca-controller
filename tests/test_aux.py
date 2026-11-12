"""The aux DAQ (load cells on a USB-6211): configuration, the simulated second clock, and the
offset/drift fit that aligns its recording with the control DAQ."""

import json
import sys
import time

import numpy as np
import pytest
from scipy import signal

from vcactl.analysis.sync import sync_run
from vcactl.cli import main
from vcactl.config import ConfigError, load_channels, load_settings

AUX = "tests/data/channels_aux.toml"


def aux_settings(daq="usb4431", text=None, tmp_path=None):
    path = AUX
    if text is not None:
        path = tmp_path / "chans.toml"
        path.write_text(text)
    return load_channels(str(path), load_settings(daq=daq))


def test_aux_config():
    st = aux_settings()
    aux = st.aux
    assert aux.rate_hz == 25600.0 and aux.daq.device == "Dev2"
    assert [c.role for c in aux.channels] == ["drive_ref", "load", "load"]
    assert [c.unit for c in aux.channels] == ["V", "N", "N"]
    assert aux.channels[0].range_v == 5.0            # holds the +/-3.5 V AO of the USB-4431
    assert aux.channels[1].range_v == 10.0           # 50 N x 100 mV/N


@pytest.mark.parametrize("change, message", [
    (('rate_hz = 25600.0', 'rate_hz = 100000.0'), "aggregate rate"),
    (('role = "drive_ref"', 'role = "load"\nsensor = "tests/data/loadcell_test.toml"'),
     "exactly one channel with role drive_ref"),
    (('name = "normal"', 'name = "force"'), "must differ from the control DAQ"),
    (('role = "drive_ref"', 'role = "speed"'), "role must be one of"),
])
def test_aux_errors(tmp_path, change, message):
    text = open(AUX).read().replace(*change)
    with pytest.raises(ConfigError, match=message):
        aux_settings(text=text, tmp_path=tmp_path)


def test_aux_needs_a_dsa_control_daq():
    text = open(AUX).read().replace('physical = "ai0"\nsensor = "pcb352b"',
                                    'physical = "ai1"\nsensor = "pcb352b"', 1)
    text = text.replace('[[channel]]\nname = "force"\nphysical = "ai1"\nsensor = "bk8230_001"\n'
                        'role = "force"\n', '')
    path = "tests/data/_tmp_aux.toml"
    try:
        open(path, "w").write(text)
        with pytest.raises(ConfigError, match="delta-sigma"):
            load_channels(path, load_settings(daq="usb6211"))
    finally:
        import os
        os.remove(path)


def _lag(a, b):
    """Sub-sample lag of ``a`` relative to ``b``."""
    c = signal.correlate(a - a.mean(), b - b.mean(), mode="full", method="fft")
    mid = len(a) - 1
    seg = c[mid - 20:mid + 21]
    j = int(np.argmax(seg))
    x, y, z = seg[j - 1:j + 2]
    return j - 20 + 0.5 * (x - z) / (x - 2 * y + z)


def test_driven_run_is_aligned(tmp_path):
    assert main(["run", "--sim", "--record", "--yes", "--seed", "1", "--log-dir", str(tmp_path),
                 "--shaker", "bk4809", "--daq", "usb4431", "--channels", AUX, "--profile",
                 "low_g", "--duration", "6", "--level", "-12", "--sim-aux-ppm", "120",
                 "--sim-aux-offset-ms", "3"]) == 0
    run = next(tmp_path.iterdir())
    assert {"ao.f32", "aux.f32", "raw.f32"} <= {p.name for p in run.iterdir()}
    fit = sync_run(run)
    assert fit.drift_ppm == pytest.approx(120.0, abs=1.0)
    assert fit.residual_samples < 0.1
    meta = json.loads((run / "meta.json").read_text())
    cols = meta["aux"]["columns"]
    assert cols == ["drive_ref_V", "normal_N", "tangential_N"]
    aligned = np.fromfile(run / "aux_aligned.f32", dtype=np.float32).reshape(-1, 3)
    raw = np.fromfile(run / "raw.f32", dtype=np.float32).reshape(-1, 3)    # drive, accel, force
    acc = raw[:, 1].astype(float)
    tan = aligned[:len(acc), 2].astype(float)
    k = np.arange(100000, len(acc) - 1000)
    assert np.all(np.isfinite(tan[k]))
    # the simulated tangential force is 5 N/g x the table acceleration at the same instant
    assert abs(_lag(tan[k], 5 * acc[k])) < 0.2
    assert np.std(tan[k] - 5 * acc[k]) < 0.05 * np.std(5 * acc[k])
    assert np.nanmean(aligned[:, 1]) == pytest.approx(20.0, abs=0.05)
    from test_analysis import _tool
    _tool().main(["friction", str(run), "--normal", "normal_N", "--tangential", "tangential_N"])
    fr = np.genfromtxt(run / "friction.csv", delimiter=",", names=True)
    mid = len(fr) // 2
    np.testing.assert_allclose(fr["mu"][mid:mid + 100],
                               fr["tangential_n"][mid:mid + 100] / fr["normal_n"][mid:mid + 100],
                               rtol=1e-5)              # the CSV holds 6 significant digits


def test_record_with_markers_is_aligned(tmp_path):
    assert main(["record", "--sim", "--yes", "--seconds", "8", "--daq", "usb4431",
                 "--channels", AUX, "--log-dir", str(tmp_path), "--seed", "1",
                 "--sim-aux-ppm", "-80", "--sim-aux-offset-ms", "1"]) == 0
    run = next(tmp_path.iterdir())
    ao = np.fromfile(run / "ao.f32", dtype=np.float32)
    assert np.max(np.abs(ao)) == pytest.approx(0.01, rel=0.05)        # chirp markers only
    fit = sync_run(run)
    assert fit.windows >= 3
    assert fit.drift_ppm == pytest.approx(-80.0, abs=1.0)


def test_nidaq_with_aux(monkeypatch):
    from test_channels import FakeNi

    fake = FakeNi()
    mods = fake.module()
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    from conftest import pin_rates
    from vcactl.daq import nidaq
    monkeypatch.setattr(nidaq, "SETTLE_HOLD_S", 0.2)
    st = pin_rates(aux_settings(), 51200, 1, 8192)
    got = []
    backend = nidaq.NiDaq(st, 4096)
    backend.sink = lambda rows, name: got.append((name, rows.shape))
    backend.start(np.zeros(8192), 10.0)
    try:
        ai = next(t for t in fake.tasks if t.name == "vcactl_ai")
        aux = next(t for t in fake.tasks if t.name == "vcactl_aux")
        assert ai.trig == "/Dev1/PFI0"
        assert aux.export_signals.start_trig_output_term == "/Dev2/PFI4"
        assert [c.name for c in aux.chans] == ["Dev2/ai0", "Dev2/ai1", "Dev2/ai2"]
        backend.write(np.zeros(4096))
        backend.read(16)
        time.sleep(0.05)
        assert ("ao.f32", (8192, 1)) in got and ("ao.f32", (4096, 1)) in got
        assert any(name == "aux.f32" and shape[1] == 3 for name, shape in got)
        assert backend.aux.conv_rate == 250000.0
    finally:
        backend.stop()


def test_estimate_follows_a_large_drift():
    from vcactl.analysis.sync import align, estimate

    fs, fa, ppm, off = 51200.0, 25600.0, 150.0, 0.004
    rng = np.random.default_rng(5)
    sos = signal.butter(6, 3000, fs=fs, output="sos")
    ao = signal.sosfilt(sos, rng.standard_normal(int(62 * fs)))
    tau = np.arange(int(60 * fa)) / fa
    ref = np.interp((tau * (1 + ppm * 1e-6) - off) * fs, np.arange(len(ao)), ao)
    fit = estimate(ao, fs, ref, fa)
    assert fit.drift_ppm == pytest.approx(ppm, abs=0.05)
    assert fit.offset_samples == pytest.approx(-off * fs, abs=0.05)
    out = align(ref[:, None], fa, None, fit, len(ao), 0.0)
    k = np.arange(int(1 * fs), int(59 * fs))
    assert np.std(out[k, 0] - ao[k]) < 0.05 * np.std(ao)


@pytest.mark.parametrize("name", ["cantilever_magnets", "cantilever_friction"])
def test_treatment_channels(tmp_path, name):
    st = load_channels(name, load_settings(daq="usb4431"))
    assert [c.name for c in st.aux.channels] == ["drive_ref", "lc1_x", "lc1_y", "lc1_z",
                                                 "lc2_x", "lc2_y", "lc2_z"]
    assert all(c.sensor.name == "ME K3D40 50N" for c in st.aux.channels[1:])
    assert main(["record", "--sim", "--yes", "--seconds", "5", "--daq", "usb4431", "--channels",
                 name, "--log-dir", str(tmp_path), "--seed", "1", "--sim-aux-ppm", "40"]) == 0
    fit = sync_run(next(tmp_path.iterdir()))
    assert fit.drift_ppm == pytest.approx(40.0, abs=1.0)
