#!/usr/bin/env python3
"""Results of an online RL session (rl_online.py): agent vs RAN defaults under the same live channel.

Usage: rl_online_report.py <experiments/rl-online/<dir>>
Outputs in <dir>/analysis/: summary.json, report.md, fig-rl-online-timeline.png, fig-rl-online-comparison.png.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import figstyle as fs  # noqa: E402

from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId  # noqa: E402

GROUPS = {"ddqn": "DDQN (O-RAN xApp)", "link-adaptation": "RAN defaults (BS link adaptation)"}
COLORS = {"ddqn": fs.BLUE, "link-adaptation": fs.ORANGE}


def load(run: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    meta = json.loads((run / "run-metadata.json").read_text())
    steps = [json.loads(x) for x in (run / "steps.jsonl").read_text().splitlines()]
    return meta, [s for s in steps if s["policy"] != "initial-state"]


def group(step: dict[str, Any]) -> str | None:
    p = step["policy"]
    return "ddqn" if p.startswith("ddqn") else ("link-adaptation" if p in ("link-adaptation", "guard") else None)


def summary(service: ServiceId, steps: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for g in GROUPS:
        xs = [s for s in steps if group(s) == g and "sla_met" in s]
        if not xs:
            continue
        row: dict[str, Any] = {"steps": len(xs), "sla_met": sum(s["sla_met"] for s in xs),
                               "sla_met_ratio": round(statistics.mean(s["sla_met"] for s in xs), 3),
                               "mean_cost": round(statistics.mean(s["cost"] for s in xs), 3),
                               "mean_reward": round(statistics.mean(s["reward"] for s in xs), 3)}
        for m in SERVICE_CATALOG[service].sla:
            vals = [s["cell"].get(f"svc_{m.name}") for s in xs if s["cell"].get(f"svc_{m.name}") is not None]
            row[f"{m.name}_median"] = statistics.median(vals) if vals else None
            row[f"{m.name}_met"] = sum(fs.meets(m, v) for v in vals)
        out[g] = row
    return out


def timeline(service: ServiceId, meta: dict[str, Any], steps: list[dict[str, Any]], out: Path, plt: Any) -> str:
    sla = SERVICE_CATALOG[service].sla
    key = sla[0]
    fig, axes = plt.subplots(3, 1, figsize=(13.0, 7.2), sharex=True, gridspec_kw={"height_ratios": [1.3, 1, 1]})
    xs = [s["step"] for s in steps]
    for s in steps:
        g = group(s)
        if g is None:
            continue
        for ax in axes:
            ax.axvspan(s["step"] - 0.5, s["step"] + 0.5, color=fs.HIGHLIGHT if g == "ddqn" else "#fbefe7", lw=0, zorder=0)
    # 1. the key SLA metric per step, target dashed
    for g in GROUPS:
        pts = [(s["step"], fs.display(key.name, s["cell"][f"svc_{key.name}"])) for s in steps
               if group(s) == g and s["cell"].get(f"svc_{key.name}") is not None]
        ok = [p for p, s in zip(pts, [s for s in steps if group(s) == g and s["cell"].get(f"svc_{key.name}") is not None],
                                strict=True) if s.get("sla_met")]
        axes[0].scatter([p[0] for p in pts], [p[1] for p in pts], s=34, color=COLORS[g], zorder=3, label=GROUPS[g])
        axes[0].scatter([p[0] for p in ok], [p[1] for p in ok], s=90, facecolor="none", edgecolor=fs.INK, lw=0.9,
                        zorder=4)
    axes[0].axhline(fs.display(key.name, key.threshold), color=fs.RED_TEXT, lw=1, ls=(0, (4, 3)))
    axes[0].set_title(f"{fs.metric(key.name)[0]} per 30 s step ({fs.metric(key.name)[2]}); ring = whole SLA met; "
                      f"dashed = target {fs.threshold_text(key)}")
    # 2. the UL action (MCS) and 3. the DL quota chosen
    for g in GROUPS:
        sel = [s for s in steps if group(s) == g]
        mcs = [s["cell"].get("ul_mcs") if s["cell"].get("ul_mcs") is not None else s["cell"].get("un3gpp_ul_mcs_observed")
               for s in sel]
        axes[1].scatter([s["step"] for s in sel], mcs, s=30, color=COLORS[g], zorder=3,
                        marker="o" if g == "ddqn" else "s")
        last = None
        for s, y in zip(sel, mcs, strict=True):  # UL RBs (reservation, or the RBs the BS saw), only where they change
            rb = s["cell"].get("ul_rb") or s["cell"].get("un3gpp_ul_rb_observed_max")
            if y is not None and rb and (y, int(rb)) != last:
                last = (y, int(rb))
                axes[1].annotate(f"{int(rb)} RB", (s["step"], y), xytext=(6, -3), textcoords="offset points",
                                 fontsize=6.5, color=fs.INK_2)
        axes[2].scatter([s["step"] for s in sel], [s["cell"].get("dl_prb") for s in sel], s=30, color=COLORS[g],
                        zorder=3, marker="o" if g == "ddqn" else "s")
    for s in steps:
        if s.get("eliminated"):
            axes[1].annotate("eliminated", (s["step"], s["cell"].get("ul_mcs") or 0), xytext=(0, 8),
                             textcoords="offset points", ha="center", fontsize=6.5, color=fs.RED_TEXT)
    axes[1].set_title("UN3GPP uplink MCS in force (agent: commanded over E2; RAN defaults: observed at the BS)")
    axes[2].set_title("OAI downlink PRB quota of the slice (commanded over E2)")
    axes[2].set_xlabel("session step (30 s of service traffic each)")
    for ax in axes:
        ax.grid(axis="y")
        ax.set_axisbelow(True)
    axes[0].legend(loc="upper left", bbox_to_anchor=(1.005, 1.0))
    if xs:
        axes[2].set_xlim(min(xs) - 0.6, max(xs) + 0.6)
    rule = meta.get("elimination_rule", "first-failure") if meta.get("elimination", True) else None
    adapt = {"first-failure": "an action that misses the SLA is eliminated for the rest of the session.",
             "majority": "an action is eliminated once its SLA misses outnumber its successes.",
             "cause": "a miss blocks every action that fails for the same cause (UL loss, UL capacity, DL queue).",
             None: "no action elimination and no online learning: the model acts exactly as trained."}[rule]
    if meta.get("learn"):
        adapt += " The agent also keeps learning from every step (anchored replay)."
    alternate = meta.get("policy") == "alternate"
    return fs.finish(fig, out / "fig-rl-online-timeline.png",
                     f"{fs.SERVICE_NAMES[service.value]}: "
                     + ("online DDQN vs RAN defaults" if alternate else "DDQN alone") + ", live on the testbed",
                     [("Steps alternate between the agent (blue bands) and the RAN defaults (orange bands) under the same "
                       "channel; " if alternate else "Every step is chosen by the agent; ")
                      + "each step is one 30 s cell of real service traffic, radio set over E2.",
                      "The agent chooses from the previous step's E2 telemetry and SLA margins; " + adapt],
                     [fs.sla_line(service), f"Data: {out.parent.name}; model {Path(meta['model']).parent.name}."])


def comparison(service: ServiceId, summ: dict[str, Any], steps: list[dict[str, Any]], out: Path, plt: Any,
               run: Path) -> str:
    """Every step as a point (medians hide the failures, which are tail events), target dashed, and for each
    requirement how many steps of each policy met it."""

    import random

    sla = SERVICE_CATALOG[service].sla
    fig, axes = plt.subplots(1, len(sla) + 2, figsize=(3.0 * (len(sla) + 2), 4.0))
    groups = [g for g in GROUPS if g in summ]
    names = ["DDQN" if g == "ddqn" else "RAN defaults" for g in groups]
    axes[0].bar(range(len(groups)), [summ[g]["sla_met_ratio"] * 100 for g in groups],
                color=[COLORS[g] for g in groups], width=0.6)
    for i, g in enumerate(groups):
        axes[0].text(i, summ[g]["sla_met_ratio"] * 100, f"{summ[g]['sla_met']}/{summ[g]['steps']}", ha="center",
                     va="bottom", fontsize=8.5, fontweight="bold")
    axes[0].set_title("Whole SLA met\n(% of steps)")
    axes[0].set_ylim(0, 110)
    rng = random.Random(1)
    cax = axes[1]  # radio resource cost of each step (what the agent saves when the SLA is met either way)
    for i, g in enumerate(groups):
        costs = [s["cost"] for s in steps if group(s) == g and "cost" in s]
        cax.scatter([i + rng.uniform(-0.18, 0.18) for _ in costs], costs, s=16, color=COLORS[g], alpha=0.75, zorder=3,
                    edgecolor="none")
        if costs:
            cax.plot([i - 0.28, i + 0.28], [sum(costs) / len(costs)] * 2, color=fs.INK, lw=1.6, zorder=4)
    cax.set_title("Radio resource cost\n(0 = least, 1 = most; tick = mean)")
    cax.set_ylim(-0.03, 1.03)
    cax.set_xlim(-0.6, len(groups) - 0.4)
    for ax, m in zip(axes[2:], sla, strict=True):
        counts = []
        for i, g in enumerate(groups):
            vals = [fs.display(m.name, s["cell"][f"svc_{m.name}"]) for s in steps
                    if group(s) == g and "sla_met" in s and s["cell"].get(f"svc_{m.name}") is not None]
            ax.scatter([i + rng.uniform(-0.18, 0.18) for _ in vals], vals, s=16, color=COLORS[g], alpha=0.75,
                       zorder=3, edgecolor="none")
            if vals:
                med = sorted(vals)[len(vals) // 2]
                ax.plot([i - 0.28, i + 0.28], [med, med], color=fs.INK, lw=1.6, zorder=4)
            counts.append(f"{summ[g][f'{m.name}_met']}/{summ[g]['steps']}")
        ax.axhline(fs.display(m.name, m.threshold), color=fs.RED_TEXT, lw=1, ls=(0, (4, 3)), zorder=2)
        if m.operator == "eq":  # equality targets (100 % intact, 0 corrupt): a physical axis
            top = 105.0 if fs.metric(m.name)[2] == "%" else max(ax.get_ylim()[1], 1.0) * 1.2
            ax.set_ylim(-0.04 * top, top)
        ax.set_title(f"{fs.metric(m.name)[1]} ({fs.metric(m.name)[2]})\nmet: " + " vs ".join(counts))
        ax.set_xlim(-0.6, len(groups) - 0.4)
    for ax in axes:
        ax.set_xticks(range(len(groups)), names)
        ax.grid(axis="y")
        ax.set_axisbelow(True)
    return fs.finish(fig, out / "fig-rl-online-comparison.png",
                     f"{fs.SERVICE_NAMES[service.value]}: end-to-end SLA, online DDQN vs RAN defaults",
                     ["Same live session, alternating steps. Each point is one 30 s step measured end to end at the "
                      "phone; black tick = median; dashed = SLA target.",
                      "Panel titles count the steps of each policy (DDQN vs RAN defaults) that met that requirement."],
                     [fs.sla_line(service), f"Data: {run.name}."])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    meta, steps = load(args.run)
    service = ServiceId(meta["service"])
    out = args.run / "analysis"
    out.mkdir(exist_ok=True)
    summ = summary(service, steps)
    eliminated = [s["eliminated"] for s in steps if s.get("eliminated")]
    result = {"run": args.run.name, "service": service.value, "model": meta["model"],
              "pc2_clock_offset_s": meta.get("pc2_clock_offset_s"), "by_policy": summ, "eliminated": eliminated,
              "ddqn_actions": sorted({s["action"] for s in steps if group(s) == "ddqn"}),
              "errors": sum("error" in s["cell"] for s in steps)}
    (out / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    fs.apply()
    import matplotlib.pyplot as plt

    figs = [timeline(service, meta, steps, out, plt), comparison(service, summ, steps, out, plt, args.run)]
    lines = [f"# Online RL session: {args.run.name}", "", f"- Service: {service.value}; model: `{meta['model']}`",
             f"- PC2 clock offset compensated: {meta.get('pc2_clock_offset_s')} s", "",
             "| Policy | Steps | SLA met | Mean cost | Mean reward |", "|---|---|---|---|---|"]
    lines += [f"| {GROUPS[g]} | {r['steps']} | {r['sla_met']} ({r['sla_met_ratio']:.0%}) | {r['mean_cost']} | "
              f"{r['mean_reward']} |" for g, r in summ.items()]
    lines += ["", f"- Actions eliminated during the session: {', '.join(eliminated) or 'none'}",
              f"- Actions chosen by the agent: {', '.join(result['ddqn_actions'])}", "", "## Figures", ""]
    lines += [f"![{f}]({f})" for f in figs]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(summ, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
