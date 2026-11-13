#!/usr/bin/env python3
"""Joint E2 gate: one xApp controls the OAI gNB (DL PRB quota) and the UN3GPP BS (UL MCS/RB) through the RIC.

Requires the RIC, the OAI E2-SF gNB, the UN3GPP research proxy on PC2 and smartfarm_sf_control on PC1.
UL load for the UN3GPP confirmation comes from iperf3 on PC3 towards the BS tun0. Every event and check is
written to <output>/e2-dual-gate.jsonl.
"""

import argparse
import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from smartfarm.contracts.radio import (
    PrbQuotaAction,
    QuotaOutcome,
    QuotaRollback,
    Un3gppOutcome,
    Un3gppRelease,
    Un3gppUlAction,
)
from smartfarm.contracts.service import Snssai
from smartfarm.e2.control_client import ActionIdAllocator, E2ControlClient


def iperf_ul(seconds: int, result: dict[str, Any]) -> None:
    cmd = f"iperf3 -c 20.20.0.10 -B 20.20.0.11 -t {seconds} -J"
    done = subprocess.run(("ssh", "-o", "BatchMode=yes", "smartfarm-pc3", cmd), capture_output=True,  # noqa: S603
                          text=True, timeout=seconds + 30, check=False)
    try:
        result["mbps"] = json.loads(done.stdout)["end"]["sum_received"]["bits_per_second"] / 1e6
    except (json.JSONDecodeError, KeyError):
        result["error"] = done.stderr[-200:]


async def run(out: Path) -> bool:
    log = (out / "e2-dual-gate.jsonl").open("a", encoding="utf-8")
    checks: list[tuple[str, bool]] = []

    def record(event: dict[str, Any]) -> None:
        log.write(json.dumps({"logged_at_ns": time.time_ns(), **event}, sort_keys=True, default=str) + "\n")
        log.flush()

    def check(name: str, ok: bool, **extra: Any) -> None:
        checks.append((name, ok))
        record({"type": "check", "check": name, "passed": ok, **extra})

    run_id, ids = uuid4(), ActionIdAllocator()
    subprocess.run(("ssh", "-o", "BatchMode=yes", "smartfarm-pc2",  # noqa: S603
                    "pkill -x iperf3; iperf3 -s -B 20.20.0.10 -D"), check=False)
    async with E2ControlClient(Path("/run/user/1000/smartfarm-sf.sock"),
                               sink=lambda e: record({"type": "xapp", "event": e})) as client:
        deadline = time.monotonic() + 10
        while (client.snapshot_for("oai") is None or client.snapshot_for("un3gpp") is None) and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        check("both_nodes_reporting", client.snapshot_for("oai") is not None and client.snapshot_for("un3gpp") is not None)

        # OAI: DL PRB quota through E2
        quota = PrbQuotaAction(action_id=ids.next(), run_id=run_id, snssai=Snssai(sst=1, sd="000001"),
                               prb_quota=52, ttl_ms=30000, issued_at_ns=time.time_ns())
        t0 = time.monotonic()
        q = await client.apply(quota)
        assert isinstance(q, QuotaOutcome)
        check("oai_quota_applied", q.status.value == "APPLIED" and q.applied_prb == 52,
              latency_ms=(time.monotonic() - t0) * 1e3)

        # UN3GPP: UL MCS/RB through E2 under UL load
        load: dict[str, Any] = {}
        worker = threading.Thread(target=iperf_ul, args=(25, load))
        worker.start()
        await asyncio.sleep(2)
        results = []
        for mcs, rb in ((9, 66), (11, 132)):
            action = Un3gppUlAction(action_id=ids.next(), run_id=run_id, ue=1, mcs=mcs, number_rb=rb,
                                    ttl_ms=30000, issued_at_ns=time.time_ns())
            t0 = time.monotonic()
            first = await client.apply(action)
            final = await client.wait_un3gpp_final(action.action_id, timeout_s=15)
            elapsed = (time.monotonic() - t0) * 1e3
            await asyncio.sleep(2)
            snap = client.snapshot_for("un3gpp")
            view = snap.un3gpp_ues[0] if snap and snap.un3gpp_ues else None
            ok = (isinstance(first, Un3gppOutcome) and final.status in ("CONFIRMED", "CONSISTENT") and view is not None
                  and view.mcs_commanded == mcs and view.rb_commanded == rb and view.rb_observed == rb)
            check(f"un3gpp_mcs{mcs}_rb{rb}", ok, first=first.model_dump(), final=final.model_dump(),
                  view=view.model_dump() if view else None, confirm_ms=elapsed)
            results.append((mcs, rb, final.status, elapsed, view.snr_db if view else None))
            if mcs == 9:
                dup = await client.apply(action)
                check("un3gpp_duplicate_replayed", isinstance(dup, Un3gppOutcome) and dup.duplicate
                      and dup.mac_seq == final.mac_seq, outcome=dup.model_dump())
        worker.join()
        record({"type": "load", **load})

        stale = Un3gppUlAction(action_id=ids.next(), run_id=run_id, ue=1, mcs=3, number_rb=33, ttl_ms=1000,
                               issued_at_ns=time.time_ns() - 15_000_000_000)  # beyond ttl + 5 s clock-skew tolerance
        s = await client.apply(stale)
        check("un3gpp_stale_rejected", isinstance(s, Un3gppOutcome) and s.status == "REJECTED_STALE")

        rel = Un3gppRelease(action_id=ids.next(), run_id=run_id, ue=1, ttl_ms=30000, issued_at_ns=time.time_ns())
        await client.apply(rel)
        r = await client.wait_un3gpp_final(rel.action_id, timeout_s=10)
        check("un3gpp_released", r.status == "RELEASED")

        back = await client.apply(QuotaRollback(action_id=ids.next(), run_id=run_id, target_action_id=quota.action_id,
                                                rollback_token=q.rollback_token, ttl_ms=30000,
                                                issued_at_ns=time.time_ns()))
        assert isinstance(back, QuotaOutcome)
        check("oai_quota_rolled_back", back.status.value == "ROLLED_BACK" and back.applied_prb == 106)
    subprocess.run(("ssh", "-o", "BatchMode=yes", "smartfarm-pc2", "pkill -x iperf3"), check=False)  # noqa: S603
    passed = all(ok for _, ok in checks)
    record({"type": "gate", "event": "end", "passed": passed, "un3gpp_results": results, "ul_load": load})
    log.close()
    print(json.dumps({"passed": passed, "checks": checks, "un3gpp": results, "ul_load_mbps": load.get("mbps")}))
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    return 0 if asyncio.run(run(args.output)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
