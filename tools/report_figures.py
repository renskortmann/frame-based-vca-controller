"""Figures for a test report, from vcactl run directories (PNG, 16 cm wide for an A4 page).

Each figure is drawn only when its inputs are given:

  random.png           --random <-12 dB> <-6 dB> <0 dB>   random runs (psd_final.csv)
  sine_sweep.png       --sine <run> [<run> ...]            sine sweep runs (status.csv)
  shaker_response.png  --frf <pretest run>                 pretest_frf.csv up to 10 kHz
  open_vs_closed.png   --open <run> --closed <run>         sine sweeps recorded with --record
  card_delay.png       --delays <delays.csv>               loopback delay per rate
  iepe_startup.png     --iepe <dir>                        tools/iepe_startup.py output

usage: python tools/report_figures.py --out figures --random r1 r2 r3 --sine s1 s2 ...
"""

import argparse
import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullFormatter  # noqa: E402

INK = "#1f2a36"           # text and reference lines
MUTED = "#8a94a0"         # limits and guides
GRID = "#e3e7ec"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]    # categorical slots 1-3, fixed order
WIDTH_IN = 16 / 2.54
TABLE_RESONANCE_HZ = 20000.0                  # BK 4809 bare-table axial resonance (datasheet)

plt.rcParams.update({
    "font.size": 8, "axes.titlesize": 9, "axes.labelsize": 8, "legend.fontsize": 7.5,
    "xtick.labelsize": 7.5, "ytick.labelsize": 7.5, "text.color": INK, "axes.labelcolor": INK,
    "axes.edgecolor": MUTED, "xtick.color": INK, "ytick.color": INK, "axes.grid": True,
    "grid.color": GRID, "grid.linewidth": 0.6, "axes.spines.top": False,
    "axes.spines.right": False, "legend.frameon": False, "lines.linewidth": 1.2,
    "axes.titleweight": "bold", "axes.titlelocation": "left", "savefig.dpi": 200,
})


def load_csv(path: Path) -> dict:
    data = np.genfromtxt(path, delimiter=",", names=True)
    return {name: data[name] for name in data.dtype.names}


def sweep_rows(run: Path) -> dict:
    with open(run / "status.csv") as fh:
        rows = [r for r in csv.DictReader(fh) if r["state"].startswith("sweep") and r["err_db"]]
    cols = ("t_s", "f_hz", "ref_pk_g", "meas_pk_g", "err_db", "drive_pk_v")
    return {c: np.array([float(r[c]) for r in rows]) for c in cols}


def level_label(run: Path) -> str:
    meta = json.loads((run / "meta.json").read_text())
    level = meta.get("level_db", 0.0)
    return "full level (0 dB)" if level == 0 else f"{level:+g} dB"


def plain_log_ticks(axis, lo, hi) -> None:
    """Log axis labelled 1-2-5 in plain numbers (20, 50, 100, ...) instead of powers of ten."""
    ticks = [m * 10 ** e for e in range(-3, 6) for m in (1, 2, 5) if lo <= m * 10 ** e <= hi]
    axis.set_major_locator(FixedLocator(ticks))
    axis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    axis.set_minor_formatter(NullFormatter())


def log_freq_axis(ax, lo, hi) -> None:
    ax.set_xscale("log")
    ax.set_xlim(lo, hi)
    plain_log_ticks(ax.xaxis, lo, hi)
    ax.set_xlabel("frequency (Hz)")


def save(fig, out: Path, name: str) -> None:
    fig.savefig(out / name)
    plt.close(fig)
    print(f"wrote {out / name}")


# ---------------------------------------------------------------------- figures
def fig_random(runs: list[Path], out: Path) -> None:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(WIDTH_IN, 5.4), constrained_layout=True)
    full = load_csv(runs[-1] / "psd_final.csv")
    b = full["ref_g2_hz"] > 0
    f = full["f_hz"][b]
    ax1.fill_between(f, full["alarm_lo"][b], full["alarm_hi"][b], color=GRID, lw=0,
                     label="tolerance ±3 dB")
    ax1.plot(f, full["abort_lo"][b], color=MUTED, ls="--", lw=0.8, label="abort ±6 dB")
    ax1.plot(f, full["abort_hi"][b], color=MUTED, ls="--", lw=0.8)
    ax1.plot(f, full["meas_g2_hz"][b], color=SERIES[0], lw=0.9, label="measured")
    ax1.plot(f, full["ref_g2_hz"][b], color=INK, lw=1.2, label="required")
    ax1.set_yscale("log")
    log_freq_axis(ax1, f[0], f[-1])
    ax1.set_ylabel("acceleration density (g²/Hz)")
    ax1.set_title(f"a  Random vibration at full level: measured spectrum against required")
    ax1.legend(loc="lower left", ncol=4)

    for i, run in enumerate(runs):
        s = load_csv(run / "psd_final.csv")
        bb = s["ref_g2_hz"] > 0
        err = 10 * np.log10(s["meas_g2_hz"][bb] / s["ref_g2_hz"][bb])
        ax2.plot(s["f_hz"][bb], err, color=SERIES[i % 3], lw=0.8, label=level_label(run))
    for y in (-3, 3):
        ax2.axhline(y, color=MUTED, ls="--", lw=0.8)
    ax2.text(f[0] * 1.05, 3.15, "tolerance ±3 dB", color=MUTED, fontsize=7, va="bottom")
    ax2.axhline(0, color=INK, lw=0.6)
    log_freq_axis(ax2, f[0], f[-1])
    ax2.set_ylim(-4, 4)
    ax2.set_ylabel("deviation from required (dB)")
    ax2.set_title("b  Deviation per frequency at each level")
    ax2.legend(loc="lower left", ncol=3)
    save(fig, out, "random.png")


def fig_sine(runs: list[Path], out: Path) -> None:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(WIDTH_IN, 5.0), constrained_layout=True)
    lo, hi = np.inf, 0.0
    for i, run in enumerate(runs):
        s = sweep_rows(run)
        lo, hi = min(lo, s["f_hz"].min()), max(hi, s["f_hz"].max())
        ax1.plot(s["f_hz"], -s["err_db"], color=SERIES[i % 3], lw=0.9,
                 label=f"{level_label(run)}, {s['ref_pk_g'].max():.3g} g")
        ax2.plot(s["f_hz"], s["drive_pk_v"] * 1000, color=SERIES[i % 3], lw=1.0,
                 label=level_label(run))
    for y in (-1, 1):
        ax1.axhline(y, color=MUTED, ls="--", lw=0.8)
    ax1.text(lo * 1.05, 1.03, "alarm limit ±1 dB (the run stops at ±3 dB)",
             color=MUTED, fontsize=7, va="bottom")
    ax1.axhline(0, color=INK, lw=0.6)
    ax1.set_ylim(-1.4, 1.4)
    log_freq_axis(ax1, lo, hi)
    ax1.set_ylabel("deviation from required (dB)")
    ax1.set_title("a  Sine sweep: measured amplitude against required")
    ax1.legend(loc="lower right", ncol=2)
    ax2.set_yscale("log")
    log_freq_axis(ax2, lo, hi)
    plain_log_ticks(ax2.yaxis, *ax2.get_ylim())
    ax2.set_ylabel("drive to amplifier (mV peak)")
    ax2.set_title("b  Drive needed: the controller compensates the shaker's own response")
    ax2.legend(loc="upper right")
    save(fig, out, "sine_sweep.png")


def mount_over_reading(f, h):
    """Sensor reading above the table motion (dB): |H| against its 1-2 kHz level, with the
    bare table's small rise towards its 20 kHz resonance taken out."""
    ref = h[(f >= 1000) & (f <= 2000)].mean()
    table = 1 / np.abs(1 - (f / TABLE_RESONANCE_HZ) ** 2)
    return 20 * np.log10(h / ref / table), ref


def fig_frf(run: Path, out: Path, band_hi: float = 3000.0) -> None:
    h = load_csv(run / "pretest_frf.csv")
    ok = (h["f_hz"] >= 20) & (h["drive_v2_hz"] > 1e-3 * h["drive_v2_hz"].max())
    f, mag = h["f_hz"][ok], h["h_mag_g_per_v"][ok]
    over, mass_line = mount_over_reading(f, mag)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(WIDTH_IN, 5.4), constrained_layout=True,
                                   gridspec_kw={"height_ratios": [3, 2]})
    ax1.axvspan(band_hi, f[-1], color=GRID, lw=0)
    ax1.text(band_hi * 1.08, mag.min() * 1.05, "above the test band\n(magnet mount)",
             color=MUTED, fontsize=7, va="bottom")
    ax1.axhline(mass_line, color=MUTED, ls="--", lw=0.8)
    ax1.text(25, mass_line * 1.12, f"expected from the datasheets: {mass_line:.0f} g/V",
             color=MUTED, fontsize=7)
    ax1.plot(f, mag, color=SERIES[0], lw=1.2)
    low = (f > 50) & (f < 1000)
    k1 = np.flatnonzero(low)[np.argmax(mag[low])]
    k2 = int(np.argmax(mag))
    ax1.annotate(f"shaker resonance\n{f[k1]:.0f} Hz", (f[k1], mag[k1]), xytext=(f[k1] * 0.35, mag[k1] * 2.6),
                 fontsize=7, arrowprops={"arrowstyle": "-", "color": MUTED, "lw": 0.6})
    ax1.annotate(f"accelerometer on its magnet\n{f[k2] / 1000:.1f} kHz",
                 (f[k2], mag[k2]), xytext=(f[k2] * 0.25, mag[k2] * 0.9), fontsize=7, ha="right",
                 arrowprops={"arrowstyle": "-", "color": MUTED, "lw": 0.6})
    ax1.set_yscale("log")
    log_freq_axis(ax1, 20, f[-1])
    plain_log_ticks(ax1.yaxis, *ax1.get_ylim())
    ax1.set_ylabel("acceleration per drive volt (g/V)")
    ax1.set_title("a  Shaker response measured in the pretest")

    m = (f >= 1500) & (f <= 5000)
    ax2.plot(f[m], over[m], color=SERIES[1], lw=1.2)
    ax2.axhline(3, color=MUTED, ls="--", lw=0.8)
    ax2.text(1550, 3.15, "tolerance 3 dB", color=MUTED, fontsize=7, va="bottom")
    ax2.axvline(band_hi, color=INK, lw=0.8)
    ax2.text(band_hi * 0.98, 5.2, f"upper limit of the tests\n{band_hi / 1000:g} kHz",
             color=INK, fontsize=7, ha="right", va="top")
    ax2.axhline(0, color=INK, lw=0.6)
    ax2.set_xlim(1500, 5000)
    ax2.set_ylim(-0.5, 5.5)
    ax2.set_xlabel("frequency (Hz)")
    ax2.set_ylabel("sensor over-reads (dB)")
    ax2.set_title("b  Error caused by the magnet mount (the table gets this much less)")
    save(fig, out, "shaker_response.png")


def three_cycles(run: Path, f_target: float):
    meta = json.loads((run / "meta.json").read_text())
    fs = meta["raw"]["fs_hz"]
    raw = np.fromfile(run / "raw.f32", dtype=np.float32).reshape(-1, 2)
    s = {c: v[4:] for c, v in sweep_rows(run).items()}    # frames after the ramp-in
    k = int(np.argmin(np.abs(s["f_hz"] - f_target)))
    # the status row covers the frame ending at t_s; take three cycles in its middle
    centre = round(s["t_s"][k] * fs) - meta["settings"]["control"]["frame_size"] // 2
    n = round(3 * fs / s["f_hz"][k])
    y = raw[centre - n // 2:centre - n // 2 + n, 1]
    return np.arange(n) / fs * 1000, y, s["f_hz"][k], s["ref_pk_g"][k]


def fig_open_closed(open_run: Path, closed_run: Path, out: Path) -> None:
    so, sc = sweep_rows(open_run), sweep_rows(closed_run)
    # the time traces at the resonance, from the open-loop run's own pretest
    h = load_csv(open_run / "pretest_frf.csv")
    m = (h["f_hz"] >= so["f_hz"].min()) & (h["f_hz"] <= 1000)
    f_show = float(h["f_hz"][m][np.argmax(h["h_mag_g_per_v"][m])])
    fig = plt.figure(figsize=(WIDTH_IN, 5.6), constrained_layout=True)
    gs = fig.add_gridspec(2, 2, height_ratios=[1.1, 1])
    ax = fig.add_subplot(gs[0, :])
    ax.plot(so["f_hz"], so["meas_pk_g"], color=SERIES[1], lw=1.2, label="open loop (no control)")
    ax.plot(sc["f_hz"], sc["meas_pk_g"], color=SERIES[0], lw=1.2, label="closed loop (vcactl)")
    ax.plot(sc["f_hz"], sc["ref_pk_g"], color=INK, lw=0.8, ls="--", label="required")
    ax.axvline(f_show, color=MUTED, lw=0.6)
    log_freq_axis(ax, so["f_hz"].min(), so["f_hz"].max())
    ax.set_ylim(0, max(so["meas_pk_g"].max(), sc["meas_pk_g"].max()) * 1.15)
    ax.set_ylabel("amplitude (g peak)")
    ax.set_title("a  Sine sweep with and without the controller")
    ax.legend(loc="upper right")

    traces = [three_cycles(open_run, f_show), three_cycles(closed_run, f_show)]
    lim = max(np.max(np.abs(t[1])) for t in traces) * 1.15
    for j, ((t, y, f, ref), name, color) in enumerate(
            zip(traces, ("b  Open loop", "c  Closed loop"), (SERIES[1], SERIES[0]))):
        a = fig.add_subplot(gs[1, j])
        for r in (-ref, ref):
            a.axhline(r, color=INK, lw=0.8, ls="--")
        a.plot(t, y, color=color, lw=1.2)
        a.set_ylim(-lim, lim)
        a.set_xlim(t[0], t[-1])
        a.set_xlabel("time (ms), three cycles")
        a.set_ylabel("acceleration (g)" if j == 0 else "")
        a.set_title(f"{name}, {f:.0f} Hz")
        a.text(t[-1], ref, f"required ±{ref:.2g} g ", color=INK, fontsize=7,
               ha="right", va="bottom")
    save(fig, out, "open_vs_closed.png")


def fig_delays(path: Path, out: Path, used_hz: float = 51200.0) -> None:
    d = load_csv(path)
    meta = json.loads((path.parent / "meta.json").read_text())
    fs, delay = d["fs_hz"], d["delay_start_samples"]
    fig, ax = plt.subplots(figsize=(WIDTH_IN, 2.8), constrained_layout=True)
    ax.axhline(meta["datasheet_samples"], color=MUTED, ls="--", lw=0.8)
    ax.text(fs.min() / 1000, meta["datasheet_samples"] + 1.5,
            f"datasheet value, used before: {meta['datasheet_samples']:g} samples",
            color=MUTED, fontsize=7, va="bottom")
    ax.plot(fs / 1000, delay, "o", color=SERIES[0], ms=4.5, mec="white", mew=0.8)
    i = int(np.argmin(np.abs(fs - used_hz)))
    ax.annotate(f"rate used in the tests:\n{delay[i]:.1f} samples = "
                f"{delay[i] / fs[i] * 1000:.1f} ms",
                (fs[i] / 1000, delay[i]), xytext=(fs[i] / 1000 * 0.3, delay[i] + 2),
                fontsize=7, ha="right", va="center",
                arrowprops={"arrowstyle": "-", "color": MUTED, "lw": 0.6})
    ax.set_xscale("log")
    ax.set_xlim(fs.min() / 1000 * 0.9, fs.max() / 1000 * 1.1)
    plain_log_ticks(ax.xaxis, *ax.get_xlim())
    ax.set_xlabel("sample rate (kS/s)")
    ax.set_ylabel("delay (samples)")
    ax.set_ylim(55, 110)
    ax.set_title(f"Measured delay of the {meta['daq']} at each sample rate")
    save(fig, out, "card_delay.png")


def fig_iepe(run: Path, out: Path) -> None:
    d = load_csv(run / "iepe_startup.csv")
    meta = json.loads((run / "meta.json").read_text())
    t, v = d["t_s"], d["ai_v"]
    fig, ax = plt.subplots(figsize=(WIDTH_IN, 2.8), constrained_layout=True)
    r, lim = meta["range_v"], meta["settle_limit_v"]
    for y in (-r, r):
        ax.axhline(y, color=MUTED, ls="--", lw=0.8)
    ax.text(t[-1], r * 0.97, f"input range ±{r:g} V", color=MUTED, fontsize=7,
            ha="right", va="top")
    ax.axhspan(-lim, lim, color=GRID, lw=0)
    ax.plot(t, v, color=SERIES[0], lw=1.0)
    if meta.get("settle_s") is not None:
        ax.axvline(meta["settle_s"], color=INK, lw=0.8)
        ax.text(meta["settle_s"] + 0.04, r * 0.55,
                f"settled: the controller\nstarts after {meta['settle_s']:.1f} s",
                color=INK, fontsize=7, va="top")
    ax.set_xlim(0, t[-1])
    ax.set_ylim(-r * 1.1, r * 1.1)
    ax.set_xlabel("time after switching on the sensor supply (s)")
    ax.set_ylabel("accelerometer output (V)")
    ax.set_title(f"Start-up of the {meta['sensor'].split(' (')[0]} (shaker at rest)")
    save(fig, out, "iepe_startup.png")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--random", type=Path, nargs="+")
    ap.add_argument("--sine", type=Path, nargs="+")
    ap.add_argument("--frf", type=Path)
    ap.add_argument("--open", type=Path)
    ap.add_argument("--closed", type=Path)
    ap.add_argument("--delays", type=Path)
    ap.add_argument("--iepe", type=Path)
    ap.add_argument("--band-hi", type=float, default=3000.0,
                    help="upper frequency of the tests (Hz), marked in shaker_response.png")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.random:
        fig_random(args.random, args.out)
    if args.sine:
        fig_sine(args.sine, args.out)
    if args.frf:
        fig_frf(args.frf, args.out, args.band_hi)
    if args.open and args.closed:
        fig_open_closed(args.open, args.closed, args.out)
    if args.delays:
        fig_delays(args.delays, args.out)
    if args.iepe:
        fig_iepe(args.iepe, args.out)


if __name__ == "__main__":
    main()
