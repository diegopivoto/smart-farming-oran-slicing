#!/usr/bin/env python3
"""Bounds the clock-offset error of the weather AoI of a calibration run and checks whether it can flip any SLA verdict.

The receiver (phone) estimates its offset to the generator (UN3GPP UE) by an NTP-like exchange and keeps the
minimum-RTT sample, so the offset error is at most RTT/2 whatever the path asymmetry. Every receiver window carries
that RTT (clock_rtt_ns); this script takes the windows of the run from the collector files, reports the RTT and the
bound, and compares the bound with the distance of each cell's AoI p95 to the SLA threshold.

    aoi_clock_bound.py experiments/calibration/<weather run> [--collected experiments/traffic/collected]
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
AOI_P95_MAX_S = 2.0  # weather SLA (SERVICE_CATALOG)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("--collected", type=Path, default=PROJECT / "experiments/traffic/collected")
    args = parser.parse_args()
    cells = [json.loads(line) for line in (args.run / "cells.jsonl").read_text().splitlines()]
    done = [c for c in cells if c.get("finished_at")]
    t0 = datetime.fromisoformat(cells[0]["started_at"]).timestamp() * 1e9
    t1 = datetime.fromisoformat(done[-1]["finished_at"]).timestamp() * 1e9
    rtt_ms = []
    for f in args.collected.glob("*.jsonl"):
        for line in f.read_text(errors="replace").splitlines():
            if '"weather"' not in line or "clock_rtt_ns" not in line:
                continue
            w = json.loads(line)
            if w.get("service") == "weather" and t0 <= w.get("at_ns", 0) <= t1 and w.get("clock_rtt_ns"):
                rtt_ms.append(w["clock_rtt_ns"] / 1e6)
    if not rtt_ms:
        sys.exit("no weather receiver windows with clock samples in the run interval")
    rows = list(csv.DictReader((args.run / "analysis/cells.csv").open()))
    aoi = [float(r["svc_aoi_p95"]) for r in rows if r.get("svc_aoi_p95") not in ("", None)]
    bound_ms = max(rtt_ms) / 2
    closest = min(abs(a - AOI_P95_MAX_S) for a in aoi)
    q = sorted(rtt_ms)
    out = {"schema": "smartfarm.aoi_clock_bound.v1", "run": str(args.run), "windows": len(rtt_ms),
           "clock_rtt_ms": {"min": round(q[0], 1), "median": round(statistics.median(q), 1),
                            "p95": round(q[int(0.95 * (len(q) - 1))], 1), "max": round(q[-1], 1)},
           "offset_error_bound_ms": round(bound_ms, 1), "cells_with_aoi": len(aoi),
           "aoi_p95_max_s": round(max(aoi), 3), "aoi_p95_median_s": round(statistics.median(aoi), 3),
           "sla_aoi_p95_max_s": AOI_P95_MAX_S, "closest_cell_to_threshold_s": round(closest, 3),
           "verdict_can_flip": closest * 1000 <= bound_ms}
    (args.run / "analysis/aoi-clock-bound.json").write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
