"""Evidence-based slice transition of both UEs to the slice of a requested service.

Order: evidence cursor -> current state -> UN3GPP request (node agent, through 5G) and handset request in parallel ->
both new sessions confirmed by the core (AMF S-NSSAI + SMF address, held without removal) -> old sessions removed ->
UE-to-UE probe through the core. Any failure ends in `failed_safe` with its cause; nothing is retried in a loop.
Every transition yields one flat record (timings in seconds) for the results dataset.

Handset drivers: `ManualApnHandset` (the operator selects the service APN; the new session must appear after the
evidence cursor and replaces the previous one) and `AppHandset` (the Smart Farm app already opened the service session
with `requestNetwork` and keeps its default `internet` session: the driver only confirms, from the core evidence, the
session at the address the app reported; nothing is replaced, and the probe targets that address).
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import IPv4Address
from typing import Any, Protocol
from uuid import uuid4

from smartfarm.baseline.endpoints import EndpointRole
from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId, ServiceProfile
from smartfarm.core.evidence import Open5gsLogEvidenceProvider, SessionEvidence, log_time_to_epoch
from smartfarm.orchestrator.un3gpp_slice import Un3gppSliceClient


class HandsetDriver(Protocol):
    """Makes the OnePlus open its PDU session for the service DNN (APN).

    `request` returns None when the coordinator must wait for the new session in the core logs (after its evidence
    cursor), or the session evidence when the driver already confirmed it. Drivers with `opens_own_session = True`
    (app mode) are always asked, even when the phone already looks in the slice, and their previous session is kept.
    """

    mode: str

    async def request(self, profile: ServiceProfile) -> SessionEvidence | None: ...


@dataclass
class ManualApnHandset:
    """Lab driver until the Android app exists: the operator selects the service APN on the phone."""

    notify: Callable[[str], None] = print
    mode: str = "manual-apn"

    opens_own_session: bool = False

    async def request(self, profile: ServiceProfile) -> None:
        self.notify(f"ACTION: on the OnePlus select APN '{profile.handset_dnn}' ({profile.service_id.value})")


@dataclass
class AppHandset:
    """App mode: the Smart Farm app opened the service session itself and reported its address (`phone_ip`).

    Confirms from the AMF/SMF logs that the phone holds that session in the service S-NSSAI and DNN for `hold_s`
    (instead of printing the manual APN instruction). The app keeps its default `internet` session, so nothing is
    replaced on the phone side.
    """

    evidence: Open5gsLogEvidenceProvider
    phone_ip: IPv4Address
    timeout_s: float = 15.0
    hold_s: float = 2.0
    mode: str = "app"
    opens_own_session: bool = True

    async def request(self, profile: ServiceProfile) -> SessionEvidence:
        return await self.evidence.await_active(
            EndpointRole.ONEPLUS_8T, profile.snssai, profile.handset_dnn, self.phone_ip,
            timeout_s=self.timeout_s, hold_s=self.hold_s,
        )


class SliceCoordinator:
    def __init__(
        self,
        evidence: Open5gsLogEvidenceProvider,
        un3gpp: Un3gppSliceClient,
        handset: HandsetDriver | None,
        *,
        catalog: dict[ServiceId, ServiceProfile] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._evidence = evidence
        self._un3gpp = un3gpp
        self._handset = handset  # None: only the UN3GPP UE is switched
        self._catalog = catalog or dict(SERVICE_CATALOG)
        self._clock = clock

    async def transition(
        self,
        service: ServiceId,
        *,
        un3gpp_timeout_s: float = 30.0,
        handset_timeout_s: float = 120.0,
        hold_s: float = 2.0,
        absent_timeout_s: float = 5.0,
    ) -> dict[str, Any]:
        profile = self._catalog[service]
        t0 = self._clock()
        record: dict[str, Any] = {
            "schema": "smartfarm.slice_transition.v1",
            "transition_id": str(uuid4()),
            "service": service.value,
            "sst": profile.snssai.sst,
            "sd": profile.snssai.sd,
            "handset_mode": self._handset.mode if self._handset else "not-switched",
            "started_at": datetime.now(UTC).isoformat(),
        }

        def elapsed() -> float:
            return round(self._clock() - t0, 3)

        cursor = self._evidence.cursor()
        try:
            before = await self._un3gpp.status()
        except Exception as error:  # noqa: BLE001 - recorded as the failure cause
            return self._fail(record, elapsed(), f"un3gpp status before transition: {error}")
        phone_before = self._evidence.current_session(EndpointRole.ONEPLUS_8T)
        record.update(
            un3gpp_previous_sd=before.sd if before else None,
            un3gpp_previous_ipv4=str(before.ipv4) if before else None,
            un3gpp_previous_pdu_session_id=before.pdu_session_id if before else None,
            handset_previous_sd=phone_before.snssai.sd if phone_before else None,
            handset_previous_dnn=phone_before.dnn if phone_before else None,
            handset_previous_ipv4=str(phone_before.ipv4) if phone_before else None,
        )

        async def switch_un3gpp() -> None:
            record["un3gpp_request_epoch"] = round(time.time(), 3)
            sent = await self._un3gpp.request(service)
            record["un3gpp_request_sent"] = sent
            record["un3gpp_request_s"] = elapsed()
            if not sent:
                record["un3gpp_already_in_slice"] = True
                return
            status = await self._un3gpp.await_status(
                service, before.pdu_session_id if before else None, timeout_s=un3gpp_timeout_s
            )
            record.update(un3gpp_accepted_s=elapsed(), un3gpp_pdu_session_id=status.pdu_session_id)
            session = await self._evidence.await_session(
                EndpointRole.UN3GPP_UE, profile.snssai, profile.dnn, cursor, timeout_s=un3gpp_timeout_s, hold_s=hold_s
            )
            record.update(
                un3gpp_ipv4=str(session.ipv4),
                un3gpp_core_confirmed_s=elapsed(),
                # core-side latency (PC1 clock, same host as the core logs): request sent -> new session established
                un3gpp_switch_core_s=round(log_time_to_epoch(session.established_at) - record["un3gpp_request_epoch"], 3),
                un3gpp_core_requested_at=session.requested_at,
                un3gpp_core_established_at=session.established_at,
            )

        app_mode = bool(getattr(self._handset, "opens_own_session", False))

        async def switch_handset() -> None:
            if self._handset is None:
                return
            if (not app_mode and phone_before and phone_before.snssai == profile.snssai
                    and phone_before.dnn == profile.handset_dnn):
                record["handset_already_in_slice"] = True
                return
            record["handset_request_epoch"] = round(time.time(), 3)
            confirmed = await self._handset.request(profile)
            record["handset_request_s"] = elapsed()
            session = confirmed if confirmed is not None else await self._evidence.await_session(
                EndpointRole.ONEPLUS_8T, profile.snssai, profile.handset_dnn, cursor,
                timeout_s=handset_timeout_s, hold_s=hold_s,
            )
            record.update(
                handset_ipv4=str(session.ipv4),
                handset_core_confirmed_s=elapsed(),
                handset_core_requested_at=session.requested_at,
                handset_core_established_at=session.established_at,
            )

        results = await asyncio.gather(switch_un3gpp(), switch_handset(), return_exceptions=True)
        failures = [f"{name}: {result}" for name, result in zip(("un3gpp", "handset"), results)
                    if isinstance(result, BaseException)]
        if failures:
            return self._fail(record, elapsed(), "; ".join(failures))

        replaced = [(EndpointRole.UN3GPP_UE, "un3gpp")]
        if app_mode:
            record["handset_previous_session_kept"] = True  # the app keeps its default session
        else:
            replaced.append((EndpointRole.ONEPLUS_8T, "handset"))
        for role, prefix in replaced:
            old, new = record.get(f"{prefix}_previous_ipv4"), record.get(f"{prefix}_ipv4")
            if old and new and old != new:
                removed_at = await self._evidence.await_absent(
                    role, IPv4Address(old), cursor, timeout_s=absent_timeout_s
                )
                record[f"{prefix}_previous_session_removed"] = removed_at is not None
                if removed_at is not None and record.get(f"{prefix}_core_established_at"):
                    # user-plane gap seen by the core: old session removed -> new session established
                    record[f"{prefix}_interruption_core_s"] = round(
                        log_time_to_epoch(record[f"{prefix}_core_established_at"]) - log_time_to_epoch(removed_at), 3
                    )
                if removed_at is None:
                    return self._fail(record, elapsed(), f"{prefix} previous session {old} still active")

        if app_mode:
            probe_target = IPv4Address(record["handset_ipv4"])  # the service session, not the default one
        else:
            phone_now = self._evidence.current_session(EndpointRole.ONEPLUS_8T)
            if phone_now is None:
                return self._fail(record, elapsed(), "handset has no active session for the probe")
            probe_target = phone_now.ipv4
        try:
            probe = await self._un3gpp.ping(probe_target)
        except Exception as error:  # noqa: BLE001
            return self._fail(record, elapsed(), f"probe: {error}")
        record.update(
            probe_target_ipv4=str(probe.target),
            probe_sent=probe.sent,
            probe_received=probe.received,
            probe_loss_ratio=round(probe.loss_ratio, 3),
            probe_rtt_min_ms=probe.rtt_min_ms,
            probe_rtt_avg_ms=probe.rtt_avg_ms,
            probe_rtt_max_ms=probe.rtt_max_ms,
        )
        if probe.received == 0:
            record["probe_pc1_to_handset_received"] = await self._pc1_ping(probe_target)
            return self._fail(record, elapsed(), "UE-to-UE probe through the core got no reply")
        record.update(outcome="slice_confirmed", cause=None, total_s=elapsed())
        return record

    @staticmethod
    async def _pc1_ping(target: IPv4Address) -> int | None:
        """Diagnostic for a failed probe: is the handset alive for PC1 (through the core)? Echo replies out of 3."""

        try:
            process = await asyncio.create_subprocess_exec(
                "ping", "-n", "-q", "-c", "3", "-i", "0.2", "-W", "1", str(target),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(process.communicate(), 10)
        except (OSError, TimeoutError):
            return None
        for line in out.decode().splitlines():
            if "received" in line:
                return int(line.split(",")[1].split()[0])
        return None

    @staticmethod
    def _fail(record: dict[str, Any], total_s: float, cause: str) -> dict[str, Any]:
        record.update(outcome="failed_safe", cause=cause, total_s=total_s)
        return record
