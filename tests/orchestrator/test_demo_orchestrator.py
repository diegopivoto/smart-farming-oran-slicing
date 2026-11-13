import asyncio
import json
import math
import urllib.error
import urllib.request
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from smartfarm.baseline.endpoints import EndpointRole
from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId
from smartfarm.orchestrator.demo import (
    IP_CONFLICT_REASON,
    DemoApi,
    DemoConfig,
    DemoOrchestrator,
    JsonlLog,
    RequestError,
    _finite,
    sla_view,
)
from smartfarm.orchestrator.demo_dryrun import DryRunLab
from smartfarm.orchestrator.un3gpp_slice import PingResult

PHONE_WEATHER, PHONE_CROP, PHONE_DRONE = "10.45.192.4", "10.45.128.2", "10.45.160.6"
PHONE_DEFAULT = "10.45.0.15"


def fast_config(**overrides: Any) -> DemoConfig:
    values: dict[str, Any] = dict(segment_s=3600, renew_margin_s=5.0, renew_pause_s=0.0, lease_s=0.0,
                                  phone_check_s=0.05, phone_timeout_s=0.3, hold_s=0.0, settle_s=0.0,
                                  flow_timeout_s=5.0, agent_timeout_s=1.0, require_phone_client=False)
    values.update(overrides)
    return DemoConfig(**values)


_created: list[DemoOrchestrator] = []


def make(tmp_path: Path, lab: DryRunLab | None = None, **config: Any) -> tuple[DemoOrchestrator, DryRunLab]:
    lab = lab or DryRunLab(switch_delay_s=0.01)
    log = JsonlLog(tmp_path, echo=None)
    orchestrator = DemoOrchestrator(lab, lab, log, fast_config(**config))  # type: ignore[arg-type]
    _created.append(orchestrator)
    return orchestrator, lab


@pytest_asyncio.fixture(autouse=True)
async def _shutdown_orchestrators() -> Any:
    yield
    while _created:
        await _created.pop().shutdown()


def body(service: str, phone_ip: str, app_session: str = "a" * 32) -> dict[str, str]:
    return {"service": service, "phone_ip": phone_ip, "app_session": app_session}


async def wait_state(orch: DemoOrchestrator, session_id: str, *states: str, timeout: float = 3.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        session = orch.session(session_id, touch=False)  # not a poll: must not renew the lease
        assert session is not None
        if session.state in states:
            return orch.view(session)
        assert asyncio.get_running_loop().time() < deadline, f"stuck in {session.state}: {session.cause}"
        await asyncio.sleep(0.01)


def log_lines(tmp_path: Path) -> list[dict[str, Any]]:
    (path,) = tmp_path.glob("*/orchestrator.jsonl")
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.asyncio
async def test_happy_path_switches_the_un3gpp_ue_and_starts_the_generator_towards_the_phone(tmp_path: Path) -> None:
    orch, lab = make(tmp_path)
    status, view = await orch.submit(body("crop", PHONE_CROP), PHONE_DEFAULT)
    assert status == 202 and view["state"] == "accepted"

    final = await wait_state(orch, view["session"], "streaming")

    assert [h["state"] for h in final["history"]] == [
        "accepted", "un3gpp_switching", "un3gpp_in_slice", "generator_started", "streaming"]
    assert final["un3gpp_ip"] == final["generator_ip"] == str(lab.ipv4) and lab.sd == "000002"
    assert lab.slice_requests == [ServiceId.CROP]
    (start,) = [t for t in lab.traffic if t[:2] == ("traffic", "crop")]
    assert start[2:4] == (PHONE_CROP, "3600") and start[5] == "20000000"  # catalog rate
    assert final["generator_run_id"] == start[4]
    events = [line["event"] for line in log_lines(tmp_path)]
    assert "slice_transition" in events and "generator_segment" in events and "phone_confirmed" in events
    transition = next(line for line in log_lines(tmp_path) if line["event"] == "slice_transition")["record"]
    assert transition["handset_mode"] == "app" and transition["handset_previous_session_kept"]
    assert transition["probe_target_ipv4"] == PHONE_CROP  # the service session, not the default one


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (body("irrigation", PHONE_CROP), "service must be"),
        (body("crop", "192.168.56.101"), "inside 10.45.0.0/16"),
        (body("crop", "10.45.x.1"), "IPv4"),
        (body("crop", PHONE_CROP, "bad id with spaces"), "app_session"),
        (["crop"], "JSON object"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_requests_are_refused_before_anything_is_switched(
    tmp_path: Path, payload: Any, message: str
) -> None:
    orch, lab = make(tmp_path)
    with pytest.raises(RequestError) as error:
        await orch.submit(payload, PHONE_DEFAULT)
    assert error.value.status == 400 and message in error.value.message
    assert lab.slice_requests == [] and orch.active is None


@pytest.mark.asyncio
async def test_phone_ip_not_in_the_core_fails_without_touching_the_un3gpp_ue(tmp_path: Path) -> None:
    orch, lab = make(tmp_path, DryRunLab(switch_delay_s=0.01, auto_phone=False))
    lab.add_phone_session(PHONE_CROP, "crop")
    _, view = await orch.submit(body("drone", PHONE_CROP), PHONE_DEFAULT)  # crop address claimed for drone

    final = await wait_state(orch, view["session"], "failed")

    assert "is not an active drone session" in final["cause"]
    assert lab.slice_requests == [] and lab.traffic == [] and orch.active is None


@pytest.mark.asyncio
async def test_client_must_be_one_of_the_phone_sessions(tmp_path: Path) -> None:
    orch, lab = make(tmp_path, require_phone_client=True)
    lab.add_phone_session(PHONE_DEFAULT, "internet")
    with pytest.raises(RequestError) as error:
        await orch.submit(body("crop", PHONE_CROP), "10.45.0.3")  # the UN3GPP UE, not the phone
    assert error.value.status == 403
    status, _ = await orch.submit(body("crop", PHONE_CROP), PHONE_DEFAULT)
    assert status == 202


@pytest.mark.asyncio
async def test_requests_are_idempotent_per_app_session(tmp_path: Path) -> None:
    orch, lab = make(tmp_path)
    _, first = await orch.submit(body("weather", PHONE_WEATHER, "s1"), PHONE_DEFAULT)
    status, again = await orch.submit(body("weather", PHONE_WEATHER, "s1"), PHONE_DEFAULT)
    assert status == 200 and again["session"] == first["session"]
    with pytest.raises(RequestError) as error:
        await orch.submit(body("drone", PHONE_DRONE, "s1"), PHONE_DEFAULT)
    assert error.value.status == 409
    await wait_state(orch, first["session"], "streaming")
    assert len([t for t in lab.traffic if t[:2] == ("traffic", "weather")]) == 1


@pytest.mark.asyncio
async def test_a_new_service_supersedes_the_active_session(tmp_path: Path) -> None:
    orch, lab = make(tmp_path)
    _, crop = await orch.submit(body("crop", PHONE_CROP, "s1"), PHONE_DEFAULT)
    await wait_state(orch, crop["session"], "streaming")
    lab.release_phone_session(PHONE_CROP)  # the app released the crop network when going back

    _, drone = await orch.submit(body("drone", PHONE_DRONE, "s2"), PHONE_DEFAULT)
    old = orch.view(orch.session(crop["session"], touch=False))  # type: ignore[arg-type]
    assert old["state"] == "superseded" and "drone" in old["cause"]
    await wait_state(orch, drone["session"], "streaming")

    stop_index = lab.traffic.index(("traffic", "stop"))
    assert stop_index < lab.traffic.index(("slice", "3"))  # old generator stopped before the switch
    assert lab.sd == "000003" and orch.active is not None and orch.active.id == drone["session"]


@pytest.mark.asyncio
async def test_superseding_during_the_switch_cancels_the_old_flow(tmp_path: Path) -> None:
    orch, lab = make(tmp_path, DryRunLab(switch_delay_s=0.3))
    _, crop = await orch.submit(body("crop", PHONE_CROP, "s1"), PHONE_DEFAULT)
    await wait_state(orch, crop["session"], "un3gpp_switching")
    _, weather = await orch.submit(body("weather", PHONE_WEATHER, "s2"), PHONE_DEFAULT)

    final = await wait_state(orch, weather["session"], "streaming")

    assert orch.view(orch.session(crop["session"]))["state"] == "superseded"  # type: ignore[arg-type]
    assert not any(t[:2] == ("traffic", "crop") for t in lab.traffic)
    assert final["sd"] == "000001"


@pytest.mark.asyncio
async def test_delete_stops_the_generator_and_leaves_the_un3gpp_ue_in_the_slice(tmp_path: Path) -> None:
    orch, lab = make(tmp_path)
    _, view = await orch.submit(body("drone", PHONE_DRONE), PHONE_DEFAULT)
    await wait_state(orch, view["session"], "streaming")

    stopped = await orch.stop(view["session"])

    assert stopped is not None and stopped["state"] == "stopped"
    assert lab.traffic[-1] == ("traffic", "stop") and lab.running == 0
    assert lab.sd == "000003" and lab.slice_requests == [ServiceId.DRONE] and orch.active is None
    assert await orch.stop("0" * 16) is None


@pytest.mark.asyncio
async def test_delete_of_a_superseded_session_does_not_stop_the_new_generator(tmp_path: Path) -> None:
    orch, lab = make(tmp_path)
    _, crop = await orch.submit(body("crop", PHONE_CROP, "s1"), PHONE_DEFAULT)
    await wait_state(orch, crop["session"], "streaming")
    _, drone = await orch.submit(body("drone", PHONE_DRONE, "s2"), PHONE_DEFAULT)
    await wait_state(orch, drone["session"], "streaming")
    stops = lab.traffic.count(("traffic", "stop"))

    late = await orch.stop(crop["session"])  # the app's DELETE arrives after its new POST

    assert late is not None and late["state"] == "superseded"
    assert lab.traffic.count(("traffic", "stop")) == stops and lab.running == 1


@pytest.mark.asyncio
async def test_generator_segments_are_renewed_for_a_continuous_stream(tmp_path: Path) -> None:
    orch, lab = make(tmp_path, segment_s=1, renew_margin_s=0.85)
    _, view = await orch.submit(body("weather", PHONE_WEATHER), PHONE_DEFAULT)
    await wait_state(orch, view["session"], "streaming")
    await asyncio.sleep(0.5)

    starts = [t for t in lab.traffic if t[:2] == ("traffic", "weather")]
    assert len(starts) >= 3 and len({t[4] for t in starts}) == len(starts)  # new run id per segment
    assert lab.running == 1  # each renewal stops the previous segment first
    session = orch.session(view["session"])
    assert session is not None and session.state == "streaming" and session.generator_run_id == starts[-1][4]
    await orch.shutdown()
    assert lab.running == 0


@pytest.mark.asyncio
async def test_lease_expiry_stops_the_generator(tmp_path: Path) -> None:
    orch, lab = make(tmp_path, lease_s=0.2)
    _, view = await orch.submit(body("crop", PHONE_CROP), PHONE_DEFAULT)
    await wait_state(orch, view["session"], "streaming")

    final = await wait_state(orch, view["session"], "stopped", timeout=2.0)

    assert "lease expired" in final["cause"] and lab.running == 0


@pytest.mark.asyncio
async def test_polls_keep_the_lease_alive(tmp_path: Path) -> None:
    orch, lab = make(tmp_path, lease_s=0.2)
    _, view = await orch.submit(body("crop", PHONE_CROP), PHONE_DEFAULT)
    for _ in range(8):
        await asyncio.sleep(0.05)
        orch.session(view["session"])  # GET touches the lease
    assert orch.view(orch.session(view["session"]))["state"] == "streaming"  # type: ignore[arg-type]
    await orch.shutdown()


@pytest.mark.asyncio
async def test_phone_session_release_stops_the_generator(tmp_path: Path) -> None:
    orch, lab = make(tmp_path)
    _, view = await orch.submit(body("drone", PHONE_DRONE), PHONE_DEFAULT)
    await wait_state(orch, view["session"], "streaming")
    lab.release_phone_session(PHONE_DRONE)

    final = await wait_state(orch, view["session"], "stopped")

    assert "was released" in final["cause"] and lab.running == 0


@pytest.mark.asyncio
async def test_un3gpp_rejection_ends_in_failed_with_the_cause_and_no_generator(tmp_path: Path) -> None:
    orch, lab = make(tmp_path, DryRunLab(switch_delay_s=0.01, reject=True))
    _, view = await orch.submit(body("crop", PHONE_CROP), PHONE_DEFAULT)

    final = await wait_state(orch, view["session"], "failed")

    assert "rejected" in final["cause"] and not any(t[0] == "traffic" for t in lab.traffic)


@pytest.mark.parametrize("tolerate", [False, True])
@pytest.mark.asyncio
async def test_probe_failure_policy(tmp_path: Path, tolerate: bool) -> None:
    orch, lab = make(tmp_path, DryRunLab(switch_delay_s=0.01, probe_received=0), tolerate_probe_failure=tolerate)
    _, view = await orch.submit(body("crop", PHONE_CROP), PHONE_DEFAULT)

    final = await wait_state(orch, view["session"], "failed", "streaming")

    assert final["state"] == ("streaming" if tolerate else "failed")
    if not tolerate:
        assert "probe" in final["cause"]
    await orch.shutdown()


@pytest.mark.asyncio
async def test_already_in_slice_skips_the_switch_but_confirms_the_phone(tmp_path: Path) -> None:
    orch, lab = make(tmp_path)  # DryRunLab starts in the weather slice
    _, view = await orch.submit(body("weather", PHONE_WEATHER), PHONE_DEFAULT)

    final = await wait_state(orch, view["session"], "streaming")

    assert lab.slice_requests == [] and final["un3gpp_ip"] == "10.45.0.3"
    assert next(h for h in final["history"] if h["state"] == "un3gpp_in_slice")["already_in_slice"] is True


@pytest.mark.asyncio
async def test_unresponsive_agent_times_out_instead_of_blocking(tmp_path: Path) -> None:
    class Hanging(DryRunLab):
        async def start_traffic(self, *args: Any, **kwargs: Any) -> str:
            await asyncio.sleep(60)
            return ""

    orch, lab = make(tmp_path, Hanging(switch_delay_s=0.01), agent_timeout_s=0.1)
    _, view = await orch.submit(body("crop", PHONE_CROP), PHONE_DEFAULT)

    final = await wait_state(orch, view["session"], "failed")

    assert "did not answer" in final["cause"]
    assert lab.traffic[-1] == ("traffic", "stop")  # the agent may have started it: stopped anyway


@pytest.mark.asyncio
async def test_view_carries_the_catalog_sla_and_offered_rate(tmp_path: Path) -> None:
    orch, _ = make(tmp_path)
    _, view = await orch.submit(body("drone", PHONE_DRONE), PHONE_DEFAULT)
    profile = SERVICE_CATALOG[ServiceId.DRONE]

    assert view["offered_rate_bps"] == profile.offered_rate_bps == 40_000_000
    assert [m["name"] for m in view["sla"]] == [m.name for m in profile.sla]
    p10 = next(m for m in view["sla"] if m["name"] == "goodput_p10")
    assert p10 == {"name": "goodput_p10", "operator": "ge", "threshold": 34_000_000.0, "unit": "bit/s", "window_s": 5}
    crop = sla_view(SERVICE_CATALOG[ServiceId.CROP])
    assert {m["unit"] for m in crop} == {"bit/s", "s", "ratio", "count"}
    assert next(m for m in crop if m["name"] == "accepted_corrupt_objects")["threshold"] == 0.0
    json.dumps(view, allow_nan=False)  # the phone's org.json rejects NaN


@pytest.mark.asyncio
async def test_view_never_carries_nan_even_from_a_failed_probe(tmp_path: Path) -> None:
    class NanProbe(DryRunLab):
        async def ping(self, target: IPv4Address) -> PingResult:
            return PingResult(target, 5, 0, math.nan, math.nan, math.nan)

    orch, _ = make(tmp_path, NanProbe(switch_delay_s=0.01), tolerate_probe_failure=True)
    api = DemoApi(orch, JsonlLog(tmp_path, echo=None))
    _, view = await orch.submit(body("crop", PHONE_CROP), PHONE_DEFAULT)
    await wait_state(orch, view["session"], "streaming")

    status, polled = await api.dispatch("GET", f"/api/v1/session/{view['session']}", b"", PHONE_DEFAULT)
    assert status == 200 and polled["probe_rtt_avg_ms"] is None
    json.dumps(_finite(polled), allow_nan=False)
    assert all(not isinstance(v, float) or math.isfinite(v) for h in _finite(polled["history"]) for v in h.values())


PHONE_INTERNET_30 = "10.45.0.21"  # live 2026-11-03: the phone's internet session was 10.45.0.21 in 10.45.0.20/30


@pytest.mark.asyncio
async def test_un3gpp_address_in_a_phone_subnet_triggers_one_more_slice_hop(tmp_path: Path) -> None:
    lab = DryRunLab(switch_delay_s=0.01)
    lab._next_host = 23  # the next UN3GPP address is 10.45.0.23, broadcast of the phone's 10.45.0.20/30
    lab.add_phone_session(PHONE_INTERNET_30, "internet")
    orch, _ = make(tmp_path, lab)
    _, view = await orch.submit(body("crop", PHONE_CROP), PHONE_INTERNET_30)

    final = await wait_state(orch, view["session"], "streaming")

    assert lab.slice_requests == [ServiceId.CROP, ServiceId.WEATHER, ServiceId.CROP]  # away and back
    assert final["un3gpp_ip"] == "10.45.0.25" and lab.sd == "000002"
    assert final["reason"] == IP_CONFLICT_REASON and final["ip_conflict_hops"] == 1
    states = [h["state"] for h in final["history"]]
    assert states == ["accepted", "un3gpp_switching", "un3gpp_switching", "un3gpp_in_slice", "generator_started",
                      "streaming"]
    hop_entry = final["history"][2]
    assert hop_entry["reason"] == IP_CONFLICT_REASON and hop_entry["un3gpp_ip"] == "10.45.0.23"
    assert hop_entry["phone_subnet"] == "10.45.0.20/30"
    (start,) = [t for t in lab.traffic if t[:2] == ("traffic", "crop")]  # generator only after the clean address
    assert lab.traffic.index(start) > lab.traffic.index(("slice", "1"))
    events = {line["event"] for line in log_lines(tmp_path)}
    assert {"un3gpp_ip_conflict", "slice_hop"} <= events
    conflict = next(line for line in log_lines(tmp_path) if line["event"] == "un3gpp_ip_conflict")
    assert conflict["phone_session_ip"] == PHONE_INTERNET_30 and conflict["un3gpp_ip"] == "10.45.0.23"


@pytest.mark.asyncio
async def test_un3gpp_already_in_slice_with_a_clashing_address_also_hops(tmp_path: Path) -> None:
    lab = DryRunLab(switch_delay_s=0.01, ipv4=IPv4Address("10.45.0.22"))  # weather slice, inside 10.45.0.20/30
    lab.add_phone_session(PHONE_INTERNET_30, "internet")
    orch, _ = make(tmp_path, lab)
    _, view = await orch.submit(body("weather", PHONE_WEATHER), PHONE_INTERNET_30)

    final = await wait_state(orch, view["session"], "streaming")

    assert lab.slice_requests == [ServiceId.CROP, ServiceId.WEATHER] and final["un3gpp_ip"] == "10.45.0.5"
    assert final["reason"] == IP_CONFLICT_REASON


@pytest.mark.asyncio
async def test_a_clash_that_persists_after_the_extra_hop_fails_with_the_reason(tmp_path: Path) -> None:
    class StickyPool(DryRunLab):
        async def await_status(self, service: ServiceId, previous_pdu_id: int | None, *,
                               timeout_s: float = 20.0) -> Any:
            status = await super().await_status(service, previous_pdu_id, timeout_s=timeout_s)
            self.ipv4 = IPv4Address("10.45.0.23")  # the core keeps handing out the clashing address
            return status

    lab = StickyPool(switch_delay_s=0.01)
    lab.add_phone_session(PHONE_INTERNET_30, "internet")
    orch, _ = make(tmp_path, lab)
    _, view = await orch.submit(body("drone", PHONE_DRONE), PHONE_INTERNET_30)

    final = await wait_state(orch, view["session"], "failed")

    assert IP_CONFLICT_REASON in final["cause"] and "10.45.0.20/30" in final["cause"]
    assert final["ip_conflict_hops"] == 1 and not any(t[:2] == ("traffic", "drone") for t in lab.traffic)


@pytest.mark.asyncio
async def test_addresses_outside_the_phone_subnets_do_not_hop(tmp_path: Path) -> None:
    lab = DryRunLab(switch_delay_s=0.01)
    lab._next_host = 24  # 10.45.0.24 is outside 10.45.0.20/30
    lab.add_phone_session(PHONE_INTERNET_30, "internet")
    orch, _ = make(tmp_path, lab)
    _, view = await orch.submit(body("crop", PHONE_CROP), PHONE_INTERNET_30)

    final = await wait_state(orch, view["session"], "streaming")

    assert lab.slice_requests == [ServiceId.CROP] and final["reason"] is None and final["ip_conflict_hops"] == 0


# ------------------------------------------------------------------------------------------------- HTTP layer


def http(method: str, url: str, payload: Any = None, raw: bytes | None = None) -> tuple[int, dict[str, Any]]:
    data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.mark.asyncio
async def test_http_api_end_to_end_on_loopback(tmp_path: Path) -> None:
    orch, lab = make(tmp_path)
    api = DemoApi(orch, JsonlLog(tmp_path, echo=None), dry_run=True)
    server = await api.serve("127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/v1"
    try:
        status, created = await asyncio.to_thread(http, "POST", f"{base}/service", body("drone", PHONE_DRONE))
        assert status == 202 and created["poll"] == f"/api/v1/session/{created['session']}"
        for _ in range(200):
            status, view = await asyncio.to_thread(http, "GET", f"{base}/session/{created['session']}")
            if view["state"] == "streaming":
                break
            await asyncio.sleep(0.01)
        assert status == 200 and view["state"] == "streaming" and view["generator_ip"] == str(lab.ipv4)
        status, health = await asyncio.to_thread(http, "GET", f"{base}/health")
        assert status == 200 and health["active_session"]["session"] == created["session"]
        assert (await asyncio.to_thread(http, "GET", f"{base}/service"))[0] == 405
        assert (await asyncio.to_thread(http, "GET", f"{base}/session/{'0' * 16}"))[0] == 404
        assert (await asyncio.to_thread(http, "POST", f"{base}/service", raw=b"{nope"))[0] == 400
        assert (await asyncio.to_thread(http, "POST", f"{base}/service", raw=b"x" * 5000))[0] == 413
        assert (await asyncio.to_thread(http, "GET", f"{base}/../etc"))[0] == 404
        status, stopped = await asyncio.to_thread(http, "DELETE", f"{base}/session/{created['session']}")
        assert status == 200 and stopped["state"] == "stopped" and lab.running == 0
    finally:
        server.close()
        await server.wait_closed()
    requests = [line for line in log_lines(tmp_path) if line["event"] == "request"]
    assert requests[0]["method"] == "POST" and requests[0]["service"] == "drone"
    assert requests[0]["phone_ip"] == PHONE_DRONE and requests[0]["status"] == 202


@pytest.mark.asyncio
async def test_api_refuses_clients_outside_the_ue_pool(tmp_path: Path) -> None:
    orch, _ = make(tmp_path)
    api = DemoApi(orch, JsonlLog(tmp_path, echo=None))
    status, payload = await api.dispatch("GET", "/api/v1/health", b"", "192.168.56.103")
    assert status == 403 and "UE pool" in payload["error"]
    assert (await api.dispatch("GET", "/api/v1/health", b"", "10.45.0.15"))[0] == 200


def test_config_respects_the_node_agent_duration_limit() -> None:
    with pytest.raises(ValueError):
        DemoConfig(segment_s=3601)
    with pytest.raises(ValueError):
        DemoConfig(segment_s=5, renew_margin_s=5)


def test_dry_run_lab_reports_the_phone_sessions() -> None:
    lab = DryRunLab()
    lab.add_phone_session(PHONE_DEFAULT, "internet")
    lab.add_phone_session(PHONE_CROP, "crop")
    sessions = lab.active_sessions(EndpointRole.ONEPLUS_8T)
    assert {s.ipv4 for s in sessions} == {IPv4Address(PHONE_DEFAULT), IPv4Address(PHONE_CROP)}
    assert next(s for s in sessions if s.dnn == "crop").snssai.sd == "000002"


def test_demo_orchestrator_never_loads_the_e2_control_client() -> None:
    """The demo modules never load the E2 client: only a `ddqn` session loads the lab port (demo_oran_lab.py), so
    the default ran-default demo cannot open the FlexRIC xApp socket."""
    import subprocess
    import sys

    script = Path(__file__).resolve().parents[2] / "deployment/scripts/demo_orchestrator.py"
    code = (
        "import runpy, sys; sys.argv = ['demo_orchestrator.py', '--help']\n"
        "try:\n    runpy.run_path(%r, run_name='__main__')\nexcept SystemExit:\n    pass\n"
        "import slice_switch, smartfarm.orchestrator.demo, smartfarm.orchestrator.demo_dryrun\n"
        "import smartfarm.orchestrator.oran_view, smartfarm.orchestrator.oran_agent, smartfarm.rl.policy\n"
        "sys.stderr.write(repr(sorted(m for m in sys.modules if m.startswith('smartfarm.e2'))))\n"
    ) % str(script)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                          env={"PYTHONPATH": str(script.parent), "PATH": "/usr/bin:/bin"}, check=True)
    assert done.stderr.strip().endswith("[]"), done.stderr
    assert "smartfarm-sf.sock" not in script.read_text()
    oran_view = script.parents[2] / "src/smartfarm/orchestrator/oran_view.py"
    assert "smartfarm-sf.sock" not in oran_view.read_text()  # the O-RAN card reads the xApp journal only
