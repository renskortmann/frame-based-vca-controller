"""NI-DAQmx backend for the USB-6211 (Windows 11 and native Linux).

AO0 drives the amplifier, AI1 reads the charge amplifier. AO uses the AI sample clock
(``/<dev>/ai/SampleClock``) so both run on one clock: AO is started first and waits for the
AI clock, which starts when the AI task starts. AO regeneration is disabled, so every sample
comes from the controller and an underflow is reported as an error instead of repeating old data.
"""

from __future__ import annotations

import warnings

import numpy as np

from ..config import StationConfig
from .base import DaqFault

TIMEBASE_HZ = 20e6  # USB-6211 sample clock timebase


def list_devices() -> list[tuple[str, str, str]]:
    """Return (name, product type, serial) of the NI-DAQmx devices on this computer."""
    import nidaqmx.system

    out = []
    for dev in nidaqmx.system.System.local().devices:
        try:
            serial = f"{dev.dev_serial_num:X}"
        except Exception:  # simulated devices have no serial number
            serial = "-"
        out.append((dev.name, dev.product_type, serial))
    return out


def force_zero(device: str, ao_channel: str) -> None:
    """Write 0 V to the AO channel with an on-demand task."""
    import nidaqmx

    with nidaqmx.Task() as task:
        task.ao_channels.add_ao_voltage_chan(f"{device}/{ao_channel}", min_val=-10.0, max_val=10.0)
        task.write(0.0, auto_start=True)
        task.stop()


class NiDaq:
    def __init__(self, station: StationConfig, block_io: int):
        import nidaqmx  # noqa: F401  (import errors surface here, not at module import)

        self.cfg = station.daq
        self.fs_io = station.daq.fs_io_hz
        self.block_io = block_io
        self.ao_task = self.ai_task = None
        self._reader = self._writer = None
        self._ai_buf = np.zeros(block_io)
        self.actual_rate = None

    def _create_tasks(self, prefill_len: int) -> None:
        import nidaqmx
        from nidaqmx.constants import AcquisitionType, RegenerationMode, TerminalConfiguration
        from nidaqmx.stream_readers import AnalogSingleChannelReader
        from nidaqmx.stream_writers import AnalogSingleChannelWriter

        d = self.cfg
        dev = d.device
        term = TerminalConfiguration[d.ai_terminal_config.upper()]

        self.ai_task = nidaqmx.Task("vcactl_ai")
        self.ai_task.ai_channels.add_ai_voltage_chan(
            f"{dev}/{d.ai_channel}", terminal_config=term,
            min_val=-d.ai_range_v, max_val=d.ai_range_v)
        self.ai_task.timing.cfg_samp_clk_timing(
            self.fs_io, sample_mode=AcquisitionType.CONTINUOUS, samps_per_chan=self.block_io * 20)
        self.ai_task.in_stream.input_buf_size = self.block_io * 20
        self.actual_rate = self.ai_task.timing.samp_clk_rate
        if abs(self.actual_rate - self.fs_io) > 1e-6 * self.fs_io:
            raise DaqFault(f"device cannot run at {self.fs_io} S/s (got {self.actual_rate} S/s); "
                           f"choose fs_io_hz so that {TIMEBASE_HZ:.0f} / fs_io_hz is an integer")

        self.ao_task = nidaqmx.Task("vcactl_ao")
        self.ao_task.ao_channels.add_ao_voltage_chan(f"{dev}/{d.ao_channel}",
                                                     min_val=-10.0, max_val=10.0)
        self.ao_task.timing.cfg_samp_clk_timing(
            self.fs_io, source=f"/{dev}/ai/SampleClock",
            sample_mode=AcquisitionType.CONTINUOUS, samps_per_chan=prefill_len)
        self.ao_task.out_stream.regen_mode = RegenerationMode.DONT_ALLOW_REGENERATION
        self.ao_task.out_stream.output_buf_size = prefill_len + self.block_io * 4

        self._reader = AnalogSingleChannelReader(self.ai_task.in_stream)
        self._writer = AnalogSingleChannelWriter(self.ao_task.out_stream, auto_start=False)

    def _call(self, fn, *args, **kwargs):
        """Run a DAQmx call; driver errors and warnings (e.g. 200015 glitch) become DaqFault."""
        import nidaqmx.errors

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", nidaqmx.errors.DaqWarning)
                return fn(*args, **kwargs)
        except (nidaqmx.errors.DaqError, nidaqmx.errors.DaqWarning) as exc:
            raise DaqFault(str(exc).strip()) from exc

    def start(self, prefill: np.ndarray) -> None:
        try:
            self._call(self._create_tasks, len(prefill))
            self._call(self._writer.write_many_sample, np.ascontiguousarray(prefill, dtype=float))
            self._call(self.ao_task.start)   # armed: waits for the AI sample clock
            self._call(self.ai_task.start)
        except Exception:
            self.stop()
            raise

    def write(self, data: np.ndarray) -> None:
        self._call(self._writer.write_many_sample, np.ascontiguousarray(data, dtype=float),
                   timeout=10.0)

    def read(self, n: int) -> np.ndarray:
        if len(self._ai_buf) != n:
            self._ai_buf = np.zeros(n)
        self._call(self._reader.read_many_sample, self._ai_buf,
                   number_of_samples_per_channel=n, timeout=10.0)
        return self._ai_buf.copy()

    def stop(self) -> None:
        for task in (self.ao_task, self.ai_task):
            if task is None:
                continue
            try:
                task.stop()
            except Exception:
                pass
            try:
                task.close()
            except Exception:
                pass
        self.ao_task = self.ai_task = None
        try:
            force_zero(self.cfg.device, self.cfg.ao_channel)
        except Exception as exc:  # report, but never mask the original problem
            warnings.warn(f"could not force AO to 0 V: {exc}")
