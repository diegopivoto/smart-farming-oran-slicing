#!/usr/bin/env python3
"""Run one service traffic test UN3GPP UE -> OnePlus through the 5G core and evaluate it against the service SLA.

    traffic_run.py --service crop --duration 60 --output experiments/traffic/<ts>-crop

The generator is started on the UN3GPP UE through the 5G network (PC3 node agent); the phone receiver (Termux,
`sf_traffic.py receive`) and the generator report to the PC1 collector (experiments/traffic/collected/<run_id>.jsonl).
Writes run-metadata.json, records.jsonl (copy of the collected records), summary.json/summary.md (SLA evaluation)
and terminal captures. Steady-state statistics (goodput, PDR/loss, jitter, gaps, AoI) drop the first and the last receiver window; the
whole-run PDR including those edges is kept as `pdr_total_including_edges`. Output in English.
"""

import argparse
import asyncio
import json
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from smartfarm.baseline.endpoints import EndpointRole
from smartfarm.baseline.open5gs_sessions import load_identities
from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId
from smartfarm.contracts.evaluation import evaluate, p95  # noqa: F401 - re-exported (calibration_sweep imports it)
from smartfarm.core.evidence import Open5gsLogEvidenceProvider
from smartfarm.orchestrator.un3gpp_slice import Un3gppSliceClient, agent_transport, load_agent_key

PROJECT = Path(__file__).resolve().parents[2]
COLLECTED = PROJECT / "experiments/traffic/collected"
sys.path.insert(0, str(PROJECT / "deployment/scripts"))
from slice_switch import AGENT_KEY, un3gpp_address  # noqa: E402


def markdown(summary: dict[str, Any]) -> str:
    lines = [f"# Traffic test: {summary['service']} ({summary['run_id']})", "",
             f"SLA met: **{summary['evaluation']['sla_met']}**", "",
             "| Metric | Target | Measured | Met |", "|---|---|---|---|"]
    symbols = {"ge": ">=", "le": "<=", "eq": "=="}
    for c in summary["evaluation"]["sla"]:
        value = c["value"]
        shown = "n/a" if value is None else (f"{value:.4g}" if isinstance(value, float) else str(value))
        lines.append(f"| {c['metric']} | {symbols[c['operator']]} {c['threshold']} {c['unit']} | {shown} | {c['met']} |")
    lines += ["", "Other measurements:", ""]
    lines += [f"- {k}: {v}" for k, v in summary["evaluation"]["measured"].items()]
    return "\n".join(lines) + "\n"


async def run(args: argparse.Namespace) -> int:
    service = ServiceId(args.service)
    profile = SERVICE_CATALOG[service]
    out: Path = args.output
    out.mkdir(parents=True, exist_ok=True)
    evidence = Open5gsLogEvidenceProvider(load_identities())
    phone = evidence.current_session(EndpointRole.ONEPLUS_8T)
    un3gpp = Un3gppSliceClient(agent_transport(un3gpp_address, load_agent_key(AGENT_KEY)))
    status = await un3gpp.status()
    if phone is None or status is None:
        print("ERROR: both UEs need an active PDU session", flush=True)
        return 2
    in_slice = phone.snssai == profile.snssai and phone.dnn == profile.handset_dnn and status.sd == profile.snssai.sd
    run_id = uuid.uuid4().hex
    metadata = {
        "schema": "smartfarm.traffic_run.v1", "run_id": run_id, "service": service.value,
        "started_at": datetime.now(UTC).isoformat(), "duration_s": args.duration,
        "offered_rate_bps": args.rate_bps or profile.offered_rate_bps,
        "un3gpp": {"ipv4": str(status.ipv4), "sd": status.sd, "pdu_session_id": status.pdu_session_id},
        "handset": {"ipv4": str(phone.ipv4), "sd": phone.snssai.sd, "dnn": phone.dnn},
        "both_in_service_slice": in_slice,
        "path": "UN3GPP UE (PC3, vrf-n3iwue) -> N3IWF -> UPF (PC1) -> OAI gNB -> OnePlus (Termux receiver)",
    }
    (out / "run-metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    if not in_slice:
        print(f"WARNING: UEs are not both in the {service.value} slice: {metadata['un3gpp']} {metadata['handset']}",
              flush=True)
    print(f"starting {service.value} for {args.duration} s run_id={run_id} "
          f"{status.ipv4} -> {phone.ipv4}", flush=True)
    print(await un3gpp.start_traffic(service, phone.ipv4, args.duration, run_id, args.rate_bps), flush=True)
    time.sleep(args.duration + args.settle_s)
    collected = COLLECTED / f"{run_id}.jsonl"
    records = [json.loads(line) for line in collected.read_text(encoding="utf-8").splitlines()] if collected.exists() else []
    (out / "records.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8")
    summary = {"run_id": run_id, "service": service.value, "records": len(records), "evaluation": evaluate(service, records)}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "summary.md").write_text(markdown(summary), encoding="utf-8")
    log = subprocess.run(("ssh", "-o", "BatchMode=yes", "smartfarm-pc3", f"cat /tmp/sf-traffic-{run_id}-{service.value}.log"),
                         capture_output=True, text=True, timeout=20, check=False)
    (out / "pc3-generator.log").write_text(log.stdout, encoding="utf-8")
    print(markdown(summary), flush=True)
    return 0 if summary["evaluation"]["sla_met"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--service", choices=[s.value for s in ServiceId], required=True)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--settle-s", type=float, default=8.0)
    parser.add_argument("--rate-bps", type=int, help="override the service offered rate (capacity sweeps)")
    parser.add_argument("--output", type=Path, required=True)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
