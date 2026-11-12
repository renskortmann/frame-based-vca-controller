"""Input-only recording (``vcactl record``): every channel of a channel set at the I/O rate, without
drive, for the background measurements before a test (amplifier off and on).

``InputStream`` (also used by ``vcactl tap``) starts the backend without an AO task, reads blocks
of all channels, checks each channel for AI overload, sensor range and the force limit, and
scales the volts to the channels' units (g, N; V for a loopback channel).
"""

from __future__ import annotations

import dataclasses
import math
import time
from dataclasses import dataclass

import numpy as np

from .config import AiChannel, Settings, auto_range_v
from .rates import io_rate_candidates
from .safety import AbortError, check_channel

BLOCK_S = 0.1                  # read block


def record_settings(settings: Settings) -> Settings:
    """``settings`` at the DAQ's lowest automatic (or fixed) I/O rate, without decimation: the
    recording is saved at the I/O rate (USB-4431: 51.2 kS/s)."""
    d = settings.daq
    fs = min(io_rate_candidates(d))
    return dataclasses.replace(
        settings, daq=dataclasses.replace(d, fs_io_hz=fs, decimation=1),
        control=dataclasses.replace(settings.control, frame_size=4096))


def block_samples(settings: Settings) -> int:
    return round(BLOCK_S * settings.daq.fs_io_hz)


def control_range_v(settings: Settings) -> float:
    """AI range of the control channel without a profile: ``daq.ai_range_v``, or for "auto" the
    smallest range for the sensor's full range."""
    d = settings.daq
    if d.ai_range_v != "auto":
        return float(d.ai_range_v)
    sn = settings.sensor
    return auto_range_v(sn, sn.sensitivity_mv_per_g * d.conditioner_gain, d,
                        settings.safety.ai_overload_fraction)


class InputStream:
    """All channels of ``settings`` from ``backend`` (started input only), block by block."""

    def __init__(self, settings: Settings, backend, block: int):
        self.settings, self.backend, self.block = settings, backend, block
        self.range_v = control_range_v(settings)
        self.channels: tuple[AiChannel, ...] = tuple(
            dataclasses.replace(c, range_v=c.range_v or self.range_v) for c in settings.channels)
        self.scale = np.array([1.0 if c.role == "loopback" else 1.0 / c.v_per_unit
                               for c in self.channels])
        self.fs = settings.daq.fs_io_hz
        self.samples = 0
        # with an aux DAQ the AO plays chirp markers (amplifier off): the clock reference that
        # vcactl.analysis.sync aligns the aux data with
        self.markers = settings.aux is not None
        self._blocks = 0

    @property
    def names(self) -> list[str]:
        return [f"{c.name}_{c.unit}" for c in self.channels]

    def start(self) -> None:
        if not self.markers:
            self.backend.start(None, self.range_v)
            return
        from .daq.aux import MARKER_EVERY_S
        from .daq.ul import chirp_marker
        self._marker = chirp_marker(self.fs)
        self._every = max(1, round(MARKER_EVERY_S * self.fs / self.block))
        queue = self.settings.daq.ao_queue_blocks
        self.backend.start(np.zeros(queue * self.block), self.range_v)

    def _ao_block(self) -> np.ndarray:
        """The next AO block: a chirp marker at the start of every ``_every``-th block."""
        x = np.zeros(self.block)
        if self._blocks % self._every == 1:
            m = self._marker[:self.block]
            x[:len(m)] = m
        self._blocks += 1
        return x

    def read(self, check: bool = True) -> np.ndarray:
        """One block (samples, channels) in the channels' units. ``check``: AbortError on an
        AI overload, a sensor over its range or the force limit; otherwise the AI peaks of the
        block (V) are left in ``last_peaks_v`` for the caller."""
        if self.markers:
            self.backend.write(self._ao_block())
        x = np.asarray(self.backend.read(self.block), dtype=float).reshape(self.block, -1)
        self.last_peaks_v = np.max(np.abs(x), axis=0)
        if check:
            for ch, peak in zip(self.channels, self.last_peaks_v):
                check_channel(ch, float(peak), ch.range_v, self.settings.safety)
        self.samples += self.block
        return x * self.scale

    def overloaded(self, peaks_v: np.ndarray) -> list[str]:
        """Names of the channels whose AI peak (V) is an AI overload or a sensor over range."""
        bad = []
        for ch, peak in zip(self.channels, peaks_v):
            try:
                check_channel(ch, float(peak), ch.range_v, self.settings.safety)
            except AbortError:
                bad.append(ch.name)
        return bad

    def stop(self) -> None:
        self.backend.stop()


@dataclass
class RecordResult:
    completed: bool
    reason: str
    seconds: float
    rms: dict           # column -> rms (channel unit)
    peak: dict          # column -> peak (channel unit)

    def format(self) -> str:
        lines = [f"{'channel':<16}{'rms':>12}{'peak':>12}"]
        for name in self.rms:
            lines.append(f"{name:<16}{self.rms[name]:>12.4g}{self.peak[name]:>12.4g}")
        return "\n".join(lines)


def run_record(settings: Settings, backend, logger, seconds: float,
               stop_requested=lambda: False, on_block=None) -> RecordResult:
    """Record ``seconds`` of every channel to ``raw.f32`` (float32 rows, the channels' units, at
    the I/O rate) and ``status.csv`` (rms and peak per channel and block)."""
    stream = InputStream(settings, backend, block_samples(settings))
    n_blocks = max(1, math.ceil(seconds * stream.fs / stream.block))
    names = stream.names
    sq = np.zeros(len(names))
    peak = np.zeros(len(names))
    reason, completed = "completed", False
    stream.start()
    try:
        for i in range(n_blocks):
            if stop_requested():
                reason = "stopped by operator"
                break
            y = stream.read()
            if logger is not None:
                logger.raw_block(y)
                row = {"t_s": round(stream.samples / stream.fs, 4)}
                row.update({f"{n}_rms": float(np.sqrt(np.mean(y[:, k] ** 2)))
                            for k, n in enumerate(names)})
                logger.status(row)
            sq += np.sum(y ** 2, axis=0)
            peak = np.maximum(peak, np.max(np.abs(y), axis=0))
            if on_block:
                on_block(stream.samples / stream.fs)
        else:
            completed = True
    except AbortError as exc:
        reason = f"ABORT: {exc}"
    finally:
        stream.stop()
    n = max(stream.samples, 1)
    return RecordResult(completed, reason, stream.samples / stream.fs,
                        dict(zip(names, np.sqrt(sq / n).tolist())),
                        dict(zip(names, peak.tolist())))
