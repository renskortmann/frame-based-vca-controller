"""Notching a random profile so that a structure's response stays within a limit (the tip of
the cantilever within the Q2 lidar's velocity range).

The profile is the drive-point acceleration PSD S_a(f) (g^2/Hz) that the controller holds. With
the measured transmissibility T(f) from the drive point to the tip (tip velocity per drive-point
acceleration, from a low-level run: lidar tip velocity / control accelerometer), the tip
velocity PSD is |T|^2 S_a and its rms the square root of the integral. While crest factor x rms
exceeds the limit, the line contributing most is notched: the profile is lowered by
``step_db`` over a band of at least ``min_lines`` control lines around it (the controller cannot
follow anything narrower), notches that touch are merged, and the result is written as new
breakpoints (log-log interpolation between them, as in the profile).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Notch:
    f_lo: float
    f_hi: float
    depth_db: float


@dataclass
class NotchResult:
    breakpoints: list           # [(f_hz, psd_g2_hz)], strictly increasing f
    notches: list               # Notch
    rms_before: float           # tip rms (FRF unit x g, e.g. mm/s)
    rms_after: float
    warnings: list


def _profile_psd(f_bp, s_bp, f):
    out = np.zeros_like(f)
    inside = (f >= f_bp[0]) & (f <= f_bp[-1])
    out[inside] = np.exp(np.interp(np.log(f[inside]), np.log(f_bp), np.log(s_bp)))
    return out


def notch_profile(f_bp, s_bp, f_frf, t_mag, limit_pk: float, crest: float = 4.0,
                  line_spacing_hz: float = 6.25, min_lines: int = 3, step_db: float = 1.0,
                  max_depth_db: float = 40.0) -> NotchResult:
    """Notch the profile (breakpoints ``f_bp``, ``s_bp``) so that crest x rms of
    |``t_mag``|^2 S_a over the FRF lines ``f_frf`` stays below ``limit_pk``."""
    f_bp = np.asarray(f_bp, dtype=float)
    s_bp = np.asarray(s_bp, dtype=float)
    f = np.asarray(f_frf, dtype=float)
    t2 = np.asarray(t_mag, dtype=float) ** 2
    df = np.gradient(f)
    base = _profile_psd(f_bp, s_bp, f)
    gain_db = np.zeros_like(f)                 # notch attenuation per FRF line
    width = min_lines * line_spacing_hz

    def rms(g_db):
        return float(np.sqrt(np.sum(t2 * base * 10 ** (g_db / 10) * df)))

    before = rms(gain_db)
    limit_rms = limit_pk / crest
    warnings = []
    centres: list = []
    while rms(gain_db) > limit_rms:
        contrib = t2 * base * 10 ** (gain_db / 10)
        k = int(np.argmax(contrib))
        f0 = f[k]
        band = (f >= f0 - width / 2) & (f <= f0 + width / 2) & (base > 0)
        if gain_db[k] <= -max_depth_db:
            warnings.append(f"the notch at {f0:.1f} Hz reached {max_depth_db:g} dB; the limit "
                            "is not met")
            break
        gain_db[band] -= step_db
        centres.append(f0)
    after = rms(gain_db)

    # notches: runs of attenuated lines, widened to the shoulders
    notches = []
    on = gain_db < 0
    i = 0
    while i < len(f):
        if on[i]:
            j = i
            while j + 1 < len(f) and on[j + 1]:
                j += 1
            notches.append(Notch(float(f[i]), float(f[j]), float(-np.min(gain_db[i:j + 1]))))
            i = j + 1
        else:
            i += 1
    for a, b in zip(notches, notches[1:]):
        if b.f_lo - a.f_hi < line_spacing_hz:
            warnings.append(f"notches at {a.f_hi:.1f} and {b.f_lo:.1f} Hz are less than a line "
                            "apart")

    # breakpoints: the profile's, plus each notch as a trapezoid (base level at its edges, the
    # notch depth over its middle), all on the profile's log-log shape
    pts = {float(x): float(y) for x, y in zip(f_bp, s_bp)}
    for n in notches:
        lo = max(n.f_lo - line_spacing_hz / 2, f_bp[0])
        hi = min(n.f_hi + line_spacing_hz / 2, f_bp[-1])
        sel = (f >= n.f_lo) & (f <= n.f_hi)
        for x in list(pts):
            if lo < x < hi:
                del pts[x]
        pts[float(lo)] = float(_profile_psd(f_bp, s_bp, np.array([lo]))[0])
        pts[float(hi)] = float(_profile_psd(f_bp, s_bp, np.array([hi]))[0])
        inner = f[sel]
        # the floor over the notch: the deepest attenuation, between the first and last line
        fl, fh = float(inner[0]), float(inner[-1])
        if fh - fl < 1e-9:
            fl, fh = fl - 0.25 * line_spacing_hz, fh + 0.25 * line_spacing_hz
        for x in (fl, fh):
            if lo < x < hi:
                pts[float(x)] = float(_profile_psd(f_bp, s_bp, np.array([x]))[0]
                                      * 10 ** (-n.depth_db / 10))
    bps = sorted(pts.items())
    return NotchResult(bps, notches, before, after, warnings)


def predicted_rms(breakpoints, f_frf, t_mag) -> float:
    """Tip rms for a profile's breakpoints and the transmissibility."""
    f_bp, s_bp = zip(*breakpoints)
    f = np.asarray(f_frf, dtype=float)
    s = _profile_psd(np.array(f_bp), np.array(s_bp), f)
    return float(np.sqrt(np.sum(np.asarray(t_mag) ** 2 * s * np.gradient(f))))
