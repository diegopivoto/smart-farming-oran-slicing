#!/usr/bin/env python3
"""Ablation of the offline Double DQN variants (leave-one-pass-out) against the reference policies.

Usage: rl_ablation.py --output <dir> <rl run dir>...   (each run dir holds eval.json from rl_offline.py)
Outputs: fig-rl-ablation.png (one row per variant, one column per service: share of session steps meeting the whole
SLA, with the mean radio resource cost) and rl-ablation.json / rl-ablation.md with the same table.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import figstyle as fs  # noqa: E402

SERVICES = ["weather", "crop", "drone"]
REFERENCES = ["oracle", "fixed winner", "link adapt.", "random"]
REF_NAMES = {"oracle": "Oracle: best single action of the held-out pass (upper bound)",
             "fixed winner": "Fixed tie-break winner of the training passes",
             "link adapt.": "RAN defaults: BS link adaptation, smallest DL quota",
             "random": "Random safe action every step"}


def variant_name(run: Path, ev: dict[str, Any]) -> str:
    c = ev.get("config", {})
    head = "Q(s, φ(a))" if c.get("action_features") else "Q(s) → |A|"
    gamma = c.get("ddqn", {}).get("gamma", 0.9)
    margin = c.get("reward", {}).get("margin_weight", 0.0)
    drift = c.get("drift", 0.15 if "drift0.15" in run.name else None)
    return f"{head} · γ {gamma:g} · margin {margin:g} · drift {drift:g}"


def collect(runs: list[Path]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows, refs = [], {}
    for run in runs:
        ev = json.loads((run / "eval.json").read_text())
        name = variant_name(run, ev)
        for policy in ("DDQN", "DDQN + elimination"):
            if policy in ev["summary"]["drone"]:
                rows.append({"variant": name + (" · + action elimination" if "elim" in policy else ""), "run": run.name,
                             "best": "elim" in policy,
                             **{s: ev["summary"][s][policy] for s in SERVICES}})
        for r in REFERENCES:
            refs.setdefault(r, {s: ev["summary"][s][r] for s in SERVICES})
    return rows, refs


def figure(rows: list[dict[str, Any]], refs: dict[str, dict[str, Any]], out: Path, data_note: str) -> str:
    fs.apply()
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch

    lines = rows + [{"variant": REF_NAMES[r], "ref": True, **refs[r]} for r in REFERENCES]
    fig, ax = plt.subplots(figsize=(12.0, 0.42 * len(lines) + 1.6))
    xs = {s: 6.2 + 1.9 * k for k, s in enumerate(SERVICES)}
    for i, line in enumerate(lines):
        y = len(lines) - i - (0.6 if line.get("ref") else 0)
        bold = line.get("best")
        if bold:
            ax.add_patch(FancyBboxPatch((-0.1, y - 0.45), 11.9, 0.9, boxstyle="round,pad=0,rounding_size=0.1",
                                        facecolor=fs.HIGHLIGHT, lw=0))
        ax.text(0, y, line["variant"], va="center", ha="left", fontsize=8.2,
                color=fs.INK_2 if line.get("ref") else fs.INK, fontweight="bold" if bold else "normal",
                style="italic" if line.get("ref") else "normal")
        for s in SERVICES:
            met, cost = line[s]["sla_met_ratio"], line[s]["mean_cost"]
            level = min(4, int(met * 4 + 1e-9)) if met < 1 else 4
            ax.add_patch(FancyBboxPatch((xs[s] - 0.75, y - 0.33), 1.5, 0.66, boxstyle="round,pad=0,rounding_size=0.12",
                                        facecolor=fs.ORDINAL[level], lw=0))
            ax.text(xs[s] - 0.06, y, f"{met:.0%}", ha="right", va="center", fontsize=8.5, fontweight="bold",
                    color="#ffffff" if level >= 2 else fs.INK)
            ax.text(xs[s] + 0.02, y, f"cost {cost:.2f}", ha="left", va="center", fontsize=7,
                    color="#ffffff" if level >= 2 else fs.INK_2)
    top = len(lines) + 0.9
    ax.text(0, top, "Policy (DDQN variant or reference)", fontsize=8.5, fontweight="bold", va="bottom")
    for s in SERVICES:
        ax.text(xs[s], top, fs.SERVICE_NAMES[s], ha="center", va="bottom", fontsize=8.5, fontweight="bold")
    sep = len(rows) + 0.5 - len(rows) + len(REFERENCES) + 0.2
    ax.plot([0, 11.8], [sep, sep], color=fs.INK_3, lw=0.6, ls=(0, (2, 2)))
    ax.set_xlim(-0.2, 11.9)
    ax.set_ylim(0, top + 0.8)
    ax.axis("off")
    return fs.finish(
        fig, out / "fig-rl-ablation.png", "DDQN design choices, tested on calibration passes the agent never saw",
        ["Each cell: share of 20-step sessions (30 s per step) meeting the whole SLA, over the four leave-one-pass-out "
         "folds, and the mean radio resource cost (0 = least, 1 = most).",
         "Q(s) → |A| = one output per joint action; Q(s, φ(a)) = the action's UL MCS, UL RBs and DL quota are inputs, so "
         "neighbouring actions share what is learned. Action elimination blocks, for the rest of the session, an "
         "action that missed the SLA."],
        [f"Data: {data_note}. Rewards: 1 per step meeting the SLA minus 0.3 × resource cost (+ margin × "
         "tightest-requirement margin where noted)."])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-note", required=True,
                        help='calibration data in the footer, e.g. "N measured cells (weather a, crop b, drone c; 4 passes each)"')
    parser.add_argument("runs", nargs="+", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rows, refs = collect(args.runs)
    fig = figure(rows, refs, args.output, args.data_note)
    (args.output / "rl-ablation.json").write_text(json.dumps({"variants": rows, "references": refs}, indent=2) + "\n")
    md = ["| Policy | " + " | ".join(f"{s} SLA / cost" for s in SERVICES) + " |", "|---|---|---|---|"]
    md += [f"| {r['variant']} | " + " | ".join(f"{r[s]['sla_met_ratio']:.1%} / {r[s]['mean_cost']:.2f}" for s in SERVICES)
           + " |" for r in rows]
    md += [f"| {REF_NAMES[k]} | " + " | ".join(f"{v[s]['sla_met_ratio']:.1%} / {v[s]['mean_cost']:.2f}" for s in SERVICES)
           + " |" for k, v in refs.items()]
    (args.output / "rl-ablation.md").write_text("\n".join(md) + "\n")
    print(fig)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
