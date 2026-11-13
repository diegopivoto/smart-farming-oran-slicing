#!/usr/bin/env python3
"""Join the PRB quota gate log with KPM telemetry into per-phase statistics.

Inputs are the gate JSONL written by ``deployment/oai/test_prb_quota.py`` and
the KPM JSONL extracted from the bridge journal. For each dwell phase the
report gives KPM throughput, PRB utilisation, RLC delay and RB-weighted PDSCH
MCS over the phase window (the first ``--settle-s`` seconds are excluded so a
new quota has taken effect), plus control-plane latency and check results.
Missing series are reported as null, never as zero.
"""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

SLICE_SD = 1
# TS 38.133 report mappings used by the OAI KPM distributions
RSRP_DBM = staticmethod(lambda level: level - 157 + 0.5)  # SS-RSRP level n: [n-157, n-156) dBm
SINR_DB = staticmethod(lambda level: level / 2 - 23 + 0.25)  # SS-SINR level n: 0.5 dB steps from -23 dB


def _distribution_mean(series: list[tuple[int, Any, dict[str, Any]]], lo: int, hi: int,
                       mapping: Any) -> dict[str, float | int | None]:
    weights: dict[int, float] = defaultdict(float)
    for at, v, labels in series:
        if lo <= at <= hi and v and labels.get("dist_bin_x") is not None:
            weights[int(labels["dist_bin_x"])] += float(v)
    total = sum(weights.values())
    if not total:
        return {"samples": 0, "mean": None, "min": None, "max": None}
    values = [mapping(level) for level in weights]
    return {"samples": int(total),
            "mean": round(sum(mapping(k) * w for k, w in weights.items()) / total, 2),
            "min": round(min(values), 2), "max": round(max(values), 2)}


def _load(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "median": None, "mean": None, "p05": None, "p95": None}
    ordered = sorted(values)

    def pct(q: float) -> float:
        return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]

    return {
        "n": len(values),
        "median": round(statistics.median(values), 3),
        "mean": round(statistics.fmean(values), 3),
        "p05": round(pct(0.05), 3),
        "p95": round(pct(0.95), 3),
    }


def phase_windows(gate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    starts: dict[str, dict[str, Any]] = {}
    windows = []
    for row in gate:
        if row.get("type") != "phase":
            continue
        if row["event"] == "start":
            starts[row["phase"]] = row
        elif row["event"] == "end" and row["phase"] in starts:
            start = starts.pop(row["phase"])
            windows.append({"phase": row["phase"], "prb": row["prb"],
                            "start_ns": start["logged_at_ns"], "end_ns": row["logged_at_ns"],
                            "sent_packets_end": row.get("sent_packets")})
    return windows


def kpm_series(kpm: list[dict[str, Any]]) -> dict[str, list[tuple[int, Any, dict[str, Any]]]]:
    series: dict[str, list[tuple[int, Any, dict[str, Any]]]] = defaultdict(list)
    for envelope in kpm:
        at = int(envelope.get("collected_at_ns", 0))
        for m in envelope.get("measurements", []):
            series[m["name"]].append((at, m.get("value"), m.get("labels", {}) | {"ue_id": envelope.get("ue_id")}))
    return series


def summarize(gate: list[dict[str, Any]], kpm: list[dict[str, Any]], settle_s: float) -> dict[str, Any]:
    series = kpm_series(kpm)
    drains = [r for r in gate if r.get("type") == "drain"]  # one per dwell, in order
    payload = next((r.get("payload_bytes") for r in gate if r.get("type") == "load"), 1200) or 1200
    phases = []
    previous_sent = 0
    for index, w in enumerate(phase_windows(gate)):
        drain = drains[index]["drain_s"] if index < len(drains) and drains[index]["phase"] == w["phase"] else None
        offered = None
        if w.get("sent_packets_end") is not None:
            duration = (w["end_ns"] - w["start_ns"]) / 1e9
            offered = round((w["sent_packets_end"] - previous_sent) * payload * 8 / duration / 1e6, 2)
            previous_sent = w["sent_packets_end"]
        lo, hi = w["start_ns"] + int(settle_s * 1e9), w["end_ns"]

        def ue_values(name: str) -> list[float]:
            return [float(v) for at, v, labels in series.get(name, [])
                    if lo <= at <= hi and v is not None and labels.get("subscription_sd") == SLICE_SD]

        mcs_weight: dict[int, float] = defaultdict(float)
        for at, v, labels in series.get("CARR.PDSCHMCSDist", []):
            if lo <= at <= hi and v and labels.get("dist_bin_z") is not None:
                mcs_weight[int(labels["dist_bin_z"])] += float(v)
        total = sum(mcs_weight.values())
        phases.append({
            **w,
            "window_s": round((hi - lo) / 1e9, 1),
            "ue_thp_dl_kbps": _stats(ue_values("DRB.UEThpDl")),
            "prb_tot_dl_percent": _stats(ue_values("RRU.PrbTotDl")),
            "rlc_sdu_delay_dl_us": _stats(ue_values("DRB.RlcSduDelayDl")),
            "pdcp_sdu_volume_dl_mbit": _stats(ue_values("DRB.PdcpSduVolumeDL")),
            "pdsch_mcs_rb_weighted_mean": round(sum(k * v for k, v in mcs_weight.items()) / total, 2) if total else None,
            "ss_rsrp_dbm": _distribution_mean(series.get("L1M.SS-RSRP", []), lo, hi, RSRP_DBM.__func__),
            "ss_sinr_db": _distribution_mean(series.get("MR.NRScSSSINR", []), lo, hi, SINR_DB.__func__),
            "offered_load_mbps": offered,
            "queue_drain_s": drain,
        })

    outcomes = [r for r in gate if r.get("type") == "outcome"]
    latencies = [r["latency_ms"] for r in outcomes]
    checks = [r for r in gate if r.get("type") == "check"]
    sentinels = [r for r in gate if r.get("type") == "sentinel"]
    end = next((r for r in reversed(gate) if r.get("type") == "gate" and r.get("event") == "end"), None)
    return {
        "phases": phases,
        "control": {
            "actions": len(outcomes),
            "status_counts": dict(sorted(_count(r["outcome"]["status"] for r in outcomes).items())),
            "outcome_latency_ms": _stats(latencies),
        },
        "checks": {"total": len(checks), "failed": [c["message"] for c in checks if not c["passed"]]},
        "sentinels": {"total": len(sentinels), "failed": [s["phase"] for s in sentinels if not s["passed"]]},
        "gate_passed": bool(end and end.get("passed")),
        "actuator_passed": bool(end and end.get("actuator_passed", end.get("passed"))),
        "rrc_clean": end.get("rrc_clean") if end else None,
        "rrc_audit": next(({k: v for k, v in r.items() if k not in ("events", "type", "logged_at_ns")}
                           for r in gate if r.get("type") == "rrc_audit"), None),
    }


def _count(items: Any) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for item in items:
        counts[item] += 1
    return counts


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "| Phase | Quota (PRB) | Window (s) | Offered load (Mbit/s) | KPM DL throughput median (Mbit/s) | p05–p95 | DL PRB usage median (%) | RLC DL delay median (ms) | Mean MCS (RB-weighted) | Mean RSRP (dBm) | Mean SINR (dB) | Queue drain (s) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for p in report["phases"]:
        thp, prb, delay = p["ue_thp_dl_kbps"], p["prb_tot_dl_percent"], p["rlc_sdu_delay_dl_us"]

        def mbps(v: float | None) -> str:
            return "—" if v is None else f"{v / 1000:.2f}"

        lines.append(
            f"| {p['phase']} | {p['prb']} | {p['window_s']} | {p.get('offered_load_mbps') or '—'} | {mbps(thp['median'])} | "
            f"{mbps(thp['p05'])}–{mbps(thp['p95'])} | {'—' if prb['median'] is None else prb['median']} | "
            f"{'—' if delay['median'] is None else round(delay['median'] / 1000, 2)} | "
            f"{'—' if p['pdsch_mcs_rb_weighted_mean'] is None else p['pdsch_mcs_rb_weighted_mean']} | "
            f"{'—' if p['ss_rsrp_dbm']['mean'] is None else p['ss_rsrp_dbm']['mean']} | "
            f"{'—' if p['ss_sinr_db']['mean'] is None else p['ss_sinr_db']['mean']} | "
            f"{'—' if p.get('queue_drain_s') is None else p['queue_drain_s']} |"
        )
    c = report["control"]
    lines += [
        "",
        f"E2 actions: {c['actions']}; status: {c['status_counts']}; action→outcome read latency "
        f"(ms): median {c['outcome_latency_ms']['median']}, p95 {c['outcome_latency_ms']['p95']}.",
        f"Checks: {report['checks']['total']} (failed: {report['checks']['failed'] or 'none'}); "
        f"sentinels: {report['sentinels']['total']} (failed: {report['sentinels']['failed'] or 'none'}).",
        f"Actuator criteria: {'passed' if report['actuator_passed'] else 'failed'}; "
        f"RRC audit: {report['rrc_audit'] if report['rrc_audit'] else 'not run'}.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gate-log", type=Path, required=True)
    parser.add_argument("--kpm", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="report JSON; a .md table is written beside it")
    parser.add_argument("--settle-s", type=float, default=5.0)
    arguments = parser.parse_args()
    report = summarize(_load(arguments.gate_log), _load(arguments.kpm), arguments.settle_s)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    arguments.output.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(markdown(report))
    return 0 if report["actuator_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
