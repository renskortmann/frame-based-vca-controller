import dataclasses

import pytest

from vcactl.config import ConfigError, load_shaker, load_station, validate_station


def test_bundled_configs_load(station):
    s1, s2 = load_shaker("tv51110"), load_shaker("TV52110")
    assert s1.accel_random_rms_g == 30 and s2.accel_random_rms_g == 25
    assert s1.force_random_rms_n == 70 and s2.force_random_rms_n == 50
    assert station.fs_control_hz == 25000


def test_unknown_key_rejected(tmp_path):
    p = tmp_path / "station.toml"
    p.write_text("[daq]\ndevcie = 'Dev1'\n")
    with pytest.raises(ConfigError, match="devcie"):
        load_station(p)


def test_wrong_type_rejected(tmp_path):
    p = tmp_path / "station.toml"
    p.write_text("[safety]\nmax_drive_v = 'high'\n")
    with pytest.raises(ConfigError, match="max_drive_v"):
        load_station(p)


@pytest.mark.parametrize("section,field,value", [
    ("safety", "max_drive_v", 12.0),
    ("daq", "ai_range_v", 2.0),
    ("daq", "fs_io_hz", 300000.0),
    ("daq", "ai_terminal_config", "XYZ"),
    ("control", "correction_gain", 0.0),
])
def test_station_validation(station, section, field, value):
    bad = dataclasses.replace(station, **{section: dataclasses.replace(getattr(station, section),
                                                                      **{field: value})})
    with pytest.raises(ConfigError):
        validate_station(bad)


def test_missing_shaker(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_shaker("tv99999", tmp_path)
