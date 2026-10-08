"""Test profiles.

- ``type = "random"`` (default): breakpoint table -> acceleration PSD (g^2/Hz).
- ``type = "sine_sweep"`` / ``"stepped_sine"``: breakpoint table -> sine acceleration peak (g)
  versus frequency, plus the sweep or step schedule.
"""

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


def load_profile(path: Path | str) -> Profile | SineProfile:
    path = Path(path)
    data = read_toml(path)
    where = str(path)
    kind = data.get("type", "random")
    if kind in SINE_TYPES:
        return load_sine_profile(data, path)
    if kind != "random":
        raise ConfigError(f"{where}: type must be one of random, {', '.join(SINE_TYPES)}")
    unknown = set(data) - {"type", "name", "duration_s", "breakpoints", "tolerance"}
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


# ---------------------------------------------------------------------------- sine profiles

SINE_TYPES = ("sine_sweep", "stepped_sine")
_AMPLITUDE_KEYS = ("accel_g", "velocity_m_s", "displacement_mm_pp")


def _to_accel_g(f: float, key: str, value: float) -> float:
    w = 2 * math.pi * f
    if key == "accel_g":
        return value
    if key == "velocity_m_s":
        return value * w / G
    return value / 2 * 1e-3 * w**2 / G           # displacement peak-peak in mm


@dataclass(frozen=True)
class SineProfile:
    """Sine reference: acceleration peak (g) at breakpoints, log-log interpolated.

    ``kind`` is "sine_sweep" (log sweep f_start -> f_end at rate_oct_min, ``sweeps`` passes,
    alternating direction) or "stepped_sine" (each level in ``levels_db`` runs every frequency
    in ``frequencies_hz``: settle, dwell and optionally a ring-down with zero drive).
    """
    name: str
    kind: str
    f_hz: tuple
    accel_g: tuple
    tolerance: Tolerance = field(default_factory=lambda: Tolerance(alarm_db=1.0, abort_db=3.0))
    f_start_hz: float = 0.0
    f_end_hz: float = 0.0
    rate_oct_min: float = 1.0
    sweeps: int = 1
    frequencies_hz: tuple = ()
    levels_db: tuple = (0.0,)
    settle_s: float = 2.0
    max_settle_s: float = 30.0
    dwell_s: float = 3.0
    ringdown_s: float = 0.0

    @property
    def f_lo(self) -> float:
        if self.kind == "stepped_sine":
            return min(self.frequencies_hz)
        return min(self.f_start_hz, self.f_end_hz)

    @property
    def f_hi(self) -> float:
        if self.kind == "stepped_sine":
            return max(self.frequencies_hz)
        return max(self.f_start_hz, self.f_end_hz)

    @property
    def duration_s(self) -> float:
        return self.duration_estimate_s()

    @property
    def max_level_db(self) -> float:
        return max(self.levels_db) if self.kind == "stepped_sine" else 0.0

    def accel_pk_g(self, freqs) -> np.ndarray:
        """Acceleration peak (g) at 0 dB; constant beyond the first/last breakpoint."""
        f = np.clip(np.asarray(freqs, dtype=float), self.f_hz[0], self.f_hz[-1])
        return np.exp(np.interp(np.log(f), np.log(self.f_hz), np.log(self.accel_g)))

    def velocity_pk_m_s(self, freqs) -> np.ndarray:
        freqs = np.asarray(freqs, dtype=float)
        return self.accel_pk_g(freqs) * G / (2 * np.pi * freqs)

    def displacement_pp_mm(self, freqs) -> np.ndarray:
        freqs = np.asarray(freqs, dtype=float)
        return 2 * self.accel_pk_g(freqs) * G / (2 * np.pi * freqs) ** 2 * 1e3

    def sweep_duration_s(self) -> float:
        """Duration of all sweep passes (sine_sweep)."""
        octaves = abs(math.log2(self.f_end_hz / self.f_start_hz))
        return self.sweeps * octaves / self.rate_oct_min * 60

    def duration_estimate_s(self) -> float:
        if self.kind == "sine_sweep":
            return self.sweep_duration_s()
        per_step = self.settle_s + self.dwell_s + self.ringdown_s
        return len(self.levels_db) * len(self.frequencies_hz) * per_step


def _sine_breakpoints(raw, where: str) -> tuple[tuple, tuple]:
    if not isinstance(raw, list) or len(raw) < 1:
        raise ConfigError(f"{where}: 'breakpoints' needs at least one entry")
    freqs, accels = [], []
    for i, bp in enumerate(raw):
        w = f"{where}: breakpoints[{i}]"
        if not isinstance(bp, dict) or "f_hz" not in bp:
            raise ConfigError(f"{w}: needs 'f_hz'")
        unknown = set(bp) - {"f_hz", *_AMPLITUDE_KEYS}
        if unknown:
            raise ConfigError(f"{w}: unknown key(s) {sorted(unknown)}")
        keys = [k for k in _AMPLITUDE_KEYS if k in bp]
        if len(keys) != 1:
            raise ConfigError(f"{w}: give exactly one of {', '.join(_AMPLITUDE_KEYS)}")
        f, value = float(bp["f_hz"]), float(bp[keys[0]])
        if f <= 0 or (freqs and f <= freqs[-1]):
            raise ConfigError(f"{w}: f_hz must be positive and strictly increasing")
        if value <= 0:
            raise ConfigError(f"{w}: {keys[0]} must be > 0")
        freqs.append(f)
        accels.append(_to_accel_g(f, keys[0], value))
    if len(freqs) == 1:                     # a single breakpoint: constant acceleration
        freqs.append(freqs[0] * 2)
        accels.append(accels[0])
    return tuple(freqs), tuple(accels)


def _number(data: dict, key: str, where: str, default: float, positive: bool = True) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: {key} must be a number")
    if positive and value <= 0:
        raise ConfigError(f"{where}: {key} must be > 0")
    return float(value)


def load_sine_profile(data: dict, path: Path) -> SineProfile:
    where = str(path)
    kind = data["type"]
    common = {"type", "name", "breakpoints", "tolerance", "f_start_hz", "f_end_hz"}
    if kind == "sine_sweep":
        allowed = common | {"rate_oct_min", "sweeps"}
    else:
        allowed = common | {"frequencies_hz", "points_per_octave", "levels_db", "direction",
                            "settle_s", "max_settle_s", "dwell_s", "ringdown_s"}
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)} for type {kind}")
    f_bp, a_bp = _sine_breakpoints(data.get("breakpoints"), where)
    tol = build_dataclass(Tolerance, {"alarm_db": 1.0, "abort_db": 3.0,
                                      **data.get("tolerance", {})}, f"{where}.tolerance")
    if not 0 < tol.alarm_db <= tol.abort_db:
        raise ConfigError(f"{where}: tolerance alarm_db must be > 0 and <= abort_db")
    kw = dict(name=str(data.get("name", path.stem)), kind=kind, f_hz=f_bp, accel_g=a_bp,
              tolerance=tol)

    if kind == "sine_sweep":
        kw["f_start_hz"] = _number(data, "f_start_hz", where, f_bp[0])
        kw["f_end_hz"] = _number(data, "f_end_hz", where, f_bp[-1])
        if kw["f_start_hz"] == kw["f_end_hz"]:
            raise ConfigError(f"{where}: f_start_hz and f_end_hz must differ")
        kw["rate_oct_min"] = _number(data, "rate_oct_min", where, 1.0)
        sweeps = data.get("sweeps", 1)
        if isinstance(sweeps, bool) or not isinstance(sweeps, int) or sweeps < 1:
            raise ConfigError(f"{where}: sweeps must be an integer >= 1")
        kw["sweeps"] = sweeps
        return SineProfile(**kw)

    if "frequencies_hz" in data:
        if {"f_start_hz", "f_end_hz", "points_per_octave"} & set(data):
            raise ConfigError(f"{where}: give frequencies_hz or f_start_hz/f_end_hz/"
                              "points_per_octave, not both")
        freqs = data["frequencies_hz"]
        if not isinstance(freqs, list) or not freqs or \
                not all(isinstance(f, (int, float)) and not isinstance(f, bool) and f > 0
                        for f in freqs):
            raise ConfigError(f"{where}: frequencies_hz must be a list of positive numbers")
        freqs = sorted(float(f) for f in freqs)
    else:
        f1 = _number(data, "f_start_hz", where, f_bp[0])
        f2 = _number(data, "f_end_hz", where, f_bp[-1])
        ppo = _number(data, "points_per_octave", where, 3.0)
        lo, hi = min(f1, f2), max(f1, f2)
        n = int(math.floor(math.log2(hi / lo) * ppo + 1e-9)) + 1
        freqs = [lo * 2 ** (i / ppo) for i in range(n)]
        if hi / freqs[-1] > 1 + 1e-9:
            freqs.append(hi)
    direction = data.get("direction", "up")
    if direction not in ("up", "down"):
        raise ConfigError(f"{where}: direction must be up or down")
    if direction == "down":
        freqs = freqs[::-1]
    levels = data.get("levels_db", [0.0])
    if not isinstance(levels, list) or not levels or \
            not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in levels):
        raise ConfigError(f"{where}: levels_db must be a list of numbers")
    kw.update(frequencies_hz=tuple(freqs), levels_db=tuple(float(v) for v in levels),
              settle_s=_number(data, "settle_s", where, 2.0),
              max_settle_s=_number(data, "max_settle_s", where, 30.0),
              dwell_s=_number(data, "dwell_s", where, 3.0),
              ringdown_s=_number(data, "ringdown_s", where, 0.0, positive=False))
    if kw["ringdown_s"] < 0:
        raise ConfigError(f"{where}: ringdown_s must be >= 0")
    if kw["max_settle_s"] < kw["settle_s"]:
        raise ConfigError(f"{where}: max_settle_s must be >= settle_s")
    return SineProfile(**kw)
