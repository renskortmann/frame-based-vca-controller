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


def preflight(profile: Profile | SineProfile, shaker: ShakerConfig, settings: Settings,
              level_db: float = 0.0) -> PreflightReport:
    """Check a profile at ``level_db`` against the shaker and settings limits before any output."""
    if isinstance(profile, SineProfile):
        return preflight_sine(profile, shaker, settings, level_db)
    k = 10 ** (level_db / 20)                 # amplitude factor
    sigma = settings.safety.displacement_sigma
    fs = settings.fs_control_hz
    df = fs / settings.control.frame_size
    a_rms = profile.accel_rms_g() * k
    v_pk = profile.velocity_rms_m_s() * k * sigma
    d_pp_mm = 2 * profile.displacement_rms_m() * k * sigma * 1e3
    mass = shaker.moving_mass_kg + settings.safety.payload_kg
    force = mass * a_rms * G

    def check(name, value, limit, unit, ok=None):
        return Check(name, value, limit, unit, value <= limit if ok is None else ok)

    checks = (
        check("payload <= static max", settings.safety.payload_kg,
              shaker.payload_static_max_kg, "kg"),
        check("accel rms", a_rms, shaker.accel_random_rms_g, "g"),
        check("force rms (m_total*a)", force, shaker.force_random_rms_n, "N"),
        check(f"velocity peak ({sigma:g} sigma)", v_pk, shaker.velocity_peak_m_s, "m/s"),
        check(f"displacement p-p ({sigma:g} s)", d_pp_mm, shaker.displacement_pp_mm, "mm"),
        check("profile f_lo >= shaker f_min", profile.f_lo, shaker.f_min_hz, "Hz",
              profile.f_lo >= shaker.f_min_hz),
        check("profile f_hi <= shaker f_max", profile.f_hi, shaker.f_max_hz, "Hz",
              profile.f_hi <= shaker.f_max_hz),
        check("profile f_hi <= 0.6 * fs/2", profile.f_hi, 0.3 * fs, "Hz"),
        check("profile f_lo >= 2 lines", profile.f_lo, 2 * df, "Hz", profile.f_lo >= 2 * df),
    )
    sensor = settings.sensor
    if sensor.type == "displacement":
        checks += (check("profile f_hi <= sensor f_max", profile.f_hi, sensor.f_max_hz, "Hz"),)
    return PreflightReport(checks)


def preflight_sine(profile: SineProfile, shaker: ShakerConfig, settings: Settings,
                   level_db: float = 0.0) -> PreflightReport:
    """Sine profile at its highest level (``levels_db``) plus ``level_db``: peak values over the
    test band against the shaker's sine ratings."""
    k = 10 ** ((level_db + profile.max_level_db) / 20)
    fs = settings.fs_control_hz
    df = fs / settings.control.frame_size
    if profile.kind == "stepped_sine":
        f = np.array(profile.frequencies_hz)
    else:
        f = np.geomspace(profile.f_lo, profile.f_hi, 2000)
    a_pk = float(np.max(profile.accel_pk_g(f))) * k
    v_pk = float(np.max(profile.velocity_pk_m_s(f))) * k
    d_pp = float(np.max(profile.displacement_pp_mm(f))) * k
    force = (shaker.moving_mass_kg + settings.safety.payload_kg) * a_pk * G

    def check(name, value, limit, unit, ok=None):
        return Check(name, value, limit, unit, value <= limit if ok is None else ok)

    checks = (
        check("payload <= static max", settings.safety.payload_kg,
              shaker.payload_static_max_kg, "kg"),
        check("accel peak", a_pk, shaker.accel_sine_peak_g, "g"),
        check("force peak (m_total*a)", force, shaker.force_sine_peak_n, "N"),
        check("velocity peak", v_pk, shaker.velocity_peak_m_s, "m/s"),
        check("displacement p-p", d_pp, shaker.displacement_pp_mm, "mm"),
        check("profile f_lo >= shaker f_min", profile.f_lo, shaker.f_min_hz, "Hz",
              profile.f_lo >= shaker.f_min_hz),
        check("profile f_hi <= shaker f_max", profile.f_hi, shaker.f_max_hz, "Hz",
              profile.f_hi <= shaker.f_max_hz),
        check("profile f_hi <= 0.6 * fs/2", profile.f_hi, 0.3 * fs, "Hz"),
        check("profile f_lo >= 2 lines", profile.f_lo, 2 * df, "Hz", profile.f_lo >= 2 * df),
    )
    if settings.sensor.type == "displacement":
        checks += (check("profile f_hi <= sensor f_max", profile.f_hi,
                         settings.sensor.f_max_hz, "Hz"),)
    return PreflightReport(checks)


class RuntimeMonitor:
    """Per-block checks. Each method raises AbortError when a limit is violated."""

    def __init__(self, settings: Settings, shaker: ShakerConfig):
        self.s = settings.safety
        self.ai_range = settings.daq.ai_range_v
        self.shaker = shaker
        self.sensor = settings.sensor
        self.low_response_blocks = 0

    def check_io(self, ai_peak_v: float, drive_rms_v: float, clip_fraction: float) -> None:
        if ai_peak_v >= self.s.ai_overload_fraction * self.ai_range:
            raise AbortError(f"AI overload: {ai_peak_v:.2f} V on the {self.ai_range:g} V range "
                             "(check charge amplifier range/sensitivity)")
        if drive_rms_v > self.shaker.max_drive_rms_v:
            raise AbortError(f"drive rms {drive_rms_v:.3f} V exceeds limit "
                             f"{self.shaker.max_drive_rms_v:.3f} V")
        if clip_fraction > self.s.max_clip_fraction:
            raise AbortError(f"drive clipping: {100 * clip_fraction:.2f}% of samples at "
                             f"+/-{self.shaker.max_drive_v:g} V")

    def check_sensor_range(self, min_mm: float, max_mm: float) -> None:
        """Displacement sensor: the target must stay inside the configured window."""
        if self.sensor.type != "displacement":
            return
        lo, hi = self.sensor.range_min_mm, self.sensor.range_max_mm
        if min_mm < lo or max_mm > hi:
            raise AbortError(f"sensor range: displacement {min_mm:.3f} .. {max_mm:.3f} mm "
                             f"outside [{lo:g}, {hi:g}] mm (target out of the laser's range?)")

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
