"""``vcactl tap``: triggered records on the hammer channel, hit checks and the averaged H1."""

import json
import math

import numpy as np
import pytest
from scipy.signal import find_peaks

from vcactl.cli import main
from vcactl.config import ConfigError, load_channels, load_settings, load_shaker
from vcactl.daq.sim import SimulatedDaq, TapSim
from vcactl.record import record_settings
from vcactl.tap import TapProfile, TapTest, load_tap_profile

MODES = ((120.0, 0.02, 0.02), (450.0, 0.01, 0.01))


def tap_settings(name="cantilever_tap_shaker"):
    return record_settings(load_channels(name, load_settings(daq="usb4431")))


def make(profile=None, sim=None, settings=None, **kw):
    st = settings or tap_settings()
    p = profile or TapProfile(record_s=0.5, pretrigger_s=0.01, hits=3, max_hits=8,
                              exp_window_end=1.0, f_max_hz=2000.0)
    sim = sim or TapSim(interval_s=0.7, modes=MODES)
    backend = SimulatedDaq(st, load_shaker("bk4809"), seed=1, tap=sim)
    return TapTest(st, backend, p, **kw)


def test_profile(tmp_path):
    p = load_tap_profile("config/test_profiles/tap_beam.toml")
    assert p.record_s == 4.0 and p.hits == 5
    bad = tmp_path / "bad.toml"
    bad.write_text('type = "tap"\nrecord_s = 1.0\npretrigger_s = 1.0\n')
    with pytest.raises(ConfigError, match="pretrigger_s"):
        load_tap_profile(bad)
    bad.write_text('type = "tap"\nhits = 5\nmax_hits = 2\n')
    with pytest.raises(ConfigError, match="max_hits"):
        load_tap_profile(bad)


def test_needs_a_hammer():
    st = record_settings(load_channels("cantilever_driven", load_settings(daq="usb4431")))
    with pytest.raises(ConfigError, match="role hammer"):
        TapTest(st, None, TapProfile())


def test_h1_peaks_at_the_modes():
    tap = make()
    assert tap.run() == "completed"
    assert tap.accepted == 3 and len(tap.hits) == 3
    assert all(abs(h.peak_n - 50.0) < 0.5 for h in tap.hits)
    h1, coh = tap.frf()
    f = tap.freqs
    mag = np.abs(h1[:, 0])
    p, _ = find_peaks(mag, prominence=0.2 * mag.max())
    np.testing.assert_allclose(f[p], [120.0, 450.0], atol=f[1])
    assert np.all(coh[p, 0] > 0.999)
    # the first response channel (index 1) gets the modes divided by 2: peak gain / (2 zeta)
    assert mag[p[0]] == pytest.approx(0.02 / 2 / (2 * 0.02), rel=0.1)


def test_double_hits_are_rejected():
    tap = make(sim=TapSim(interval_s=0.7, modes=MODES, double_every=2))
    tap.run()
    assert [h.double_hit for h in tap.hits][:4] == [False, True, False, True]
    assert [h.accepted for h in tap.hits][:4] == [True, False, True, False]
    assert tap.accepted == 3


def test_overloads_are_rejected():
    # 600 N x 22.7 mV/N = 13.6 V on the +/-10 V range
    tap = make(sim=TapSim(interval_s=0.7, modes=MODES, peak_n=600.0))
    reason = tap.run()
    assert tap.accepted == 0 and "hammer" in tap.hits[0].overload
    assert reason == "stopped after 8 hits"


def test_confirm_in_a_thread():
    answers = iter([False, True, True, True])
    tap = make(confirm=lambda hit: next(answers), confirm_async=True)
    tap.run()
    assert [h.accepted for h in tap.hits] == [False, True, True, True]


def test_exponential_window():
    tap = make(TapProfile(record_s=0.5, pretrigger_s=0.01, hits=1, exp_window_end=0.01))
    assert tap.tau_w == pytest.approx(0.49 / math.log(100))
    force, exp = tap.windows
    assert exp[tap.n_pre - 1] == 1.0 and exp[-1] == pytest.approx(0.01, rel=1e-3)
    assert force[0] == 1.0 and force[-1] == 0.0


def test_cli_tap(tmp_path):
    prof = tmp_path / "tap.toml"
    prof.write_text('type = "tap"\nrecord_s = 0.5\npretrigger_s = 0.01\nhits = 2\n'
                    'f_max_hz = 2000.0\n')
    assert main(["tap", "--sim", "--yes", "--daq", "usb4431", "--channels",
                 "cantilever_tap_beam", "--profile", str(prof), "--log-dir", str(tmp_path / "runs"),
                 "--seed", "1"]) == 0
    run = next((tmp_path / "runs").iterdir())
    meta = json.loads((run / "meta.json").read_text())
    assert meta["accepted_hits"] == 2
    cols = meta["hits"]["columns"]
    assert cols == ["hammer_N", "drive_point_g", "base_g"]
    rec = np.fromfile(run / "hits.f32", dtype=np.float32)
    assert rec.size == 2 * meta["hits"]["record_samples"] * len(cols)
    head = (run / "tap_frf.csv").read_text().splitlines()[0]
    assert head.startswith("f_hz,force_n2,drive_point_mag_g_per_N,drive_point_phase_deg")
    assert "hit,t_s,peak_n,double_hit,overload,accepted" in (run / "hits.csv").read_text()
