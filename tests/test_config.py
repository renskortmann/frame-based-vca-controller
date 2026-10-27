import dataclasses

import pytest

from vcactl.config import ConfigError, load_shaker, load_settings, validate_setup, validate_settings
from conftest import pin_rates


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
    ("daq", "ai_range_v", "max"),
    ("safety", "ai_range_margin_db", -1.0),
    ("daq", "fs_io_hz", 300000.0),
    ("daq", "fs_io_hz", "fast"),
    ("daq", "decimation", "max"),
    ("control", "frame_size", "big"),
    ("control", "frame_size", 1001),
    ("control", "line_spacing_hz", 0.0),
    ("daq", "ai_terminal_config", "XYZ"),
    ("control", "correction_gain", 0.0),
    ("sensor", "sensitivity_mv_per_g", 0.0),
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
    # P-2000 at 9 V/V (measured): 5 A rms into the ~2 ohm coil needs 10 V rms out, 1.11 V rms in
    sh = load_shaker("bk4809")
    assert sh.amp_input_full_v == pytest.approx(5.0 * 2.0 / 9.0, abs=0.01)
    assert sh.max_drive_rms_v <= sh.amp_input_full_v
    assert sh.max_drive_v == 2.7
    assert sh.f_min_hz == 20.0                     # amplifier response from 20 Hz
    assert sh.sim.amp_gain_a_per_v == pytest.approx(9.0 / 2.0)


ALL_DAQS = ["usb6211", "usb4431", "pxie4468", "cdaq9234_9260", "cdaq9232_9263", "dt9837a"]


@pytest.mark.parametrize("daq", ALL_DAQS)
@pytest.mark.parametrize("shaker", ["tv51110", "tv52110", "bk4809", "bk4801_4812"])
def test_daq_profiles_load(daq, shaker):
    st = load_settings(daq=daq)
    validate_setup(st, load_shaker(shaker))
    assert st.daq.limits.antialias_filter == (daq != "usb6211")


def test_cdaq_and_ul_profiles():
    st = load_settings(daq="cdaq9234_9260")
    assert (st.daq.device, st.daq.ai_device, st.daq.ao_device) == ("cDAQ1", "cDAQ1Mod1", "cDAQ1Mod2")
    assert st.daq.limits.ao_sync == "shared_timebase" and st.daq.limits.driver == "nidaqmx"
    st = load_settings(daq="cdaq9232_9263")
    assert not st.daq.limits.ao_reconstruction_filter and st.daq.limits.ao_sync == "sample_clock"
    st = load_settings(daq="dt9837a")
    assert (st.daq.limits.driver, st.daq.limits.product, st.daq.limits.ao_sync) == \
        ("uldaq", "DT9837A", "readback")
    assert st.daq.limits.ao_lead_samples(51200.0) == 8192 + 20480


def test_decimation_needs_both_converter_filters():
    st = pin_rates(load_settings(daq="cdaq9232_9263"), 51200.0, 2)
    validate_settings(st)
    with pytest.raises(ConfigError, match="reconstruction filter"):
        validate_settings(pin_rates(st, 51200.0, 1))


@pytest.mark.parametrize("daq,changes,match", [
    ("usb6211", {"ao_sync": "readback"}, "ao_sync"),           # a uldaq mode on nidaqmx
    ("dt9837a", {"ao_sync": "sample_clock"}, "ao_sync"),
    ("dt9837a", {"driver": "visa"}, "driver"),
    ("dt9837a", {"product": ""}, "product"),
    ("cdaq9234_9260", {"iepe_compliance_v": 0.0}, "iepe_compliance_v"),
    ("dt9837a", {"ao_fifo_samples": -1}, "ao_fifo_samples"),
])
def test_driver_limits_validated(daq, changes, match):
    st = load_settings(daq=daq)
    bad = dataclasses.replace(st, daq=dataclasses.replace(
        st.daq, limits=dataclasses.replace(st.daq.limits, **changes)))
    with pytest.raises(ConfigError, match=match):
        validate_settings(bad)


def test_iepe_compliance():
    """PCB 352B: 11.1 V bias + 4.86 V at 5 g = 16.0 V, within the DT9837A's 18 V."""
    st = load_settings(daq="dt9837a")
    on = dataclasses.replace(st, daq=dataclasses.replace(st.daq, iepe_current_ma=4.0))
    validate_settings(on)
    high = dataclasses.replace(on, sensor=dataclasses.replace(on.sensor, iepe_bias_max_v=14.0))   # datasheet
    with pytest.raises(ConfigError, match="compliance voltage 18 V"):
        validate_settings(high)
    off = dataclasses.replace(high, daq=dataclasses.replace(high.daq, iepe_current_ma=0.0))
    validate_settings(off)                           # IEPE off: not checked


def test_dsa_profiles():
    usb4431, pxie4468 = load_settings(daq="usb4431").daq, load_settings(daq="pxie4468").daq
    assert usb4431.ao_range_v == 3.5 and usb4431.fs_io_hz == usb4431.decimation == "auto"
    assert pxie4468.limits.ai_ranges_v[0] == 0.316 and pxie4468.limits.fs_max_hz == 200000
    # USB-4431 delay, measured per AO divider n (rate fM / n)
    assert [usb4431.limits.filter_delay_at(fs) for fs in (1250, 3000, 10000, 20000, 25600, 51200,
                                                          96000)] ==         [103.25, 102.8, 98.85, 93.6, 83.05, 62.1, 62.1]


@pytest.mark.parametrize("daq,fs_io_hz,decimation,f_max", [
    ("usb6211", 100000.0, 4, 7500.0),         # with decimation: 0.6 x control Nyquist
    ("usb4431", 51200.0, 2, 7680.0),
    ("pxie4468", 51200.0, 2, 7680.0),
    ("usb6211", 250000.0, 2, 37500.0),        # highest rate, lowest decimation
    ("usb4431", 96000.0, 1, 0.453 * 96000),   # decimation 1: converter passband
    ("pxie4468", 200000.0, 1, 0.454 * 200000),
])
def test_daq_band_limit(daq, fs_io_hz, decimation, f_max):
    st = pin_rates(load_settings(daq=daq), fs_io_hz, decimation)
    validate_settings(st)
    assert st.f_band_max_hz == pytest.approx(f_max)


@pytest.mark.parametrize("value", [0.0, 0.6])
def test_passband_fraction_validated(value):
    st = load_settings(daq="usb4431")
    bad = dataclasses.replace(st, daq=dataclasses.replace(
        st.daq, limits=dataclasses.replace(st.daq.limits, passband_fraction=value)))
    with pytest.raises(ConfigError, match="passband_fraction"):
        validate_settings(bad)


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
    ("max_drive_rms_v", 1.2, "amp_input_full_v"),  # above the P-2000 full-rating input (1.11 V)
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
        validate_setup(settings, dataclasses.replace(tv51110, max_drive_rms_v=0.005,
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


def test_pcb352b_is_default_sensor():
    st = load_settings()
    sn = st.sensor
    assert sn.name.startswith("PCB 352B") and sn.sensitivity_mv_per_g == 972.0      # calibrated
    assert (sn.range_g_pk, sn.f_min_hz, sn.f_max_hz, sn.mass_kg) == (5.0, 2.0, 10000.0, 0.025)
    assert (sn.iepe_min_ma, sn.iepe_max_ma, sn.iepe_bias_max_v) == (2.0, 20.0, 11.1)
    assert st.v_per_g == pytest.approx(0.972)
    assert st.payload_total_kg == pytest.approx(st.safety.payload_kg + 0.025)


def test_pcb356b18_sensor():
    st = load_settings(sensor="pcb356b18")
    sn = st.sensor
    assert sn.name.startswith("PCB 356B18") and sn.sensitivity_mv_per_g == 1048.0   # z axis
    assert (sn.range_g_pk, sn.f_min_hz, sn.f_max_hz, sn.mass_kg) == (5.0, 0.5, 3000.0, 0.025)
    assert (sn.iepe_min_ma, sn.iepe_max_ma) == (2.0, 20.0)
    assert st.v_per_g == pytest.approx(1.048)
    assert st.payload_total_kg == pytest.approx(st.safety.payload_kg + 0.025)


def test_conditioner_gain(tmp_path):
    p = tmp_path / "settings.toml"
    p.write_text("[daq]\nconditioner_gain = 2.0\n")
    assert load_settings(p).v_per_g == pytest.approx(1.944)


def test_sensor_table_in_settings_rejected(tmp_path):
    p = tmp_path / "settings.toml"
    p.write_text("[sensor]\nsensitivity_mv_per_g = 10.0\n")
    with pytest.raises(ConfigError, match="--sensor"):
        load_settings(p)


def test_missing_sensor():
    with pytest.raises(ConfigError, match="not found"):
        load_settings(sensor="nope")


def test_iepe_current_checked_against_sensor():
    st = load_settings(daq="usb4431")
    on = dataclasses.replace(st, daq=dataclasses.replace(st.daq, iepe_current_ma=2.1,
                                                         ai_coupling="AC"))
    validate_settings(on)                       # 2.1 mA is inside the 352B's 2-20 mA
    with pytest.raises(ConfigError, match="outside"):
        validate_settings(dataclasses.replace(on, sensor=dataclasses.replace(
            on.sensor, iepe_min_ma=4.0)))
    with pytest.raises(ConfigError, match="not an IEPE sensor"):
        validate_settings(dataclasses.replace(on, sensor=dataclasses.replace(
            on.sensor, iepe_min_ma=0.0, iepe_max_ma=0.0)))


@pytest.mark.parametrize("field,value", [
    ("range_g_pk", 0.0),
    ("f_min_hz", 0.0),
    ("f_max_hz", 0.5),                          # below f_min_hz
    ("mass_kg", -0.01),
    ("iepe_min_ma", 30.0),                      # above iepe_max_ma
])
def test_sensor_validation(settings, field, value):
    bad = dataclasses.replace(settings, sensor=dataclasses.replace(settings.sensor,
                                                                   **{field: value}))
    with pytest.raises(ConfigError, match="sensor"):
        validate_settings(bad)


def test_settings_key(tmp_path):
    assert load_settings("default") == load_settings()
    (tmp_path / "settings").mkdir()
    (tmp_path / "settings" / "default.toml").write_text("[pretest]\ndrive_rms_v = 0.02\n")
    for kind in ("daq", "sensors"):
        (tmp_path / kind).mkdir()
    (tmp_path / "daq" / "usb6211.toml").write_text(open("config/daq/usb6211.toml").read())
    (tmp_path / "sensors" / "pcb352b.toml").write_text(
        open("config/sensors/pcb352b.toml").read())
    assert load_settings(config_dir=tmp_path).pretest.drive_rms_v == 0.02


def test_ai_range_auto_default():
    for daq in ("usb6211", "usb4431", "pxie4468"):
        assert load_settings(daq=daq).daq.ai_range_v == "auto"


def test_ai_range_number_from_toml(tmp_path):
    p = tmp_path / "settings.toml"
    p.write_text("[daq]\nai_range_v = 1\n")
    assert load_settings(p).daq.ai_range_v == 1.0
    p.write_text("[daq]\nai_range_v = true\n")
    with pytest.raises(ConfigError, match="ai_range_v"):
        load_settings(p)


def test_rates_from_toml(tmp_path):
    p = tmp_path / "settings.toml"
    p.write_text("[daq]\nfs_io_hz = 50000\ndecimation = 2\n[control]\nframe_size = 2048\n")
    st = load_settings(p)
    assert (st.daq.fs_io_hz, st.daq.decimation, st.control.frame_size) == (50000.0, 2, 2048)
    assert st.rates_resolved and st.fs_control_hz == 25000
    p.write_text("[daq]\ndecimation = 2.5\n")
    with pytest.raises(ConfigError, match="decimation"):
        load_settings(p)
