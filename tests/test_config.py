import dataclasses

import pytest

from conftest import ROOT
from vcactl.config import ConfigError, load_shaker, load_settings, validate_setup, validate_settings


def test_bundled_configs_load(settings):
    s1, s2 = load_shaker("tv51110"), load_shaker("TV52110")
    assert s1.accel_random_rms_g == 30 and s2.accel_random_rms_g == 25
    assert s1.force_random_rms_n == 70 and s2.force_random_rms_n == 50
    assert settings.fs_control_hz == 25000


def test_unknown_key_rejected(tmp_path):
    p = tmp_path / "settings.toml"
    p.write_text("[daq]\ndevcie = 'Dev1'\n")
    with pytest.raises(ConfigError, match="devcie"):
        load_settings(p)


def test_wrong_type_rejected(tmp_path):
    p = tmp_path / "settings.toml"
    p.write_text("[safety]\npayload_kg = 'heavy'\n")
    with pytest.raises(ConfigError, match="payload_kg"):
        load_settings(p)


@pytest.mark.parametrize("section,field,value", [
    ("daq", "ai_range_v", 2.0),
    ("daq", "fs_io_hz", 300000.0),
    ("daq", "ai_terminal_config", "XYZ"),
    ("control", "correction_gain", 0.0),
])
def test_settings_validation(settings, section, field, value):
    bad = dataclasses.replace(settings, **{section: dataclasses.replace(getattr(settings, section),
                                                                      **{field: value})})
    with pytest.raises(ConfigError):
        validate_settings(bad)


def test_laser_settings_load(laser_settings):
    sensor = load_settings(ROOT / "config" / "settings_laser.toml").sensor
    assert sensor.type == "displacement" and sensor.mm_per_v != 0
    assert laser_settings.daq.model == "NI USB-6211"
    assert laser_settings.fs_control_hz == 5000


@pytest.mark.parametrize("fields,match", [
    ({"type": "velocity"}, "sensor.type"),
    ({"mm_per_v": 0.0}, "mm_per_v"),
    ({"range_min_mm": 10.0, "range_max_mm": 0.0}, "range_min_mm"),
    ({"f_max_hz": 0.0}, "f_max_hz"),
])
def test_sensor_validation(laser_settings, fields, match):
    bad = dataclasses.replace(laser_settings,
                              sensor=dataclasses.replace(laser_settings.sensor, **fields))
    with pytest.raises(ConfigError, match=match):
        validate_settings(bad)


def test_missing_shaker(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_shaker("tv99999", tmp_path)


def test_bk4809_profile_loads():
    s = load_shaker("bk4809")
    assert s.moving_mass_kg == 0.06 and s.displacement_pp_mm == 8.0
    assert s.f_max_hz == 20000.0 and s.sim.bl_n_per_a == 6.4


def test_tira_bda120_full_power_input():
    # datasheet: 3.5 V peak (1 kHz sine) for full power
    for key in ("tv51110", "tv52110"):
        s = load_shaker(key)
        assert s.amp_input_full_v == pytest.approx(3.5 / 2 ** 0.5, abs=0.01)
        assert s.max_drive_rms_v <= s.amp_input_full_v


@pytest.mark.parametrize("key,limit", [("tv51110", 5.3), ("tv52110", 10.0), ("bk4809", 4.9),
                                       ("bk4801_4812", 13.5)])
def test_static_payload_limits(key, limit):
    s = load_shaker(key)
    assert s.payload_static_max_kg == limit
    # cross-check the derived limits: the sag at the limit must not exceed the half-stroke
    sag_mm = limit * 9.80665 / s.suspension_stiffness_n_per_mm
    assert sag_mm <= s.displacement_pp_mm / 2 + 0.1


def test_bk4801_4812_profile_loads():
    s = load_shaker("bk4801_4812")
    assert s.moving_mass_kg == 0.454 and s.force_sine_peak_n == 445.0
    assert s.armature_resonance_hz == 7200.0 and s.velocity_peak_m_s == 1.14
    assert s.max_drive_rms_v <= s.amp_input_full_v == 1.57


def test_bk4809_drive_limits_match_amp():
    sh = load_shaker("bk4809")
    assert sh.max_drive_rms_v <= sh.amp_input_full_v == 1.0
    assert sh.max_drive_v == 2.5


@pytest.mark.parametrize("daq", ["usb6211", "usb4431", "pxie4468"])
@pytest.mark.parametrize("shaker", ["tv51110", "tv52110", "bk4809", "bk4801_4812"])
def test_daq_profiles_load(daq, shaker):
    st = load_settings(daq=daq)
    validate_setup(st, load_shaker(shaker))
    assert st.daq.limits.antialias_filter == (daq != "usb6211")


def test_dsa_profiles():
    usb4431, pxie4468 = load_settings(daq="usb4431").daq, load_settings(daq="pxie4468").daq
    assert usb4431.ao_range_v == 3.5 and usb4431.fs_io_hz == 51200
    assert pxie4468.limits.ai_ranges_v[0] == 0.316 and pxie4468.limits.fs_max_hz == 200000


@pytest.mark.parametrize("daq,field,value", [
    ("usb4431", "fs_io_hz", 50000.0),         # not an AO update rate
    ("usb4431", "ai_range_v", 5.0),
    ("usb4431", "ai_terminal_config", "DIFF"),
    ("usb4431", "iepe_current_ma", 4.0),
    ("usb4431", "ao_terminal_config", "DIFF"),
    ("pxie4468", "fs_io_hz", 250000.0),       # AO max 200 kS/s
    ("pxie4468", "ao_range_v", 5.0),
    ("usb6211", "fs_io_hz", 30000.0),         # 20 MHz / 30 kHz is not an integer
    ("usb6211", "decimation", 1),             # no anti-aliasing filter
    ("usb6211", "ai_coupling", "AC"),
])
def test_daq_validation(daq, field, value):
    settings = load_settings(daq=daq)
    bad = dataclasses.replace(settings, daq=dataclasses.replace(settings.daq, **{field: value}))
    with pytest.raises(ConfigError, match=field):
        validate_settings(bad)


def test_iepe_requires_ac_coupling():
    settings = load_settings(daq="pxie4468")
    bad = dataclasses.replace(settings, daq=dataclasses.replace(
        settings.daq, iepe_current_ma=4.0, ai_coupling="DC"))
    with pytest.raises(ConfigError, match="AC"):
        validate_settings(bad)


def test_max_drive_limited_by_ao_range(tv51110):
    # the TV 51110 allows 3.5 V peak, the USB-4431 AO range is +/-3.5 V
    settings = load_settings(daq="usb4431")
    with pytest.raises(ConfigError, match="ao_range_v"):
        validate_setup(settings, dataclasses.replace(tv51110, max_drive_v=4.0))


@pytest.mark.parametrize("field,value,match", [
    ("max_drive_rms_v", 4.0, "max_drive_v"),       # rms above peak clip
    ("max_drive_rms_v", 1.2, "amp_input_full_v"),  # above the BK 2718 full-rating input (1 V)
])
def test_shaker_drive_limits_validated(tmp_path, field, value, match):
    text = open("config/shakers/bk4809.toml").read()
    lines = [f"{field} = {value}" if l.startswith(field + " ") else l for l in text.splitlines()]
    p = tmp_path / "bk.toml"
    p.write_text("\n".join(lines))
    with pytest.raises(ConfigError, match=match):
        load_shaker(str(p))


def test_pretest_drive_limited_by_shaker(settings, tv51110):
    with pytest.raises(ConfigError, match="pretest"):
        validate_setup(settings, dataclasses.replace(tv51110, max_drive_rms_v=0.01,
                                                    max_drive_v=0.03))


def test_settings_daq_override(tmp_path):
    p = tmp_path / "settings.toml"
    p.write_text("[daq]\ndevice = 'Dev3'\nai_channel = 'ai2'\n")
    st = load_settings(p, "usb4431")
    assert st.daq.device == "Dev3" and st.daq.ai_channel == "ai2" and st.daq.model == "NI USB-4431"
    p.write_text("[daq.limits]\nfs_max_hz = 1e6\n")
    with pytest.raises(ConfigError, match="limits"):
        load_settings(p, "usb4431")


def test_missing_daq_profile():
    with pytest.raises(ConfigError, match="not found"):
        load_settings(daq="usb9999")
