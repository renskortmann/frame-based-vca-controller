"""Aligning the aux DAQ's recording with the control DAQ (``[aux]`` of a channels file).

The AO samples the controller wrote (``ao.f32``, control-DAQ clock) and the same signal measured
by the aux DAQ (column ``drive_ref`` of ``aux.f32``, aux clock) give the mapping between the two
clocks: the AO index n at aux time tau is n = a + (fs_main + b) tau, fitted by least squares to
the delays found by cross-correlation in windows along the run (windows without signal, e.g.
between chirp markers, are skipped). b / fs_main is the clock drift.

``align`` then resamples every aux channel onto the control DAQ's AI samples: AI sample m measured
the instant the AO sample m - D was output, D = the DAQ's converter filter delay (DAC + ADC), the
same model the controller uses. So row m of ``aux_aligned.f32`` belongs to AI sample m, which is
row m of ``raw.f32`` (``vcactl record``, or ``vcactl run --record`` at decimation 1). Each aux
channel c is sampled c / convert_rate after the aux sample clock (multiplexed ADC); that skew is
removed too.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from scipy import signal

WINDOW_S = 1.0                 # correlation window (aux time)
SEARCH = 64                    # residual lag searched per window (control-DAQ samples)
MIN_CORR = 0.7                 # normalized correlation that counts as a match
COARSE_S = 1.5                 # reference span for the coarse offset
MAX_OFFSET_S = 0.5             # largest start offset between the two DAQs


@dataclass
class SyncFit:
    offset_samples: float      # a: AO index at aux time 0
    rate_hz: float             # fs_main + b: AO samples per aux second
    drift_ppm: float           # aux clock relative to the control DAQ's (+: aux slower)
    residual_samples: float    # rms of the window delays about the line
    windows: int               # windows used

    def ao_index(self, tau: np.ndarray) -> np.ndarray:
        return self.offset_samples + self.rate_hz * tau

    def format(self) -> str:
        return (f"offset {self.offset_samples:.2f} samples, drift {self.drift_ppm:+.2f} ppm, "
                f"residual {self.residual_samples:.3f} samples rms, {self.windows} windows")


def _sub_lag(x: np.ndarray, y: np.ndarray, search: int):
    """Lag of ``y`` relative to ``x`` in [-search, search] (parabolic peak) and the normalized
    correlation at the peak."""
    c = signal.correlate(y, x, mode="full", method="fft")
    mid = len(x) - 1
    lo, hi = mid - search, mid + search + 1
    seg = c[lo:hi]
    k = int(np.argmax(seg))
    rho = seg[k] / (np.linalg.norm(x) * np.linalg.norm(y) + 1e-30)
    frac = 0.0
    if 0 < k < len(seg) - 1:
        a, b, e = seg[k - 1:k + 2]
        den = a - 2 * b + e
        frac = 0.5 * (a - e) / den if den != 0 else 0.0
    return k - search + frac, float(rho)


def estimate(ao: np.ndarray, fs_main: float, ref: np.ndarray, fs_aux: float,
             ref_skew_s: float = 0.0, window_s: float = WINDOW_S) -> SyncFit:
    """Fit the clock mapping from the AO (``ao``, control DAQ) and its aux measurement
    (``ref``), whose samples are at aux times k / fs_aux + ``ref_skew_s``."""
    tau_all = np.arange(len(ref)) / fs_aux + ref_skew_s
    # coarse offset from the first COARSE_S of the reference (short, so that the clock drift
    # cannot smear the correlation peak, and at most one or two chirp markers)
    span = min(len(ref) / fs_aux, COARSE_S)
    n_c = int(span * fs_main)
    ref_m = np.interp(np.arange(n_c) / fs_main, tau_all, ref, left=0.0, right=0.0)
    ao_c = ao[:n_c + int(MAX_OFFSET_S * fs_main)]
    c = signal.correlate(ao_c, ref_m, mode="full", method="fft")
    a0 = float(np.argmax(c) - (len(ref_m) - 1))
    w = int(window_s * fs_aux)
    rate, offset = float(fs_main), a0
    for track in (True, False):
        # pass 1 follows the drift window by window (it accumulates over a long run); pass 2
        # measures every window again with the fitted line held fixed (the reference is then
        # resampled with the right rate inside each window)
        taus, deltas = [], []
        for k0 in range(0, len(ref), w):
            tau = tau_all[k0:k0 + w]
            r = ref[k0:k0 + w]
            n_pred = offset + rate * tau            # predicted AO index of each ref sample
            # keep the part of the window the AO record covers (start and end of the run)
            inside = (n_pred >= SEARCH) & (n_pred <= len(ao) - SEARCH - 2)
            if inside.sum() < w // 4:
                continue
            tau, r, n_pred = tau[inside], r[inside], n_pred[inside]
            if not np.any(r):
                continue
            n0 = int(np.floor(n_pred[0])) - SEARCH
            n1 = int(np.ceil(n_pred[-1])) + SEARCH + 1
            grid = np.arange(n0, n1)
            r_m = np.interp(grid, n_pred, r, left=0.0, right=0.0)   # ref on the AO grid
            lag, rho = _sub_lag(r_m, ao[n0:n1], SEARCH)
            if rho < MIN_CORR:
                continue
            mid = len(tau) // 2
            taus.append(tau[mid])
            deltas.append(offset + rate * tau[mid] + lag)
            if track:
                if len(taus) >= 2:
                    rate, offset = (float(v) for v in np.polyfit(taus, deltas, 1))
                else:
                    offset += lag
        if len(taus) < 2:
            raise ValueError("fewer than two windows with the drive reference: is the AO "
                             "recorded on the aux DAQ (drive_ref)?")
        rate, offset = (float(v) for v in np.polyfit(taus, deltas, 1))
    a0 = offset
    resid = np.asarray(deltas) - np.polyval([rate, a0], taus)
    return SyncFit(a0, rate, (rate / fs_main - 1) * 1e6, float(np.sqrt(np.mean(resid ** 2))),
                   len(taus))


def align(aux: np.ndarray, fs_aux: float, convert_rate_hz: float | None, fit: SyncFit,
          n_main: int, filter_delay: float) -> np.ndarray:
    """Every aux column resampled (linear interpolation) onto the control DAQ's AI samples
    0..n_main-1; NaN where the aux recording does not cover them."""
    m = np.arange(n_main)
    tau = (m - filter_delay - fit.offset_samples) / fit.rate_hz
    out = np.full((n_main, aux.shape[1]), np.nan)
    skew = 1.0 / convert_rate_hz if convert_rate_hz else 0.0
    k_axis = np.arange(len(aux))
    for c in range(aux.shape[1]):
        k = (tau - c * skew) * fs_aux
        out[:, c] = np.interp(k, k_axis, aux[:, c], left=np.nan, right=np.nan)
    return out


def sync_run(run: Path | str) -> SyncFit:
    """Fit and align a run directory with an aux DAQ: writes ``aux_aligned.f32`` (rows: the
    control DAQ's AI samples, columns as in ``meta.json["aux"]``) and ``sync.json``."""
    run = Path(run)
    meta = json.loads((run / "meta.json").read_text())
    am = meta["aux"]
    cols = am["columns"]
    aux = np.fromfile(run / am["file"], dtype=np.float32).astype(float).reshape(-1, len(cols))
    ao = np.fromfile(run / am["ao_file"], dtype=np.float32).astype(float)
    fs_main = float(am["fs_main_hz"])
    conv = am.get("convert_rate_hz")
    r = int(am["drive_ref"])
    skew = r / conv if conv else 0.0
    fit = estimate(ao, fs_main, aux[:, r], float(am["fs_hz"]), skew)
    n_main = int(am.get("n_main") or len(ao))
    aligned = align(aux, float(am["fs_hz"]), conv, fit, n_main, float(am["filter_delay_samples"]))
    aligned.astype(np.float32).tofile(run / "aux_aligned.f32")
    (run / "sync.json").write_text(json.dumps({**asdict(fit), "columns": cols,
                                               "file": "aux_aligned.f32",
                                               "rows": "control-DAQ AI samples (rows of raw.f32 "
                                                       "at decimation 1)"}, indent=2))
    return fit
