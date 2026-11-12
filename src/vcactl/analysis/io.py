"""Reading run directories."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class Recording:
    """A float32 recording with named columns (``raw.f32`` or ``hits.f32``)."""
    dir: Path
    meta: dict
    data: np.ndarray                    # (samples, columns), float64
    columns: list
    fs: float
    events: list = field(default_factory=list)

    def __getitem__(self, name: str) -> np.ndarray:
        try:
            return self.data[:, self.columns.index(name)]
        except ValueError:
            raise KeyError(f"no column '{name}' in {self.dir} (columns: "
                           f"{', '.join(self.columns)})") from None

    @property
    def response_lag(self) -> int:
        """Samples from a drive sample to its response sample (``vcactl run --record``)."""
        return int(self.meta.get("raw", {}).get("response_lag_samples", 0))

    def period_starts(self, max_change_db: float = 0.0, skip_after_change: int = 1) -> list:
        """Start samples of the periods of a periodic random run that can be averaged: the drive
        changed by at most ``max_change_db`` at their start, and none of the
        ``skip_after_change`` periods before changed more (the response must settle), and the
        whole period was driven before the ramp-down (and its response recorded)."""
        ev = [e for e in self.events if e.get("event") == "period_start"]
        if not ev:
            return []
        n = int(ev[0]["period_samples"])
        lag = self.response_lag
        end = len(self.data) - lag
        for e in self.events:
            if e.get("event") == "rampdown":
                end = min(end, int(e["raw_sample"]))
        out = []
        for i, e in enumerate(ev):
            recent = ev[max(0, i - skip_after_change):i + 1]
            if i == 0 or any(float(r.get("change_db", "inf")) > max_change_db for r in recent):
                continue
            k = int(e["raw_sample"])
            if k + n <= end:
                out.append(k)
        return out


def read_events(path: Path) -> list:
    if not path.exists():
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def load_raw(run: Path | str, name: str = "raw.f32") -> Recording:
    """``raw.f32`` (``vcactl run --record``, ``vcactl record``) or ``hits.f32`` (``vcactl tap``)
    of a run directory, with ``meta.json`` and ``events.csv``. Old recordings without
    ``columns`` in meta.json are (drive_v, response_g) pairs."""
    run = Path(run)
    meta = json.loads((run / "meta.json").read_text())
    info = meta.get("hits" if name == "hits.f32" else "raw", {})
    columns = list(info.get("columns", ["drive_v", "response_g"]))
    fs = float(info["fs_hz"])
    data = np.fromfile(run / name, dtype=np.float32).astype(float).reshape(-1, len(columns))
    return Recording(run, meta, data, columns, fs, read_events(run / "events.csv"))


def load_hits(run: Path | str) -> tuple[Recording, np.ndarray]:
    """``hits.f32`` of a tap test as (Recording, records): records (hits, samples, columns),
    and the accepted flags from hits.csv in ``Recording.events``."""
    rec = load_raw(run, "hits.f32")
    n = int(rec.meta["hits"]["record_samples"])
    records = rec.data.reshape(-1, n, len(rec.columns))
    rec.events = read_events(Path(run) / "hits.csv")
    return rec, records


def read_csv(path: Path | str) -> dict:
    """A CSV with a header row as columns of floats (e.g. tap_frf.csv, pretest_frf.csv)."""
    data = np.genfromtxt(path, delimiter=",", names=True)
    return {name: np.asarray(data[name], dtype=float) for name in data.dtype.names}
