"""Closed-loop tests against the simulated shaker."""

import dataclasses

import numpy as np
import pytest

from vcactl.config import load_settings
from vcactl.controller import Controller, State
from vcactl.daq.base import DaqFault
from vcactl.daq.sim import SimulatedDaq
from vcactl.logger import RunLogger


def run(settings, shaker, profile, *, duration=10.0, sim_kwargs=None, rows=None,
        pretest_only=False, **kw):
    backend = SimulatedDaq(settings, shaker, seed=1, **(sim_kwargs or {}))
    ctl = Controller(settings, shaker, profile, backend, duration_s=duration, seed=2,
                     on_status=(rows.append if rows is not None else None), **kw)
    return ctl, backend, ctl.run(pretest_only=pretest_only)


def ao_stream(backend):
    return np.concatenate(backend.written)


def assert_safe_stop(shaker, backend):
    ao = ao_stream(backend)
    assert np.max(np.abs(ao)) <= shaker.max_drive_v + 1e-12
    assert np.all(ao[-1000:] == 0.0), "AO stream must end in zeros"
    assert backend.stopped_at_zero


def test_converges_on_both_shakers(settings, shaker, wideband_profile):
    rows = []
    ctl, backend, res = run(settings, shaker, wideband_profile, rows=rows)
    assert res.completed and res.state == State.DONE, res.reason
    assert abs(res.rms_err_db) < 0.5
    ev = ctl.last_eval
    assert ev.lines_alarm <= 0.05 * ev.n_lines
    levels = [r["level_db"] for r in rows if r["state"] == "ramp"]
    assert levels[0] == pytest.approx(settings.control.start_level_db)
    assert sorted(set(levels)) == [-12.0, -9.0, -6.0, -3.0, 0.0]
    assert_safe_stop(shaker, backend)


def test_reduced_target_level(settings, tv51110, flat_profile):
    _, _, res = run(settings, tv51110, flat_profile, target_level_db=-6.0)
    assert res.completed
    assert res.ref_rms_g == pytest.approx(flat_profile.accel_rms_g() / 2, rel=0.01)  # discrete lines vs. exact integral
    assert abs(res.rms_err_db) < 0.5


def test_nonlinear_shaker_is_equalized(settings, tv51110, wideband_profile):
    shaker = dataclasses.replace(tv51110, sim=dataclasses.replace(tv51110.sim, cubic_coeff=0.005))
    ctl, _, res = run(settings, shaker, wideband_profile, duration=20.0)
    assert res.completed, res.reason
    assert abs(res.rms_err_db) < 0.5
    assert ctl.last_eval.lines_alarm <= 0.05 * ctl.last_eval.n_lines


def test_sensor_disconnect_aborts(settings, tv51110, flat_profile):
    ctl, backend, res = run(settings, tv51110, flat_profile, duration=30.0,
                            sim_kwargs={"disconnect_after_s": 12.0})
    assert not res.completed and res.state == State.ABORTED
    assert "open loop" in res.reason
    assert_safe_stop(tv51110, backend)


def test_no_response_fails_pretest(settings, tv51110, flat_profile):
    _, backend, res = run(settings, tv51110, flat_profile, sim_kwargs={"disconnect_after_s": 0.0})
    assert not res.completed and "pretest" in res.reason
    assert_safe_stop(tv51110, backend)


def test_insufficient_drive_capability_aborts_after_pretest(settings, tv51110, flat_profile):
    sh = dataclasses.replace(tv51110, max_drive_rms_v=0.2, max_drive_v=0.6)
    ctl, backend, res = run(settings, sh, flat_profile)
    assert not res.completed and "drive" in res.reason
    assert ctl.run_time_s == 0
    assert_safe_stop(sh, backend)


def test_preflight_failure_outputs_nothing(settings, tv51110, wideband_profile):
    backend = SimulatedDaq(settings, tv51110)
    res = Controller(settings, tv51110, wideband_profile, backend, target_level_db=15).run()
    assert not res.completed and "pre-flight" in res.reason
    assert backend.written == []


def test_operator_stop_ramps_down(settings, tv51110, flat_profile):
    holder = {}

    def on_status(row):
        if row["state"] == "run" and row["run_time_s"] > 2.0:
            holder["ctl"].stop_requested = True

    backend = SimulatedDaq(settings, tv51110, seed=1)
    ctl = Controller(settings, tv51110, flat_profile, backend, duration_s=60, on_status=on_status)
    holder["ctl"] = ctl
    res = ctl.run()
    assert res.state == State.ABORTED and "operator" in res.reason
    assert res.run_time_s < 3.0
    ao = ao_stream(backend)
    # ramp-down: the rms of the final non-zero half second decreases towards zero
    nz = np.flatnonzero(ao)
    tail = ao[nz[-1] - 50000:nz[-1] + 1]
    first, last = np.std(tail[:10000]), np.std(tail[-10000:])
    assert last < 0.5 * first
    assert_safe_stop(tv51110, backend)


def test_daq_fault_stops_immediately(settings, tv51110, flat_profile):
    class FaultyDaq(SimulatedDaq):
        reads = 0

        def read(self, n):
            self.reads += 1
            if self.reads == 60:
                raise DaqFault("device removed")
            return super().read(n)

    backend = FaultyDaq(settings, tv51110, seed=1)
    res = Controller(settings, tv51110, flat_profile, backend, duration_s=30).run()
    assert not res.completed and "DAQ fault" in res.reason
    assert backend.stopped_at_zero


def test_pretest_only(settings, tv51110, flat_profile):
    ctl, backend, res = run(settings, tv51110, flat_profile, pretest_only=True)
    assert res.completed and res.reason == "pretest completed"
    assert ctl.pretest_info["coherence_mean"] > 0.9
    assert_safe_stop(tv51110, backend)


def test_logging(settings, tv51110, flat_profile, tmp_path):
    logger = RunLogger(tmp_path, "test")
    _, _, res = run(settings, tv51110, flat_profile, duration=6.0, logger=logger)
    logger.close()
    files = {p.name for p in logger.dir.iterdir()}
    assert {"status.csv", "pretest_frf.csv", "psd_final.csv", "psd_000005s.csv"} <= files
    data = np.genfromtxt(logger.dir / "psd_final.csv", delimiter=",", names=True)
    band = data["ref_g2_hz"] > 0
    ratio_db = 10 * np.log10(data["meas_g2_hz"][band] / data["ref_g2_hz"][band])
    assert np.mean(np.abs(ratio_db) < 3) > 0.95


@pytest.mark.parametrize("daq", ["usb4431", "pxie4468"])
def test_converges_on_dsa_devices(daq, tv51110, wideband_profile):
    """DSA devices add ~100 samples of converter filter delay; the controller compensates it."""
    settings = load_settings(daq=daq)
    ctl, backend, res = run(settings, tv51110, wideband_profile)
    assert res.completed and res.state == State.DONE, res.reason
    assert abs(res.rms_err_db) < 0.5
    assert ctl.pretest_info["coherence_mean"] > 0.95
    assert_safe_stop(tv51110, backend)
