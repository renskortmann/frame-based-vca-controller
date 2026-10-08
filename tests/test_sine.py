"""Sine sweep, stepped sine and ring-down against the simulated shaker."""

import dataclasses
import math

import numpy as np
import pytest

from vcactl.controller import State
from vcactl.daq.sim import SimulatedDaq
from vcactl.dsp.resample import design_lowpass
from vcactl.logger import RunLogger
from vcactl.profile import load_profile
from vcactl.sine import SINE_STATUS_FIELDS, SineController
from conftest import PROFILES


def write_profile(tmp_path, body: str):
    p = tmp_path / "profile.toml"
    p.write_text(body)
    return load_profile(p)


def run(settings, shaker, profile, *, sim_kwargs=None, rows=None, sim_cls=SimulatedDaq, **kw):
    backend = sim_cls(settings, shaker, seed=1, **(sim_kwargs or {}))
    ctl = SineController(settings, shaker, profile, backend, seed=2,
                         on_status=(rows.append if rows is not None else None), **kw)
    return ctl, backend, ctl.run()


def assert_safe_stop(shaker, backend):
    ao = np.concatenate(backend.written)
    assert np.max(np.abs(ao)) <= shaker.max_drive_v + 1e-12
    assert np.all(ao[-1000:] == 0.0), "AO stream must end in zeros"
    assert backend.stopped_at_zero


SWEEP = ('type = "sine_sweep"\nbreakpoints = [{ f_hz = 20.0, accel_g = 0.5 }]\n'
         'f_start_hz = 20.0\nf_end_hz = 1000.0\nrate_oct_min = 6.0\nsweeps = 2\n')


def test_sweep_tracks_reference(settings, shaker, tmp_path):
    rows = []
    ctl, backend, res = run(settings, shaker, write_profile(tmp_path, SWEEP), rows=rows)
    assert res.completed and res.state == State.DONE, res.reason
    sweep = [r for r in rows if r["state"].startswith("sweep") and "err_db" in r]
    errs = np.abs([r["err_db"] for r in sweep])
    assert np.mean(errs <= 1.0) > 0.95 and errs.max() < 3.0
    up = [r["f_hz"] for r in sweep if r["state"] == "sweep 1/2"]
    down = [r["f_hz"] for r in sweep if r["state"] == "sweep 2/2"]
    assert up[-1] > 950 and down[-1] < 21          # both directions reach their end frequency
    # the measured frame lags the drive by about half a frame around the turnaround
    assert np.all(np.diff(up[3:]) > 0) and np.all(np.diff(down[3:]) < 0)
    assert_safe_stop(shaker, backend)


def test_sweep_on_nonlinear_shaker(settings, tv51110, tmp_path):
    sh = dataclasses.replace(tv51110, sim=dataclasses.replace(tv51110.sim, cubic_coeff=0.05))
    rows = []
    _, _, res = run(settings, sh, write_profile(tmp_path, SWEEP.replace("0.5 }", "2.0 }")),
                    rows=rows)
    assert res.completed, res.reason
    errs = np.abs([r["err_db"] for r in rows if r["state"].startswith("sweep") and "err_db" in r])
    assert np.mean(errs <= 1.0) > 0.95


def test_stepped_sine_steps_within_tolerance(settings, tv51110, tmp_path):
    prof = load_profile(PROFILES / "example_stepped_sine.toml")
    prof = dataclasses.replace(prof, frequencies_hz=prof.frequencies_hz[::4], settle_s=0.5,
                               dwell_s=1.0)
    logger = RunLogger(tmp_path, "stepped", SINE_STATUS_FIELDS)
    ctl, backend, res = run(settings, tv51110, prof, logger=logger)
    logger.close()
    assert res.completed, res.reason
    assert len(ctl.steps) == len(prof.levels_db) * len(prof.frequencies_hz)
    for row in ctl.steps:
        assert abs(row["err_db"]) <= prof.tolerance.alarm_db
        assert row["ref_pk_g"] == pytest.approx(0.5 * 10 ** (row["level_db"] / 20))
    data = np.genfromtxt(logger.dir / "steps.csv", delimiter=",", names=True, dtype=None,
                         encoding=None)
    assert len(data) == len(ctl.steps)
    # a linear shaker: the same H at every level
    h = {}
    for row in ctl.steps:
        h.setdefault(row["f_hz"], []).append(row["h_mag_g_per_v"])
    for values in h.values():
        assert max(values) / min(values) < 1.02
    assert_safe_stop(tv51110, backend)


def test_ringdown(settings, tv51110, tmp_path):
    prof = write_profile(tmp_path, 'type = "stepped_sine"\n'
                         'breakpoints = [{ f_hz = 120.0, accel_g = 0.5 }]\n'
                         'frequencies_hz = [120.0, 300.0]\nsettle_s = 0.5\ndwell_s = 1.0\n'
                         'ringdown_s = 0.5\n')
    logger = RunLogger(tmp_path, "ring", SINE_STATUS_FIELDS)
    ctl, backend, res = run(settings, tv51110, prof, logger=logger)
    logger.close()
    assert res.completed, res.reason
    assert len(ctl.events) == 2 and len(ctl.steps) == 2
    assert {"events.csv", "ringdown_L1F1.csv", "ringdown_L1F2.csv"} <= {
        p.name for p in logger.dir.iterdir()}

    ao = np.concatenate(backend.written)
    R = settings.daq.decimation
    taps = design_lowpass(settings.daq.fs_io_hz, R)
    d = (len(taps) - 1) // 2                         # interpolation delay (I/O samples)
    for ev, step in zip(ctl.events, ctl.steps):
        cut_io = round(ev["t_stream_s"] * settings.fs_control_hz) * R + d
        quiet = ao[cut_io + len(taps):cut_io + round(ev["ringdown_s"] * settings.daq.fs_io_hz)]
        assert np.all(np.abs(quiet) < 1e-12), "zero drive during the ring-down"
        # cut at a zero crossing: the drive just before the cut is small
        assert np.max(np.abs(ao[cut_io - R:cut_io])) < 0.1 * step["drive_pk_v"]

    data = np.genfromtxt(logger.dir / "ringdown_L1F2.csv", delimiter=",", names=True)
    t, y = data["t_s"], data["response_g"]
    assert not np.any(np.isnan(y))
    assert t[0] == pytest.approx(-0.1) and t[-1] == pytest.approx(0.5, abs=1e-3)
    before = np.max(np.abs(y[t < -0.01]))
    after = np.max(np.abs(y[t > 0.1]))
    assert before == pytest.approx(0.5, rel=0.05)
    assert after < 0.05 * before
    assert_safe_stop(tv51110, backend)


def test_displacement_sensor_stepped_sine(laser_settings, tv51110):
    prof = load_profile(PROFILES / "example_stepped_sine_low_freq.toml")
    prof = dataclasses.replace(prof, frequencies_hz=prof.frequencies_hz[::5], dwell_s=2.0)
    ctl, backend, res = run(laser_settings, tv51110, prof)
    assert res.completed, res.reason
    for row in ctl.steps:
        assert abs(row["err_db"]) <= prof.tolerance.alarm_db
    assert_safe_stop(tv51110, backend)


def test_open_loop_aborts(settings, tv51110, tmp_path):
    _, backend, res = run(settings, tv51110, write_profile(tmp_path, SWEEP),
                          sim_kwargs={"disconnect_after_s": 15.0})
    assert not res.completed and res.state == State.ABORTED
    assert "open loop" in res.reason or "amplitude error" in res.reason, res.reason
    assert_safe_stop(tv51110, backend)


def test_insufficient_drive_aborts_after_pretest(settings, tv51110, tmp_path):
    # 5 g needs about 0.42 V peak; the limit is min(0.3, sqrt(2) * 0.1) = 0.14 V peak
    sh = dataclasses.replace(tv51110, max_drive_rms_v=0.1, max_drive_v=0.3)
    ctl, backend, res = run(settings, sh, write_profile(tmp_path, SWEEP.replace("0.5 }", "5.0 }")))
    assert not res.completed and "drive" in res.reason, res.reason
    assert ctl.run_time_s == 0
    assert_safe_stop(sh, backend)


def test_amplitude_loss_aborts(settings, tv51110, tmp_path):
    """The response suddenly drops by 12 dB (e.g. a loosened mount): the amplitude abort
    or the open-loop check must stop the test."""
    class DropSim(SimulatedDaq):
        def read(self, n):
            v = super().read(n)
            return v / 4 if self.samples_read / self.fs_io > 12.0 else v

    prof = write_profile(tmp_path, 'type = "stepped_sine"\n'
                         'breakpoints = [{ f_hz = 100.0, accel_g = 0.5 }]\n'
                         'frequencies_hz = [100.0]\ndwell_s = 20.0\n')
    _, backend, res = run(settings, tv51110, prof, sim_cls=DropSim)
    assert not res.completed and res.state == State.ABORTED
    assert "amplitude error" in res.reason or "open loop" in res.reason, res.reason
    assert_safe_stop(tv51110, backend)


def test_operator_stop_fades_sine(settings, tv51110, tmp_path):
    holder = {}

    def on_status(row):
        if row["state"].startswith("sweep") and row["t_s"] > 12:
            holder["ctl"].stop_requested = True

    backend = SimulatedDaq(settings, tv51110, seed=1)
    ctl = SineController(settings, tv51110, write_profile(tmp_path, SWEEP), backend,
                         on_status=on_status)
    holder["ctl"] = ctl
    res = ctl.run()
    assert res.state == State.ABORTED and "operator" in res.reason
    ao = np.concatenate(backend.written)
    nz = np.flatnonzero(ao)
    tail = ao[nz[-1] - 20000:nz[-1] + 1]
    assert np.max(np.abs(tail[-2000:])) < 0.3 * np.max(np.abs(tail[:2000]))
    assert_safe_stop(tv51110, backend)
