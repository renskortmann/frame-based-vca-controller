import pytest

from vcactl.config import load_shaker, load_settings
from vcactl.profile import load_profile

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "config" / "test_profiles"


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture(params=["tv51110", "tv52110"])
def shaker(request):
    return load_shaker(request.param)


@pytest.fixture
def tv51110():
    return load_shaker("tv51110")


@pytest.fixture
def flat_profile():
    return load_profile(PROFILES / "example_flat.toml")


@pytest.fixture
def wideband_profile():
    return load_profile(PROFILES / "example_wideband.toml")
