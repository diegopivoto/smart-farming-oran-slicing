"""Selectable O-RAN radio control of the demo: the RAN defaults, or the frozen DDQN agent of the paper.

Modes (per demo session: the app's optional `oran_mode`, else the orchestrator's `--oran-mode`, default
`ran-default`):
  - `ran-default`: no O-RAN action at all; the radios stay at the BS link adaptation on the full band and no DL quota.
    The xApp control socket is never opened.
  - `ddqn`: while the session streams, one controller task runs the frozen agent (smartfarm.rl.policy, the same model,
    mask and decision rule as `rl_online.py --policy ddqn --elimination-rule majority`, no learning) in 30 s steps
    over the content stream, which keeps flowing (it is never restarted per step). Step 0 applies the RAN defaults a0
    (BS link adaptation on the full 132-RB band, smallest DL quota of the service) to get the first state. A step:
    joint action through the sf-control xApp (UL MCS/RBs or release to the link adaptation, waiting for the BS
    proxy's final verdict; DL PRB quota of the slice with a 30 s TTL, renewed every 10 s), settle, observe, then the
    state from the same functions as calibration/rl_online: SLA metrics with `evaluate` from the per-second windows
    and objects the app reports to the lab collector for the session's generator run, UN3GPP read-back sampled every
    second, KPM of the slice from the bridge. A verdict that cannot be computed is undetermined: logged, never a
    failure for the guard or the elimination. On session end, error or shutdown: the safe configuration (UL release
    to the link adaptation on the full band + 106-PRB quota), as rl_online.

The xApp serves one control client at a time: when it is busy (an experiment or rl_online holds it) or down, the
controller does not fight for it; the session goes on under the RAN defaults and the `oran` view says why. The
controller only watches the orchestrator (polling `active`); it never changes a session, and its failures stay here.
Every step is one JSON line in `<log root>/<YYYYMMDD>/oran-<session>/steps.jsonl` (plus run-metadata.json; the lab
port writes the E2 events next to it). This module does not import the E2 client: the lab port
(deployment/scripts/demo_oran_lab.py) is loaded only when a DDQN session starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import statistics
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from smartfarm.contracts.service import ServiceId
from smartfarm.rl.dataset import Action
from smartfarm.rl.policy import Decision, FrozenDdqnPolicy, FrozenModel, PolicyLoadError

RAN_DEFAULT = "ran-default"
DDQN = "ddqn"
MODES = (RAN_DEFAULT, DDQN)
STEP_SCHEMA = "smartfarm.demo_oran_step.v1"
VERDICT = {True: "met", False: "not met", None: "undetermined"}


def parse_mode(value: Any, default: str | None = None) -> str | None:
    """A valid mode, `default` when absent; ValueError for anything else."""

    if value is None:
        return default
    if isinstance(value, str) and value in MODES:
        return value
    raise ValueError(f"oran_mode must be one of {', '.join(MODES)}")


class RadioPort(Protocol):
    """The radio as the controller drives it (lab: xApp + collector + KPM; dry run: in-memory)."""

    async def open(self, service: ServiceId) -> str | None:
        """Take the xApp control; None when ready, otherwise why not (busy, down). Nothing is applied."""
        ...

    async def apply(self, action: Action) -> dict[str, Any]:
        """Apply the joint action: {"ul_status", "dl_status"} ("dl_status" absent when the UL was rejected)."""
        ...

    async def renew_quota(self, prb: int) -> str: ...

    def ul_sample(self) -> dict[str, float] | None:
        """Latest UN3GPP read-back of the UE: snr_db, mcs_observed, rb_observed, la_mcs, mac_mbps."""
        ...

    async def measure(self, run_ids: list[str], t0: float, t1: float) -> dict[str, Any]:
        """The service metrics of [t0, t1] (svc_*, sla_met, traffic_records) plus the slice KPM (kpm_*)."""
        ...

    async def after_error(self, error: BaseException) -> bool:
        """Recover after a failed step (reconnect, safe configuration); False: the control is lost."""
        ...

    async def safe_defaults(self, reason: str) -> dict[str, Any]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class AgentConfig:
    settle_s: float = 3.0  # after applying the action, before the observation window (rl_online --settle-s)
    cell_s: float = 30.0  # observation window of a step (rl_online --cell-s)
    flush_s: float = 3.0  # the app reports a window about a second after it ends
    quota_renew_s: float = 10.0  # DL quota TTL is 30 s
    poll_s: float = 1.0  # how often the supervisor looks at the orchestrator's active session
    guard_failures: int = 3
    max_step_errors: int = 3  # consecutive failed steps before the agent hands back to the RAN defaults
    history: int = 6
    safe_timeout_s: float = 45.0


def _mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 4) if values else None


def ran_summary(samples: list[dict[str, float]]) -> dict[str, Any]:
    """The UN3GPP fields of a calibration cell, from the read-backs sampled every second."""

    return {
        "un3gpp_ul_snr_db": _mean([s["snr_db"] for s in samples]),
        "un3gpp_ul_mcs_observed": _mean([float(s["mcs_observed"]) for s in samples]),
        "un3gpp_ul_rb_observed_max": max((s["rb_observed"] for s in samples), default=None),
        "un3gpp_la_mcs": _mean([float(s["la_mcs"]) for s in samples]),
        "un3gpp_mac_mbps": _mean([s["mac_mbps"] for s in samples]),
        "un3gpp_samples": len(samples),
    }


def cell_spec(a: Action) -> dict[str, Any]:
    """Action fields of a cell record (rl_online.cell_spec)."""

    if a.link_adaptation:
        return {"ul_mode": "link-adaptation", "ul_mcs": None, "ul_rb": None, "dl_prb": a.dl_prb}
    return {"ul_mode": "override", "ul_mcs": a.ul_mcs, "ul_rb": a.ul_rb, "dl_prb": a.dl_prb}


def step_records(records: list[dict[str, Any]], t0_ns: int, t1_ns: int, service: ServiceId) -> list[dict[str, Any]]:
    """The app's records of one step, ready for `evaluate`: receiver windows and objects collected in [t0, t1] (PC1
    clock of the collector). Crop adds a generator summary for the step, since the content generator only sends its
    own at the end of a segment: the TCP retransmitted-bytes ratio of the step is estimated from the cumulative ratio
    each object carries (`tcp_retrans_ratio_so_far`, at the object's start) and the bytes of the objects before it
    (sent = payload / (1 - ratio)), between the last anchor before the step and the last one in it; with fewer than
    two anchors it stays unknown (undetermined). `sent_bytes` marks the stream as running, so a step without a
    completed object has zero object goodput (a failure), as in the calibration."""

    def collected(r: dict[str, Any]) -> int:
        return int(r.get("collected_at_ns") or 0)

    inside = [r for r in records if t0_ns <= collected(r) <= t1_ns
              and r.get("kind") in ("receiver_window", "receiver_object")]
    if service is not ServiceId.CROP:
        return inside
    objects = sorted((r for r in records if r.get("kind") == "receiver_object"), key=collected)
    payload, anchors = 0, []
    for o in objects:
        ratio = o.get("tcp_retrans_ratio_so_far")
        if isinstance(ratio, (int, float)) and 0 <= ratio < 1 and payload > 0:
            sent = payload / (1 - ratio)
            anchors.append((collected(o), sent, ratio * sent))
        payload += int(o.get("size_bytes") or 0)
    before = [x for x in anchors if x[0] < t0_ns]
    within = [x for x in anchors if t0_ns <= x[0] <= t1_ns]
    summary: dict[str, Any] = {"kind": "generator_summary", "sent_bytes": 1, "estimated_from": "app objects"}
    points = before[-1:] + within
    if len(points) >= 2 and points[-1][1] > points[0][1]:
        summary["tcp_bytes_sent"] = points[-1][1] - points[0][1]
        summary["tcp_bytes_retrans"] = max(0.0, points[-1][2] - points[0][2])
    return [*inside, summary]


@dataclass
class AgentStatus:
    session: str
    service: str
    requested: str
    effective: str = RAN_DEFAULT  # ran-default | starting | ddqn
    detail: str = ""
    running: bool = False
    step: int | None = None
    policy: str | None = None
    action: str | None = None
    last_verdict: str | None = None
    last_step_at: float | None = None
    blocked: int = 0
    steps: list[dict[str, Any]] = field(default_factory=list)
    safe: dict[str, Any] | None = None
    log_dir: str | None = None


class AgentSession:
    """The frozen agent for one demo session; `run()` is the controller task."""

    def __init__(self, status: AgentStatus, service: ServiceId, port: RadioPort, model: Callable[[], FrozenModel],
                 run_ids: Callable[[], str | None], out: Path, config: AgentConfig,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        self.status, self.service, self.port, self.model = status, service, port, model
        self.run_ids, self.out, self.config, self.clock, self.sleep = run_ids, out, config, clock, sleep
        self.policy: FrozenDdqnPolicy | None = None
        self._quota_due = 0.0
        self._prb: int | None = None

    def _fallback(self, detail: str) -> None:
        self.status.effective, self.status.detail, self.status.running = RAN_DEFAULT, detail, False

    def _write(self, name: str, record: dict[str, Any]) -> None:
        with contextlib.suppress(OSError):
            self.out.mkdir(parents=True, exist_ok=True)
            with (self.out / name).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")

    async def _wait(self, seconds: float, samples: list[dict[str, float]] | None = None) -> None:
        """Sleep in 1 s ticks, renewing the DL quota when due and sampling the UN3GPP read-back."""

        end = self.clock() + seconds
        while (left := end - self.clock()) > 0:
            await self.sleep(min(1.0, left))
            if samples is not None and (s := self.port.ul_sample()) is not None:
                samples.append(s)
            if self._prb is not None and self.clock() >= self._quota_due:
                self._quota_due = self.clock() + self.config.quota_renew_s
                await self.port.renew_quota(self._prb)

    async def run(self) -> None:
        self.status.effective, self.status.detail = "starting", "loading the DDQN model"
        try:
            model = self.model()
        except PolicyLoadError as error:
            self._fallback(str(error))
            return
        self.policy = policy = FrozenDdqnPolicy(model, self.service, guard_failures=self.config.guard_failures)
        self.status.detail = "taking the O-RAN xApp control"
        reason = await self.port.open(self.service)
        if reason is not None:
            self._fallback(reason)
            with contextlib.suppress(Exception):
                await self.port.close()
            return
        self.status.effective, self.status.detail, self.status.running = DDQN, "frozen DDQN agent in control", True
        self.status.log_dir = str(self.out)
        self._write_metadata(model)
        applied, errors, step = False, 0, 0
        try:
            while True:
                decision = policy.choose()
                action = policy.action(decision.action)
                self.status.step, self.status.policy, self.status.action = step, decision.policy, action.label()
                t_decided = self.clock()
                row: dict[str, Any] | None = None
                entry: dict[str, Any] = {}
                try:
                    applied = True
                    row, entry = await self._step(action)
                    errors = 0
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - one bad step must not end the session
                    errors += 1
                    entry = {"error": repr(error)[:300]}
                    if errors >= self.config.max_step_errors or not await self.port.after_error(error):
                        self._log_step(step, decision, action, None, entry, t_decided, {})
                        self._fallback(f"E2 control lost ({repr(error)[:120]})")
                        break
                outcome = policy.record(decision, row)
                self._log_step(step, decision, action, row, entry, t_decided, outcome)
                step += 1
                if row is None:  # skipped or failed: keep the cadence instead of retrying at once
                    await self._wait(self.config.settle_s)
        finally:
            self.status.running = False
            if applied:
                with contextlib.suppress(Exception):
                    self.status.safe = await asyncio.wait_for(self.port.safe_defaults("end of demo session"),
                                                              self.config.safe_timeout_s)
                if self.status.effective == DDQN:
                    self.status.effective, self.status.detail = RAN_DEFAULT, "session ended: safe configuration"
            with contextlib.suppress(Exception):
                await self.port.close()

    async def _step(self, action: Action) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        t_apply = self.clock()
        statuses = await self.port.apply(action)
        entry: dict[str, Any] = {"apply_s": round(self.clock() - t_apply, 3), **statuses}
        if str(statuses.get("ul_status", "")).startswith("REJECTED"):
            entry["skipped"] = "ul action rejected by the BS safety mask"
            return None, entry
        self._prb, self._quota_due = action.dl_prb, self.clock() + self.config.quota_renew_s
        await self._wait(self.config.settle_s)
        runs = [r for r in (self.run_ids(),) if r]
        samples: list[dict[str, float]] = []
        t0 = self.clock()
        await self._wait(self.config.cell_s, samples)
        t1 = self.clock()
        await self._wait(self.config.flush_s)
        if (later := self.run_ids()) and later not in runs:  # the generator segment was renewed in the window
            runs.append(later)
        measured = await self.port.measure(runs, t0, t1)
        row = {**cell_spec(action), "service": self.service.value, **statuses, **measured, **ran_summary(samples),
               "window_start": datetime.fromtimestamp(t0, UTC).isoformat(),
               "window_end": datetime.fromtimestamp(t1, UTC).isoformat(), "generator_run_ids": runs}
        return row, entry

    def _log_step(self, step: int, d: Decision, action: Action, row: dict[str, Any] | None, entry: dict[str, Any],
                  t_decided: float, outcome: dict[str, Any]) -> None:
        assert self.policy is not None
        verdict = None if row is None else row.get("sla_met")
        shown = "skipped" if entry.get("skipped") else "error" if entry.get("error") else VERDICT[verdict]
        record = {"schema": STEP_SCHEMA, "session": self.status.session, "service": self.service.value,
                  "step": step, "policy": d.policy, "action": action.label(), "action_index": d.action,
                  "q_top5": d.q_top5, "blocked": [self.policy.action(i).label() for i in sorted(self.policy.blocked)],
                  "decided_at": datetime.fromtimestamp(t_decided, UTC).isoformat(),
                  "step_s": round(self.clock() - t_decided, 3), "verdict": shown, "sla_met": verdict,
                  "failures": self.policy.failures, **entry, **outcome, "cell": row}
        self._write("steps.jsonl", record)
        self.status.last_verdict, self.status.last_step_at = shown, self.clock()
        self.status.blocked = len(self.policy.blocked)
        self.status.steps.insert(0, {"step": step, "policy": d.policy, "action": action.label(), "verdict": shown,
                                     "at": self.clock()})
        del self.status.steps[self.config.history:]

    def _write_metadata(self, model: FrozenModel) -> None:
        assert self.policy is not None
        with contextlib.suppress(OSError):
            self.out.mkdir(parents=True, exist_ok=True)
            (self.out / "run-metadata.json").write_text(json.dumps({
                "schema": "smartfarm.demo_oran_run.v1", "session": self.status.session, "service": self.service.value,
                "started_at": datetime.now(UTC).isoformat(), "model": str(model.model), "policy": "ddqn",
                "learn": False, "epsilon": 0.0, "elimination_rule": "majority",
                "guard_failures": self.policy.guard_failures, "config": self.config.__dict__,
                "default_action": self.policy.action(self.policy.default).label(),
                "safe_actions": [self.policy.action(int(i)).label() for i in self.policy.mask.nonzero()[0]],
            }, indent=2) + "\n", encoding="utf-8")


class SessionLike(Protocol):
    id: str
    service: ServiceId
    state: str
    generator_run_id: str | None
    oran_mode: str | None


class OrchestratorLike(Protocol):
    @property
    def active(self) -> Any: ...

    def session(self, session_id: str, *, touch: bool = True) -> Any: ...


PortFactory = Callable[[str, Path], RadioPort]


class OranAgentSupervisor:
    """Starts the DDQN controller when a `ddqn` session streams, stops it when the session is no longer the active
    streaming one, and serves the mode/agent part of the session's `oran` view."""

    def __init__(self, orchestrator: OrchestratorLike, *, default_mode: str = RAN_DEFAULT,
                 port_factory: PortFactory | None = None, model: Callable[[], FrozenModel] | None = None,
                 log_root: Path | None = None, config: AgentConfig | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.orchestrator = orchestrator
        self.default_mode = parse_mode(default_mode) or RAN_DEFAULT
        self.port_factory = port_factory
        self.model = model
        self.log_root = log_root or Path("experiments/demo")
        self.config = config or AgentConfig()
        self.clock = clock
        self._status: OrderedDict[str, AgentStatus] = OrderedDict()
        self._task: asyncio.Task[None] | None = None
        self._task_session: str | None = None
        self._loop: asyncio.Task[None] | None = None
        self.ports_opened = 0

    # ------------------------------------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._loop = asyncio.create_task(self._watch(), name="oran-agent-supervisor")

    async def close(self) -> None:
        if self._loop is not None:
            self._loop.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._loop
        await self._stop_controller()

    async def _watch(self) -> None:
        while True:
            with contextlib.suppress(Exception):  # a supervisor hiccup must never reach the demo
                await self.tick()
            await asyncio.sleep(self.config.poll_s)

    def mode_of(self, session: Any) -> str:
        return getattr(session, "oran_mode", None) or self.default_mode

    def _status_for(self, session: Any) -> AgentStatus:
        st = self._status.get(session.id)
        if st is None:
            requested = self.mode_of(session)
            st = AgentStatus(session.id, session.service.value, requested,
                             detail="selected" if requested == RAN_DEFAULT else "the agent starts when the stream is up")
            if requested == DDQN:
                st.effective = "starting"
            self._status[session.id] = st
            while len(self._status) > 50:
                self._status.popitem(last=False)
        return st

    async def _stop_controller(self) -> None:
        task, self._task, self._task_session = self._task, None, None
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(task), self.config.safe_timeout_s + 5)

    async def tick(self) -> None:
        active = self.orchestrator.active
        streaming = active is not None and active.state == "streaming"
        if self._task_session is not None and (not streaming or active.id != self._task_session):
            await self._stop_controller()
        if active is None:
            return
        st = self._status_for(active)
        if st.requested != DDQN:
            return
        if not streaming:
            if active.state not in ("accepted", "un3gpp_switching", "un3gpp_in_slice", "generator_started"):
                if st.effective == "starting":
                    st.effective, st.detail = RAN_DEFAULT, f"session {active.state} before the agent started"
            return
        if self._task is None and st.effective == "starting" and not st.running and st.step is None:
            if self.port_factory is None or self.model is None:
                st.effective, st.detail = RAN_DEFAULT, "DDQN agent not available in this orchestrator"
                return
            day = datetime.now().strftime("%Y%m%d")
            out = self.log_root / day / f"oran-{active.id}"
            try:
                port = self.port_factory(active.service.value, out)
            except Exception as error:  # noqa: BLE001
                st.effective, st.detail = RAN_DEFAULT, f"O-RAN control unavailable: {error!r}"[:200]
                return
            self.ports_opened += 1
            session_id = active.id

            def run_id() -> str | None:
                s = self.orchestrator.session(session_id, touch=False)
                return getattr(s, "generator_run_id", None)

            controller = AgentSession(st, active.service, port, self.model, run_id, out, self.config)
            self._task = asyncio.create_task(controller.run(), name=f"oran-agent-{active.id}")
            self._task_session = active.id

    # ------------------------------------------------------------------------------------------- view

    def overlay(self, session: Any, base: dict[str, Any]) -> dict[str, Any]:
        """The mode and agent fields of an active session's `oran` view, on top of the read-back view `base`."""

        st = self._status_for(session)
        now = self.clock()
        out = dict(base)
        out["mode"] = {"requested": st.requested, "effective": st.effective, "detail": st.detail}
        out["agent_running"] = st.running
        out["agent"] = {"running": st.running, "step": st.step, "policy": st.policy, "action": st.action,
                        "last_verdict": st.last_verdict, "blocked": st.blocked,
                        "last_step_age_s": None if st.last_step_at is None else round(now - st.last_step_at, 1)}
        if st.effective == RAN_DEFAULT:
            out["decided_by"] = "RAN defaults"
        elif st.running and st.step is not None:
            out["decided_by"] = {"ddqn": f"DDQN agent (step {st.step})",
                                 "guard": f"RAN defaults (DDQN guard, step {st.step})",
                                 "initial-state": "RAN defaults (DDQN step 0, first state)"}.get(st.policy or "",
                                                                                                  "DDQN agent")
        if st.steps:
            out["history"] = [{"link": "step", "value": f"#{s['step']} {s['action']}",
                               "status": f"SLA {s['verdict']}" if s["verdict"] in VERDICT.values() else s["verdict"],
                               "ok": {"met": True, "not met": False}.get(s["verdict"]),
                               "age_s": round(now - s["at"], 1), "policy": s["policy"]} for s in st.steps]
        return out
