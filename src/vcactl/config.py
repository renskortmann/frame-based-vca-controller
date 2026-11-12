"""Loading and validation of TOML configuration files (shakers, DAQ devices, sensors, settings)."""

from __future__ import annotations

import dataclasses
import math
import sys
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

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
    # AI channels in one task with the device's terminal configuration (USB-6211: 8 differential);
    # 0: not checked
    ai_channels_max: int = 0
    # multiplexed devices (one ADC for all channels): channels x rate may not exceed this;
    # 0: simultaneous sampling, not checked
    ai_aggregate_rate_hz: float = 0.0

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


SENSOR_UNITS = ("g", "N")


@dataclass(frozen=True)
class SensorConfig:
    """An accelerometer (unit g) or force sensor (unit N), from its data sheet and calibration.
    The control sensor must be an accelerometer."""
    name: str
    f_min_hz: float = 0.0                 # frequency range (data-sheet tolerance band); 0, 0:
    f_max_hz: float = 0.0                 # not given (allowed for recorded channels only)
    unit: str = "g"
    sensitivity_mv_per_g: float = 0.0     # calibrated sensitivity of the axis used (unit g)
    range_g_pk: float = 0.0               # measurement range (unit g)
    sensitivity_mv_per_n: float = 0.0     # calibrated sensitivity (unit N)
    range_n_pk: float = 0.0               # measurement range (unit N)
    mass_kg: float = 0.0                  # added to safety.payload_kg (control sensor)
    iepe_min_ma: float = 0.0              # IEPE excitation current range; 0, 0 = not IEPE
    iepe_max_ma: float = 0.0
    iepe_bias_max_v: float = 0.0          # highest IEPE output bias voltage; 0: unknown, not checked

    @property
    def mv_per_unit(self) -> float:
        return self.sensitivity_mv_per_n if self.unit == "N" else self.sensitivity_mv_per_g

    @property
    def range_pk(self) -> float:
        return self.range_n_pk if self.unit == "N" else self.range_g_pk

    @property
    def is_iepe(self) -> bool:
        return self.iepe_max_ma > 0


CHANNEL_ROLES = ("control", "response", "force", "hammer", "loopback")
AUX_ROLES = ("load", "drive_ref")


@dataclass(frozen=True)
class AuxConfig:
    """A second DAQ that records alongside the control DAQ (``[aux]`` of a channels file: the
    treatment load cells on a USB-6211). Its clock is not shared: both start on one trigger
    (``trigger_out`` of the aux DAQ wired to ``trigger_in`` of the control DAQ) and the drift is
    removed afterwards from ``drive_ref``, the AO drive recorded on both (vcactl.analysis.sync)."""
    daq: DaqConfig
    rate_hz: float
    channels: tuple                       # AiChannel, roles in AUX_ROLES
    trigger_out: str = "PFI4"             # aux DAQ terminal that exports its AI start trigger
    trigger_in: str = "PFI0"              # control DAQ terminal its AI task starts on


@dataclass(frozen=True)
class ChannelConfig:
    """One ``[[channel]]`` of a channels file (``config/channels/*.toml``)."""
    name: str                             # column name in the recordings (e.g. "force")
    physical: str                         # AI channel on the device (e.g. "ai1")
    sensor: str = ""                      # sensor file key or path (not for role "loopback")
    role: str = "response"                # see CHANNEL_ROLES
    sensitivity: float = 0.0              # calibrated mV per sensor unit; 0: the sensor file's
    iepe_current_ma: float = -1.0         # -1: the DAQ's IEPE current for IEPE sensors, else off
    coupling: str = ""                    # "": AC with IEPE, else the DAQ file's ai_coupling
    range_v: Union[float, str] = "auto"   # "auto": smallest range for the sensor's full range


@dataclass(frozen=True)
class AiChannel:
    """A resolved AI channel: the control channel (from the DAQ file and --sensor) or a recorded
    channel of a channels file. ``range_v`` 0: chosen per run (control, loopback)."""
    name: str
    physical: str
    role: str
    sensor: SensorConfig
    mv_per_unit: float
    iepe_current_ma: float
    coupling: str
    range_v: float

    @property
    def unit(self) -> str:
        return "V" if self.role in ("loopback", "drive_ref") else self.sensor.unit

    @property
    def v_per_unit(self) -> float:
        return self.mv_per_unit / 1000


@dataclass(frozen=True)
class ControlConfig:
    frame_size: Union[int, str] = "auto"  # "auto": chosen per run (see vcactl.rates)
    line_spacing_hz: float = 6.25         # auto frame size: the largest line spacing
    dof: int = 120
    control_dof: int = 32
    frf_averages: int = 32
    correction_gain: float = 0.1
    periodic_correction_gain: float = 0.5   # periodic random: per period (one correction each)
    periodic_deadband_db: float = 1.0       # ... from the period's average, on lines off by more
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
    force_limit_n: float = 0.0            # abort above this peak on a force channel; 0: off


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
    # recorded channels besides the control channel (channels file, see load_channels)
    extra_channels: tuple[AiChannel, ...] = ()
    has_control: bool = True              # False: a channels file without a control channel
    channels_name: str = ""               # name of the channels file
    control_name: str = "control"         # name of the control channel (channels file)
    aux: Optional[AuxConfig] = None       # second DAQ recording alongside (channels file [aux])

    @property
    def control_channel(self) -> AiChannel:
        d = self.daq
        return AiChannel(name=self.control_name, physical=d.ai_channel, role="control", sensor=self.sensor,
                         mv_per_unit=self.sensor.sensitivity_mv_per_g * d.conditioner_gain,
                         iepe_current_ma=d.iepe_current_ma, coupling=d.ai_coupling, range_v=0.0)

    @property
    def channels(self) -> tuple[AiChannel, ...]:
        """All AI channels in read order: the control channel (if any) first."""
        return ((self.control_channel,) if self.has_control else ()) + self.extra_channels

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
                 config_dir: Path = DEFAULT_CONFIG_DIR, sensor: str = "pcb352b") -> Settings:
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
    if sn.unit not in SENSOR_UNITS:
        raise ConfigError(f"{where}: sensor.unit must be one of {_fmt(SENSOR_UNITS)}")
    if sn.unit == "g" and (sn.sensitivity_mv_per_g <= 0 or sn.range_g_pk <= 0):
        raise ConfigError(f"{where}: sensor.sensitivity_mv_per_g and range_g_pk must be > 0")
    if sn.unit == "N" and (sn.sensitivity_mv_per_n <= 0 or sn.range_n_pk <= 0):
        raise ConfigError(f"{where}: sensor.sensitivity_mv_per_n and range_n_pk must be > 0 "
                          "(unit N)")
    if (sn.f_min_hz, sn.f_max_hz) != (0.0, 0.0) and not 0 < sn.f_min_hz < sn.f_max_hz:
        raise ConfigError(f"{where}: sensor.f_min_hz must be > 0 and < f_max_hz (or both 0: "
                          "not given)")
    if sn.mass_kg < 0 or sn.iepe_bias_max_v < 0:
        raise ConfigError(f"{where}: sensor.mass_kg and iepe_bias_max_v must be >= 0")
    if not 0 <= sn.iepe_min_ma <= sn.iepe_max_ma:
        raise ConfigError(f"{where}: sensor.iepe_min_ma must be >= 0 and <= iepe_max_ma")


def _check_iepe(sn: SensorConfig, current_ma: float, d: DaqConfig, where: str,
                what: str) -> None:
    """An IEPE current ``current_ma`` (> 0) must suit the sensor, and the sensor's bias plus its
    swing at full range must stay below the DAQ's IEPE compliance voltage."""
    if current_ma <= 0:
        return
    if not sn.is_iepe:
        raise ConfigError(f"{where}: {what} = {current_ma:g} mA, but the {sn.name} is not an "
                          "IEPE sensor")
    if not sn.iepe_min_ma <= current_ma <= sn.iepe_max_ma:
        raise ConfigError(f"{where}: {what} = {current_ma:g} mA is outside the {sn.name} range "
                          f"{sn.iepe_min_ma:g}-{sn.iepe_max_ma:g} mA")
    swing_v = sn.range_pk * sn.mv_per_unit / 1000
    if sn.iepe_bias_max_v > 0 and sn.iepe_bias_max_v + swing_v > d.limits.iepe_compliance_v:
        raise ConfigError(f"{where}: the {sn.name} output reaches {sn.iepe_bias_max_v:g} V "
                          f"bias + {swing_v:.2f} V at {sn.range_pk:g} {sn.unit}, above the "
                          f"{d.model} IEPE compliance voltage {d.limits.iepe_compliance_v:g} V")


def _channel_key(physical: str) -> str:
    return physical.strip().lower()


def validate_channels(cfg: Settings, where: str = "settings") -> None:
    """Checks of the channel set: distinct names and physical channels, the device's channel
    count, and per recorded channel its sensor, IEPE current, coupling and range."""
    d = cfg.daq
    lim = d.limits
    chans = cfg.channels
    names = [c.name for c in chans]
    if len(set(names)) != len(names):
        raise ConfigError(f"{where}: channel names must be distinct, got {names}")
    phys = [_channel_key(c.physical) for c in chans]
    if len(set(phys)) != len(phys):
        raise ConfigError(f"{where}: each physical AI channel may be used once, got "
                          f"{[c.physical for c in chans]}")
    if lim.ai_channels_max and len(chans) > lim.ai_channels_max:
        raise ConfigError(f"{where}: {len(chans)} AI channels, but the {d.model} has "
                          f"{lim.ai_channels_max}")
    for c in cfg.extra_channels:
        w = f"{where}: channel '{c.name}'"
        if c.role not in CHANNEL_ROLES or c.role == "control":
            raise ConfigError(f"{w}: role must be one of {_fmt(CHANNEL_ROLES[1:])}")
        if c.role == "loopback":
            continue
        validate_sensor(c.sensor, w)
        if c.mv_per_unit <= 0:
            raise ConfigError(f"{w}: sensitivity must be > 0")
        if c.role in ("force", "hammer") and c.sensor.unit != "N":
            raise ConfigError(f"{w}: role {c.role} needs a force sensor (unit N)")
        if c.iepe_current_ma > 0 and not _one_of(c.iepe_current_ma, lim.iepe_currents_ma):
            raise ConfigError(f"{w}: iepe_current_ma must be 0 or one of "
                              f"{_fmt(lim.iepe_currents_ma)} ({d.model})")
        _check_iepe(c.sensor, c.iepe_current_ma, d, w, "iepe_current_ma")
        if c.coupling.upper() not in lim.ai_couplings:
            raise ConfigError(f"{w}: coupling must be one of {_fmt(lim.ai_couplings)} ({d.model})")
        if c.iepe_current_ma > 0 and c.coupling.upper() != "AC":
            raise ConfigError(f"{w}: coupling must be AC when IEPE excitation is on")
        if not _one_of(c.range_v, lim.ai_ranges_v):
            raise ConfigError(f"{w}: range_v must be \"auto\" or one of {_fmt(lim.ai_ranges_v)} "
                              f"({d.model})")


def auto_range_v(sn: SensorConfig, mv_per_unit: float, d: DaqConfig, overload_fraction: float
                 ) -> float:
    """Smallest AI range of the device that holds the sensor's full range below
    ``overload_fraction`` of the range (the largest range if none does)."""
    need = sn.range_pk * mv_per_unit / 1000 / overload_fraction
    ranges = sorted(d.limits.ai_ranges_v)
    fit = [r for r in ranges if r >= need]
    return fit[0] if fit else ranges[-1]


def load_sensor(name_or_path: str, config_dir: Path = DEFAULT_CONFIG_DIR) -> SensorConfig:
    path = resolve_config_path(name_or_path, "sensors", config_dir)
    sn = build_dataclass(SensorConfig, read_toml(path), str(path))
    validate_sensor(sn, str(path))
    return sn


def load_channels(name_or_path: str, settings: Settings,
                  config_dir: Path = DEFAULT_CONFIG_DIR, sensor_given: bool = False) -> Settings:
    """Apply a channels file (key or path, ``config/channels/<key>.toml``) to ``settings``.

    A ``[[channel]]`` with role ``control`` replaces the control channel of the DAQ file and the
    sensor (``--sensor``); without one the run has no control channel (``vcactl record``,
    ``vcactl tap``). Every other entry is recorded alongside, in file order.
    """
    path = resolve_config_path(name_or_path, "channels", config_dir)
    table = read_toml(path)
    unknown = set(table) - {"name", "channel", "aux"}
    if unknown:
        raise ConfigError(f"{path}: unknown key(s) {sorted(unknown)}")
    entries = table.get("channel", [])
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{path}: needs at least one [[channel]]")
    d = settings.daq
    control = None
    extras = []
    for i, entry in enumerate(entries):
        where = f"{path} [[channel]] {i + 1}"
        ch = build_dataclass(ChannelConfig, entry, where)
        if ch.role not in CHANNEL_ROLES:
            raise ConfigError(f"{where}: role must be one of {_fmt(CHANNEL_ROLES)}")
        if ch.role == "loopback":
            extras.append(AiChannel(ch.name, ch.physical, "loopback", settings.sensor, 1000.0,
                                    0.0, "DC", 0.0))
            continue
        if not ch.sensor:
            raise ConfigError(f"{where}: sensor is required")
        sn = load_sensor(ch.sensor, config_dir)
        if ch.sensitivity < 0:
            raise ConfigError(f"{where}: sensitivity must be >= 0")
        if ch.sensitivity > 0:
            key = "sensitivity_mv_per_n" if sn.unit == "N" else "sensitivity_mv_per_g"
            sn = dataclasses.replace(sn, **{key: ch.sensitivity})
        if ch.iepe_current_ma >= 0:
            iepe = ch.iepe_current_ma
        elif sn.is_iepe and d.limits.iepe_currents_ma:
            iepe = d.iepe_current_ma or d.limits.iepe_currents_ma[0]
        else:
            iepe = 0.0
        coupling = (ch.coupling or ("AC" if iepe > 0 else d.ai_coupling)).upper()
        if ch.role == "control":
            if control is not None:
                raise ConfigError(f"{where}: only one channel may have role control")
            if sensor_given:
                raise ConfigError(f"{where}: the channels file sets the control sensor; leave "
                                  "out --sensor")
            if isinstance(ch.range_v, float) or ch.range_v != "auto":
                raise ConfigError(f"{where}: the control channel's range is chosen per run "
                                  "(daq.ai_range_v, --ai-range); leave range_v out")
            control = (ch, sn, iepe, coupling)
            continue
        if isinstance(ch.range_v, str):
            if ch.range_v != "auto":
                raise ConfigError(f"{where}: range_v must be \"auto\" or a number")
            rng = auto_range_v(sn, sn.mv_per_unit, d, settings.safety.ai_overload_fraction)
        else:
            rng = ch.range_v
        extras.append(AiChannel(ch.name, ch.physical, ch.role, sn, sn.mv_per_unit, iepe,
                                coupling, rng))
    out = dataclasses.replace(settings, extra_channels=tuple(extras), has_control=control is not None,
                              channels_name=str(table.get("name", path.stem)))
    if control is not None:
        ch, sn, iepe, coupling = control
        daq = dataclasses.replace(d, ai_channel=ch.physical, iepe_current_ma=iepe,
                                  ai_coupling=coupling)
        out = dataclasses.replace(out, daq=daq, sensor=sn, control_name=ch.name)
    if "aux" in table:
        out = dataclasses.replace(out, aux=_load_aux(table["aux"], out, f"{path} [aux]",
                                                     config_dir))
    validate_settings(out, str(path))
    return out


def _load_aux(table: dict, settings: Settings, where: str, config_dir: Path) -> AuxConfig:
    """``[aux]``: daq (key or path), rate_hz, trigger_out, trigger_in, the DAQ settings to
    override (device, ai_terminal_config) and ``[[aux.channel]]`` entries (roles load and
    drive_ref)."""
    if not isinstance(table, dict):
        raise ConfigError(f"{where}: expected a table")
    known = {"daq", "rate_hz", "trigger_out", "trigger_in", "channel", "device",
             "ai_terminal_config"}
    unknown = set(table) - known
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}")
    if "daq" not in table or "rate_hz" not in table:
        raise ConfigError(f"{where}: daq and rate_hz are required")
    daq_path = resolve_config_path(str(table["daq"]), "daq", config_dir)
    daq_table = read_toml(daq_path)
    for key in ("device", "ai_terminal_config"):
        if key in table:
            daq_table[key] = table[key]
    daq = build_dataclass(DaqConfig, daq_table, f"{where} daq {daq_path}")
    validate_daq(daq, f"{where} daq {daq_path}")
    rate = table["rate_hz"]
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        raise ConfigError(f"{where}: rate_hz must be a number")
    chans = []
    for i, entry in enumerate(table.get("channel", [])):
        w = f"{where} [[aux.channel]] {i + 1}"
        ch = build_dataclass(ChannelConfig, entry, w)
        if ch.role not in AUX_ROLES:
            raise ConfigError(f"{w}: role must be one of {_fmt(AUX_ROLES)}")
        if ch.role == "drive_ref":
            # the AO drive, through a BNC tee at the amplifier input: volts
            rng = ch.range_v if not isinstance(ch.range_v, str) else min(
                (r for r in daq.limits.ai_ranges_v
                 if r * settings.safety.ai_overload_fraction >= settings.daq.ao_range_v),
                default=max(daq.limits.ai_ranges_v))
            chans.append(AiChannel(ch.name, ch.physical, "drive_ref", settings.sensor, 1000.0,
                                   0.0, "DC", float(rng)))
            continue
        if not ch.sensor:
            raise ConfigError(f"{w}: sensor is required")
        sn = load_sensor(ch.sensor, config_dir)
        if ch.sensitivity > 0:
            key = "sensitivity_mv_per_n" if sn.unit == "N" else "sensitivity_mv_per_g"
            sn = dataclasses.replace(sn, **{key: ch.sensitivity})
        rng = auto_range_v(sn, sn.mv_per_unit, daq, settings.safety.ai_overload_fraction) \
            if isinstance(ch.range_v, str) else ch.range_v
        chans.append(AiChannel(ch.name, ch.physical, "load", sn, sn.mv_per_unit,
                               max(ch.iepe_current_ma, 0.0), (ch.coupling or "DC").upper(),
                               float(rng)))
    return AuxConfig(daq=daq, rate_hz=float(rate), channels=tuple(chans),
                     trigger_out=str(table.get("trigger_out", "PFI4")),
                     trigger_in=str(table.get("trigger_in", "PFI0")))


def validate_aux(cfg: Settings, where: str = "settings") -> None:
    """The aux DAQ's channels, rate and the control DAQ it starts with."""
    aux = cfg.aux
    if aux is None:
        return
    d, lim = aux.daq, aux.daq.limits
    w = f"{where} [aux]"
    if lim.driver != "nidaqmx" or cfg.daq.limits.driver != "nidaqmx":
        raise ConfigError(f"{w}: an aux DAQ is supported with NI-DAQmx devices only")
    if not cfg.daq.limits.antialias_filter:
        raise ConfigError(f"{w}: the control DAQ must be a delta-sigma (DSA) device: the aux "
                          "data are aligned to its AI samples at decimation 1")
    refs = [c for c in aux.channels if c.role == "drive_ref"]
    if len(refs) != 1:
        raise ConfigError(f"{w}: needs exactly one channel with role drive_ref (the AO drive "
                          "through a BNC tee), for the clock drift")
    n = len(aux.channels)
    if lim.ai_channels_max and n > lim.ai_channels_max:
        raise ConfigError(f"{w}: {n} channels, but the {d.model} has {lim.ai_channels_max}")
    if not lim.fs_min_hz <= aux.rate_hz <= lim.fs_max_hz:
        raise ConfigError(f"{w}: rate_hz must be in [{lim.fs_min_hz:g}, {lim.fs_max_hz:g}] "
                          f"({d.model})")
    if lim.ai_aggregate_rate_hz and n * aux.rate_hz > lim.ai_aggregate_rate_hz:
        raise ConfigError(f"{w}: {n} channels x {aux.rate_hz:g} S/s exceeds the {d.model} "
                          f"aggregate rate {lim.ai_aggregate_rate_hz:g} S/s")
    keys = [_channel_key(c.physical) for c in aux.channels]
    if len(set(keys)) != len(keys) or len({c.name for c in aux.channels}) != n:
        raise ConfigError(f"{w}: channel names and physical channels must be distinct")
    main_names = {c.name for c in cfg.channels}
    if main_names & {c.name for c in aux.channels}:
        raise ConfigError(f"{w}: channel names must differ from the control DAQ's channels")
    for c in aux.channels:
        cw = f"{w} channel '{c.name}'"
        if not _one_of(c.range_v, lim.ai_ranges_v):
            raise ConfigError(f"{cw}: range_v must be one of {_fmt(lim.ai_ranges_v)} "
                              f"({d.model})")
        if c.coupling.upper() not in lim.ai_couplings:
            raise ConfigError(f"{cw}: coupling must be one of {_fmt(lim.ai_couplings)}")
        if c.iepe_current_ma > 0:
            raise ConfigError(f"{cw}: no IEPE excitation on the aux DAQ")
        if c.role == "load":
            validate_sensor(c.sensor, cw)


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
    if c.periodic_deadband_db < 0:
        raise ConfigError(f"{where}: control.periodic_deadband_db must be >= 0")
    if c.line_spacing_hz <= 0:
        raise ConfigError(f"{where}: control.line_spacing_hz must be > 0")
    if not 0 < c.correction_gain <= 1 or not 0 < c.periodic_correction_gain <= 1:
        raise ConfigError(f"{where}: control.correction_gain and periodic_correction_gain must "
                          "be in (0, 1]")
    if c.dof < 2 or c.control_dof < 2 or c.frf_averages < 1:
        raise ConfigError(f"{where}: control.dof/control_dof must be >= 2, frf_averages >= 1")
    if c.start_level_db > 0 or c.level_step_db <= 0:
        raise ConfigError(f"{where}: control.start_level_db must be <= 0 and level_step_db > 0")
    validate_sensor(cfg.sensor, where)
    if cfg.sensor.unit != "g":
        raise ConfigError(f"{where}: the control sensor must be an accelerometer (unit g), "
                          f"not the {cfg.sensor.name} (unit {cfg.sensor.unit})")
    if cfg.sensor.f_min_hz <= 0:
        raise ConfigError(f"{where}: the control sensor needs its frequency range "
                          "(sensor.f_min_hz, f_max_hz)")
    _check_iepe(cfg.sensor, d.iepe_current_ma, d, where, "daq.iepe_current_ma")
    validate_channels(cfg, where)
    validate_aux(cfg, where)
    sc = cfg.sine
    if not 0 < sc.correction_gain <= 1:
        raise ConfigError(f"{where}: sine.correction_gain must be in (0, 1]")
    if sc.max_step_db <= 0 or sc.max_correction_db <= 0 or sc.ramp_s <= 0 \
            or sc.settled_blocks < 1:
        raise ConfigError(f"{where}: sine.max_step_db, max_correction_db and ramp_s must be > 0, "
                          "settled_blocks >= 1")
    if s.force_limit_n < 0:
        raise ConfigError(f"{where}: safety.force_limit_n must be >= 0 (0: off)")
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
