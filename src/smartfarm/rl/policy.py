"""Frozen Double DQN policy of one service session, as the online agent runs it (deployment/scripts/rl_online.py).

Same model, action index, safety mask and decision rule as `rl_online.py --policy ddqn --epsilon 0
--elimination-rule majority --guard-failures 3` without `--learn`:
  - only the service's safe actions (`Dataset.allowed`: measured, not rejected by the BS, UL MCS <= 14);
  - greedy over the Q-values of the actions not eliminated in this session;
  - majority elimination: an agent action is blocked for the rest of the session once its SLA misses outnumber its
    successes (always leaving one action to choose);
  - guard: after `guard_failures` consecutive agent steps missing the SLA, one step falls back to the RAN defaults
    (BS link adaptation, smallest DL quota of the service), then control returns to the agent;
  - no gradient update, no exploration.
One addition for steps judged from the app's reports (demo): a step whose verdict is undetermined (a requirement
could not be computed) is neither a success nor a failure for the guard and the elimination; it only moves the state.
A skipped step (the BS rejected the UL action) counts as a failure of the agent, as in rl_online.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId
from smartfarm.rl.dataset import Action, Dataset, cell_from_row
from smartfarm.rl.ddqn import DdqnAgent
from smartfarm.rl.env import OBS_DIM, RewardConfig, observe, resource_cost, reward

# the final model of the paper and the calibration runs that define its action index (report.md of the model)
FINAL_MODEL = "experiments/rl/20261104_170128-ddqn-phi-g0.5-m0.0-d1.0-final-crop/model-final.npz"
FINAL_RUNS = ("experiments/calibration/20261104_071216-drone-final-4passes",
              "experiments/calibration/20261104_121141-crop-final-4passes",
              "experiments/calibration/20261103_123134-weather-4passes")
MARGIN_WEIGHT = 0.0  # reward of the final model (rl_online --margin-weight 0.0)


class PolicyLoadError(RuntimeError):
    """The model or its calibration runs cannot be loaded, or they do not match."""


@dataclass
class FrozenModel:
    data: Dataset
    agent: DdqnAgent
    model: Path

    @classmethod
    def load(cls, model: Path, runs: list[Path]) -> FrozenModel:
        try:
            data = Dataset.load(runs)
            agent = DdqnAgent.load(model, OBS_DIM, len(data.actions))
            saved = np.load(model, allow_pickle=False)
            meta = ast.literal_eval(str(saved["meta"])) if "meta" in saved.files else {}
        except (OSError, ValueError, KeyError, SyntaxError) as error:
            raise PolicyLoadError(f"cannot load the DDQN model: {error}") from error
        labels = meta.get("actions")
        if labels is not None and labels != [a.label() for a in data.actions]:
            raise PolicyLoadError("the calibration runs do not give the model's action index")
        return cls(data, agent, model)


@dataclass
class Decision:
    action: int
    policy: str  # initial-state | ddqn | guard
    q_top5: list[tuple[str, float]]


@dataclass
class FrozenDdqnPolicy:
    """Decisions and session bookkeeping of the frozen agent for one service session."""

    model: FrozenModel
    service: ServiceId
    guard_failures: int = 3
    elimination: bool = True
    rcfg: RewardConfig = field(default_factory=lambda: RewardConfig(margin_weight=MARGIN_WEIGHT))
    blocked: set[int] = field(default_factory=set)
    tally: dict[int, list[int]] = field(default_factory=dict)  # agent action -> [SLA met, SLA missed]
    failures: int = 0
    obs: np.ndarray | None = None

    def __post_init__(self) -> None:
        data = self.model.data
        self.mask = np.zeros(len(data.actions), dtype=bool)
        self.mask[data.allowed[self.service]] = True
        self.default = data.actions.index(Action(-1, -1, min(SERVICE_CATALOG[self.service].radio_bounds.dl_prb)))

    def action(self, index: int) -> Action:
        return self.model.data.actions[index]

    def choose(self) -> Decision:
        """The next joint action: the RAN defaults first (to get a state), the guard step, or the greedy agent."""

        if self.obs is None:
            return Decision(self.default, "initial-state", [])
        mask = self.mask.copy()
        if self.elimination:
            mask[list(self.blocked)] = False
        q = self.model.agent.q(self.obs, mask)
        top = [(self.action(int(i)).label(), round(float(q[i]), 4)) for i in np.argsort(-q)[:5]]
        if self.failures >= self.guard_failures:
            return Decision(self.default, "guard", top)
        return Decision(int(np.argmax(q)), "ddqn", top)

    def record(self, decision: Decision, row: dict[str, Any] | None) -> dict[str, Any]:
        """Account one finished step. `row`: the measured step (cell record with svc_*, sla_met, telemetry, the
        action fields), or None when the step was skipped (UL action rejected) or failed. Returns what to log."""

        a, policy = decision.action, decision.policy
        out: dict[str, Any] = {}
        if row is None:
            if policy == "ddqn":
                self.failures += 1
            elif policy == "guard":
                self.failures = 0
            return out
        cell = cell_from_row({**row, "service": self.service.value})
        verdict = row.get("sla_met")  # True | False | None (undetermined)
        out.update(reward=reward(self.service, cell, self.rcfg), cost=resource_cost(self.service, cell, self.rcfg))
        self.obs = observe(self.service, cell)  # the state moves even when the verdict is undetermined
        if verdict is None:
            out["undetermined"] = True
            return out
        if policy == "ddqn":
            self.failures = 0 if verdict else self.failures + 1
            met_missed = self.tally.setdefault(a, [0, 0])
            met_missed[0 if verdict else 1] += 1
            if (self.elimination and not verdict and met_missed[1] > met_missed[0]
                    and len(self.blocked) < int(self.mask.sum()) - 1):
                self.blocked.add(a)
                out["eliminated"] = self.action(a).label()
        elif policy == "guard":
            self.failures = 0
        return out
