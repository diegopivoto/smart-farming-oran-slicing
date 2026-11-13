#!/usr/bin/env python3
"""Run slice transitions of the lab UEs and record one JSON line per transition (results dataset).

    slice_switch.py --sequence crop --handset manual-apn --output experiments/slice-switch/<ts>
    slice_switch.py --sequence weather,crop,drone --repeat 5 --handset none --output ...   # UN3GPP latency series

The UN3GPP UE is commanded through the 5G network (PC3 node agent at its PDU-session address); the OnePlus either
follows the operator selecting the service APN (`manual-apn`) or is left alone (`none`). Writes transitions.jsonl,
run-metadata.json and summary.{json,md} into --output, plus per transition (transition-NN-<service>/) the AMF/SMF log
lines of the transition (core-*.log with IMSIs replaced by the UE role; core-*.raw.log mode 0600 unmodified), the
relevant terminals (PC3 N3IWUE and node agent, PC2 N3IWF) and screenshots of the PC2/PC3 displays. Terminal captures
and screenshots use the management link (documentation only; all control goes through the 5G network).
Terminal and file output are in English.
"""

import argparse
import asyncio
import hashlib
import json
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any

from smartfarm.baseline.endpoints import EndpointRole
from smartfarm.baseline.open5gs_sessions import active_sessions, load_identities, read_smf_log
from smartfarm.contracts.service import ServiceId
from smartfarm.core.evidence import EvidenceCursor, Open5gsLogEvidenceProvider
from smartfarm.core.open5gs_config import validate_slices
from smartfarm.orchestrator.slice_coordinator import ManualApnHandset, SliceCoordinator
from smartfarm.orchestrator.un3gpp_slice import Un3gppSliceClient, agent_transport, load_agent_key

PROJECT = Path(__file__).resolve().parents[2]
AGENT_KEY = Path.home() / ".config/smartfarm/node-agent.key"
GNB_CONF = Path.home() / "open5gs-gnb.sa.band78.fr1.106PRB.usrpb210.conf"
AMF_PATCH = PROJECT / "deployment/open5gs/0001-amf-select-slice-by-dnn.patch"
TIMING_KEYS = (
    "un3gpp_switch_core_s", "un3gpp_interruption_core_s", "handset_interruption_core_s", "un3gpp_accepted_s",
    "un3gpp_core_confirmed_s", "handset_core_confirmed_s", "total_s", "probe_rtt_avg_ms",
)


TERMINALS = (("smartfarm-pc3", "n3iwue"), ("smartfarm-pc3", "sf-agent"), ("smartfarm-pc2", "n3iwf"))
SCREENS = (("smartfarm-pc2", "pc2"), ("smartfarm-pc3", "pc3"))
# terminal windows captured maximized (legible), by window title: (host, title, file label)
WINDOWS = (("smartfarm-pc2", "n3iwf", "pc2-n3iwf"), ("smartfarm-pc2", "e2proxy", "pc2-e2proxy"),
           ("smartfarm-pc2", "modem", "pc2-modem"), ("smartfarm-pc3", "n3iwue", "pc3-n3iwue"),
           ("smartfarm-pc3", "Smart Farming node agent (5G)", "pc3-sf-agent"),
           ("smartfarm-pc3", "gr-inatelxg", "pc3-modem"))
CAPTURE_WINDOW = PROJECT / "deployment/lab/capture_window.py"


def capture_evidence(directory: Path, cursor: EvidenceCursor, record: dict[str, Any]) -> list[str]:
    """Store the transition's core log lines, terminal captures and screenshots; returns capture problems."""

    directory.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    identities = load_identities()
    roles = {identity.supi: f"imsi-{role.value.upper()}" for role, identity in identities.items()}
    for name, log_cursor in (("amf", cursor.amf), ("smf", cursor.smf)):
        lines = [line for line in log_cursor.read()[0] if any(supi in line for supi in roles)]
        raw = directory / f"core-{name}.raw.log"
        raw.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        raw.chmod(0o600)
        public = []
        for line in lines:
            for supi, label in roles.items():
                line = line.replace(supi, label)
            public.append(line)
        (directory / f"core-{name}.log").write_text("".join(f"{line}\n" for line in public), encoding="utf-8")
    for host, session in TERMINALS:
        done = subprocess.run(("ssh", "-o", "BatchMode=yes", host, f"sudo -n tmux capture-pane -p -J -S -300 -t {session}"),
                              capture_output=True, text=True, timeout=20, check=False)
        if done.returncode == 0:
            (directory / f"{host.split('-')[-1]}-{session}.txt").write_text(done.stdout, encoding="utf-8")
        else:
            problems.append(f"terminal {host}:{session}: {done.stderr.strip()[-120:]}")
    for host, label in SCREENS:
        xwd = directory / f"{label}-screen.xwd"
        with xwd.open("wb") as handle:
            done = subprocess.run(("ssh", "-o", "BatchMode=yes", host,
                                   "DISPLAY=:0 XAUTHORITY=$HOME/.Xauthority xwd -root -silent"),
                                  stdout=handle, stderr=subprocess.PIPE, timeout=30, check=False)
        converted = done.returncode == 0 and subprocess.run(
            ("convert", f"xwd:{xwd}", str(directory / f"{label}-screen.png")), capture_output=True, check=False
        ).returncode == 0
        xwd.unlink(missing_ok=True)
        if not converted:
            problems.append(f"screenshot {label} failed")
    for host, title, label in WINDOWS:
        xwd = directory / f"{label}-window.xwd.gz"
        with CAPTURE_WINDOW.open("rb") as script, xwd.open("wb") as handle:
            done = subprocess.run(("ssh", "-o", "BatchMode=yes", host,
                                   f"export DISPLAY=:0 XAUTHORITY=$HOME/.Xauthority; python3 - '{title}' | gzip -1"),
                                  stdin=script, stdout=handle, stderr=subprocess.PIPE, timeout=40, check=False)
        converted = done.returncode == 0 and subprocess.run(
            ("bash", "-c", f"gunzip -c '{xwd}' | convert xwd:- '{directory / f'{label}-window.png'}'"),
            capture_output=True, check=False
        ).returncode == 0
        xwd.unlink(missing_ok=True)
        if not converted:
            problems.append(f"window {label} ({title!r}) failed")
    (directory / "record.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return problems


def un3gpp_address() -> IPv4Address:
    """Current 5G address of the UN3GPP UE only (the phone may legitimately have no session yet)."""

    supi = load_identities()[EndpointRole.UN3GPP_UE].supi
    candidates = [s for s in active_sessions(read_smf_log()).get(supi, []) if s.ipv4 in IPv4Network("10.45.0.0/16")]
    if not candidates:
        raise ValueError("Open5GS SMF has no active PDU session for the UN3GPP UE")
    return max(candidates, key=lambda session: session.order).ipv4


def metadata(args: argparse.Namespace) -> dict[str, Any]:
    commit = subprocess.run(("git", "-C", str(PROJECT), "rev-parse", "HEAD"), capture_output=True, text=True,
                            check=False).stdout.strip()
    etc = Path("/etc/open5gs")
    report = validate_slices(amf=etc / "amf.yaml", smf=etc / "smf.yaml", nssf=etc / "nssf.yaml",
                             upf=etc / "upf.yaml", gnb=GNB_CONF if GNB_CONF.exists() else None)
    return {
        "schema": "smartfarm.slice_switch_run.v1",
        "started_at": datetime.now(UTC).isoformat(),
        "git_commit": commit,
        "sequence": args.sequence,
        "repeat": args.repeat,
        "handset_mode": args.handset,
        "hold_s": args.hold_s,
        "pause_s": args.pause_s,
        "core_config_valid": report.passed,
        "core_config_errors": list(report.errors),
        "core_config_sha256": dict(report.checked),
        "amf_patch_sha256": hashlib.sha256(AMF_PATCH.read_bytes()).hexdigest() if AMF_PATCH.exists() else None,
        "control_path": "PC1 -> UPF -> N3IWF -> IPsec -> PC3 node agent (vrf-n3iwue:9700)",
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    def stats(values: list[float]) -> dict[str, float | int | None]:
        if not values:
            return {"n": 0, "min": None, "median": None, "p95": None, "max": None}
        ordered = sorted(values)
        p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
        return {"n": len(ordered), "min": ordered[0], "median": round(statistics.median(ordered), 3),
                "p95": p95, "max": ordered[-1]}

    groups: dict[str, list[dict[str, Any]]] = {"all": records}
    for record in records:
        groups.setdefault(record["service"], []).append(record)
    summary: dict[str, Any] = {}
    for name, items in groups.items():
        confirmed = [r for r in items if r["outcome"] == "slice_confirmed"]
        switched = [r for r in items if r.get("un3gpp_request_sent")]  # timings also kept for probe failures
        summary[name] = {
            "transitions": len(items),
            "confirmed": len(confirmed),
            "failed_safe": len(items) - len(confirmed),
            "causes": sorted({r["cause"] for r in items if r["cause"]}),
            **{key: stats([float(r[key]) for r in switched if r.get(key) is not None]) for key in TIMING_KEYS},
        }
    return summary


def markdown(summary: dict[str, Any]) -> str:
    lines = [
        "| Group | Transitions | Confirmed | Failed | UN3GPP switch, core (s) median/p95 | UN3GPP interruption, core (s) "
        "median/p95 | Handset interruption, core (s) median/p95 | UE-to-UE RTT avg (ms) median |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, s in summary.items():
        def mp(key: str) -> str:
            return "n/a" if s[key]["median"] is None else f"{s[key]['median']}/{s[key]['p95']}"
        lines.append(f"| {name} | {s['transitions']} | {s['confirmed']} | {s['failed_safe']} | {mp('un3gpp_switch_core_s')} | "
                     f"{mp('un3gpp_interruption_core_s')} | {mp('handset_interruption_core_s')} | "
                     f"{s['probe_rtt_avg_ms']['median'] if s['probe_rtt_avg_ms']['median'] is not None else 'n/a'} |")
    return "\n".join(lines) + "\n"


async def run(args: argparse.Namespace) -> int:
    out: Path = args.output
    out.mkdir(parents=True, exist_ok=True)
    (out / "run-metadata.json").write_text(json.dumps(metadata(args), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    evidence = Open5gsLogEvidenceProvider(load_identities())
    client = Un3gppSliceClient(agent_transport(un3gpp_address, load_agent_key(AGENT_KEY)))
    handset = ManualApnHandset() if args.handset == "manual-apn" else None
    coordinator = SliceCoordinator(evidence, client, handset)
    services = [ServiceId(s) for s in args.sequence.split(",")] * args.repeat
    records: list[dict[str, Any]] = []
    with (out / "transitions.jsonl").open("a", encoding="utf-8") as log:
        for index, service in enumerate(services, 1):
            print(f"[{index}/{len(services)}] transition to {service.value} ...", flush=True)
            capture_cursor = evidence.cursor()
            record = await coordinator.transition(service, hold_s=args.hold_s, handset_timeout_s=args.handset_timeout_s)
            record["sequence_index"] = index
            evidence_dir = out / f"transition-{index:02d}-{service.value}"
            record["evidence_dir"] = evidence_dir.name
            record["evidence_problems"] = capture_evidence(evidence_dir, capture_cursor, record)
            records.append(record)
            log.write(json.dumps(record, sort_keys=True) + "\n")
            log.flush()
            print(f"    {record['outcome']} total={record['total_s']} s un3gpp={record.get('un3gpp_ipv4')} "
                  f"handset={record.get('handset_ipv4')} probe={record.get('probe_received')}/{record.get('probe_sent')} "
                  f"rtt_avg={record.get('probe_rtt_avg_ms')} ms cause={record['cause']}", flush=True)
            if index < len(services):
                time.sleep(args.pause_s)
    summary = summarize(records)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "summary.md").write_text(markdown(summary), encoding="utf-8")
    print(markdown(summary))
    return 0 if all(r["outcome"] == "slice_confirmed" for r in records) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sequence", required=True, help="comma-separated services: weather,crop,drone")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--handset", choices=("manual-apn", "none"), default="none")
    parser.add_argument("--hold-s", type=float, default=2.0)
    parser.add_argument("--pause-s", type=float, default=5.0)
    parser.add_argument("--handset-timeout-s", type=float, default=180.0)
    parser.add_argument("--output", type=Path, required=True)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
