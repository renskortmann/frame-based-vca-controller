"""Random vibration test profiles: breakpoint table -> acceleration PSD (g^2/Hz)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import ConfigError, build_dataclass, read_toml

G = 9.80665  # m/s^2 per g


@dataclass(frozen=True)
class Tolerance:
    alarm_db: float = 3.0
    abort_db: float = 6.0
    rms_alarm_db: float = 1.5
    rms_abort_db: float = 3.0
    max_abort_lines_pct: float = 5.0


@dataclass(frozen=True)
class Profile:
    name: str
    f_hz: tuple            # breakpoint frequencies (ascending)
    psd_g2_hz: tuple       # PSD at each breakpoint
    tolerance: Tolerance = field(default_factory=Tolerance)
    duration_s: float = 60.0

    @property
    def f_lo(self) -> float:
        return self.f_hz[0]

    @property
    def f_hi(self) -> float:
        return self.f_hz[-1]

    def _exponents(self):
        """Power-law exponent b of each segment: S = S1 * (f/f1)^b."""
        f, s = self.f_hz, self.psd_g2_hz
        return [math.log(s[i + 1] / s[i]) / math.log(f[i + 1] / f[i]) for i in range(len(f) - 1)]

    def psd(self, freqs) -> np.ndarray:
        """PSD (g^2/Hz) at ``freqs``; zero outside [f_lo, f_hi]. Log-log interpolation."""
        freqs = np.asarray(freqs, dtype=float)
        out = np.zeros_like(freqs)
        inside = (freqs >= self.f_lo) & (freqs <= self.f_hi)
        fi = freqs[inside]
        out[inside] = np.exp(np.interp(np.log(fi), np.log(self.f_hz), np.log(self.psd_g2_hz)))
        return out

    def _integrate(self, extra_exponent: float) -> float:
        """Exact integral of S(f) * f^extra_exponent over the profile band."""
        total = 0.0
        for i, b in enumerate(self._exponents()):
            f1, f2, s1 = self.f_hz[i], self.f_hz[i + 1], self.psd_g2_hz[i]
            c = s1 * f1 ** (-b)          # S = c * f^b
            e = b + extra_exponent
            if abs(e + 1) < 1e-12:
                total += c * math.log(f2 / f1)
            else:
                total += c * (f2 ** (e + 1) - f1 ** (e + 1)) / (e + 1)
        return total

    def accel_rms_g(self) -> float:
        return math.sqrt(self._integrate(0.0))

    def velocity_rms_m_s(self) -> float:
        return math.sqrt(self._integrate(-2.0) * G**2 / (2 * math.pi) ** 2)

    def displacement_rms_m(self) -> float:
        return math.sqrt(self._integrate(-4.0) * G**2 / (2 * math.pi) ** 4)


def resolve_breakpoints(raw: list, where: str) -> tuple[tuple, tuple]:
    if not isinstance(raw, list) or len(raw) < 2:
        raise ConfigError(f"{where}: 'breakpoints' needs at least two entries")
    freqs, psds = [], []
    for i, bp in enumerate(raw):
        w = f"{where}: breakpoints[{i}]"
        if not isinstance(bp, dict) or "f_hz" not in bp:
            raise ConfigError(f"{w}: needs 'f_hz'")
        unknown = set(bp) - {"f_hz", "psd_g2_hz", "slope_db_oct"}
        if unknown:
            raise ConfigError(f"{w}: unknown key(s) {sorted(unknown)}")
        f = float(bp["f_hz"])
        if f <= 0 or (freqs and f <= freqs[-1]):
            raise ConfigError(f"{w}: f_hz must be positive and strictly increasing")
        has_psd, has_slope = "psd_g2_hz" in bp, "slope_db_oct" in bp
        if has_psd == has_slope:
            raise ConfigError(f"{w}: give exactly one of 'psd_g2_hz' or 'slope_db_oct'")
        if has_slope:
            if i == 0:
                raise ConfigError(f"{w}: the first breakpoint needs 'psd_g2_hz'")
            octaves = math.log2(f / freqs[-1])
            psd = psds[-1] * 10 ** (float(bp["slope_db_oct"]) * octaves / 10)
        else:
            psd = float(bp["psd_g2_hz"])
        if psd <= 0:
            raise ConfigError(f"{w}: PSD must be > 0")
        freqs.append(f)
        psds.append(psd)
    return tuple(freqs), tuple(psds)


def load_profile(path: Path | str) -> Profile:
    path = Path(path)
    data = read_toml(path)
    where = str(path)
    unknown = set(data) - {"name", "duration_s", "breakpoints", "tolerance"}
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}")
    f, s = resolve_breakpoints(data.get("breakpoints"), where)
    tol = build_dataclass(Tolerance, data.get("tolerance", {}), f"{where}.tolerance")
    if not 0 < tol.alarm_db <= tol.abort_db or not 0 < tol.rms_alarm_db <= tol.rms_abort_db:
        raise ConfigError(f"{where}: tolerance alarm limits must be > 0 and <= abort limits")
    duration = float(data.get("duration_s", 60.0))
    if duration <= 0:
        raise ConfigError(f"{where}: duration_s must be > 0")
    return Profile(name=str(data.get("name", path.stem)), f_hz=f, psd_g2_hz=s,
                   tolerance=tol, duration_s=duration)
