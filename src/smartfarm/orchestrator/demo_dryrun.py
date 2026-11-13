"""In-memory stand-ins for the core evidence and the UN3GPP node agent, for `demo_orchestrator.py --dry-run`.

`DryRunLab` behaves like the lab seen from PC1: the UN3GPP UE switches slice after `switch_delay_s` and gets a new
10.45.0.x address, the phone's service sessions are accepted when the reported address is inside the SMF pool of
that DNN (as the lab's per-DNN pools), generator starts/stops are recorded. Nothing touches the network.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from ipaddress import IPv4Address, IPv4Network
from typing import Any

from smartfarm.baseline.endpoints import EndpointRole
from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId, Snssai
from smartfarm.core.evidence import CurrentSession, EvidenceTimeout, SessionEvidence
from smartfarm.orchestrator.un3gpp_slice import SLICE_INDEX, PingResult, Un3gppSliceError, Un3gppSliceStatus

# per-DNN SMF pools of the phone (deployment/open5gs: crop 10.45.128-159, drone 160-191, weather 192-223)
PHONE_POOLS = {
    "crop": IPv4Network("10.45.128.0/19"),
    "drone": IPv4Network("10.45.160.0/19"),
    "weather": IPv4Network("10.45.192.0/19"),
}


def log_ts() -> str:
    """Open5GS log timestamp (local time) for now."""

    return datetime.now().strftime("%m/%d %H:%M:%S.%f")[:-3]


@dataclass
class DryRunLab:
    switch_delay_s: float = 0.4
    reject: bool = False
    probe_received: int = 5
    auto_phone: bool = True  # accept any reported phone address inside its DNN pool
    sd: str = "000001"
    pdu_session_id: int = 1
    ipv4: IPv4Address = IPv4Address("10.45.0.3")
    phone: dict[IPv4Address, CurrentSession] = field(default_factory=dict)
    traffic: list[tuple[str, ...]] = field(default_factory=list)
    slice_requests: list[ServiceId] = field(default_factory=list)
    running: int = 0
    _next_host: int = 4
    _switched_at: str = field(default_factory=log_ts)

    # ------------------------------------------------------------------------------------------- phone helpers

    def add_phone_session(self, ip: str, dnn: str) -> None:
        sd = next((p.snssai.sd for p in SERVICE_CATALOG.values() if p.handset_dnn == dnn), "000001")
        address = IPv4Address(ip)
        self.phone[address] = CurrentSession(Snssai(sst=1, sd=sd), dnn, address, log_ts(), log_ts())

    def release_phone_session(self, ip: str) -> None:
        self.phone.pop(IPv4Address(ip), None)

    # ---------------------------------------------------------------------------------------- evidence (core)

    def cursor(self) -> str:
        return "dry-run"

    def active_sessions(self, role: EndpointRole) -> list[CurrentSession]:
        if role is EndpointRole.ONEPLUS_8T:
            return sorted(self.phone.values(), key=lambda s: s.established_at)
        snssai = Snssai(sst=1, sd=self.sd)
        return [CurrentSession(snssai, "internet", self.ipv4, self._switched_at, self._switched_at)]

    def current_session(self, role: EndpointRole) -> CurrentSession | None:
        sessions = self.active_sessions(role)
        return sessions[-1] if sessions else None

    async def await_active(self, role: EndpointRole, snssai: Snssai, dnn: str, ipv4: IPv4Address, *,
                           timeout_s: float, hold_s: float = 2.0) -> SessionEvidence:
        session = self.phone.get(ipv4)
        if session is None and self.auto_phone and dnn in PHONE_POOLS and ipv4 in PHONE_POOLS[dnn]:
            self.add_phone_session(str(ipv4), dnn)
            session = self.phone[ipv4]
        if session is None or session.dnn != dnn or session.snssai.sd != snssai.sd.lower():
            await asyncio.sleep(min(timeout_s, 0.2))
            raise EvidenceTimeout(f"{role.value} has no active {dnn} session {ipv4} in SD {snssai.sd}")
        return SessionEvidence(role, f"{role.value}-dry-run", snssai, dnn, ipv4,
                               session.requested_at or session.established_at, session.established_at, hold_s)

    async def await_session(self, role: EndpointRole, snssai: Snssai, dnn: str, since: Any, *,
                            timeout_s: float, hold_s: float = 2.0) -> SessionEvidence:
        if role is not EndpointRole.UN3GPP_UE or snssai.sd != self.sd:
            raise EvidenceTimeout(f"no stable {dnn} session in SD {snssai.sd} for {role.value}")
        return SessionEvidence(role, f"{role.value}-dry-run", snssai, dnn, self.ipv4, self._switched_at,
                               self._switched_at, hold_s)

    async def await_absent(self, role: EndpointRole, ipv4: IPv4Address, since: Any, *,
                           timeout_s: float) -> str | None:
        return self._switched_at

    # ----------------------------------------------------------------------------------- node agent (UN3GPP UE)

    def _status(self) -> Un3gppSliceStatus:
        return Un3gppSliceStatus(int(self.sd[-1]), 1, self.sd, self.pdu_session_id, "vrf-n3iwue",
                                 f"gretun-id-{self.pdu_session_id}-1", self.ipv4, "accepted")

    async def status(self) -> Un3gppSliceStatus | None:
        return self._status()

    async def request(self, service: ServiceId) -> bool:
        if self.sd == SERVICE_CATALOG[service].snssai.sd:
            return False
        self.slice_requests.append(service)
        self.traffic.append(("slice", str(SLICE_INDEX[service])))
        return True

    async def await_status(self, service: ServiceId, previous_pdu_id: int | None, *,
                           timeout_s: float = 20.0) -> Un3gppSliceStatus:
        await asyncio.sleep(min(self.switch_delay_s, timeout_s))
        if self.reject:
            raise Un3gppSliceError(f"N3IWUE slice switch rejected (active SD {self.sd})")
        self.running = 0  # the generator dies with the old PDU session
        self.sd = SERVICE_CATALOG[service].snssai.sd
        self.pdu_session_id += 1
        self.ipv4 = IPv4Address(f"10.45.0.{self._next_host}")
        self._next_host = 4 + (self._next_host - 3) % 200
        self._switched_at = log_ts()
        return self._status()

    async def ping(self, target: IPv4Address) -> PingResult:
        return PingResult(target, 5, self.probe_received, 58.1, 61.4, 66.0)

    async def start_traffic(self, service: ServiceId, target: IPv4Address, seconds: int, run_id: str,
                            rate_bps: int | None = None) -> str:
        self.traffic.append(("traffic", service.value, str(target), str(seconds), run_id, str(rate_bps)))
        self.running += 1
        return f"TRAFFIC started service={service.value} target={target} duration_s={seconds} run_id={run_id} pid=0"

    async def stop_traffic(self) -> str:
        self.traffic.append(("traffic", "stop"))
        stopped, self.running = self.running, 0
        return f"TRAFFIC stopped={stopped}"


# ------------------------------------------------------------------------------------------------ fake O-RAN


@dataclass
class FakeOutcome:
    node: str  # un3gpp | oai
    action_id: int
    status: str
    mcs: int = 0
    rb: int = 0
    prb: int = 0
    at_ns: int = 0


@dataclass
class FakeRadio:
    """In-memory radios for `--dry-run`: what the fake DDQN port applies, what the fake xApp journal reads back.
    At rest: BS link adaptation on the full band, no DL quota rule (the RAN defaults)."""

    ul: tuple[int, int] | None = None  # (MCS, RBs) override, None = BS link adaptation
    quota: int | None = None  # DL PRB quota rule of the slice, None = none
    quota_expires_ns: int = 0
    outcomes: list[FakeOutcome] = field(default_factory=list)
    clock_ns: Any = time.time_ns
    applied: int = 0

    def record(self, outcome: FakeOutcome) -> None:
        self.outcomes.append(outcome)
        del self.outcomes[:-12]


@dataclass
class FakeOranJournal:
    """Fake xApp journal for `--dry-run`: lines in the exact sf-control.jsonl format (the labels the gNB and the
    UN3GPP proxy publish) describing the `FakeRadio` for the slice of the fake UN3GPP UE. Fed to `OranMonitor` like the
    real journal, so the app's O-RAN card shows the radio state and the outcomes without the lab."""

    lab: DryRunLab
    radio: FakeRadio = field(default_factory=FakeRadio)

    def __call__(self) -> list[str]:
        now = int(self.radio.clock_ns())
        sd = self.lab.sd
        if self.radio.quota is not None and now > self.radio.quota_expires_ns:
            self.radio.quota = None  # TTL expired, not renewed
        un3gpp = [f"sf=0.1;kind=un3gpp_outcome;action_id={o.action_id};mac_seq={o.action_id % 100000};"
                  f"status={o.status};dup=0;ue=1;mcs={o.mcs};rb={o.rb};at_ns={o.at_ns}"
                  for o in self.radio.outcomes if o.node == "un3gpp"][-1:]
        oai = [f"sf=0.1;kind=outcome;action_id={o.action_id};status={o.status};dup=0;sst=1;sd={sd};rnti=0;"
               f"requested={o.prb};applied={o.prb};previous=106;token=0000000000000000;"
               f"expires_at_ns={o.at_ns + 30_000_000_000};version=1;at_ns={o.at_ns}"
               for o in self.radio.outcomes if o.node == "oai"][-1:]
        lines = []
        # earlier outcomes as older indications, so the monitor's history sees them
        for k, o in enumerate(self.radio.outcomes[:-2]):
            label = (f"sf=0.1;kind=un3gpp_outcome;action_id={o.action_id};mac_seq=1;status={o.status};dup=0;ue=1;"
                     f"mcs={o.mcs};rb={o.rb};at_ns={o.at_ns}" if o.node == "un3gpp" else
                     f"sf=0.1;kind=outcome;action_id={o.action_id};status={o.status};dup=0;sst=1;sd={sd};rnti=0;"
                     f"requested={o.prb};applied={o.prb};previous=106;token=0000000000000000;"
                     f"expires_at_ns={o.at_ns + 30_000_000_000};version=1;at_ns={o.at_ns}")
            lines.append(json.dumps({"type": "indication", "epoch": "dry-run", "seq": k + 1,
                                     "received_at_ns": o.at_ns + 100_000_000, "node": o.node, "labels": [label]}))
        ul = self.radio.ul
        if ul is None:
            ue = ("sf=0.1;kind=un3gpp_ue;ue=1;override=0;mcs_cmd=8;rb_first=0;rb_cmd=132;mcs_obs=8;rb_obs=40;la_mcs=8;"
                  "snr_db=22.60;pdus=150;mac_mbps=6.100;age_ms=120")
        else:
            ue = (f"sf=0.1;kind=un3gpp_ue;ue=1;override=1;mcs_cmd={ul[0]};rb_first=0;rb_cmd={ul[1]};mcs_obs={ul[0]};"
                  f"rb_obs={ul[1]};la_mcs=8;snr_db=22.60;pdus=190;mac_mbps=12.400;age_ms=120")
        quota = self.radio.quota
        state = [f"sf=0.1;kind=ue;rnti=41972;sst=1;sd={sd};prb={quota or 106};action_id={1 if quota else 0}"]
        if quota is not None:
            state.insert(0, f"sf=0.1;kind=rule;sst=1;sd={sd};rnti=0;prb={quota};action_id=1;"
                            f"expires_at_ns={self.radio.quota_expires_ns};version=1")
        n = len(lines)
        lines.append(json.dumps({"type": "indication", "epoch": "dry-run", "seq": n + 1, "received_at_ns": now,
                                 "node": "un3gpp", "labels": [*un3gpp, ue]}))
        lines.append(json.dumps({"type": "indication", "epoch": "dry-run", "seq": n + 2, "received_at_ns": now,
                                 "node": "oai", "labels": [*oai, *state]}))
        return lines


class DryRunRadioPort:
    """`RadioPort` of the DDQN controller for `--dry-run`: actions go to the `FakeRadio` (every one CONFIRMED and
    APPLIED), and a step's measurement replays a calibration cell of the same service and action (a random pass), so
    the frozen agent decides on realistic outcomes. Nothing touches the lab."""

    def __init__(self, radio: FakeRadio, model: Any, service: ServiceId, seed: int = 7,
                 busy: str | None = None) -> None:
        import random

        self.radio, self.model, self.service, self.busy = radio, model, service, busy
        self.rng = random.Random(seed)
        self.opened = self.closed = False
        self.safe: list[str] = []
        self._ids = int(time.time_ns())
        self._action: Any = None

    def _id(self) -> int:
        self._ids += 1
        return self._ids

    async def open(self, service: ServiceId) -> str | None:
        if self.busy:
            return self.busy
        self.opened = True
        return None

    async def apply(self, action: Any) -> dict[str, Any]:
        now = int(self.radio.clock_ns())
        self._action = action
        self.radio.applied += 1
        if action.link_adaptation:
            self.radio.ul = None
            self.radio.record(FakeOutcome("un3gpp", self._id(), "RELEASED", at_ns=now))
            ul = "RELEASED"
        else:
            self.radio.ul = (action.ul_mcs, action.ul_rb)
            self.radio.record(FakeOutcome("un3gpp", self._id(), "CONFIRMED", action.ul_mcs, action.ul_rb, at_ns=now))
            ul = "CONFIRMED"
        await self.renew_quota(action.dl_prb)
        return {"ul_status": ul, "dl_status": "APPLIED"}

    async def renew_quota(self, prb: int) -> str:
        now = int(self.radio.clock_ns())
        self.radio.quota, self.radio.quota_expires_ns = prb, now + 30_000_000_000
        self.radio.record(FakeOutcome("oai", self._id(), "APPLIED", prb=prb, at_ns=now))
        return "APPLIED"

    def ul_sample(self) -> dict[str, float] | None:
        mcs, rb = self.radio.ul if self.radio.ul else (8, 40)
        return {"snr_db": 22.6, "mcs_observed": mcs, "rb_observed": rb, "la_mcs": 8, "mac_mbps": 6.1}

    async def measure(self, run_ids: list[str], t0: float, t1: float) -> dict[str, Any]:
        data = self.model.data
        index = data.actions.index(self._action)
        cells = [c for c in data.cells if c.service is self.service and data.actions.index(c.action) == index]
        if not cells:
            return {"sla_met": None, "traffic_records": 0}
        raw = self.rng.choice(cells).raw
        keep = {k: v for k, v in raw.items() if k.startswith(("svc_", "kpm_")) or k == "sla_met"}
        return {**keep, "traffic_records": raw.get("traffic_records"), "replayed_pass": raw.get("pass")}

    async def after_error(self, error: BaseException) -> bool:
        return True

    async def safe_defaults(self, reason: str) -> dict[str, Any]:
        self.safe.append(reason)
        now = int(self.radio.clock_ns())
        self.radio.ul = None
        self.radio.record(FakeOutcome("un3gpp", self._id(), "RELEASED", at_ns=now))
        await self.renew_quota(106)
        return {"reason": reason, "ul": "RELEASED", "dl": "APPLIED"}

    async def close(self) -> None:
        self.closed = True
