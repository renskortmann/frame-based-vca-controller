"""Write a PDF that compares the shakers in config/shakers.

All ratings and limits are read from the shaker files, so rerun this after changing them.

usage: python tools/shaker_comparison.py [--out docs/shaker_comparison.pdf]
requires: pip install -e ".[report]"
"""

from __future__ import annotations

import argparse
import tempfile
import time
from math import pi, sqrt
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.enums import TA_LEFT  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate,  # noqa: E402
                                Spacer, Table, TableStyle)

from vcactl.config import load_shaker  # noqa: E402

G = 9.80665
F_LO = 20.0                          # start frequency of the flat reference profiles (Hz)
PSDS = (0.001, 0.01, 0.05)           # g^2/Hz
F_MAXS = (200, 500, 1000, 2000, 5000, 7000, 10000)

KEYS = ("tv51110", "tv52110", "bk4809", "bk4801_4812")

# Facts that are only in comments of the shaker files or in the manuals.
EXTRA = {
    "tv51110": dict(short="TIRA TV 51110",
                    system="TIRA Vibration Test System TV 51110: exciter S 51110 with power "
                           "amplifier BDA 120",
                    amp="TIRA BDA 120 (in system)",
                    amp_out="120 VA; 22 V rms, 5.5 A rms", amp_mode="voltage mode only",
                    cooling="none needed", exciter_kg="12 kg", half_mm=None,
                    derived=set(), static_src="derived"),
    "tv52110": dict(short="TIRA TV 52110",
                    system="TIRA Vibration Test System TV 52110: exciter S 52110 with power "
                           "amplifier BDA 120",
                    amp="TIRA BDA 120 (in system)",
                    amp_out="120 VA; 22 V rms, 5.5 A rms", amp_mode="voltage mode only",
                    cooling="none needed", exciter_kg="36 kg", half_mm=None,
                    derived=set(), static_src="derived"),
    "bk4809": dict(short="B&K 4809",
                   system="Br\u00fcel & Kj\u00e6r Vibration Exciter Type 4809 with Power "
                          "Amplifier Type 2718",
                   amp="B&K 2718",
                   amp_out="75 VA into 3 Ω; current limit 1-5 A rms",
                   amp_mode="voltage, 20 dB gain (assumed)",
                   cooling="optional air (not used in ratings)", exciter_kg="8.3 kg", half_mm=None,
                   derived={"force_random_rms_n", "accel_random_rms_g"}, static_src="derived"),
    "bk4801_4812": dict(short="B&K 4801/4812",
                        system="Br\u00fcel & Kj\u00e6r Exciter Body Type 4801 with General "
                               "Purpose Head Type 4812 and Power Amplifier Type 2707",
                        amp="B&K 2707",
                        amp_out="220 VA into 0.5 Ω; 10 V rms, 22 A rms",
                        amp_mode="current, 14 A/V (assumed)",
                        cooling="blower in body (required)", exciter_kg="80 kg; 3-phase mains",
                        half_mm=6.35, derived={"force_random_rms_n", "accel_random_rms_g"},
                        static_src="manual"),
}

# Light-mode categorical slots 1-4 of the dataviz reference palette, plus distinct markers so
# identity never depends on colour alone.
SERIES = {"tv51110": ("#2a78d6", "o"), "tv52110": ("#eb6834", "s"),
          "bk4809": ("#1baf7a", "^"), "bk4801_4812": ("#eda100", "D")}
INK, INK_2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#ffffff"


def stroke_pp_mm(psd: float, f_max: float) -> float:
    """3 sigma peak-peak displacement of a flat acceleration PSD from F_LO to f_max."""
    var = psd * G**2 / (2 * pi) ** 4 * (F_LO**-3 - f_max**-3) / 3
    return 6 * sqrt(var) * 1e3


def payload_limit(s, half_mm: float, psd: float, f_max: float) -> tuple[float, bool, float]:
    """(max payload kg, limited by stroke?, a_rms g) for a flat profile, vertical mounting."""
    a = sqrt(psd * (f_max - F_LO))
    by_force = s.force_random_rms_n / (a * G) - s.moving_mass_kg
    by_stroke = s.suspension_stiffness_n_per_mm * (half_mm - stroke_pp_mm(psd, f_max) / 2) / G
    return min(by_force, by_stroke), by_stroke < by_force, a


def fmt_f(f: float) -> str:
    return f"{f / 1000:g} kHz" if f >= 1000 else f"{f:g} Hz"


def chart(shakers: dict, path: Path) -> None:
    psd = 0.01
    fig, ax = plt.subplots(figsize=(7.0, 3.6), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    for key, s in shakers.items():
        half = EXTRA[key]["half_mm"] or s.displacement_pp_mm / 2
        f = np.geomspace(200, min(10000, s.f_max_hz), 60)
        m = [payload_limit(s, half, psd, x)[0] for x in f]
        color, marker = SERIES[key]
        ax.plot(f, m, color=color, lw=2, solid_capstyle="round")
        fm = [x for x in F_MAXS if x <= s.f_max_hz]
        ax.plot(fm, [payload_limit(s, half, psd, x)[0] for x in fm], ls="none", marker=marker,
                ms=6, color=color, markeredgecolor=SURFACE, markeredgewidth=1.2)
        ax.annotate(EXTRA[key]["short"], (f[-1], m[-1]), xytext=(6, 0),
                    textcoords="offset points", va="center", fontsize=8, color=INK)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(180, 10000 * 2.6)
    ax.set_xticks([200, 500, 1000, 2000, 5000, 10000])
    ax.set_xticklabels(["200 Hz", "500 Hz", "1 kHz", "2 kHz", "5 kHz", "10 kHz"])
    ax.set_yticks([0.1, 0.2, 0.5, 1, 2, 5, 10])
    ax.set_yticklabels(["0.1", "0.2", "0.5", "1", "2", "5", "10"])
    ax.minorticks_off()
    ax.set_xlabel("f_max of a flat profile from 20 Hz at 0.01 g²/Hz", color=INK_2, fontsize=8)
    ax.set_ylabel("max payload (kg)", color=INK_2, fontsize=8)
    ax.tick_params(colors=INK_2, labelsize=8, length=0)
    ax.grid(True, color=GRID, lw=0.8)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    handles = [plt.Line2D([], [], color=SERIES[k][0], marker=SERIES[k][1], lw=2, ms=6,
                          markeredgecolor=SURFACE) for k in shakers]
    ax.legend(handles, [EXTRA[k]["short"] for k in shakers], frameon=False, fontsize=8,
              loc="lower left", ncol=2, labelcolor=INK)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def build(out: Path) -> None:
    shakers = {k: load_shaker(k) for k in KEYS}
    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["Normal"], fontName="Helvetica", fontSize=9,
                          leading=12.5, textColor=colors.HexColor(INK), alignment=TA_LEFT)
    small = ParagraphStyle("small", parent=body, fontSize=7.5, leading=10,
                           textColor=colors.HexColor(INK_2))
    cell = ParagraphStyle("cell", parent=body, fontSize=8, leading=10)
    cellb = ParagraphStyle("cellb", parent=cell, fontName="Helvetica-Bold")
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontName="Helvetica-Bold", fontSize=18,
                        leading=22, alignment=TA_LEFT, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=11.5,
                        leading=14, spaceBefore=10, spaceAfter=4)

    def P(text, style=cell):
        return Paragraph(text, style)

    def esc(text):
        return text.replace("&", "&amp;")

    def table(rows, widths, header_rows=1, first_col_bold=True):
        data = [[P(c, cellb if (r < header_rows or (j == 0 and first_col_bold)) else cell)
                 for j, c in enumerate(row)] for r, row in enumerate(rows)]
        t = Table(data, colWidths=widths, repeatRows=header_rows)
        style = [("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("LINEBELOW", (0, header_rows - 1), (-1, header_rows - 1), 0.8,
                  colors.HexColor(INK_2)),
                 ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]
        for r in range(header_rows, len(rows)):
            if (r - header_rows) % 2:
                style.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#f4f3f0")))
        t.setStyle(TableStyle(style))
        return t

    def val(key, field, text):
        return text + ("<super>d</super>" if field in EXTRA[key]["derived"] else "")

    story = [P("Shaker comparison", h1),
             P(f"The four vibration test systems configured in <font face='Courier'>config/shakers</font>, "
               f"generated on {time.strftime('%Y-%m-%d')} from the shaker files.", small),
             Spacer(1, 6)]

    story += [P("Shakers compared", h2)]
    for k in KEYS:
        story.append(P(f"\u2022 <b>{esc(EXTRA[k]['short'])}</b> "
                       f"(<font face='Courier'>--shaker {k}</font>): {esc(EXTRA[k]['system'])}",
                       body))

    story += [P("At a glance", h2), P(
        "The <b>B&amp;K 4801/4812</b> is the only heavy-payload system: about 4.5 times the random "
        "force of the TV 51110 and a 13.5 kg static payload limit, but it "
        "weighs 80 kg, needs three-phase mains and must run with its blower. The <b>TIRA TV 51110</b> "
        "and <b>TV 52110</b> are complete small systems with the same 100 N sine force; the TV 51110 "
        "has more random force (70 vs. 50 N rms) and acceleration, the TV 52110 more stroke "
        "(15 vs. 13 mm) and a stiffer suspension. The <b>B&amp;K 4809</b> has the lightest moving "
        "element and the widest frequency range (to 20 kHz), which suits small, light specimens "
        "and high frequencies, but the least force, so payload drops fastest with level.", body)]

    names = [esc(EXTRA[k]["short"]) for k in KEYS]
    w0, wc = 46 * mm, 32 * mm
    rows = [["", *names]]

    def row(label, fn):
        rows.append([label, *[fn(k, shakers[k]) for k in KEYS]])

    row("Amplifier", lambda k, s: esc(EXTRA[k]["amp"]))
    row("Frequency range", lambda k, s: f"{s.f_min_hz:g} Hz - {fmt_f(s.f_max_hz)}")
    row("Force, sine peak", lambda k, s: f"{s.force_sine_peak_n:g} N")
    row("Force, random rms", lambda k, s: val(k, "force_random_rms_n", f"{s.force_random_rms_n:g} N"))
    row("Acceleration, sine peak", lambda k, s: f"{s.accel_sine_peak_g:g} g")
    row("Acceleration, random rms", lambda k, s: val(k, "accel_random_rms_g", f"{s.accel_random_rms_g:g} g"))
    row("Stroke (peak-peak)", lambda k, s: f"{s.displacement_pp_mm:g} mm")
    row("Velocity, peak", lambda k, s: f"{s.velocity_peak_m_s:g} m/s")
    row("Moving mass", lambda k, s: f"{s.moving_mass_kg * 1000:g} g")
    row("Suspension stiffness", lambda k, s: f"{s.suspension_stiffness_n_per_mm:g} N/mm")
    row("Armature resonance", lambda k, s: fmt_f(s.armature_resonance_hz))
    row("Static payload limit", lambda k, s: f"{s.payload_static_max_kg:g} kg" +
        ("<super>d</super>" if EXTRA[k]["static_src"] == "derived" else ""))
    row("Sag per kg (vertical)", lambda k, s: f"{G / s.suspension_stiffness_n_per_mm:.2f} mm")
    row("Exciter mass", lambda k, s: EXTRA[k]["exciter_kg"])
    row("Cooling", lambda k, s: EXTRA[k]["cooling"])
    story += [P("Ratings", h2), table(rows, [w0] + [wc] * 4),
              P("<super>d</super> Derived, not in the datasheet: random ratings as sine peak / "
                "√2; static payload limit as stiffness × half-stroke / g (the payload "
                "whose weight uses up the half-stroke on a vertical shaker).", small)]

    rows = [["", *names]]
    row("Amplifier output", lambda k, s: EXTRA[k]["amp_out"])
    row("Mode and gain", lambda k, s: EXTRA[k]["amp_mode"])
    row("Input for full output", lambda k, s: f"{s.amp_input_full_v:g} V rms")
    row("Drive clip (peak)", lambda k, s: f"{s.max_drive_v:g} V")
    row("Drive abort (rms)", lambda k, s: f"{s.max_drive_rms_v:g} V")
    row("Usable with USB-4431 (±3.5 V AO)", lambda k, s: "yes" if s.max_drive_v <= 3.5 else "no")
    story += [KeepTogether([P("Amplifier and drive limits", h2), table(rows, [w0] + [wc] * 4),
              P("Drive limits are the controller's limits at the amplifier input, set in each shaker "
                "file. Amplifier gain settings marked 'assumed' must match the real knob "
                "positions.", small)])]

    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "payload.png"
        chart(shakers, png)
        img = Image(str(png), width=170 * mm, height=170 * mm * 3.6 / 7.0)
        story += [KeepTogether([
            P("Payload limits", h2),
            P("Maximum payload (accelerometer + fixture + specimen) for flat random profiles "
              "from 20 Hz to f_max at 0.01 g<super>2</super>/Hz, mounted vertically. Wider "
              "bands need more rms acceleration and therefore more force; the 4801/4812 curve is "
              "flat at low f_max because its stroke, not its force, limits the payload there.",
              body), Spacer(1, 4), img])]

        for psd in PSDS:
            stroke = stroke_pp_mm(psd, 10000)
            rows = [[f"f_max ({fmt_f(F_LO)} start)", *names]]
            for f in F_MAXS:
                cells = []
                for k in KEYS:
                    s = shakers[k]
                    if f > s.f_max_hz:
                        cells.append("-")
                        continue
                    half = EXTRA[k]["half_mm"] or s.displacement_pp_mm / 2
                    m, by_stroke, a = payload_limit(s, half, psd, f)
                    cells.append(f"{m:.2f} kg" + ("†" if by_stroke else ""))
                a = sqrt(psd * (f - F_LO))
                rows.append([f"{fmt_f(f)} ({a:.1f} g rms)", *cells])
            story += [KeepTogether([
                P(f"Flat {psd:g} g<super>2</super>/Hz: stroke {stroke:.2f} mm peak-peak (3σ)",
                  ParagraphStyle("h3", parent=h2, fontSize=9.5, spaceBefore=8)),
                table(rows, [w0] + [wc] * 4)])]
        story += [Spacer(1, 3), P("Unmarked values are limited by force: payload ≤ F<sub>random rms</sub> / "
                    "(a<sub>rms</sub> × g) - moving mass. † Limited by stroke on a "
                    "vertical shaker: static sag plus half the vibration stroke must fit in the "
                    "half-stroke; mounted horizontally these values rise to the force limit. "
                    "The stroke barely depends on f_max but grows steeply for a lower start "
                    "frequency (about 2.8× at 10 Hz). A heavy payload also lowers the "
                    "armature resonance, which this force check does not cover.", small)]

        story += [P("Notes and caveats", h2)]
        for text in (
            "The B&amp;K values come from old instruction manuals and a product datasheet; the "
            "manuals give no random ratings, so those are derived from the sine ratings.",
            "Amplifier gains for the B&amp;K systems are assumptions (2718 at 20 dB, 2707 in "
            "current mode with the gain fully clockwise). Different settings change the input "
            "for full output and the drive limits.",
            "The 4809 can technically run on the 2707, but the 2707's slow rms current trip "
            "(50 s or 2.5 s averaging) protects the 4809 coil much less well than the 2718's "
            "instantaneous current limiter; keep the 4809 on the 2718.",
            "Only the simulator and the pre-flight check have been run for the B&amp;K setups; "
            "start real tests at a low level (for example --level -12).",
        ):
            story.append(P("• " + text, body))

        doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                                topMargin=16 * mm, bottomMargin=16 * mm,
                                title="Shaker comparison", author="vcactl")
        doc.build(story)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=Path("docs/shaker_comparison.pdf"))
    args = ap.parse_args()
    build(args.out)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
