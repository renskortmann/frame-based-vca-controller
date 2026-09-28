import math

import numpy as np
import pytest

from vcactl.config import ConfigError
from vcactl.profile import G, load_profile
from conftest import PROFILES


def test_flat_rms(flat_profile):
    assert flat_profile.accel_rms_g() == pytest.approx(math.sqrt(0.01 * 1980))


def test_slopes_resolved():
    p = load_profile(PROFILES / "example_sloped.toml")
    # +6 dB/oct over two octaves = +12 dB
    assert p.psd_g2_hz[1] == pytest.approx(0.002 * 10 ** 1.2)
    assert p.psd_g2_hz[2] == pytest.approx(p.psd_g2_hz[1])
    assert p.psd_g2_hz[3] == pytest.approx(p.psd_g2_hz[2] / 10 ** 0.6)
    # log-log interpolation at the geometric midpoint of the sloped segment
    assert p.psd([40.0])[0] == pytest.approx(0.002 * 10 ** 0.6)
    assert p.psd([10.0, 3000.0]).tolist() == [0.0, 0.0]


def test_exact_integrals_match_numeric():
    p = load_profile(PROFILES / "example_sloped.toml")
    f = np.geomspace(p.f_lo, p.f_hi, 200001)
    s = p.psd(f)
    assert p.accel_rms_g() == pytest.approx(math.sqrt(np.trapezoid(s, f)), rel=1e-5)
    v2 = np.trapezoid(s * G**2 / (2 * np.pi * f) ** 2, f)
    assert p.velocity_rms_m_s() == pytest.approx(math.sqrt(v2), rel=1e-5)
    d2 = np.trapezoid(s * G**2 / (2 * np.pi * f) ** 4, f)
    assert p.displacement_rms_m() == pytest.approx(math.sqrt(d2), rel=1e-5)


@pytest.mark.parametrize("body,match", [
    ("breakpoints = [{ f_hz = 20.0, psd_g2_hz = 0.01 }]", "at least two"),
    ("breakpoints = [{ f_hz = 20.0, slope_db_oct = 3.0 }, { f_hz = 40.0, psd_g2_hz = 0.01 }]",
     "first breakpoint"),
    ("breakpoints = [{ f_hz = 40.0, psd_g2_hz = 0.01 }, { f_hz = 20.0, psd_g2_hz = 0.01 }]",
     "increasing"),
    ("breakpoints = [{ f_hz = 20.0, psd_g2_hz = 0.01, slope_db_oct = 1.0 }, { f_hz = 40.0, psd_g2_hz = 0.01 }]",
     "exactly one"),
])
def test_invalid_profiles(tmp_path, body, match):
    p = tmp_path / "p.toml"
    p.write_text(body + "\n")
    with pytest.raises(ConfigError, match=match):
        load_profile(p)
