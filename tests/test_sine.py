"""Sine sweep, stepped sine and ring-down against the simulated shaker."""

import dataclasses
import math

import numpy as np
import pytest

from vcactl.config import load_settings
from vcactl.controller import State
from vcactl.daq.sim import SimulatedDaq
from vcactl.dsp.resample import design_lowpass
from vcactl.logger import RunLogger
from vcactl.profile import load_profile
from vcactl.rates import resolve_rates
from vcactl.sine import SINE_STATUS_FIELDS, SineController
from conftest import PROFILES, TEST_SENSOR


def write_profile(tmp_path, body: str):
    p = tmp_path / "profile.toml"
    p.write_text(body)
    return load_profile(p)


def run(settings, shaker, profile, *, sim_kwargs=None, rows=None, sim_cls=SimulatedDaq, **kw):
    if not settings.rates_resolved:
        settings, _ = resolve_rates(profile, shaker, settings)
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


def test_sweep_with_a_long_ao_queue(tv51110, tmp_path):
    """DT9837A: 8 half-frames of AO queue (0.64 s); the sine correction gain is scaled down
    (4 / 10). The late correction tracks the suspension resonance less tightly, so the sweep
    runs at half the rate of the other sweep tests (3 instead of 6 oct/min)."""
    settings = load_settings(daq="dt9837a", sensor=TEST_SENSOR)
    prof = write_profile(tmp_path, SWEEP.replace("1000.0", "200.0").replace("6.0", "3.0")
                         .replace("sweeps = 2", "sweeps = 1"))
    rows = []
    ctl, backend, res = run(settings, tv51110, prof, rows=rows)
    assert res.completed and res.state == State.DONE, res.reason
    assert ctl.P == 8 and ctl.gain == pytest.approx(settings.sine.correction_gain * 4 / 10)
    errs = np.abs([r["err_db"] for r in rows if r["state"].startswith("sweep") and "err_db" in r])
    assert np.mean(errs <= 1.0) > 0.95 and errs.max() < 3.0
    assert_safe_stop(tv51110, backend)


def test_sweep_on_nonlinear_shaker(settings, tv51110, tmp_path):
    sh = dataclasses.replace(tv51110, sim=dataclasses.replace(tv51110.sim, cubic_coeff=0.05))
    rows = []
    _, _, res = run(settings, sh, write_profile(tmp_path, SWEEP.replace("0.5 }", "2.0 }")),
                    rows=rows)
    assert res.completed, res.reason
    errs = np.abs([r["err_db"] for r in rows if r["state"].startswith("sweep") and "err_db" in r])
    assert np.mean(errs <= 1.0) > 0.95


def test_stepped_sine_steps_within_tolerance(settings, tv51110, tmp_path):
    prof = load_profile(PROFILES / "stepped_sine.toml")
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


@pytest.mark.parametrize("fade", [True, False])
def test_large_step_fades_instead_of_gliding(settings, tmp_path, monkeypatch, fade):
    """500 -> 30 Hz glides through the simulated BK 4809 resonance (about 54 Hz) and overshoots;
    a jump of more than JUMP_FADE_OCT fades out and ramps in again, so it does not."""
    from vcactl import sine
    from vcactl.config import load_shaker
    if not fade:
        monkeypatch.setattr(sine, "JUMP_FADE_OCT", 100.0)
    prof = write_profile(tmp_path, 'type = "stepped_sine"\n'
                         'breakpoints = [{ f_hz = 30.0, accel_g = 0.5 }]\n'
                         'frequencies_hz = [500.0, 30.0]\nsettle_s = 0.5\ndwell_s = 0.5\n')
    logger = RunLogger(tmp_path, "jump", SINE_STATUS_FIELDS)
    rows = []
    ctl, backend, res = run(settings, load_shaker("bk4809"), prof, logger=logger, record=True,
                            rows=rows)
    logger.close()
    assert res.completed, res.reason
    assert any(r["state"] == "fade" for r in rows) == fade
    raw = np.fromfile(logger.dir / "raw.f32", dtype=np.float32).reshape(-1, 2)[:, 1]
    first, second = ctl.steps
    between = raw[round(first["t_end_s"] * ctl.fs):round(second["t_start_s"] * ctl.fs)]
    overshoot = np.max(np.abs(between)) / second["ref_pk_g"]
    if fade:
        assert overshoot < 1.1
    else:
        assert overshoot > 1.3                     # the glide this test guards against
    assert_safe_stop(load_shaker("bk4809"), backend)


def test_ringdown(settings, tv51110, tmp_path):
    prof = write_profile(tmp_path, 'type = "stepped_sine"\n'
                         'breakpoints = [{ f_hz = 120.0, accel_g = 0.5 }]\n'
                         'frequencies_hz = [120.0, 300.0]\nsettle_s = 0.5\ndwell_s = 1.0\n'
                         'ringdown_s = 0.5\n')
    logger = RunLogger(tmp_path, "ring", SINE_STATUS_FIELDS)
    ctl, backend, res = run(settings, tv51110, prof, logger=logger)
    # written only after the stream: formatting it during the run can underflow the AO
    assert not (logger.dir / "ringdown_L1F1.csv").exists()
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


def test_open_loop_aborts(settings, tv51110, tmp_path):
    _, backend, res = run(settings, tv51110, write_profile(tmp_path, SWEEP),
                          sim_kwargs={"disconnect_after_s": 15.0})
    assert not res.completed and res.state == State.ABORTED
    assert "open loop" in res.reason or "amplitude error" in res.reason, res.reason
    assert_safe_stop(tv51110, backend)


ONE_SWEEP = SWEEP.replace("sweeps = 2", "sweeps = 1")


def test_open_loop_sweep_with_raw_record(settings, tv51110, tmp_path):
    """--open-loop: constant drive calibrated at 500 Hz, never corrected; the run completes
    although the response leaves the tolerance. --record: raw.f32 holds every sample."""
    logger = RunLogger(tmp_path, "open", SINE_STATUS_FIELDS)
    rows = []
    ctl, backend, res = run(settings, tv51110, write_profile(tmp_path, ONE_SWEEP), rows=rows,
                            logger=logger, record=True, open_loop_cal_hz=500.0)
    logger.close()
    assert res.completed, res.reason
    sweep = [r for r in rows if r["state"].startswith("sweep") and "err_db" in r]
    drive = np.array([r["drive_pk_v"] for r in sweep[4:]])     # frames after the ramp-in
    assert drive.max() / drive.min() < 1.001                     # never corrected
    err = np.array([r["err_db"] for r in sweep])
    assert np.max(np.abs(err)) > ctl.tol.abort_db                # outside the abort tolerance
    f = np.array([r["f_hz"] for r in sweep])
    assert np.min(np.abs(err[(f > 400) & (f < 600)])) < 0.5      # right at the calibration
    raw = np.fromfile(logger.dir / "raw.f32", dtype=np.float32).reshape(-1, 2)
    assert len(raw) == ctl.samples
    i = round(sweep[-1]["t_s"] * ctl.fs)                         # t_s = sample index / fs
    tail = raw[i - ctl.N:i, 1]
    assert np.max(np.abs(tail)) == pytest.approx(sweep[-1]["meas_pk_g"], rel=0.05)
    assert_safe_stop(tv51110, backend)


def test_open_loop_keeps_the_safety_checks(settings, tv51110, tmp_path):
    prof = write_profile(tmp_path, ONE_SWEEP)
    _, backend, res = run(settings, tv51110, prof, open_loop_cal_hz=500.0,
                          sim_kwargs={"disconnect_after_s": 15.0})
    assert not res.completed and "open loop" in res.reason, res.reason
    assert_safe_stop(tv51110, backend)
    # the uncorrected response would exceed the sensor range: stopped after the pretest
    narrow = dataclasses.replace(settings, sensor=dataclasses.replace(settings.sensor,
                                                                      range_g_pk=0.6))
    ctl, backend, res = run(narrow, tv51110, prof, open_loop_cal_hz=500.0)
    assert not res.completed and "open loop would reach" in res.reason, res.reason
    assert ctl.run_time_s == 0
    with pytest.raises(ValueError, match="open loop"):
        run(settings, tv51110, load_profile(PROFILES / "stepped_sine.toml"),
            open_loop_cal_hz=100.0)


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
