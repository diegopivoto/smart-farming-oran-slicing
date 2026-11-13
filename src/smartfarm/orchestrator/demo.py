"""Demo orchestrator: a tap on a service in the Smart Farm app drives the whole lab demo.

The app opens the service slice on the phone itself (`requestNetwork`) and POSTs, over its default `internet`
session, `{"service", "phone_ip", "app_session"}` to this API on PC1 (10.45.0.1). One demo session then runs:

    accepted -> un3gpp_switching -> un3gpp_in_slice -> generator_started -> streaming
                                         (any step) -> failed (with the cause)
    streaming -> stopped (DELETE, lease expired, phone session released) | superseded (a newer request)

- The phone address is checked against the core: it must be an active SMF session of the phone in the service DNN
  and S-NSSAI (AMF/SMF log evidence), before anything is switched.
- The UN3GPP UE is moved with the tested `SliceCoordinator` path (node agent through 5G, core evidence, UE-to-UE
  probe), with the `AppHandset` driver: the phone already moved, its session is only confirmed.
- The content generator is started on PC3 through the node agent (`traffic-*`) towards the phone. The agent caps a
  run at 3600 s, so a continuous stream is a chain of segments: just before a segment ends the generator is
  stopped and restarted with a new run id (gap of about `renew_pause_s` plus two agent round trips).
- One active demo session at a time; a request for another service (or a new app session) supersedes it: the old
  generator is stopped, then the new slice is set up. Requests are idempotent per `app_session`.
- The app polls `GET /api/v1/session/<id>`; polls renew a lease, and without polls for `lease_s` the generator is
  stopped (the app was killed or lost its default session). `DELETE` stops the generator and leaves the UN3GPP UE
  in the slice (switching away is part of the next request).
- Every request and transition is one JSON line in `experiments/demo/<YYYYMMDD>/orchestrator.jsonl`.
- The session view carries the service's SLA list and offered rate from `SERVICE_CATALOG`, so the app shows the
  targets in force (not a copy compiled into the APK).
- Address clash guard: if the UN3GPP UE's new address falls inside the /30 of any phone PDU session (seen live: the
  UE got 10.45.0.23, the broadcast address of the phone's `internet` 10.45.0.20/30, and Android dropped its
  packets), the session records the reason `un3gpp_ip_conflicts_with_phone_subnet` and the UE makes one more slice
  hop (to another service slice, UN3GPP UE only, then back) to get a new address, up to `ip_conflict_hops` times.
- O-RAN radio control (oran_agent): the request may carry `oran_mode` (`ran-default` | `ddqn`; absent: the
  orchestrator's default). It is stored on the session and read by the agent supervisor, which runs beside the flow
  and never changes it; the supervisor adds the mode and agent fields to the `oran` view.
- O-RAN card (optional, read-only): with an `OranSource` (smartfarm.orchestrator.oran_view), an active session's
  `GET` view also carries `oran`, the radio action in force for the slice read back from the E2 control xApp's
  journal, and `GET /api/v1/oran` serves it alone. It never changes the session flow; without a source, or on any
  error in it, the views are exactly as before. The health view and the POST/DELETE replies never carry it.

Stdlib only (asyncio streams HTTP/1.1, `Connection: close`).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import math
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from smartfarm.baseline.endpoints import EndpointRole
from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId, ServiceProfile
from smartfarm.core.evidence import CurrentSession, EvidenceTimeout, SessionEvidence
from smartfarm.orchestrator.slice_coordinator import AppHandset, SliceCoordinator
from smartfarm.orchestrator.un3gpp_slice import PingResult, Un3gppSliceStatus

SCHEMA = "smartfarm.demo_orchestrator.v1"
PDU_NETWORK = ipaddress.IPv4Network("10.45.0.0/16")
AGENT_MAX_SEGMENT_S = 3600  # smartfarm_node_agent.py: traffic duration 1..3600 s
ACTIVE_STATES = ("accepted", "un3gpp_switching", "un3gpp_in_slice", "generator_started", "streaming")
TERMINAL_STATES = ("failed", "stopped", "superseded")
_APP_SESSION = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SESSION_PATH = re.compile(r"^/api/v1/session/(?P<id>[0-9a-f]{16})$")
MAX_BODY = 4096
IP_CONFLICT_REASON = "un3gpp_ip_conflicts_with_phone_subnet"
ORAN_MODES = ("ran-default", "ddqn")  # smartfarm.orchestrator.oran_agent.MODES


class Evidence(Protocol):
    """What the orchestrator needs from `Open5gsLogEvidenceProvider` (plus what `SliceCoordinator` uses)."""

    def cursor(self) -> Any: ...

    def current_session(self, role: EndpointRole) -> CurrentSession | None: ...

    def active_sessions(self, role: EndpointRole) -> list[CurrentSession]: ...

    async def await_active(self, role: EndpointRole, snssai: Any, dnn: str, ipv4: IPv4Address, *,
                           timeout_s: float, hold_s: float = 2.0) -> SessionEvidence: ...


class Un3gppClient(Protocol):
    """What the orchestrator needs from `Un3gppSliceClient` (plus what `SliceCoordinator` uses)."""

    async def status(self) -> Un3gppSliceStatus | None: ...

    async def ping(self, target: IPv4Address) -> PingResult: ...

    async def start_traffic(self, service: ServiceId, target: IPv4Address, seconds: int, run_id: str,
                            rate_bps: int | None = None) -> str: ...

    async def stop_traffic(self) -> str: ...


@dataclass
class DemoConfig:
    segment_s: int = AGENT_MAX_SEGMENT_S  # generator run length; renewed just before it ends
    renew_margin_s: float = 5.0  # restart the generator this long before the segment ends
    renew_pause_s: float = 1.0  # between traffic-stop and the next traffic start (the node closes its run)
    lease_s: float = 90.0  # stop the generator without app polls for this long (0: never)
    phone_check_s: float = 10.0  # how often the phone's service session is checked while streaming
    phone_timeout_s: float = 10.0  # wait for the phone session reported by the app to show in the core
    hold_s: float = 2.0  # sessions must stay up this long to count (as in slice_switch.py)
    un3gpp_timeout_s: float = 30.0
    absent_timeout_s: float = 5.0
    flow_timeout_s: float = 120.0  # accepted -> streaming, whole
    agent_timeout_s: float = 20.0  # one node-agent command (traffic start/stop, status)
    settle_s: float = 2.0  # generator_started -> streaming: the UE must still be in the slice after this
    tolerate_probe_failure: bool = False  # start the generator even if the UE-to-UE ping got no reply
    require_phone_client: bool = True  # the HTTP client must be one of the phone's active PDU sessions
    ip_conflict_hops: int = 1  # extra UN3GPP slice hops when its address clashes with a phone session's subnet
    phone_subnet_prefix: int = 30  # prefix Android puts on the phone's PDU session addresses (rmnet, seen live)
    kept_sessions: int = 50

    def __post_init__(self) -> None:
        if not 1 <= self.segment_s <= AGENT_MAX_SEGMENT_S:
            raise ValueError(f"segment_s must be within 1..{AGENT_MAX_SEGMENT_S} (node agent limit)")
        if self.renew_margin_s >= self.segment_s:
            raise ValueError("renew_margin_s must be shorter than segment_s")
        if self.ip_conflict_hops < 0:
            raise ValueError("ip_conflict_hops must not be negative")
        if not 8 <= self.phone_subnet_prefix <= 32:
            raise ValueError("phone_subnet_prefix must be within 8..32")


class JsonlLog:
    """Append-only JSON lines under `<root>/<YYYYMMDD>/orchestrator.jsonl` (local date, new file each day)."""

    def __init__(self, root: Path, *, echo: Callable[[str], None] | None = print,
                 static: dict[str, Any] | None = None) -> None:
        self._root = root
        self._echo = echo
        self._static = static or {}

    def path(self) -> Path:
        return self._root / datetime.now().strftime("%Y%m%d") / "orchestrator.jsonl"

    def write(self, event: str, *, echo: bool = True, **fields: Any) -> dict[str, Any]:
        now = time.time()
        line = {"schema": SCHEMA, "event": event, "at": datetime.fromtimestamp(now, UTC).isoformat(),
                "epoch": round(now, 3), **self._static, **fields}
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True, default=str) + "\n")
        if self._echo is not None and echo:
            summary = " ".join(f"{k}={fields[k]}" for k in ("session", "service", "state", "status", "method",
                                                           "path", "client", "un3gpp_ip", "phone_ip", "run_id",
                                                           "cause") if fields.get(k) is not None)
            self._echo(f"{datetime.now().strftime('%H:%M:%S')} {event} {summary}")
        return line


class RequestError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class FlowFailed(Exception):
    """A demo session step failed; the message is the user-facing cause."""


@dataclass
class DemoSession:
    id: str
    service: ServiceId
    app_session: str
    phone_ip: IPv4Address
    client_ip: str
    created: float
    last_seen: float
    state: str = "accepted"
    history: list[dict[str, Any]] = field(default_factory=list)
    un3gpp_ip: str | None = None
    generator_run_id: str | None = None
    segments: int = 0
    segment_started: float = 0.0
    generator_maybe_running: bool = False
    cause: str | None = None
    reason: str | None = None  # machine-readable reason of the latest extra step (e.g. IP_CONFLICT_REASON)
    ip_conflict_hops: int = 0
    oran_mode: str | None = None  # requested O-RAN radio control (None: the orchestrator's default)
    transition: dict[str, Any] | None = None
    task: asyncio.Task[None] | None = None

    def view(self, profile: ServiceProfile) -> dict[str, Any]:
        return {
            "session": self.id,
            "service": self.service.value,
            "app_session": self.app_session,
            "phone_ip": str(self.phone_ip),
            "state": self.state,
            "active": self.state in ACTIVE_STATES,
            "cause": self.cause,
            "un3gpp_ip": self.un3gpp_ip,
            "generator_ip": self.un3gpp_ip,  # the generator runs on the UN3GPP UE
            "generator_run_id": self.generator_run_id,
            "generator_segments": self.segments,
            "reason": self.reason,
            "ip_conflict_hops": self.ip_conflict_hops,
            "sst": profile.snssai.sst,
            "sd": profile.snssai.sd,
            "phone_dnn": profile.handset_dnn,
            # the targets in force (SERVICE_CATALOG): the app evaluates its SLA panel against these
            "offered_rate_bps": profile.offered_rate_bps,
            "sla": sla_view(profile),
            "history": list(self.history),
            # NaN (e.g. a failed probe) is not JSON: the phone's org.json rejects the whole reply
            "probe_rtt_avg_ms": _finite((self.transition or {}).get("probe_rtt_avg_ms")),
            "un3gpp_switch_core_s": _finite((self.transition or {}).get("un3gpp_switch_core_s")),
            "poll": f"/api/v1/session/{self.id}",
        }


def sla_view(profile: ServiceProfile) -> list[dict[str, Any]]:
    """The service's SLA list as JSON (name, operator ge|le|eq, threshold, unit, window_s)."""

    return [{"name": m.name, "operator": m.operator, "threshold": _finite(float(m.threshold)), "unit": m.unit,
             "window_s": m.window_s} for m in profile.sla]


def _finite(value: Any) -> Any:
    """Replace NaN/inf (anywhere in a reply) with None: they are not JSON and the phone's org.json rejects them."""

    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


class DemoOrchestrator:
    def __init__(
        self,
        evidence: Evidence,
        un3gpp: Un3gppClient,
        log: JsonlLog,
        config: DemoConfig | None = None,
        *,
        catalog: dict[ServiceId, ServiceProfile] | None = None,
        clock: Callable[[], float] = time.monotonic,
        coordinator_factory: Callable[[DemoSession], SliceCoordinator] | None = None,
    ) -> None:
        self._evidence = evidence
        self._un3gpp = un3gpp
        self._log = log
        self.config = config or DemoConfig()
        self._catalog = catalog or dict(SERVICE_CATALOG)
        self._clock = clock
        self._coordinator_factory = coordinator_factory or self._coordinator
        self._sessions: OrderedDict[str, DemoSession] = OrderedDict()
        self._by_app_session: dict[str, str] = {}
        self._active: DemoSession | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------------------------------------ public API

    @property
    def active(self) -> DemoSession | None:
        return self._active

    def view(self, session: DemoSession) -> dict[str, Any]:
        return session.view(self._catalog[session.service])

    def session(self, session_id: str, *, touch: bool = True) -> DemoSession | None:
        session = self._sessions.get(session_id)
        if session is not None and touch:
            session.last_seen = self._clock()
        return session

    def phone_client(self, client_ip: str) -> bool:
        """True when `client_ip` is one of the phone's active PDU sessions (its default `internet` one)."""

        try:
            address = IPv4Address(client_ip)
        except ValueError:
            return False
        return any(s.ipv4 == address for s in self._evidence.active_sessions(EndpointRole.ONEPLUS_8T))

    async def submit(self, body: Any, client_ip: str) -> tuple[int, dict[str, Any]]:
        service, phone_ip, app_session = self._validate(body)
        oran_mode = self._validate_oran_mode(body)
        async with self._lock:
            existing_id = self._by_app_session.get(app_session)
            if existing_id is not None and existing_id in self._sessions:
                existing = self._sessions[existing_id]
                if (existing.service, existing.phone_ip) != (service, phone_ip):
                    raise RequestError(409, "app_session already used for another service or phone_ip")
                existing.last_seen = self._clock()
                return 200, self.view(existing)
            if self.config.require_phone_client and not self.phone_client(client_ip):
                raise RequestError(403, f"client {client_ip} is not an active PDU session of the phone")
            previous = self._active
            if previous is not None:
                await self._end_locked(previous, "superseded", f"superseded by a {service.value} request")
            now = self._clock()
            session = DemoSession(id=secrets.token_hex(8), service=service, app_session=app_session,
                                  phone_ip=phone_ip, client_ip=client_ip, created=now, last_seen=now,
                                  oran_mode=oran_mode)
            self._remember(session)
            self._active = session
            self._set_state(session, "accepted", superseded=previous.id if previous else None)
            session.task = asyncio.create_task(self._run(session), name=f"demo-{session.id}")
            return 202, self.view(session)

    async def stop(self, session_id: str, why: str = "stopped by the app") -> dict[str, Any] | None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session is self._active:
                await self._end_locked(session, "stopped", why)
            return self.view(session)

    async def shutdown(self) -> None:
        async with self._lock:
            if self._active is not None:
                await self._end_locked(self._active, "stopped", "orchestrator shutting down")

    # ------------------------------------------------------------------------------------------- the demo flow

    def _coordinator(self, session: DemoSession) -> SliceCoordinator:
        handset = AppHandset(self._evidence, session.phone_ip,  # type: ignore[arg-type]
                             timeout_s=self.config.phone_timeout_s, hold_s=self.config.hold_s)
        return SliceCoordinator(self._evidence, self._un3gpp, handset, catalog=self._catalog)  # type: ignore[arg-type]

    async def _run(self, session: DemoSession) -> None:
        try:
            await asyncio.wait_for(self._prepare(session), self.config.flow_timeout_s)
        except TimeoutError:
            await self._fail(session, f"demo setup did not finish within {self.config.flow_timeout_s:g} s "
                                      f"(stuck in {session.state})")
            return
        except FlowFailed as error:
            await self._fail(session, str(error))
            return
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - reported to the app as the cause
            await self._fail(session, f"internal error in {session.state}: {error!r}")
            return
        await self._keep(session)

    async def _prepare(self, session: DemoSession) -> None:
        profile = self._catalog[session.service]
        try:
            phone = await self._evidence.await_active(
                EndpointRole.ONEPLUS_8T, profile.snssai, profile.handset_dnn, session.phone_ip,
                timeout_s=self.config.phone_timeout_s, hold_s=0.0,
            )
        except EvidenceTimeout:
            raise FlowFailed(f"phone_ip {session.phone_ip} is not an active {profile.handset_dnn} session of the "
                             f"phone in SD {profile.snssai.sd} (Open5GS AMF/SMF logs)") from None
        self._log.write("phone_confirmed", session=session.id, phone_ip=str(phone.ipv4), dnn=phone.dnn,
                        sd=phone.snssai.sd, phone_established_at=phone.established_at)

        self._set_state(session, "un3gpp_switching")
        record = await self._switch(session)
        un3gpp_ip = self._un3gpp_ip(record)
        # checked before the outcome: a clashing address also makes the UE-to-UE probe fail (Android drops it)
        while un3gpp_ip and self._switch_usable(record, un3gpp_ip) \
                and (clash := self._phone_subnet_clash(session, un3gpp_ip)) is not None:
            phone_net = IPv4Network(f"{clash.ipv4}/{self.config.phone_subnet_prefix}", strict=False)
            if session.ip_conflict_hops >= self.config.ip_conflict_hops:
                raise FlowFailed(f"UN3GPP UE address {un3gpp_ip} is inside {phone_net} of the phone's {clash.dnn} "
                                 f"session {clash.ipv4} after {session.ip_conflict_hops} extra slice hop(s): the "
                                 f"phone would drop its packets ({IP_CONFLICT_REASON})")
            session.ip_conflict_hops += 1
            session.reason = IP_CONFLICT_REASON
            self._log.write("un3gpp_ip_conflict", session=session.id, un3gpp_ip=un3gpp_ip,
                            phone_session_ip=str(clash.ipv4), phone_dnn=clash.dnn, phone_subnet=str(phone_net),
                            hop=session.ip_conflict_hops, reason=IP_CONFLICT_REASON)
            self._set_state(session, "un3gpp_switching", reason=IP_CONFLICT_REASON, un3gpp_ip=un3gpp_ip,
                            phone_subnet=str(phone_net), hop=session.ip_conflict_hops)
            await self._hop_away(session)
            record = await self._switch(session)
            un3gpp_ip = self._un3gpp_ip(record)
        if record["outcome"] != "slice_confirmed":
            if not (self._switch_usable(record, un3gpp_ip) and self.config.tolerate_probe_failure):
                raise FlowFailed(f"UN3GPP slice switch failed: {record.get('cause')}")
            self._log.write("probe_failure_tolerated", session=session.id, cause=record.get("cause"))
        if un3gpp_ip is None:
            raise FlowFailed("UN3GPP UE address on the slice unknown after the switch")
        session.un3gpp_ip = un3gpp_ip
        self._set_state(session, "un3gpp_in_slice", un3gpp_ip=un3gpp_ip,
                        un3gpp_switch_core_s=record.get("un3gpp_switch_core_s"),
                        already_in_slice=bool(record.get("un3gpp_already_in_slice")),
                        probe_rtt_avg_ms=record.get("probe_rtt_avg_ms"))

        await self._start_generator(session)
        self._set_state(session, "generator_started", run_id=session.generator_run_id, un3gpp_ip=un3gpp_ip)

        await asyncio.sleep(self.config.settle_s)
        status = await self._agent(self._un3gpp.status(), "UN3GPP status after the generator start")
        if status is None or (status.sst, status.sd.lower()) != (profile.snssai.sst, profile.snssai.sd.lower()) \
                or status.status != "accepted":
            raise FlowFailed(f"UN3GPP UE left the {session.service.value} slice after the generator start: {status}")
        self._set_state(session, "streaming", un3gpp_ip=un3gpp_ip, run_id=session.generator_run_id)

    async def _switch(self, session: DemoSession) -> dict[str, Any]:
        """Both UEs to the service slice (`SliceCoordinator`, app handset); the record is logged and kept."""

        record = await self._coordinator_factory(session).transition(
            session.service, un3gpp_timeout_s=self.config.un3gpp_timeout_s,
            handset_timeout_s=self.config.phone_timeout_s, hold_s=self.config.hold_s,
            absent_timeout_s=self.config.absent_timeout_s,
        )
        session.transition = record
        self._log.write("slice_transition", session=session.id, record=record)
        return record

    @staticmethod
    def _un3gpp_ip(record: dict[str, Any]) -> str | None:
        return record.get("un3gpp_ipv4") or (
            record.get("un3gpp_previous_ipv4") if record.get("un3gpp_already_in_slice") else None)

    @staticmethod
    def _switch_usable(record: dict[str, Any], un3gpp_ip: str | None) -> bool:
        """The UN3GPP UE is in the slice at `un3gpp_ip`: confirmed, or only the UE-to-UE probe failed."""

        if not un3gpp_ip:
            return False
        if record.get("outcome") == "slice_confirmed":
            return True
        return (record.get("cause") or "").startswith(("UE-to-UE probe", "probe:"))

    def _phone_subnet_clash(self, session: DemoSession, un3gpp_ip: str) -> CurrentSession | None:
        """The phone PDU session whose /prefix (Android rmnet) contains the UN3GPP UE address, if any."""

        try:
            address = IPv4Address(un3gpp_ip)
        except ValueError:
            return None
        try:
            sessions = self._evidence.active_sessions(EndpointRole.ONEPLUS_8T)
        except Exception as error:  # noqa: BLE001 - a log read problem must not stop the demo
            self._log.write("phone_check_error", session=session.id, cause=repr(error))
            return None
        for phone in sessions:
            network = IPv4Network(f"{phone.ipv4}/{self.config.phone_subnet_prefix}", strict=False)
            if address in network:
                return phone
        return None

    async def _hop_away(self, session: DemoSession) -> None:
        """UN3GPP UE only (no handset driver) to another service slice, so the way back gives it a new address."""

        hop = next(s for s in self._catalog if s is not session.service)
        coordinator = SliceCoordinator(self._evidence, self._un3gpp, None, catalog=self._catalog)  # type: ignore[arg-type]
        record = await coordinator.transition(
            hop, un3gpp_timeout_s=self.config.un3gpp_timeout_s, handset_timeout_s=self.config.phone_timeout_s,
            hold_s=self.config.hold_s, absent_timeout_s=self.config.absent_timeout_s,
        )
        self._log.write("slice_hop", session=session.id, hop_service=hop.value, reason=session.reason, record=record)
        if not record.get("un3gpp_ipv4"):
            raise FlowFailed(f"slice hop to {hop.value} for a new UN3GPP UE address failed: {record.get('cause')}")

    async def _keep(self, session: DemoSession) -> None:
        """Keep the generator running: renew segments, end on lease expiry or when the phone session goes away."""

        cfg = self.config
        next_check = self._clock() + cfg.phone_check_s
        renew_failures = 0
        while True:
            renew_at = session.segment_started + cfg.segment_s - cfg.renew_margin_s
            now = self._clock()
            wake = min(renew_at, next_check, session.last_seen + cfg.lease_s if cfg.lease_s else renew_at)
            await asyncio.sleep(max(0.02, wake - now))
            now = self._clock()
            if cfg.lease_s and now - session.last_seen > cfg.lease_s:
                await self._end(session, "stopped", f"lease expired: no poll from the app for {cfg.lease_s:g} s")
                return
            if now >= next_check:
                next_check = now + cfg.phone_check_s
                if not self._phone_session_active(session):
                    await self._end(session, "stopped", f"phone session {session.phone_ip} was released")
                    return
            if now >= renew_at:
                try:
                    await self._renew(session)
                    renew_failures = 0
                except FlowFailed as error:
                    renew_failures += 1
                    self._log.write("generator_renew_failed", session=session.id, attempt=renew_failures,
                                    cause=str(error))
                    if renew_failures >= 3:
                        await self._fail(session, f"generator renewal failed 3 times: {error}")
                        return
                    session.segment_started = self._clock() - cfg.segment_s + cfg.renew_margin_s + 3.0

    def _phone_session_active(self, session: DemoSession) -> bool:
        profile = self._catalog[session.service]
        try:
            sessions = self._evidence.active_sessions(EndpointRole.ONEPLUS_8T)
        except Exception as error:  # noqa: BLE001 - a log read problem must not stop the demo
            self._log.write("phone_check_error", session=session.id, cause=repr(error))
            return True
        return any(s.ipv4 == session.phone_ip and s.dnn == profile.handset_dnn for s in sessions)

    async def _agent(self, call: Any, what: str) -> Any:
        try:
            return await asyncio.wait_for(call, self.config.agent_timeout_s)
        except TimeoutError:
            raise FlowFailed(f"{what}: node agent did not answer within {self.config.agent_timeout_s:g} s") from None
        except FlowFailed:
            raise
        except Exception as error:  # noqa: BLE001 - Un3gppSliceError and transport errors
            raise FlowFailed(f"{what}: {error}") from error

    async def _start_generator(self, session: DemoSession) -> None:
        profile = self._catalog[session.service]
        run_id = uuid4().hex
        session.generator_maybe_running = True  # the agent may start it even if its reply is lost
        reply = await self._agent(
            self._un3gpp.start_traffic(session.service, session.phone_ip, self.config.segment_s, run_id,
                                       profile.offered_rate_bps),
            "generator start",
        )
        session.generator_run_id = run_id
        session.segments += 1
        session.segment_started = self._clock()
        self._log.write("generator_segment", session=session.id, service=session.service.value, run_id=run_id,
                        phone_ip=str(session.phone_ip), duration_s=self.config.segment_s,
                        rate_bps=profile.offered_rate_bps, segment=session.segments, reply=reply)

    async def _renew(self, session: DemoSession) -> None:
        async with self._lock:
            if session is not self._active:
                return
            await self._agent(self._un3gpp.stop_traffic(), "generator stop before renewal")
            await asyncio.sleep(self.config.renew_pause_s)
            await self._start_generator(session)

    async def _stop_generator(self, session: DemoSession) -> None:
        if not session.generator_maybe_running:
            return
        try:
            reply = await self._agent(self._un3gpp.stop_traffic(), "generator stop")
            session.generator_maybe_running = False
            self._log.write("generator_stopped", session=session.id, run_id=session.generator_run_id, reply=reply)
        except FlowFailed as error:
            self._log.write("generator_stop_failed", session=session.id, run_id=session.generator_run_id,
                            cause=str(error))

    async def _fail(self, session: DemoSession, cause: str) -> None:
        await self._end(session, "failed", cause)

    async def _end(self, session: DemoSession, state: str, cause: str) -> None:
        async with self._lock:
            if session is self._active:
                await self._end_locked(session, state, cause, cancel=False)

    async def _end_locked(self, session: DemoSession, state: str, cause: str, *, cancel: bool = True) -> None:
        """Stop `session` (lock held): cancel its flow, stop its generator, record the final state."""

        task = session.task
        if cancel and task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._stop_generator(session)
        session.cause = cause
        self._set_state(session, state, cause=cause)
        if self._active is session:
            self._active = None

    # ----------------------------------------------------------------------------------------------- helpers

    def _set_state(self, session: DemoSession, state: str, **detail: Any) -> None:
        session.state = state
        entry: dict[str, Any] = {"state": state, "at": datetime.now(UTC).isoformat(),
                 "t_s": round(self._clock() - session.created, 3)}
        entry.update({k: v for k, v in detail.items() if v is not None and k != "cause"})
        if detail.get("cause"):
            entry["cause"] = detail["cause"]
        session.history.append(entry)
        self._log.write("transition", session=session.id, service=session.service.value,
                        app_session=session.app_session, phone_ip=str(session.phone_ip), **entry)

    def _remember(self, session: DemoSession) -> None:
        self._sessions[session.id] = session
        self._by_app_session[session.app_session] = session.id
        while len(self._sessions) > self.config.kept_sessions:
            old_id, old = next(iter(self._sessions.items()))
            if old is self._active:
                break
            del self._sessions[old_id]
            self._by_app_session.pop(old.app_session, None)

    @staticmethod
    def _validate_oran_mode(body: Any) -> str | None:
        """Optional `oran_mode` of the request (ran-default | ddqn); absent or null: the orchestrator's default."""

        value = body.get("oran_mode") if isinstance(body, dict) else None
        if value is None:
            return None
        if not isinstance(value, str) or value not in ORAN_MODES:
            raise RequestError(400, f"oran_mode must be one of {', '.join(ORAN_MODES)}")
        return value

    @staticmethod
    def _validate(body: Any) -> tuple[ServiceId, IPv4Address, str]:
        if not isinstance(body, dict):
            raise RequestError(400, "body must be a JSON object")
        try:
            service = ServiceId(str(body.get("service")))
        except ValueError:
            raise RequestError(400, "service must be one of weather, crop, drone") from None
        try:
            phone_ip = IPv4Address(str(body.get("phone_ip", "")))
        except ValueError:
            raise RequestError(400, "phone_ip must be an IPv4 address") from None
        if phone_ip not in PDU_NETWORK or phone_ip in (PDU_NETWORK.network_address, PDU_NETWORK.broadcast_address):
            raise RequestError(400, f"phone_ip must be inside {PDU_NETWORK}")
        app_session = body.get("app_session")
        if not isinstance(app_session, str) or not _APP_SESSION.match(app_session):
            raise RequestError(400, "app_session must be 1-64 characters [A-Za-z0-9_.:-]")
        return service, phone_ip, app_session


# ---------------------------------------------------------------------------------------------------- HTTP

REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
           405: "Method Not Allowed", 409: "Conflict", 411: "Length Required", 413: "Payload Too Large",
           500: "Internal Server Error"}


def client_allowed(client: str) -> bool:
    """Only the UE pool (the phone, through the core) and PC1 itself."""

    try:
        address = ipaddress.ip_address(client)
    except ValueError:
        return False
    return address.version == 4 and (address in PDU_NETWORK or address.is_loopback)


class OranSource(Protocol):
    """Read-only O-RAN view of a slice (smartfarm.orchestrator.oran_view.OranMonitor); must never raise."""

    def view(self, sd: str) -> dict[str, Any]: ...


class OranOverlay(Protocol):
    """Mode and agent fields of a session's `oran` view (smartfarm.orchestrator.oran_agent.OranAgentSupervisor)."""

    def overlay(self, session: Any, base: dict[str, Any]) -> dict[str, Any]: ...


class DemoApi:
    def __init__(self, orchestrator: DemoOrchestrator, log: JsonlLog, *, dry_run: bool = False,
                 oran: OranSource | None = None, agent: OranOverlay | None = None) -> None:
        self.orchestrator = orchestrator
        self._log = log
        self._dry_run = dry_run
        self._oran = oran
        self._agent = agent

    def _with_oran(self, view: dict[str, Any]) -> dict[str, Any]:
        """Add the read-only `oran` object to an active session's view (absent without a monitor or when inactive)."""

        if self._oran is None or not view.get("active") or not isinstance(view.get("sd"), str):
            return view
        try:
            oran = self._oran.view(view["sd"])
        except Exception:  # noqa: BLE001 - the O-RAN card must never break the session view
            return view
        if self._agent is not None:
            session = self.orchestrator.session(str(view.get("session")), touch=False)
            if session is not None:
                with contextlib.suppress(Exception):
                    oran = self._agent.overlay(session, oran)
        return {**view, "oran": oran}

    async def dispatch(self, method: str, target: str, body: bytes, client: str) -> tuple[int, dict[str, Any]]:
        path = urlsplit(target).path
        if not client_allowed(client):
            return 403, {"error": "only the 5G UE pool may use this API"}
        try:
            if path == "/api/v1/health":
                if method != "GET":
                    return 405, {"error": "GET only"}
                active = self.orchestrator.active
                return 200, {"ok": True, "api": SCHEMA, "dry_run": self._dry_run,
                             "active_session": self.orchestrator.view(active) if active else None}
            if path == "/api/v1/service":
                if method != "POST":
                    return 405, {"error": "POST only"}
                try:
                    payload = json.loads(body.decode("utf-8") or "null")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return 400, {"error": "body must be JSON"}
                return await self.orchestrator.submit(payload, client)
            if path == "/api/v1/oran":
                if method != "GET":
                    return 405, {"error": "GET only"}
                if self._oran is None:
                    return 404, {"error": "O-RAN view disabled (--no-oran-view)"}
                service = parse_qs(urlsplit(target).query).get("service", [None])[0]
                active = self.orchestrator.active
                if service is None and active is None:
                    return 400, {"error": "no active session: pass ?service=weather|crop|drone"}
                try:
                    sid = ServiceId(service) if service is not None else active.service  # type: ignore[union-attr]
                except ValueError:
                    return 400, {"error": "service must be weather, crop or drone"}
                oran = self._oran.view(SERVICE_CATALOG[sid].snssai.sd)
                if self._agent is not None and active is not None and active.service is sid:
                    with contextlib.suppress(Exception):
                        oran = self._agent.overlay(active, oran)
                return 200, {"service": sid.value, **oran}
            if (m := _SESSION_PATH.match(path)) is not None:
                if method == "GET":
                    session = self.orchestrator.session(m["id"])
                    return (404, {"error": "unknown session"}) if session is None else \
                        (200, self._with_oran(self.orchestrator.view(session)))
                if method == "DELETE":
                    view = await self.orchestrator.stop(m["id"])
                    return (404, {"error": "unknown session"}) if view is None else (200, view)
                return 405, {"error": "GET or DELETE only"}
            return 404, {"error": "not found"}
        except RequestError as error:
            return error.status, {"error": error.message}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        started = time.monotonic()
        peer = writer.get_extra_info("peername") or ("?", 0)
        client = str(peer[0])
        method, target, status = "?", "?", 500
        request_fields: dict[str, str] = {}
        payload: dict[str, Any] = {"error": "internal error"}
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5.0)
                lines = head.decode("latin-1").split("\r\n")
                method, target, _version = lines[0].split(" ", 2)
                headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
                length = int(headers.get("content-length", "0"))
                if length < 0:
                    raise ValueError
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ValueError):
                status, payload = 400, {"error": "malformed HTTP request"}
            else:
                if length > MAX_BODY:
                    status, payload = 413, {"error": f"body above {MAX_BODY} bytes"}
                else:
                    try:
                        body = await asyncio.wait_for(reader.readexactly(length), 5.0) if length else b""
                    except (asyncio.IncompleteReadError, TimeoutError):
                        status, payload = 400, {"error": "incomplete body"}
                    else:
                        if method == "POST":
                            with contextlib.suppress(Exception):
                                parsed = json.loads(body.decode("utf-8"))
                                if isinstance(parsed, dict):
                                    request_fields = {k: str(parsed.get(k))[:64] for k in
                                                      ("service", "phone_ip", "app_session") if k in parsed}
                        try:
                            status, payload = await self.dispatch(method, target, body, client)
                        except Exception as error:  # noqa: BLE001 - never kill the server
                            status, payload = 500, {"error": f"internal error: {error!r}"}
            data = json.dumps(_finite(payload), sort_keys=True, allow_nan=False).encode()
            writer.write(
                f"HTTP/1.1 {status} {REASONS.get(status, 'Error')}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(data)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n".encode()
                + data
            )
            with contextlib.suppress(ConnectionError, TimeoutError):
                await asyncio.wait_for(writer.drain(), 5.0)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            quiet = method == "GET" and status == 200  # app polls (1/s while connecting): file only
            self._log.write("request", echo=not quiet, method=method, path=urlsplit(target).path if target != "?" else target,
                            client=client, status=status, ms=round((time.monotonic() - started) * 1000, 1),
                            session=payload.get("session") if status < 300 and isinstance(payload, dict) else None,
                            state=payload.get("state") if status < 300 and isinstance(payload, dict) else None,
                            cause=payload.get("error") if isinstance(payload, dict) else None,
                            **request_fields)

    async def serve(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(self.handle, host, port, limit=8192)
