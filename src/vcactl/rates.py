"""DAQ rate and frame size for a run, chosen from the profile band and the selected hardware.

``daq.fs_io_hz``, ``daq.decimation`` and ``control.frame_size`` are numbers or ``"auto"``.
``resolve_rates`` replaces every "auto" before the backend is created:

- I/O rate: the lowest of ``daq.fs_io_auto_hz`` (default ``limits.fs_allowed_hz``) whose band
  (``daq_band_max_hz``) covers the profile's f_hi.
- Decimation: 1 on devices with an anti-aliasing filter (no FIR rate change needed); otherwise
  the largest factor from 2 to ``MAX_DECIMATION`` that still covers f_hi and gives a whole-number
  control rate.
- Frame size: the smallest power of two (>= ``MIN_FRAME_SIZE``) with a line spacing of at most
  ``control.line_spacing_hz`` and at most f_lo / 2 (the profile starts at >= 2 lines). The
  frame duration, and with it the loop timing (averaging, frames-per-level limits, pretest
  length, abort reaction), then stays about the same at every rate.

Before that, the profile band is checked against the shaker, the sensor and the DAQ, and every
mismatch is reported in one ConfigError.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from .config import ConfigError, DaqConfig, Settings, ShakerConfig, daq_band_max_hz
from .profile import Profile, SineProfile

MAX_DECIMATION = 16     # the FIR length grows with the factor, and the FIR runs at the I/O rate
MIN_FRAME_SIZE = 256


@dataclass(frozen=True)
class RateChoice:
    """DAQ rates and frame size of one run."""
    fs_io_hz: float
    decimation: int
    frame_size: int
    band_max_hz: float          # highest test frequency at these rates
    line_spacing_hz: float
    auto: tuple[str, ...]       # the values chosen by resolve_rates, not set by hand

    @property
    def fs_control_hz(self) -> float:
        return self.fs_io_hz / self.decimation

    def format(self) -> str:
        if not self.auto:
            how = "manual"
        elif len(self.auto) == 3:
            how = "auto"
        else:
            how = "auto: " + ", ".join(self.auto)
        return (f"I/O {_rate(self.fs_io_hz)}, decimation {self.decimation}, control "
                f"{_rate(self.fs_control_hz)}, frame {self.frame_size} "
                f"(df {self.line_spacing_hz:.3g} Hz), band to {_freq(self.band_max_hz)} ({how})")


def _rate(fs: float) -> str:
    return f"{fs / 1000:g} kS/s" if fs >= 1000 else f"{fs:g} S/s"


def _freq(f: float) -> str:
    return f"{f / 1000:.4g} kHz" if f >= 1000 else f"{f:.4g} Hz"


def band_problems(profile: Profile | SineProfile, shaker: ShakerConfig,
                  settings: Settings) -> list[str]:
    """The profile band against the frequency ranges of the shaker and the sensor."""
    f_lo, f_hi = profile.f_lo, profile.f_hi
    sn = settings.sensor
    out = []
    if f_hi > shaker.f_max_hz:
        out.append(f"f_hi {f_hi:g} Hz > f_max {shaker.f_max_hz:g} Hz of the shaker ({shaker.name})")
    if f_hi > sn.f_max_hz:
        out.append(f"f_hi {f_hi:g} Hz > f_max {sn.f_max_hz:g} Hz of the sensor ({sn.name})")
    if f_lo < shaker.f_min_hz:
        out.append(f"f_lo {f_lo:g} Hz < f_min {shaker.f_min_hz:g} Hz of the shaker ({shaker.name})")
    if f_lo < sn.f_min_hz:
        out.append(f"f_lo {f_lo:g} Hz < f_min {sn.f_min_hz:g} Hz of the sensor ({sn.name})")
    return out


def io_rate_candidates(daq: DaqConfig) -> list[float]:
    """I/O rates to try, lowest first: the fixed ``daq.fs_io_hz`` or the auto rates."""
    if not isinstance(daq.fs_io_hz, str):
        return [daq.fs_io_hz]
    return sorted(daq.fs_io_auto_hz or daq.limits.fs_allowed_hz)


def decimation_candidates(daq: DaqConfig, fs_io_hz: float) -> list[int]:
    """Decimation factors to try at ``fs_io_hz``, largest (lowest control rate) first."""
    if not isinstance(daq.decimation, str):
        return [daq.decimation]
    if daq.limits.antialias_filter:
        return [1]
    return [r for r in range(MAX_DECIMATION, 1, -1)
            if abs(fs_io_hz / r - round(fs_io_hz / r)) < 1e-6]


def auto_frame_size(fs_control_hz: float, f_lo: float, line_spacing_hz: float) -> int:
    """Smallest power of two (>= MIN_FRAME_SIZE) with a line spacing of at most
    ``line_spacing_hz`` and at most ``f_lo / 2``."""
    df_max = min(line_spacing_hz, f_lo / 2)
    n = MIN_FRAME_SIZE
    while fs_control_hz / n > df_max:
        n *= 2
    return n


def resolve_rates(profile: Profile | SineProfile, shaker: ShakerConfig,
                  settings: Settings) -> tuple[Settings, RateChoice]:
    """Settings with every "auto" rate and frame size replaced (see the module docstring).

    Raises ConfigError listing every mismatch between the profile band and the shaker, the
    sensor, the DAQ and a fixed frame size.
    """
    d, c = settings.daq, settings.control
    f_lo, f_hi = profile.f_lo, profile.f_hi
    problems = band_problems(profile, shaker, settings)
    choice = None
    widest = None           # (band, fs_io, R) with the highest band, for the error message
    too_coarse = None       # (line spacing, control rate) with a fixed frame size
    for fs_io in io_rate_candidates(d):
        for r in decimation_candidates(d, fs_io):
            band = daq_band_max_hz(d.limits, fs_io, r)
            if widest is None or band > widest[0]:
                widest = (band, fs_io, r)
            if band < f_hi:
                continue
            fs_ctrl = fs_io / r
            if isinstance(c.frame_size, str):
                n = auto_frame_size(fs_ctrl, f_lo, c.line_spacing_hz)
            else:
                n = c.frame_size
                if f_lo < 2 * fs_ctrl / n:
                    too_coarse = too_coarse or (fs_ctrl / n, fs_ctrl)
                    continue
            choice = (fs_io, r, n, band)
            break
        if choice:
            break
    if choice is None:
        if too_coarse:
            df, fs_ctrl = too_coarse
            problems.append(f"f_lo {f_lo:g} Hz needs a line spacing <= {f_lo / 2:.3g} Hz; "
                            f"control.frame_size = {c.frame_size} gives {df:.3g} Hz at the lowest "
                            f"suitable control rate ({_rate(fs_ctrl)}): use a larger frame_size "
                            "or \"auto\"")
        elif widest:
            band, fs_io, r = widest
            fixed = [name for name, v in (("daq.fs_io_hz", d.fs_io_hz),
                                          ("daq.decimation", d.decimation))
                     if not isinstance(v, str)]
            problems.append(f"f_hi {f_hi:g} Hz > {band:g} Hz, the highest test frequency of the "
                            f"{d.model} (at {_rate(fs_io)}, decimation {r}"
                            + (f"; fixed: {', '.join(fixed)}" if fixed else "") + ")")
        else:
            problems.append(f"no decimation from 2 to {MAX_DECIMATION} gives a whole-number "
                            f"control rate at the rates daq.fs_io_auto_hz ({d.model})")
    if problems:
        raise ConfigError(f"the profile ({f_lo:g}-{f_hi:g} Hz) does not fit the selected "
                          "hardware and settings:\n  - " + "\n  - ".join(problems))
    fs_io, r, n, band = choice
    auto = tuple(name for name, v in (("fs_io_hz", d.fs_io_hz), ("decimation", d.decimation),
                                      ("frame_size", c.frame_size)) if isinstance(v, str))
    resolved = dataclasses.replace(settings,
                                   daq=dataclasses.replace(d, fs_io_hz=fs_io, decimation=r),
                                   control=dataclasses.replace(c, frame_size=n))
    return resolved, RateChoice(fs_io, r, n, band, fs_io / r / n, auto)
