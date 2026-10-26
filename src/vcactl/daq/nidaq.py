"""NI-DAQmx backend for the NI devices in ``config/daq`` (Windows 11; native Linux for the
PXIe-4468 and the CompactDAQ modules).

One AO channel drives the amplifier, one AI channel reads the accelerometer signal. AI and AO run
at the same rate and start together, so AO sample k and AI sample k belong to the same clock edge
(apart from the converter filter delay of DSA devices, which the controller compensates):

- ``ao_sync = "sample_clock"`` (USB-6211, cDAQ NI 9232 + NI 9263): AO is clocked from
  ``/<dev>/ai/SampleClock``.
- ``ao_sync = "start_trigger"`` (DSA devices such as USB-4431, PXIe-4468): AO uses its own
  sample clock, derived from the same timebase, and starts on ``/<dev>/ai/StartTrigger``.
- ``ao_sync = "shared_timebase"`` (cDAQ NI 9234 + NI 9260): AI and AO are separate delta-sigma
  modules with their own oscillators, so the AO task also takes the AI task's sample clock
  timebase.

On CompactDAQ, ``device`` is the chassis (where the terminals are) and ``ai_device`` /
``ao_device`` are the modules. In all cases AO is started first and waits for AI, which starts
when the AI task starts. AO regeneration is disabled, so every sample comes from the controller
and an underflow is reported as an error instead of repeating old data.
"""

from __future__ import annotations

import warnings

import numpy as np

from ..config import DaqConfig, Settings
from .base import DaqFault, channel_number

# IEPE start-up: switched on from cold, the PCB 352B on the USB-4431 (AC coupling) sits at the
# end of the +/-10 V range for 0.1 s and is within 30 mV only after about 3 s (2026-10-26).
# The excitation stays on between tasks, so the sensor settles in a separate task first.
SETTLE_FRACTION = 0.02          # settled: peak below this fraction of the AI range ...
SETTLE_HOLD_S = 0.5             # ... for this long
SETTLE_TIMEOUT_S = 30.0
SETTLE_CHUNK_S = 0.1


def wait_until_settled(read_chunk, fs: float, limit_v: float, hold_s: float = SETTLE_HOLD_S,
                       timeout_s: float = SETTLE_TIMEOUT_S) -> float:
    """Read chunks (``read_chunk()`` -> samples at ``fs``) until the peak has stayed below
    ``limit_v`` for ``hold_s``; return the time read (s). DaqFault after ``timeout_s``."""
    quiet = elapsed = 0.0
    while quiet < hold_s:
        if elapsed >= timeout_s:
            raise DaqFault(f"the IEPE sensor output did not settle within {timeout_s:g} s "
                           f"(peak above {limit_v:.3g} V): check the sensor and its cable")
        x = np.asarray(read_chunk())
        dt = len(x) / fs
        elapsed += dt
        quiet = quiet + dt if np.max(np.abs(x)) < limit_v else 0.0
    return elapsed


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
        task.ao_channels.add_ao_voltage_chan(f"{d.ao_device or d.device}/{d.ao_channel}",
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
    """``loopback_channel``: a second AI channel (``vcactl loopback``), read in the same task with
    IEPE off and DC coupling; ``read`` then returns two columns (control, loopback)."""

    def __init__(self, settings: Settings, block_io: int, loopback_channel: str | None = None):
        import nidaqmx  # noqa: F401  (import errors surface here, not at module import)

        settings.require_rates("NiDaq")
        self.cfg = settings.daq
        self.fs_io = settings.daq.fs_io_hz
        self.block_io = block_io
        self.loopback_channel = loopback_channel
        self.ao_task = self.ai_task = None
        self._reader = self._writer = None
        self._ai_buf = None
        self._columns = [0]
        self.actual_rate = None
        self.ai_range_v = None
        self.settle_s = 0.0               # time spent waiting for the IEPE sensor in start

    def _add_ai_channel(self, task, name: str, control: bool) -> None:
        """Control channel: the DAQ file's coupling and IEPE; loopback channel: DC, IEPE off."""
        from nidaqmx.constants import (Coupling, ExcitationSource, ExcitationVoltageOrCurrent,
                                       TerminalConfiguration)

        d = self.cfg
        ai = task.ai_channels.add_ai_voltage_chan(
            f"{d.ai_device or d.device}/{name}",
            terminal_config=TerminalConfiguration[d.ai_terminal_config.upper()],
            min_val=-self.ai_range_v, max_val=self.ai_range_v)
        coupling = d.ai_coupling if control else "DC"
        if len(d.limits.ai_couplings) > 1 and coupling.upper() in d.limits.ai_couplings:
            ai.ai_coupling = Coupling[coupling.upper()]
        if d.limits.iepe_currents_ma:
            if control and d.iepe_current_ma > 0:
                ai.ai_excit_src = ExcitationSource.INTERNAL
                ai.ai_excit_voltage_or_current = ExcitationVoltageOrCurrent.USE_CURRENT
                ai.ai_excit_val = d.iepe_current_ma / 1000
            else:
                ai.ai_excit_src = ExcitationSource.NONE

    def _settle_iepe(self) -> None:
        """Switch the IEPE excitation on in an AI-only task (same channel, rate and range, so
        the AO is not disturbed) and read until the sensor output has settled."""
        import nidaqmx
        from nidaqmx.constants import AcquisitionType

        n = max(1, round(self.fs_io * SETTLE_CHUNK_S))
        with nidaqmx.Task("vcactl_settle") as task:
            self._add_ai_channel(task, self.cfg.ai_channel, control=True)
            task.timing.cfg_samp_clk_timing(self.fs_io, sample_mode=AcquisitionType.CONTINUOUS,
                                            samps_per_chan=n * 20)
            task.start()
            self.settle_s = wait_until_settled(
                lambda: task.read(number_of_samples_per_channel=n, timeout=10.0),
                self.fs_io, SETTLE_FRACTION * self.ai_range_v)
            task.stop()

    def _create_tasks(self, prefill_len: int) -> None:
        import nidaqmx
        from nidaqmx.constants import (AcquisitionType, RegenerationMode, TaskMode,
                                       TerminalConfiguration)
        from nidaqmx.stream_readers import AnalogMultiChannelReader, AnalogSingleChannelReader
        from nidaqmx.stream_writers import AnalogSingleChannelWriter

        d = self.cfg
        dev = d.device                       # CompactDAQ: the chassis, where the terminals are
        ao_dev = d.ao_device or dev

        self.ai_task = nidaqmx.Task("vcactl_ai")
        names = [d.ai_channel] + ([self.loopback_channel] if self.loopback_channel else [])
        order = sorted(range(len(names)), key=lambda i: channel_number(names[i]))
        for i in order:                      # DSA devices need ascending channel order
            self._add_ai_channel(self.ai_task, names[i], control=i == 0)
        self._columns = [order.index(i) for i in range(len(names))]
        self.ai_task.timing.cfg_samp_clk_timing(
            self.fs_io, sample_mode=AcquisitionType.CONTINUOUS, samps_per_chan=self.block_io * 20)
        self.ai_task.in_stream.input_buf_size = self.block_io * 20
        self.actual_rate = self.ai_task.timing.samp_clk_rate
        if abs(self.actual_rate - self.fs_io) > 1e-6 * self.fs_io:
            raise DaqFault(f"{d.model} cannot run AI at {self.fs_io} S/s "
                           f"(got {self.actual_rate} S/s); choose another fs_io_hz")

        self.ao_task = nidaqmx.Task("vcactl_ao")
        ao = self.ao_task.ao_channels.add_ao_voltage_chan(
            f"{ao_dev}/{d.ao_channel}", min_val=-d.ao_range_v, max_val=d.ao_range_v)
        if d.ao_terminal_config:
            ao.ao_term_cfg = TerminalConfiguration[d.ao_terminal_config.upper()]
        if d.limits.ao_sync == "sample_clock":
            self.ao_task.timing.cfg_samp_clk_timing(
                self.fs_io, source=f"/{dev}/ai/SampleClock",
                sample_mode=AcquisitionType.CONTINUOUS, samps_per_chan=prefill_len)
        else:
            self.ao_task.timing.cfg_samp_clk_timing(
                self.fs_io, sample_mode=AcquisitionType.CONTINUOUS, samps_per_chan=prefill_len)
            if d.limits.ao_sync == "shared_timebase":
                # separate delta-sigma modules: AO uses the AI module's master timebase, so the
                # two oscillators cannot drift apart (untested on hardware: vcactl loopback)
                self.ai_task.control(TaskMode.TASK_RESERVE)
                self.ao_task.timing.samp_clk_timebase_src = \
                    self.ai_task.timing.samp_clk_timebase_term
                self.ao_task.timing.samp_clk_timebase_rate = \
                    self.ai_task.timing.samp_clk_timebase_rate
            self.ao_task.triggers.start_trigger.cfg_dig_edge_start_trig(f"/{dev}/ai/StartTrigger")
            ao_rate = self.ao_task.timing.samp_clk_rate
            if abs(ao_rate - self.fs_io) > 1e-6 * self.fs_io:
                raise DaqFault(f"{d.model} cannot run AO at {self.fs_io} S/s "
                               f"(got {ao_rate} S/s); choose another fs_io_hz")
        self.ao_task.out_stream.regen_mode = RegenerationMode.DONT_ALLOW_REGENERATION
        self.ao_task.out_stream.output_buf_size = prefill_len + self.block_io * 4

        reader = AnalogMultiChannelReader if self.loopback_channel else AnalogSingleChannelReader
        self._reader = reader(self.ai_task.in_stream)
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

    def start(self, prefill: np.ndarray, ai_range_v: float) -> None:
        self.ai_range_v = ai_range_v
        try:
            if self.cfg.limits.iepe_currents_ma and self.cfg.iepe_current_ma > 0:
                self._call(self._settle_iepe)
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
        """``n`` AI samples (V): shape (n,), or (n, 2) with a loopback channel."""
        shape = (len(self._columns), n) if self.loopback_channel else (n,)
        if self._ai_buf is None or self._ai_buf.shape != shape:
            self._ai_buf = np.zeros(shape)
        self._call(self._reader.read_many_sample, self._ai_buf,
                   number_of_samples_per_channel=n, timeout=10.0)
        if self.loopback_channel:
            return self._ai_buf[self._columns].T.copy()
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
