"""UL for Linux backend (vcactl.daq.ul) against a fake uldaq module.

The fake DT9837A runs its AO and AI in real time from the moment each scan starts: UL takes AO
data ``lead`` samples ahead of the output, the output lags the taken data by a 29-sample DAC
delay, and the AI scan returns the control channel (0.5 x the AO output, 3 samples later), a
loopback channel (the AO output) and the AO readback (with gain and offset errors).
"""

import dataclasses
import threading
import time
import types

import numpy as np
import pytest

import vcactl.daq.ul as ul
from vcactl.config import load_settings
from vcactl.daq.base import DaqFault
from conftest import pin_rates

FS = 51200.0
BLOCK = 2048
DAC_DELAY = 29


class FakeDevice:
    def __init__(self, lead=6000, ao_rate=None):
        self.lead = lead
        self.ao_rate = ao_rate
        self.lock = threading.Lock()
        self.t_ai0 = self.t_ao0 = None
        self.ai_running = self.ao_running = False
        self.sent = 0
        self.taken = np.zeros(0)            # AO samples UL has taken, in order
        self.ai_count = 0
        self.a_out_values = []
        self.iepe = {}
        self.coupling = {}
        self.released = False

    # time -> state
    def advance(self):
        now = time.monotonic()
        if self.ao_running:
            sent = int((now - self.t_ao0) * FS) + self.lead
            if sent > self.sent:
                idx = np.arange(self.sent, sent) % self.ao_len
                self.taken = np.concatenate([self.taken, self.ao_buf[idx]])
                self.sent = sent
        if self.ai_running:
            count = int((now - self.t_ai0) * FS)
            if count > self.ai_count:
                i = np.arange(self.ai_count, count)
                shift = (round((self.t_ai0 - self.t_ao0) * FS) - DAC_DELAY
                         if self.t_ao0 is not None else -10**9)

                def out(k):                 # AO sample at the output at AI index k
                    q = k + shift
                    ok = (q >= 0) & (q < len(self.taken))
                    return np.where(ok, self.taken[np.clip(q, 0, max(len(self.taken) - 1, 0))]
                                    if len(self.taken) else 0.0, 0.0)

                cols = {"control": 0.5 * out(i - 3), "loopback": out(i),
                        "readback": 0.98 * out(i) + 0.012}
                for c, name in enumerate(self.columns):
                    self.ai_buf[(i % self.ai_len) * self.n_in + c] = cols[name]
                self.ai_count = count


class FakeUl(types.SimpleNamespace):
    pass


def make_fake(devices):
    """A fake ``uldaq`` module with the DaqDevices ``devices`` (list of (unique id, FakeDevice))."""
    m = FakeUl()
    m.ULException = type("ULException", (Exception,), {})
    m.InterfaceType = types.SimpleNamespace(USB="usb")
    m.Range = types.SimpleNamespace(BIP1VOLTS="bip1", BIP10VOLTS="bip10")
    m.ScanOption = types.SimpleNamespace(CONTINUOUS="continuous")
    m.ScanStatus = types.SimpleNamespace(RUNNING="running", IDLE="idle")
    m.AOutScanFlag = m.AOutFlag = m.DaqInScanFlag = types.SimpleNamespace(DEFAULT=0)
    m.DaqInChanType = types.SimpleNamespace(ANALOG_SE="se", DAC="dac")
    m.IepeMode = types.SimpleNamespace(ENABLED="on", DISABLED="off")
    m.CouplingMode = types.SimpleNamespace(AC="ac", DC="dc")
    m.DaqInChanDescriptor = lambda channel, kind, rng: (channel, kind, rng)
    m.create_float_buffer = lambda n_chan, n: (ctypes_double * (n_chan * n))()
    m.get_daq_device_inventory = lambda interface: [
        types.SimpleNamespace(product_name="DT9837A", unique_id=uid) for uid, _ in devices]
    by_id = dict(devices)

    class Xfer:
        def __init__(self, n):
            self.current_scan_count = n

    class DaqDevice:
        def __init__(self, descriptor):
            self.dev = by_id[descriptor.unique_id]

        def connect(self):
            pass

        def disconnect(self):
            pass

        def release(self):
            self.dev.released = True

        def get_ai_device(self):
            dev = self.dev

            class Config:
                def set_chan_iepe_mode(self, ch, mode):
                    dev.iepe[ch] = mode

                def set_chan_coupling_mode(self, ch, mode):
                    dev.coupling[ch] = mode

            return types.SimpleNamespace(get_config=lambda: Config())

        def get_ao_device(self):
            dev = self.dev

            def a_out_scan(lo, hi, rng, n, rate, options, flags, buf):
                with dev.lock:
                    dev.ao_buf = np.ctypeslib.as_array(buf)
                    dev.ao_len = n
                    dev.t_ao0 = time.monotonic()
                    dev.ao_running = True
                return dev.ao_rate or rate

            def get_scan_status():
                with dev.lock:
                    dev.advance()
                    return (m.ScanStatus.RUNNING if dev.ao_running else m.ScanStatus.IDLE,
                            Xfer(dev.sent))

            def scan_stop():
                dev.ao_running = False

            def a_out(ch, rng, flags, value):
                dev.a_out_values.append(value)

            return types.SimpleNamespace(a_out_scan=a_out_scan, get_scan_status=get_scan_status,
                                         scan_stop=scan_stop, a_out=a_out)

        def get_daqi_device(self):
            dev = self.dev

            def daq_in_scan(descriptors, n, rate, options, flags, buf):
                with dev.lock:
                    dev.columns = [("readback" if kind == "dac" else
                                    ("control" if ch == 0 else "loopback"))
                                   for ch, kind, _ in descriptors]
                    dev.n_in = len(descriptors)
                    dev.ai_buf = np.ctypeslib.as_array(buf)
                    dev.ai_len = n
                    dev.t_ai0 = time.monotonic()
                    dev.ai_running = True
                return rate

            def get_scan_status():
                with dev.lock:
                    dev.advance()
                    return (m.ScanStatus.RUNNING if dev.ai_running else m.ScanStatus.IDLE,
                            Xfer(dev.ai_count))

            def scan_stop():
                dev.ai_running = False

            return types.SimpleNamespace(daq_in_scan=daq_in_scan, get_scan_status=get_scan_status,
                                         scan_stop=scan_stop)

    m.DaqDevice = DaqDevice
    return m


ctypes_double = __import__("ctypes").c_double


@pytest.fixture
def settings():
    st = pin_rates(load_settings(daq="dt9837a"), FS, 1, 2 * BLOCK)
    return dataclasses.replace(st, daq=dataclasses.replace(st.daq, iepe_current_ma=4.0))


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(ul, "PAD_S", 0.1)
    monkeypatch.setattr(ul, "AI_BUFFER_S", 2.0)


def install(monkeypatch, *devices):
    monkeypatch.setitem(__import__("sys").modules, "uldaq",
                        make_fake(list(devices) or [("01", FakeDevice())]))


def started(settings, monkeypatch, device=None, queue=4, loopback=None):
    dev = device or FakeDevice()
    install(monkeypatch, ("01", dev))
    backend = ul.UlDaq(settings, BLOCK, loopback)
    backend.start(np.zeros(queue * BLOCK), 10.0)
    return backend, dev


def test_streams_aligned_on_the_readback(settings, monkeypatch):
    backend, dev = started(settings, monkeypatch, loopback="ai1")
    rng = np.random.default_rng(1)
    stream = [np.zeros(4 * BLOCK)]
    got = []
    try:
        for _ in range(12):
            x = 0.1 * rng.standard_normal(BLOCK)
            backend.write(x)
            stream.append(x)
            got.append(backend.read(BLOCK))
    finally:
        backend.stop()
    x = np.concatenate(stream)[:12 * BLOCK]
    y = np.concatenate(got)
    assert y.shape == (12 * BLOCK, 2)
    np.testing.assert_allclose(y[3:, 0], 0.5 * x[:-3], atol=1e-12)    # control: plant, 3 later
    np.testing.assert_allclose(y[:, 1], x, atol=1e-12)                # loopback: the AO output
    assert dev.iepe == {0: "on", 1: "off"} and dev.coupling == {0: "ac", 1: "dc"}
    assert 0 < backend.measured_lag <= dev.lead + DAC_DELAY + 64


def test_stop_forces_zero_and_releases(settings, monkeypatch):
    backend, dev = started(settings, monkeypatch)
    backend.write(np.zeros(BLOCK))
    backend.read(BLOCK)
    backend.stop()
    backend.stop()                                   # safe to call twice
    assert dev.a_out_values and dev.a_out_values[-1] == 0.0
    assert dev.released and not dev.ao_running and not dev.ai_running


def test_ao_underrun(settings, monkeypatch):
    backend, _ = started(settings, monkeypatch)
    time.sleep(0.5)                                  # UL takes far beyond the written drive
    with pytest.raises(DaqFault, match="AO underrun"):
        backend.write(np.zeros(BLOCK))
    backend.stop()


def test_ai_overrun(settings, monkeypatch):
    monkeypatch.setattr(ul, "AI_BUFFER_S", 0.3)
    backend, _ = started(settings, monkeypatch)
    time.sleep(0.5)
    with pytest.raises(DaqFault, match="AI overrun"):
        backend.read(BLOCK)
    backend.stop()


def test_watchdog_stops_the_ao(settings, monkeypatch):
    monkeypatch.setattr(ul, "STALL_S", 0.2)
    backend, dev = started(settings, monkeypatch)
    time.sleep(0.5)                                  # no new drive
    assert not dev.ao_running and dev.a_out_values[-1] == 0.0
    with pytest.raises(DaqFault, match="no new drive data"):
        backend.read(BLOCK)
    backend.stop()


def test_zeroes_taken_ao_samples(settings, monkeypatch):
    """Drive taken by UL is cleared, so a stalled controller cannot replay old drive."""
    backend, dev = started(settings, monkeypatch)
    for _ in range(6):
        backend.write(np.ones(BLOCK))
        backend.read(BLOCK)
    time.sleep(0.05)
    with dev.lock:
        dev.advance()
        taken = dev.sent
    in_buffer = backend._ao_np[np.arange(max(0, taken - backend.ao_len + 1), taken - 2000)
                               % backend.ao_len]
    assert np.all(in_buffer == 0.0)
    backend.stop()


def test_rate_mismatch(settings, monkeypatch):
    dev = FakeDevice(ao_rate=FS - 0.5)
    install(monkeypatch, ("01", dev))
    with pytest.raises(DaqFault, match="AI runs at"):
        ul.UlDaq(settings, BLOCK).start(np.zeros(4 * BLOCK), 10.0)
    assert dev.a_out_values[-1] == 0.0               # stopped at 0 V


def test_lag_longer_than_the_queue(settings, monkeypatch):
    install(monkeypatch, ("01", FakeDevice(lead=12000)))
    with pytest.raises(DaqFault, match="lags"):
        ul.UlDaq(settings, BLOCK).start(np.zeros(4 * BLOCK), 10.0)


def test_device_selection(settings, monkeypatch):
    install(monkeypatch, ("01", FakeDevice()), ("02", FakeDevice()))
    with pytest.raises(DaqFault, match="several DT9837A"):
        ul.UlDaq(settings, BLOCK).start(np.zeros(4 * BLOCK), 10.0)
    named = dataclasses.replace(settings, daq=dataclasses.replace(settings.daq, device="03"))
    with pytest.raises(DaqFault, match="no DT9837A with unique ID 03"):
        ul.UlDaq(named, BLOCK).start(np.zeros(4 * BLOCK), 10.0)


def test_marker_search():
    m = ul.chirp_marker(FS)
    rng = np.random.default_rng(0)
    x = rng.normal(0.0, 3e-4, 40000) + 0.012         # readback noise and offset
    assert ul.find_marker(x, m) is None
    x[12345:12345 + len(m)] += 0.98 * m
    assert ul.find_marker(x, m) == 12345
