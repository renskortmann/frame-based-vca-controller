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
from .base import DaqFault, channel_number, read_channels

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
    """Reads ``read_channels(settings, loopback_channel)`` in one AI task: the control channel and
    the recorded channels of a channels file, each with its own coupling, IEPE current and range,
    and for ``vcactl loopback`` the loopback channel (IEPE off, DC). ``read`` returns (n,) for one
    channel, else (n, channels) in that order."""

    def __init__(self, settings: Settings, block_io: int, loopback_channel: str | None = None):
        import nidaqmx  # noqa: F401  (import errors surface here, not at module import)

        settings.require_rates("NiDaq")
        self.cfg = settings.daq
        self.fs_io = settings.daq.fs_io_hz
        self.block_io = block_io
        self.channels = read_channels(settings, loopback_channel)
        self.aux_cfg = settings.aux          # aux DAQ: started by its trigger, see daq.aux_daq
        self.aux = None
        self.sink = None                     # sink(rows, name): ao.f32 and aux.f32 (aux DAQ)
        self.ao_task = self.ai_task = None
        self._reader = self._writer = None
        self._ai_buf = None
        self._columns = [0]
        self.actual_rate = None
        self.ai_range_v = None
        self.settle_s = 0.0               # time spent waiting for the IEPE sensor in start

    def _range_v(self, ch) -> float:
        """AI range of a channel: its own, or the run's range (control, loopback)."""
        return ch.range_v or self.ai_range_v

    def _add_ai_channel(self, task, ch) -> None:
        """One AI channel with its coupling, IEPE current and range."""
        from nidaqmx.constants import (Coupling, ExcitationSource, ExcitationVoltageOrCurrent,
                                       TerminalConfiguration)

        d = self.cfg
        rng = self._range_v(ch)
        ai = task.ai_channels.add_ai_voltage_chan(
            f"{d.ai_device or d.device}/{ch.physical}",
            terminal_config=TerminalConfiguration[d.ai_terminal_config.upper()],
            min_val=-rng, max_val=rng)
        coupling = ch.coupling.upper()
        if len(d.limits.ai_couplings) > 1 and coupling in d.limits.ai_couplings:
            ai.ai_coupling = Coupling[coupling]
        if d.limits.iepe_currents_ma:
            if ch.iepe_current_ma > 0:
                ai.ai_excit_src = ExcitationSource.INTERNAL
                ai.ai_excit_voltage_or_current = ExcitationVoltageOrCurrent.USE_CURRENT
                ai.ai_excit_val = ch.iepe_current_ma / 1000
            else:
                ai.ai_excit_src = ExcitationSource.NONE

    def _ordered(self, chans) -> list:
        """Channels in ascending physical order (DSA devices need it)."""
        return sorted(chans, key=lambda c: channel_number(c.physical))

    def _settle_iepe(self) -> None:
        """Switch the IEPE excitation on in an AI-only task (same channels, rate and ranges, so
        the AO is not disturbed) and read until every IEPE sensor output has settled."""
        import nidaqmx
        from nidaqmx.constants import AcquisitionType

        chans = self._ordered([c for c in self.channels if c.iepe_current_ma > 0])
        scale = np.array([1.0 / self._range_v(c) for c in chans])[:, None]
        n = max(1, round(self.fs_io * SETTLE_CHUNK_S))

        def chunk(task):
            x = np.asarray(task.read(number_of_samples_per_channel=n, timeout=10.0))
            # fraction of each channel's range, flattened: len / (fs x channels) = chunk time
            return (x.reshape(len(chans), -1) * scale).ravel()

        with nidaqmx.Task("vcactl_settle") as task:
            for ch in chans:
                self._add_ai_channel(task, ch)
            task.timing.cfg_samp_clk_timing(self.fs_io, sample_mode=AcquisitionType.CONTINUOUS,
                                            samps_per_chan=n * 20)
            task.start()
            self.settle_s = wait_until_settled(lambda: chunk(task), self.fs_io * len(chans),
                                               SETTLE_FRACTION)
            task.stop()

    def _create_tasks(self, prefill_len: int | None) -> None:
        """AI task, and the AO task unless ``prefill_len`` is None (input only)."""
        import nidaqmx
        from nidaqmx.constants import (AcquisitionType, RegenerationMode, TaskMode,
                                       TerminalConfiguration)
        from nidaqmx.stream_readers import AnalogMultiChannelReader, AnalogSingleChannelReader
        from nidaqmx.stream_writers import AnalogSingleChannelWriter

        d = self.cfg
        dev = d.device                       # CompactDAQ: the chassis, where the terminals are
        ao_dev = d.ao_device or dev

        self.ai_task = nidaqmx.Task("vcactl_ai")
        order = self._ordered(self.channels)  # DSA devices need ascending channel order
        for ch in order:
            self._add_ai_channel(self.ai_task, ch)
        self._columns = [order.index(ch) for ch in self.channels]
        self.ai_task.timing.cfg_samp_clk_timing(
            self.fs_io, sample_mode=AcquisitionType.CONTINUOUS, samps_per_chan=self.block_io * 20)
        self.ai_task.in_stream.input_buf_size = self.block_io * 20
        self.actual_rate = self.ai_task.timing.samp_clk_rate
        if abs(self.actual_rate - self.fs_io) > 1e-6 * self.fs_io:
            raise DaqFault(f"{d.model} cannot run AI at {self.fs_io} S/s "
                           f"(got {self.actual_rate} S/s); choose another fs_io_hz")
        if self.aux_cfg is not None:
            from .aux_daq import NiAuxDaq
            self.aux = NiAuxDaq(self.aux_cfg, self.sink)
            self.aux.create()
            # the AI task starts on the aux DAQ's exported start trigger (wired PFI to PFI)
            self.ai_task.triggers.start_trigger.cfg_dig_edge_start_trig(
                f"/{dev}/{self.aux_cfg.trigger_in}")

        multi = len(self.channels) > 1
        reader = AnalogMultiChannelReader if multi else AnalogSingleChannelReader
        self._reader = reader(self.ai_task.in_stream)
        if prefill_len is None:                      # input only: no AO task
            return

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

    def start(self, prefill: np.ndarray | None, ai_range_v: float) -> None:
        """``prefill`` None: input only (no AO task; ``write`` raises DaqFault)."""
        self.ai_range_v = ai_range_v
        try:
            if self.cfg.limits.iepe_currents_ma and any(c.iepe_current_ma > 0
                                                        for c in self.channels):
                self._call(self._settle_iepe)
            self._call(self._create_tasks, None if prefill is None else len(prefill))
            if prefill is not None:
                self._call(self._writer.write_many_sample,
                           np.ascontiguousarray(prefill, dtype=float))
                self._log_ao(prefill)
                self._call(self.ao_task.start)   # armed: waits for the AI clock / start trigger
            self._call(self.ai_task.start)       # with an aux DAQ: armed, waits for its trigger
            if self.aux is not None:
                self._call(self.aux.start)
        except Exception:
            self.stop()
            raise

    def write(self, data: np.ndarray) -> None:
        if self.ao_task is None:
            raise DaqFault("write without an AO task (input-only start)")
        self._call(self._writer.write_many_sample, np.ascontiguousarray(data, dtype=float),
                   timeout=10.0)
        self._log_ao(data)

    def _log_ao(self, data) -> None:
        if self.aux is not None and self.sink is not None:
            self.sink(np.asarray(data, dtype=float)[:, None], "ao.f32")

    def read(self, n: int) -> np.ndarray:
        """``n`` AI samples (V): shape (n,) for one channel, else (n, channels)."""
        if self.aux is not None:
            self.aux.check()
        multi = len(self.channels) > 1
        shape = (len(self._columns), n) if multi else (n,)
        if self._ai_buf is None or self._ai_buf.shape != shape:
            self._ai_buf = np.zeros(shape)
        self._call(self._reader.read_many_sample, self._ai_buf,
                   number_of_samples_per_channel=n, timeout=10.0)
        if multi:
            return self._ai_buf[self._columns].T.copy()
        return self._ai_buf.copy()

    def stop(self) -> None:
        if self.aux is not None:
            self.aux.stop()
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
