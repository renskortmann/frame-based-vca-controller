"""Random drive synthesis: random-phase frames, sqrt-Hann windowed, 50 % overlap-add; and
periodic random (``PeriodicDriveGenerator``) for leakage-free analysis of recordings.

Each frame gets a new set of random phases, so the output is non-periodic, Gaussian and
stationary; the window pair sin^2 + cos^2 = 1 keeps the variance constant across overlaps.
"""

from __future__ import annotations

import math

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


class PeriodicDriveGenerator:
    """Periodic random drive: one period of ``period_n`` samples (a power of two, at least one
    frame) with random phases drawn once, repeated exactly. The amplitudes follow the PSD passed
    to ``next_block`` at the start of each period (interpolated from the frame grid onto the
    period's finer grid), so a new PSD takes effect only at a period boundary and every period
    without a change is an exact repeat (leakage-free analysis with records of one period)."""

    def __init__(self, n: int, fs: float, period_n: int, rng: np.random.Generator | None = None):
        if period_n < n or period_n % (n // 2):
            raise ValueError("period must be a multiple of half a frame and at least one frame")
        self.n, self.fs, self.half, self.period_n = n, fs, n // 2, period_n
        self.coarse = np.fft.rfftfreq(n, 1 / fs)
        self.fine = np.fft.rfftfreq(period_n, 1 / fs)
        rng = rng or np.random.default_rng()
        self.phase = np.exp(1j * rng.uniform(0.0, 2 * np.pi, len(self.fine)))
        self.pos = 0                     # next sample within the period
        self.period = np.zeros(period_n)
        self.periods = 0                 # periods started
        self.changed = False             # the period just started differs from the one before
        self.change_db = math.inf        # ... by at most this much on any line (dB)
        self._psd = None

    @property
    def at_boundary(self) -> bool:
        return self.pos == 0

    def _synthesize(self, psd: np.ndarray) -> None:
        fine = np.interp(self.fine, self.coarse, np.maximum(psd, 0.0), left=0.0, right=0.0)
        df = self.fs / self.period_n
        amp = self.period_n * np.sqrt(fine * df / 2)
        amp[0] = amp[-1] = 0.0
        self.period = np.fft.irfft(amp * self.phase, self.period_n)

    def next_block(self, psd: np.ndarray) -> np.ndarray:
        """The next ``n/2`` samples; ``psd`` (frame grid, units^2/Hz) is used only at a period
        boundary."""
        if self.pos == 0:
            self.changed = self._psd is None or not np.array_equal(psd, self._psd)
            self.change_db = math.inf if self._psd is None else _max_change_db(self._psd, psd)
            if self.changed:
                self._synthesize(psd)
                self._psd = np.array(psd, dtype=float)
            self.periods += 1
        out = self.period[self.pos:self.pos + self.half].copy()
        self.pos = (self.pos + self.half) % self.period_n
        return out

    def reset(self) -> None:
        self.pos = 0
        self._psd = None


def _max_change_db(old: np.ndarray, new: np.ndarray) -> float:
    """Largest PSD change (dB) on the lines where either PSD is > 0; inf if a line switched on
    or off."""
    on = (old > 0) | (new > 0)
    if not np.any(on):
        return 0.0
    if np.any((old[on] > 0) != (new[on] > 0)):
        return math.inf
    return float(np.max(np.abs(10 * np.log10(new[on] / old[on]))))


def period_samples(period_s: float, fs: float, n: int) -> int:
    """Samples per period: the smallest power of two >= ``period_s * fs``, at least ``n``."""
    return max(n, 1 << max(0, math.ceil(math.log2(max(period_s * fs, 1.0)))))


def flat_psd(freqs: np.ndarray, f_lo: float, f_hi: float, rms: float) -> np.ndarray:
    """Flat one-sided PSD over [f_lo, f_hi] with total ``rms``."""
    band = (freqs >= f_lo) & (freqs <= f_hi)
    df = freqs[1] - freqs[0]
    psd = np.zeros(len(freqs))
    psd[band] = rms**2 / (band.sum() * df)
    return psd
