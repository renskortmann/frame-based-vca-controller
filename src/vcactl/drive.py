"""Random drive synthesis: random-phase frames, sqrt-Hann windowed, 50 % overlap-add.

Each frame gets a new set of random phases, so the output is non-periodic, Gaussian and
stationary; the window pair sin^2 + cos^2 = 1 keeps the variance constant across overlaps.
"""

from __future__ import annotations

import numpy as np


class RandomDriveGenerator:
    def __init__(self, n: int, fs: float, rng: np.random.Generator | None = None):
        if n % 2:
            raise ValueError("frame length must be even")
        self.n, self.fs, self.half = n, fs, n // 2
        self.df = fs / n
        self.rng = rng or np.random.default_rng()
        self.window = np.sin(np.pi * (np.arange(n) + 0.5) / n)
        self.tail = np.zeros(self.half)

    def next_block(self, psd: np.ndarray) -> np.ndarray:
        """Return the next ``n/2`` samples for one-sided PSD ``psd`` (units^2/Hz per rfft bin)."""
        amp = self.n * np.sqrt(np.maximum(psd, 0.0) * self.df / 2)
        amp[0] = 0.0
        amp[-1] = 0.0
        phase = self.rng.uniform(0.0, 2 * np.pi, len(amp))
        frame = np.fft.irfft(amp * np.exp(1j * phase), self.n) * self.window
        out = self.tail + frame[: self.half]
        self.tail = frame[self.half:].copy()
        return out

    def reset(self) -> None:
        self.tail[:] = 0.0


def flat_psd(freqs: np.ndarray, f_lo: float, f_hi: float, rms: float) -> np.ndarray:
    """Flat one-sided PSD over [f_lo, f_hi] with total ``rms``."""
    band = (freqs >= f_lo) & (freqs <= f_hi)
    df = freqs[1] - freqs[0]
    psd = np.zeros(len(freqs))
    psd[band] = rms**2 / (band.sum() * df)
    return psd
