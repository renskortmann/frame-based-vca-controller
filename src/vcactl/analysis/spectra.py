"""Spectra and FRF estimates from recordings."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import signal


@dataclass
class Frf:
    """H1 = Sxy / Sxx (response per reference) and coherence, with the averaging used."""
    f: np.ndarray
    h1: np.ndarray
    coherence: np.ndarray
    sxx: np.ndarray
    averages: int
    method: str


def psd(x: np.ndarray, fs: float, df: float = 0.25) -> tuple[np.ndarray, np.ndarray]:
    """One-sided PSD (unit^2/Hz), Welch with Hann windows of 1/``df`` s and 50 % overlap."""
    nper = min(len(x), int(round(fs / df)))
    return signal.welch(x, fs=fs, window="hann", nperseg=nper, noverlap=nper // 2,
                        detrend="constant")


def h1_welch(x: np.ndarray, y: np.ndarray, fs: float, df: float = 0.25) -> Frf:
    """H1 and coherence of ``y`` per ``x``: Hann windows of 1/``df`` s, 50 % overlap
    (continuous random: some leakage bias near lightly damped peaks; keep df well below the
    half-power bandwidth)."""
    nper = min(len(x), int(round(fs / df)))
    kw = dict(fs=fs, window="hann", nperseg=nper, noverlap=nper // 2, detrend="constant")
    f, sxy = signal.csd(x, y, **kw)
    _, sxx = signal.welch(x, **kw)
    _, syy = signal.welch(y, **kw)
    n_avg = max(1, (len(x) - nper) // (nper - nper // 2) + 1)
    return _frf(f, sxx, syy, sxy, n_avg, f"Welch, Hann, {nper} samples, 50 % overlap")


def h1_periodic(x: np.ndarray, y: np.ndarray, fs: float, starts, period: int,
                lag: int = 0) -> Frf:
    """H1 and coherence over whole periods of a periodic drive, without a window (no leakage):
    ``x[k:k + period]`` against ``y[k + lag:k + lag + period]`` for each start ``k``."""
    starts = list(starts)
    if not starts:
        raise ValueError("no steady periods to average")
    sxx = syy = 0.0
    sxy = 0.0
    for k in starts:
        X = np.fft.rfft(x[k:k + period])
        Y = np.fft.rfft(y[k + lag:k + lag + period])
        sxx = sxx + np.abs(X) ** 2
        syy = syy + np.abs(Y) ** 2
        sxy = sxy + np.conj(X) * Y
    f = np.fft.rfftfreq(period, 1 / fs)
    scale = 2 / (fs * period * len(starts))
    return _frf(f, sxx * scale, syy * scale, sxy * scale, len(starts),
                f"period-synchronous, {period} samples, no window")


def _frf(f, sxx, syy, sxy, n_avg: int, method: str) -> Frf:
    sxx_safe = np.where(sxx > 0, sxx, np.inf)
    h1 = sxy / sxx_safe
    coh = np.abs(sxy) ** 2 / (sxx_safe * np.where(syy > 0, syy, np.inf))
    return Frf(f, h1, np.clip(coh, 0.0, 1.0), sxx, n_avg, method)


def random_error(coherence: np.ndarray, averages: int) -> np.ndarray:
    """Relative random error of |H1|: sqrt(1 - gamma^2) / (|gamma| sqrt(2 n))."""
    g = np.sqrt(np.clip(coherence, 1e-12, 1.0))
    return np.sqrt(1 - g ** 2) / (g * np.sqrt(2 * averages))
