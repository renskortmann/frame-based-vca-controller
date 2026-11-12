"""Profile notches: the tip velocity predicted from a transmissibility must end under the limit."""

import numpy as np
import pytest

from conftest import ROOT
from vcactl.analysis.notch import notch_profile, predicted_rms
from vcactl.profile import load_profile

G = 9.80665


def tip_frf():
    """Tip velocity (mm/s) per drive-point acceleration (g): two held modes, zeta 0.5 %."""
    f = np.arange(20.0, 4000.0, 0.05)
    t = np.ones_like(f)
    for fn, gain in ((33.0, 1.0), (210.0, 0.2)):
        r = f / fn
        z = 0.005
        t = t + gain * np.sqrt((1 + (2 * z * r) ** 2) / ((1 - r ** 2) ** 2 + (2 * z * r) ** 2))
    return f, t * G / (2 * np.pi * f) * 1000


def test_notches_bring_the_tip_under_the_limit():
    f, t = tip_frf()
    res = notch_profile([20.0, 4000.0], [1e-5, 1e-5], f, t, limit_pk=15.5, crest=4.0)
    assert res.rms_before > 15.5 / 4 and res.rms_after <= 15.5 / 4
    assert predicted_rms(res.breakpoints, f, t) <= 15.5 / 4 * 1.001
    centres = [(n.f_lo + n.f_hi) / 2 for n in res.notches]
    assert any(abs(c - 33.0) < 5 for c in centres)
    assert all(n.f_hi - n.f_lo >= 3 * 6.25 - 0.1 for n in res.notches)   # at least 3 lines
    fs = [p[0] for p in res.breakpoints]
    assert fs == sorted(fs) and len(set(fs)) == len(fs)
    assert fs[0] == 20.0 and fs[-1] == 4000.0


def test_no_notch_when_under_the_limit():
    f, t = tip_frf()
    res = notch_profile([20.0, 4000.0], [1e-12, 1e-12], f, t, limit_pk=15.5)
    assert res.notches == [] and res.breakpoints == [(20.0, 1e-12), (4000.0, 1e-12)]


def test_tool_writes_a_loadable_profile(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("notch_profile", ROOT / "tools" / "notch_profile.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    f, t = tip_frf()
    csv = tmp_path / "tip.csv"
    np.savetxt(csv, np.column_stack([f, t]), delimiter=",", header="f_hz,mag", comments="")
    out = tmp_path / "notched.toml"
    assert tool.main(["--profile", str(ROOT / "config/test_profiles/cantilever_random_periodic.toml"),
                      "--frf", str(csv), "--level", "20", "--out", str(out)]) == 0
    p = load_profile(out)
    assert p.drive == "periodic" and p.period_s == 20.0 and p.tolerance.abort_db == 6.0
    assert predicted_rms([(x, s * 100) for x, s in zip(p.f_hz, p.psd_g2_hz)], f, t) <= 15.5 / 4 * 1.001
