"""Periodic random drive: the generator, the profile keys and the controller at full level."""

import dataclasses
import json

import numpy as np
import pytest

from conftest import PROFILES, pin_rates
from vcactl.cli import main
from vcactl.config import ConfigError, load_settings, load_shaker
from vcactl.controller import Controller
from vcactl.daq.sim import SimulatedDaq
from vcactl.drive import PeriodicDriveGenerator, flat_psd, period_samples
from vcactl.logger import RunLogger
from vcactl.profile import load_profile

N, FS = 1024, 10240.0


def test_period_samples():
    assert period_samples(20.0, 51200.0, 8192) == 2 ** 20
    assert period_samples(0.01, 51200.0, 8192) == 8192


def test_generator_repeats_and_changes_only_at_boundaries():
    freqs = np.fft.rfftfreq(N, 1 / FS)
    psd = flat_psd(freqs, 100.0, 2000.0, 0.5)
    g = PeriodicDriveGenerator(N, FS, 4 * N, np.random.default_rng(1))
    first = np.concatenate([g.next_block(psd) for _ in range(8)])
    second = np.concatenate([g.next_block(psd) for _ in range(8)])
    np.testing.assert_array_equal(first, second)
    assert g.periods == 2 and not g.changed and g.change_db == 0.0
    assert np.std(first) == pytest.approx(0.5, rel=0.02)
    # a new PSD mid-period is ignored until the next period
    a = g.next_block(psd)
    b = g.next_block(4 * psd)
    np.testing.assert_array_equal(np.concatenate([a, b]), first[:N])
    for _ in range(6):
        g.next_block(4 * psd)
    third = np.concatenate([g.next_block(4 * psd) for _ in range(8)])
    assert g.changed and g.change_db == pytest.approx(6.02, abs=0.01)
    np.testing.assert_allclose(third, 2 * first, atol=1e-12)


def test_generator_rejects_bad_periods():
    with pytest.raises(ValueError):
        PeriodicDriveGenerator(N, FS, N // 2)
    with pytest.raises(ValueError):
        PeriodicDriveGenerator(N, FS, N + 100)


def test_profile_keys(tmp_path):
    p = load_profile(PROFILES / "cantilever_random_periodic.toml")
    assert p.drive == "periodic" and p.period_s == 20.0
    bad = tmp_path / "bad.toml"
    bad.write_text('drive = "burst"\nbreakpoints = [{ f_hz = 20.0, psd_g2_hz = 1e-4 },'
                   '{ f_hz = 200.0, psd_g2_hz = 1e-4 }]\n')
    with pytest.raises(ConfigError, match="drive must be"):
        load_profile(bad)


def periodic_profile(tmp_path, period_s=0.5):
    text = (PROFILES / "low_g.toml").read_text()
    path = tmp_path / "periodic.toml"
    path.write_text(f'drive = "periodic"\nperiod_s = {period_s}\n' + text)
    return load_profile(path)


def test_controller_runs_periodic_drive(tmp_path):
    st = pin_rates(load_settings(daq="usb4431"), 51200, 1, 8192)
    # a 1 s period holds only 16 frames: a wide deadband for the short test
    st = dataclasses.replace(st, control=dataclasses.replace(st.control, periodic_deadband_db=3.0))
    shaker = load_shaker("bk4809")
    profile = periodic_profile(tmp_path, period_s=1.0)
    logger = RunLogger(tmp_path, "periodic")
    ctl = Controller(st, shaker, profile, SimulatedDaq(st, shaker, seed=1), duration_s=12.0,
                     seed=2, logger=logger, record=True, target_level_db=-12)
    res = ctl.run()
    logger.close()
    assert res.completed, res.reason
    assert abs(res.rms_err_db) < 0.5
    n = ctl.period_n
    assert n == 65536
    ev = ctl.period_events
    assert len(ev) >= 8 and all(e["period_samples"] == n for e in ev)
    ps = [e for e in ev if e["event"] == "period_start"]
    assert all(b["raw_sample"] - a["raw_sample"] == n for a, b in zip(ps, ps[1:]))
    assert (logger.dir / "events.csv").read_text().startswith(
        "event,period,raw_sample,t_s,changed,change_db,period_samples")
    raw = np.fromfile(logger.dir / "raw.f32", dtype=np.float32).reshape(-1, 2)
    drive = raw[:, 0]
    # (the first period starts with the crossfade from the random drive, the last one ends with
    # the ramp-down)
    starts = [e for e in ev if e["event"] == "period_start"]
    end = next(e["raw_sample"] for e in ev if e["event"] == "rampdown")
    steady = [e for e in starts[2:] if not e["changed"] and e["raw_sample"] + n <= end]
    assert len(steady) >= 3
    for e in steady:                 # an unchanged period repeats the one before exactly
        k = e["raw_sample"]
        np.testing.assert_array_equal(drive[k:k + n], drive[k - n:k])
        assert e["change_db"] == 0.0


def test_cli_meta_has_the_period_and_lag(tmp_path):
    prof = tmp_path / "p.toml"
    prof.write_text('drive = "periodic"\nperiod_s = 0.5\n' + (PROFILES / "low_g.toml").read_text())
    assert main(["run", "--sim", "--record", "--yes", "--seed", "1", "--log-dir",
                 str(tmp_path / "runs"), "--shaker", "bk4809", "--daq", "usb4431", "--profile",
                 str(prof), "--duration", "2", "--level", "-12"]) == 0
    meta = json.loads((next((tmp_path / "runs").iterdir()) / "meta.json").read_text())
    assert meta["periodic"]["period_samples"] == 32768
    assert meta["raw"]["response_lag_samples"] > 0
