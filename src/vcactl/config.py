"""Loading and validation of TOML configuration files (shakers, DAQ devices, settings)."""

from __future__ import annotations

import dataclasses
import math
import sys
import typing
from dataclasses import dataclass, field
from pathlib import Path

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
    ao_sync: str                          # "sample_clock": AO uses ai/SampleClock;
                                          # "start_trigger": AO starts on ai/StartTrigger
    ao_on_demand: bool                    # AO supports software-timed (on-demand) writes
    filter_delay_samples: float           # AO + AI converter filter delay in fs_io samples


@dataclass(frozen=True)
class DaqConfig:
    model: str
    limits: DaqLimits
    device: str = "Dev1"
    ao_channel: str = "ao0"
    ai_channel: str = "ai1"
    ai_terminal_config: str = "DIFF"
    ai_range_v: float = 10.0
    ai_coupling: str = "DC"
    iepe_current_ma: float = 0.0
    ao_range_v: float = 10.0
    ao_terminal_config: str = ""          # empty: driver default
    fs_io_hz: float = 100000.0
    decimation: int = 4
    ao_queue_blocks: int = 2


SENSOR_TYPES = ("accelerometer", "displacement")


@dataclass(frozen=True)
class SensorConfig:
    """Control sensor: an accelerometer (mV/g) or a displacement sensor (mm/V).

    A displacement signal is converted to acceleration per spectral line by the controller.
    """
    type: str = "accelerometer"           # "accelerometer" or "displacement"
    sensitivity_mv_per_g: float = 10.0    # accelerometer chain sensitivity at the DAQ input
    mm_per_v: float = 0.0                 # displacement: mm = mm_per_v * V + offset_mm
    offset_mm: float = 0.0
    range_min_mm: float = -math.inf       # displacement: abort outside this window
    range_max_mm: float = math.inf
    f_max_hz: float = math.inf            # displacement: usable sensor bandwidth (pre-flight)


@dataclass(frozen=True)
class ControlConfig:
    frame_size: int = 4096
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
class SafetyConfig:
    max_clip_fraction: float = 0.005
    payload_kg: float = 0.02
    displacement_sigma: float = 3.0
    open_loop_db: float = 12.0
    ai_overload_fraction: float = 0.98
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
    sensor: SensorConfig = field(default_factory=SensorConfig)
    control: ControlConfig = field(default_factory=ControlConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    pretest: PretestConfig = field(default_factory=PretestConfig)

    @property
    def fs_control_hz(self) -> float:
        return self.daq.fs_io_hz / self.daq.decimation


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
                 config_dir: Path = DEFAULT_CONFIG_DIR) -> Settings:
    """Load a settings file and combine it with the DAQ device profile ``daq``.

    The settings file may have a ``[daq]`` table that overrides settings of the DAQ profile
    (for example ``device`` or ``ai_channel``), but not its ``[limits]``.
    """
    path = Path(path) if path else DEFAULT_CONFIG_DIR / "settings.toml"
    daq_path = resolve_config_path(daq, "daq", config_dir)
    table = read_toml(path)
    daq_table = read_toml(daq_path)
    overrides = table.get("daq", {})
    if not isinstance(overrides, dict) or "limits" in overrides or "model" in overrides:
        raise ConfigError(f"{path}: [daq] may only override DAQ settings, not model or limits")
    table = {**table, "daq": {**daq_table, **overrides}}
    where = f"{path} + {daq_path}"
    cfg = build_dataclass(Settings, table, where)
    validate_settings(cfg, where)
    return cfg


def _one_of(value: float, allowed: tuple[float, ...]) -> bool:
    return any(abs(value - a) <= 1e-6 * max(abs(a), 1.0) for a in allowed)


def _fmt(values) -> str:
    return ", ".join(f"{v:g}" if isinstance(v, float) else str(v) for v in values)


def validate_daq(d: DaqConfig, where: str = "daq") -> None:
    lim = d.limits
    if lim.ao_sync not in ("sample_clock", "start_trigger"):
        raise ConfigError(f"{where}: limits.ao_sync must be sample_clock or start_trigger")
    if d.ai_terminal_config.upper() not in lim.ai_terminal_configs:
        raise ConfigError(f"{where}: daq.ai_terminal_config must be one of "
                          f"{_fmt(lim.ai_terminal_configs)} ({d.model})")
    if not _one_of(d.ai_range_v, lim.ai_ranges_v):
        raise ConfigError(f"{where}: daq.ai_range_v must be one of {_fmt(lim.ai_ranges_v)} "
                          f"({d.model})")
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
    if not lim.fs_min_hz <= d.fs_io_hz <= lim.fs_max_hz:
        raise ConfigError(f"{where}: daq.fs_io_hz must be in [{lim.fs_min_hz:g}, "
                          f"{lim.fs_max_hz:g}] ({d.model})")
    if lim.fs_allowed_hz and not _one_of(d.fs_io_hz, lim.fs_allowed_hz):
        raise ConfigError(f"{where}: daq.fs_io_hz must be one of {_fmt(lim.fs_allowed_hz)} "
                          f"({d.model})")
    if lim.timebase_hz > 0:
        n = lim.timebase_hz / d.fs_io_hz
        if abs(n - round(n)) > 1e-9 * n:
            raise ConfigError(f"{where}: daq.fs_io_hz must divide {lim.timebase_hz:g} exactly "
                              f"({d.model})")
    if d.decimation < 1:
        raise ConfigError(f"{where}: daq.decimation must be >= 1")
    if not lim.antialias_filter and d.decimation < 2:
        raise ConfigError(f"{where}: daq.decimation must be >= 2: the {d.model} has no "
                          "anti-aliasing filter, so AI must be oversampled and filtered digitally")


def validate_settings(cfg: Settings, where: str = "settings") -> None:
    d, c, s = cfg.daq, cfg.control, cfg.safety
    validate_daq(d, where)
    if d.ao_queue_blocks < 1:
        raise ConfigError(f"{where}: daq.ao_queue_blocks must be >= 1")
    if c.frame_size < 256 or c.frame_size % 2:
        raise ConfigError(f"{where}: control.frame_size must be even and >= 256")
    if not 0 < c.correction_gain <= 1:
        raise ConfigError(f"{where}: control.correction_gain must be in (0, 1]")
    if c.dof < 2 or c.control_dof < 2 or c.frf_averages < 1:
        raise ConfigError(f"{where}: control.dof/control_dof must be >= 2, frf_averages >= 1")
    if c.start_level_db > 0 or c.level_step_db <= 0:
        raise ConfigError(f"{where}: control.start_level_db must be <= 0 and level_step_db > 0")
    sn = cfg.sensor
    if sn.type not in SENSOR_TYPES:
        raise ConfigError(f"{where}: sensor.type must be one of {_fmt(SENSOR_TYPES)}")
    if sn.type == "accelerometer" and sn.sensitivity_mv_per_g <= 0:
        raise ConfigError(f"{where}: sensor.sensitivity_mv_per_g must be > 0")
    if sn.type == "displacement":
        if sn.mm_per_v == 0 or not math.isfinite(sn.mm_per_v):
            raise ConfigError(f"{where}: sensor.mm_per_v must be a non-zero number")
        if not sn.range_min_mm < sn.range_max_mm:
            raise ConfigError(f"{where}: sensor.range_min_mm must be < range_max_mm")
        if sn.f_max_hz <= 0:
            raise ConfigError(f"{where}: sensor.f_max_hz must be > 0")
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
