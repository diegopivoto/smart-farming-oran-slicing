"""Selectable O-RAN radio control of the demo: modes, the frozen DDQN policy and its controller, the supervisor."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import pytest_asyncio

from smartfarm.contracts.evaluation import evaluate
from smartfarm.contracts.service import ServiceId
from smartfarm.orchestrator.demo import DemoApi, DemoConfig, DemoOrchestrator, JsonlLog
from smartfarm.orchestrator.demo_dryrun import DryRunLab, DryRunRadioPort, FakeRadio
from smartfarm.orchestrator.oran_agent import (
    DDQN,
    RAN_DEFAULT,
    AgentConfig,
    AgentSession,
    AgentStatus,
    OranAgentSupervisor,
    parse_mode,
    step_records,
)
from smartfarm.rl.dataset import LA, Action, Cell, Dataset
from smartfarm.rl.policy import FINAL_MODEL, FINAL_RUNS, FrozenDdqnPolicy, FrozenModel, PolicyLoadError

PROJECT = Path(__file__).resolve().parents[2]
W = ServiceId.WEATHER
A0, CHEAP, MID, DEAR = Action(LA, LA, 24), Action(2, 66, 24), Action(5, 66, 24), Action(10, 66, 52)


class StubAgent:
    """Q-values fixed by preference (the policy only calls q)."""

    def __init__(self, prefs: list[float]) -> None:
        self.prefs = np.asarray(prefs)

    def q(self, obs: np.ndarray, mask: np.ndarray) -> np.ndarray:
        return np.where(mask, self.prefs, -1e9)


def toy_model(prefs: tuple[float, ...] = (0.0, 3.0, 2.0, 1.0)) -> FrozenModel:
    actions = [A0, CHEAP, MID, DEAR]
    cells = [Cell(W, a, 1, True, (0.5, 0.5, 0.5), {}, {}) for a in actions]
    data = Dataset(cells, actions, {W: [0, 1, 2, 3], ServiceId.CROP: [], ServiceId.DRONE: []}, {W: [1]})
    return FrozenModel(data, StubAgent(list(prefs)), Path("toy.npz"))  # type: ignore[arg-type]


def row(met: bool | None, a: Action = CHEAP) -> dict[str, Any]:
    return {"ul_mode": "link-adaptation" if a.link_adaptation else "override", "ul_mcs": a.ul_mcs, "ul_rb": a.ul_rb,
            "dl_prb": a.dl_prb, "sla_met": met, "svc_pdr": 0.999 if met else 0.9, "svc_aoi_p95": 0.5,
            "svc_max_freshness_gap": 1.0, "kpm_ss_sinr_db": 23.0}


# ------------------------------------------------------------------------------------------------------ modes


def test_mode_parsing_and_default() -> None:
    assert parse_mode(None) is None and parse_mode(None, RAN_DEFAULT) == RAN_DEFAULT
    assert parse_mode("ddqn") == DDQN and parse_mode("ran-default", DDQN) == RAN_DEFAULT
    for bad in ("DDQN", "", 1, ["ddqn"], "ddqn-adaptive"):
        with pytest.raises(ValueError):
            parse_mode(bad)
    assert OranAgentSupervisor(object()).default_mode == RAN_DEFAULT  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------------ policy


def test_policy_starts_at_the_ran_defaults_then_goes_greedy() -> None:
    p = FrozenDdqnPolicy(toy_model(), W)
    d0 = p.choose()
    assert (d0.policy, p.action(d0.action)) == ("initial-state", A0)
    p.record(d0, row(True, A0))
    d1 = p.choose()
    assert (d1.policy, p.action(d1.action)) == ("ddqn", CHEAP) and d1.q_top5[0][0] == CHEAP.label()


def test_guard_after_three_consecutive_failures_then_back_to_the_agent() -> None:
    p = FrozenDdqnPolicy(toy_model(), W, elimination=False)
    p.record(p.choose(), row(True, A0))
    for _ in range(3):
        d = p.choose()
        assert d.policy == "ddqn"
        p.record(d, row(False))
    g = p.choose()
    assert (g.policy, p.action(g.action)) == ("guard", A0)
    p.record(g, row(False, A0))  # the guard step resets the count whatever it measures
    assert p.failures == 0 and p.choose().policy == "ddqn"


def test_majority_elimination_blocks_an_action_once_misses_outnumber_successes() -> None:
    p = FrozenDdqnPolicy(toy_model(), W)
    p.record(p.choose(), row(True, A0))
    d = p.choose()
    p.record(d, row(True))  # CHEAP: 1 met
    p.record(p.choose(), row(False))  # CHEAP: 1 met, 1 missed
    assert p.action(p.choose().action) == CHEAP  # 1 met / 1 missed: not yet
    out = p.record(p.choose(), row(False))  # 1 met / 2 missed
    assert out["eliminated"] == CHEAP.label() and p.action(p.choose().action) == MID


def test_undetermined_steps_are_neither_success_nor_failure() -> None:
    p = FrozenDdqnPolicy(toy_model(), W)
    p.record(p.choose(), row(True, A0))
    for _ in range(5):
        out = p.record(p.choose(), row(None))
        assert out["undetermined"] is True
    assert p.failures == 0 and not p.blocked and p.tally == {} and p.choose().policy == "ddqn"
    assert p.obs is not None  # the state still moved


def test_skipped_step_counts_as_an_agent_failure_without_elimination() -> None:
    p = FrozenDdqnPolicy(toy_model(), W)
    p.record(p.choose(), row(True, A0))
    p.record(p.choose(), None)
    assert p.failures == 1 and not p.blocked


def test_one_action_always_stays_available() -> None:
    p = FrozenDdqnPolicy(toy_model(), W)
    p.record(p.choose(), row(True, A0))
    for _ in range(40):
        p.record(p.choose(), row(False, DEAR))
    assert len(p.blocked) == 3 and p.choose().policy in ("ddqn", "guard")


def final_model_or_skip() -> FrozenModel:
    if not (PROJECT / FINAL_MODEL).exists() or not all((PROJECT / r).exists() for r in FINAL_RUNS):
        pytest.skip("final model and calibration runs are not in this checkout")
    return FrozenModel.load(PROJECT / FINAL_MODEL, [PROJECT / r for r in FINAL_RUNS])


def test_final_model_loads_with_its_action_index_and_matches_rl_online(tmp_path: Path) -> None:
    model = final_model_or_skip()
    assert len(model.data.actions) == 146
    sys.path.insert(0, str(PROJECT / "deployment/scripts"))
    rl_online = pytest.importorskip("rl_online")
    for service in ServiceId:
        args = argparse.Namespace(service=service.value, margin_weight=0.0, output=tmp_path, no_elimination=True,
                                  guard_failures=3, policy="ddqn", epsilon=0.0)
        reference = rl_online.OnlineAgent(args, model.data, model.agent)
        mine = FrozenDdqnPolicy(model, service, elimination=False)
        assert mine.default == reference.default and (mine.mask == reference.mask).all()
        for c in [c for c in model.data.cells if c.service is service][:12]:
            mine.record(mine.choose(), {**c.raw})
            a, policy, top = reference.choose(mine.obs, 1, mine.failures)
            d = mine.choose()
            assert (d.action, d.policy, d.q_top5) == (a, policy, top)


def test_mismatched_action_index_is_refused(tmp_path: Path) -> None:
    model = final_model_or_skip()
    with pytest.raises(PolicyLoadError):
        FrozenModel.load(model.model, [PROJECT / FINAL_RUNS[0]])
    with pytest.raises(PolicyLoadError):
        FrozenModel.load(tmp_path / "missing.npz", [PROJECT / r for r in FINAL_RUNS])


# ------------------------------------------------------------------------------------------------------ records


def window(at_s: float, received: int = 10, expected: int = 10, aoi: float = 0.5, gap: float = 0.2) -> dict[str, Any]:
    return {"kind": "receiver_window", "received": received, "expected_in_window": expected, "goodput_bps": 100000,
            "at_ns": int(at_s * 1e9), "collected_at_ns": int(at_s * 1e9), "aoi_p95_s": aoi, "max_receive_gap_s": gap,
            "jitter_s": 0.001, "pdr_total": 1.0}


def test_step_records_keep_the_step_window_and_evaluate_like_the_calibration() -> None:
    records = [window(t) for t in range(0, 60)]
    step = step_records(records, int(10e9), int(40e9), W)
    assert len(step) == 31 and all(10e9 <= r["collected_at_ns"] <= 40e9 for r in step)
    assert evaluate(W, step)["sla_met"] is True
    lossy = step_records([window(t, received=9) for t in range(0, 60)], int(10e9), int(40e9), W)
    assert evaluate(W, lossy)["sla_met"] is False
    assert evaluate(W, step_records(records, int(100e9), int(130e9), W))["sla_met"] is None  # no report: undetermined


def obj(at_s: float, ratio: float | None, size: int = 1_000_000, valid: bool = True) -> dict[str, Any]:
    return {"kind": "receiver_object", "collected_at_ns": int(at_s * 1e9), "size_bytes": size, "sha256_valid": valid,
            "goodput_bps": 20e6, "object_transfer_time_s": 5.0, "receive_time_s": 5.0,
            "tcp_retrans_ratio_so_far": ratio}


def test_crop_retransmissions_of_the_step_from_the_cumulative_ratio() -> None:
    # objects of 1 MB; cumulative ratio 0 for the first 3 MB, then 1 % of the bytes after: 2 MB sent in the step
    objects = [obj(0, None), obj(5, 0.0), obj(10, 0.0), obj(15, 0.0), obj(20, 0.0025), obj(25, 0.004)]
    step = step_records(objects, int(12e9), int(26e9), ServiceId.CROP)
    summary = next(r for r in step if r["kind"] == "generator_summary")
    ratio = summary["tcp_bytes_retrans"] / summary["tcp_bytes_sent"]
    assert 0.005 < ratio < 0.015
    measured = evaluate(ServiceId.CROP, step)["measured"]
    assert measured["retransmitted_bytes_ratio"] == pytest.approx(ratio) and measured["objects_received"] == 3
    lonely = step_records([obj(20, 0.01)], int(12e9), int(26e9), ServiceId.CROP)
    assert evaluate(ServiceId.CROP, lonely)["measured"]["retransmitted_bytes_ratio"] is None
    empty = evaluate(ServiceId.CROP, step_records(objects, int(100e9), int(130e9), ServiceId.CROP))
    assert empty["measured"]["goodput"] == 0.0 and empty["sla_met"] is False  # stream on, nothing completed


# ------------------------------------------------------------------------------------------------------ controller


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s
        await asyncio.sleep(0)


class FakePort:
    def __init__(self, verdicts: list[bool | None], busy: str | None = None, fail_at: int | None = None) -> None:
        self.verdicts, self.busy, self.fail_at = list(verdicts), busy, fail_at
        self.calls: list[tuple[str, Any]] = []
        self.applied: list[Action] = []
        self.closed = False

    async def open(self, service: ServiceId) -> str | None:
        self.calls.append(("open", service))
        return self.busy

    async def apply(self, action: Action) -> dict[str, Any]:
        if self.fail_at is not None and len(self.applied) == self.fail_at:
            self.applied.append(action)
            raise OSError("xApp closed the control socket")
        self.applied.append(action)
        self.calls.append(("apply", action))
        return {"ul_status": "CONFIRMED", "dl_status": "APPLIED"}

    async def renew_quota(self, prb: int) -> str:
        self.calls.append(("quota", prb))
        return "APPLIED"

    def ul_sample(self) -> dict[str, float] | None:
        return {"snr_db": 22.0, "mcs_observed": 8, "rb_observed": 40, "la_mcs": 8, "mac_mbps": 0.1}

    async def measure(self, run_ids: list[str], t0: float, t1: float) -> dict[str, Any]:
        self.calls.append(("measure", (tuple(run_ids), round(t1 - t0))))
        if not self.verdicts:
            await asyncio.Event().wait()  # park until cancelled
        met = self.verdicts.pop(0)
        return {"sla_met": met, "svc_pdr": 0.999 if met else 0.5, "svc_aoi_p95": 0.4, "svc_max_freshness_gap": 1.0,
                "traffic_records": 30}

    async def after_error(self, error: BaseException) -> bool:
        self.calls.append(("after_error", repr(error)))
        return False

    async def safe_defaults(self, reason: str) -> dict[str, Any]:
        self.calls.append(("safe", reason))
        return {"reason": reason}

    async def close(self) -> None:
        self.closed = True


def session(port: FakePort, tmp_path: Path, model: Any = None) -> tuple[AgentSession, AgentStatus, FakeClock]:
    clock = FakeClock()
    st = AgentStatus("s1", "weather", DDQN, effective="starting")
    a = AgentSession(st, W, port, model or (lambda: toy_model()), lambda: "ab" * 16, tmp_path / "oran-s1",
                     AgentConfig(), clock=clock, sleep=clock.sleep)
    return a, st, clock


@pytest.mark.asyncio
async def test_controller_steps_renews_the_quota_and_restores_the_safe_configuration(tmp_path: Path) -> None:
    port = FakePort([True, True, False, None])
    agent, st, _ = session(port, tmp_path)
    task = asyncio.create_task(agent.run())
    for _ in range(500):
        await asyncio.sleep(0)
        if not port.verdicts and port.calls[-1][0] == "measure":
            break
    assert st.effective == DDQN and st.running
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert port.applied[0] == A0 and port.applied[1] == CHEAP  # step 0 = RAN defaults, then the agent
    assert port.calls[-1] == ("safe", "end of demo session") and port.closed and not st.running
    assert st.effective == RAN_DEFAULT
    measures = [c[1] for c in port.calls if c[0] == "measure"]
    assert measures[0] == (("ab" * 16,), 30)
    quotas = [c for c in port.calls if c[0] == "quota"]
    assert len(quotas) >= 3 * 3  # every 10 s of the 36 s step
    steps = [json.loads(x) for x in (tmp_path / "oran-s1/steps.jsonl").read_text().splitlines()]
    assert [s["verdict"] for s in steps] == ["met", "met", "not met", "undetermined"]
    assert steps[0]["policy"] == "initial-state" and steps[1]["policy"] == "ddqn"
    assert steps[1]["cell"]["un3gpp_samples"] == 30 and steps[3]["undetermined"] is True
    assert json.loads((tmp_path / "oran-s1/run-metadata.json").read_text())["learn"] is False
    assert [s["verdict"] for s in st.steps] == ["undetermined", "not met", "met", "met"]


@pytest.mark.asyncio
async def test_busy_xapp_falls_back_to_the_ran_defaults_without_any_action(tmp_path: Path) -> None:
    port = FakePort([], busy="xApp busy: another control client is connected")
    agent, st, _ = session(port, tmp_path)
    await agent.run()
    assert st.effective == RAN_DEFAULT and "busy" in st.detail and not st.running
    assert port.applied == [] and not any(c[0] == "safe" for c in port.calls) and port.closed


@pytest.mark.asyncio
async def test_model_load_error_falls_back_before_touching_the_xapp(tmp_path: Path) -> None:
    def broken() -> FrozenModel:
        raise PolicyLoadError("cannot load the DDQN model: missing")

    port = FakePort([])
    agent, st, _ = session(port, tmp_path, broken)
    await agent.run()
    assert st.effective == RAN_DEFAULT and "missing" in st.detail and port.calls == []


@pytest.mark.asyncio
async def test_lost_e2_control_ends_the_agent_with_the_safe_configuration(tmp_path: Path) -> None:
    port = FakePort([True], fail_at=1)
    agent, st, _ = session(port, tmp_path)
    await agent.run()
    assert st.effective == RAN_DEFAULT and "E2 control lost" in st.detail
    assert port.calls[-1] == ("safe", "end of demo session")
    steps = [json.loads(x) for x in (tmp_path / "oran-s1/steps.jsonl").read_text().splitlines()]
    assert steps[-1]["verdict"] == "error"


# ------------------------------------------------------------------------------------------------------ supervisor

_created: list[DemoOrchestrator] = []


@pytest_asyncio.fixture(autouse=True)
async def _shutdown() -> Any:
    yield
    while _created:
        await _created.pop().shutdown()


def make_api(tmp_path: Path, default: str, factory: Any, model: Any = None) -> tuple[DemoApi, OranAgentSupervisor]:
    lab = DryRunLab(switch_delay_s=0.01)
    config = DemoConfig(segment_s=3600, renew_margin_s=5.0, renew_pause_s=0.0, lease_s=0.0, phone_check_s=0.05,
                        phone_timeout_s=0.3, hold_s=0.0, settle_s=0.0, flow_timeout_s=5.0, agent_timeout_s=1.0,
                        require_phone_client=False)
    orch = DemoOrchestrator(lab, lab, JsonlLog(tmp_path, echo=None), config)  # type: ignore[arg-type]
    _created.append(orch)
    sup = OranAgentSupervisor(orch, default_mode=default, port_factory=factory, model=model or (lambda: toy_model()),
                              log_root=tmp_path, config=AgentConfig(settle_s=0.01, cell_s=0.05, flush_s=0.01,
                                                                     poll_s=0.01))

    class Monitor:
        def view(self, sd: str) -> dict[str, Any]:
            return {"available": True, "controller": "ran-default", "decided_by": "RAN defaults", "history": []}

    return DemoApi(orch, JsonlLog(tmp_path, echo=None), dry_run=True, oran=Monitor(), agent=sup), sup


async def post(api: DemoApi, extra: dict[str, Any], service: str = "weather",
               ip: str = "10.45.192.4", app: str = "s1") -> tuple[int, dict[str, Any]]:
    body = json.dumps({"service": service, "phone_ip": ip, "app_session": app, **extra}).encode()
    return await api.dispatch("POST", "/api/v1/service", body, "127.0.0.1")


async def until(api: DemoApi, sid: str, cond: Any, sup: OranAgentSupervisor, n: int = 400) -> dict[str, Any]:
    view: dict[str, Any] = {}
    for _ in range(n):
        await sup.tick()
        _, view = await api.dispatch("GET", f"/api/v1/session/{sid}", b"", "127.0.0.1")
        if cond(view):
            return view
        await asyncio.sleep(0.01)
    raise AssertionError(view.get("oran"))


def never(service: str, out: Path) -> Any:
    raise AssertionError("the xApp port must not be created in ran-default")


@pytest.mark.asyncio
async def test_ran_default_never_touches_the_xapp_and_old_apps_keep_working(tmp_path: Path) -> None:
    api, sup = make_api(tmp_path, RAN_DEFAULT, never)
    status, created = await post(api, {})  # 1.2.1 / 1.3.0 request: no oran_mode
    assert status == 202
    view = await until(api, created["session"], lambda v: v["state"] == "streaming", sup)
    for _ in range(5):
        await sup.tick()
    assert view["oran"]["mode"] == {"requested": "ran-default", "effective": "ran-default", "detail": "selected"}
    assert view["oran"]["agent_running"] is False and view["oran"]["decided_by"] == "RAN defaults"
    assert sup.ports_opened == 0
    status, again = await post(api, {"oran_mode": "ran-default"}, app="s2")
    assert status == 202
    await until(api, again["session"], lambda v: v["state"] == "streaming", sup)
    assert sup.ports_opened == 0


@pytest.mark.asyncio
async def test_invalid_oran_mode_is_refused_before_anything_starts(tmp_path: Path) -> None:
    api, _ = make_api(tmp_path, RAN_DEFAULT, never)
    for bad in ("DDQN", 3, "adaptive"):
        status, payload = await post(api, {"oran_mode": bad})
        assert status == 400 and "oran_mode" in payload["error"]
    assert api.orchestrator.active is None
    assert (await post(api, {"oran_mode": None}))[0] == 202  # null = the orchestrator's default


@pytest.mark.asyncio
async def test_per_session_ddqn_runs_the_agent_and_stops_with_the_safe_configuration(tmp_path: Path) -> None:
    radios: list[DryRunRadioPort] = []

    def factory(service: str, out: Path) -> DryRunRadioPort:
        radios.append(DryRunRadioPort(FakeRadio(), toy_model(), ServiceId(service)))
        return radios[-1]

    api, sup = make_api(tmp_path, RAN_DEFAULT, factory)
    _, created = await post(api, {"oran_mode": "ddqn"})
    sid = created["session"]
    view = await until(api, sid, lambda v: ((v.get("oran", {}).get("agent") or {}).get("step") or 0) >= 2, sup)
    oran = view["oran"]
    assert oran["mode"]["requested"] == "ddqn" and oran["mode"]["effective"] == "ddqn" and oran["agent_running"]
    assert oran["decided_by"].startswith(("DDQN agent", "RAN defaults (DDQN"))
    assert oran["history"][0]["value"].startswith("#") and oran["history"][-1]["value"].startswith("#0 ")
    await api.dispatch("DELETE", f"/api/v1/session/{sid}", b"", "127.0.0.1")
    for _ in range(50):
        await sup.tick()
        if radios[0].closed:
            break
        await asyncio.sleep(0.01)
    assert radios[0].safe == ["end of demo session"] and radios[0].closed and radios[0].radio.ul is None
    # the next session without oran_mode follows the orchestrator default (ran-default): no new port
    _, other = await post(api, {}, app="s2")
    await until(api, other["session"], lambda v: v["state"] == "streaming", sup)
    for _ in range(5):
        await sup.tick()
    assert len(radios) == 1


@pytest.mark.asyncio
async def test_default_ddqn_with_a_busy_xapp_keeps_the_ran_defaults(tmp_path: Path) -> None:
    def factory(service: str, out: Path) -> DryRunRadioPort:
        return DryRunRadioPort(FakeRadio(), toy_model(), ServiceId(service), busy="xApp busy: rl_online running")

    api, sup = make_api(tmp_path, DDQN, factory)
    _, created = await post(api, {})
    view = await until(api, created["session"], lambda v: v["oran"]["mode"]["effective"] == "ran-default", sup)
    assert view["oran"]["mode"]["requested"] == "ddqn" and "busy" in view["oran"]["mode"]["detail"]
    assert view["state"] == "streaming" and view["oran"]["decided_by"] == "RAN defaults"


# ------------------------------------------------------------------------------------------------------ lab port


def lab_module() -> Any:
    sys.path.insert(0, str(PROJECT / "deployment/scripts"))
    return pytest.importorskip("demo_oran_lab")


def test_xapp_client_state_from_its_log(tmp_path: Path) -> None:
    lab = lab_module()
    log = tmp_path / "sf-control.stderr"
    assert lab.xapp_client_connected(log) is False  # no log
    log.write_text("=== sf_control start x ===\nsmartfarm_sf_control: client connected\n")
    assert lab.xapp_client_connected(log) is True
    log.write_text(log.read_text() + "smartfarm_sf_control: client disconnected\n")
    assert lab.xapp_client_connected(log) is False
    log.write_text(log.read_text() + "smartfarm_sf_control: client connected\n=== sf_control start y ===\n")
    assert lab.xapp_client_connected(log) is False  # restarted since


@pytest.mark.asyncio
async def test_lab_port_does_not_fight_for_a_busy_or_absent_xapp(tmp_path: Path) -> None:
    lab = lab_module()
    (tmp_path / "sf-control.stderr").write_text("smartfarm_sf_control: client connected\n")
    sock = tmp_path / "absent.sock"
    port = lab.LabRadioPort("crop", tmp_path / "out", log_dir=tmp_path, socket=sock)
    assert "busy" in await port.open(ServiceId.CROP)
    (tmp_path / "sf-control.stderr").write_text("smartfarm_sf_control: client disconnected\n")
    assert "not running" in await port.open(ServiceId.CROP)
    assert port.sw is None and not (tmp_path / "out").exists()


@pytest.mark.asyncio
async def test_lab_port_measures_a_step_from_the_collected_app_records(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    lab = lab_module()
    monkeypatch.setattr(lab, "kpm_window", lambda t0, t1, sd: {"kpm_ss_sinr_db": 23.0, "kpm_sd": sd})
    run = "cd" * 16
    (tmp_path / f"{run}.jsonl").write_text("".join(json.dumps(window(t)) + "\n" for t in range(0, 60)) + "{broken\n")
    port = lab.LabRadioPort("weather", tmp_path / "out", log_dir=tmp_path, socket=tmp_path / "x.sock",
                            collected=tmp_path)
    m = await port.measure([run, "ef" * 16], 10.0, 40.0)
    assert m["sla_met"] is True and m["svc_pdr"] == 1.0 and m["traffic_records"] == 31
    assert m["kpm_sd"] == 1 and m["kpm_ss_sinr_db"] == 23.0


def test_traffic_run_still_exports_the_moved_evaluate() -> None:
    sys.path.insert(0, str(PROJECT / "deployment/scripts"))
    traffic_run = pytest.importorskip("traffic_run")
    assert traffic_run.evaluate is evaluate
