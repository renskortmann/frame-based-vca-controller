"""Notch a random profile so that the cantilever tip stays within the lidar's velocity range.

    python tools/notch_profile.py --profile config/test_profiles/cantilever_random_periodic.toml \\
        --frf tip_per_drive.csv --level -20 --limit 15.5 --crest 4 --out notched.toml

``--frf``: CSV with columns f_hz and mag, the tip velocity (mm/s) per drive-point acceleration
(g), from a low-level run (lidar tip velocity over the control accelerometer, e.g. a sine sweep
or random run at -30 dB). ``--level``: the level (dB) the notched profile will run at; the limit
applies there. The result keeps every key of the input profile (drive, period_s, duration_s,
tolerance) and replaces its breakpoints; notches narrower than three control lines are widened
(the controller cannot follow them).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vcactl.analysis.io import read_csv  # noqa: E402
from vcactl.analysis.notch import notch_profile, predicted_rms  # noqa: E402
from vcactl.config import read_toml  # noqa: E402
from vcactl.profile import load_profile  # noqa: E402


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return repr(float(v)) if isinstance(v, float) else str(v)


def write_profile(path: Path, table: dict, breakpoints, comment: str) -> None:
    lines = [f"# {line}" for line in comment.splitlines()]
    for k, v in table.items():
        if k in ("breakpoints", "tolerance"):
            continue
        lines.append(f"{k} = {_toml_value(v)}")
    lines.append("")
    lines.append("breakpoints = [")
    for f, s in breakpoints:
        lines.append(f"    {{ f_hz = {f:.4f}, psd_g2_hz = {s:.6e} }},")
    lines.append("]")
    if "tolerance" in table:
        lines.append("")
        lines.append("[tolerance]")
        for k, v in table["tolerance"].items():
            lines.append(f"{k} = {_toml_value(v)}")
    path.write_text("\n".join(lines) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True, help="base random profile (TOML)")
    ap.add_argument("--frf", required=True, help="CSV: f_hz, mag (tip mm/s per drive-point g)")
    ap.add_argument("--level", type=float, default=0.0, help="run level (dB) of the result")
    ap.add_argument("--limit", type=float, default=15.5, help="tip velocity peak limit (mm/s)")
    ap.add_argument("--crest", type=float, default=4.0, help="peak / rms of the tip velocity")
    ap.add_argument("--line-spacing", type=float, default=6.25, help="control lines (Hz)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    profile = load_profile(a.profile)
    table = read_toml(Path(a.profile))
    d = read_csv(a.frf)
    k = 10 ** (a.level / 10)
    s_bp = np.array(profile.psd_g2_hz) * k
    res = notch_profile(profile.f_hz, s_bp, d["f_hz"], d["mag"], a.limit, a.crest,
                        a.line_spacing)
    bps = [(f, s / k) for f, s in res.breakpoints]     # back to 0 dB of the profile
    print(f"tip velocity at {a.level:+g} dB: {res.rms_before:.3g} mm/s rms before, "
          f"{res.rms_after:.3g} after (limit {a.limit:g} / {a.crest:g} = "
          f"{a.limit / a.crest:.3g} mm/s rms)")
    for n in res.notches:
        print(f"  notch {n.f_lo:8.2f}-{n.f_hi:8.2f} Hz, {n.depth_db:4.1f} dB")
    for w in res.warnings:
        print(f"warning: {w}")
    table["name"] = f"{profile.name}, notched"
    write_profile(Path(a.out), table, bps,
                  f"Notched with tools/notch_profile.py from {a.profile} and {a.frf}:\n"
                  f"tip <= {a.limit:g} mm/s peak (crest {a.crest:g}) at {a.level:+g} dB.")
    check = predicted_rms([(f, s * k) for f, s in bps], d["f_hz"], d["mag"])
    load_profile(a.out)                       # the result must load as a profile
    print(f"written: {a.out} (predicted {check:.3g} mm/s rms at {a.level:+g} dB)")
    return 0 if not res.warnings else 1


if __name__ == "__main__":
    sys.exit(main())
