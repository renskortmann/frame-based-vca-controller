"""FRFs and damping from vcactl recordings (cantilever damping tests).

    python tools/frf_damping.py psd RUN [--df 0.25]
        PSD of every column of raw.f32 (vcactl record: background, amplifier off/on).
    python tools/frf_damping.py frf RUN --ref force_N --resp response_g [--df 0.25]
        H1 and coherence of a recorded run (vcactl run --record): per steady period for a
        periodic random run (no window), else Welch with Hann windows.
    python tools/frf_damping.py fit FRF.csv --band 30 36 [--column NAME]
        One-mode fit (frequency, damping and their standard errors) in a band of an FRF CSV
        (frf_*.csv from this tool, or tap_frf.csv of vcactl tap, with the exponential-window
        damping subtracted).
    python tools/frf_damping.py ringdown RINGDOWN.csv --band 25 45 [--column response_g]
        Damping from a free decay (ringdown_<step>.csv of a stepped sine), and along the decay.
    python tools/frf_damping.py energy RINGDOWN.csv --force force_N --accel response_g --f 27
        Energy per cycle the shaker takes from the beam during a ring-down, and the equivalent
        viscous damper at the drive point.
    python tools/frf_damping.py sync RUN
        A run with an aux DAQ (load cells on a USB-6211): fit the clock offset and drift from
        the drive reference and write aux_aligned.f32 (rows = the control DAQ's AI samples).
    python tools/frf_damping.py friction RUN --normal normal_N --tangential tangential_N
        After sync: the friction coefficient (tangential / normal force) over the run.

Results are printed and written as CSV next to the input.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vcactl.analysis.io import load_raw, read_csv  # noqa: E402
from vcactl.analysis.modal import fit_mode, half_power  # noqa: E402
from vcactl.analysis.ringdown import damper_energy, fit_decay, zeta_vs_amplitude  # noqa: E402
from vcactl.analysis.spectra import h1_periodic, h1_welch, psd  # noqa: E402


def _save(path: Path, cols: dict) -> None:
    np.savetxt(path, np.column_stack(list(cols.values())), delimiter=",",
               header=",".join(cols), comments="", fmt="%.6e")
    print(f"written: {path}")


def cmd_psd(a) -> None:
    rec = load_raw(a.run)
    cols = {}
    for name in rec.columns:
        f, p = psd(rec[name], rec.fs, a.df)
        cols.setdefault("f_hz", f)
        cols[f"{name}2_hz"] = p
        print(f"{name:<16} rms {np.std(rec[name]):.4g}")
    _save(Path(a.run) / "psd.csv", cols)


def cmd_frf(a) -> None:
    rec = load_raw(a.run)
    x, y = rec[a.ref], rec[a.resp]
    period = rec.meta.get("periodic", {}).get("period_samples")
    starts = rec.period_starts(a.max_change_db) if period else []
    lag = rec.response_lag if a.ref.startswith("drive") else 0
    if starts:
        frf = h1_periodic(x, y, rec.fs, starts, int(period), lag)
    else:
        if period:
            print("no steady periods: Welch with Hann windows instead")
        frf = h1_welch(x[:len(x) - lag], y[lag:], rec.fs, a.df)
    print(f"H1 {a.resp} / {a.ref}: {frf.averages} averages, {frf.method}, "
          f"df {frf.f[1] - frf.f[0]:.4g} Hz")
    keep = frf.f <= a.f_max
    _save(Path(a.run) / f"frf_{a.resp}_per_{a.ref}.csv",
          {"f_hz": frf.f[keep], "mag": np.abs(frf.h1[keep]),
           "phase_deg": np.degrees(np.angle(frf.h1[keep])), "coherence": frf.coherence[keep]})


def cmd_fit(a) -> None:
    path = Path(a.csv)
    d = read_csv(path)
    f = d["f_hz"]
    if "mag" in d and not a.column:                 # frf_*.csv of this tool
        mag, phase = d["mag"], d["phase_deg"]
    else:                                           # tap_frf.csv: <name>_mag_<unit>_per_N
        names = [k.split("_mag_")[0] for k in d if "_mag_" in k]
        name = a.column or (names[0] if names else None)
        mag_key = next((k for k in d if k.startswith(f"{name}_mag_")), None)
        if mag_key is None:
            raise SystemExit(f"no magnitude column for '{name}' in {path} (channels: "
                             f"{', '.join(names)})")
        mag, phase = d[mag_key], d[f"{name}_phase_deg"]
    h = mag * np.exp(1j * np.radians(phase))
    tau_w = None
    meta = path.parent / "meta.json"
    if meta.exists():
        tau_w = json.loads(meta.read_text()).get("tau_w_s")
    lo, hi = a.band
    fit = fit_mode(f, h, lo, hi)
    print(fit.format())
    try:
        print(half_power(f, h, lo, hi).format())
    except ValueError as exc:
        print(f"half-power: {exc}")
    if tau_w:
        dz = 1 / (2 * math.pi * fit.f_n * tau_w)
        print(f"exponential window (tau_w {tau_w:.3g} s) adds {100 * dz:.3g} %: "
              f"zeta {100 * (fit.zeta - dz):.4g} %")


def cmd_ringdown(a) -> None:
    d = read_csv(a.csv)
    t = d["t_s"]
    fs = 1 / (t[1] - t[0])
    y = d[a.column]
    fit = fit_decay(t, y, fs, *a.band, t_start=a.t_start)
    print(fit.format())
    amp, z = zeta_vs_amplitude(t, y, fs, *a.band, cycles=a.cycles, t_start=a.t_start)
    _save(Path(a.csv).with_name(Path(a.csv).stem + f"_zeta_{a.band[0]:g}-{a.band[1]:g}Hz.csv"),
          {"envelope": amp, "zeta": z})


def cmd_energy(a) -> None:
    d = read_csv(a.csv)
    t = d["t_s"]
    fs = 1 / (t[1] - t[0])
    after = t >= a.t_start
    t0, e, c = damper_energy(d[a.force][after], d[a.accel][after], fs, a.f)
    good = np.isfinite(c)
    print(f"{len(t0)} cycles; equivalent damper median {np.median(c[good]):.4g} N s/m")
    _save(Path(a.csv).with_name(Path(a.csv).stem + "_energy.csv"),
          {"t_s": t0 + a.t_start, "energy_j": e, "c_eq_ns_m": c})


def cmd_sync(a) -> None:
    from vcactl.analysis.sync import sync_run
    fit = sync_run(a.run)
    print(fit.format())
    print(f"written: {Path(a.run) / 'aux_aligned.f32'}, {Path(a.run) / 'sync.json'}")


def cmd_friction(a) -> None:
    run = Path(a.run)
    meta = json.loads((run / "meta.json").read_text())
    cols = meta["aux"]["columns"]
    data = np.fromfile(run / "aux_aligned.f32", dtype=np.float32).reshape(-1, len(cols))
    n = data[:, cols.index(a.normal)].astype(float)
    t = data[:, cols.index(a.tangential)].astype(float)
    ok = np.isfinite(n) & np.isfinite(t) & (np.abs(n) > a.min_normal)
    mu = np.full(len(n), np.nan)
    mu[ok] = t[ok] / n[ok]
    print(f"normal force {np.nanmean(n):.4g} N mean; friction coefficient |mu| "
          f"{np.nanpercentile(np.abs(mu), 99):.4g} (99th percentile)")
    fs = float(meta["aux"]["fs_main_hz"])
    step = max(1, int(fs / 1000))                    # 1 kS/s is plenty for the output
    _save(run / "friction.csv", {"t_s": np.arange(0, len(n), step) / fs, "normal_n": n[::step],
                                 "tangential_n": t[::step], "mu": mu[::step]})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("psd")
    p.add_argument("run")
    p.add_argument("--df", type=float, default=0.25)
    p = sub.add_parser("frf")
    p.add_argument("run")
    p.add_argument("--ref", default="drive_v")
    p.add_argument("--resp", default="response_g")
    p.add_argument("--df", type=float, default=0.25, help="Welch line spacing (non-periodic)")
    p.add_argument("--max-change-db", type=float, default=0.0,
                   help="periodic: use periods whose drive changed by at most this much")
    p.add_argument("--f-max", type=float, default=5000.0)
    p = sub.add_parser("fit")
    p.add_argument("csv")
    p.add_argument("--band", type=float, nargs=2, required=True, metavar=("LO", "HI"))
    p.add_argument("--column", default=None, help="tap_frf.csv: the response channel name")
    p = sub.add_parser("ringdown")
    p.add_argument("csv")
    p.add_argument("--band", type=float, nargs=2, required=True, metavar=("LO", "HI"))
    p.add_argument("--column", default="response_g")
    p.add_argument("--t-start", type=float, default=0.0)
    p.add_argument("--cycles", type=int, default=20)
    p = sub.add_parser("energy")
    p.add_argument("csv")
    p.add_argument("--force", default="force_N")
    p.add_argument("--accel", default="response_g")
    p.add_argument("--f", type=float, required=True, help="mode frequency (Hz)")
    p.add_argument("--t-start", type=float, default=0.0)
    p = sub.add_parser("sync")
    p.add_argument("run")
    p = sub.add_parser("friction")
    p.add_argument("run")
    p.add_argument("--normal", required=True, help="aux column of the normal force")
    p.add_argument("--tangential", required=True, help="aux column of the friction force")
    p.add_argument("--min-normal", type=float, default=0.5, help="N; below: no coefficient")
    a = ap.parse_args(argv)
    {"psd": cmd_psd, "frf": cmd_frf, "fit": cmd_fit, "ringdown": cmd_ringdown,
     "energy": cmd_energy, "sync": cmd_sync, "friction": cmd_friction}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
