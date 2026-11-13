#!/usr/bin/env python3
"""Analyse a calibration sweep: per-action statistics over the passes, the tie-break winner and paper figures.

    analyze_calibration.py experiments/calibration/<run>

Writes <run>/analysis/: cells.csv (cells.jsonl as a spreadsheet, one row per measured cell, `error` set on
excluded ones), actions.csv and actions.json (one row per action), winners.json, report.md and fig-*.png.
Output in English; every column is described in docs/reference/live-metrics-glossary.md (section 8.1).

Winner rule (docs/design/calibration-action-spaces.md): among UL-override actions that met the service SLA in every
valid pass, the lowest UL MCS, then the lowest UL RB count, then the lowest DL PRB quota. Cells followed by a UE
incident (unhealthy event before the next cell) stay in the statistics and are flagged.
"""

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId

OPS = {"ge": ">=", "le": "<=", "eq": "=="}


def read_cells(out: Path) -> list[dict[str, Any]]:
    """Recorded cells; those listed in invalidated.json carry an `error` (kept in the file, excluded everywhere)."""

    path = out / "cells.jsonl"
    cells = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    invalidated = out / "invalidated.json"
    if invalidated.exists():
        spec = json.loads(invalidated.read_text())
        for c in cells:
            if c["index"] in spec["indices"]:
                c.setdefault("error", f"invalidated: {spec['reason']}")
    return cells


def load(run: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    cells = read_cells(run)
    events = [json.loads(x) for x in (run / "events.jsonl").read_text().splitlines()]
    metadata = json.loads((run / "run-metadata.json").read_text())
    return cells, events, metadata


def flag_incidents(cells: list[dict[str, Any]], events: list[dict[str, Any]]) -> None:
    """Mark the last cell that finished before each `unhealthy` event (the incident started during or after it)."""

    finished = sorted((c for c in cells if c.get("finished_at")), key=lambda c: c["finished_at"])
    for e in events:
        if e["event"] != "unhealthy":
            continue
        before = [c for c in finished if c["finished_at"] <= e["at"]]
        if before:
            before[-1].setdefault("incidents", []).append(f"{e['at']}: {e['detail']}")


def action_key(c: dict[str, Any]) -> tuple[int, int, int, int]:
    """Sort key: overrides by (MCS, RB, PRB); link adaptation after them."""

    la = c["ul_mode"] == "link-adaptation"
    return (1 if la else 0, c["ul_mcs"] or 0, c["ul_rb"] or 0, c["dl_prb"])


def label(c: dict[str, Any]) -> str:
    ul = "UL link-adaptation" if c["ul_mode"] == "link-adaptation" else f"UL MCS {c['ul_mcs']} / {c['ul_rb']} RB"
    return f"{ul} + DL {c['dl_prb']} PRB"


def r(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def stats(values: list[float]) -> dict[str, float | None]:
    return {"median": r(statistics.median(values)) if values else None,
            "min": r(min(values)) if values else None, "max": r(max(values)) if values else None}


def per_action(cells: list[dict[str, Any]], service: ServiceId) -> list[dict[str, Any]]:
    sla = SERVICE_CATALOG[service].sla
    groups: dict[tuple[int, int, int, int], list[dict[str, Any]]] = defaultdict(list)
    for c in cells:
        if "error" not in c and not c.get("skipped"):
            groups[action_key(c)].append(c)
    rows = []
    for key in sorted(groups):
        items = groups[key]
        first = items[0]

        def values(field: str) -> list[float]:
            return [float(c[field]) for c in items if c.get(field) is not None]  # noqa: B023 - evaluated in-loop

        row: dict[str, Any] = {
            "action": label(first), "ul_mode": first["ul_mode"], "ul_mcs": first["ul_mcs"], "ul_rb": first["ul_rb"],
            "dl_prb": first["dl_prb"], "ul_capacity_mbps_est": first.get("ul_capacity_mbps_est"),
            "dl_capacity_mbps_est": first.get("dl_capacity_mbps_est"), "n_valid": len(items),
            "passes": sorted(c["pass"] for c in items),
            "sla_met_count": sum(bool(c.get("sla_met")) for c in items),
        }
        row["sla_met_ratio"] = r(row["sla_met_count"] / len(items), 3)
        for metric in sla:
            s = stats(values(f"svc_{metric.name}"))
            row[f"{metric.name}_median"] = s["median"]
            # worst pass with respect to the target: lowest value for ">=" metrics, highest for "<="
            row[f"{metric.name}_worst"] = s["min"] if metric.operator == "ge" else s["max"]
        row["goodput_mbps_median"] = r(statistics.median([v / 1e6 for v in values("svc_goodput")]), 2) \
            if values("svc_goodput") else None
        for out, field in (("ul_snr_db_mean", "un3gpp_ul_snr_db"), ("ul_mcs_observed_mean", "un3gpp_ul_mcs_observed"),
                           ("ul_la_mcs_mean", "un3gpp_la_mcs"), ("ul_mac_mbps_mean", "un3gpp_mac_mbps"),
                           ("kpm_ue_thp_dl_mbps_mean", "kpm_ue_thp_dl_mbps"), ("kpm_prb_tot_dl_pct_mean", "kpm_prb_tot_dl_pct"),
                           ("kpm_rlc_delay_dl_ms_mean", "kpm_rlc_delay_dl_ms"), ("kpm_ss_sinr_db_mean", "kpm_ss_sinr_db"),
                           ("kpm_ss_rsrp_dbm_mean", "kpm_ss_rsrp_dbm"), ("kpm_pdsch_mcs_mean", "kpm_pdsch_mcs_mean")):
            v = values(field)
            row[out] = r(statistics.mean(v), 3) if v else None
        row["incidents_after_cell"] = sum(len(c.get("incidents", [])) for c in items)
        rows.append(row)
    return rows


def wilson_low(k: int, n: int, z: float = 1.96) -> float | None:
    """Lower bound of the 95 % Wilson score interval of a success ratio k/n."""

    if n == 0:
        return None
    p = k / n
    return round((p + z * z / (2 * n) - z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5) / (1 + z * z / n), 3)


def pooled_ul(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per UL action over all DL quotas: the DL quotas of the mask all exceed the offered rate, so pooling isolates
    the uplink (the bottleneck of the UN3GPP -> phone path) with n = passes x DL quotas."""

    groups: dict[tuple[int, int, int], list[dict[str, Any]]] = defaultdict(list)
    for c in cells:
        la = c["ul_mode"] == "link-adaptation"
        groups[(1 if la else 0, c["ul_mcs"] or 0, c["ul_rb"] or 0)].append(c)
    rows = []
    for key in sorted(groups):
        items = groups[key]
        k = sum(bool(c.get("sla_met")) for c in items)
        loss = [c["svc_packet_loss_ratio"] for c in items if c.get("svc_packet_loss_ratio") is not None]
        good = [c["svc_goodput"] / 1e6 for c in items if c.get("svc_goodput") is not None]
        snr = [c["un3gpp_ul_snr_db"] for c in items if c.get("un3gpp_ul_snr_db") is not None]
        rows.append({"ul_action": "UL link-adaptation" if key[0] else f"UL MCS {key[1]} / {key[2]} RB",
                     "ul_mcs": None if key[0] else key[1], "ul_rb": None if key[0] else key[2], "n": len(items),
                     "sla_met_count": k, "sla_met_ratio": round(k / len(items), 3), "sla_met_wilson95_low": wilson_low(k, len(items)),
                     "goodput_mbps_median": r(statistics.median(good), 2) if good else None,
                     "loss_median": r(statistics.median(loss)) if loss else None,
                     "ul_snr_db_mean": r(statistics.mean(snr), 2) if snr else None})
    return rows


def pass_overview(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Radio drift indicator: share of override cells meeting the SLA and link-adaptation behaviour per pass."""

    rows = []
    for p in sorted({c["pass"] for c in cells}):
        ov = [c for c in cells if c["pass"] == p and c["ul_mode"] == "override"]
        la = [c for c in cells if c["pass"] == p and c["ul_mode"] == "link-adaptation"]
        rows.append({"pass": p, "first_cell": min(c["started_at"] for c in ov + la),
                     "override_cells": len(ov), "override_sla_met": sum(bool(c.get("sla_met")) for c in ov),
                     "la_sla_met": f"{sum(bool(c.get('sla_met')) for c in la)}/{len(la)}",
                     "la_ul_snr_db_mean": r(statistics.mean([c["un3gpp_ul_snr_db"] for c in la if c.get("un3gpp_ul_snr_db")]), 2)
                     if la else None,
                     "la_ul_mcs_mean": r(statistics.mean([c["un3gpp_ul_mcs_observed"] for c in la
                                                          if c.get("un3gpp_ul_mcs_observed") is not None]), 2) if la else None})
    return rows


def winners(rows: list[dict[str, Any]], cells: list[dict[str, Any]], passes: int) -> dict[str, Any]:
    overrides = [x for x in rows if x["ul_mode"] == "override"]

    def pick(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        return min(candidates, key=lambda x: (x["ul_mcs"], x["ul_rb"], x["dl_prb"])) if candidates else None

    strict = [x for x in overrides if x["n_valid"] >= passes and x["sla_met_count"] == x["n_valid"]]
    relaxed = [x for x in overrides if x["sla_met_count"] >= max(1, x["n_valid"] - 1) and x["n_valid"] >= passes - 1]
    per_pass = {}
    for p in sorted({c["pass"] for c in cells}):
        met = [c for c in cells if c["pass"] == p and c["ul_mode"] == "override" and c.get("sla_met")]
        best = min(met, key=lambda c: (c["ul_mcs"], c["ul_rb"], c["dl_prb"])) if met else None
        per_pass[p] = label(best) if best else None
    la = [x for x in rows if x["ul_mode"] == "link-adaptation"]
    return {
        "rule": "lowest UL MCS, then lowest UL RB count, then lowest DL PRB quota",
        "strict": {"criterion": f"SLA met in all {passes} passes", "candidates": len(strict),
                   "winner": pick(strict)},
        "relaxed": {"criterion": "SLA met in all but at most one pass", "candidates": len(relaxed),
                    "winner": pick(relaxed)},
        "per_pass": per_pass,
        "link_adaptation_baseline": [{k: x[k] for k in ("action", "n_valid", "sla_met_count", "sla_met_ratio")} for x in la],
        "reliable_actions": [x["action"] for x in sorted(strict, key=lambda x: (x["ul_mcs"], x["ul_rb"], x["dl_prb"]))],
        "winner_wilson95_low": wilson_low(passes, passes) if strict else None,
        "ul_pooled_over_dl_quotas": pooled_ul(cells),
        "per_pass_overview": pass_overview(cells),
    }


def figures(rows: list[dict[str, Any]], cells: list[dict[str, Any]], service: ServiceId, win: dict[str, Any],
            out: Path, run: Path) -> list[str]:
    """Per-metric end-to-end curves vs the commanded UN3GPP UL MCS and the BS-reported UL SNR. The joint (UL x DL)
    views, the ranking and the O-RAN evidence are drawn by analyze_oran_evidence.py."""

    import figstyle as fs

    fs.apply()
    import matplotlib.pyplot as plt

    valid = [c for c in cells if "error" not in c and not c.get("skipped")]
    overrides = [c for c in valid if c["ul_mode"] == "override"]
    baseline = [c for c in valid if c["ul_mode"] != "override"]
    mcs = sorted({c["ul_mcs"] for c in overrides})
    rbs = sorted({c["ul_rb"] for c in overrides})
    prbs = sorted({c["dl_prb"] for c in valid})
    passes = len({c["pass"] for c in valid})
    name = fs.SERVICE_NAMES[service.value]
    foot = [fs.sla_line(service),
            f"Data: calibration run {run.name} — {passes} passes, 30 s of traffic per cell ({len(valid)} valid cells); "
            "service metrics end to end at the phone, radio telemetry over E2."]
    written = []
    for metric in SERVICE_CATALOG[service].sla:
        field = f"svc_{metric.name}"
        readable, short, unit, _ = fs.metric(metric.name)
        fig, ax = plt.subplots(figsize=(7.4, 4.0))
        ax.grid(axis="y")
        ax.set_axisbelow(True)
        for k, rb in enumerate(rbs):
            xs, ys = [], []
            for m in mcs:
                v = [fs.display(metric.name, c[field]) for c in overrides
                     if c["ul_mcs"] == m and c["ul_rb"] == rb and c.get(field) is not None]
                if v:
                    xs.append(m)
                    ys.append(statistics.median(v))
            if xs:
                ax.plot(xs, ys, color=[fs.BLUE, fs.ORANGE, fs.AQUA][k % 3], lw=2, marker="o", ms=5.5,
                        markeredgecolor=fs.SURFACE, markeredgewidth=1.5, label=f"{rb} UL RBs", zorder=3)
        la = [fs.display(metric.name, c[field]) for c in baseline if c.get(field) is not None]
        if la:
            ax.axhline(statistics.median(la), color=fs.INK_3, lw=1.2, ls=(0, (1, 2)), zorder=2,
                       label="BS link adaptation (median)")
        t = fs.display(metric.name, metric.threshold)
        ax.axhline(t, color=fs.RED_TEXT, lw=1, ls=(0, (4, 3)), zorder=2)
        low, high = ax.get_ylim()
        ax.axhspan(*((low, t) if metric.operator == "ge" else (t, high)), color="#fbeaea", lw=0, zorder=0)
        ax.set_ylim(low, high)
        ax.annotate(f"target {fs.threshold_text(metric)}", (mcs[-1] + 0.45, t), xytext=(0, 3), textcoords="offset points",
                    fontsize=7.5, color=fs.RED_TEXT, va="bottom", ha="right",
                    bbox={"facecolor": fs.SURFACE, "edgecolor": "none", "pad": 0.5, "alpha": 0.85})
        ax.set_xticks(mcs)
        ax.set_xlim(mcs[0] - 0.5, mcs[-1] + 0.5)
        ax.set_xlabel("UN3GPP uplink MCS commanded by the O-RAN xApp")
        ax.set_ylabel(f"{short} ({unit})")
        ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0))
        written.append(fs.finish(
            fig, out / f"fig-{metric.name}-vs-ul-mcs.png", f"{name}: {readable.lower()} vs UN3GPP uplink MCS",
            [f"One line per UN3GPP uplink RB allocation; each point = median over {passes} passes and the "
             f"{len(prbs)} OAI downlink quotas, end to end at the phone.",
             "Shaded: misses the SLA target. Dotted: the BS's own link adaptation, for reference."],
            foot))

    fig, ax = plt.subplots(figsize=(7.4, 3.8))
    ax.grid(axis="y")
    ax.set_axisbelow(True)
    pts = [(c["ul_mcs"], c["un3gpp_ul_snr_db"]) for c in overrides if c.get("un3gpp_ul_snr_db") is not None]
    spread = [m + 0.36 * ((i * 0.618) % 1 - 0.5) for i, (m, _) in enumerate(pts)]
    ax.scatter(spread, [p[1] for p in pts], s=10, color=fs.BLUE, alpha=0.35, edgecolors="none", label="one cell")
    med = [(m, statistics.median([p[1] for p in pts if p[0] == m])) for m in mcs if any(p[0] == m for p in pts)]
    ax.plot([m for m, _ in med], [v for _, v in med], color=fs.INK, lw=2, marker="o", ms=5, markeredgecolor=fs.SURFACE,
            label="median per MCS")
    la = [c["un3gpp_ul_snr_db"] for c in baseline if c.get("un3gpp_ul_snr_db") is not None]
    if la:
        ax.axhline(statistics.median(la), color=fs.INK_3, lw=1.2, ls=(0, (1, 2)), label="BS link adaptation (median)")
    ax.set_xticks(mcs)
    ax.set_xlabel("UN3GPP uplink MCS commanded by the O-RAN xApp")
    ax.set_ylabel("UL SNR reported by the BS (dB)")
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0))
    written.append(fs.finish(
        fig, out / "fig-ul-snr-vs-ul-mcs.png", f"{name}: uplink SNR reported by the UN3GPP BS vs commanded MCS",
        ["Mean of the BS's 1 s E2 readbacks during each cell. The radio channel did not change with the action, yet the "
         "reported SNR rises with the MCS:", "the estimator depends on the modulation, so the raw value must not enter "
         "the RL state as a channel measurement."],
        foot))
    return written


def report(run: Path, service: ServiceId, cells: list[dict[str, Any]], events: list[dict[str, Any]],
           rows: list[dict[str, Any]], win: dict[str, Any], figs: list[str], metadata: dict[str, Any]) -> str:
    sla = SERVICE_CATALOG[service].sla
    errors = [c for c in cells if "error" in c]
    valid = [c for c in cells if "error" not in c and not c.get("skipped")]
    lines = [f"# Calibration analysis: {service.value} ({run.name})", "",
             f"- Cells recorded: {len(cells)} (valid {len(valid)}, errors {len(errors)}, "
             f"rejected by the BS safety mask {sum(bool(c.get('skipped')) for c in cells)})",
             f"- Valid cells whose SLA could not be evaluated (a required metric missing, e.g. the generator's TCP "
             f"summary never reached the collector), counted as SLA not met: "
             f"{sum(c.get('sla_met') is None for c in valid)}",
             f"- Actions: {len(rows)} ({sum(x['ul_mode'] == 'override' for x in rows)} UL overrides x DL quotas, "
             f"{sum(x['ul_mode'] == 'link-adaptation' for x in rows)} link-adaptation baselines); passes: {metadata['passes']}",
             f"- Offered rate: {metadata['rate_bps'] / 1e6:g} Mbit/s; cell traffic: {metadata['cell_s']} s",
             "- SLA: " + "; ".join(f"{m.name} {OPS[m.operator]} {m.threshold:g} {m.unit}" for m in sla),
             f"- First cell: {min(c['started_at'] for c in valid)}; last cell: {max(c['finished_at'] for c in valid)} (UTC)", "",
             "## Tie-break winner", "", f"Rule: {win['rule']}.", ""]
    for kind in ("strict", "relaxed"):
        w = win[kind]["winner"]
        lines.append(f"- **{kind}** ({win[kind]['criterion']}; {win[kind]['candidates']} candidates): "
                     + (f"**{w['action']}**, SLA {w['sla_met_count']}/{w['n_valid']}, "
                        + ", ".join(f"{m.name} median {w[f'{m.name}_median']} (worst {w[f'{m.name}_worst']})" for m in sla)
                        if w else "none"))
    lines += ["", "Winner of each pass (lowest MCS/RB/PRB that met the SLA in that pass):", ""]
    lines += [f"- pass {p}: {a}" for p, a in win["per_pass"].items()]
    lines += ["", "Link-adaptation baseline (UL override released):", ""]
    lines += [f"- {x['action']}: SLA {x['sla_met_count']}/{x['n_valid']}" for x in win["link_adaptation_baseline"]]
    lines += ["", f"Actions meeting the SLA in every pass ({len(win['reliable_actions'])}):", ""]
    lines += [f"- {a}" for a in win["reliable_actions"]] or ["- none"]
    if win["winner_wilson95_low"] is not None:
        lines += ["", f"With n = {metadata['passes']} passes, a {metadata['passes']}/{metadata['passes']} action has a "
                      f"95 % Wilson lower bound of {win['winner_wilson95_low']} on its SLA-met probability."]
    lines += ["", "## Uplink actions pooled over the DL quotas", "",
              "All DL quotas of the mask exceed the offered rate, so the DL quota is not expected to change the outcome; "
              "pooling isolates the UN3GPP uplink (n = passes x DL quotas).", "",
              "| UL action | n | SLA met | Wilson 95 % low | Goodput median (Mbit/s) | Loss median | UL SNR mean (dB) |",
              "|---|---|---|---|---|---|---|"]
    lines += [f"| {x['ul_action']} | {x['n']} | {x['sla_met_count']}/{x['n']} | {x['sla_met_wilson95_low']} | "
              f"{x['goodput_mbps_median']} | {x['loss_median']} | {x['ul_snr_db_mean']} |" for x in win["ul_pooled_over_dl_quotas"]]
    lines += ["", "## Per pass", "", "| Pass | First cell (UTC) | Override cells meeting SLA | LA cells meeting SLA | "
              "LA UL SNR (dB) | LA UL MCS |", "|---|---|---|---|---|---|"]
    lines += [f"| {x['pass']} | {x['first_cell'][:19]} | {x['override_sla_met']}/{x['override_cells']} | {x['la_sla_met']} | "
              f"{x['la_ul_snr_db_mean']} | {x['la_ul_mcs_mean']} |" for x in win["per_pass_overview"]]
    lines += ["", "## Incidents", ""]
    incidents = [e for e in events if e["event"] in ("unhealthy", "restart_n3iwue", "abort", "resume",
                                                       "un3gpp_slice_restored", "recovered")]
    lines += [f"- {e['at']} {e['event']}: " + json.dumps({k: v for k, v in e.items() if k not in ('at', 'event')})
              for e in incidents] or ["- none"]
    flagged = [c for c in cells if c.get("incidents")]
    if flagged:
        lines += ["", "Cells followed by an incident (kept in the statistics):", ""]
        lines += [f"- cell {c['index']} (pass {c['pass']}, {label(c)}): {'; '.join(c['incidents'])}" for c in flagged]
    if errors:
        lines += ["", "Cells with errors (excluded; re-run on resume when applicable):", ""]
        lines += [f"- cell {c['index']} (pass {c['pass']}, {label(c)}): {c['error']}" for c in errors]
    lines += ["", "## Per-action table", "",
              "| Action | n | SLA met | " + " | ".join(f"{m.name} median (worst)" for m in sla)
              + " | UL SNR (dB) | UL MCS obs | DL SINR (dB) | DL PRB (%) |",
              "|---|---|---|" + "---|" * len(sla) + "---|---|---|---|"]
    for x in rows:
        lines.append(f"| {x['action']} | {x['n_valid']} | {x['sla_met_count']}/{x['n_valid']} | "
                     + " | ".join(f"{x[f'{m.name}_median']} ({x[f'{m.name}_worst']})" for m in sla)
                     + f" | {x['ul_snr_db_mean']} | {x['ul_mcs_observed_mean']} | {x['kpm_ss_sinr_db_mean']} | "
                       f"{x['kpm_prb_tot_dl_pct_mean']} |")
    lines += ["", "## Figures", ""] + [f"![{f}]({f})" for f in figs]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("--no-figures", action="store_true")
    args = parser.parse_args()
    cells, events, metadata = load(args.run)
    service = ServiceId(metadata["service"])
    flag_incidents(cells, events)
    rows = per_action(cells, service)
    win = winners(rows, [c for c in cells if "error" not in c and not c.get("skipped")], metadata["passes"])
    out = args.run / "analysis"
    out.mkdir(exist_ok=True)
    with (out / "cells.csv").open("w", newline="", encoding="utf-8") as handle:
        columns = list(dict.fromkeys(k for c in cells for k in c))
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in c.items()} for c in cells)
    (out / "actions.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    with (out / "actions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows({**x, "passes": " ".join(map(str, x["passes"]))} for x in rows)
    (out / "winners.json").write_text(json.dumps(win, indent=2) + "\n", encoding="utf-8")
    figs = [] if args.no_figures else figures(rows, cells, service, win, out, args.run)
    (out / "report.md").write_text(report(args.run, service, cells, events, rows, win, figs, metadata), encoding="utf-8")
    print((out / "report.md").read_text().split("## Per-action table")[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
