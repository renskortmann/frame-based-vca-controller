"""Safety: pre-flight checks of a profile against shaker limits, and runtime monitors."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import ShakerConfig, Settings
from .profile import G, Profile, SineProfile


class AbortError(RuntimeError):
    """Raised to stop the test; the controller ramps the drive down and zeroes AO."""


@dataclass(frozen=True)
class Check:
    name: str
    value: float
    limit: float
    unit: str
    ok: bool


@dataclass(frozen=True)
class PreflightReport:
    checks: tuple

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def format(self) -> str:
        lines = [f"{'check':<28}{'value':>12}{'limit':>12}  unit  result"]
        for c in self.checks:
            lines.append(f"{c.name:<28}{c.value:>12.4g}{c.limit:>12.4g}  {c.unit:<5} "
                         f"{'ok' if c.ok else 'FAIL'}")
        return "\n".join(lines)


@dataclass(frozen=True)
class AiRange:
    """AI range for one run (the AI task keeps one range from noise floor to ramp-down)."""
    range_v: float
    auto: bool              # chosen by select_ai_range, not set by hand
    peak_v: float           # expected AI peak at the target level
    required_v: float       # peak with margin, as a fraction of the range it may use

    def format(self) -> str:
        how = "auto" if self.auto else "manual"
        return f"+/-{self.range_v:g} V ({how}; expected peak {self.peak_v:.3g} V)"


def expected_accel_peak_g(profile: Profile | SineProfile, settings: Settings,
                          level_db: float = 0.0) -> float:
    """Expected acceleration peak at ``level_db``: crest factor x rms (random) or the highest sine
    peak over the band at the highest level (sine)."""
    if isinstance(profile, SineProfile):
        k = 10 ** ((level_db + profile.max_level_db) / 20)
        if profile.kind == "stepped_sine":
            f = np.array(profile.frequencies_hz)
        else:
            f = np.geomspace(profile.f_lo, profile.f_hi, 2000)
        return float(np.max(profile.accel_pk_g(f))) * k
    return profile.accel_rms_g() * 10 ** (level_db / 20) * settings.safety.accel_crest_factor


def select_ai_range(profile: Profile | SineProfile, settings: Settings, level_db: float = 0.0,
                    override: float | str | None = None) -> AiRange:
    """AI range for a run at ``level_db``: ``override`` (CLI) or ``daq.ai_range_v``; ``"auto"``
    takes the smallest range of the device that holds the expected peak plus
    ``safety.ai_range_margin_db`` below ``safety.ai_overload_fraction`` of the range (the largest
    range if none does; the pre-flight check then fails)."""
    s = settings.safety
    peak_v = expected_accel_peak_g(profile, settings, level_db) * settings.v_per_g
    required_v = peak_v * 10 ** (s.ai_range_margin_db / 20) / s.ai_overload_fraction
    choice = settings.daq.ai_range_v if override is None else override
    if choice != "auto":
        return AiRange(float(choice), False, peak_v, required_v)
    ranges = sorted(settings.daq.limits.ai_ranges_v)
    fit = [r for r in ranges if r >= required_v]
    return AiRange(fit[0] if fit else ranges[-1], True, peak_v, required_v)


def _ai_range_check(ai: AiRange, settings: Settings) -> Check:
    s = settings.safety
    margin = 10 ** (s.ai_range_margin_db / 20)
    return Check(f"AI peak (+{s.ai_range_margin_db:g} dB) <= range", ai.peak_v * margin,
                 ai.range_v * s.ai_overload_fraction, "V",
                 ai.peak_v * margin <= ai.range_v * s.ai_overload_fraction)


def preflight(profile: Profile | SineProfile, shaker: ShakerConfig, settings: Settings,
              level_db: float = 0.0, ai_range: AiRange | None = None) -> PreflightReport:
    """Check a profile at ``level_db`` against the shaker and settings limits before any output.

    ``ai_range``: the AI range of the run (default: ``select_ai_range`` from the settings).
    """
    if ai_range is None:
        ai_range = select_ai_range(profile, settings, level_db)
    if isinstance(profile, SineProfile):
        return preflight_sine(profile, shaker, settings, level_db, ai_range)
    k = 10 ** (level_db / 20)                 # amplitude factor
    sigma = settings.safety.displacement_sigma
    fs = settings.fs_control_hz
    df = fs / settings.control.frame_size
    a_rms = profile.accel_rms_g() * k
    v_pk = profile.velocity_rms_m_s() * k * sigma
    d_pp_mm = 2 * profile.displacement_rms_m() * k * sigma * 1e3
    mass = shaker.moving_mass_kg + settings.payload_total_kg
    force = mass * a_rms * G
    crest = settings.safety.accel_crest_factor
    a_pk = expected_accel_peak_g(profile, settings, level_db)
    sensor = settings.sensor

    def check(name, value, limit, unit, ok=None):
        return Check(name, value, limit, unit, value <= limit if ok is None else ok)

    checks = (
        check("payload <= static max", settings.payload_total_kg,
              shaker.payload_static_max_kg, "kg"),
        check("accel rms", a_rms, shaker.accel_random_rms_g, "g"),
        check(f"accel peak ({crest:g} s) <= sensor", a_pk, sensor.range_g_pk, "g"),
        _ai_range_check(ai_range, settings),
        check("force rms (m_total*a)", force, shaker.force_random_rms_n, "N"),
        check(f"velocity peak ({sigma:g} sigma)", v_pk, shaker.velocity_peak_m_s, "m/s"),
        check(f"displacement p-p ({sigma:g} s)", d_pp_mm, shaker.displacement_pp_mm, "mm"),
        check("profile f_lo >= shaker f_min", profile.f_lo, shaker.f_min_hz, "Hz",
              profile.f_lo >= shaker.f_min_hz),
        check("profile f_hi <= shaker f_max", profile.f_hi, shaker.f_max_hz, "Hz",
              profile.f_hi <= shaker.f_max_hz),
        check("profile f_hi <= DAQ band", profile.f_hi, settings.f_band_max_hz, "Hz"),
        check("profile f_lo >= 2 lines", profile.f_lo, 2 * df, "Hz", profile.f_lo >= 2 * df),
        check("profile f_lo >= sensor f_min", profile.f_lo, sensor.f_min_hz, "Hz",
              profile.f_lo >= sensor.f_min_hz),
        check("profile f_hi <= sensor f_max", profile.f_hi, sensor.f_max_hz, "Hz"),
    )
    return PreflightReport(checks)


def preflight_sine(profile: SineProfile, shaker: ShakerConfig, settings: Settings,
                   level_db: float = 0.0, ai_range: AiRange | None = None) -> PreflightReport:
    """Sine profile at its highest level (``levels_db``) plus ``level_db``: peak values over the
    test band against the shaker's sine ratings."""
    if ai_range is None:
        ai_range = select_ai_range(profile, settings, level_db)
    k = 10 ** ((level_db + profile.max_level_db) / 20)
    fs = settings.fs_control_hz
    df = fs / settings.control.frame_size
    if profile.kind == "stepped_sine":
        f = np.array(profile.frequencies_hz)
    else:
        f = np.geomspace(profile.f_lo, profile.f_hi, 2000)
    a_pk = expected_accel_peak_g(profile, settings, level_db)
    v_pk = float(np.max(profile.velocity_pk_m_s(f))) * k
    d_pp = float(np.max(profile.displacement_pp_mm(f))) * k
    force = (shaker.moving_mass_kg + settings.payload_total_kg) * a_pk * G
    sensor = settings.sensor

    def check(name, value, limit, unit, ok=None):
        return Check(name, value, limit, unit, value <= limit if ok is None else ok)

    checks = (
        check("payload <= static max", settings.payload_total_kg,
              shaker.payload_static_max_kg, "kg"),
        check("accel peak", a_pk, shaker.accel_sine_peak_g, "g"),
        check("accel peak <= sensor range", a_pk, sensor.range_g_pk, "g"),
        _ai_range_check(ai_range, settings),
        check("force peak (m_total*a)", force, shaker.force_sine_peak_n, "N"),
        check("velocity peak", v_pk, shaker.velocity_peak_m_s, "m/s"),
        check("displacement p-p", d_pp, shaker.displacement_pp_mm, "mm"),
        check("profile f_lo >= shaker f_min", profile.f_lo, shaker.f_min_hz, "Hz",
              profile.f_lo >= shaker.f_min_hz),
        check("profile f_hi <= shaker f_max", profile.f_hi, shaker.f_max_hz, "Hz",
              profile.f_hi <= shaker.f_max_hz),
        check("profile f_hi <= DAQ band", profile.f_hi, settings.f_band_max_hz, "Hz"),
        check("profile f_lo >= 2 lines", profile.f_lo, 2 * df, "Hz", profile.f_lo >= 2 * df),
        check("profile f_lo >= sensor f_min", profile.f_lo, sensor.f_min_hz, "Hz",
              profile.f_lo >= sensor.f_min_hz),
        check("profile f_hi <= sensor f_max", profile.f_hi, sensor.f_max_hz, "Hz"),
    )
    return PreflightReport(checks)


class RuntimeMonitor:
    """Per-block checks. Each method raises AbortError when a limit is violated."""

    def __init__(self, settings: Settings, shaker: ShakerConfig, ai_range: AiRange | None = None):
        self.s = settings.safety
        if ai_range is None:
            if settings.daq.ai_range_v == "auto":
                raise ValueError("RuntimeMonitor: daq.ai_range_v is \"auto\"; pass the AiRange "
                                 "chosen with select_ai_range")
            ai_range = AiRange(float(settings.daq.ai_range_v), False, 0.0, 0.0)
        self.ai_range = ai_range.range_v
        self.ai_auto = ai_range.auto
        self.shaker = shaker
        self.sensor = settings.sensor
        self.v_per_g = settings.v_per_g
        self.low_response_blocks = 0

    def check_io(self, ai_peak_v: float, drive_rms_v: float, clip_fraction: float) -> None:
        if ai_peak_v >= self.s.ai_overload_fraction * self.ai_range:
            how = "selected automatically for the target level" if self.ai_auto else "set by hand"
            raise AbortError(f"AI overload: {ai_peak_v:.3g} V on the +/-{self.ai_range:g} V range "
                             f"({how}; check the sensor sensitivity and conditioner gain, or "
                             "choose a larger range with --ai-range)")
        if ai_peak_v / self.v_per_g > self.sensor.range_g_pk:
            raise AbortError(f"sensor over range: {ai_peak_v / self.v_per_g:.2f} g peak exceeds "
                             f"the {self.sensor.name} range {self.sensor.range_g_pk:g} g")
        if drive_rms_v > self.shaker.max_drive_rms_v:
            raise AbortError(f"drive rms {drive_rms_v:.3f} V exceeds limit "
                             f"{self.shaker.max_drive_rms_v:.3f} V")
        if clip_fraction > self.s.max_clip_fraction:
            raise AbortError(f"drive clipping: {100 * clip_fraction:.2f}% of samples at "
                             f"+/-{self.shaker.max_drive_v:g} V")

    def check_response(self, response_rms_g: float) -> None:
        if response_rms_g > self.shaker.accel_random_rms_g:
            raise AbortError(f"response {response_rms_g:.2f} g rms exceeds shaker limit "
                             f"{self.shaker.accel_random_rms_g:g} g rms")

    def check_response_peak(self, response_pk_g: float) -> None:
        if response_pk_g > self.shaker.accel_sine_peak_g:
            raise AbortError(f"response {response_pk_g:.2f} g peak exceeds shaker limit "
                             f"{self.shaker.accel_sine_peak_g:g} g peak")

    def check_open_loop(self, response_rms_g: float, reference_rms_g: float) -> None:
        """Detect loss of the feedback signal (sensor or amplifier disconnected)."""
        if response_rms_g < reference_rms_g * 10 ** (-self.s.open_loop_db / 20):
            self.low_response_blocks += 1
            if self.low_response_blocks >= 3:
                raise AbortError(f"open loop: response {response_rms_g:.3g} g rms vs reference "
                                 f"{reference_rms_g:.3g} g rms (sensor/amplifier disconnected?)")
        else:
            self.low_response_blocks = 0
