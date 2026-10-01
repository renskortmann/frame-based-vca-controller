import dataclasses

import pytest

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


def test_missing_shaker(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_shaker("tv99999", tmp_path)


def test_bk4809_profile_loads():
    s = load_shaker("bk4809")
    assert s.moving_mass_kg == 0.06 and s.displacement_pp_mm == 8.0
    assert s.f_max_hz == 20000.0 and s.sim.bl_n_per_a == 6.4


def test_bk4809_drive_limits_match_amp():
    sh = load_shaker("bk4809")
    assert sh.max_drive_rms_v <= sh.amp_input_full_v == 1.0
    assert sh.max_drive_v == 2.5


@pytest.mark.parametrize("daq", ["usb6211", "usb4431", "pxie4468"])
@pytest.mark.parametrize("shaker", ["tv51110", "tv52110", "bk4809"])
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
