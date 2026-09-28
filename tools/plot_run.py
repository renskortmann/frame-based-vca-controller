"""Plot a vcactl run directory: PSD vs. tolerance bands, FRF and status history.

usage: python tools/plot_run.py runs/<run-dir> [--snapshot psd_final] [--save out.png]
"""

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


def load_csv(path: Path) -> dict:
    data = np.genfromtxt(path, delimiter=",", names=True)
    return {name: data[name] for name in data.dtype.names}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--snapshot", default="psd_final")
    ap.add_argument("--save", type=Path)
    args = ap.parse_args()

    fig, axes = plt.subplots(3, 1, figsize=(10, 11), constrained_layout=True)
    ax_psd, ax_frf, ax_stat = axes

    snap = args.run_dir / f"{args.snapshot}.csv"
    if snap.exists():
        s = load_csv(snap)
        f = s["f_hz"]
        band = s["ref_g2_hz"] > 0
        ax_psd.loglog(f[band], s["ref_g2_hz"][band], "k", label="reference")
        ax_psd.loglog(f[band], s["alarm_lo"][band], "C1--", lw=0.8, label="alarm")
        ax_psd.loglog(f[band], s["alarm_hi"][band], "C1--", lw=0.8)
        ax_psd.loglog(f[band], s["abort_lo"][band], "C3:", lw=0.8, label="abort")
        ax_psd.loglog(f[band], s["abort_hi"][band], "C3:", lw=0.8)
        ax_psd.loglog(f[1:], np.maximum(s["meas_g2_hz"][1:], 1e-12), "C0", lw=0.8, label="control")
        ax_psd.set(xlabel="frequency (Hz)", ylabel="PSD (g²/Hz)", title=f"{args.run_dir.name}: {args.snapshot}")
        ax_psd.legend()
        ax_psd.grid(True, which="both", alpha=0.3)

    frf_path = args.run_dir / "pretest_frf.csv"
    if frf_path.exists():
        h = load_csv(frf_path)
        excited = h["drive_v2_hz"] > 1e-3 * h["drive_v2_hz"].max()
        ax_frf.loglog(h["f_hz"][excited], h["h_mag_g_per_v"][excited], "C0", label="|H| pretest (g/V)")
        ax2 = ax_frf.twinx()
        ax2.semilogx(h["f_hz"][excited], h["coherence"][excited], "C2", lw=0.6, label="coherence")
        ax2.set_ylim(0, 1.05)
        ax2.set_ylabel("coherence")
        ax_frf.set(xlabel="frequency (Hz)", ylabel="|H| (g/V)")
        ax_frf.grid(True, which="both", alpha=0.3)

    status_path = args.run_dir / "status.csv"
    if status_path.exists():
        with open(status_path) as fh:
            rows = [r for r in csv.DictReader(fh) if r["meas_rms_g"]]
        if rows:
            t = [float(r["t_s"]) for r in rows]
            ax_stat.plot(t, [float(r["ref_rms_g"]) for r in rows], "k", label="reference rms (g)")
            ax_stat.plot(t, [float(r["meas_rms_g"]) for r in rows], "C0", label="control rms (g)")
            ax_stat.plot(t, [float(r["drive_rms_v"]) for r in rows], "C1", label="drive rms (V)")
            ax_stat.set(xlabel="time (s)")
            ax_stat.legend()
            ax_stat.grid(True, alpha=0.3)

    if args.save:
        fig.savefig(args.save, dpi=120)
    else:
        plt.show()


if __name__ == "__main__":
    main()
