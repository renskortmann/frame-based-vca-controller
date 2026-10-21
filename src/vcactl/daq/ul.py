"""UL for Linux backend (``uldaq``) for the MCC/Data Translation DT9837A.

The AI scan (control channel plus the AO readback channel, a 16-bit measurement of the AO output
taken with every A/D sample) and the AO scan run continuously on circular buffers at the same
rate. Their clocks come from one 48 MHz reference with identical PLL settings up to 52.734 kS/s,
so they do not drift, but UL starts them with separate software commands, so their offset is not
known. Every start therefore begins the AO with a low-level chirp marker followed by zeros; the
backend finds the marker in the readback channel and from then on returns AI sample n together
with AO sample n at the AO output (the readback includes the DAC delay and the output filter).

UL keeps the AO FIFO and 32 USB transfers full, so the output lags the data handed over by
``limits.ao_lead_samples`` (about 0.5 s at 51.2 kS/s). ``rates.resolve_rates`` sizes the AO queue
for it; ``start`` checks the measured lag against the queue.

A circular AO buffer would replay old drive if the controller stopped writing. A watchdog thread
therefore zeroes every AO sample once UL has taken it, and stops the AO (0 V) if no data arrive
for ``STALL_S``; the next ``read`` or ``write`` then raises DaqFault.
"""

from __future__ import annotations

import threading
import time
import warnings

import numpy as np
from scipy import signal

from ..config import Settings
from .base import DaqFault, channel_number

MARKER_S = 0.04                 # chirp marker at the start of every run
MARKER_V = 0.01                 # marker peak (V)
MARKER_F_HZ = (500.0, 4000.0)   # inside the AO's 10 kHz output filter
LEAD_IN_S = 0.05                # zeros before the marker
PAD_S = 0.5                     # zeros between the marker and the controller's first sample
FIND_TIMEOUT_S = 2.0            # time allowed to find the marker in the readback channel
MARKER_MIN_CORR = 0.7           # normalized correlation that counts as found
AI_BUFFER_S = 4.0
STALL_S = 2.0                   # watchdog: stop the AO if no data arrive for this long
WATCH_S = 0.01                  # watchdog interval
POLL_S = 0.002
READ_TIMEOUT_S = 10.0


def list_devices() -> list[tuple[str, str, str]]:
    """Return (unique ID, product name, unique ID) of the UL USB devices on this computer."""
    import uldaq

    return [(d.unique_id, d.product_name, d.unique_id)
            for d in uldaq.get_daq_device_inventory(uldaq.InterfaceType.USB)]


def chirp_marker(fs: float) -> np.ndarray:
    """Hann-weighted linear chirp, ``MARKER_V`` peak, ``MARKER_S`` long."""
    n = max(256, round(MARKER_S * fs))
    t = np.arange(n) / fs
    f0, f1 = MARKER_F_HZ
    phase = 2 * np.pi * (f0 * t + (f1 - f0) / (2 * t[-1]) * t**2)
    return MARKER_V * np.hanning(n) * np.sin(phase)


def find_marker(x: np.ndarray, marker: np.ndarray) -> int | None:
    """Index where ``marker`` starts in ``x`` (normalized cross-correlation of at least
    ``MARKER_MIN_CORR``), or None."""
    m = len(marker)
    if len(x) < m:
        return None
    x = x - np.median(x)                        # the readback has an offset of up to 15 mV
    c = signal.correlate(x, marker, mode="valid")
    cs = np.concatenate([[0.0], np.cumsum(x**2)])
    energy = np.sqrt(np.maximum(cs[m:] - cs[:-m], 0.0))        # energy of each window
    norm = energy * np.linalg.norm(marker)
    rho = np.divide(c, norm, out=np.zeros_like(c), where=norm > 0)
    j = int(np.argmax(rho))
    return j if rho[j] >= MARKER_MIN_CORR else None


class UlDaq:
    """``loopback_channel``: a second AI channel (``vcactl loopback``), with IEPE off and DC
    coupling; ``read`` then returns two columns (control, loopback)."""

    def __init__(self, settings: Settings, block_io: int, loopback_channel: str | None = None):
        import uldaq  # noqa: F401  (import errors surface here, not at module import)

        settings.require_rates("UlDaq")
        self.cfg = settings.daq
        self.fs_io = settings.daq.fs_io_hz
        self.block_io = block_io
        self.lead = settings.daq.limits.ao_lead_samples(self.fs_io)
        self.loopback_channel = loopback_channel
        self.device = self.ai = self.ao = self.daqi = None
        self.ai_range_v = None
        self.measured_lag = None
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._thread = None
        self._fault = None

    # ------------------------------------------------------------------ helpers
    def _call(self, fn, *args, **kwargs):
        """Run a UL call; UL errors become DaqFault."""
        import uldaq

        try:
            return fn(*args, **kwargs)
        except uldaq.ULException as exc:
            raise DaqFault(f"{self.cfg.model}: {exc}") from exc

    def _open(self) -> None:
        import uldaq

        d = self.cfg
        found = [x for x in uldaq.get_daq_device_inventory(uldaq.InterfaceType.USB)
                 if x.product_name == d.limits.product and (not d.device or x.unique_id == d.device)]
        if not found:
            which = f" with unique ID {d.device}" if d.device else ""
            raise DaqFault(f"no {d.limits.product}{which} found (see vcactl list-devices)")
        if len(found) > 1:
            ids = ", ".join(x.unique_id for x in found)
            raise DaqFault(f"several {d.limits.product} found ({ids}): set daq.device")
        self.device = uldaq.DaqDevice(found[0])
        self.device.connect()
        self.ai = self.device.get_ai_device()
        self.ao = self.device.get_ao_device()
        self.daqi = self.device.get_daqi_device()

    def _ai_count(self) -> int:
        import uldaq

        status, xfer = self.daqi.get_scan_status()
        if status != uldaq.ScanStatus.RUNNING:
            raise DaqFault(f"{self.cfg.model}: the AI scan stopped (overrun or USB error)")
        return xfer.current_scan_count

    def _ao_count(self) -> int:
        """AO samples UL has taken from the buffer (they lead the output by the AO lag)."""
        import uldaq

        status, xfer = self.ao.get_scan_status()
        if status != uldaq.ScanStatus.RUNNING:
            raise DaqFault(f"{self.cfg.model}: the AO scan stopped (USB error)")
        return xfer.current_scan_count

    def _ai_slice(self, start: int, stop: int, column: int) -> np.ndarray:
        idx = (np.arange(start, stop) % self.ai_len) * self.n_in + column
        return self._ai_np[idx]

    def _halt_ao(self) -> None:
        import uldaq

        try:
            self.ao.scan_stop()
        except Exception:
            pass
        ch = channel_number(self.cfg.ao_channel)
        self.ao.a_out(ch, uldaq.Range.BIP10VOLTS, uldaq.AOutFlag.DEFAULT, 0.0)

    def _check_fault(self) -> None:
        if self._fault:
            raise DaqFault(self._fault)

    def _watch(self) -> None:
        """Zero the AO samples UL has taken; stop the AO when no new data arrive."""
        while not self._halt.wait(WATCH_S):
            try:
                with self._lock:
                    sent = self._ao_count()
                    if sent > self._zeroed:
                        k0 = max(self._zeroed, sent - self.ao_len)
                        self._ao_np[np.arange(k0, sent) % self.ao_len] = 0.0
                        self._zeroed = sent
                    stalled = time.monotonic() - self._last_write > STALL_S
                if stalled:
                    self._fault = f"AO stopped: no new drive data for {STALL_S:g} s"
                    self._halt_ao()
                    return
            except Exception as exc:  # never leave the AO running unwatched
                self._fault = f"AO watchdog: {exc}"
                try:
                    self._halt_ao()
                except Exception:
                    pass
                return

    def _align(self, marker: np.ndarray, marker_at: int) -> None:
        """Find the marker in the readback channel; set the AI index of controller sample 0."""
        n_max = round(FIND_TIMEOUT_S * self.fs_io)
        t_end = time.monotonic() + FIND_TIMEOUT_S + 1.0
        while True:
            have = min(self._ai_count(), n_max)
            j = find_marker(self._ai_slice(0, have, self.n_in - 1), marker)
            if j is not None and j + len(marker) + 16 <= have:
                break
            if have >= n_max or time.monotonic() > t_end:
                raise DaqFault(f"{self.cfg.model}: no AO marker in the readback channel within "
                               f"{FIND_TIMEOUT_S:g} s (is the AO running?)")
            time.sleep(0.02)
        self.ai_read = j - marker_at + self.offset      # AI index of the controller's sample 0
        # AO lag: samples UL has taken minus the AO index now at the output
        with self._lock:
            self.measured_lag = self._ao_count() - (self._ai_count() + marker_at - j)

    # ------------------------------------------------------------------ backend interface
    def start(self, prefill: np.ndarray, ai_range_v: float) -> None:
        import uldaq

        d = self.cfg
        fs = self.fs_io
        self.ai_range_v = ai_range_v
        try:
            self._call(self._open)
            ai_range = {1.0: uldaq.Range.BIP1VOLTS, 10.0: uldaq.Range.BIP10VOLTS}[float(ai_range_v)]
            names = [d.ai_channel] + ([self.loopback_channel] if self.loopback_channel else [])
            order = sorted(range(len(names)), key=lambda i: channel_number(names[i]))
            config = self.ai.get_config()
            descriptors = []
            for i in order:                       # UL needs ascending channel order
                ch = channel_number(names[i])
                control = i == 0
                iepe = control and d.iepe_current_ma > 0
                ac = control and d.ai_coupling.upper() == "AC"
                self._call(config.set_chan_iepe_mode, ch,
                           uldaq.IepeMode.ENABLED if iepe else uldaq.IepeMode.DISABLED)
                self._call(config.set_chan_coupling_mode, ch,
                           uldaq.CouplingMode.AC if ac else uldaq.CouplingMode.DC)
                descriptors.append(uldaq.DaqInChanDescriptor(ch, uldaq.DaqInChanType.ANALOG_SE,
                                                             ai_range))
            descriptors.append(uldaq.DaqInChanDescriptor(0, uldaq.DaqInChanType.DAC,
                                                         uldaq.Range.BIP10VOLTS))
            self.n_in = len(descriptors)
            self.columns = [order.index(i) for i in range(len(names))]

            # AO buffer: zeros, marker, zeros, the controller's prefill, room for the queue
            marker = chirp_marker(fs)
            marker_at = round(LEAD_IN_S * fs)
            self.offset = marker_at + len(marker) + round(PAD_S * fs)
            self.ao_len = self.offset + len(prefill) + 4 * self.block_io + self.lead
            self._ao_buf = uldaq.create_float_buffer(1, self.ao_len)
            self._ao_np = np.ctypeslib.as_array(self._ao_buf)
            self._ao_np[:] = 0.0
            self._ao_np[marker_at:marker_at + len(marker)] = marker
            self._ao_np[self.offset:self.offset + len(prefill)] = prefill
            self.written = self.offset + len(prefill)
            self._zeroed = 0
            self.ai_len = round(AI_BUFFER_S * fs)
            self._ai_buf = uldaq.create_float_buffer(self.n_in, self.ai_len)
            self._ai_np = np.ctypeslib.as_array(self._ai_buf)

            # AI first: the marker then always arrives after the AI scan has started
            ai_rate = self._call(self.daqi.daq_in_scan, descriptors, self.ai_len, fs,
                                 uldaq.ScanOption.CONTINUOUS, uldaq.DaqInScanFlag.DEFAULT,
                                 self._ai_buf)
            ch = channel_number(d.ao_channel)
            ao_rate = self._call(self.ao.a_out_scan, ch, ch, uldaq.Range.BIP10VOLTS, self.ao_len,
                                 fs, uldaq.ScanOption.CONTINUOUS, uldaq.AOutScanFlag.DEFAULT,
                                 self._ao_buf)
            self._last_write = time.monotonic()
            if abs(ai_rate - ao_rate) > 1e-9 * fs or abs(ai_rate - fs) > 1e-3 * fs:
                raise DaqFault(f"{d.model}: AI runs at {ai_rate:g} S/s and AO at {ao_rate:g} "
                               f"S/s for {fs:g} S/s; choose another fs_io_hz")
            self._thread = threading.Thread(target=self._watch, name="vcactl-ul-watchdog",
                                            daemon=True)
            self._thread.start()
            self._align(marker, marker_at)
            if self.measured_lag >= len(prefill) - self.block_io // 4:
                raise DaqFault(f"{d.model}: the AO output lags {self.measured_lag} samples "
                               f"behind, more than the AO queue of {len(prefill)}: raise "
                               "limits.ao_transfer_s or daq.ao_queue_blocks")
        except Exception:
            self.stop()
            raise

    def write(self, data: np.ndarray) -> None:
        self._check_fault()
        data = np.asarray(data, dtype=float)
        with self._lock:
            k0, k1 = self.written, self.written + len(data)
            sent = self._call(self._ao_count)
            if sent > k0:
                raise DaqFault(f"AO underrun: UL took {sent - k0} samples beyond the written "
                               "drive (zeros were output)")
            if k1 - sent > self.ao_len:
                raise DaqFault("AO buffer overflow: more drive queued than the buffer holds")
            self._ao_np[np.arange(k0, k1) % self.ao_len] = data
            self.written = k1
            self._last_write = time.monotonic()

    def read(self, n: int) -> np.ndarray:
        """``n`` AI samples (V): shape (n,), or (n, 2) with a loopback channel."""
        need = self.ai_read + n
        t_end = time.monotonic() + READ_TIMEOUT_S
        while True:
            self._check_fault()
            count = self._call(self._ai_count)
            if count >= need:
                break
            if time.monotonic() > t_end:
                raise DaqFault(f"AI read timeout: {count - self.ai_read} of {n} samples")
            time.sleep(POLL_S)
        cols = [self._ai_slice(self.ai_read, need, c) for c in self.columns]
        if self._call(self._ai_count) - self.ai_read > self.ai_len:
            raise DaqFault("AI overrun: data were overwritten before they were read")
        self.ai_read = need
        return cols[0] if len(cols) == 1 else np.column_stack(cols)

    def stop(self) -> None:
        self._halt.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._thread = None
        for dev in (self.daqi, self.ao):
            if dev is None:
                continue
            try:
                dev.scan_stop()
            except Exception:
                pass
        if self.ao is not None:
            try:
                self._halt_ao()
            except Exception as exc:  # report, but never mask the original problem
                warnings.warn(f"could not force AO to 0 V: {exc}")
        if self.device is not None:
            for fn in (self.device.disconnect, self.device.release):
                try:
                    fn()
                except Exception:
                    pass
        self.device = self.ai = self.ao = self.daqi = None
