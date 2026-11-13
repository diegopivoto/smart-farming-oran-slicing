"""Read-only O-RAN view of the demo orchestrator (smartfarm.orchestrator.oran_view) and its place in the API."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from smartfarm.orchestrator.demo import DemoApi, DemoConfig, DemoOrchestrator, JsonlLog
from smartfarm.orchestrator.demo_dryrun import DryRunLab, FakeOranJournal, FakeOutcome, FakeRadio
from smartfarm.orchestrator.oran_view import JournalTail, OranMonitor, default_journal, tail_lines

NOW = 1_793_918_300_000_000_000  # PC1 wall clock (ns) of the synthetic journals
SD_DRONE, SD_CROP = "000003", "000002"


def indication(node: str, labels: list[str], received_at_ns: int = NOW - 100_000_000, seq: int = 1) -> str:
    return json.dumps({"type": "indication", "epoch": "e", "seq": seq, "received_at_ns": received_at_ns,
                       "node": node, "nb_id": 1, "labels": labels})


def ul_ue(override: bool, mcs: int = 8, rb: int = 132) -> str:
    return (f"sf=0.1;kind=un3gpp_ue;ue=1;override={int(override)};mcs_cmd={mcs};rb_first=0;rb_cmd={rb};mcs_obs={mcs};"
            f"rb_obs={rb if override else 6};la_mcs=8;snr_db=22.79;pdus=145;mac_mbps=0.990;age_ms=894")


def ul_outcome(action_id: int, status: str, mcs: int, rb: int, at_ns: int) -> str:
    return (f"sf=0.1;kind=un3gpp_outcome;action_id={action_id};mac_seq=7;status={status};dup=0;ue=1;mcs={mcs};"
            f"rb={rb};at_ns={at_ns}")


def dl_outcome(action_id: int, status: str, prb: int, at_ns: int, sd: str = SD_DRONE) -> str:
    return (f"sf=0.1;kind=outcome;action_id={action_id};status={status};dup=0;sst=1;sd={sd};rnti=0;requested={prb};"
            f"applied={prb};previous=106;token=c1d43d7f92afd37a;expires_at_ns={at_ns + 30_000_000_000};version=5;"
            f"at_ns={at_ns}")


def dl_rule(prb: int, action_id: int, sd: str = SD_DRONE) -> str:
    return (f"sf=0.1;kind=rule;sst=1;sd={sd};rnti=0;prb={prb};action_id={action_id};"
            f"expires_at_ns={NOW + 20_000_000_000};version=5")


def dl_ue(prb: int, action_id: int, sd: str = SD_DRONE) -> str:
    return f"sf=0.1;kind=ue;rnti=41972;sst=1;sd={sd};prb={prb};action_id={action_id}"


COUNTERS = "sf=0.1;kind=counters;lookups=1;unmapped=0;expired=0;fallback=0;capped=0;bypass_retx=0;version=5"


def monitor(lines: list[str], **kw: Any) -> OranMonitor:
    return OranMonitor(lambda: lines, clock_ns=lambda: NOW, cache_s=0.0, **kw)


def test_ran_defaults_link_adaptation_and_no_quota() -> None:
    """The lab journal at rest (2026-11-05): BS link adaptation, no quota rule, last actions = safe configuration."""
    lines = [
        indication("oai", [dl_outcome(11, "APPLIED", 106, NOW - 3_600_000_000_000), dl_ue(106, 0), COUNTERS]),
        indication("un3gpp", [ul_outcome(10, "RELEASED", 0, 0, NOW - 3_600_000_000_000), ul_ue(False)]),
    ]
    v = monitor(lines).view(SD_DRONE)
    assert v["available"] is True and v["controller"] == "ran-default" and v["decided_by"] == "RAN defaults"
    assert v["agent_running"] is False
    ul, dl = v["uplink"], v["downlink"]
    assert ul["mode"] == "link-adaptation" and ul["mcs"] is None and ul["label"] == "BS link adaptation (MCS 8)"
    assert ul["last_action"] == {"action_id": 10, "status": "RELEASED", "ok": True, "value": "BS link adaptation",
                                 "age_s": 3600.0}
    assert dl["prb_quota"] == 106 and dl["by_e2"] is False and dl["label"] == "No quota · 106 PRB"
    assert dl["last_action"]["status"] == "APPLIED" and dl["last_action"]["ok"] is True
    assert v["history"] == []  # older than HISTORY_MAX_AGE_S


def test_e2_override_and_quota_rule_in_force() -> None:
    lines = [
        indication("un3gpp", [ul_outcome(20, "CONFIRMED", 10, 66, NOW - 4_000_000_000), ul_ue(True, 10, 66)]),
        indication("oai", [dl_outcome(21, "APPLIED", 52, NOW - 3_000_000_000), dl_rule(52, 21), dl_ue(52, 21)]),
    ]
    v = monitor(lines).view(SD_DRONE)
    assert v["controller"] == "e2-action" and v["decided_by"] == "E2 action (O-RAN xApp)"
    assert v["uplink"]["label"] == "MCS 10 · 66 RB" and v["uplink"]["by_e2"] is True
    assert v["uplink"]["readback"].startswith("MAC uses MCS 10 · 66 RB")
    assert v["downlink"]["label"] == "52 PRB quota" and v["downlink"]["expires_in_s"] == 20.0
    assert [(h["link"], h["status"], h["value"]) for h in v["history"]] == [
        ("downlink", "APPLIED", "DL 52 PRB"), ("uplink", "CONFIRMED", "UL MCS 10 · 66 RB")]


def test_rejections_and_pending_statuses() -> None:
    lines = [
        indication("un3gpp", [ul_outcome(30, "SENT", 13, 99, NOW - 1_000_000_000), ul_ue(False)], seq=1),
        indication("un3gpp", [ul_outcome(30, "REJECTED_STALE", 13, 99, NOW - 1_000_000_000), ul_ue(False)], seq=2),
        indication("oai", [dl_outcome(31, "REJECTED_RANGE", 24, NOW - 900_000_000), dl_ue(106, 0)]),
    ]
    v = monitor(lines).view(SD_DRONE)
    assert v["uplink"]["last_action"]["status"] == "REJECTED_STALE" and v["uplink"]["last_action"]["ok"] is False
    assert v["downlink"]["last_action"]["ok"] is False and v["controller"] == "ran-default"
    statuses = {h["action_id"]: h["status"] for h in v["history"]}
    assert statuses == {30: "REJECTED_STALE", 31: "REJECTED_RANGE"}  # the latest status of an action wins
    pending = monitor([indication("un3gpp", [ul_outcome(32, "SENT", 9, 66, NOW), ul_ue(False)])]).view(SD_DRONE)
    assert pending["uplink"]["last_action"]["ok"] is None


def test_downlink_is_per_slice_and_history_survives_between_reads() -> None:
    lines = [indication("oai", [dl_outcome(40, "APPLIED", 78, NOW - 5_000_000_000, SD_CROP), dl_rule(78, 40, SD_CROP),
                                dl_ue(78, 40, SD_CROP)])]
    m = monitor(lines)
    crop, drone = m.view(SD_CROP), m.view(SD_DRONE)
    assert crop["downlink"]["label"] == "78 PRB quota" and crop["history"][0]["value"] == "DL 78 PRB"
    assert drone["downlink"]["prb_quota"] is None and drone["downlink"]["phone_in_slice"] is False
    assert drone["downlink"]["label"] == "No quota rule" and drone["history"] == []
    lines[:] = [indication("oai", [dl_outcome(41, "APPLIED", 52, NOW - 1_000_000_000, SD_CROP), dl_ue(52, 41, SD_CROP)])]
    assert [h["action_id"] for h in m.view(SD_CROP)["history"]] == [41, 40]


def test_missing_journal_stale_nodes_and_garbage_never_raise(tmp_path: Path) -> None:
    v = OranMonitor(JournalTail(tmp_path / "absent.jsonl")).view(SD_DRONE)
    assert v["available"] is False and "not readable" in v["detail"]
    old = NOW - 300_000_000_000
    gone = monitor([indication("oai", [dl_ue(106, 0)], received_at_ns=old)]).view(SD_DRONE)
    assert gone["available"] is False and "no recent" in gone["detail"]
    stale = monitor([indication("un3gpp", [ul_ue(False)], received_at_ns=NOW - 30_000_000_000)]).view(SD_DRONE)
    assert stale["uplink"]["stale"] is True and stale["uplink"]["age_s"] == 30.0
    junk = ["", "{not json", json.dumps([1]), json.dumps({"type": "control", "accepted": True}),
            indication("oai", ["sf=0.1;kind=bogus;x=1", "garbage"]), indication("un3gpp", [ul_ue(False)])]
    assert monitor(junk).view(SD_DRONE)["uplink"]["mode"] == "link-adaptation"

    def broken() -> list[str]:
        raise RuntimeError("boom")

    assert OranMonitor(broken).view(SD_DRONE)["available"] is False


def test_tail_reads_only_the_end_and_drops_the_partial_first_line(tmp_path: Path) -> None:
    path = tmp_path / "sf-control.jsonl"
    path.write_text("".join(f'{{"n": {i}}}\n' for i in range(1000)) + '{"partial":')
    lines = tail_lines(path, 100)
    assert lines[-1] == b'{"partial":' and all(line.startswith(b"{") for line in lines)
    assert json.loads(lines[-2]) == {"n": 999} and len(lines) < 12


def test_reads_are_cached() -> None:
    calls = []

    def source() -> list[str]:
        calls.append(1)
        return [indication("un3gpp", [ul_ue(False)])]

    m = OranMonitor(source, clock_ns=lambda: NOW, cache_s=1.0)
    m.view(SD_DRONE)
    m.view(SD_CROP)
    assert len(calls) == 1


def test_default_journal_follows_the_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SF_CONTROL_LOG_DIR", raising=False)
    assert default_journal(Path("/p")) == Path("/p/experiments/gates/20261102_192902-un3gpp-e2-proxy/sf-control.jsonl")
    monkeypatch.setenv("SF_CONTROL_LOG_DIR", "/var/log/sf")
    assert default_journal(Path("/p")) == Path("/var/log/sf/sf-control.jsonl")


def test_dry_run_fake_journal_reads_back_the_fake_radio() -> None:
    lab = DryRunLab()
    lab.sd = SD_CROP
    clock = [NOW]
    radio = FakeRadio(clock_ns=lambda: clock[0])
    m = OranMonitor(FakeOranJournal(lab, radio), clock_ns=lambda: clock[0], cache_s=0.0)
    rest = m.view(SD_CROP)
    assert rest["controller"] == "ran-default" and rest["uplink"]["mode"] == "link-adaptation"
    assert rest["downlink"]["label"] == "No quota · 106 PRB"
    radio.ul, radio.quota, radio.quota_expires_ns = (9, 66), 52, NOW + 30_000_000_000
    radio.record(FakeOutcome("un3gpp", 5, "CONFIRMED", 9, 66, at_ns=NOW - 1_000_000_000))
    radio.record(FakeOutcome("oai", 6, "APPLIED", prb=52, at_ns=NOW - 900_000_000))
    v = m.view(SD_CROP)
    assert v["controller"] == "e2-action" and v["uplink"]["label"] == "MCS 9 · 66 RB"
    assert v["downlink"]["label"] == "52 PRB quota" and v["uplink"]["last_action"]["status"] == "CONFIRMED"
    clock[0] = NOW + 31_000_000_000  # the quota expires without renewal
    assert m.view(SD_CROP)["downlink"]["label"] == "No quota · 106 PRB"


# ------------------------------------------------------------------------------------------------------ API

_created: list[DemoOrchestrator] = []


@pytest_asyncio.fixture(autouse=True)
async def _shutdown() -> Any:
    yield
    while _created:
        await _created.pop().shutdown()


def api_for(tmp_path: Path, oran: Any) -> tuple[DemoApi, DryRunLab]:
    lab = DryRunLab(switch_delay_s=0.01)
    config = DemoConfig(segment_s=3600, renew_margin_s=5.0, renew_pause_s=0.0, lease_s=0.0, phone_check_s=0.05,
                        phone_timeout_s=0.3, hold_s=0.0, settle_s=0.0, flow_timeout_s=5.0, agent_timeout_s=1.0,
                        require_phone_client=False)
    orch = DemoOrchestrator(lab, lab, JsonlLog(tmp_path, echo=None), config)  # type: ignore[arg-type]
    _created.append(orch)
    return DemoApi(orch, JsonlLog(tmp_path, echo=None), dry_run=True, oran=oran), lab


async def streaming(api: DemoApi) -> str:
    body = json.dumps({"service": "drone", "phone_ip": "10.45.160.6", "app_session": "s1"}).encode()
    status, created = await api.dispatch("POST", "/api/v1/service", body, "127.0.0.1")
    assert status == 202
    for _ in range(300):
        _, view = await api.dispatch("GET", f"/api/v1/session/{created['session']}", b"", "127.0.0.1")
        if view["state"] == "streaming":
            return str(created["session"])
        await asyncio.sleep(0.01)
    raise AssertionError("not streaming")


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    def view(self, sd: str) -> dict[str, Any]:
        self.calls.append(sd)
        if self.fail:
            raise RuntimeError("journal exploded")
        return {"available": True, "sd": sd}


@pytest.mark.asyncio
async def test_active_session_view_carries_oran_and_nothing_else_changes(tmp_path: Path) -> None:
    rec = Recorder()
    api, _ = api_for(tmp_path, rec)
    plain, _ = api_for(tmp_path, None)
    sid = await streaming(api)
    sid_plain = await streaming(plain)
    _, view = await api.dispatch("GET", f"/api/v1/session/{sid}", b"", "127.0.0.1")
    _, view_plain = await plain.dispatch("GET", f"/api/v1/session/{sid_plain}", b"", "127.0.0.1")
    assert view["oran"] == {"available": True, "sd": "000003"} and rec.calls[-1] == "000003"
    assert "oran" not in view_plain and set(view) - set(view_plain) == {"oran"}
    _, health = await api.dispatch("GET", "/api/v1/health", b"", "127.0.0.1")
    assert "oran" not in health["active_session"]
    _, stopped = await api.dispatch("DELETE", f"/api/v1/session/{sid}", b"", "127.0.0.1")
    assert "oran" not in stopped
    _, after = await api.dispatch("GET", f"/api/v1/session/{sid}", b"", "127.0.0.1")
    assert after["state"] == "stopped" and "oran" not in after  # the app hides the card when the session ends


@pytest.mark.asyncio
async def test_a_failing_oran_source_leaves_the_view_untouched(tmp_path: Path) -> None:
    api, _ = api_for(tmp_path, Recorder(fail=True))
    sid = await streaming(api)
    status, view = await api.dispatch("GET", f"/api/v1/session/{sid}", b"", "127.0.0.1")
    assert status == 200 and view["state"] == "streaming" and "oran" not in view


@pytest.mark.asyncio
async def test_oran_endpoint(tmp_path: Path) -> None:
    api, _ = api_for(tmp_path, Recorder())
    assert (await api.dispatch("GET", "/api/v1/oran", b"", "127.0.0.1"))[0] == 400  # no active session
    status, view = await api.dispatch("GET", "/api/v1/oran?service=crop", b"", "127.0.0.1")
    assert status == 200 and view["service"] == "crop" and view["sd"] == "000002"
    assert (await api.dispatch("GET", "/api/v1/oran?service=nope", b"", "127.0.0.1"))[0] == 400
    assert (await api.dispatch("POST", "/api/v1/oran", b"", "127.0.0.1"))[0] == 405
    assert (await api.dispatch("GET", "/api/v1/oran", b"", "192.168.56.103"))[0] == 403
    await streaming(api)
    status, view = await api.dispatch("GET", "/api/v1/oran", b"", "127.0.0.1")
    assert status == 200 and view["service"] == "drone"
    disabled, _ = api_for(tmp_path, None)
    assert (await disabled.dispatch("GET", "/api/v1/oran", b"", "127.0.0.1"))[0] == 404
