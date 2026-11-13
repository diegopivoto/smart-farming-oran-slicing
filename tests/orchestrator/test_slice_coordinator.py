from dataclasses import dataclass, field
from ipaddress import IPv4Address
from typing import Any

import pytest

from smartfarm.baseline.endpoints import EndpointRole
from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId, ServiceProfile, Snssai
from smartfarm.core.evidence import CurrentSession, EvidenceTimeout, SessionEvidence
from smartfarm.orchestrator.slice_coordinator import SliceCoordinator
from smartfarm.orchestrator.un3gpp_slice import PingResult, Un3gppSliceError, Un3gppSliceStatus

WEATHER, CROP = Snssai(sst=1, sd="000001"), Snssai(sst=1, sd="000002")


def un3gpp_status(sd: str, psi: int, ip: str) -> Un3gppSliceStatus:
    return Un3gppSliceStatus(int(sd[-1]), 1, sd, psi, "vrf-n3iwue", f"gretun-id-{psi + 1}-1", IPv4Address(ip), "accepted")


@dataclass
class FakeEvidence:
    phone: CurrentSession | None
    phone_after: CurrentSession | None = None
    missing: set[EndpointRole] = field(default_factory=set)
    still_active: set[str] = field(default_factory=set)
    sessions: dict[EndpointRole, str] = field(
        default_factory=lambda: {EndpointRole.UN3GPP_UE: "10.45.0.93", EndpointRole.ONEPLUS_8T: "10.45.0.94"}
    )

    def cursor(self) -> str:
        return "cursor"

    def current_session(self, role: EndpointRole) -> CurrentSession | None:
        return self.phone_after if self.phone_after is not None else self.phone

    async def await_session(self, role: EndpointRole, snssai: Snssai, dnn: str, since: Any, **_: Any) -> SessionEvidence:
        if role in self.missing:
            raise EvidenceTimeout(f"no stable {dnn} session for {role.value}")
        ipv4 = IPv4Address(self.sessions[role])
        if role is EndpointRole.ONEPLUS_8T:
            self.phone_after = CurrentSession(snssai=snssai, dnn=dnn, ipv4=ipv4, established_at="11/02 21:00:04.445")
        return SessionEvidence(role, "ref", snssai, dnn, ipv4, "11/02 21:00:04.440", "11/02 21:00:04.445", 2.0)

    async def await_absent(self, role: EndpointRole, ipv4: IPv4Address, since: Any, **_: Any) -> str | None:
        return None if str(ipv4) in self.still_active else "11/02 21:00:03.383"


@dataclass
class FakeUn3gpp:
    before: Un3gppSliceStatus | None
    reject: bool = False
    received: int = 5
    requests: list[ServiceId] = field(default_factory=list)

    async def status(self) -> Un3gppSliceStatus | None:
        return self.before

    async def request(self, service: ServiceId) -> bool:
        if self.before and self.before.sd == SERVICE_CATALOG[service].snssai.sd:
            return False
        self.requests.append(service)
        return True

    async def await_status(self, service: ServiceId, previous: int | None, **_: Any) -> Un3gppSliceStatus:
        if self.reject:
            raise Un3gppSliceError("N3IWUE slice switch rejected (active SD 000001)")
        return un3gpp_status(SERVICE_CATALOG[service].snssai.sd, (previous or 0) + 1, "10.45.0.93")

    async def ping(self, target: IPv4Address) -> PingResult:
        return PingResult(target, 5, self.received, 55.6, 60.9, 64.4)


@dataclass
class FakeHandset:
    mode: str = "fake"
    requested: list[str] = field(default_factory=list)

    async def request(self, profile: ServiceProfile) -> None:
        self.requested.append(profile.handset_dnn)


def phone_in(snssai: Snssai, dnn: str, ip: str) -> CurrentSession:
    return CurrentSession(snssai=snssai, dnn=dnn, ipv4=IPv4Address(ip), established_at="11/02 20:57:23.094")


def coordinator(evidence: FakeEvidence, un3gpp: FakeUn3gpp, handset: FakeHandset | None) -> SliceCoordinator:
    return SliceCoordinator(evidence, un3gpp, handset)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_happy_path_switches_both_ues_and_records_timings() -> None:
    handset = FakeHandset()
    evidence = FakeEvidence(phone=phone_in(WEATHER, "internet", "10.45.0.92"))
    un3gpp = FakeUn3gpp(before=un3gpp_status("000001", 1, "10.45.0.88"))

    record = await coordinator(evidence, un3gpp, handset).transition(ServiceId.CROP)

    assert record["outcome"] == "slice_confirmed", record["cause"]
    assert un3gpp.requests == [ServiceId.CROP] and handset.requested == ["crop"]
    assert record["un3gpp_ipv4"] == "10.45.0.93" and record["handset_ipv4"] == "10.45.0.94"
    assert record["un3gpp_previous_session_removed"] and record["handset_previous_session_removed"]
    assert record["probe_received"] == 5 and record["probe_target_ipv4"] == "10.45.0.94"
    for key in ("un3gpp_accepted_s", "un3gpp_core_confirmed_s", "handset_core_confirmed_s", "total_s"):
        assert isinstance(record[key], float)
    assert record["un3gpp_interruption_core_s"] == record["handset_interruption_core_s"] == 1.062


@pytest.mark.asyncio
async def test_already_in_slice_is_a_no_op_but_still_probed() -> None:
    handset = FakeHandset()
    evidence = FakeEvidence(phone=phone_in(CROP, "crop", "10.45.0.94"))
    un3gpp = FakeUn3gpp(before=un3gpp_status("000002", 2, "10.45.0.93"))

    record = await coordinator(evidence, un3gpp, handset).transition(ServiceId.CROP)

    assert record["outcome"] == "slice_confirmed"
    assert record["un3gpp_already_in_slice"] and record["handset_already_in_slice"]
    assert un3gpp.requests == [] and handset.requested == []


@pytest.mark.parametrize(
    ("evidence", "un3gpp", "cause"),
    [
        (FakeEvidence(phone=phone_in(WEATHER, "internet", "10.45.0.92")),
         FakeUn3gpp(before=un3gpp_status("000001", 1, "10.45.0.88"), reject=True), "rejected"),
        (FakeEvidence(phone=phone_in(WEATHER, "internet", "10.45.0.92"), missing={EndpointRole.ONEPLUS_8T}),
         FakeUn3gpp(before=un3gpp_status("000001", 1, "10.45.0.88")), "handset: no stable crop session"),
        (FakeEvidence(phone=phone_in(WEATHER, "internet", "10.45.0.92"), still_active={"10.45.0.88"}),
         FakeUn3gpp(before=un3gpp_status("000001", 1, "10.45.0.88")), "previous session 10.45.0.88 still active"),
        (FakeEvidence(phone=phone_in(WEATHER, "internet", "10.45.0.92")),
         FakeUn3gpp(before=un3gpp_status("000001", 1, "10.45.0.88"), received=0), "probe through the core got no reply"),
    ],
)
@pytest.mark.asyncio
async def test_failures_end_in_failed_safe_with_cause(evidence: FakeEvidence, un3gpp: FakeUn3gpp, cause: str) -> None:
    record = await coordinator(evidence, un3gpp, FakeHandset()).transition(ServiceId.CROP)

    assert record["outcome"] == "failed_safe"
    assert cause in record["cause"]


@pytest.mark.asyncio
async def test_un3gpp_only_mode_does_not_touch_the_handset() -> None:
    evidence = FakeEvidence(phone=phone_in(WEATHER, "internet", "10.45.0.92"))
    record = await coordinator(evidence, FakeUn3gpp(before=un3gpp_status("000001", 1, "10.45.0.88")), None).transition(
        ServiceId.DRONE
    )

    assert record["outcome"] == "slice_confirmed" and record["handset_mode"] == "not-switched"
    assert "handset_ipv4" not in record


@dataclass
class AppEvidence(FakeEvidence):
    """The phone already holds its default session and the service session it opened before the request."""

    confirm_fails: bool = False

    async def await_active(self, role: EndpointRole, snssai: Snssai, dnn: str, ipv4: IPv4Address, **_: Any) -> SessionEvidence:
        if self.confirm_fails:
            raise EvidenceTimeout(f"{role.value} has no active {dnn} session {ipv4}")
        return SessionEvidence(role, "ref", snssai, dnn, ipv4, "11/02 21:00:01.000", "11/02 21:00:01.005", 2.0)

    async def await_session(self, role: EndpointRole, snssai: Snssai, dnn: str, since: Any, **kw: Any) -> SessionEvidence:
        assert role is EndpointRole.UN3GPP_UE, "app mode must not wait for a new phone session after the cursor"
        return await super().await_session(role, snssai, dnn, since, **kw)

    async def await_absent(self, role: EndpointRole, ipv4: IPv4Address, since: Any, **kw: Any) -> str | None:
        assert role is EndpointRole.UN3GPP_UE, "app mode keeps the phone's previous (default) session"
        return await super().await_absent(role, ipv4, since, **kw)


@pytest.mark.asyncio
async def test_app_mode_confirms_the_session_the_app_opened_and_probes_it() -> None:
    from smartfarm.orchestrator.slice_coordinator import AppHandset

    # the latest phone session is already crop (the app opened it): app mode still confirms the reported address
    evidence = AppEvidence(phone=phone_in(CROP, "crop", "10.45.128.2"))
    un3gpp = FakeUn3gpp(before=un3gpp_status("000001", 1, "10.45.0.88"))
    handset = AppHandset(evidence, IPv4Address("10.45.128.2"))  # type: ignore[arg-type]

    record = await SliceCoordinator(evidence, un3gpp, handset).transition(ServiceId.CROP)  # type: ignore[arg-type]

    assert record["outcome"] == "slice_confirmed", record["cause"]
    assert record["handset_mode"] == "app" and "handset_already_in_slice" not in record
    assert record["handset_ipv4"] == "10.45.128.2" and record["handset_previous_session_kept"]
    assert record["probe_target_ipv4"] == "10.45.128.2" and un3gpp.requests == [ServiceId.CROP]


@pytest.mark.asyncio
async def test_app_mode_fails_safe_when_the_reported_session_is_not_in_the_core() -> None:
    from smartfarm.orchestrator.slice_coordinator import AppHandset

    evidence = AppEvidence(phone=phone_in(WEATHER, "internet", "10.45.0.15"), confirm_fails=True)
    un3gpp = FakeUn3gpp(before=un3gpp_status("000001", 1, "10.45.0.88"))
    handset = AppHandset(evidence, IPv4Address("10.45.128.9"))  # type: ignore[arg-type]

    record = await SliceCoordinator(evidence, un3gpp, handset).transition(ServiceId.CROP)  # type: ignore[arg-type]

    assert record["outcome"] == "failed_safe" and "handset: " in record["cause"] and "10.45.128.9" in record["cause"]
