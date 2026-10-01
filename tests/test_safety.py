import pytest

from vcactl.profile import load_profile
from vcactl.safety import AbortError, RuntimeMonitor, preflight
from conftest import PROFILES


def test_bundled_profiles_pass(settings, shaker):
    for name in ("example_flat", "example_sloped", "example_wideband"):
        assert preflight(load_profile(PROFILES / f"{name}.toml"), shaker, settings).ok


def test_accel_limit_tv52110(settings, wideband_profile):
    from vcactl.config import load_shaker
    # 5.91 g rms at +13 dB = 26.4 g rms > 25 g rms limit of the TV 52110
    report = preflight(wideband_profile, load_shaker("tv52110"), settings, level_db=13)
    failed = {c.name for c in report.checks if not c.ok}
    assert not report.ok and "accel rms" in failed


def test_displacement_limit(settings, tv51110, tmp_path):
    p = tmp_path / "lowfreq.toml"
    p.write_text("breakpoints = [{ f_hz = 15.0, psd_g2_hz = 2.0 }, { f_hz = 50.0, psd_g2_hz = 2.0 }]\n")
    report = preflight(load_profile(p), tv51110, settings)
    assert any(c.name.startswith("displacement") and not c.ok for c in report.checks)


def test_band_beyond_shaker(settings, tv51110, tmp_path):
    p = tmp_path / "hf.toml"
    p.write_text("breakpoints = [{ f_hz = 100.0, psd_g2_hz = 0.001 }, { f_hz = 9000.0, psd_g2_hz = 0.001 }]\n")
    assert not preflight(load_profile(p), tv51110, settings).ok


def test_runtime_monitor(settings, tv51110):
    m = RuntimeMonitor(settings, tv51110)
    m.check_io(1.0, 0.5, 0.0)
    with pytest.raises(AbortError, match="overload"):
        m.check_io(9.9, 0.5, 0.0)
    with pytest.raises(AbortError, match="drive rms"):
        m.check_io(1.0, 2.0, 0.0)
    with pytest.raises(AbortError, match="clipping"):
        m.check_io(1.0, 0.5, 0.05)
    with pytest.raises(AbortError, match="shaker limit"):
        m.check_response(31.0)
    m.check_open_loop(0.01, 1.0)
    m.check_open_loop(0.01, 1.0)
    with pytest.raises(AbortError, match="open loop"):
        m.check_open_loop(0.01, 1.0)
