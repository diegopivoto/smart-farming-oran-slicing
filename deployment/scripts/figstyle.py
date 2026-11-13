"""Shared look of the paper figures: palette, typography, readable metric names, the service SLA line and a standard
header (title, how to read it) and footer (requirements, data source) placed outside the plot area.

Palette: the validated reference instance of the dataviz method (blue sequential ramp, categorical blue/orange/aqua,
blue<->red diverging pair around a neutral gray, light surface #fcfcfb).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId

SURFACE, INK, INK_2, INK_3, GRID, HIGHLIGHT = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1", "#eaf2fc"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
RED_TEXT = "#c4302f"
RAMP = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf",
        "#1c5cab", "#184f95", "#104281", "#0d366b"]
ORDINAL = ["#f0efec", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]  # 0..4 passes; 1+ clear 2:1 on the surface
DIVERGING = ["#a8282a", "#e34948", "#f2a39e", "#f0efec", "#9ec5f4", "#3987e5", "#104281"]  # misses <- target -> meets

SERVICE_NAMES = {"drone": "Drone video", "crop": "Crop imagery", "weather": "Weather telemetry"}
# metric -> (readable name, short name, display unit, divide raw value by)
METRICS: dict[str, tuple[str, str, str, float]] = {
    "goodput_p10": ("Goodput, 10th percentile of 1 s windows", "Goodput p10", "Mbit/s", 1e6),
    "goodput": ("Goodput", "Goodput", "Mbit/s", 1e6),
    "packet_loss_ratio": ("Packet loss", "Packet loss", "%", 0.01),
    "jitter_p95": ("Jitter, 95th percentile", "Jitter p95", "ms", 1e-3),
    "max_receive_gap": ("Longest gap between received packets", "Longest gap", "ms", 1e-3),
    "pdr": ("Packet delivery ratio", "Delivery ratio", "%", 0.01),
    "aoi_p95": ("Age of information, 95th percentile", "AoI p95", "s", 1.0),
    "max_freshness_gap": ("Longest freshness gap", "Freshness gap", "s", 1.0),
    "object_transfer_time": ("Worst 25 MB object transfer time", "Object time", "s", 1.0),
    "sha256_valid_ratio": ("Objects with a valid SHA-256", "Intact objects", "%", 0.01),
    "retransmitted_bytes_ratio": ("TCP bytes retransmitted", "TCP retrans.", "%", 0.01),
    "accepted_corrupt_objects": ("Corrupt objects accepted", "Corrupt objects", "count", 1.0),
}
OPS = {"ge": "≥", "le": "≤", "eq": "="}


def apply() -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "font.family": ["Noto Sans", "DejaVu Sans"], "font.size": 9, "text.color": INK, "axes.labelcolor": INK,
        "axes.edgecolor": INK_3, "axes.linewidth": 0.8, "axes.titlesize": 9.5, "axes.titleweight": "bold",
        "axes.titlecolor": INK, "axes.titlelocation": "left", "axes.titlepad": 8, "axes.facecolor": SURFACE,
        "axes.spines.top": False, "axes.spines.right": False, "xtick.color": INK_2, "ytick.color": INK_2,
        "xtick.labelsize": 8, "ytick.labelsize": 8, "xtick.major.size": 0, "ytick.major.size": 0,
        "legend.frameon": False, "legend.fontsize": 8, "figure.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "grid.color": GRID, "grid.linewidth": 0.8, "hatch.color": "#d9d8d3", "hatch.linewidth": 0.6,
    })


def metric(name: str) -> tuple[str, str, str, float]:
    return METRICS.get(name, (name.replace("_", " "), name.replace("_", " "), "", 1.0))


def display(name: str, raw: float) -> float:
    return raw / metric(name)[3]


def fmt(name: str, raw: float | None) -> str:
    if raw is None:
        return "–"
    v = display(name, raw)
    return f"{v:.2g}" if abs(v) < 1 else (f"{v:.3g}" if abs(v) < 100 else f"{v:.0f}")


def unit_suffix(name: str) -> str:
    unit = metric(name)[2]
    return "" if unit in ("", "count") else (unit if unit == "%" else f" {unit}")


def target(m: Any, short: bool = True) -> str:
    """'Goodput p10 ≥ 38 Mbit/s' (short) or the readable name."""

    label = metric(m.name)[1 if short else 0]
    return f"{label} {OPS[m.operator]} {display(m.name, m.threshold):g}{unit_suffix(m.name)}"


def threshold_text(m: Any) -> str:
    return f"{OPS[m.operator]} {display(m.name, m.threshold):g}{unit_suffix(m.name)}"


def meets(m: Any, raw: float | None) -> bool:
    return raw is not None and {"ge": raw >= m.threshold, "le": raw <= m.threshold, "eq": raw == m.threshold}[m.operator]


def sla_line(service: ServiceId) -> str:
    profile = SERVICE_CATALOG[service]
    rate = profile.offered_rate_bps
    load = f"{rate / 1e6:g} Mbit/s" if rate >= 1e6 else f"{rate / 1e3:g} kbit/s"
    return (f"{SERVICE_NAMES[service.value]} SLA (offered load {load}, slice SD {profile.snssai.sd}): "
            + "  ·  ".join(target(m) for m in profile.sla))


def action_text(ul_mcs: Any, ul_rb: Any, dl_prb: int) -> str:
    ul = "UL link adaptation" if ul_mcs in (None, 0) else f"UL MCS {ul_mcs} · {ul_rb} RB"
    return f"{ul}  |  DL {dl_prb} PRB"


def wrap(fig: Any, renderer: Any, text: str, width_in: float, **style: Any) -> list[str]:
    """Word wrap of `text` to lines whose rendered width (inches) fits `width_in`, balanced: when it takes several
    lines, the narrowest width giving the same line count is used, so no line is left with one or two words."""

    import re

    probe = fig.text(0, 0, "", **style)
    tokens = re.split(r"(\s+)", text)

    def fits(s: str, limit: float) -> bool:
        probe.set_text(s)
        return probe.get_window_extent(renderer).width / fig.dpi <= limit

    def greedy(limit: float) -> list[str]:
        lines, current = [], ""
        for token in tokens:
            candidate = current + token
            if current.strip() and not token.isspace() and not fits(candidate, limit):
                lines.append(current.rstrip())
                current = token.lstrip()
            else:
                current = candidate
        if current.strip():
            lines.append(current.rstrip())
        return lines or [text]

    lines = greedy(width_in)
    if len(lines) > 1:
        lo, hi = width_in / len(lines), width_in
        for _ in range(12):
            mid = (lo + hi) / 2
            lo, hi = (lo, mid) if len(greedy(mid)) <= len(lines) else (mid, hi)
        lines = greedy(hi)
    probe.remove()
    return lines


def finish(fig: Any, path: Path, title: str, subtitle: list[str] | None = None, footer: list[str] | None = None) -> str:
    """Title and reading guide above everything drawn, requirements/source below, then save.

    Every header and footer line is wrapped to the width of what is drawn, so a long note never widens the canvas
    (with bbox_inches="tight" it would push the plot to the left and leave an empty band on the right).

    With SF_FIG_VARIANTS_DIR set, two extra copies go there, named <parent dir>__<name>: `.body.png` (only what is
    drawn, no title, reading guide or footer: the slide copies) and `.paper.png` (without the "Data:" footer lines:
    the article copies, whose captions cite the data).
    """

    import os

    import matplotlib.pyplot as plt

    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    box = fig.get_tightbbox(renderer)  # inches
    height = fig.get_figheight()
    left = box.x0 / fig.get_figwidth()
    width = box.width
    sub_style = {"fontsize": 8.5, "color": INK_2}
    title_style = {"fontsize": 12, "fontweight": "bold", "color": INK}
    foot_style = {"fontsize": 7.5, "color": INK_3}
    header, data_lines = [], []
    y = box.y1 + 0.12
    for line in reversed([w for s in subtitle or [] for w in wrap(fig, renderer, s, width, **sub_style)]):
        header.append(fig.text(left, y / height, line, ha="left", va="bottom", **sub_style))
        y += 0.2
    y += 0.04
    for line in reversed(wrap(fig, renderer, title, width, **title_style)):
        header.append(fig.text(left, y / height, line, ha="left", va="bottom", **title_style))
        y += 0.25
    y = box.y0 - 0.12
    for entry in footer or []:
        for line in wrap(fig, renderer, entry, width, **foot_style):
            text = fig.text(left, y / height, line, ha="left", va="top", **foot_style)
            (data_lines if entry.startswith("Data") else header).append(text)
            y -= 0.18
    fig.savefig(path, dpi=220, bbox_inches="tight", pad_inches=0.25)
    variants = os.environ.get("SF_FIG_VARIANTS_DIR")
    if variants:
        stem = Path(variants) / f"{path.resolve().parent.parent.name}__{path.resolve().parent.name}__{path.stem}"
        for text in data_lines:
            text.set_visible(False)
        fig.savefig(f"{stem}.paper.png", dpi=220, bbox_inches="tight", pad_inches=0.25)
        for text in header:
            text.set_visible(False)
        fig.savefig(f"{stem}.body.png", dpi=220, bbox_inches="tight", pad_inches=0.12)
    plt.close(fig)
    return path.name
