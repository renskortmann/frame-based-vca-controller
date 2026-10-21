"""Automatic DAQ rate and frame size (vcactl.rates)."""

import dataclasses
import re

import pytest

from vcactl.config import ConfigError, load_settings, load_shaker, validate_settings
from vcactl.daq.sim import SimulatedDaq
from vcactl.profile import load_profile
from vcactl.rates import resolve_rates
from conftest import PROFILES, TEST_SENSOR


def flat(tmp_path, f_lo, f_hi):
    p = tmp_path / "band.toml"
    p.write_text(f"breakpoints = [{{ f_hz = {f_lo}, psd_g2_hz = 1e-6 }}, "
                 f"{{ f_hz = {f_hi}, psd_g2_hz = 1e-6 }}]\n")
    return load_profile(p)


def wide(settings, shaker):
    """Sensor and shaker without a band limit, so that only the DAQ limits the test."""
    settings = dataclasses.replace(settings, sensor=dataclasses.replace(settings.sensor,
                                                                        f_max_hz=1e6))
    return settings, dataclasses.replace(shaker, f_max_hz=1e6)


@pytest.mark.parametrize("daq,profile,fs_io,R,N", [
    ("usb6211", "flat", 100000.0, 10, 2048),       # flat 20-2000 Hz
    ("usb6211", "low_g", 100000.0, 10, 2048),      # 20-3000 Hz
    ("usb6211", "wideband", 100000.0, 4, 4096),    # 20-7000 Hz: the rates used before
    ("usb4431", "flat", 5000.0, 1, 1024),
    ("usb4431", "low_g", 10000.0, 1, 2048),
    ("usb4431", "wideband", 20000.0, 1, 4096),
    ("pxie4468", "flat", 6400.0, 1, 1024),
    ("pxie4468", "low_g", 12800.0, 1, 2048),
    ("pxie4468", "wideband", 25600.0, 1, 4096),
    ("cdaq9234_9260", "flat", 5120.0, 1, 1024),
    ("cdaq9234_9260", "low_g", 10240.0, 1, 2048),
    ("cdaq9234_9260", "wideband", 25600.0, 1, 4096),
    ("cdaq9232_9263", "flat", 51200.0, 5, 2048),    # AO without reconstruction filter: R >= 2
    ("cdaq9232_9263", "low_g", 51200.0, 5, 2048),
    ("cdaq9232_9263", "wideband", 51200.0, 2, 4096),
    ("dt9837a", "flat", 51200.0, 1, 8192),          # >= 51.2 kS/s: short AO lag
    ("dt9837a", "low_g", 51200.0, 1, 8192),
    ("dt9837a", "wideband", 51200.0, 1, 8192),
])
def test_lowest_rate_that_fits(daq, profile, fs_io, R, N):
    st = load_settings(daq=daq, sensor=TEST_SENSOR)
    resolved, rates = resolve_rates(load_profile(PROFILES / f"{profile}.toml"),
                                    load_shaker("bk4809"), st)
    assert (rates.fs_io_hz, rates.decimation, rates.frame_size) == (fs_io, R, N)
    assert (resolved.daq.fs_io_hz, resolved.daq.decimation,
            resolved.control.frame_size) == (fs_io, R, N)
    assert rates.auto == ("fs_io_hz", "decimation", "frame_size")
    assert rates.band_max_hz == resolved.f_band_max_hz
    assert 3.125 < rates.line_spacing_hz <= 6.25    # frame of 160-320 ms at every rate
    validate_settings(resolved)


@pytest.mark.parametrize("daq,f_max,fs_io,R,N", [
    ("usb6211", 37500.0, 250000.0, 2, 32768),
    ("usb4431", 0.453 * 96000, 96000.0, 1, 16384),
    ("pxie4468", 0.454 * 200000, 200000.0, 1, 32768),
    ("cdaq9234_9260", 0.45 * 51200, 51200.0, 1, 8192),
    ("cdaq9232_9263", 0.3 * 102400 / 2, 102400.0, 2, 8192),
    ("dt9837a", 0.453 * 52734, 52734.0, 1, 16384),
])
def test_highest_test_frequency(daq, f_max, fs_io, R, N, tmp_path):
    st, shaker = wide(load_settings(daq=daq, sensor=TEST_SENSOR), load_shaker("bk4809"))
    _, rates = resolve_rates(flat(tmp_path, 100.0, f_max), shaker, st)
    assert (rates.fs_io_hz, rates.decimation, rates.frame_size) == (fs_io, R, N)
    with pytest.raises(ConfigError, match="highest test frequency of the " + re.escape(st.daq.model)):
        resolve_rates(flat(tmp_path, 100.0, f_max * 1.001), shaker, st)


def test_ao_queue_covers_the_driver_lag(tmp_path):
    """DT9837A: uldaq holds 8192 + 0.4 s x fs AO samples ahead; the queue is raised to cover it."""
    st = load_settings(daq="dt9837a", sensor=TEST_SENSOR)
    resolved, rates = resolve_rates(load_profile(PROFILES / "low_g.toml"), load_shaker("bk4809"), st)
    assert (rates.ao_queue_blocks, resolved.daq.ao_queue_blocks) == (8, 8)   # 28672 / 4096 + 1
    assert rates.ao_queue_blocks * rates.frame_size / 2 > rates.ao_lead_samples
    assert "AO queue 8 half-frames (0.64 s" in rates.format()
    _, nidaq = resolve_rates(load_profile(PROFILES / "low_g.toml"), load_shaker("bk4809"),
                             load_settings(sensor=TEST_SENSOR))
    assert nidaq.ao_queue_blocks == 2 and "AO queue" not in nidaq.format()


def test_usb6211_stays_oversampled_for_low_bands(tmp_path):
    """At 100 kS/s (no anti-aliasing filter) the decimation stops at 16 (6.25 kS/s control)."""
    st = load_settings(sensor=TEST_SENSOR)
    _, rates = resolve_rates(flat(tmp_path, 20.0, 200.0), load_shaker("bk4809"), st)
    assert (rates.fs_io_hz, rates.decimation, rates.frame_size) == (100000.0, 16, 1024)


def test_error_lists_every_component(tmp_path):
    st = load_settings()                           # PCB 356B18: 0.5-3000 Hz
    with pytest.raises(ConfigError) as exc:
        resolve_rates(flat(tmp_path, 1.0, 12000.0), load_shaker("tv51110"), st)  # 2-7000 Hz
    msg = str(exc.value)
    assert "does not fit" in msg
    assert "f_hi 12000 Hz > f_max 3000 Hz of the sensor (PCB 356B18" in msg
    assert "f_hi 12000 Hz > f_max 7000 Hz of the shaker (" in msg
    assert "f_lo 1 Hz < f_min 2 Hz of the shaker (" in msg
    assert "USB-6211" not in msg                   # 12 kHz fits the DAQ


def test_fixed_io_rate_auto_decimation():
    st = load_settings(sensor=TEST_SENSOR)
    st = dataclasses.replace(st, daq=dataclasses.replace(st.daq, fs_io_hz=250000.0))
    _, rates = resolve_rates(load_profile(PROFILES / "low_g.toml"), load_shaker("bk4809"), st)
    # 0.3 * 250 k / 16 = 4.69 kHz >= 3 kHz; 15625 S/s control rate
    assert (rates.fs_io_hz, rates.decimation, rates.frame_size) == (250000.0, 16, 4096)
    assert rates.auto == ("decimation", "frame_size")
    assert rates.format().endswith("(auto: decimation, frame_size)")


def test_fixed_decimation_auto_io_rate():
    st = load_settings(daq="usb4431", sensor=TEST_SENSOR)
    st = dataclasses.replace(st, daq=dataclasses.replace(st.daq, decimation=2))
    _, rates = resolve_rates(load_profile(PROFILES / "low_g.toml"), load_shaker("bk4809"), st)
    # decimation 2: 0.3 * fs / 2 >= 3 kHz needs fs >= 20 kS/s
    assert (rates.fs_io_hz, rates.decimation, rates.frame_size) == (20000.0, 2, 2048)


def test_fixed_rates_beyond_their_band(tmp_path):
    st = load_settings(sensor=TEST_SENSOR)
    st = dataclasses.replace(st, daq=dataclasses.replace(st.daq, fs_io_hz=100000.0,
                                                         decimation=4))
    with pytest.raises(ConfigError, match="fixed: daq.fs_io_hz, daq.decimation"):
        resolve_rates(flat(tmp_path, 20.0, 9000.0), load_shaker("bk4809"), st)


def test_frame_size_follows_f_lo(tmp_path):
    st = load_settings(sensor=TEST_SENSOR)
    profile = flat(tmp_path, 2.0, 2000.0)          # needs df <= 1 Hz
    _, rates = resolve_rates(profile, load_shaker("tv51110"), st)
    assert (rates.fs_io_hz, rates.decimation, rates.frame_size) == (100000.0, 10, 16384)
    fixed = dataclasses.replace(st, control=dataclasses.replace(st.control, frame_size=4096))
    with pytest.raises(ConfigError, match="frame_size = 4096 gives 2.44 Hz"):
        resolve_rates(profile, load_shaker("tv51110"), fixed)


def test_sine_profiles(tmp_path):
    st = load_settings(daq="usb4431", sensor=TEST_SENSOR)
    _, rates = resolve_rates(load_profile(PROFILES / "sine_sweep.toml"),
                             load_shaker("tv51110"), st)
    assert rates.fs_io_hz == 5000.0                # sweep to 2 kHz


def test_pxie4468_delay_by_rate(tv51110, flat_profile):
    st = load_settings(daq="pxie4468", sensor=TEST_SENSOR)
    lim = st.daq.limits
    assert [lim.filter_delay_at(f) for f in (1600, 10000, 12800, 51200, 102400, 195000,
                                             200000)] == [65, 65, 67, 75, 85, 106, 106]
    resolved, rates = resolve_rates(flat_profile, tv51110, st)  # 20-2000 Hz: 6.4 kS/s
    assert SimulatedDaq(resolved, tv51110).delay_samples == tv51110.sim.io_delay_samples + 65


@pytest.mark.parametrize("daq,changes,match", [
    ("usb6211", {"fs_io_auto_hz": (30000.0,)}, "fs_io_auto_hz"),  # 20 MHz / 30 kHz: no integer
    ("usb6211", {"fs_io_auto_hz": ()}, "fs_io_auto_hz"),          # nothing to choose from
    ("usb4431", {"fs_io_auto_hz": (50000.0,)}, "fs_io_auto_hz"),  # not an AO update rate
])
def test_auto_rates_validated(daq, changes, match):
    st = load_settings(daq=daq)
    with pytest.raises(ConfigError, match=match):
        validate_settings(dataclasses.replace(st, daq=dataclasses.replace(st.daq, **changes)))


def test_delay_table_validated():
    st = load_settings(daq="pxie4468")
    lim = dataclasses.replace(st.daq.limits, filter_delay_by_rate=(65.0,))
    with pytest.raises(ConfigError, match="filter_delay"):
        validate_settings(dataclasses.replace(st, daq=dataclasses.replace(st.daq, limits=lim)))
