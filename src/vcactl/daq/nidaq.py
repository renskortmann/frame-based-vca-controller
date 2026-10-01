"""NI-DAQmx backend (Windows 11 and native Linux) for the devices in ``config/daq``.

One AO channel drives the amplifier, one AI channel reads the accelerometer signal. AI and AO run
at the same rate and start together, so AO sample k and AI sample k belong to the same clock edge
(apart from the converter filter delay of DSA devices, which the controller compensates):

- ``ao_sync = "sample_clock"`` (USB-6211): AO is clocked from ``/<dev>/ai/SampleClock``.
- ``ao_sync = "start_trigger"`` (DSA devices such as USB-4431, PXIe-4468): AO uses its own
  sample clock, derived from the same timebase, and starts on ``/<dev>/ai/StartTrigger``.

In both cases AO is started first and waits for AI, which starts when the AI task starts.
AO regeneration is disabled, so every sample comes from the controller and an underflow is
reported as an error instead of repeating old data.
"""

from __future__ import annotations

import warnings

import numpy as np

from ..config import DaqConfig, Settings
from .base import DaqFault


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


def force_zero(d: DaqConfig) -> None:
    """Write 0 V to the AO channel: on demand, or as a short hardware-timed burst of zeros
    on devices whose AO is hardware-timed only (DSA)."""
    import nidaqmx
    from nidaqmx.constants import AcquisitionType

    with nidaqmx.Task() as task:
        task.ao_channels.add_ao_voltage_chan(f"{d.device}/{d.ao_channel}",
                                             min_val=-d.ao_range_v, max_val=d.ao_range_v)
        if d.limits.ao_on_demand:
            task.write(0.0, auto_start=True)
        else:
            # long enough to flush the DAC filter
            n = max(1024, int(d.fs_io_hz * 0.05))
            task.timing.cfg_samp_clk_timing(d.fs_io_hz, sample_mode=AcquisitionType.FINITE,
                                            samps_per_chan=n)
            task.write(np.zeros(n), auto_start=True)
            task.wait_until_done(timeout=5.0)
        task.stop()


class NiDaq:
    def __init__(self, settings: Settings, block_io: int):
        import nidaqmx  # noqa: F401  (import errors surface here, not at module import)

        self.cfg = settings.daq
        self.fs_io = settings.daq.fs_io_hz
        self.block_io = block_io
        self.ao_task = self.ai_task = None
        self._reader = self._writer = None
        self._ai_buf = np.zeros(block_io)
        self.actual_rate = None

    def _create_tasks(self, prefill_len: int) -> None:
        import nidaqmx
        from nidaqmx.constants import (AcquisitionType, Coupling, ExcitationSource,
                                       ExcitationVoltageOrCurrent, RegenerationMode,
                                       TerminalConfiguration)
        from nidaqmx.stream_readers import AnalogSingleChannelReader
        from nidaqmx.stream_writers import AnalogSingleChannelWriter

        d = self.cfg
        dev = d.device
        term = TerminalConfiguration[d.ai_terminal_config.upper()]

        self.ai_task = nidaqmx.Task("vcactl_ai")
        ai = self.ai_task.ai_channels.add_ai_voltage_chan(
            f"{dev}/{d.ai_channel}", terminal_config=term,
            min_val=-d.ai_range_v, max_val=d.ai_range_v)
        if len(d.limits.ai_couplings) > 1:
            ai.ai_coupling = Coupling[d.ai_coupling.upper()]
        if d.limits.iepe_currents_ma:
            if d.iepe_current_ma > 0:
                ai.ai_excit_src = ExcitationSource.INTERNAL
                ai.ai_excit_voltage_or_current = ExcitationVoltageOrCurrent.USE_CURRENT
                ai.ai_excit_val = d.iepe_current_ma / 1000
            else:
                ai.ai_excit_src = ExcitationSource.NONE
        self.ai_task.timing.cfg_samp_clk_timing(
            self.fs_io, sample_mode=AcquisitionType.CONTINUOUS, samps_per_chan=self.block_io * 20)
        self.ai_task.in_stream.input_buf_size = self.block_io * 20
        self.actual_rate = self.ai_task.timing.samp_clk_rate
        if abs(self.actual_rate - self.fs_io) > 1e-6 * self.fs_io:
            raise DaqFault(f"{d.model} cannot run AI at {self.fs_io} S/s "
                           f"(got {self.actual_rate} S/s); choose another fs_io_hz")

        self.ao_task = nidaqmx.Task("vcactl_ao")
        ao = self.ao_task.ao_channels.add_ao_voltage_chan(
            f"{dev}/{d.ao_channel}", min_val=-d.ao_range_v, max_val=d.ao_range_v)
        if d.ao_terminal_config:
            ao.ao_term_cfg = TerminalConfiguration[d.ao_terminal_config.upper()]
        if d.limits.ao_sync == "sample_clock":
            self.ao_task.timing.cfg_samp_clk_timing(
                self.fs_io, source=f"/{dev}/ai/SampleClock",
                sample_mode=AcquisitionType.CONTINUOUS, samps_per_chan=prefill_len)
        else:
            self.ao_task.timing.cfg_samp_clk_timing(
                self.fs_io, sample_mode=AcquisitionType.CONTINUOUS, samps_per_chan=prefill_len)
            self.ao_task.triggers.start_trigger.cfg_dig_edge_start_trig(f"/{dev}/ai/StartTrigger")
            ao_rate = self.ao_task.timing.samp_clk_rate
            if abs(ao_rate - self.fs_io) > 1e-6 * self.fs_io:
                raise DaqFault(f"{d.model} cannot run AO at {self.fs_io} S/s "
                               f"(got {ao_rate} S/s); choose another fs_io_hz")
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
            self._call(self.ao_task.start)   # armed: waits for the AI sample clock / start trigger
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
            force_zero(self.cfg)
        except Exception as exc:  # report, but never mask the original problem
            warnings.warn(f"could not force AO to 0 V: {exc}")
