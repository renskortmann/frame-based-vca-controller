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


# ---------------------------------------------------------------------------- sine profiles

def test_sine_profiles_load():
    sweep = load_profile(PROFILES / "example_sine_sweep.toml")
    assert sweep.kind == "sine_sweep" and (sweep.f_lo, sweep.f_hi) == (20.0, 2000.0)
    assert sweep.sweep_duration_s() == pytest.approx(math.log2(100) * 60)
    stepped = load_profile(PROFILES / "example_stepped_sine.toml")
    assert stepped.kind == "stepped_sine" and stepped.levels_db == (-12.0, -6.0, 0.0)
    ring = load_profile(PROFILES / "example_ringdown.toml")
    assert ring.frequencies_hz == (120.0,) and ring.ringdown_s == 2.0
    assert ring.tolerance.alarm_db == 1.0 and ring.tolerance.abort_db == 3.0


def test_sine_amplitude_units(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('type = "sine_sweep"\nbreakpoints = [{ f_hz = 10.0, displacement_mm_pp = 2.0 },'
                 '{ f_hz = 40.0, displacement_mm_pp = 2.0 }, { f_hz = 100.0, velocity_m_s = 0.1 }]\n')
    prof = load_profile(p)
    f = np.array([10.0, 17.3, 25.0, 40.0])
    # two displacement breakpoints: exactly constant displacement in between (a ~ f^2)
    assert prof.displacement_pp_mm(f) == pytest.approx(np.full(4, 2.0))
    assert prof.velocity_pk_m_s([100.0])[0] == pytest.approx(0.1)
    assert prof.accel_pk_g([100.0])[0] == pytest.approx(0.1 * 2 * math.pi * 100 / G)


def test_points_per_octave(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('type = "stepped_sine"\nbreakpoints = [{ f_hz = 100.0, accel_g = 1.0 }]\n'
                 'f_start_hz = 100.0\nf_end_hz = 450.0\npoints_per_octave = 2\ndirection = "down"\n')
    freqs = load_profile(p).frequencies_hz
    assert freqs == pytest.approx([450.0, 400.0, 100 * 2 ** 1.5, 200.0, 100 * 2 ** 0.5, 100.0])


@pytest.mark.parametrize("body,match", [
    ('type = "sine"\nbreakpoints = [{ f_hz = 10.0, accel_g = 1.0 }]', "type"),
    ('type = "sine_sweep"\nbreakpoints = [{ f_hz = 10.0, accel_g = 1.0 }]\ndwell_s = 2.0', "dwell_s"),
    ('type = "sine_sweep"\nbreakpoints = [{ f_hz = 10.0 }]', "exactly one"),
    ('type = "sine_sweep"\nbreakpoints = [{ f_hz = 10.0, accel_g = 1.0, velocity_m_s = 1.0 }]',
     "exactly one"),
    ('type = "sine_sweep"\nbreakpoints = [{ f_hz = 10.0, accel_g = 1.0 }]\nf_start_hz = 50.0\n'
     'f_end_hz = 50.0', "differ"),
    ('type = "stepped_sine"\nbreakpoints = [{ f_hz = 10.0, accel_g = 1.0 }]\n'
     'frequencies_hz = [50.0]\npoints_per_octave = 3', "not both"),
    ('type = "stepped_sine"\nbreakpoints = [{ f_hz = 10.0, accel_g = 1.0 }]\n'
     'frequencies_hz = [50.0]\nringdown_s = -1.0', "ringdown_s"),
    ('type = "stepped_sine"\nbreakpoints = [{ f_hz = 10.0, accel_g = 1.0 }]\n'
     'frequencies_hz = [50.0]\ndirection = "sideways"', "direction"),
])
def test_invalid_sine_profiles(tmp_path, body, match):
    p = tmp_path / "bad.toml"
    p.write_text(body + "\n")
    with pytest.raises(ConfigError, match=match):
        load_profile(p)
