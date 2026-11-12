"""Ring-down (free decay) fits and the energy a damper takes per cycle."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import signal


@dataclass
class DecayFit:
    f_hz: float             # mean frequency of the decay
    zeta: float
    zeta_std: float
    t_fit: tuple            # fitted time span (s)
    amplitude: tuple        # envelope at the start and end of the fit

    def format(self) -> str:
        return (f"f {self.f_hz:.4g} Hz, zeta {100 * self.zeta:.4g} % (+/-{100 * self.zeta_std:.2g}), "
                f"fit {self.t_fit[0]:.3g}-{self.t_fit[1]:.3g} s, envelope "
                f"{self.amplitude[0]:.3g} -> {self.amplitude[1]:.3g}")


def band_envelope(y: np.ndarray, fs: float, f_lo: float, f_hi: float):
    """Zero-phase band-pass (4th-order Butterworth, forward and backward) and the Hilbert
    envelope and instantaneous frequency of the result."""
    sos = signal.butter(4, [f_lo, f_hi], btype="bandpass", fs=fs, output="sos")
    yb = signal.sosfiltfilt(sos, y)
    z = signal.hilbert(yb)
    env = np.abs(z)
    inst_f = np.gradient(np.unwrap(np.angle(z))) * fs / (2 * math.pi)
    return yb, env, inst_f


def fit_decay(t: np.ndarray, y: np.ndarray, fs: float, f_lo: float, f_hi: float,
              t_start: float = 0.0, high: float = 0.8, low: float = 0.05) -> DecayFit:
    """Damping of one mode from a free decay: band-pass around the mode, Hilbert envelope, and a
    least-squares line through ln(envelope) between ``high`` and ``low`` of the envelope at
    ``t_start`` (avoiding the filter's edge effects): slope = -zeta * 2 pi f."""
    yb, env, inst_f = band_envelope(y, fs, f_lo, f_hi)
    start = int(np.searchsorted(t, t_start))
    peak = start + int(np.argmax(env[start:start + max(1, int(0.05 * fs))]))
    e0 = env[peak]
    # from the first fall below high*e0 after the peak to the first fall below low*e0 (the
    # filter and Hilbert edge effects at the ends of the record stay out of the fit)
    below_high = np.nonzero(env[peak:] <= high * e0)[0]
    if not len(below_high):
        raise ValueError("the envelope does not decay below the upper fit limit")
    i0 = peak + int(below_high[0])
    below_low = np.nonzero(env[i0:] < low * e0)[0]
    i1 = i0 + int(below_low[0]) if len(below_low) else len(env)
    if i1 - i0 < 10:
        raise ValueError("too little decay between the fit limits")
    k = np.arange(i0, i1)
    tt, ln_e = t[k], np.log(env[k])
    a = np.column_stack([tt, np.ones_like(tt)])
    p, res, *_ = np.linalg.lstsq(a, ln_e, rcond=None)
    f = float(np.median(inst_f[k]))
    w = 2 * math.pi * f
    resid = ln_e - a @ p
    # the envelope samples are correlated over about one cycle: effective points per cycle
    n_eff = max(len(k) * f / fs, 3)
    var = (resid @ resid) / max(len(k) - 2, 1) * len(k) / n_eff
    s_slope = math.sqrt(var / np.sum((tt - tt.mean()) ** 2))
    return DecayFit(f, -p[0] / w, s_slope / w, (float(tt[0]), float(tt[-1])),
                    (float(env[k[0]]), float(env[k[-1]])))


def zeta_vs_amplitude(t, y, fs, f_lo, f_hi, cycles: int = 20, t_start: float = 0.0):
    """Local damping along the decay: line fits of ln(envelope) over ``cycles`` cycles, stepped
    by half that; returns (envelope at the window centre, zeta) arrays (amplitude-dependent
    damping, e.g. friction)."""
    yb, env, inst_f = band_envelope(y, fs, f_lo, f_hi)
    f = float(np.median(inst_f[t >= t_start]))
    n = max(int(round(cycles * fs / f)), 8)
    amps, zetas = [], []
    start = int(np.searchsorted(t, t_start))
    for i in range(start, len(t) - n, n // 2):
        k = slice(i, i + n)
        if np.min(env[k]) <= 0:
            break
        p = np.polyfit(t[k], np.log(env[k]), 1)
        amps.append(float(np.exp(np.polyval(p, t[i + n // 2]))))
        zetas.append(-p[0] / (2 * math.pi * f))
    return np.array(amps), np.array(zetas)


def damper_energy(force_n: np.ndarray, accel_g: np.ndarray, fs: float, f_hz: float,
                  f_hp: float | None = None):
    """Energy a damper at the drive point takes from the beam per cycle during a decay:
    velocity from the drive-point acceleration (integrated, high-passed at ``f_hp``, default
    f/4), power P = -F v (F: force on the beam), integrated per cycle. Returns (cycle start
    times, energy per cycle J, equivalent viscous damper c_eq = E / (pi w X^2) N s/m with X
    the displacement amplitude in that cycle)."""
    g = 9.80665
    f_hp = f_hz / 4 if f_hp is None else f_hp
    sos = signal.butter(2, f_hp, btype="highpass", fs=fs, output="sos")
    v = signal.sosfiltfilt(sos, np.cumsum(accel_g * g) / fs)
    x = signal.sosfiltfilt(sos, np.cumsum(v) / fs)
    p = -force_n * v
    n = int(round(fs / f_hz))
    w = 2 * math.pi * f_hz
    t0, energy, c_eq = [], [], []
    for i in range(0, len(p) - n, n):
        e = float(np.sum(p[i:i + n]) / fs)
        amp = (np.max(x[i:i + n]) - np.min(x[i:i + n])) / 2
        t0.append(i / fs)
        energy.append(e)
        c_eq.append(e / (math.pi * w * amp ** 2) if amp > 0 else math.nan)
    return np.array(t0), np.array(energy), np.array(c_eq)
