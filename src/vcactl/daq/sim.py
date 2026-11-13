"""Simulated DAQ + shaker for development and tests without hardware.

Model (acceleration per volt at the amplifier input):

    a/V = (G_amp * Bl / m) * s^2 / (s^2 + 2 z w1 s + w1^2) * w2^2 / (s^2 + (w2/Q) s + w2^2)

w1 = suspension resonance (stiffness, moving mass + payload), w2 = armature resonance.
"""

from __future__ import annotations

import dataclasses
import time

import numpy as np
from scipy import signal

from ..config import SensorConfig, ShakerConfig, Settings
from ..profile import G
from .base import DaqFault, read_channels

# Recorded channels of role "response" (clamp, base accelerometers) see this fraction of the
# simulated table acceleration.
RESPONSE_FRACTION = 0.02


@dataclasses.dataclass(frozen=True)
class TapSim:
    """Tap-test model: half-sine hammer pulses (N) every ``interval_s`` from ``first_s``, and
    response channels (g) that are a sum of modes, acceleration per force
    ``gain * s^2 / (s^2 + 2 zeta w s + w^2)`` per mode (``modes``: (f_hz, zeta, gain g/N));
    response channel i gets the modes divided by (i + 1). ``double_every`` > 0: every that many
    hits a second pulse of half the force follows after ``double_after_s``."""
    interval_s: float = 1.0
    first_s: float = 0.2
    pulse_s: float = 0.0005
    peak_n: float = 50.0
    modes: tuple = ((120.0, 0.02, 0.02), (450.0, 0.01, 0.01), (1300.0, 0.01, 0.005))
    double_every: int = 0
    double_after_s: float = 0.02


def _mode_sos(f_hz: float, zeta: float, gain: float, fs: float) -> np.ndarray:
    w = 2 * np.pi * f_hz
    b, a = signal.bilinear([gain, 0.0, 0.0], [1.0, 2 * zeta * w, w * w], fs)
    return signal.tf2sos(b, a)


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
    """Returns one column per channel of ``read_channels(settings, loopback_channel)``:

    - control: the shaker model's acceleration;
    - response: ``RESPONSE_FRACTION`` of it (clamp or base accelerometer);
    - force: the payload (``safety.payload_kg``) times the acceleration;
    - hammer: noise only;
    - loopback: the AO, delayed by the DAQ's converter filter delay only (``vcactl loopback``).

    Every column gets ``sim.sensor_noise_v`` and is clipped at 1.05 x its AI range.
    """

    def __init__(self, settings: Settings, shaker: ShakerConfig, *,
                 sensor: SensorConfig | None = None, realtime: bool = False,
                 seed: int | None = None, disconnect_after_s: float | None = None,
                 loopback_channel: str | None = None, tap: TapSim | None = None,
                 aux_ppm: float = 0.0, aux_offset_s: float = 0.0):
        settings.require_rates("SimulatedDaq")
        self.fs_io = settings.daq.fs_io_hz
        self.ai_range = None           # set by start()
        self.ao_range = settings.daq.ao_range_v
        if sensor is not None:                    # a sensor other than the one in the settings
            settings = dataclasses.replace(settings, sensor=sensor)
        self.v_per_g = settings.v_per_g
        self.sim = shaker.sim
        self.loopback_delay = round(settings.daq.limits.filter_delay_at(self.fs_io))
        self.delay_samples = self.sim.io_delay_samples + self.loopback_delay
        self.channels = read_channels(settings, loopback_channel)
        self.loopback = any(c.role == "loopback" for c in self.channels)
        self.payload_kg = settings.safety.payload_kg
        # aux DAQ (channels file [aux]): its own clock, fed from the AO and the acceleration
        self.aux_sim = None
        self._sink = None
        if settings.aux is not None:
            from .aux_daq import SimulatedAux
            self.aux_sim = SimulatedAux(settings.aux, self.fs_io, self.loopback_delay,
                                        ppm=aux_ppm, offset_s=aux_offset_s, seed=seed)
            self._ao_all = np.zeros(0)
            self._accel_all = np.zeros(0)
        self.tap = tap
        if tap is not None:
            self.tap_sos = [[_mode_sos(f, z, g / (i + 1), self.fs_io) for f, z, g in tap.modes]
                            for i in range(len(self.channels))]
            self.tap_zi = [[np.zeros((m.shape[0], 2)) for m in ch] for ch in self.tap_sos]
        self.loopback_queue = np.zeros(0)
        self.sos = shaker_model_sos(shaker, settings.payload_total_kg, self.fs_io)
        self.zi = np.zeros((self.sos.shape[0], 2))
        self.rng = np.random.default_rng(seed)
        self.realtime = realtime
        self.disconnect_after_s = disconnect_after_s
        self.queue = np.zeros(0)
        self.samples_read = 0
        self.running = False
        self.ao_value = 0.0            # last AO sample generated (held after stop)
        self.written: list[np.ndarray] = []
        self.ai_only = False
        self.stopped_at_zero = None

    @property
    def sink(self):
        return self._sink

    @sink.setter
    def sink(self, fn) -> None:
        """``fn(rows, name)``: with an aux DAQ, the AO samples go to ao.f32 and the aux samples
        to aux.f32."""
        self._sink = fn
        if self.aux_sim is not None:
            self.aux_sim.sink = fn

    def _log_ao(self, data: np.ndarray) -> None:
        if self.aux_sim is not None:
            self._ao_all = np.concatenate([self._ao_all, data])
            if self._sink is not None:
                self._sink(np.asarray(data, dtype=float)[:, None], "ao.f32")

    def start(self, prefill: np.ndarray | None, ai_range_v: float) -> None:
        """``prefill`` None: input only (the AO stays at 0 V; ``write`` raises DaqFault)."""
        self.ai_range = ai_range_v
        self.ai_only = prefill is None
        if prefill is None:
            prefill = np.zeros(0)
        self.queue = np.concatenate([np.zeros(self.delay_samples), prefill])
        self.loopback_queue = np.concatenate([np.zeros(self.loopback_delay), prefill])
        self.running = True
        self.t0 = time.monotonic()
        self.written.append(np.asarray(prefill, dtype=float).copy())
        self._log_ao(np.asarray(prefill, dtype=float))

    def write(self, data: np.ndarray) -> None:
        if not self.running:
            raise DaqFault("write on a stopped task")
        if self.ai_only:
            raise DaqFault("write without an AO task (input-only start)")
        if np.any(np.abs(data) > self.ao_range):
            raise DaqFault(f"AO sample outside +/-{self.ao_range:g} V")
        self.queue = np.concatenate([self.queue, data])
        if self.loopback:
            self.loopback_queue = np.concatenate([self.loopback_queue, data])
        self.written.append(np.asarray(data, dtype=float).copy())
        self._log_ao(np.asarray(data, dtype=float))

    def read(self, n: int) -> np.ndarray:
        if not self.running:
            raise DaqFault("read on a stopped task")
        if self.ai_only:
            self.queue = np.zeros(n)
            self.loopback_queue = np.zeros(n)
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
        t = self.samples_read / self.fs_io
        if self.disconnect_after_s is not None and t >= self.disconnect_after_s:
            accel = np.zeros(n)
        if self.loopback:
            loop, self.loopback_queue = self.loopback_queue[:n], self.loopback_queue[n:]
        hammer = self._hammer(n) if self.tap is not None else None
        cols = []
        for i, ch in enumerate(self.channels):
            if hammer is not None and ch.role != "loopback":
                v = self._tap_column(i, ch, hammer)
            elif ch.role == "control":
                v = accel * self.v_per_g
            elif ch.role == "loopback":
                v = loop
            elif ch.role == "response":
                v = RESPONSE_FRACTION * accel * ch.v_per_unit
            elif ch.role == "force":
                v = self.payload_kg * accel * G * ch.v_per_unit
            else:
                v = np.zeros(n)
            rng = ch.range_v or self.ai_range
            v = v + self.rng.normal(0.0, self.sim.sensor_noise_v, n)
            cols.append(np.clip(v, -rng * 1.05, rng * 1.05))
        self.samples_read += n
        if self.aux_sim is not None:
            self._accel_all = np.concatenate([self._accel_all, accel])
            self.aux_sim.generate(self._ao_all, self._accel_all)
        return cols[0] if len(cols) == 1 else np.column_stack(cols)

    def _hammer(self, n: int) -> np.ndarray:
        """Hammer force (N) for the next ``n`` samples."""
        tp = self.tap
        t = (self.samples_read + np.arange(n)) / self.fs_io - tp.first_s
        k = np.floor(t / tp.interval_s)
        u = t - k * tp.interval_s
        f = np.where((t >= 0) & (u < tp.pulse_s), tp.peak_n * np.sin(np.pi * u / tp.pulse_s), 0.0)
        if tp.double_every:
            d = u - tp.double_after_s
            second = (t >= 0) & ((k + 1) % tp.double_every == 0) & (d >= 0) & (d < tp.pulse_s)
            f = f + np.where(second, 0.5 * tp.peak_n * np.sin(np.pi * d / tp.pulse_s), 0.0)
        return f

    def _tap_column(self, i: int, ch, force: np.ndarray) -> np.ndarray:
        """Volts of channel ``i`` in the tap model."""
        if ch.role == "hammer":
            return force * ch.v_per_unit
        acc = np.zeros(len(force))
        for j, sos in enumerate(self.tap_sos[i]):
            y, self.tap_zi[i][j] = signal.sosfilt(sos, force, zi=self.tap_zi[i][j])
            acc += y
        return acc * ch.v_per_unit

    def stop(self) -> None:
        if self.running:
            self.running = False
        self.ao_value = 0.0
        self.stopped_at_zero = True
