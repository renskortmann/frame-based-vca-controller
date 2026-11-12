"""vcactl.analysis: synthetic modes with known frequency and damping must come back out."""

import dataclasses
import math

import numpy as np
import pytest
from scipy import signal

from conftest import PROFILES, pin_rates
from vcactl.analysis.io import load_hits, load_raw
from vcactl.analysis.modal import fit_mode, half_power
from vcactl.analysis.ringdown import damper_energy, fit_decay, zeta_vs_amplitude
from vcactl.analysis.spectra import h1_periodic, h1_welch, psd
from vcactl.cli import main
from vcactl.config import load_settings, load_shaker
from vcactl.controller import Controller
from vcactl.daq.sim import SimulatedDaq
from vcactl.logger import RunLogger
from vcactl.profile import load_profile

FS = 2048.0
F_N, ZETA = 33.0, 0.005


def sdof_filter(f_n=F_N, zeta=ZETA, fs=FS):
    """Mobility-like SDOF (velocity per force), discretized with the impulse-invariant map so the
    pole lands exactly on f_n, zeta."""
    w = 2 * math.pi * f_n
    p = -zeta * w + 1j * w * math.sqrt(1 - zeta ** 2)
    zp = np.exp(np.array([p, np.conj(p)]) / fs)
    a = np.real(np.poly(zp))
    b = np.array([1.0, -1.0])            # a zero at DC (velocity-like)
    return b, a


def test_fit_mode_on_an_analytic_frf():
    f = np.arange(0, 100, 0.01)
    s = 2j * np.pi * f
    w = 2 * np.pi * F_N
    h = s ** 2 / (s ** 2 + 2 * ZETA * w * s + w ** 2) * 1e-2 + 3e-3      # accelerance + residual
    fit = fit_mode(f, h, 31.0, 35.0)
    assert fit.f_n == pytest.approx(F_N, rel=1e-6)
    assert fit.zeta == pytest.approx(ZETA, rel=1e-4)
    hp = half_power(f, h, 31.0, 35.0)
    assert hp.zeta == pytest.approx(ZETA, rel=0.05)


def test_periodic_h1_has_no_leakage():
    rng = np.random.default_rng(1)
    period = 2 ** 15                                 # 16 s at 2048 S/s, df 0.0625 Hz
    x1 = np.fft.irfft(np.exp(2j * np.pi * rng.uniform(size=period // 2 + 1)), period)
    x = np.tile(x1, 6)
    b, a = sdof_filter()
    y = signal.lfilter(b, a, x) + 1e-6 * rng.standard_normal(len(x))
    starts = [k * period for k in range(2, 6)]       # skip the transient
    frf = h1_periodic(x, y, FS, starts, period)
    assert frf.averages == 4
    fit = fit_mode(frf.f, frf.h1, 31.0, 35.0)
    assert fit.f_n == pytest.approx(F_N, rel=1e-4)
    assert fit.zeta == pytest.approx(ZETA, rel=0.01)
    band = (frf.f > 5) & (frf.f < 500)
    assert np.min(frf.coherence[band]) > 0.999

    # the same response analysed with Hann windows of 4 s (df 0.25 Hz): leakage widens the peak
    rng2 = np.random.default_rng(2)
    xr = rng2.standard_normal(len(x))
    yr = signal.lfilter(b, a, xr)
    welch = h1_welch(xr, yr, FS, df=0.25)
    assert fit_mode(welch.f, welch.h1, 31.0, 35.0).zeta > 1.2 * ZETA


def test_decay_fit():
    t = np.arange(0, 8, 1 / FS)
    y = (np.exp(-ZETA * 2 * np.pi * F_N * t) * np.cos(2 * np.pi * F_N * np.sqrt(1 - ZETA ** 2) * t)
         + 0.3 * np.exp(-0.01 * 2 * np.pi * 170 * t) * np.sin(2 * np.pi * 170 * t)
         + 1e-4 * np.random.default_rng(3).standard_normal(len(t)))
    fit = fit_decay(t, y, FS, 25.0, 45.0)
    assert fit.f_hz == pytest.approx(F_N, rel=1e-3)
    assert fit.zeta == pytest.approx(ZETA, rel=0.02)
    assert fit.zeta_std < 0.05 * ZETA
    amp, z = zeta_vs_amplitude(t, y, FS, 25.0, 45.0, t_start=0.5)
    mid = (amp < 0.5) & (amp > 0.05)
    np.testing.assert_allclose(z[mid], ZETA, rtol=0.05)          # viscous: constant


def test_damper_energy():
    fs, f, c = 20000.0, 120.0, 20.0
    t = np.arange(0, 0.5, 1 / fs)
    w = 2 * np.pi * f
    x = 1e-4 * np.exp(-2 * t) * np.sin(w * t)
    v = np.gradient(x, 1 / fs)
    acc = np.gradient(v, 1 / fs) / 9.80665
    force = -c * v                           # the damper's force on the beam
    t0, e, c_eq = damper_energy(force, acc, fs, f)
    mid = (t0 > 0.05) & (t0 < 0.4)
    np.testing.assert_allclose(c_eq[mid], c, rtol=0.03)
    assert np.all(e[mid] > 0)


def test_psd_level():
    x = np.random.default_rng(4).standard_normal(int(60 * FS))
    f, p = psd(x, FS, df=1.0)
    assert np.mean(p[10:-10]) == pytest.approx(2 / FS, rel=0.05)    # one-sided white noise


def test_periodic_run_analysis(tmp_path):
    """End to end: a simulated periodic random run, read back and analysed per period."""
    st = pin_rates(load_settings(daq="usb4431"), 51200, 1, 8192)
    st = dataclasses.replace(st, control=dataclasses.replace(st.control, periodic_deadband_db=3.0))
    shaker = load_shaker("bk4809")
    prof = tmp_path / "p.toml"
    prof.write_text('drive = "periodic"\nperiod_s = 1.0\n' + (PROFILES / "low_g.toml").read_text())
    logger = RunLogger(tmp_path, "periodic")
    ctl = Controller(st, shaker, load_profile(prof), SimulatedDaq(st, shaker, seed=1),
                     duration_s=10.0, seed=2, logger=logger, record=True, target_level_db=-12)
    assert ctl.run().completed
    logger.meta({"raw": {"columns": ["drive_v", "response_g"], "fs_hz": ctl.fs,
                         "response_lag_samples": ctl.response_lag}})
    logger.close()
    rec = load_raw(logger.dir)
    starts = rec.period_starts()
    assert len(starts) >= 4
    assert any(e["event"] == "rampdown" for e in rec.events)
    frf = h1_periodic(rec["drive_v"], rec["response_g"], rec.fs, starts, ctl.period_n,
                      rec.response_lag)
    band = (frf.f > 25) & (frf.f < 2000)
    assert np.min(frf.coherence[band]) > 0.99
    # the simulator's mass line above the suspension resonance: |H| = amp gain * Bl / m / g
    sim = shaker.sim
    m = shaker.moving_mass_kg + st.payload_total_kg
    mass_line = sim.amp_gain_a_per_v * sim.bl_n_per_a / m / 9.80665
    k = (frf.f > 1000) & (frf.f < 1500)
    assert np.median(np.abs(frf.h1[k])) == pytest.approx(mass_line, rel=0.1)


def test_load_hits(tmp_path):
    prof = tmp_path / "tap.toml"
    prof.write_text('type = "tap"\nrecord_s = 0.5\npretrigger_s = 0.01\nhits = 2\n'
                    'f_max_hz = 2000.0\n')
    assert main(["tap", "--sim", "--yes", "--daq", "usb4431", "--channels", "cantilever_tap_beam",
                 "--profile", str(prof), "--log-dir", str(tmp_path / "runs"), "--seed", "1"]) == 0
    rec, records = load_hits(next((tmp_path / "runs").iterdir()))
    assert records.shape == (2, 25600, 3)
    assert [e["accepted"] for e in rec.events] == ["True", "True"]
    assert np.max(records[0, :, 0]) == pytest.approx(50.0, rel=0.01)


def _tool():
    import importlib.util
    from conftest import ROOT
    spec = importlib.util.spec_from_file_location("frf_damping", ROOT / "tools" / "frf_damping.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tool_fit_and_ringdown(tmp_path, capsys):
    tool = _tool()
    prof = tmp_path / "tap.toml"
    prof.write_text('type = "tap"\nrecord_s = 1.0\npretrigger_s = 0.01\nhits = 2\n'
                    'exp_window_end = 0.05\nf_max_hz = 2000.0\n')
    assert main(["tap", "--sim", "--yes", "--daq", "usb4431", "--channels", "cantilever_tap_beam",
                 "--profile", str(prof), "--log-dir", str(tmp_path / "runs"), "--seed", "1"]) == 0
    run = next((tmp_path / "runs").iterdir())
    tool.main(["fit", str(run / "tap_frf.csv"), "--band", "100", "140", "--column", "drive_point"])
    out = capsys.readouterr().out
    zeta = float(out.split("zeta ")[-1].split(" %")[0])          # after the window correction
    assert zeta == pytest.approx(2.0, abs=0.05)                  # the simulator's mode: 2 %

    t = np.arange(0, 6, 1 / FS)
    y = np.exp(-ZETA * 2 * np.pi * F_N * t) * np.cos(2 * np.pi * F_N * t)
    csv = tmp_path / "ringdown_L1F1.csv"
    np.savetxt(csv, np.column_stack([t, y]), delimiter=",", header="t_s,response_g",
               comments="")
    tool.main(["ringdown", str(csv), "--band", "25", "45"])
    out = capsys.readouterr().out
    assert float(out.split("zeta ")[1].split(" %")[0]) == pytest.approx(0.5, abs=0.01)
    assert (tmp_path / "ringdown_L1F1_zeta_25-45Hz.csv").exists()
