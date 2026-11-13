#!/usr/bin/env python3
"""Mobility experiment in one figure: the agent with online adaptation, the RAN defaults (same A/B session) and the
frozen agent (control session right after, same position), per step and in total.

    mobility_figure.py --adapted experiments/rl-online/<A/B session> --frozen experiments/rl-online/<frozen session> \
        --output docs/results/figures/<dir> --position "gNB RSRP about -108 to -114 dBm"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import figstyle as fs  # noqa: E402

GOODPUT_TARGET = 34.0  # drone SLA, Mbit/s


def steps(run: Path, policy: str) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in (run / "steps.jsonl").read_text().splitlines()]
    return [s for s in rows if s["policy"] == policy and "sla_met" in s]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--adapted", type=Path, required=True)
    parser.add_argument("--frozen", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--position", required=True)
    args = parser.parse_args()
    groups = [("DDQN, online adaptation", steps(args.adapted, "ddqn"), fs.BLUE),
              ("RAN defaults (BS link adaptation, 52 PRB)", steps(args.adapted, "link-adaptation"), fs.ORANGE),
              ("DDQN, frozen (no adaptation)", steps(args.frozen, "ddqn"), fs.INK_3)]
    fs.apply()
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(15.0, 4.6), gridspec_kw={"width_ratios": [1.0, 1.6, 1.0]})
    names = [g[0] for g in groups]
    ys = list(range(len(groups)))[::-1]
    ratios = [100 * sum(s["sla_met"] for s in g[1]) / len(g[1]) for g in groups]
    axes[0].barh(ys, ratios, color=[g[2] for g in groups], height=0.55, zorder=2)
    for y, g, r in zip(ys, groups, ratios, strict=True):
        axes[0].text(r + 2, y, f"{sum(s['sla_met'] for s in g[1])}/{len(g[1])}", va="center", fontsize=9, color=fs.INK)
    axes[0].set_yticks(ys, [n.replace(", ", ",\n", 1) for n in names], fontsize=8.5)
    axes[0].set_xlim(0, 118)
    axes[0].set_title("Steps meeting the whole SLA (%)")
    axes[0].grid(axis="x")
    axes[0].set_axisbelow(True)
    for y, (_, g, color) in zip(ys, groups, strict=True):
        vals = [s["cell"].get("svc_goodput_p10", 0) / 1e6 for s in g]
        axes[1].scatter(vals, [y + (i % 5 - 2) * 0.06 for i in range(len(vals))], s=26, color=color, alpha=0.8,
                        edgecolors=fs.SURFACE, linewidths=0.8, zorder=3)
    axes[1].axvline(GOODPUT_TARGET, color=fs.RED_TEXT, ls=(0, (4, 3)), lw=1.2, zorder=2)
    axes[1].text(GOODPUT_TARGET, len(groups) - 0.45, f" target ≥ {GOODPUT_TARGET:g}", color=fs.RED_TEXT, fontsize=8,
                 va="bottom")
    axes[1].set_yticks(ys, ["" for _ in ys])
    axes[1].set_ylim(-0.6, len(groups) - 0.3)
    axes[1].set_xlabel("goodput p10 of each 30 s step (Mbit/s)")
    axes[1].set_title("Drone goodput per step (one dot per step)")
    axes[1].grid(axis="x")
    for y, (_, g, color) in zip(ys, groups, strict=True):
        quotas: dict[int, int] = {}
        for s in g:
            quotas[s["cell"]["dl_prb"]] = quotas.get(s["cell"]["dl_prb"], 0) + 1
        left = 0.0
        for q in sorted(quotas):
            share = 100 * quotas[q] / len(g)
            axes[2].barh(y, share, left=left, color=color, alpha={52: 0.35, 78: 0.65, 106: 1.0}.get(q, 0.5),
                         height=0.55, edgecolor=fs.SURFACE, linewidth=1.5, zorder=2)
            if share >= 9:
                axes[2].text(left + share / 2, y, f"{q}", ha="center", va="center", fontsize=8.5,
                             color=fs.SURFACE if q != 52 else fs.INK)
            left += share
    axes[2].set_yticks(ys, ["" for _ in ys])
    axes[2].set_xlim(0, 100)
    axes[2].set_title("Downlink PRB quota used (% of steps)")
    axes[2].set_xlabel("share of the policy's steps; label = PRB quota")
    path = fs.finish(
        fig, args.output / "fig-mobility-three-policies.png",
        "Drone video with the phone at a degraded position: online adaptation vs RAN defaults vs frozen agent",
        [f"Position: {args.position}; 40 Mbit/s does not fit the smallest drone quota (52 PRB). The adapted agent and the "
         "RAN defaults alternate step by step in one A/B session;",
         "the frozen agent (same model, no online learning or elimination) ran right after, at the same position."],
        [f"Data: {args.adapted.name} (A/B, {len(groups[0][1])} + {len(groups[1][1])} steps) and {args.frozen.name} "
         f"({len(groups[2][1])} steps); 30 s of real service traffic per step, radio set over E2. "
         "Drone SLA: goodput p10 ≥ 34 Mbit/s, loss ≤ 5 %, jitter p95 ≤ 20 ms, longest gap ≤ 500 ms."])
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
