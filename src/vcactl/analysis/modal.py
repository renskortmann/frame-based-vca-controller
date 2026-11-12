"""Single-mode fits of an FRF in a band around a resonance."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass
class ModeFit:
    f_n: float              # natural frequency (Hz)
    zeta: float             # damping ratio
    f_n_std: float          # standard errors from the residuals
    zeta_std: float
    band: tuple
    method: str

    def format(self) -> str:
        return (f"f_n {self.f_n:.4g} Hz (+/-{self.f_n_std:.2g}), zeta {100 * self.zeta:.4g} % "
                f"(+/-{100 * self.zeta_std:.2g}), {self.band[0]:g}-{self.band[1]:g} Hz, "
                f"{self.method}")


def fit_mode(f: np.ndarray, h: np.ndarray, f_lo: float, f_hi: float,
             iterations: int = 5) -> ModeFit:
    """Fit H(s) = (a0 + a1 s + a2 s^2) / (s^2 + b1 s + b0) in [f_lo, f_hi] (one mode plus the
    residual effect of the others, for receptance, mobility or accelerance alike) by linear least
    squares, re-weighted with the previous denominator (Sanathanan-Koerner). The pole gives
    f_n = sqrt(b0) / 2 pi and zeta = b1 / (2 sqrt(b0)); their standard errors come from the
    residuals of the last linear fit."""
    band = (f >= f_lo) & (f <= f_hi) & np.isfinite(h)
    if band.sum() < 6:
        raise ValueError(f"too few lines in {f_lo:g}-{f_hi:g} Hz for a mode fit")
    w0 = 2 * math.pi * 0.5 * (f_lo + f_hi)
    s = 2j * math.pi * f[band] / w0                     # normalized for conditioning
    hb = h[band]
    weight = np.ones(len(s))
    for _ in range(iterations):
        # H (s^2 + b1 s + b0) = a0 + a1 s + a2 s^2  ->  [-H s, -H, 1, s, s^2] p = H s^2
        a = np.column_stack([-hb * s, -hb, np.ones_like(s), s, s ** 2]) / weight[:, None]
        rhs = hb * s ** 2 / weight
        ar = np.vstack([a.real, a.imag])
        br = np.concatenate([rhs.real, rhs.imag])
        p, *_ = np.linalg.lstsq(ar, br, rcond=None)
        b1, b0 = p[0], p[1]
        weight = np.abs(s ** 2 + b1 * s + b0)
        weight = np.maximum(weight, 1e-12 * weight.max())
    resid = br - ar @ p
    dof = max(len(br) - len(p), 1)
    cov = np.linalg.pinv(ar.T @ ar) * (resid @ resid) / dof
    if b0 <= 0:
        raise ValueError(f"no resonance found in {f_lo:g}-{f_hi:g} Hz")
    wn = math.sqrt(b0)
    zeta = b1 / (2 * wn)
    # gradients of (wn, zeta) with respect to (b1, b0)
    g_wn = np.array([0.0, 0.5 / wn])
    g_z = np.array([1 / (2 * wn), -b1 / (4 * wn ** 3)])
    c = cov[:2, :2]
    wn_std = math.sqrt(max(g_wn @ c @ g_wn, 0.0))
    z_std = math.sqrt(max(g_z @ c @ g_z, 0.0))
    return ModeFit(wn * w0 / (2 * math.pi), zeta, wn_std * w0 / (2 * math.pi), z_std,
                   (f_lo, f_hi), "rational fraction (one mode), least squares")


def half_power(f: np.ndarray, h: np.ndarray, f_lo: float, f_hi: float) -> ModeFit:
    """Peak of |H| in the band and the half-power points (|H| = peak / sqrt 2, interpolated):
    zeta = (f2 - f1) / (2 f_n). A cross-check; biased when the lines are coarse."""
    band = np.nonzero((f >= f_lo) & (f <= f_hi))[0]
    mag = np.abs(h)
    k = band[np.argmax(mag[band])]
    half = mag[k] / math.sqrt(2)
    i = k
    while i > band[0] and mag[i] > half:
        i -= 1
    j = k
    while j < band[-1] and mag[j] > half:
        j += 1
    if mag[i] > half or mag[j] > half:
        raise ValueError("the half-power points are outside the band")
    f1 = np.interp(half, [mag[i], mag[i + 1]], [f[i], f[i + 1]])
    f2 = np.interp(half, [mag[j], mag[j - 1]], [f[j], f[j - 1]])
    df = f[1] - f[0]
    return ModeFit(float(f[k]), float((f2 - f1) / (2 * f[k])), df / 2, df / (2 * f[k]),
                   (f_lo, f_hi), "half-power bandwidth")
