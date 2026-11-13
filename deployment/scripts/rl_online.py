#!/usr/bin/env python3
"""Online Double DQN joint radio control in the lab: the agent decides, the O-RAN xApp applies it over E2.

Usage: rl_online.py --service crop --model experiments/rl/<run>/model-final.npz --output experiments/rl-online/<dir>
                    [--steps 20] [--policy ddqn|link-adaptation|alternate] [--learn] [--epsilon 0.0]

Every step is one 30 s cell of real service traffic, run exactly like the calibration sweep (same E2 control path,
same health checks, recovery and safe defaults; deployment/scripts/calibration_sweep.py): the joint action (UN3GPP UL
MCS/RBs or the BS link adaptation, plus the OAI DL PRB quota of the slice) is chosen by the policy from the state the
previous cell produced (RAN telemetry over E2, KPM, end-to-end SLA margins at the phone, the action in force). The
session starts under the RAN defaults (BS link adaptation, smallest DL quota) to get the first state.

Safety and adaptation, on top of the sweep's own:
  - only actions of the service's safety mask can be chosen (BS-rejected actions and UL MCS >= 15 are excluded);
  - action elimination (default on, --no-elimination to disable): an agent action that misses the SLA is blocked for
    the rest of the session and the agent falls back to its next best action (evaluated offline in rl_offline.py);
  - after --guard-failures consecutive steps missing the SLA, the next step falls back to the RAN defaults (logged as
    a guard step), then control returns to the agent.
--policy alternate interleaves agent and link-adaptation steps for an A/B comparison under the same channel.
--learn keeps training the agent online on the lab transitions (replay + one gradient step per stored transition) and
saves the updated model next to the log.
Output: <out>/steps.jsonl (one record per step: the cell record + policy, chosen action, top Q-values, reward),
        <out>/cells.jsonl, events.jsonl (sweep), run-metadata.json, model-online.npz with --learn.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "deployment/scripts"))
import calibration_sweep as sweep_mod  # noqa: E402
from calibration_sweep import SOCKET, Sweep  # noqa: E402
from rl_offline import RUNS  # noqa: E402

from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId  # noqa: E402
from smartfarm.e2.control_client import E2ControlClient, E2ControlError  # noqa: E402
from smartfarm.rl.dataset import LA, Action, Dataset, cell_from_row  # noqa: E402
from smartfarm.rl.ddqn import DdqnAgent  # noqa: E402
from smartfarm.rl.env import OBS_DIM, CalibrationEnv, RewardConfig, observe, resource_cost, reward  # noqa: E402


def cell_spec(a: Action) -> dict[str, Any]:
    if a.link_adaptation:
        return {"ul_mode": "link-adaptation", "ul_mcs": None, "ul_rb": None, "dl_prb": a.dl_prb}
    return {"ul_mode": "override", "ul_mcs": a.ul_mcs, "ul_rb": a.ul_rb, "dl_prb": a.dl_prb}


async def reconnect_e2(sw: Sweep, attempts: int = 30) -> bool:
    """New E2 control connection after the xApp went away (it is restarted by run_sf_control_supervised.sh)."""

    try:
        await sw.e2.close()
    except Exception:  # noqa: BLE001 - the old connection is already broken
        pass
    for _ in range(attempts):
        try:
            e2 = E2ControlClient(SOCKET)
            await e2.connect()
            for _ in range(50):
                if e2.snapshot_for("un3gpp") and e2.snapshot_for("oai"):
                    sw.e2 = e2
                    sw.event("e2_reconnected")
                    return True
                await asyncio.sleep(0.2)
            await e2.close()
        except (OSError, E2ControlError):
            pass
        await asyncio.sleep(2)
    return False


class OnlineAgent:
    def __init__(self, args: argparse.Namespace, data: Dataset, agent: DdqnAgent) -> None:
        self.args, self.data, self.agent = args, data, agent
        self.service = ServiceId(args.service)
        self.mask = np.zeros(len(data.actions), dtype=bool)
        self.mask[data.allowed[self.service]] = True
        self.default = data.actions.index(Action(-1, -1, min(SERVICE_CATALOG[self.service].radio_bounds.dl_prb)))
        self.rcfg = RewardConfig(margin_weight=args.margin_weight)
        self.steps_log = (args.output / "steps.jsonl").open("a", encoding="utf-8")
        self.blocked: set[int] = set()
        self.tally: dict[int, list[int]] = {}  # agent action -> [SLA met, SLA missed] in this session
        self.recent: list[tuple[int, bool]] = []  # agent steps: (action, SLA met), for --converge-k

    def block_by_cause(self, a: int, record: dict[str, Any]) -> dict[str, Any] | None:
        """Monotone elimination from the cause of an SLA miss, seen in any step (agent or RAN defaults):
        loss/retransmissions on the UN3GPP uplink -> block that UL MCS and every higher one (any RB, any DL quota);
        UL capacity (goodput short, the BS MAC below 97 % of the offered rate) -> block that UL MCS and every lower
        one with as many RBs or fewer; a DL queue (goodput short, RLC delay over 300 ms) -> block that DL quota and
        every lower one. No known cause -> None, and the joint-action rule (majority) applies."""
        failed = {m.name for m in SERVICE_CATALOG[self.service].sla
                  if (v := record.get("svc_" + m.name)) is not None
                  and not {"ge": v >= m.threshold, "le": v <= m.threshold, "eq": v == m.threshold}[m.operator]}
        act = self.data.actions[a]
        allowed = set(np.flatnonzero(self.mask))
        if failed & {"packet_loss_ratio", "retransmitted_bytes_ratio"} and act.ul_mcs != LA:
            rule, hit = f"UL MCS >= {act.ul_mcs}", {i for i in allowed if self.data.actions[i].ul_mcs not in (LA,)
                                                    and self.data.actions[i].ul_mcs >= act.ul_mcs}
        elif (failed & {"goodput_p10", "goodput"} and act.ul_mcs != LA and (mac := record.get("un3gpp_mac_mbps"))
              and mac * 1e6 < 0.97 * SERVICE_CATALOG[self.service].offered_rate_bps):
            rule, hit = f"UL MCS <= {act.ul_mcs} with <= {act.ul_rb} RB", {
                i for i in allowed if self.data.actions[i].ul_mcs not in (LA,)
                and self.data.actions[i].ul_mcs <= act.ul_mcs and self.data.actions[i].ul_rb <= act.ul_rb}
        elif failed & {"goodput_p10", "goodput"} and (record.get("kpm_rlc_delay_dl_ms") or 0) > 300:
            rule, hit = f"DL quota <= {act.dl_prb}", {i for i in allowed if self.data.actions[i].dl_prb <= act.dl_prb}
        else:
            return None
        new = hit - self.blocked  # the guard still falls back to the RAN defaults, but the agent may not pick them
        if len(self.blocked | new) >= len(allowed):  # always leave something to choose
            return None
        self.blocked |= new
        return {"rule": rule, "failed": sorted(failed), "blocked_now": len(new)}

    def anchor_replay(self, n: int, seed: int = 11) -> None:
        """Seed the online replay with calibration transitions of this service, so online updates mix the new lab
        experience with what the agent already knew instead of overwriting it with a handful of samples."""
        env = CalibrationEnv(self.data, self.data.passes, self.rcfg, steps=20, drift=1.0, seed=seed)
        allowed = list(self.data.allowed[self.service])
        rng = np.random.default_rng(seed)
        obs = env.reset(self.service)
        for k in range(n):
            a = int(rng.choice(allowed))
            new_obs, r, done, _ = env.step(a)
            self.agent.replay.add(obs, a, r, new_obs, False, self.mask)
            obs = env.reset(self.service) if done else new_obs

    def converged(self) -> bool:
        """--converge-k K: the agent's last K steps took the same action and all met the SLA (after --min-syncs
        target-network updates with --learn)."""
        k = self.args.converge_k
        if not k or len(self.recent) < k:
            return False
        last = self.recent[-k:]
        syncs = self.agent.grad_steps // self.agent.cfg.target_sync if self.args.learn else self.args.min_syncs
        return len({a for a, _ in last}) == 1 and all(met for _, met in last) and syncs >= self.args.min_syncs

    def choose(self, obs: np.ndarray, step: int, failures: int) -> tuple[int, str, list[tuple[str, float]]]:
        mask = self.mask.copy()
        if not self.args.no_elimination:
            mask[list(self.blocked)] = False
        q = self.agent.q(obs, mask)
        top = [(self.data.actions[i].label(), round(float(q[i]), 4)) for i in np.argsort(-q)[:5]]
        if failures >= self.args.guard_failures:
            return self.default, "guard", top
        policy = self.args.policy
        if policy == "alternate":
            policy = "ddqn" if step % 2 else "link-adaptation"
        if policy == "link-adaptation":
            return self.default, "link-adaptation", top
        explore = self.args.epsilon > 0 and self.agent.rng.random() < self.args.epsilon
        a = int(self.agent.rng.choice(np.flatnonzero(mask))) if explore else int(np.argmax(q))
        return a, "ddqn-explore" if explore else "ddqn", top

    async def run(self, sw: Sweep) -> int:
        sw.event("rl_start", service=self.service.value, steps=self.args.steps, policy=self.args.policy,
                 model=str(self.args.model), learn=self.args.learn)
        obs: np.ndarray | None = None
        failures, index = 0, 0
        try:
            for step in range(self.args.steps + 1):  # step 0 = RAN defaults, gives the first state
                ok, detail = await sw.healthy()
                if not ok:
                    sw.event("unhealthy", detail=detail)
                    if not await sw.recover():
                        sw.event("abort", detail="UEs not recovered within the outage budget")
                        return 2
                if obs is None:
                    a, policy, top = self.default, "initial-state", []
                else:
                    a, policy, top = self.choose(obs, step, failures)
                index += 1
                t0 = time.time()
                try:
                    record = await sw.cell(index, 0, cell_spec(self.data.actions[a]))
                except Exception as error:  # noqa: BLE001 - one bad step must not end the session
                    record = {"index": index, "error": repr(error)[:300], **cell_spec(self.data.actions[a])}
                    if isinstance(error, (E2ControlError, OSError)):  # xApp gone: reconnect before anything else
                        sw.event("e2_lost", detail=repr(error)[:200])
                        if not await reconnect_e2(sw):
                            sw.event("abort", detail="E2 control socket did not come back")
                            return 2
                    await sw.safe_defaults("step error")
                sw.cells_log.write(json.dumps(record, default=str) + "\n")
                sw.cells_log.flush()
                entry: dict[str, Any] = {"schema": "smartfarm.rl_online_step.v1", "step": step, "policy": policy,
                                         "blocked": [self.data.actions[i].label() for i in sorted(self.blocked)],
                                         "action": self.data.actions[a].label(), "action_index": a, "q_top5": top,
                                         "decided_at": datetime.fromtimestamp(t0, UTC).isoformat(), "cell": record}
                if "error" not in record and not record.get("skipped"):
                    cell = cell_from_row({**record, "service": self.service.value})
                    r = reward(self.service, cell, self.rcfg)
                    entry.update(reward=r, sla_met=cell.sla_met, cost=resource_cost(self.service, cell, self.rcfg))
                    new_obs = observe(self.service, cell)
                    if self.args.learn and obs is not None:
                        self.agent.replay.add(obs, a, r, new_obs, False, self.mask)
                        self.agent.env_steps += 1
                        for _ in range(self.args.learn_steps):
                            self.agent.learn()
                        entry["target_syncs"] = self.agent.grad_steps // self.agent.cfg.target_sync
                    if policy.startswith("ddqn"):  # the guard watches the agent only (A/B steps stay untouched)
                        failures = 0 if cell.sla_met else failures + 1
                    elif policy == "guard":  # one fallback step, then control returns to the agent
                        failures = 0
                    if policy.startswith("ddqn"):
                        met_missed = self.tally.setdefault(a, [0, 0])
                        met_missed[0 if cell.sla_met else 1] += 1
                    # first-failure: block on the first miss; majority: block once misses outnumber successes
                    eliminate = (not cell.sla_met and (self.args.elimination_rule == "first-failure"
                                                       or met_missed[1] > met_missed[0])) if policy.startswith("ddqn") else False
                    if self.args.elimination_rule == "cause" and not cell.sla_met and not self.args.no_elimination:
                        cause_blocked = self.block_by_cause(a, record)
                        if cause_blocked:  # a known cause replaces the joint-action rule; otherwise majority applies
                            eliminate = False
                            entry["eliminated_by_cause"] = cause_blocked
                    if (eliminate and not self.args.no_elimination
                            and len(self.blocked) < int(self.mask.sum()) - 1):
                        self.blocked.add(a)
                        entry["eliminated"] = self.data.actions[a].label()
                    obs = new_obs
                elif policy.startswith("ddqn"):
                    failures += 1
                elif policy == "guard":
                    failures = 0
                self.steps_log.write(json.dumps(entry, default=str) + "\n")
                self.steps_log.flush()
                sw.event("rl_step", step=step, policy=policy, action=entry["action"], sla=entry.get("sla_met"),
                         reward=entry.get("reward"), error=record.get("error"))
                if policy.startswith("ddqn"):
                    self.recent.append((a, bool(entry.get("sla_met"))))
                if self.converged() and (self.args.policy != "alternate" or step % 2 == 0):  # end on a full A/B pair
                    sw.event("converged", step=step, action=self.data.actions[self.recent[-1][0]].label(),
                             k=self.args.converge_k, target_syncs=self.agent.grad_steps // self.agent.cfg.target_sync)
                    break
                await asyncio.sleep(self.args.pause_s)
        finally:
            await sw.safe_defaults("end of RL session")
            if self.args.learn:
                self.agent.save(self.args.output / "model-online.npz",
                                {"actions": [x.label() for x in self.data.actions], "from": str(self.args.model)})
        return 0


async def main_async(args: argparse.Namespace) -> int:
    service = ServiceId(args.service)
    sweep_mod.SERVICE = service  # the sweep's cell() targets this service's slice and traffic
    args.rate_bps = args.rate_bps or SERVICE_CATALOG[service].offered_rate_bps
    args.output.mkdir(parents=True, exist_ok=True)
    data = Dataset.load([Path(r) if "/" in r else PROJECT / "experiments/calibration" / r for r in args.runs])
    agent = DdqnAgent.load(args.model, OBS_DIM, len(data.actions))
    if args.target_sync:
        agent.cfg = replace(agent.cfg, target_sync=args.target_sync)
    (args.output / "run-metadata.json").write_text(json.dumps({
        "schema": "smartfarm.rl_online_run.v1", "started_at": datetime.now(UTC).isoformat(), "service": service.value,
        "model": str(args.model), "policy": args.policy, "steps": args.steps, "learn": args.learn,
        "epsilon": args.epsilon, "guard_failures": args.guard_failures, "elimination": not args.no_elimination,
        "elimination_rule": args.elimination_rule, "learn_steps": args.learn_steps, "anchor_replay": args.anchor_replay,
        "agent_config": asdict(agent.cfg), "converge_k": args.converge_k, "min_syncs": args.min_syncs,
        "cell_s": args.cell_s,
        "rate_bps": args.rate_bps, "safe_actions": [data.actions[i].label() for i in data.allowed[service]]},
        indent=2) + "\n", encoding="utf-8")
    sweep_mod.UN3GPP_CLOCK_OFFSET_NS = sweep_mod.measure_pc2_offset_ns()
    offset_s = sweep_mod.UN3GPP_CLOCK_OFFSET_NS / 1e9
    print(f"PC2 clock offset vs PC1: {offset_s:+.3f} s (UL actions stamped in the BS clock)", flush=True)
    meta = json.loads((args.output / "run-metadata.json").read_text())
    (args.output / "run-metadata.json").write_text(json.dumps({**meta, "pc2_clock_offset_s": offset_s}, indent=2) + "\n")
    async with E2ControlClient(SOCKET) as e2:
        for _ in range(50):
            if e2.snapshot_for("un3gpp") and e2.snapshot_for("oai"):
                break
            await asyncio.sleep(0.2)
        online = OnlineAgent(args, data, agent)
        if args.learn and args.anchor_replay:
            online.anchor_replay(args.anchor_replay)
            print(f"replay anchored with {agent.replay.n} calibration transitions", flush=True)
        return await online.run(Sweep(args, e2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--service", choices=[s.value for s in ServiceId], required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--runs", nargs="+", default=RUNS,
                        help="calibration runs the model was trained on (they define the action index)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--policy", choices=["ddqn", "link-adaptation", "alternate"], default="ddqn")
    parser.add_argument("--epsilon", type=float, default=0.0, help="online exploration rate (0 = greedy)")
    parser.add_argument("--learn", action="store_true", help="keep training on the lab transitions")
    parser.add_argument("--learn-steps", type=int, default=8, help="gradient steps per lab transition with --learn")
    parser.add_argument("--margin-weight", type=float, default=0.0, help="must match the trained model's reward (best model: 0.0)")
    parser.add_argument("--guard-failures", type=int, default=3)
    parser.add_argument("--no-elimination", action="store_true", help="disable online action elimination")
    parser.add_argument("--anchor-replay", type=int, default=0,
                        help="with --learn: seed the replay with this many calibration transitions of the service")
    parser.add_argument("--elimination-rule", choices=["first-failure", "majority", "cause"], default="first-failure",
                        help="block an agent action on its first SLA miss, or once its misses outnumber its successes; "
                             "cause: block by the cause of any miss (UL loss -> that UL MCS and higher, DL queue -> that "
                             "DL quota and lower)")
    parser.add_argument("--target-sync", type=int, default=0,
                        help="with --learn: gradient steps between target-network updates (0 = the model's own)")
    parser.add_argument("--converge-k", type=int, default=0,
                        help="end the session (--steps is then the cap) once the agent's last K steps took the same "
                             "action and met the SLA")
    parser.add_argument("--min-syncs", type=int, default=0,
                        help="with --converge-k and --learn: also require this many target-network updates")
    parser.add_argument("--cell-s", type=int, default=30)
    parser.add_argument("--settle-s", type=float, default=3.0)
    parser.add_argument("--pause-s", type=float, default=3.0)
    parser.add_argument("--rate-bps", type=int)
    parser.add_argument("--max-outage-s", type=float, default=600)
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
