#!/usr/bin/env python3
"""Live gate of the OAI per-slice DL PRB quota actuator (plan 3, task 6).

Drives the E2SM-SF/0.1 control xApp through a fixed scenario while a paced
UDP load flows from PC1 to the OnePlus, and records every action, outcome,
readback snapshot and sentinel result as JSON lines in the gate directory.
KPM throughput/PRB evidence is read afterwards from the KPM bridge journal by
``summarize_prb_quota_gate.py``. The script never restarts lab components.
"""

import argparse
import asyncio
import json
import socket
import subprocess
import threading
import time
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from smartfarm.baseline.endpoints import EndpointRole
from smartfarm.baseline.open5gs_sessions import (
    DEFAULT_IDENTITIES,
    DEFAULT_SMF_LOG,
    load_identities,
    read_smf_log,
    resolve_open5gs_endpoints,
)
from smartfarm.contracts.radio import PrbQuotaAction, QuotaOutcome, QuotaRollback, QuotaStatus
from smartfarm.contracts.service import Snssai
from smartfarm.e2.control_client import ActionIdAllocator, E2ControlClient

ONEPLUS_SLICE = Snssai(sst=1, sd="000001")
SEQUENCE = (106, 24, 52, 78, 106)


class GateFailure(RuntimeError):
    """A gate criterion failed; the run stops and the evidence is kept."""


class JsonlLog:
    def __init__(self, path: Path) -> None:
        self._handle = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def __call__(self, event: dict[str, Any]) -> None:
        line = json.dumps({"logged_at_ns": time.time_ns(), **event}, sort_keys=True, default=str)
        with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()

    def close(self) -> None:
        self._handle.close()


class UdpLoad:
    """Paced UDP sender from PC1 to the UE address (traffic leaves through ogstun)."""

    def __init__(self, target: IPv4Address, port: int, mbps: float, payload: int = 1200) -> None:
        self._target = (str(target), port)
        self._interval = payload * 8 / (mbps * 1e6)
        self._payload = bytes(payload)
        self._stop = threading.Event()
        self._running = threading.Event()
        self._running.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.sent_packets = 0
        self.payload_bytes = payload

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        next_send = time.perf_counter()
        while not self._stop.is_set():
            if not self._running.wait(timeout=0.1):
                next_send = time.perf_counter()
                continue
            now = time.perf_counter()
            if now < next_send:
                time.sleep(min(next_send - now, 0.001))
                continue
            try:
                sock.sendto(self._payload, self._target)
                self.sent_packets += 1
            except OSError:
                pass  # ICMP port unreachable from the UE surfaces here; keep pacing
            next_send += self._interval
            if now - next_send > 0.05:
                next_send = now  # never burst to catch up after a stall
        sock.close()

    def __enter__(self) -> "UdpLoad":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()


def sentinel(log: JsonlLog, phase: str, oneplus: IPv4Address, *, required: bool = True) -> bool:
    """Three VRF pings PC3 -> OnePlus; any loss fails the gate when required."""

    argv = ("ssh", "-o", "BatchMode=yes", "smartfarm-pc3",
            f"sudo -n ip vrf exec vrf-n3iwue ping -c 3 -W 2 {oneplus}")
    done = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)  # noqa: S603
    received = 0
    for line in done.stdout.splitlines():
        if "packets transmitted" in line:
            received = int(line.split(",")[1].split()[0])
    ok = done.returncode == 0 and received == 3
    log({"type": "sentinel" if required else "probe", "phase": phase, "received": received, "passed": ok,
         "stdout": done.stdout[-400:]})
    if required and not ok:
        raise GateFailure(f"sentinel failed after {phase}: {received}/3")
    return ok


def drain_then_sentinel(log: JsonlLog, phase: str, oneplus: IPv4Address, load: "UdpLoad | None",
                        max_drain_s: float) -> None:
    """Pause the load, wait for the DL queue to drain, then require 3/3 connectivity.

    A quota below the offered load builds a deep RLC queue (seconds of delay), so
    pings sent while saturated time out without any RRC/PDU loss. The drain time
    is recorded as a metric; the sentinel itself stays strict.
    """
    if load is not None:
        load.pause()
    started = time.monotonic()
    while time.monotonic() - started < max_drain_s:
        if sentinel(log, f"{phase}-drain", oneplus, required=False):
            break
    log({"type": "drain", "phase": phase, "drain_s": round(time.monotonic() - started, 2)})
    sentinel(log, phase, oneplus)
    if load is not None:
        load.resume()


class Gate:
    def __init__(self, client: E2ControlClient, log: JsonlLog, run_id: UUID, oneplus: IPv4Address,
                 max_drain_s: float = 60.0) -> None:
        self.client = client
        self.log = log
        self.run_id = run_id
        self.oneplus = oneplus
        self.ids = ActionIdAllocator()
        self.load: UdpLoad | None = None
        self.max_drain_s = max_drain_s

    def quota(self, prb: int, ttl_ms: int = 30_000, action_id: int | None = None) -> PrbQuotaAction:
        return PrbQuotaAction(
            action_id=action_id if action_id is not None else self.ids.next(),
            run_id=self.run_id,
            snssai=ONEPLUS_SLICE,
            prb_quota=prb,
            ttl_ms=ttl_ms,
            issued_at_ns=time.time_ns(),
        )

    async def send(self, phase: str, action: PrbQuotaAction | QuotaRollback) -> QuotaOutcome:
        sent = time.time_ns()
        outcome = await self.client.apply(action, timeout_s=3.0)
        self.log({
            "type": "outcome", "phase": phase, "action_id": action.action_id,
            "latency_ms": (time.time_ns() - sent) / 1e6, "outcome": outcome.model_dump(mode="json"),
        })
        return outcome

    async def readback(self, phase: str) -> None:
        snap = await self.client.wait_readback(timeout_s=3.0)
        self.log({
            "type": "readback", "phase": phase, "seq": snap.seq,
            "rules": [r.model_dump(mode="json") for r in snap.rules],
            "ues": [u.model_dump(mode="json") for u in snap.ues],
            "counters": snap.counters.model_dump(mode="json") if snap.counters else None,
        })

    async def ue_quota(self) -> set[int]:
        snap = await self.client.wait_readback(timeout_s=3.0)
        return {u.prb_quota for u in snap.ues if u.snssai == ONEPLUS_SLICE}

    def expect(self, condition: bool, message: str) -> None:
        self.log({"type": "check", "passed": condition, "message": message})
        if not condition:
            raise GateFailure(message)

    async def dwell(self, prb: int, seconds: float, refresh_s: float) -> None:
        """Hold one quota, renewing the lease, and verify the readback each refresh."""

        phase = f"dwell-{prb}"
        started = time.monotonic()
        self.log({"type": "phase", "phase": phase, "event": "start", "prb": prb})
        while True:
            outcome = await self.send(phase, self.quota(prb))
            self.expect(outcome.status is QuotaStatus.APPLIED and outcome.applied_prb == prb,
                        f"{phase}: quota {prb} applied ({outcome.status}, applied={outcome.applied_prb})")
            await self.readback(phase)
            self.expect(await self.ue_quota() == {prb}, f"{phase}: OnePlus readback enforces {prb}")
            remaining = seconds - (time.monotonic() - started)
            if remaining <= 0:
                break
            await asyncio.sleep(min(refresh_s, remaining))
        sent = self.load.sent_packets if self.load else 0
        self.log({"type": "phase", "phase": phase, "event": "end", "prb": prb, "sent_packets": sent})
        drain_then_sentinel(self.log, phase, self.oneplus, self.load, self.max_drain_s)

    async def safety_checks(self) -> None:
        # duplicate: same id and payload returns the original outcome without reapplying
        first = self.quota(52)
        original = await self.send("duplicate", first)
        await self.send("duplicate", self.quota(24))
        replay = await self.send("duplicate", first)
        self.expect(replay.duplicate and replay.applied_prb == original.applied_prb,
                    "duplicate returns the original outcome")
        self.expect(await self.ue_quota() == {24}, "duplicate did not reapply 52")

        # conflict: same id, different payload
        conflict = await self.send("conflict", self.quota(78, action_id=first.action_id))
        self.expect(conflict.status is QuotaStatus.CONFLICT, "reused action id with new payload is a conflict")
        self.expect(await self.ue_quota() == {24}, "conflict left the quota unchanged")

        # TTL expiry falls back to 106 without any new E2 message
        await self.send("ttl", self.quota(24, ttl_ms=5_000))
        self.expect(await self.ue_quota() == {24}, "short-lived quota active")
        await asyncio.sleep(6.5)
        await self.readback("ttl-expired")
        self.expect(await self.ue_quota() == {106}, "expired quota returned to 106")

        # chained rollback: 52 then 24, undo 24 -> 52, undo 52 -> 106
        a = await self.send("rollback", self.quota(52))
        b = await self.send("rollback", self.quota(24))
        wrong = await self.send("rollback", QuotaRollback(
            action_id=self.ids.next(), run_id=self.run_id, target_action_id=b.action_id,
            rollback_token=b.rollback_token ^ 1, ttl_ms=30_000, issued_at_ns=time.time_ns()))
        self.expect(wrong.status is QuotaStatus.ROLLBACK_TOKEN, "rollback with a wrong token is refused")
        undo_b = await self.send("rollback", QuotaRollback(
            action_id=self.ids.next(), run_id=self.run_id, target_action_id=b.action_id,
            rollback_token=b.rollback_token, ttl_ms=30_000, issued_at_ns=time.time_ns()))
        self.expect(undo_b.status is QuotaStatus.ROLLED_BACK and undo_b.applied_prb == 52, "rollback restores 52")
        self.expect(await self.ue_quota() == {52}, "readback shows 52 after rollback")
        undo_a = await self.send("rollback", QuotaRollback(
            action_id=self.ids.next(), run_id=self.run_id, target_action_id=a.action_id,
            rollback_token=a.rollback_token, ttl_ms=30_000, issued_at_ns=time.time_ns()))
        self.expect(undo_a.status is QuotaStatus.ROLLED_BACK and undo_a.applied_prb == 106, "rollback restores 106")
        self.expect(await self.ue_quota() == {106}, "readback shows 106 after the last rollback")
        sentinel(self.log, "safety", self.oneplus)


RRC_PATTERNS = {"ul_failure": "Detected UL Failure", "reestablishment": "Reestablishment RNTI"}


def gnb_log_lines(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def rrc_audit(path: Path | None, first_line: int) -> dict[str, Any]:
    """Count RRC/UL failure events written by the gNB since `first_line`."""

    if path is None or not path.exists():
        return {"available": False}
    counts = dict.fromkeys(RRC_PATTERNS, 0)
    events: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, start=1):
            if number <= first_line:
                continue
            for key, needle in RRC_PATTERNS.items():
                if needle in line:
                    counts[key] += 1
                    events.append(line.strip()[-160:])
    return {"available": True, "first_line": first_line, **counts, "events": events}


def resolve_oneplus(smf_log: Path, identities: Path) -> tuple[IPv4Address, IPv4Address]:
    selection = resolve_open5gs_endpoints(
        lines=read_smf_log(smf_log),
        identities=load_identities(identities),
        pdu_network=IPv4Network("10.45.0.0/16"),
        gateway=IPv4Address("10.45.0.1"),
        log_path=smf_log,
    )
    return selection.by_role(EndpointRole.ONEPLUS_8T).ipv4, selection.by_role(EndpointRole.UN3GPP_UE).ipv4


async def run(arguments: argparse.Namespace, log: JsonlLog) -> dict[str, Any]:
    first_line = gnb_log_lines(arguments.gnb_log)
    oneplus, un3gpp = resolve_oneplus(arguments.smf_log, arguments.identities)
    run_id = uuid4()
    log({"type": "gate", "event": "start", "run_id": str(run_id), "oneplus_ip": str(oneplus),
         "un3gpp_ip": str(un3gpp), "load_mbps": arguments.load_mbps, "dwell_s": arguments.dwell_s,
         "sequence": arguments.sequence, "safety": arguments.safety, "slice": ONEPLUS_SLICE.model_dump()})
    sentinel(log, "pre", oneplus)
    async with E2ControlClient(arguments.socket, sink=lambda e: log({"type": "xapp", "event": e})) as client:
        gate = Gate(client, log, run_id, oneplus, max_drain_s=arguments.max_drain_s)
        await gate.readback("initial")
        with UdpLoad(oneplus, arguments.udp_port, arguments.load_mbps) as load:
            gate.load = load
            for prb in arguments.sequence:
                await gate.dwell(prb, arguments.dwell_s, arguments.refresh_s)
            log({"type": "load", "sent_packets": load.sent_packets, "payload_bytes": load.payload_bytes})
            gate.load = None
        if arguments.rejection_check and arguments.safety:
            try:
                gate.quota(30)
            except ValueError:
                gate.expect(True, "PRB 30 (outside the action mask) rejected before E2")
            else:
                gate.expect(False, "PRB 30 (outside the action mask) was accepted")
        if arguments.safety:
            await gate.safety_checks()
        await gate.readback("final")
    audit = rrc_audit(arguments.gnb_log, first_line)
    log({"type": "rrc_audit", **audit})
    rrc_clean = audit.get("available", False) and audit["reestablishment"] == 0 and audit["ul_failure"] == 0
    log({"type": "gate", "event": "end", "passed": rrc_clean, "actuator_passed": True,
         "rrc_clean": rrc_clean, "run_id": str(run_id)})
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--socket", type=Path, required=True, help="SMARTFARM_SF_SOCKET of the control xApp")
    parser.add_argument("--output", type=Path, required=True, help="gate directory")
    parser.add_argument("--load-mbps", type=float, default=120.0)
    parser.add_argument("--max-drain-s", type=float, default=60.0)
    parser.add_argument("--udp-port", type=int, default=9)
    parser.add_argument("--dwell-s", type=float, default=40.0)
    parser.add_argument("--refresh-s", type=float, default=10.0)
    parser.add_argument("--gnb-log", type=Path, help="nr-softmodem log, audited for RRC/UL failures")
    parser.add_argument("--smf-log", type=Path, default=DEFAULT_SMF_LOG)
    parser.add_argument("--identities", type=Path, default=DEFAULT_IDENTITIES)
    parser.add_argument("--no-rejection-check", dest="rejection_check", action="store_false")
    parser.add_argument("--sequence", type=lambda v: tuple(int(x) for x in v.split(",")), default=SEQUENCE,
                        help="comma-separated dwell quotas, e.g. 106,106,106 for a no-cap control run")
    parser.add_argument("--no-safety", dest="safety", action="store_false",
                        help="skip duplicate/conflict/TTL/rollback checks (control runs)")
    arguments = parser.parse_args()
    arguments.output.mkdir(parents=True, exist_ok=True)
    log = JsonlLog(arguments.output / "prb-quota-gate.jsonl")
    try:
        audit = asyncio.run(run(arguments, log))
    except Exception as error:  # noqa: BLE001 - record every failure as evidence
        log({"type": "gate", "event": "end", "passed": False, "actuator_passed": False,
             "error": f"{type(error).__name__}: {error}"})
        print(f"GATE FAILED: {error}")
        return 1
    finally:
        log.close()
    if not audit.get("available"):
        print("ACTUATOR CRITERIA PASSED; RRC NOT AUDITED (pass --gnb-log)")
        return 1
    if audit["reestablishment"] or audit["ul_failure"]:
        print(f"ACTUATOR CRITERIA PASSED; RRC EVENTS: {audit['reestablishment']} re-establishment(s), "
              f"{audit['ul_failure']} UL failure(s)")
        return 2
    print("GATE PASSED (actuator criteria and RRC clean)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

