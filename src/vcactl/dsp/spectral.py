"""Windowed spectra, averaged auto/cross spectra, H1 FRF and coherence."""

from __future__ import annotations

import numpy as np
from scipy import signal


class Spectrum:
    """Hann-windowed one-sided spectra of frames of length ``n`` at rate ``fs``."""

    def __init__(self, n: int, fs: float):
        self.n, self.fs = n, fs
        self.window = signal.get_window("hann", n)
        self.freqs = np.fft.rfftfreq(n, 1 / fs)
        self.df = fs / n
        # |X|^2 * scale -> one-sided PSD (units^2/Hz)
        self.scale = np.full(len(self.freqs), 2.0 / (fs * np.sum(self.window**2)))
        self.scale[0] /= 2
        if n % 2 == 0:
            self.scale[-1] /= 2

    def fft(self, x: np.ndarray) -> np.ndarray:
        return np.fft.rfft(x * self.window)


def exp_alpha(n_avg: float, count: int) -> float:
    """Weight of the newest estimate: linear average during start-up, then exponential.

    The exponential weight 2/(n_avg + 1) has the same variance reduction as ``n_avg``
    linear averages.
    """
    return max(1.0 / count, 2.0 / (n_avg + 1))


class AutoSpectrumAverager:
    def __init__(self, n_avg: float):
        self.n_avg, self.count, self.value = n_avg, 0, None

    def update(self, psd: np.ndarray) -> None:
        self.count += 1
        if self.value is None:
            self.value = psd.copy()
        else:
            a = exp_alpha(self.n_avg, self.count)
            self.value += a * (psd - self.value)


class CrossSpectrumAverager:
    """Averages Gxx, Gyy, Gxy for the H1 estimate and coherence."""

    def __init__(self, n_avg: float):
        self.n_avg, self.count = n_avg, 0
        self.gxx = self.gyy = self.gxy = None

    def update(self, X: np.ndarray, Y: np.ndarray, scale: np.ndarray) -> None:
        gxx, gyy, gxy = np.abs(X) ** 2 * scale, np.abs(Y) ** 2 * scale, np.conj(X) * Y * scale
        self.count += 1
        if self.gxx is None:
            self.gxx, self.gyy, self.gxy = gxx, gyy, gxy
            return
        a = exp_alpha(self.n_avg, self.count)
        self.gxx += a * (gxx - self.gxx)
        self.gyy += a * (gyy - self.gyy)
        self.gxy += a * (gxy - self.gxy)

    @property
    def h1(self) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(self.gxx > 0, self.gxy / self.gxx, 0.0)

    @property
    def coherence(self) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            den = self.gxx * self.gyy
            return np.where(den > 0, np.abs(self.gxy) ** 2 / den, 0.0)
