"""Run logging: metadata JSON, per-frame status CSV and PSD snapshot CSVs."""

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
    def __init__(self, root: Path | str, tag: str):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_tag = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in tag)
        self.dir = Path(root) / f"{stamp}_{safe_tag}"
        self.dir.mkdir(parents=True, exist_ok=False)
        self._status_fh = open(self.dir / "status.csv", "w", newline="")
        self._status = csv.DictWriter(self._status_fh, fieldnames=STATUS_FIELDS)
        self._status.writeheader()

    def meta(self, data: dict) -> None:
        with open(self.dir / "meta.json", "w") as fh:
            json.dump(data, fh, indent=2, default=_jsonable)

    def status(self, row: dict) -> None:
        self._status.writerow({k: row.get(k, "") for k in STATUS_FIELDS})

    def spectra(self, name: str, freqs: np.ndarray, columns: dict) -> Path:
        path = self.dir / f"{name}.csv"
        names = ["f_hz", *columns]
        data = np.column_stack([freqs, *[np.asarray(c, dtype=float) for c in columns.values()]])
        np.savetxt(path, data, delimiter=",", header=",".join(names), comments="", fmt="%.6e")
        return path

    def close(self) -> None:
        self._status_fh.close()
