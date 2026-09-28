"""Stateful FIR decimation (AI) and interpolation (AO) between I/O and control rate.

The USB-6211 has no analog anti-aliasing filter, so AI is oversampled and filtered digitally;
AO is interpolated so the amplifier does not see images of the control-rate signal.
"""

from __future__ import annotations

import numpy as np
from scipy import signal


def design_lowpass(fs_io: float, factor: int, atten_db: float = 80.0) -> np.ndarray:
    """Linear-phase low-pass for rate change by ``factor``.

    Passband to 0.6 * control Nyquist, stopband from the control Nyquist. The length is
    chosen as 2*factor*k + 1 so the combined delay of interpolator + decimator
    (len - 1 samples at fs_io) is an integer number of control-rate samples.
    """
    if factor == 1:
        return np.array([1.0])
    nyq_ctrl = fs_io / factor / 2
    f_pass, f_stop = 0.6 * nyq_ctrl, nyq_ctrl
    numtaps, beta = signal.kaiserord(atten_db, (f_stop - f_pass) / (fs_io / 2))
    step = 2 * factor
    numtaps = int(np.ceil((numtaps - 1) / step)) * step + 1
    return signal.firwin(numtaps, (f_pass + f_stop) / 2, window=("kaiser", beta), fs=fs_io)


def total_delay_control_samples(taps: np.ndarray, factor: int) -> int:
    """Group delay of interpolation + decimation, in control-rate samples."""
    return (len(taps) - 1) // factor


class Decimator:
    def __init__(self, taps: np.ndarray, factor: int):
        self.taps, self.factor = taps, factor
        self.zi = np.zeros(len(taps) - 1)

    def process(self, x: np.ndarray) -> np.ndarray:
        if len(x) % self.factor:
            raise ValueError("block length must be a multiple of the decimation factor")
        if self.factor == 1:
            return np.asarray(x, dtype=float).copy()
        y, self.zi = signal.lfilter(self.taps, 1.0, x, zi=self.zi)
        return y[:: self.factor]


class Interpolator:
    def __init__(self, taps: np.ndarray, factor: int):
        self.taps, self.factor = taps * factor, factor
        self.zi = np.zeros(len(taps) - 1)

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.factor == 1:
            return np.asarray(x, dtype=float).copy()
        up = np.zeros(len(x) * self.factor)
        up[:: self.factor] = x
        y, self.zi = signal.lfilter(self.taps, 1.0, up, zi=self.zi)
        return y
