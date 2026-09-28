"""Loading and validation of TOML configuration files (shakers, station)."""

from __future__ import annotations

import dataclasses
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
    sim: SimModelConfig = field(default_factory=SimModelConfig)


@dataclass(frozen=True)
class DaqConfig:
    device: str = "Dev1"
    ao_channel: str = "ao0"
    ai_channel: str = "ai1"
    ai_terminal_config: str = "DIFF"
    ai_range_v: float = 10.0
    fs_io_hz: float = 100000.0
    decimation: int = 4
    ao_queue_blocks: int = 2


@dataclass(frozen=True)
class SensorConfig:
    sensitivity_mv_per_g: float = 10.0


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
    max_drive_v: float = 3.5
    max_drive_rms_v: float = 1.5
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
class StationConfig:
    daq: DaqConfig = field(default_factory=DaqConfig)
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


def resolve_shaker_path(name_or_path: str, config_dir: Path = DEFAULT_CONFIG_DIR) -> Path:
    """Accept a shaker key (``tv51110``) or a path to a TOML file."""
    p = Path(name_or_path)
    if p.suffix == ".toml" or p.exists():
        return p
    return config_dir / "shakers" / f"{name_or_path.lower()}.toml"


def load_shaker(name_or_path: str, config_dir: Path = DEFAULT_CONFIG_DIR) -> ShakerConfig:
    path = resolve_shaker_path(name_or_path, config_dir)
    cfg = build_dataclass(ShakerConfig, read_toml(path), str(path))
    for name in ("f_min_hz", "f_max_hz", "force_random_rms_n", "accel_random_rms_g",
                 "displacement_pp_mm", "velocity_peak_m_s", "moving_mass_kg",
                 "armature_resonance_hz", "amp_input_full_v"):
        if getattr(cfg, name) <= 0:
            raise ConfigError(f"{path}: {name} must be > 0")
    if cfg.f_min_hz >= cfg.f_max_hz:
        raise ConfigError(f"{path}: f_min_hz must be < f_max_hz")
    return cfg


def load_station(path: Path | str | None = None) -> StationConfig:
    path = Path(path) if path else DEFAULT_CONFIG_DIR / "station.toml"
    cfg = build_dataclass(StationConfig, read_toml(path), str(path))
    validate_station(cfg, str(path))
    return cfg


def validate_station(cfg: StationConfig, where: str = "station") -> None:
    d, c, s = cfg.daq, cfg.control, cfg.safety
    if d.ai_terminal_config.upper() not in ("DIFF", "RSE", "NRSE"):
        raise ConfigError(f"{where}: daq.ai_terminal_config must be DIFF, RSE or NRSE")
    if d.ai_range_v not in (0.2, 1.0, 5.0, 10.0):
        raise ConfigError(f"{where}: daq.ai_range_v must be one of 0.2, 1, 5, 10 (USB-6211)")
    if not 0 < d.fs_io_hz <= 250e3:
        raise ConfigError(f"{where}: daq.fs_io_hz must be in (0, 250000] (USB-6211 limit)")
    if d.decimation < 1:
        raise ConfigError(f"{where}: daq.decimation must be >= 1")
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
    if not 0 < s.max_drive_v <= 10:
        raise ConfigError(f"{where}: safety.max_drive_v must be in (0, 10] V")
    if not 0 < s.max_drive_rms_v <= s.max_drive_v:
        raise ConfigError(f"{where}: safety.max_drive_rms_v must be in (0, max_drive_v]")
    if cfg.sensor.sensitivity_mv_per_g <= 0:
        raise ConfigError(f"{where}: sensor.sensitivity_mv_per_g must be > 0")
    if cfg.pretest.drive_rms_v <= 0 or cfg.pretest.drive_rms_v > s.max_drive_rms_v:
        raise ConfigError(f"{where}: pretest.drive_rms_v must be in (0, safety.max_drive_rms_v]")
