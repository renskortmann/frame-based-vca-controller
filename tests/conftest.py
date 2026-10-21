import dataclasses

import pytest

from vcactl.config import load_shaker, load_settings
from vcactl.profile import load_profile

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "config" / "test_profiles"
TEST_SENSOR = str(ROOT / "tests" / "data" / "sensor_test.toml")


def pin_rates(settings, fs_io_hz=100000.0, decimation=4, frame_size=4096):
    """Settings with a fixed DAQ rate and frame size (default: the USB-6211 values used before
    the automatic selection), for tests that need exact numbers."""
    return dataclasses.replace(
        settings, daq=dataclasses.replace(settings.daq, fs_io_hz=fs_io_hz, decimation=decimation),
        control=dataclasses.replace(settings.control, frame_size=frame_size))


@pytest.fixture
def settings():
    return pin_rates(load_settings(sensor=TEST_SENSOR))


@pytest.fixture(params=["tv51110", "tv52110"])
def shaker(request):
    return load_shaker(request.param)


@pytest.fixture
def tv51110():
    return load_shaker("tv51110")


@pytest.fixture
def flat_profile():
    return load_profile(PROFILES / "flat.toml")


@pytest.fixture
def wideband_profile():
    return load_profile(PROFILES / "wideband.toml")
