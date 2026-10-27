"""Run logging: metadata JSON, per-frame status CSV, PSD snapshot CSVs, for sine tests
step/event tables and ring-down time series, and optionally the raw drive and response."""

from __future__ import annotations

import csv
import dataclasses
import json
import time
from pathlib import Path

import numpy as np

STATUS_FIELDS = ["t_s", "state", "level_db", "ref_rms_g", "meas_rms_g", "rms_err_db",
                 "drive_rms_v", "drive_peak_v", "clip_fraction", "ai_peak_v", "block_rms_g",
                 "lines_alarm", "lines_abort"]


def _jsonable(obj):
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    raise TypeError(f"not serializable: {type(obj)}")


class RunLogger:
    def __init__(self, root: Path | str, tag: str, fields: list[str] = STATUS_FIELDS):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_tag = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in tag)
        self.dir = Path(root) / f"{stamp}_{safe_tag}"
        self.dir.mkdir(parents=True, exist_ok=False)
        self._status_fh = open(self.dir / "status.csv", "w", newline="")
        self.fields = list(fields)
        self._status = csv.DictWriter(self._status_fh, fieldnames=self.fields)
        self._status.writeheader()
        self._raw_fh = None
        self._pending: list[tuple[Path, np.ndarray, str]] = []    # time series, written by close()

    def meta(self, data: dict) -> None:
        with open(self.dir / "meta.json", "w") as fh:
            json.dump(data, fh, indent=2, default=_jsonable)

    def status(self, row: dict) -> None:
        self._status.writerow({k: row.get(k, "") for k in self.fields})

    def spectra(self, name: str, freqs: np.ndarray, columns: dict) -> Path:
        path = self.dir / f"{name}.csv"
        names = ["f_hz", *columns]
        data = np.column_stack([freqs, *[np.asarray(c, dtype=float) for c in columns.values()]])
        np.savetxt(path, data, delimiter=",", header=",".join(names), comments="", fmt="%.6e")
        return path

    def append_row(self, name: str, row: dict) -> Path:
        """Append ``row`` to ``<name>.csv`` (header from the first row), flushed immediately."""
        path = self.dir / f"{name}.csv"
        new = not path.exists()
        with open(path, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)
        return path

    def timeseries(self, name: str, columns: dict) -> Path:
        """Queue ``<name>.csv``; it is written by ``close()``, after the stream has stopped: at
        51.2 kS/s a 2 s ring-down takes about 0.15 s to format, as long as the AO queue lasts."""
        path = self.dir / f"{name}.csv"
        data = np.column_stack([np.asarray(c, dtype=float) for c in columns.values()])
        self._pending.append((path, data, ",".join(columns)))
        return path

    def raw(self, drive: np.ndarray, response: np.ndarray) -> None:
        """Append one half-frame to ``raw.f32``: float32 pairs (drive V, response g) at the
        control rate; sample k is at t = k / fs, the time base of ``status.csv``."""
        if self._raw_fh is None:
            self._raw_fh = open(self.dir / "raw.f32", "ab")
        np.column_stack([drive, response]).astype(np.float32).tofile(self._raw_fh)

    def close(self) -> None:
        self._status_fh.close()
        if self._raw_fh is not None:
            self._raw_fh.close()
        for path, data, header in self._pending:
            np.savetxt(path, data, delimiter=",", header=header, comments="", fmt="%.6e")
        self._pending.clear()
