"""Hardware abstraction: a continuous, clock-shared AO -> AI stream at the I/O rate."""

from __future__ import annotations

from typing import Protocol

import numpy as np


class DaqFault(RuntimeError):
    """Hardware/driver failure (underflow, overflow, device removed). No ramp-down possible."""


def channel_number(name: str) -> int:
    """Number of a channel name such as ``ai1`` or ``ao0``."""
    digits = name[len(name.rstrip("0123456789")):]
    return int(digits) if digits else 0


class DaqBackend(Protocol):
    fs_io: float

    def start(self, prefill: np.ndarray, ai_range_v: float) -> None:
        """Queue ``prefill`` AO samples, then start AO (armed) and AI on one sample clock.

        AO sample k and AI sample k are taken on the same clock edge. AI uses +/-``ai_range_v``
        for the whole run.
        """

    def write(self, data: np.ndarray) -> None:
        """Append AO samples (volts) to the output stream."""

    def read(self, n: int) -> np.ndarray:
        """Block until ``n`` AI samples (volts) are available and return them."""

    def stop(self) -> None:
        """Stop both tasks and force AO to 0 V. Must be safe to call more than once."""
