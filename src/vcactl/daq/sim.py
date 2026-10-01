"""Simulated DAQ + shaker for development and tests without hardware.

Model (acceleration per volt at the amplifier input):

    a/V = (G_amp * Bl / m) * s^2 / (s^2 + 2 z w1 s + w1^2) * w2^2 / (s^2 + (w2/Q) s + w2^2)

w1 = suspension resonance (stiffness, moving mass + payload), w2 = armature resonance.
"""

from __future__ import annotations

import time

import numpy as np
from scipy import signal

from ..config import SensorConfig, ShakerConfig, Settings
from ..profile import G
from .base import DaqFault


def shaker_model_sos(shaker: ShakerConfig, payload_kg: float, fs: float) -> np.ndarray:
    """Discrete-time (bilinear) model: amplifier input volts -> acceleration in g."""
    sim = shaker.sim
    m = shaker.moving_mass_kg + payload_kg
    w1 = np.sqrt(shaker.suspension_stiffness_n_per_mm * 1e3 / m)
    w2 = 2 * np.pi * shaker.armature_resonance_hz
    gain = sim.amp_gain_a_per_v * sim.bl_n_per_a / m / G
    z = [0.0, 0.0]
    p1 = np.roots([1.0, 2 * sim.suspension_zeta * w1, w1**2])
    p2 = np.roots([1.0, w2 / sim.armature_q, w2**2])
    k = gain * w2**2
    zd, pd, kd = signal.bilinear_zpk(z, np.concatenate([p1, p2]), k, fs)
    return signal.zpk2sos(zd, pd, kd)


class SimulatedDaq:
    def __init__(self, settings: Settings, shaker: ShakerConfig, *,
                 sensor: SensorConfig | None = None, realtime: bool = False,
                 seed: int | None = None, disconnect_after_s: float | None = None):
        self.fs_io = settings.daq.fs_io_hz
        self.ai_range = settings.daq.ai_range_v
        self.ao_range = settings.daq.ao_range_v
        self.v_per_g = (sensor or settings.sensor).sensitivity_mv_per_g / 1000
        self.sim = shaker.sim
        self.delay_samples = self.sim.io_delay_samples + round(
            settings.daq.limits.filter_delay_samples)
        self.sos = shaker_model_sos(shaker, settings.safety.payload_kg, self.fs_io)
        self.zi = np.zeros((self.sos.shape[0], 2))
        self.rng = np.random.default_rng(seed)
        self.realtime = realtime
        self.disconnect_after_s = disconnect_after_s
        self.queue = np.zeros(0)
        self.samples_read = 0
        self.running = False
        self.ao_value = 0.0            # last AO sample generated (held after stop)
        self.written: list[np.ndarray] = []
        self.stopped_at_zero = None

    def start(self, prefill: np.ndarray) -> None:
        self.queue = np.concatenate([np.zeros(self.delay_samples), prefill])
        self.running = True
        self.t0 = time.monotonic()
        self.written.append(np.asarray(prefill, dtype=float).copy())

    def write(self, data: np.ndarray) -> None:
        if not self.running:
            raise DaqFault("write on a stopped task")
        if np.any(np.abs(data) > self.ao_range):
            raise DaqFault(f"AO sample outside +/-{self.ao_range:g} V")
        self.queue = np.concatenate([self.queue, data])
        self.written.append(np.asarray(data, dtype=float).copy())

    def read(self, n: int) -> np.ndarray:
        if not self.running:
            raise DaqFault("read on a stopped task")
        if len(self.queue) < n:
            raise DaqFault("AO underflow: generation overtook the written data (-200621)")
        if self.realtime:
            due = self.t0 + (self.samples_read + n) / self.fs_io
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        drive, self.queue = self.queue[:n], self.queue[n:]
        self.ao_value = float(drive[-1])
        accel, self.zi = signal.sosfilt(self.sos, drive, zi=self.zi)
        if self.sim.cubic_coeff:
            accel = accel + self.sim.cubic_coeff * accel**3
        volts = accel * self.v_per_g
        t = self.samples_read / self.fs_io
        if self.disconnect_after_s is not None and t >= self.disconnect_after_s:
            volts = np.zeros(n)
        volts = volts + self.rng.normal(0.0, self.sim.sensor_noise_v, n)
        self.samples_read += n
        return np.clip(volts, -self.ai_range * 1.05, self.ai_range * 1.05)

    def stop(self) -> None:
        if self.running:
            self.running = False
        self.ao_value = 0.0
        self.stopped_at_zero = True
