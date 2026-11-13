"""Service SLA evaluation of the receiver records of one traffic run (moved from deployment/scripts/traffic_run.py,
which re-exports it): the calibration sweep, the online agent and the demo's O-RAN agent judge a step the same way.

Steady-state statistics (goodput, PDR/loss, jitter, gaps, AoI) drop the first and the last receiver window. A metric
that cannot be computed from the records is None, and the verdict `sla_met` is then None (undetermined) unless another
requirement already failed.
"""

from __future__ import annotations

import statistics
from typing import Any

from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


def evaluate(service: ServiceId, records: list[dict[str, Any]]) -> dict[str, Any]:
    windows = sorted((r for r in records if r.get("kind") == "receiver_window" and r.get("received")),
                     key=lambda r: r["at_ns"])
    steady = windows[1:-1] if len(windows) > 2 else windows
    objects = [r for r in records if r.get("kind") == "receiver_object"]
    generator = next((r for r in records if r.get("kind") == "generator_summary"), {})
    measured: dict[str, Any] = {"windows": len(windows), "steady_windows": len(steady)}
    if service is ServiceId.CROP:
        complete = [o for o in objects if o.get("sha256_valid") is not None]
        # nothing completed although the generator sent: zero object goodput, a failure rather than "no data"
        no_object = 0.0 if not complete and generator.get("sent_bytes") else None
        measured |= {
            "objects_received": len(complete),
            "objects_aborted_at_deadline": generator.get("aborted_objects"),
            "goodput": statistics.mean([o["goodput_bps"] for o in complete]) if complete else no_object,
            "object_transfer_time": max((o["object_transfer_time_s"] or o["receive_time_s"]) for o in complete) if complete else None,
            "object_transfer_time_median": statistics.median([o["object_transfer_time_s"] or o["receive_time_s"] for o in complete]) if complete else None,
            "sha256_valid_ratio": sum(o["sha256_valid"] for o in complete) / len(complete) if complete else None,
            "accepted_corrupt_objects": sum(not o["sha256_valid"] for o in complete),
            "retransmitted_bytes_ratio": (generator["tcp_bytes_retrans"] / generator["tcp_bytes_sent"])
            if generator.get("tcp_bytes_sent") else None,
        }
    else:
        total = windows[-1] if windows else {}
        measured |= {
            "goodput": statistics.mean([w["goodput_bps"] for w in steady]) if steady else None,
            "goodput_p10": sorted(w["goodput_bps"] for w in steady)[int(0.1 * (len(steady) - 1))] if steady else None,
            "pdr": round(sum(w["received"] for w in steady) / max(1, sum(w["expected_in_window"] for w in steady)), 5)
            if steady else None,
            "packet_loss_ratio": round(1 - sum(w["received"] for w in steady)
                                       / max(1, sum(w["expected_in_window"] for w in steady)), 5) if steady else None,
            "pdr_total_including_edges": total.get("pdr_total"),
            "jitter_p95": p95([w["jitter_s"] for w in steady]),
            "max_receive_gap": max((w["max_receive_gap_s"] or 0) for w in steady) if steady else None,
            "max_freshness_gap": max((w["max_receive_gap_s"] or 0) for w in steady) if steady else None,
            "aoi_p95": p95([w["aoi_p95_s"] for w in steady if w.get("aoi_p95_s") is not None]),
            "delay_mean": statistics.mean([w["delay_mean_s"] for w in steady if w.get("delay_mean_s") is not None])
            if any(w.get("delay_mean_s") is not None for w in steady) else None,
            "generator_sent_packets": generator.get("sent_packets"),
            "generator_offered_bps": generator.get("offered_bps"),
        }
    checks = []
    for metric in SERVICE_CATALOG[service].sla:
        value = measured.get(metric.name)
        ok = None if value is None else {"ge": value >= metric.threshold, "le": value <= metric.threshold,
                                          "eq": value == metric.threshold}[metric.operator]
        checks.append({"metric": metric.name, "operator": metric.operator, "threshold": metric.threshold,
                       "unit": metric.unit, "value": value, "met": ok})
    return {"service": service.value, "measured": measured, "sla": checks,
            "sla_met": False if any(c["met"] is False for c in checks)
            else (True if all(c["met"] for c in checks) else None)}
