import dataclasses

import pytest

from vcactl.config import load_settings, load_shaker
from vcactl.profile import load_profile
from vcactl.safety import AiRange, AbortError, RuntimeMonitor, preflight, select_ai_range
from conftest import PROFILES, TEST_SENSOR, pin_rates


def test_bundled_profiles_pass(settings, shaker):
    for name in ("flat", "sloped", "wideband", "sine_sweep",
                 "stepped_sine", "ringdown"):
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


@pytest.mark.parametrize("f_hi,ok", [(43000.0, True), (44000.0, False)])
def test_daq_band_check(f_hi, ok, tmp_path):
    """USB-4431 at 96 kS/s without decimation: the band is the converter passband (43.5 kHz)."""
    st = pin_rates(load_settings(daq="usb4431", sensor=TEST_SENSOR), 96000.0, 1, 16384)
    p = tmp_path / "hf.toml"
    p.write_text(f"breakpoints = [{{ f_hz = 100.0, psd_g2_hz = 1e-6 }}, "
                 f"{{ f_hz = {f_hi}, psd_g2_hz = 1e-6 }}]\n")
    check = {c.name: c for c in preflight(load_profile(p), load_shaker("bk4809"), st).checks}
    assert check["profile f_hi <= DAQ band"].ok == ok


def test_runtime_monitor(settings, tv51110):
    m = RuntimeMonitor(settings, tv51110, AiRange(10.0, False, 0.0, 0.0))
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


def test_static_payload_limit(settings, flat_profile):
    import dataclasses
    from vcactl.config import load_shaker
    shaker = load_shaker("bk4809")             # static limit 4.9 kg
    heavy = dataclasses.replace(settings, safety=dataclasses.replace(settings.safety, payload_kg=5.0))
    report = preflight(flat_profile, shaker, heavy, level_db=-20)
    failed = {c.name for c in report.checks if not c.ok}
    assert "payload <= static max" in failed


@pytest.mark.parametrize("amp,failed", [
    ("accel_g = 46.0", "accel peak"),                     # TV 51110: 45 g peak
    ("accel_g = 40.0", "force peak (m_total*a)"),         # (0.23 + 0.1) kg * 40 g = 129 N > 100 N
    ("displacement_mm_pp = 14.0", "displacement p-p"),    # 13 mm p-p stroke
])
def test_sine_preflight_limits(settings, tv51110, tmp_path, amp, failed):
    import dataclasses
    heavy = dataclasses.replace(settings, safety=dataclasses.replace(settings.safety, payload_kg=0.1))
    p = tmp_path / "s.toml"
    p.write_text(f'type = "sine_sweep"\nbreakpoints = [{{ f_hz = 20.0, {amp} }}]\n'
                 'f_end_hz = 100.0\n')
    report = preflight(load_profile(p), tv51110, heavy)
    assert failed in {c.name for c in report.checks if not c.ok}


def test_stepped_sine_preflight_uses_highest_level(settings, tv51110, tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('type = "stepped_sine"\nbreakpoints = [{ f_hz = 100.0, accel_g = 30.0 }]\n'
                 'frequencies_hz = [100.0]\nlevels_db = [-6.0, 4.0]\n')
    report = preflight(load_profile(p), tv51110, settings)
    assert "accel peak" in {c.name for c in report.checks if not c.ok}     # 30 g + 4 dB = 47.5 g


def test_sensor_range_and_band(tv51110, flat_profile, tmp_path):
    from vcactl.config import load_settings
    st = pin_rates(load_settings(sensor="pcb356b18"))     # +/-5 g, 0.5-3000 Hz
    failed = {c.name for c in preflight(flat_profile, tv51110, st).checks if not c.ok}
    assert failed == {"accel peak (4.5 s) <= sensor",            # 4.45 g rms * 4.5
                      "AI peak (+3 dB) <= range"}                # 21 V at 1048 mV/g > 10 V
    p = tmp_path / "to5k.toml"
    p.write_text("breakpoints = [{ f_hz = 20.0, psd_g2_hz = 1e-5 }, "
                 "{ f_hz = 5000.0, psd_g2_hz = 1e-5 }]\n")
    failed = {c.name for c in preflight(load_profile(p), tv51110, st).checks if not c.ok}
    assert failed == {"profile f_hi <= sensor f_max"}
    p.write_text('type = "sine_sweep"\nbreakpoints = [{ f_hz = 20.0, accel_g = 6.0 }]\n'
                 'f_end_hz = 100.0\n')
    failed = {c.name for c in preflight(load_profile(p), tv51110, st).checks if not c.ok}
    assert failed == {"accel peak <= sensor range"}


def test_payload_includes_sensor_mass(settings, flat_profile):
    import dataclasses
    from vcactl.config import load_shaker
    shaker = load_shaker("bk4809")             # static limit 4.9 kg
    s = dataclasses.replace(settings, safety=dataclasses.replace(settings.safety, payload_kg=4.89))
    assert preflight(flat_profile, shaker, s, level_db=-20).ok
    s = dataclasses.replace(s, sensor=dataclasses.replace(s.sensor, mass_kg=0.025))
    failed = {c.name for c in preflight(flat_profile, shaker, s, level_db=-20).checks if not c.ok}
    assert failed == {"payload <= static max"}


def test_sensor_over_range_monitor(tv51110):
    from vcactl.config import load_settings
    m = RuntimeMonitor(load_settings(), tv51110,   # 972 mV/g, +/-5 g, 10 V AI range
                       AiRange(10.0, False, 0.0, 0.0))
    m.check_io(4.8, 0.0, 0.0)                      # 4.94 g
    with pytest.raises(AbortError, match="sensor over range"):
        m.check_io(4.9, 0.0, 0.0)                  # 5.04 g, below the AI overload limit


def test_select_ai_range_auto():
    from vcactl.config import load_settings
    st = load_settings()                       # USB-6211 (0.2, 1, 5, 10 V), PCB 352B 972 mV/g
    low_g = load_profile(PROFILES / "low_g.toml")   # 0.546 g rms: 2.46 g peak (4.5 s)
    ai = select_ai_range(low_g, st)
    assert ai.auto and ai.range_v == 5.0 and ai.peak_v == pytest.approx(2.388, rel=1e-3)
    assert select_ai_range(low_g, st, level_db=-12).range_v == 1.0     # 0.6 V * 1.41 / 0.98
    assert select_ai_range(low_g, st, level_db=-40).range_v == 0.2
    assert select_ai_range(low_g, st, level_db=6).range_v == 10.0
    manual = select_ai_range(low_g, st, level_db=-40, override=10.0)
    assert not manual.auto and manual.range_v == 10.0


def test_ai_range_preflight_check(tv51110):
    import dataclasses
    from vcactl.config import load_settings
    st = pin_rates(load_settings())
    low_g = load_profile(PROFILES / "low_g.toml")
    name = "AI peak (+3 dB) <= range"
    assert preflight(low_g, tv51110, st).ok                            # auto: 5 V
    failed = {c.name for c in preflight(low_g, tv51110, st,
                                        ai_range=select_ai_range(low_g, st, override=1.0)).checks
              if not c.ok}
    assert failed == {name}                                            # 3.6 V on 1 V
    st10 = dataclasses.replace(st, daq=dataclasses.replace(st.daq, ai_range_v=1.0))
    assert name in {c.name for c in preflight(low_g, tv51110, st10).checks if not c.ok}


def test_runtime_monitor_needs_range(tv51110):
    from vcactl.config import load_settings
    with pytest.raises(ValueError, match="auto"):
        RuntimeMonitor(load_settings(), tv51110)
    m = RuntimeMonitor(load_settings(), tv51110, AiRange(1.0, True, 0.3, 0.6))
    with pytest.raises(AbortError, match="automatically.*--ai-range"):
        m.check_io(0.99, 0.0, 0.0)
