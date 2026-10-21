"""Loading and validation of TOML configuration files (shakers, DAQ devices, sensors, settings)."""

from __future__ import annotations

import dataclasses
import math
import sys
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


class ConfigError(ValueError):
    pass


# Default location of the bundled config directory (repo checkout).
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@dataclass(frozen=True)
class SimModelConfig:
    amp_gain_a_per_v: float = 1.57
    bl_n_per_a: float = 12.9
    suspension_zeta: float = 0.15
    armature_q: float = 8.0
    sensor_noise_v: float = 50e-6
    io_delay_samples: int = 3
    cubic_coeff: float = 0.0          # response nonlinearity: a + cubic_coeff * a^3 (a in g)


@dataclass(frozen=True)
class ShakerConfig:
    name: str
    f_min_hz: float
    f_max_hz: float
    force_sine_peak_n: float
    force_random_rms_n: float
    accel_sine_peak_g: float
    accel_random_rms_g: float
    displacement_pp_mm: float
    velocity_peak_m_s: float
    moving_mass_kg: float
    suspension_stiffness_n_per_mm: float
    armature_resonance_hz: float
    amp_input_full_v: float
    max_drive_v: float                # hard clip of the drive (peak) at the amplifier input
    max_drive_rms_v: float            # abort if the drive rms exceeds this
    payload_static_max_kg: float      # payload whose weight uses up the half-stroke (vertical mounting)
    sim: SimModelConfig = field(default_factory=SimModelConfig)


@dataclass(frozen=True)
class DaqLimits:
    """Capabilities of a DAQ device type, from its data sheet. Settings are validated against these."""
    ai_ranges_v: tuple[float, ...]
    ao_ranges_v: tuple[float, ...]
    ai_terminal_configs: tuple[str, ...]
    ao_terminal_configs: tuple[str, ...]  # empty: fixed by the hardware, not configurable
    ai_couplings: tuple[str, ...]
    iepe_currents_ma: tuple[float, ...]   # selectable IEPE excitation currents, 0 = off
    fs_min_hz: float                      # common AI/AO rate range
    fs_max_hz: float
    fs_allowed_hz: tuple[float, ...]      # if not empty, only these rates are allowed
    timebase_hz: float                    # if > 0, timebase_hz / fs_io_hz must be an integer
    antialias_filter: bool                # built-in AI anti-aliasing (delta-sigma ADC)
    ao_reconstruction_filter: bool        # built-in AO anti-imaging filter (delta-sigma DAC)
    driver: str                           # "nidaqmx" or "uldaq" (UL for Linux)
    ao_sync: str                          # how AO and AI share one clock (see validate_daq):
                                          # nidaqmx: "sample_clock", "start_trigger" or
                                          # "shared_timebase"; uldaq: "readback"
    ao_on_demand: bool                    # AO supports software-timed (on-demand) writes
    iepe_compliance_v: float              # IEPE compliance voltage (0: no IEPE excitation)
    filter_delay_samples: float           # AO + AI converter filter delay in fs_io samples
                                          # (above the last rate of the table below)
    passband_fraction: float              # converter passband (AI and AO) as a fraction of fs_io
    # rate-dependent delay (PXIe-4468, NI 9232): filter_delay_by_rate[i] samples for
    # fs_io_hz <= filter_delay_max_rate_hz[i]; empty: filter_delay_samples at every rate
    filter_delay_max_rate_hz: tuple[float, ...] = ()
    filter_delay_by_rate: tuple[float, ...] = ()
    product: str = ""                     # uldaq: product name of the device (e.g. "DT9837A")
    # AO data the driver keeps ahead of the output (device FIFO + transfers in flight); the AO
    # queue (daq.ao_queue_blocks) is raised per run to cover it (see vcactl.rates)
    ao_fifo_samples: int = 0
    ao_transfer_s: float = 0.0

    def ao_lead_samples(self, fs_io_hz: float) -> int:
        """AO samples the driver holds ahead of the output at ``fs_io_hz``."""
        return self.ao_fifo_samples + math.ceil(self.ao_transfer_s * fs_io_hz)

    def filter_delay_at(self, fs_io_hz: float) -> float:
        """Converter filter delay in samples at the I/O rate ``fs_io_hz``."""
        for f_max, delay in zip(self.filter_delay_max_rate_hz, self.filter_delay_by_rate):
            if fs_io_hz <= f_max:
                return delay
        return self.filter_delay_samples


@dataclass(frozen=True)
class DaqConfig:
    model: str
    limits: DaqLimits
    device: str = "Dev1"                  # device (nidaqmx: also the chassis for terminals)
    ai_device: str = ""                   # module with the AI channel (cDAQ: "cDAQ1Mod1"); empty: device
    ao_device: str = ""                   # module with the AO channel; empty: device
    ao_channel: str = "ao0"
    ai_channel: str = "ai1"
    ai_terminal_config: str = "DIFF"
    ai_range_v: Union[float, str] = "auto"  # "auto": smallest range for the expected peak
    ai_coupling: str = "DC"
    iepe_current_ma: float = 0.0
    ao_range_v: float = 10.0
    ao_terminal_config: str = ""          # empty: driver default
    fs_io_hz: Union[float, str] = "auto"  # "auto": chosen per run (see vcactl.rates)
    fs_io_auto_hz: tuple[float, ...] = ()   # rates "auto" chooses from (default: limits.fs_allowed_hz)
    decimation: Union[int, str] = "auto"
    ao_queue_blocks: int = 2
    conditioner_gain: float = 1.0         # gain of an external IEPE conditioner between sensor and AI


@dataclass(frozen=True)
class SensorConfig:
    """Control accelerometer, from its data sheet and calibration."""
    name: str
    sensitivity_mv_per_g: float           # calibrated sensitivity of the axis used for control
    range_g_pk: float                     # measurement range
    f_min_hz: float                       # frequency range (data-sheet tolerance band)
    f_max_hz: float
    mass_kg: float = 0.0                  # added to safety.payload_kg
    iepe_min_ma: float = 0.0              # IEPE excitation current range; 0, 0 = not IEPE
    iepe_max_ma: float = 0.0
    iepe_bias_max_v: float = 0.0          # highest IEPE output bias voltage; 0: unknown, not checked


@dataclass(frozen=True)
class ControlConfig:
    frame_size: Union[int, str] = "auto"  # "auto": chosen per run (see vcactl.rates)
    line_spacing_hz: float = 6.25         # auto frame size: the largest line spacing
    dof: int = 120
    control_dof: int = 32
    frf_averages: int = 32
    correction_gain: float = 0.1
    max_step_db: float = 1.0
    max_correction_db: float = 15.0
    coherence_min: float = 0.5
    h_dynamic_range_db: float = 60.0
    start_level_db: float = -12.0
    level_step_db: float = 3.0
    level_tolerance_db: float = 1.0
    min_frames_per_level: int = 8
    max_frames_per_level: int = 200
    rampdown_s: float = 0.5
    snapshot_interval_s: float = 5.0


@dataclass(frozen=True)
class SineConfig:
    correction_gain: float = 0.15      # fraction of the amplitude error (dB) corrected per block
                                       # (> 0.25 overshoots: loop delay is about 4 blocks)
    max_step_db: float = 1.0           # max correction per block
    max_correction_db: float = 20.0    # max accumulated correction relative to the pretest FRF
    ramp_s: float = 2.0                # ramp from control.start_level_db at start / after ring-down
    settled_blocks: int = 3            # stepped sine: blocks within alarm_db before the dwell


@dataclass(frozen=True)
class SafetyConfig:
    max_clip_fraction: float = 0.005
    payload_kg: float = 0.02
    displacement_sigma: float = 3.0
    accel_crest_factor: float = 4.5
    open_loop_db: float = 12.0
    ai_overload_fraction: float = 0.98
    ai_range_margin_db: float = 3.0       # auto AI range: headroom above the expected peak
    abort_rampdown_s: float = 0.05


@dataclass(frozen=True)
class PretestConfig:
    drive_rms_v: float = 0.05
    frames: int = 40
    noise_blocks: int = 6
    min_snr_db: float = 10.0


@dataclass(frozen=True)
class Settings:
    daq: DaqConfig
    sensor: SensorConfig
    control: ControlConfig = field(default_factory=ControlConfig)
    sine: SineConfig = field(default_factory=SineConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    pretest: PretestConfig = field(default_factory=PretestConfig)

    @property
    def rates_resolved(self) -> bool:
        """False while daq.fs_io_hz, daq.decimation or control.frame_size is "auto"."""
        return not any(isinstance(v, str) for v in
                       (self.daq.fs_io_hz, self.daq.decimation, self.control.frame_size))

    def require_rates(self, who: str) -> None:
        if not self.rates_resolved:
            raise ValueError(f"{who}: daq.fs_io_hz, daq.decimation or control.frame_size is "
                             "\"auto\"; resolve them first with vcactl.rates.resolve_rates")

    @property
    def fs_control_hz(self) -> float:
        self.require_rates("Settings.fs_control_hz")
        return self.daq.fs_io_hz / self.daq.decimation

    @property
    def f_band_max_hz(self) -> float:
        """Highest test frequency at the DAQ rates of these settings (``daq_band_max_hz``)."""
        self.require_rates("Settings.f_band_max_hz")
        return daq_band_max_hz(self.daq.limits, self.daq.fs_io_hz, self.daq.decimation)

    @property
    def v_per_g(self) -> float:
        """Sensitivity of the measurement chain at the DAQ input."""
        return self.sensor.sensitivity_mv_per_g * self.daq.conditioner_gain / 1000

    @property
    def payload_total_kg(self) -> float:
        """Mass on the armature: fixture and specimen plus the control sensor."""
        return self.safety.payload_kg + self.sensor.mass_kg


def daq_band_max_hz(limits: DaqLimits, fs_io_hz: float, decimation: int) -> float:
    """Highest test frequency at an I/O rate and decimation: the converter passband
    (``limits.passband_fraction * fs_io_hz``) and, with decimation, the passband of the
    rate-change FIR (0.6 x control Nyquist)."""
    f = limits.passband_fraction * fs_io_hz
    if decimation > 1:
        f = min(f, 0.3 * fs_io_hz / decimation)
    return f


def build_dataclass(cls, table: dict, where: str):
    """Construct dataclass ``cls`` from a TOML table, rejecting unknown keys."""
    if not isinstance(table, dict):
        raise ConfigError(f"{where}: expected a table")
    hints = typing.get_type_hints(cls)
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = set(table) - names
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}")
    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.name not in table:
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                raise ConfigError(f"{where}: missing required key '{f.name}'")
            continue
        value = table[f.name]
        typ = hints[f.name]
        if dataclasses.is_dataclass(typ):
            value = build_dataclass(typ, value, f"{where}.{f.name}")
        elif typing.get_origin(typ) is tuple:
            if not isinstance(value, list):
                raise ConfigError(f"{where}.{f.name}: expected a list, got {value!r}")
            item = typing.get_args(typ)[0]
            if item is float and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                     for v in value):
                value = tuple(float(v) for v in value)
            elif item is str and all(isinstance(v, str) for v in value):
                value = tuple(value)
            else:
                raise ConfigError(f"{where}.{f.name}: expected a list of {item.__name__}, "
                                  f"got {value!r}")
        elif typ in (Union[float, str], Union[int, str]):
            number = float if typ == Union[float, str] else int
            numbers = (int, float) if number is float else (int,)
            if isinstance(value, bool) or not isinstance(value, (*numbers, str)):
                kind = "a number" if number is float else "an integer"
                raise ConfigError(f"{where}.{f.name}: expected {kind} or a string, got {value!r}")
            value = value if isinstance(value, str) else number(value)
        elif typ is bool:
            if not isinstance(value, bool):
                raise ConfigError(f"{where}.{f.name}: expected true or false, got {value!r}")
        elif typ is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"{where}.{f.name}: expected a number, got {value!r}")
            value = float(value)
        elif typ is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"{where}.{f.name}: expected an integer, got {value!r}")
        elif typ is str:
            if not isinstance(value, str):
                raise ConfigError(f"{where}.{f.name}: expected a string, got {value!r}")
        kwargs[f.name] = value
    return cls(**kwargs)


def read_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None


def resolve_config_path(name_or_path: str, kind: str,
                        config_dir: Path = DEFAULT_CONFIG_DIR) -> Path:
    """Accept a key (``tv51110``) or a path to a TOML file; keys resolve to ``<kind>/<key>.toml``."""
    p = Path(name_or_path)
    if p.suffix == ".toml" or p.exists():
        return p
    return config_dir / kind / f"{name_or_path.lower()}.toml"


def resolve_shaker_path(name_or_path: str, config_dir: Path = DEFAULT_CONFIG_DIR) -> Path:
    return resolve_config_path(name_or_path, "shakers", config_dir)


def load_shaker(name_or_path: str, config_dir: Path = DEFAULT_CONFIG_DIR) -> ShakerConfig:
    path = resolve_shaker_path(name_or_path, config_dir)
    cfg = build_dataclass(ShakerConfig, read_toml(path), str(path))
    for name in ("f_min_hz", "f_max_hz", "force_random_rms_n", "accel_random_rms_g",
                 "displacement_pp_mm", "velocity_peak_m_s", "moving_mass_kg",
                 "armature_resonance_hz", "amp_input_full_v", "max_drive_v",
                 "max_drive_rms_v", "payload_static_max_kg"):
        if getattr(cfg, name) <= 0:
            raise ConfigError(f"{path}: {name} must be > 0")
    if cfg.max_drive_rms_v > cfg.max_drive_v:
        raise ConfigError(f"{path}: max_drive_rms_v must be <= max_drive_v")
    if cfg.max_drive_rms_v > cfg.amp_input_full_v:
        raise ConfigError(f"{path}: max_drive_rms_v must be <= amp_input_full_v "
                          "(the amplifier input for full rating)")
    if cfg.f_min_hz >= cfg.f_max_hz:
        raise ConfigError(f"{path}: f_min_hz must be < f_max_hz")
    return cfg


def load_settings(path: Path | str | None = None, daq: str = "usb6211",
                 config_dir: Path = DEFAULT_CONFIG_DIR, sensor: str = "pcb356b18") -> Settings:
    """Load a settings file (key or path, default ``default``: ``settings/default.toml``) and
    combine it with the DAQ device profile ``daq`` and the sensor file ``sensor``.

    The settings file may have a ``[daq]`` table that overrides settings of the DAQ profile
    (for example ``device`` or ``ai_channel``), but not its ``[limits]``.
    """
    path = resolve_config_path(str(path) if path else "default", "settings", config_dir)
    daq_path = resolve_config_path(daq, "daq", config_dir)
    sensor_path = resolve_config_path(sensor, "sensors", config_dir)
    table = read_toml(path)
    daq_table = read_toml(daq_path)
    overrides = table.get("daq", {})
    if not isinstance(overrides, dict) or "limits" in overrides or "model" in overrides:
        raise ConfigError(f"{path}: [daq] may only override DAQ settings, not model or limits")
    if "sensor" in table:
        raise ConfigError(f"{path}: [sensor] is no longer a settings table; the sensor is "
                          "selected with --sensor (config/sensors/*.toml)")
    table = {**table, "daq": {**daq_table, **overrides}, "sensor": read_toml(sensor_path)}
    where = f"{path} + {daq_path} + {sensor_path}"
    cfg = build_dataclass(Settings, table, where)
    validate_settings(cfg, where)
    return cfg


def _one_of(value: float, allowed: tuple[float, ...]) -> bool:
    return any(abs(value - a) <= 1e-6 * max(abs(a), 1.0) for a in allowed)


def _fmt(values) -> str:
    return ", ".join(f"{v:g}" if isinstance(v, float) else str(v) for v in values)


def _check_rate(d: DaqConfig, fs: float, name: str, where: str) -> None:
    """``fs`` must be a rate the device runs AI and AO at."""
    lim = d.limits
    if not lim.fs_min_hz <= fs <= lim.fs_max_hz:
        raise ConfigError(f"{where}: {name} must be in [{lim.fs_min_hz:g}, {lim.fs_max_hz:g}] "
                          f"({d.model}), got {fs:g}")
    if lim.fs_allowed_hz and not _one_of(fs, lim.fs_allowed_hz):
        raise ConfigError(f"{where}: {name} must be one of {_fmt(lim.fs_allowed_hz)} "
                          f"({d.model}), got {fs:g}")
    if lim.timebase_hz > 0:
        n = lim.timebase_hz / fs
        if abs(n - round(n)) > 1e-9 * n:
            raise ConfigError(f"{where}: {name} must divide {lim.timebase_hz:g} exactly "
                              f"({d.model}), got {fs:g}")


AO_SYNC_MODES = {"nidaqmx": ("sample_clock", "start_trigger", "shared_timebase"),
                 "uldaq": ("readback",)}


def validate_daq(d: DaqConfig, where: str = "daq") -> None:
    lim = d.limits
    if lim.driver not in AO_SYNC_MODES:
        raise ConfigError(f"{where}: limits.driver must be one of {_fmt(tuple(AO_SYNC_MODES))}")
    if lim.ao_sync not in AO_SYNC_MODES[lim.driver]:
        raise ConfigError(f"{where}: limits.ao_sync must be one of "
                          f"{_fmt(AO_SYNC_MODES[lim.driver])} with driver {lim.driver}")
    if lim.driver == "uldaq" and not lim.product:
        raise ConfigError(f"{where}: limits.product (the UL product name) is required with "
                          "driver uldaq")
    if lim.iepe_currents_ma and lim.iepe_compliance_v <= 0:
        raise ConfigError(f"{where}: limits.iepe_compliance_v must be > 0 for a device with IEPE "
                          "excitation")
    if d.ai_terminal_config.upper() not in lim.ai_terminal_configs:
        raise ConfigError(f"{where}: daq.ai_terminal_config must be one of "
                          f"{_fmt(lim.ai_terminal_configs)} ({d.model})")
    if isinstance(d.ai_range_v, str):
        if d.ai_range_v != "auto":
            raise ConfigError(f"{where}: daq.ai_range_v must be \"auto\" or one of "
                              f"{_fmt(lim.ai_ranges_v)} ({d.model})")
    elif not _one_of(d.ai_range_v, lim.ai_ranges_v):
        raise ConfigError(f"{where}: daq.ai_range_v must be \"auto\" or one of "
                          f"{_fmt(lim.ai_ranges_v)} ({d.model})")
    if d.ao_terminal_config and d.ao_terminal_config.upper() not in lim.ao_terminal_configs:
        raise ConfigError(f"{where}: daq.ao_terminal_config must be "
                          + (f"one of {_fmt(lim.ao_terminal_configs)}" if lim.ao_terminal_configs
                             else "empty (not configurable)") + f" ({d.model})")
    if not _one_of(d.ao_range_v, lim.ao_ranges_v):
        raise ConfigError(f"{where}: daq.ao_range_v must be one of {_fmt(lim.ao_ranges_v)} "
                          f"({d.model})")
    if d.ai_coupling.upper() not in lim.ai_couplings:
        raise ConfigError(f"{where}: daq.ai_coupling must be one of {_fmt(lim.ai_couplings)} "
                          f"({d.model})")
    if not _one_of(d.iepe_current_ma, (0.0, *lim.iepe_currents_ma)):
        raise ConfigError(f"{where}: daq.iepe_current_ma must be one of "
                          f"{_fmt((0.0, *lim.iepe_currents_ma))} ({d.model})")
    if d.iepe_current_ma > 0 and d.ai_coupling.upper() != "AC":
        raise ConfigError(f"{where}: daq.ai_coupling must be AC when IEPE excitation is on "
                          "(the sensor bias voltage would use up the AI range)")
    if isinstance(d.fs_io_hz, str):
        if d.fs_io_hz != "auto":
            raise ConfigError(f"{where}: daq.fs_io_hz must be \"auto\" or a rate in S/s "
                              f"({d.model})")
        if not (d.fs_io_auto_hz or lim.fs_allowed_hz):
            raise ConfigError(f"{where}: daq.fs_io_hz = \"auto\" needs daq.fs_io_auto_hz, the "
                              f"rates to choose from ({d.model})")
    else:
        _check_rate(d, d.fs_io_hz, "daq.fs_io_hz", where)
    for fs in d.fs_io_auto_hz:
        _check_rate(d, fs, "daq.fs_io_auto_hz", where)
    if isinstance(d.decimation, str):
        if d.decimation != "auto":
            raise ConfigError(f"{where}: daq.decimation must be \"auto\" or an integer >= 1")
    elif d.decimation < 1:
        raise ConfigError(f"{where}: daq.decimation must be \"auto\" or an integer >= 1")
    elif not lim.antialias_filter and d.decimation < 2:
        raise ConfigError(f"{where}: daq.decimation must be >= 2: the {d.model} has no "
                          "anti-aliasing filter, so AI must be oversampled and filtered digitally")
    elif not lim.ao_reconstruction_filter and d.decimation < 2:
        raise ConfigError(f"{where}: daq.decimation must be >= 2: the {d.model} has no AO "
                          "reconstruction filter, so AO must be interpolated digitally")
    if not 0 < lim.passband_fraction <= 0.5:
        raise ConfigError(f"{where}: limits.passband_fraction must be in (0, 0.5]")
    rates = list(lim.filter_delay_max_rate_hz)
    if len(rates) != len(lim.filter_delay_by_rate) or rates != sorted(rates):
        raise ConfigError(f"{where}: limits.filter_delay_max_rate_hz must be ascending and as "
                          "long as limits.filter_delay_by_rate")
    if d.conditioner_gain <= 0:
        raise ConfigError(f"{where}: daq.conditioner_gain must be > 0")
    if lim.ao_fifo_samples < 0 or lim.ao_transfer_s < 0:
        raise ConfigError(f"{where}: limits.ao_fifo_samples and ao_transfer_s must be >= 0")


def validate_sensor(sn: SensorConfig, where: str = "sensor") -> None:
    if sn.sensitivity_mv_per_g <= 0 or sn.range_g_pk <= 0:
        raise ConfigError(f"{where}: sensor.sensitivity_mv_per_g and range_g_pk must be > 0")
    if not 0 < sn.f_min_hz < sn.f_max_hz:
        raise ConfigError(f"{where}: sensor.f_min_hz must be > 0 and < f_max_hz")
    if sn.mass_kg < 0 or sn.iepe_bias_max_v < 0:
        raise ConfigError(f"{where}: sensor.mass_kg and iepe_bias_max_v must be >= 0")
    if not 0 <= sn.iepe_min_ma <= sn.iepe_max_ma:
        raise ConfigError(f"{where}: sensor.iepe_min_ma must be >= 0 and <= iepe_max_ma")


def validate_settings(cfg: Settings, where: str = "settings") -> None:
    d, c, s = cfg.daq, cfg.control, cfg.safety
    validate_daq(d, where)
    if d.ao_queue_blocks < 1:
        raise ConfigError(f"{where}: daq.ao_queue_blocks must be >= 1")
    if isinstance(c.frame_size, str):
        frame_ok = c.frame_size == "auto"
    else:
        frame_ok = c.frame_size >= 256 and c.frame_size % 2 == 0
    if not frame_ok:
        raise ConfigError(f"{where}: control.frame_size must be \"auto\" or an even number "
                          ">= 256")
    if c.line_spacing_hz <= 0:
        raise ConfigError(f"{where}: control.line_spacing_hz must be > 0")
    if not 0 < c.correction_gain <= 1:
        raise ConfigError(f"{where}: control.correction_gain must be in (0, 1]")
    if c.dof < 2 or c.control_dof < 2 or c.frf_averages < 1:
        raise ConfigError(f"{where}: control.dof/control_dof must be >= 2, frf_averages >= 1")
    if c.start_level_db > 0 or c.level_step_db <= 0:
        raise ConfigError(f"{where}: control.start_level_db must be <= 0 and level_step_db > 0")
    validate_sensor(cfg.sensor, where)
    if d.iepe_current_ma > 0:
        sn = cfg.sensor
        if sn.iepe_max_ma <= 0:
            raise ConfigError(f"{where}: daq.iepe_current_ma = {d.iepe_current_ma:g} mA, but "
                              f"the {sn.name} is not an IEPE sensor")
        if not sn.iepe_min_ma <= d.iepe_current_ma <= sn.iepe_max_ma:
            raise ConfigError(f"{where}: daq.iepe_current_ma = {d.iepe_current_ma:g} mA is "
                              f"outside the {sn.name} range {sn.iepe_min_ma:g}-"
                              f"{sn.iepe_max_ma:g} mA")
        swing_v = sn.range_g_pk * sn.sensitivity_mv_per_g / 1000
        if sn.iepe_bias_max_v > 0 and sn.iepe_bias_max_v + swing_v > d.limits.iepe_compliance_v:
            raise ConfigError(f"{where}: the {sn.name} output reaches {sn.iepe_bias_max_v:g} V "
                              f"bias + {swing_v:.2f} V at {sn.range_g_pk:g} g, above the "
                              f"{d.model} IEPE compliance voltage "
                              f"{d.limits.iepe_compliance_v:g} V")
    sc = cfg.sine
    if not 0 < sc.correction_gain <= 1:
        raise ConfigError(f"{where}: sine.correction_gain must be in (0, 1]")
    if sc.max_step_db <= 0 or sc.max_correction_db <= 0 or sc.ramp_s <= 0 \
            or sc.settled_blocks < 1:
        raise ConfigError(f"{where}: sine.max_step_db, max_correction_db and ramp_s must be > 0, "
                          "settled_blocks >= 1")
    if s.ai_range_margin_db < 0:
        raise ConfigError(f"{where}: safety.ai_range_margin_db must be >= 0")
    if cfg.pretest.drive_rms_v <= 0:
        raise ConfigError(f"{where}: pretest.drive_rms_v must be > 0")


def validate_setup(settings: Settings, shaker: ShakerConfig) -> None:
    """Checks that need both the shaker (amplifier drive limits) and the settings/DAQ."""
    d = settings.daq
    if shaker.max_drive_v > d.ao_range_v:
        raise ConfigError(f"{shaker.name}: max_drive_v = {shaker.max_drive_v:g} V exceeds the "
                          f"{d.model} AO range daq.ao_range_v = {d.ao_range_v:g} V")
    if settings.pretest.drive_rms_v > shaker.max_drive_rms_v:
        raise ConfigError(f"pretest.drive_rms_v = {settings.pretest.drive_rms_v:g} V exceeds "
                          f"{shaker.name} max_drive_rms_v = {shaker.max_drive_rms_v:g} V")
