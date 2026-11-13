"""The aux DAQ: a second, AI-only device recording alongside the control DAQ (the treatment
load cells on a USB-6211, ``[aux]`` of a channels file).

The two devices cannot share a clock (the USB-4431 imports only a start trigger), so:

- the aux DAQ exports its AI start trigger on ``trigger_out`` (USB-6211 PFI 4), wired to
  ``trigger_in`` of the control DAQ (USB-4431 PFI 0), whose AI task is armed on that edge before
  the aux task starts; both start within the trigger latency;
- the AO drive is recorded on both: written by the controller to ``ao.f32`` and measured on the
  aux channel with role ``drive_ref`` (BNC tee at the amplifier input). Without drive (vcactl
  record, tap) the AO plays low-level chirp markers instead (amplifier off).
  ``vcactl.analysis.sync`` fits the offset and drift from these and resamples the aux data onto
  the control DAQ's AI samples.

The backend writes the aux samples (channel units: N, V for drive_ref) to ``aux.f32`` through a
sink ``sink(rows, name)`` (``RunLogger.raw_block``).
"""

from __future__ import annotations

import threading
import warnings

import numpy as np

from ..config import AuxConfig
from .base import DaqFault, channel_number

MARKER_EVERY_S = 2.0          # input-only runs with an aux DAQ: one AO chirp marker this often
AUX_READ_S = 0.1


def aux_columns(aux: AuxConfig) -> list:
    return [f"{c.name}_{c.unit}" for c in aux.channels]


def aux_scale(aux: AuxConfig) -> np.ndarray:
    return np.array([1.0 / c.v_per_unit for c in aux.channels])


def aux_meta(aux: AuxConfig, fs_main_hz: float, filter_delay_samples: float,
             conv_rate_hz: float | None = None) -> dict:
    """``meta.json["aux"]``: what vcactl.analysis.sync needs (the control DAQ's rate and its
    converter filter delay, AO to AI)."""
    lim = aux.daq.limits
    return {"file": "aux.f32", "dtype": "float32", "fs_hz": aux.rate_hz,
            "fs_main_hz": fs_main_hz, "filter_delay_samples": filter_delay_samples,
            "columns": aux_columns(aux), "units": [c.unit for c in aux.channels],
            "sensitivity_mv_per_unit": [None if c.role == "drive_ref" else c.mv_per_unit
                                        for c in aux.channels],
            "drive_ref": next(i for i, c in enumerate(aux.channels) if c.role == "drive_ref"),
            # multiplexed: channel i is converted i / conv_rate after the sample clock edge
            "convert_rate_hz": conv_rate_hz or lim.ai_aggregate_rate_hz or None,
            "ao_file": "ao.f32", "device": aux.daq.model,
            "trigger": f"{aux.daq.device}/{aux.trigger_out} -> {aux.trigger_in}"}


class NiAuxDaq:
    """The aux DAQ with NI-DAQmx: an AI task that exports its start trigger, read by a thread
    into ``sink``. ``start`` must come after the control DAQ's AI task is armed."""

    def __init__(self, aux: AuxConfig, sink=None):
        import nidaqmx  # noqa: F401

        self.aux, self.sink = aux, sink
        self.task = None
        self.conv_rate = None
        self._thread = None
        self._stop = threading.Event()
        self.fault = None

    def create(self) -> None:
        import nidaqmx
        from nidaqmx.constants import AcquisitionType, TerminalConfiguration

        a, d = self.aux, self.aux.daq
        self.task = nidaqmx.Task("vcactl_aux")
        for c in sorted(a.channels, key=lambda c: channel_number(c.physical)):
            self.task.ai_channels.add_ai_voltage_chan(
                f"{d.ai_device or d.device}/{c.physical}",
                terminal_config=TerminalConfiguration[d.ai_terminal_config.upper()],
                min_val=-c.range_v, max_val=c.range_v)
        self._order = sorted(range(len(a.channels)),
                             key=lambda i: channel_number(a.channels[i].physical))
        self._columns = [self._order.index(i) for i in range(len(a.channels))]
        n = max(1, round(a.rate_hz * AUX_READ_S))
        self.task.timing.cfg_samp_clk_timing(a.rate_hz, sample_mode=AcquisitionType.CONTINUOUS,
                                             samps_per_chan=n * 50)
        actual = self.task.timing.samp_clk_rate
        if abs(actual - a.rate_hz) > 1e-6 * a.rate_hz:
            raise DaqFault(f"{d.model} cannot run AI at {a.rate_hz:g} S/s (got {actual:g})")
        try:
            self.conv_rate = float(self.task.timing.ai_conv_rate)
        except Exception:
            self.conv_rate = None
        self.task.export_signals.start_trig_output_term = f"/{d.device}/{a.trigger_out}"
        self._n = n

    def start(self) -> None:
        """Start the aux task: its start trigger starts the armed control DAQ."""
        from nidaqmx.stream_readers import AnalogMultiChannelReader

        reader = AnalogMultiChannelReader(self.task.in_stream)
        scale = aux_scale(self.aux)[:, None]
        buf = np.zeros((len(self.aux.channels), self._n))

        def run():
            try:
                while not self._stop.is_set():
                    reader.read_many_sample(buf, number_of_samples_per_channel=self._n,
                                            timeout=10.0)
                    if self.sink is not None:
                        self.sink((buf[self._columns] * scale).T.copy(), "aux.f32")
            except Exception as exc:          # reported by the control backend's next read
                if not self._stop.is_set():
                    self.fault = f"aux DAQ: {exc}"

        self.task.start()
        self._thread = threading.Thread(target=run, name="vcactl-aux", daemon=True)
        self._thread.start()

    def check(self) -> None:
        if self.fault:
            raise DaqFault(self.fault)

    def stop(self) -> None:
        self._stop.set()
        if self.task is not None:
            try:
                self.task.stop()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self.task is not None:
            try:
                self.task.close()
            except Exception as exc:
                warnings.warn(f"could not close the aux task: {exc}")
        self.task = None


class SimulatedAux:
    """Simulated aux DAQ for ``SimulatedDaq``: its own clock running ``ppm`` fast, started
    ``offset_s`` before the control DAQ's AI, channel i converted i / ``convert_rate`` after each
    sample clock edge.

    Physical model (control-DAQ samples p): AO sample n is at the amplifier input at
    p = n + ``dac``; AI sample i measures p = i - ``adc`` (``dac + adc`` = the DAQ's converter
    filter delay). drive_ref measures the AO at the amplifier input; a load channel named
    ``*tangential*`` reads ``TANGENTIAL_N_PER_G`` x the table acceleration, any other load
    channel ``NORMAL_N``.
    """

    NORMAL_N = 20.0
    TANGENTIAL_N_PER_G = 5.0

    def __init__(self, aux: AuxConfig, fs_main: float, filter_delay: int, *, ppm: float = 0.0,
                 offset_s: float = 0.0, noise_v: float = 20e-6, seed: int | None = None):
        self.aux, self.fs_main = aux, fs_main
        self.adc = filter_delay // 2
        self.dac = filter_delay - self.adc
        self.ratio = 1 + ppm * 1e-6
        self.offset_s = offset_s
        self.conv = aux.daq.limits.ai_aggregate_rate_hz or 1e9
        self.noise_v = noise_v
        self.rng = np.random.default_rng(seed)
        self.k = 0                                  # next aux sample
        self.sink = None

    def generate(self, ao: np.ndarray, accel_ai: np.ndarray) -> None:
        """Emit every aux sample whose physical time is covered by ``ao`` (AO samples written so
        far) and ``accel_ai`` (table acceleration in g by control-DAQ AI index)."""
        a = self.aux
        n_ch = len(a.channels)
        p_max = len(accel_ai) - 1 - self.adc
        # aux sample k, channel c at aux time tau = k / fs + c / conv; physical p (main samples)
        last = (np.arange(self.k, self.k + 4096) / a.rate_hz + (n_ch - 1) / self.conv)
        rows = []
        while True:
            p_last = (last * self.ratio - self.offset_s) * self.fs_main
            n = int(np.searchsorted(p_last, p_max, side="right"))
            if n == 0:
                break
            k = np.arange(self.k, self.k + n)
            block = np.zeros((n, n_ch))
            for c, ch in enumerate(a.channels):
                tau = k / a.rate_hz + c / self.conv
                p = (tau * self.ratio - self.offset_s) * self.fs_main
                if ch.role == "drive_ref":
                    v = np.interp(p - self.dac, np.arange(len(ao)), ao, left=0.0, right=0.0)
                elif "tangential" in ch.name:
                    acc = np.interp(p + self.adc, np.arange(len(accel_ai)), accel_ai,
                                    left=0.0, right=0.0)
                    v = self.TANGENTIAL_N_PER_G * acc * ch.v_per_unit
                else:
                    v = np.full(n, self.NORMAL_N * ch.v_per_unit)
                v = v + self.rng.normal(0.0, self.noise_v, n)
                block[:, c] = np.clip(v, -ch.range_v * 1.05, ch.range_v * 1.05) / ch.v_per_unit
            rows.append(block)
            self.k += n
            last = (np.arange(self.k, self.k + 4096) / a.rate_hz + (n_ch - 1) / self.conv)
            if n < 4096:
                break
        if rows and self.sink is not None:
            self.sink(np.vstack(rows), "aux.f32")
