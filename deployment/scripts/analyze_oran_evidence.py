#!/usr/bin/env python3
"""Joint (UN3GPP UL x OAI DL) end-to-end results and O-RAN control evidence of a calibration run.

Usage: analyze_oran_evidence.py <run> [--timeline-cells A-B]

The service metrics in cells.jsonl are end to end: generator on PC3 -> UN3GPP UL (BS, PC2) -> N3IWF -> UPF ->
OAI gNB DL -> receiver on the phone. This script shows them against both radio decisions at once and proves the
decisions were taken and applied through the O-RAN, from the raw exports in <run>/raw/:
  xapp-sf-control.jsonl.gz   E2 indications received by the xApp (UN3GPP BS readback + OAI quota outcomes)
  kpm-bridge-journal.jsonl.gz  OAI E2SM-KPM samples (per UE, per S-NSSAI subscription)
and the receiver's 1 s windows in experiments/traffic/collected/<traffic_run_id>.jsonl.

Writes <run>/analysis/: fig-ranking-joint-actions.png (top 15 + baselines) and -all.png, fig-joint-e2e-<metric>.png (one per SLA metric, plus sla_met), fig-dl-quota-effect.png, fig-oran-command-vs-observed.png,
fig-oran-control-timeline.png, oran-evidence.json and oran-evidence.md.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import json
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import figstyle as fs
from analyze_calibration import read_cells
from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId

PROJECT = Path(__file__).resolve().parents[2]
COLLECTED = PROJECT / "experiments/traffic/collected"


def ns(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1e9)


def fields(label: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in label.split(";") if "=" in part)


def load_e2(run: Path, sd: int) -> dict[str, Any]:
    """Time series and outcomes from the xApp's E2 indications (PC1 receive clock)."""

    ue: list[tuple[int, dict[str, str]]] = []  # UN3GPP BS per-UE readback
    quota: list[tuple[int, int]] = []  # OAI per-UE PRB quota in force for the service slice
    capped: list[tuple[int, int]] = []  # OAI scheduler counter: allocations capped by a quota
    outcomes: dict[tuple[str, str], dict[str, str]] = {}
    with gzip.open(run / "raw/xapp-sf-control.jsonl.gz", "rt") as handle:
        for line in handle:
            record = json.loads(line)
            t = record["received_at_ns"]
            for label in record.get("labels", []):
                f = fields(label)
                kind = f.get("kind")
                if kind == "un3gpp_ue":
                    ue.append((t, f))
                elif kind == "ue" and int(f.get("sd", "0"), 16) == sd:
                    quota.append((t, int(f["prb"])))
                elif kind == "counters" and "capped" in f:
                    capped.append((t, int(f["capped"])))
                elif kind in ("un3gpp_outcome", "outcome") and f.get("action_id", "0") != "0":
                    key = (record["node"], f["action_id"])  # final status wins (SENT -> CONFIRMED/CONSISTENT)
                    outcomes[key] = f | {"first_seen_ns": outcomes.get(key, {}).get("first_seen_ns", str(t))}
    return {"ue": ue, "quota": quota, "capped": capped, "outcomes": outcomes}


def load_kpm(run: Path, sd: int) -> list[tuple[int, dict[str, float]]]:
    samples = []
    with gzip.open(run / "raw/kpm-bridge-journal.jsonl.gz", "rt") as handle:
        for line in handle:
            if not line.startswith("{"):
                continue
            record = json.loads(line)
            values = {m["name"]: m["value"] for m in record.get("measurements", [])
                      if m.get("labels", {}).get("subscription_sd") == sd and m.get("value") is not None}
            if values:
                samples.append((record["collected_at_ns"], values))
    samples.sort(key=lambda s: s[0])
    return samples


def window(series: list[tuple[int, Any]], start: int, end: int) -> list[Any]:
    times = [t for t, _ in series]
    return [v for _, v in series[bisect.bisect_left(times, start):bisect.bisect_right(times, end)]]


def receiver_windows(run_id: str, kind: str = "receiver_window") -> list[dict[str, Any]]:
    path = COLLECTED / f"{run_id}.jsonl"
    if not path.exists():
        return []
    return [r for r in map(json.loads, path.read_text().splitlines()) if r.get("kind") == kind]


def per_cell_control(cells: list[dict[str, Any]], e2: dict[str, Any]) -> None:
    """Attach, per cell, the quota the gNB reported in force and the BS readback during the traffic window."""

    for c in cells:
        if not c.get("started_at") or not c.get("finished_at"):
            continue
        start, end = ns(c["started_at"]) + 8_000_000_000, ns(c["finished_at"]) - 6_000_000_000  # skip setup/flush
        if c.get("skipped"):  # rejected by the BS safety mask: ~1 s long, no traffic; read the whole cell
            start, end = ns(c["started_at"]), ns(c["finished_at"]) + 1_000_000_000
        q = window(e2["quota"], start, end)
        c["e2_dl_quota_applied_median"] = statistics.median(q) if q else None
        readback = window(e2["ue"], start, end)
        c["e2_override_ratio"] = (sum(f["override"] == "1" for f in readback) / len(readback)) if readback else None
        c["e2_mcs_cmd_median"] = statistics.median(int(f["mcs_cmd"]) for f in readback) if readback else None
        c["e2_rb_cmd_median"] = statistics.median(int(f["rb_cmd"]) for f in readback) if readback else None


def footer(service: ServiceId, cells: list[dict[str, Any]], run: Path) -> list[str]:
    valid = [c for c in cells if "error" not in c]
    passes = len({c["pass"] for c in valid})
    actions = len({(c["ul_mode"], c.get("ul_mcs"), c.get("ul_rb"), c["dl_prb"]) for c in valid})
    rejected = sum(bool(c.get("skipped")) for c in valid)
    masked = (f"; {rejected} of them rejected by the BS safety mask, not measured, hatched" if rejected else "")
    return [fs.sla_line(service),
            f"Data: calibration run {run.name} — {passes} passes × {actions} joint actions, 30 s of traffic each "
            f"({len(valid)} valid cells{masked}). Radio control and RAN telemetry over E2 (FlexRIC); service metrics at the phone."]


def action_groups(cells: list[dict[str, Any]]) -> dict[tuple[bool, int, int, int], list[dict[str, Any]]]:
    """(is link adaptation, UL MCS, UL RBs, DL PRB) -> valid cells."""

    groups: dict[tuple[bool, int, int, int], list[dict[str, Any]]] = {}
    for c in cells:
        if "error" not in c:
            key = (c["ul_mode"] != "override", c.get("ul_mcs") or 0, c.get("ul_rb") or 0, c["dl_prb"])
            groups.setdefault(key, []).append(c)
    return groups


def figure_ranking(cells: list[dict[str, Any]], service: ServiceId, out: Path, plt: Any, top: int | None,
                   foot: list[str]) -> str:
    """Single ranked table-chart of the joint actions: per requirement, passes meeting it; then the whole SLA; then
    medians. Order = the selection rule (most passes meeting the whole SLA, then lowest UL MCS, UL RBs, DL PRB)."""

    from matplotlib.patches import FancyBboxPatch, Rectangle

    metrics = SERVICE_CATALOG[service].sla
    rows = []
    for key, items in action_groups(cells).items():
        rows.append({"key": key, "n": len(items), "all": sum(bool(c.get("sla_met")) for c in items),
                     "per": [sum(fs.meets(m, c.get(f"svc_{m.name}")) for c in items) for m in metrics],
                     "med": [statistics.median(v) if (v := [c[f"svc_{m.name}"] for c in items
                                                            if c.get(f"svc_{m.name}") is not None]) else None
                             for m in metrics]})
    fixed = sorted((r for r in rows if not r["key"][0]), key=lambda r: (-r["all"] / r["n"], r["key"][1:]))
    refs = sorted((r for r in rows if r["key"][0]), key=lambda r: r["key"][3])
    shown = (fixed[:top] if top else fixed) + refs
    n_passes = max(r["n"] for r in rows)

    x_rank, x_mcs, x_rb, x_prb = 0.0, 1.1, 2.35, 3.6
    x_req = [5.0 + 1.3 * k for k in range(len(metrics))]
    x_all = x_req[-1] + 1.6
    x_med = [x_all + 1.55 + 1.25 * k for k in range(len(metrics))]
    gap = 1.1
    ys = [len(shown) - 1 - i + (gap if i < len(shown) - len(refs) else 0) for i in range(len(shown))]
    top_y = max(ys)
    fig, ax = plt.subplots(figsize=(12.6, 0.34 * (len(shown) + gap) + 2.35))

    for i, (r, y) in enumerate(zip(shown, ys, strict=True)):
        selected = i == 0 and r["all"] == r["n"]
        if selected:
            ax.add_patch(Rectangle((x_rank - 0.5, y - 0.48), x_med[-1] + 1.1, 0.96, facecolor=fs.HIGHLIGHT, lw=0))
        elif i < len(shown) - len(refs):
            ax.plot([x_rank - 0.5, x_med[-1] + 0.6], [y - 0.5, y - 0.5], color=fs.GRID, lw=0.6)
        la, mcs, rb, prb = r["key"]
        weight = "bold" if selected else "normal"
        ax.text(x_rank, y, "★" if selected else ("" if la else str(i + 1)), ha="center", va="center",
                fontsize=9 if selected else 8, color=fs.BLUE if selected else fs.INK_3, fontweight="bold")
        for x, v in ((x_mcs, "auto" if la else mcs), (x_rb, "auto" if la else rb), (x_prb, prb)):
            ax.text(x, y, str(v), ha="center", va="center", fontsize=9, color=fs.INK, fontweight=weight)
        for x, met in [*zip(x_req, r["per"], strict=True), (x_all, r["all"])]:
            level = round(4 * met / r["n"])
            whole = x == x_all
            ax.add_patch(FancyBboxPatch((x - 0.5, y - 0.33), 1.0, 0.66, boxstyle="round,pad=0,rounding_size=0.14",
                                        facecolor=fs.ORDINAL[level], edgecolor=fs.INK if whole and selected else "none",
                                        lw=1.2))
            ax.text(x, y, f"{met}/{r['n']}", ha="center", va="center", fontsize=8.5 if whole else 8,
                    color="#ffffff" if level >= 2 else fs.INK, fontweight="bold" if whole else "normal")
        for x, m, v in zip(x_med, metrics, r["med"], strict=True):
            ok = fs.meets(m, v)
            ax.text(x, y, fs.fmt(m.name, v), ha="center", va="center", fontsize=8.5,
                    color=fs.INK if ok else fs.RED_TEXT, fontweight=weight)

    head = top_y + 0.85

    def wrap(name: str) -> str:  # long metric names on two lines so adjacent columns never touch
        return name.replace(" ", "\n", 1) if len(name) > 9 else name

    groups = [("Joint radio action (O-RAN xApp)", x_mcs - 0.5, x_prb + 0.55),
              (f"Passes meeting each requirement (of {n_passes})", x_req[0] - 0.55, x_req[-1] + 0.55),
              ("Whole SLA", x_all - 0.6, x_all + 0.6),
              ("Median over passes", x_med[0] - 0.55, x_med[-1] + 0.55)]
    for name, a, b in groups:
        ax.text((a + b) / 2, head + 1.45, name, ha="center", va="bottom", fontsize=8.5, color=fs.INK, fontweight="bold")
        ax.plot([a, b], [head + 1.35, head + 1.35], color=fs.INK_3, lw=0.8)
    subheads = [(x_mcs, "UN3GPP UL\nMCS"), (x_rb, "UN3GPP UL\nRBs"), (x_prb, "OAI DL\nPRB quota"), (x_all, "all\nrequirements")]
    subheads += [(x, f"{wrap(fs.metric(m.name)[1])}\n{fs.threshold_text(m)}") for x, m in zip(x_req, metrics, strict=True)]
    subheads += [(x, f"{wrap(fs.metric(m.name)[1])}\n({fs.metric(m.name)[2]})") for x, m in zip(x_med, metrics, strict=True)]
    for x, text in subheads:
        ax.text(x, head, text, ha="center", va="bottom", fontsize=7.5, color=fs.INK_2, linespacing=1.3)
    ax.text(x_rank, head, "rank", ha="center", va="bottom", fontsize=7.5, color=fs.INK_2)
    if refs:
        line_y = len(refs) - 0.5 + gap / 2
        ax.text(x_mcs - 0.5, line_y + 0.02, "Reference: UN3GPP BS link adaptation (no O-RAN override of UL MCS/RBs)",
                ha="left", va="center", fontsize=7.5, color=fs.INK_2, style="italic",
                bbox={"facecolor": fs.SURFACE, "edgecolor": "none", "pad": 1.5})
        ax.plot([x_rank - 0.5, x_med[-1] + 0.6], [line_y, line_y], color=fs.INK_3, lw=0.6, ls=(0, (2, 2)), zorder=0)

    key_y = -1.25
    ax.text(x_rank - 0.5, key_y, f"Passes (of {n_passes}) meeting the requirement:", ha="left", va="center", fontsize=7.5,
            color=fs.INK_2)
    for level in range(5):
        x = x_req[0] - 0.2 + 0.62 * level
        ax.add_patch(FancyBboxPatch((x - 0.25, key_y - 0.22), 0.5, 0.44, boxstyle="round,pad=0,rounding_size=0.1",
                                    facecolor=fs.ORDINAL[level], lw=0))
        ax.text(x, key_y, str(level), ha="center", va="center", fontsize=7, color="#ffffff" if level >= 2 else fs.INK)
    ax.text(x_req[0] + 3.0, key_y, "★ selected: most passes meeting the whole SLA, then lowest UL MCS, UL RBs, DL PRB.\n"
            "Red median = misses the target.", ha="left", va="center", fontsize=7.5, color=fs.INK_2, linespacing=1.4)
    ax.set_xlim(x_rank - 0.6, x_med[-1] + 0.7)
    ax.set_ylim(key_y - 0.5, head + 2.0)
    ax.axis("off")
    name = fs.SERVICE_NAMES[service.value]
    scope = (f"the {top} best of {len(fixed)} fixed actions" if top and top < len(fixed) else f"all {len(fixed)} fixed actions")
    return fs.finish(
        fig, out / ("fig-ranking-joint-actions.png" if top else "fig-ranking-joint-actions-all.png"),
        f"{name}: joint radio actions ranked by end-to-end SLA compliance",
        ["Each row is one joint action: UN3GPP uplink MCS and RBs at the BS plus the OAI downlink PRB quota of the slice.",
         f"Cells count the passes in which the metric, measured end to end at the phone, met the requirement; {scope}, "
         "then the link-adaptation references."],
        foot)


def figure_joint(cells: list[dict[str, Any]], service: ServiceId, out: Path, plt: Any, foot: list[str]) -> list[str]:
    """One heatmap per end-to-end metric (and the whole SLA): rows = UN3GPP UL MCS (top row: link adaptation),
    columns = UN3GPP UL RBs grouped by OAI DL PRB quota. Colour = margin to the target (blue meets, red misses)."""

    from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm
    from matplotlib.patches import Rectangle

    groups = action_groups(cells)
    fixed = [k for k in groups if not k[0]]
    mcs = sorted({k[1] for k in fixed})
    rbs = sorted({k[2] for k in fixed})
    prbs = sorted({k[3] for k in groups})
    has_la = any(k[0] for k in groups)
    group_gap = 0.45
    col_x = {(prb, rb): g * (len(rbs) + group_gap) + j for g, prb in enumerate(prbs) for j, rb in enumerate(rbs)}
    row_y = {m: i for i, m in enumerate(mcs)}
    la_y = len(mcs) + 0.35
    diverging = LinearSegmentedColormap.from_list("margin", fs.DIVERGING)
    name = fs.SERVICE_NAMES[service.value]
    n_passes = max(len(v) for v in groups.values())
    written = []
    for metric in [None, *SERVICE_CATALOG[service].sla]:
        values: dict[tuple[bool, int, int, int], float] = {}
        for key, items in groups.items():
            if metric is None:
                values[key] = sum(bool(c.get("sla_met")) for c in items)
            else:
                v = [c[f"svc_{metric.name}"] for c in items if c.get(f"svc_{metric.name}") is not None]
                if v:
                    values[key] = statistics.median(v)
        if metric is not None:
            margin = {k: (v - metric.threshold if metric.operator == "ge" else metric.threshold - v) for k, v in values.items()}
            lo, hi = min(margin.values()), max(margin.values())
            norm = TwoSlopeNorm(vcenter=0, vmin=min(lo, -1e-12 - abs(hi) * 0.05), vmax=max(hi, 1e-12 + abs(lo) * 0.05))
        width = len(prbs) * (len(rbs) + group_gap)
        fig, ax = plt.subplots(figsize=(0.82 * width + 2.6, 0.36 * (len(mcs) + 1.4) + 1.4))
        for key, v in values.items():
            la, m, rb, prb = key
            if la:
                x0, y0, w = col_x[(prb, rbs[0])], la_y, len(rbs)
            else:
                x0, y0, w = col_x[(prb, rb)], row_y[m], 1
            if metric is None:
                level = round(4 * v / len(groups[key]))
                color, text, bold = fs.ORDINAL[level], f"{int(v)}/{len(groups[key])}", v == len(groups[key])
                dark = level >= 2
            else:
                color = diverging(norm(margin[key]))
                text, bold = fs.fmt(metric.name, v), fs.meets(metric, v)
                dark = abs(norm(margin[key]) - 0.5) > 0.3
            ax.add_patch(Rectangle((x0 - 0.47, y0 - 0.45), w - 0.06, 0.9, facecolor=color, lw=0))
            ax.text(x0 + (w - 1) / 2, y0, text, ha="center", va="center", fontsize=7.5,
                    color="#ffffff" if dark else fs.INK, fontweight="bold" if bold else "normal")
        for (prb, rb), x in col_x.items():
            for m, y in row_y.items():
                if (False, m, rb, prb) not in values:
                    ax.add_patch(Rectangle((x - 0.47, y - 0.45), 0.94, 0.9, facecolor=fs.SURFACE, hatch="/////",
                                           edgecolor=fs.GRID, lw=0))
        for g, prb in enumerate(prbs):
            a = col_x[(prb, rbs[0])] - 0.47
            b = col_x[(prb, rbs[-1])] + 0.47
            top = (la_y if has_la else len(mcs) - 1) + 0.75
            ax.text((a + b) / 2, top + 0.35, f"OAI DL quota  {prb} PRB", ha="center", va="bottom", fontsize=8.5,
                    fontweight="bold", color=fs.INK)
            ax.plot([a, b], [top + 0.22, top + 0.22], color=fs.INK_3, lw=0.8)
        ax.set_xticks(list(col_x.values()), [str(rb) for (_, rb) in col_x])
        ticks = [row_y[m] for m in mcs] + ([la_y] if has_la else [])
        ax.set_yticks(ticks, [str(m) for m in mcs] + (["link adapt."] if has_la else []))
        ax.set_xlabel("UN3GPP UL resource blocks (RBs)")
        ax.set_ylabel("UN3GPP UL MCS")
        ax.set_xlim(-0.6, max(col_x.values()) + 0.6)
        ax.set_ylim(-0.6, (la_y if has_la else len(mcs) - 1) + 1.5)
        for side in ("left", "bottom"):
            ax.spines[side].set_visible(False)
        if metric is None:
            fname, title = "sla_met", f"{name}: passes meeting the whole SLA per joint radio action"
            how = (f"Value: passes (of {n_passes}) in which every requirement was met end to end at the phone; "
                   "bold = all passes. Hatched: outside the admissible action space.")
            for level in range(5):
                x = max(col_x.values()) + 1.2
                ax.add_patch(Rectangle((x - 0.3, level * 0.75 - 0.3), 0.6, 0.6, facecolor=fs.ORDINAL[level], lw=0,
                                       clip_on=False))
                ax.text(x + 0.45, level * 0.75, f"{level}/{n_passes}", va="center", fontsize=7.5, color=fs.INK_2)
        else:
            fname = metric.name
            title = f"{name}: {fs.metric(metric.name)[0].lower()} per joint radio action"
            how = (f"Value: median over {n_passes} passes, end to end at the phone. Target {fs.threshold_text(metric)}: "
                   "blue meets it, red misses it, darker = farther from the target (bold = meets). Hatched: outside "
                   "the admissible action space.")
            from matplotlib.cm import ScalarMappable

            bar = fig.colorbar(ScalarMappable(norm=norm, cmap=diverging), ax=ax, fraction=0.025, pad=0.02, aspect=30)
            back = (lambda d: metric.threshold + d) if metric.operator == "ge" else (lambda d: metric.threshold - d)
            ticks_m = [norm.vmin, 0.0, norm.vmax]
            bar.set_ticks(ticks_m, labels=[fs.fmt(metric.name, back(t)) + ("  target" if t == 0 else "") for t in ticks_m])
            bar.ax.tick_params(labelsize=7, colors=fs.INK_2)
            bar.outline.set_visible(False)
            bar.set_label(f"{fs.metric(metric.name)[1]} ({fs.metric(metric.name)[2]})", fontsize=7.5, color=fs.INK_2)
        written.append(fs.finish(
            fig, out / f"fig-joint-e2e-{fname}.png", title,
            ["Rows: UN3GPP uplink MCS at the BS (top: BS link adaptation, no override). Columns: UN3GPP uplink RBs, "
             "grouped by the OAI downlink PRB quota of the slice.", how],
            foot))
    return written


def strip(ax: Any, groups: list[list[list[float]]], labels: list[str], fmt: Any, colors: list[str]) -> None:
    """groups[k][r] = values of x category k in regime r; regimes are dodged side by side, bar and label = median."""

    width = 0.78 / len(colors)
    for k, regimes in enumerate(groups):
        for r, v in enumerate(regimes):
            if not v:
                continue
            x = k + (r - (len(colors) - 1) / 2) * width
            spread = [x + 0.55 * width * ((i * 0.618) % 1 - 0.5) for i in range(len(v))]
            ax.scatter(spread, v, s=7, color=colors[r], alpha=0.4, edgecolors="none", zorder=2)
            med = statistics.median(v)
            ax.plot([x - 0.36 * width, x + 0.36 * width], [med, med], color=fs.INK, lw=2, solid_capstyle="round",
                    zorder=3)
            ax.annotate(fmt(med), (x, med), xytext=(0, 3), textcoords="offset points", ha="center", va="bottom",
                        fontsize=6.5, color=fs.INK, fontweight="bold",
                        bbox={"facecolor": fs.SURFACE, "edgecolor": "none", "pad": 0.3, "alpha": 0.8}, zorder=4)
    ax.set_xticks(range(len(labels)), labels)
    ax.set_xlim(-0.5, len(labels) - 0.5)
    ax.grid(axis="y")
    ax.set_axisbelow(True)


def ul_regimes(valid: list[dict[str, Any]]) -> tuple[list[str], list[str], Any]:
    """Split cells by what the UN3GPP uplink allowed, so the uplink cannot hide the downlink effect: fixed UL actions
    that met the whole SLA in at least one of their cells (any DL quota, any pass), fixed UL actions that never did,
    and the BS link adaptation."""

    met: Counter[tuple[Any, Any]] = Counter()
    for c in valid:
        met[(c["ul_mcs"], c["ul_rb"])] += c.get("sla_met") is True
    feasible = sorted({(c["ul_mcs"], c["ul_rb"]) for c in valid if c["ul_mode"] == "override" and met[(c["ul_mcs"], c["ul_rb"])]})
    never = sorted({(c["ul_mcs"], c["ul_rb"]) for c in valid if c["ul_mode"] == "override"} - set(feasible))
    names = [f"fixed UL actions that met the whole SLA at least once ({len(feasible)})",
             f"fixed UL actions that never met it ({len(never)}: UL MCS " + ", ".join(f"{m}/{rb}" for m, rb in never[:3])
             + (", …" if len(never) > 3 else "") + ")",
             "UL link adaptation of the BS"]

    colors = [fs.BLUE, fs.ORANGE, fs.AQUA]
    if not never:  # every fixed UL action met the SLA at least once: no empty regime in the legend
        del names[1], colors[1]

    def regime(c: dict[str, Any]) -> int:
        if c["ul_mode"] != "override":
            return len(colors) - 1
        return 0 if (c["ul_mcs"], c["ul_rb"]) in feasible else 1

    return names, colors, regime


def figure_dl_effect(cells: list[dict[str, Any]], service: ServiceId, out: Path, plt: Any, foot: list[str]) -> str:
    valid = [c for c in cells if "error" not in c]
    prbs = sorted({c["dl_prb"] for c in valid})
    labels = [f"{p} PRB" for p in prbs]
    names, colors, regime = ul_regimes(valid)

    def groups(value: Any) -> list[list[list[float]]]:
        return [[[value(c) for c in valid if c["dl_prb"] == p and regime(c) == r and value(c) is not None]
                 for r in range(len(colors))] for p in prbs]

    ran = [("kpm_rlc_delay_dl_ms", "RLC queueing delay, DL", "ms"), ("kpm_prb_tot_dl_pct", "PRB usage, DL", "%"),
           ("kpm_ue_thp_dl_mbps", "UE throughput, DL", "Mbit/s")]
    metrics = SERVICE_CATALOG[service].sla
    cols = max(len(ran), len(metrics))
    fig, axes = plt.subplots(2, cols, figsize=(3.6 * cols, 6.0), squeeze=False)
    for k, (field, title, unit) in enumerate(ran):
        ax = axes[0][k]
        strip(ax, groups(lambda c, f=field: c.get(f)), labels, lambda v: f"{v:.3g}", colors)
        ax.set_title(f"{title} ({unit})")
    for k in range(len(ran), cols):
        axes[0][k].set_visible(False)
    for k, m in enumerate(metrics):
        ax = axes[1][k]
        key = f"svc_{m.name}"
        strip(ax, groups(lambda c, f=key, n=m.name: None if c.get(f) is None else fs.display(n, c[f])), labels,
              lambda v: f"{v:.3g}", colors)
        t = fs.display(m.name, m.threshold)
        ax.axhline(t, color=fs.RED_TEXT, lw=1, ls=(0, (4, 3)), zorder=1)
        low, high = ax.get_ylim()
        if m.operator == "eq":  # equality targets (100 % intact, 0 corrupt): a physical axis, nothing to shade
            top = 105.0 if fs.metric(m.name)[2] == "%" else max(high, 1.0) * 1.2
            low, high = -0.04 * top, top
            if fs.metric(m.name)[2] == "count":
                ax.yaxis.set_major_locator(plt.MaxNLocator(integer=True))
        elif m.operator == "ge":
            ax.axhspan(low, t, color="#fbeaea", lw=0, zorder=0)
        else:
            ax.axhspan(t, high, color="#fbeaea", lw=0, zorder=0)
        ax.set_ylim(low, high)
        ax.text(1, 1.02, f"target {fs.threshold_text(m)}", transform=ax.transAxes, ha="right", va="bottom",
                fontsize=8, color=fs.RED_TEXT)
        ax.set_title(f"{fs.metric(m.name)[1]} ({fs.metric(m.name)[2]})")
    for k in range(len(metrics), cols):
        axes[1][k].set_visible(False)
    for row, text in enumerate(["RAN side: OAI gNB, KPM of the service slice", "End to end: measured at the phone"]):
        axes[row][0].annotate(text, (0, 1.2), xycoords="axes fraction", fontsize=9, fontweight="bold", color=fs.BLUE)
    handles = [plt.Line2D([], [], ls="", marker="o", ms=5, color=c, alpha=0.8) for c in colors]
    fig.tight_layout(h_pad=3.2, w_pad=2.0)
    fig.legend(handles, names, loc="lower left", bbox_to_anchor=(axes[0][0].get_position().x0, 1.0), ncol=1,
               title="UN3GPP uplink regime of the cell", title_fontsize=8, alignment="left", handletextpad=0.3)
    return fs.finish(
        fig, out / "fig-dl-quota-effect.png", f"{fs.SERVICE_NAMES[service.value]}: what the OAI downlink PRB quota changes",
        ["Every valid cell, grouped by the downlink PRB quota the O-RAN xApp set on the gNB for the service slice and "
         "split by what the UN3GPP uplink allowed in that cell; bar and label = median.",
         "The same uplink actions repeat under every quota, so comparing columns of one colour isolates the downlink "
         "quota. Bottom row: the SLA target is dashed and the region that misses it is shaded."],
        foot)


def figure_command_vs_observed(cells: list[dict[str, Any]], service: ServiceId, out: Path, plt: Any,
                               foot: list[str]) -> str:
    valid = [c for c in cells if "error" not in c and c["ul_mode"] == "override"]
    fig, axes = plt.subplots(1, 4, figsize=(15.6, 4.3))
    sets = [("ul_mcs", "un3gpp_ul_mcs_observed", "① UN3GPP uplink MCS", "commanded by the O-RAN xApp (E2 control)",
             "used by the UE over the air\n(inferred by the BS from received PDUs)"),
            ("ul_rb", "e2_rb_cmd_median", "② UN3GPP uplink RB reservation", "commanded by the O-RAN xApp (E2 control)",
             "reservation in force at the BS MAC\n(E2 readback)"),
            ("ul_rb", "un3gpp_ul_rb_observed_max", "③ UN3GPP uplink RBs actually filled", "RB reservation (ceiling)",
             "largest RB count of a UE data PDU\n(per cell, E2 readback)"),
            ("dl_prb", "e2_dl_quota_applied_median", "④ OAI downlink PRB quota", "commanded by the O-RAN xApp (E2 control)",
             "quota in force at the gNB scheduler\n(E2 control outcome)")]
    every = [c for c in cells if "error" not in c and not c.get("skipped")]  # the DL quota is commanded in LA cells too
    for ax, (cmd, obs, title, xlabel, ylabel) in zip(axes, sets, strict=True):
        pts = Counter((c[cmd], round(c[obs])) for c in (every if cmd == "dl_prb" else valid) if c.get(obs) is not None)
        if not pts:
            continue
        values = [v for p in pts for v in p]
        pad = max(2.0, 0.12 * (max(values) - min(values)))
        span = [min(values) - pad, max(values) + pad]
        ax.plot(span, span, color=fs.INK_3, lw=1, ls=(0, (4, 3)), zorder=1)
        on = {p: n for p, n in pts.items() if abs(p[0] - p[1]) < 0.5}
        below = {p: n for p, n in pts.items() if p not in on}
        for group, color in ((on, fs.BLUE), (below, fs.ORANGE)):
            if group:
                ax.scatter([p[0] for p in group], [p[1] for p in group], s=[24 + 9 * n for n in group.values()],
                           color=color, alpha=0.75, edgecolors=fs.SURFACE, linewidths=1.2, zorder=3)
        total = sum(pts.values())
        match = sum(on.values())
        if obs == "un3gpp_ul_rb_observed_max":
            note = f"{match} of {total} cells filled the whole reservation"
            if below:
                off = [c for c in valid if c.get(obs) is not None and round(c[obs]) != c[cmd]]
                mcs = sorted({c["ul_mcs"] for c in off})
                mac = statistics.median(c["un3gpp_mac_mbps"] for c in off if c.get("un3gpp_mac_mbps") is not None)
                note += (f"\n{len(off)} below (orange), all at UL MCS {mcs[0]}–{mcs[-1]}: the UE sizes each\n"
                         f"PDU to its buffer (a light load, or a high MCS, needs fewer RBs)\n"
                         f"(BS MAC rate in these cells: median {mac:.3g} Mbit/s)")
        else:
            note = f"{match} of {total} cells: applied = commanded"
        ax.text(0.03, 0.97, note, transform=ax.transAxes, ha="left", va="top", fontsize=7.5, color=fs.INK,
                bbox={"facecolor": fs.HIGHLIGHT, "edgecolor": "none", "boxstyle": "round,pad=0.4"}, zorder=5)
        ax.set_xlim(span)
        ax.set_ylim(span)
        ax.set_title(title)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.grid(True)
        ax.set_axisbelow(True)
    fig.tight_layout(w_pad=2.5)
    return fs.finish(
        fig, out / "fig-oran-command-vs-observed.png",
        f"{fs.SERVICE_NAMES[service.value]}: radio decisions sent by the O-RAN xApp vs applied by each RAN node",
        ["①–③: every cell with a fixed UL action; ④: every measured cell (link adaptation included). One bubble per "
         "(x, y) pair, area = number of cells. On the dashed line the value read back equals the value commanded.",
         "①②④ prove that each RAN node applied the command. ③ is how much of the UL reservation the UE needed: it is a "
         "ceiling, and the UE MAC only fills the RBs its buffer requires."],
        foot)


def figure_timeline(cells: list[dict[str, Any]], e2: dict[str, Any], kpm: list[tuple[int, dict[str, float]]],
                    service: ServiceId, span: tuple[int, int], out: Path, plt: Any, foot: list[str]) -> str:
    from matplotlib.ticker import MaxNLocator

    chosen = sorted((c for c in cells if span[0] <= c["index"] <= span[1] and "error" not in c), key=lambda c: c["index"])
    t0, t1 = ns(chosen[0]["started_at"]) - 5_000_000_000, ns(chosen[-1]["finished_at"]) + 5_000_000_000

    def minutes(t: int) -> float:
        return (t - t0) / 60e9

    fig, axes = plt.subplots(5, 1, figsize=(12, 9.6), sharex=True, gridspec_kw={"hspace": 0.38})
    readback = [(t, f) for t, f in e2["ue"] if t0 <= t <= t1]
    xs = [minutes(t) for t, _ in readback]
    for ax, key, title in ((axes[0], "mcs", "UN3GPP uplink MCS"), (axes[1], "rb", "UN3GPP uplink RBs")):
        cmd = [int(f[f"{key}_cmd"]) if f["override"] == "1" else float("nan") for _, f in readback]
        ax.step(xs, cmd, where="post", color=fs.BLUE, lw=2.4, label="commanded by the xApp (override active)")
        ax.step(xs, [int(f[f"{key}_obs"]) for _, f in readback], where="post", color=fs.ORANGE, lw=1.1,
                label="applied in the BS MAC (E2 readback)")
        ax.set_title(title)
        ax.yaxis.set_major_locator(MaxNLocator(integer=True, nbins=5))
    axes[0].set_title("UN3GPP uplink MCS", pad=52)
    quota = [(t, v) for t, v in e2["quota"] if t0 <= t <= t1]
    axes[2].step([minutes(t) for t, _ in quota], [v for _, v in quota], where="post", color=fs.BLUE, lw=2.4,
                 label="quota in force at the gNB (E2 control outcome)")
    axes[2].set_title("OAI downlink PRB quota of the slice")
    k = [(t, v) for t, v in kpm if t0 <= t <= t1]
    axes[3].plot([minutes(t) for t, _ in k], [v.get("RRU.PrbTotDl", float("nan")) for _, v in k], color=fs.AQUA, lw=1.3,
                 label="RRU.PrbTotDl (KPM)")
    axes[3].set_title("OAI downlink PRB usage (%)")
    axes[4].plot([minutes(t) for t, _ in k], [v.get("DRB.UEThpDl", 0) / 1e3 for _, v in k], color=fs.ORANGE, lw=1.1,
                 label="gNB DL throughput, DRB.UEThpDl (KPM)")
    windows = [w for c in chosen if "traffic_run_id" in c for w in receiver_windows(c["traffic_run_id"])]
    if windows:
        axes[4].scatter([minutes(w["collected_at_ns"]) for w in windows], [w["goodput_bps"] / 1e6 for w in windows],
                        s=9, color=fs.BLUE, zorder=3, label="end-to-end goodput at the phone (1 s windows)")
    # object services (crop): one segment per object received, spanning its transfer, at its mean goodput
    objects = [o for c in chosen if "traffic_run_id" in c for o in receiver_windows(c["traffic_run_id"], "receiver_object")]
    for n, o in enumerate(objects):
        end = minutes(o["collected_at_ns"])
        axes[4].plot([end - o["object_transfer_time_s"] / 60, end], [o["goodput_bps"] / 1e6] * 2, color=fs.BLUE, lw=2.6,
                     solid_capstyle="butt", zorder=3,
                     label="end-to-end goodput at the phone (one segment per 25 MB object)" if n == 0 else None)
    axes[4].set_title("Throughput (Mbit/s)")
    axes[4].set_xlabel("time since the first cell (minutes)")
    for n, c in enumerate(chosen):
        a, b = minutes(ns(c["started_at"])), minutes(ns(c["finished_at"]))
        for ax in axes:
            ax.axvspan(a, b, color=fs.HIGHLIGHT if n % 2 == 0 else "#f4f3f0", lw=0, zorder=0)
        ul = "UL link adapt." if c["ul_mode"] != "override" else f"UL {c['ul_mcs']} / {c['ul_rb']} RB"
        if c.get("skipped"):  # rejected by the BS safety mask: ~1 s, no traffic; a mark instead of a label
            axes[0].annotate("✕", ((a + b) / 2, 1.04), xycoords=("data", "axes fraction"), ha="center", va="bottom",
                             fontsize=9, color=fs.RED_TEXT, fontweight="bold")
            continue
        axes[0].annotate(f"cell {c['index']}\n{ul}\nDL {c['dl_prb']} PRB", ((a + b) / 2, 1.04),
                         xycoords=("data", "axes fraction"), ha="center", va="bottom", fontsize=7, color=fs.INK_2,
                         linespacing=1.25)
    for ax in axes:
        ax.legend(loc="upper left", bbox_to_anchor=(1.005, 1.0), fontsize=7.5, handlelength=1.6)
        ax.grid(axis="y")
        ax.set_axisbelow(True)
        ax.margins(x=0)
    return fs.finish(
        fig, out / "fig-oran-control-timeline.png",
        f"{fs.SERVICE_NAMES[service.value]}: O-RAN control in action, cells {span[0]}–{span[1]}",
        ["Each shaded band is one cell (30 s of service traffic) with the joint action labelled on top. The xApp sets "
         "the UN3GPP uplink MCS/RBs on the BS and the downlink PRB quota on the OAI gNB over E2;",
         "both RAN nodes report back what they apply, and the end-to-end goodput at the phone follows each decision."
         + ("  ✕ = UL action rejected by the BS safety mask (no traffic; the BS stays on link adaptation)."
            if any(c.get("skipped") for c in chosen) else "")],
        foot)


def evidence(cells: list[dict[str, Any]], e2: dict[str, Any], kpm: list[Any], run: Path) -> dict[str, Any]:
    valid = [c for c in cells if "error" not in c]
    rejected = [c for c in valid if c.get("skipped")]
    overrides = [c for c in valid if c["ul_mode"] == "override" and not c.get("skipped")]
    start, end = min(ns(c["started_at"]) for c in valid), max(ns(c["finished_at"]) for c in valid)
    outcomes = [f for (node, _), f in e2["outcomes"].items() if start <= int(f["first_seen_ns"]) <= end]
    by_node = Counter((("un3gpp" if "mac_seq" in f else "oai"), f.get("status")) for f in outcomes)
    capped = [v for t, v in e2["capped"] if start <= t <= end]
    q_ok = [c for c in valid if c.get("e2_dl_quota_applied_median") is not None]
    return {
        "run": run.name,
        "window": [datetime.fromtimestamp(start / 1e9).isoformat(), datetime.fromtimestamp(end / 1e9).isoformat()],
        "e2_control_outcomes_by_node_status": {f"{n}:{s}": v for (n, s), v in sorted(by_node.items())},
        "e2_indications_un3gpp_readback": sum(start <= t <= end for t, _ in e2["ue"]),
        "kpm_samples_service_slice": sum(start <= t <= end for t, _ in kpm),
        "oai_quota_capped_allocations": (capped[-1] - capped[0]) if len(capped) > 1 else None,
        "cells_ul_mcs_observed_equals_commanded": [sum(abs(c["un3gpp_ul_mcs_observed"] - c["ul_mcs"]) < 0.5 for c in overrides
                                                       if c.get("un3gpp_ul_mcs_observed") is not None), len(overrides)],
        "cells_ul_rb_reservation_in_force_equals_commanded": [sum(c.get("e2_rb_cmd_median") == c["ul_rb"]
                                                                  for c in overrides), len(overrides)],
        "cells_ul_rb_observed_max_equals_commanded": [sum(c.get("un3gpp_ul_rb_observed_max") == c["ul_rb"] for c in overrides),
                                                      len(overrides)],
        "cells_dl_quota_in_force_equals_commanded": [sum(abs(c["e2_dl_quota_applied_median"] - c["dl_prb"]) < 0.5 for c in q_ok),
                                                     len(q_ok)],
        "cells_override_active_in_readback": [sum((c.get("e2_override_ratio") or 0) > 0.8 for c in overrides), len(overrides)],
        "cells_link_adaptation_override_off": [sum(c.get("e2_override_ratio") is not None and c["e2_override_ratio"] < 0.2 for c in valid
                                                   if c["ul_mode"] != "override"),
                                               sum(c["ul_mode"] != "override" for c in valid)],
        "cells_rejected_by_bs_mask_override_off": [sum(c.get("e2_override_ratio") is not None and c["e2_override_ratio"] < 0.2
                                                       for c in rejected), len(rejected)],
    }


def markdown(ev: dict[str, Any], figs: list[str], samples: list[str]) -> str:
    def frac(pair: list[int]) -> str:
        return f"{pair[0]}/{pair[1]}"

    lines = [f"# O-RAN control evidence and joint end-to-end results: {ev['run']}", "",
             f"Window: {ev['window'][0]} to {ev['window'][1]} (local).", "",
             "## Control path", "",
             "Every radio decision of this run was issued by the O-RAN xApp on PC1 as an E2 control action and confirmed by "
             "the RAN node in its E2 indications: UN3GPP UL MCS/RBs to the BS (PC2, through its E2 proxy) and the DL PRB "
             "quota of the service slice to the OAI gNB (PC1). No radio parameter was set over SSH.", "",
             "| Evidence | Value |", "|---|---|"]
    lines += [f"| E2 control outcomes ({k}) | {v} |" for k, v in ev["e2_control_outcomes_by_node_status"].items()]
    lines += [f"| UN3GPP readback indications (1 s E2) | {ev['e2_indications_un3gpp_readback']} |",
              f"| OAI KPM samples, service slice | {ev['kpm_samples_service_slice']} |",
              f"| OAI allocations capped by the quota | {ev['oai_quota_capped_allocations']} |",
              f"| Cells: UL MCS observed = commanded | {frac(ev['cells_ul_mcs_observed_equals_commanded'])} |",
              f"| Cells: UL RB reservation in force = commanded | "
              f"{frac(ev['cells_ul_rb_reservation_in_force_equals_commanded'])} |",
              f"| Cells: largest UE PDU filled the whole UL reservation | "
              f"{frac(ev['cells_ul_rb_observed_max_equals_commanded'])} |",
              f"| Cells: DL quota in force = commanded | {frac(ev['cells_dl_quota_in_force_equals_commanded'])} |",
              f"| Cells: override active in the BS readback | {frac(ev['cells_override_active_in_readback'])} |",
              f"| Link-adaptation cells: override off in the BS | {frac(ev['cells_link_adaptation_override_off'])} |",
              f"| Cells rejected by the BS safety mask: override stayed off | "
              f"{frac(ev['cells_rejected_by_bs_mask_override_off'])} |",
              "", "Raw E2 indications received by the xApp (one per node, from this run):", "", "```"]
    lines += samples + ["```", "", "## Figures", ""]
    lines += [f"![{f}]({f})" for f in figs]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--timeline-cells", default="1-10", help="cell index range for the timeline figure")
    args = parser.parse_args()
    cells = read_cells(args.run)
    service = ServiceId(cells[0]["service"])
    sd = int(SERVICE_CATALOG[service].snssai.sd, 16)
    e2 = load_e2(args.run, sd)
    kpm = load_kpm(args.run, sd)
    per_cell_control(cells, e2)
    out = args.run / "analysis"
    out.mkdir(exist_ok=True)

    fs.apply()
    import matplotlib.pyplot as plt

    a, b = (int(x) for x in args.timeline_cells.split("-"))
    foot = footer(service, cells, args.run)
    measured = [c for c in cells if not c.get("skipped")]  # rejected by the BS safety mask: no traffic to show
    figs = [figure_ranking(measured, service, out, plt, 15, foot), figure_ranking(measured, service, out, plt, None, foot),
            *figure_joint(measured, service, out, plt, foot), figure_dl_effect(measured, service, out, plt, foot),
            figure_command_vs_observed(measured, service, out, plt, foot),
            figure_timeline(cells, e2, kpm, service, (a, b), out, plt, foot)]
    ev = evidence(cells, e2, kpm, args.run)
    samples = []
    with gzip.open(args.run / "raw/xapp-sf-control.jsonl.gz", "rt") as handle:
        seen = set()
        for line in handle:
            node = json.loads(line)["node"]
            if node not in seen and any(k in line for k in ("status=CONFIRMED", "status=CONSISTENT", "status=APPLIED")):
                seen.add(node)
                samples.append(line.strip())
            if len(seen) == 2:
                break
    (out / "oran-evidence.json").write_text(json.dumps(ev, indent=2) + "\n", encoding="utf-8")
    (out / "oran-evidence.md").write_text(markdown(ev, figs, samples), encoding="utf-8")
    per_cell = {c["index"]: {k: c.get(k) for k in ("e2_dl_quota_applied_median", "e2_override_ratio")} for c in cells}
    (out / "oran-evidence-per-cell.json").write_text(json.dumps(per_cell) + "\n", encoding="utf-8")
    print(json.dumps(ev, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
