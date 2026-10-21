"""Hardware self-test: the AO looped back into a spare AI channel, amplifier disconnected.

Drives band-limited noise at the DAQ's highest automatic rate and reads the loopback channel
together with the control channel. Reports the AO -> AI delay at the start and at the end of the
run (cross-correlation with sub-sample interpolation), the drift between the AO and AI clocks,
the delay against ``limits.filter_delay_samples`` (what the controller compensates), and the
AO -> AI gain at a few frequencies.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass

import numpy as np
from scipy import signal

from .config import Settings
from .rates import io_rate_candidates

BLOCK_S = 0.1                  # I/O block
WINDOW_S = 5.0                 # analysis window at the start and at the end
F_LO_HZ = 20.0                 # noise band: F_LO_HZ to BAND_FRACTION * fs
BAND_FRACTION = 0.4
MAX_DRIFT_PPM = 1.0
MAX_DELAY_ERROR = 2.0          # samples
MIN_CORRELATION = 0.5          # normalized peak below this: no loopback signal
GAIN_FREQS_HZ = (100.0, 1000.0, 5000.0, 10000.0, 20000.0, 40000.0)


@dataclass(frozen=True)
class LoopbackResult:
    fs_io_hz: float
    duration_s: float
    delay_start: float           # AO -> AI delay (samples) in the first window
    delay_end: float             # ... and in the last window
    span_s: float                # time between the two windows
    expected_delay: float        # limits.filter_delay_samples at this rate
    correlation: float           # normalized peak of the first window
    gain_db: dict                # frequency (Hz) -> AO -> AI gain (dB)

    @property
    def drift_ppm(self) -> float:
        return (self.delay_end - self.delay_start) / (self.span_s * self.fs_io_hz) * 1e6

    @property
    def ok(self) -> bool:
        return (self.correlation >= MIN_CORRELATION and abs(self.drift_ppm) <= MAX_DRIFT_PPM
                and abs(self.delay_start - self.expected_delay) <= MAX_DELAY_ERROR)

    def format(self) -> str:
        lines = [f"rate          {self.fs_io_hz:g} S/s, {self.duration_s:.0f} s",
                 f"correlation   {self.correlation:.3f} (>= {MIN_CORRELATION:g}: loopback "
                 "signal found)",
                 f"delay         {self.delay_start:.2f} samples at the start, "
                 f"{self.delay_end:.2f} at the end",
                 f"expected      {self.expected_delay:g} samples (limits.filter_delay_samples); "
                 f"error {self.delay_start - self.expected_delay:+.2f} "
                 f"(limit +/-{MAX_DELAY_ERROR:g})",
                 f"drift         {self.drift_ppm:+.3f} ppm (limit +/-{MAX_DRIFT_PPM:g})",
                 "gain          " + ", ".join(f"{f:g} Hz {g:+.2f} dB"
                                              for f, g in self.gain_db.items())]
        return "\n".join(lines)


def block_samples(settings: Settings) -> int:
    return round(BLOCK_S * settings.daq.fs_io_hz)


def loopback_settings(settings: Settings) -> Settings:
    """``settings`` at the DAQ's highest automatic (or fixed) I/O rate, without decimation."""
    d = settings.daq
    fs = max(io_rate_candidates(d))
    return dataclasses.replace(
        settings, daq=dataclasses.replace(d, fs_io_hz=fs, decimation=1),
        control=dataclasses.replace(settings.control, frame_size=4096))


def _delay(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Delay of ``y`` after ``x`` (samples, parabolic peak interpolation) and the normalized
    correlation at the peak."""
    n = len(x)
    c = signal.correlate(y, x, mode="full", method="fft")
    lags = np.arange(-n + 1, n)
    k = int(np.argmax(np.abs(c)))
    rho = abs(c[k]) / (np.linalg.norm(x) * np.linalg.norm(y) + 1e-30)
    if 0 < k < len(c) - 1:
        a, b, e = np.abs(c[k - 1:k + 2])
        frac = 0.5 * (a - e) / (a - 2 * b + e) if a - 2 * b + e != 0 else 0.0
    else:
        frac = 0.0
    return float(lags[k] + frac), float(rho)


def run_loopback(settings: Settings, backend, *, duration_s: float = 120.0,
                 level_v: float = 0.05, seed: int | None = None) -> LoopbackResult:
    """Drive noise through ``backend`` (created with a loopback channel and the settings from
    ``loopback_settings``) and analyse the loopback channel."""
    d = settings.daq
    fs = d.fs_io_hz
    block = block_samples(settings)
    lead = d.limits.ao_lead_samples(fs)
    queue = max(d.ao_queue_blocks, math.ceil(lead / block) + 1)
    n_blocks = max(math.ceil(duration_s * fs / block), math.ceil(2 * WINDOW_S * fs / block) + 1)
    rng = np.random.default_rng(seed)
    sos = signal.butter(4, [F_LO_HZ, BAND_FRACTION * fs], btype="bandpass", fs=fs, output="sos")
    zi = np.zeros((sos.shape[0], 2))
    scale = None

    def noise() -> np.ndarray:
        nonlocal zi, scale
        x, zi = signal.sosfilt(sos, rng.standard_normal(block), zi=zi)
        if scale is None:
            scale = level_v / np.std(x)
        return np.clip(x * scale, -4 * level_v, 4 * level_v)

    ai_range = min((r for r in d.limits.ai_ranges_v if r >= 6 * level_v),
                   default=max(d.limits.ai_ranges_v))
    ao, ai = [np.zeros(queue * block)], []
    backend.start(ao[0], ai_range)
    try:
        for _ in range(n_blocks):
            x = noise()
            backend.write(x)
            ao.append(x)
            ai.append(np.asarray(backend.read(block))[:, 1])
    finally:
        backend.stop()
    x = np.concatenate(ao)[:n_blocks * block]
    y = np.concatenate(ai)
    w = round(WINDOW_S * fs)
    start = queue * block                     # first driven sample
    d0, rho = _delay(x[start:start + w], y[start:start + w])
    d1, _ = _delay(x[-w:], y[-w:])
    f, gxy = signal.csd(x[start:], y[start:], fs=fs, nperseg=8192)
    _, gxx = signal.welch(x[start:], fs=fs, nperseg=8192)
    h = np.abs(gxy) / np.maximum(gxx, 1e-30)
    gain = {fg: float(20 * np.log10(np.interp(fg, f, h)))
            for fg in GAIN_FREQS_HZ if F_LO_HZ < fg < BAND_FRACTION * fs}
    span_s = (len(y) - w - start) / fs
    return LoopbackResult(fs, len(y) / fs, d0, d1, span_s, d.limits.filter_delay_at(fs), rho,
                          gain)
