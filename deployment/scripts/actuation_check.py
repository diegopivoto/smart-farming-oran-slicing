#!/usr/bin/env python3
"""Three-minute check that every O-RAN radio command lands as intended, before a long measurement.

Usage: actuation_check.py --service drone [--output <dir>]
Checks, reading back over E2 (BS readback for UN3GPP, gNB outcome for OAI):
  1. UL override MCS a / RB b  -> override on, mcs_cmd = a, rb_cmd = b
  2. UL override MCS c / RB d  -> mcs_cmd = c, rb_cmd = d
  3. release (sweep path)      -> override off and rb_cmd back to the full band (LA_DEFAULT_RB)
  4. DL PRB quota of the slice -> APPLIED with the requested quota
Exit 0 only if all pass. Writes actuation-check.json when --output is given.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "deployment/scripts"))
import calibration_sweep as sweep_mod  # noqa: E402
from calibration_sweep import SOCKET, Sweep  # noqa: E402

from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId  # noqa: E402
from smartfarm.e2.control_client import E2ControlClient  # noqa: E402


async def readback(e2: E2ControlClient, seconds: float = 3.0) -> Any:
    await asyncio.sleep(seconds)
    snap = e2.snapshot_for("un3gpp")
    return snap.un3gpp_ues[0] if snap and snap.un3gpp_ues else None


async def run(args: argparse.Namespace) -> int:
    service = ServiceId(args.service)
    sweep_mod.SERVICE = service
    sweep_mod.UN3GPP_CLOCK_OFFSET_NS = sweep_mod.measure_pc2_offset_ns()
    bounds = SERVICE_CATALOG[service].radio_bounds
    rbs = sorted(bounds.ul_rb)
    checks: list[dict[str, Any]] = []
    out = args.output or PROJECT / "experiments/checks" / datetime.now().strftime("%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    ns = argparse.Namespace(output=out, settle_s=0, cell_s=0, rate_bps=None, max_outage_s=0)
    async with E2ControlClient(SOCKET) as e2:
        for _ in range(50):
            if e2.snapshot_for("un3gpp") and e2.snapshot_for("oai"):
                break
            await asyncio.sleep(0.2)
        sw = Sweep(ns, e2)
        for mcs, rb in ((max(bounds.ul_mcs_min, 8), rbs[-1]), (max(bounds.ul_mcs_min, 10), rbs[0])):
            status = await sw.ul(mcs, rb)
            ue = await readback(e2)
            ok = bool(ue and ue.override and ue.mcs_commanded == mcs and ue.rb_commanded == rb)
            checks.append({"check": f"ul override MCS {mcs} / {rb} RB", "status": status, "ok": ok,
                           "readback": None if ue is None else {"override": ue.override, "mcs_cmd": ue.mcs_commanded,
                                                                 "rb_cmd": ue.rb_commanded, "mcs_obs": ue.mcs_observed}})
        status = await sw.ul(None, None)
        ue = await readback(e2)
        ok = bool(ue and not ue.override and ue.rb_commanded == sweep_mod.LA_DEFAULT_RB)
        checks.append({"check": f"release -> link adaptation on {sweep_mod.LA_DEFAULT_RB} RB", "status": status,
                       "ok": ok, "readback": None if ue is None else {"override": ue.override, "rb_cmd": ue.rb_commanded,
                                                                      "la_mcs": ue.la_mcs}})
        prb = min(bounds.dl_prb)
        status = await sw.quota(prb)
        q = sw.last_quota
        ok = status.endswith("APPLIED") and getattr(q, "applied_prb", prb) == prb
        checks.append({"check": f"DL quota {prb} PRB", "status": status, "ok": ok})
        await sw.quota(106)  # back to the default quota
    result = {"at": datetime.now(UTC).isoformat(), "service": service.value,
              "pc2_clock_offset_s": sweep_mod.UN3GPP_CLOCK_OFFSET_NS / 1e9, "checks": checks,
              "all_ok": all(c["ok"] for c in checks)}
    (out / "actuation-check.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    for c in checks:
        print(("PASS " if c["ok"] else "FAIL ") + c["check"] + f"  [{c['status']}]  {c.get('readback') or ''}")
    print("ALL PASS" if result["all_ok"] else "SOME CHECKS FAILED", "->", out / "actuation-check.json")
    return 0 if result["all_ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--service", choices=[s.value for s in ServiceId], default="drone")
    parser.add_argument("--output", type=Path)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
