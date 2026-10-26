"""IEPE settling before the NI tasks start (vcactl.daq.nidaq.wait_until_settled)."""

import numpy as np
import pytest

from vcactl.daq.base import DaqFault
from vcactl.daq.nidaq import wait_until_settled

FS = 51200.0


def chunks(x, n=5120):
    it = iter(range(0, len(x), n))
    return lambda: x[next(it):][:n]


def test_waits_for_the_iepe_start_up():
    """Like the 352B on the USB-4431: clipped at 10.5 V, then a decay with undershoot."""
    t = np.arange(int(10 * FS)) / FS
    x = np.clip(25.0 * np.exp(-t / 0.4) * np.cos(2 * np.pi * 0.35 * t), -10.5, 10.5)
    elapsed = wait_until_settled(chunks(x), FS, 0.2)
    i = int(elapsed * FS)
    assert np.max(np.abs(x[i - int(0.5 * FS):i])) < 0.2     # quiet for the last 0.5 s
    assert 1.5 < elapsed < 5.0


def test_settled_sensor_costs_only_the_hold_time():
    assert wait_until_settled(chunks(np.zeros(int(2 * FS))), FS, 0.2) == pytest.approx(0.5)


def test_no_settling_is_a_fault():
    x = np.full(int(40 * FS), 10.5)                          # e.g. open circuit
    with pytest.raises(DaqFault, match="did not settle within 30 s"):
        wait_until_settled(chunks(x), FS, 0.2)
