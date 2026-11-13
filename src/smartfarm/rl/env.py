"""Empirical environment built from the measured calibration cells (offline training and evaluation).

An episode is one service session: the phone requested a service, both UEs are in its slice, and the agent picks one
joint radio action every 30 s. The outcome of an action is a real measured cell of that action. The channel condition
is the calibration pass the cell came from; it persists across steps and drifts to another pass with a small
probability, as the UN3GPP link did between passes. The session starts under the RAN defaults (BS link adaptation,
smallest DL quota), so the first observation is a real cell of that action.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import numpy as np

from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId
from smartfarm.rl.dataset import MAX_REQUIREMENTS, MAX_SAFE_UL_MCS, SERVICES, TELEMETRY, Action, Cell, Dataset

OBS_DIM = len(SERVICES) + len(TELEMETRY) + MAX_REQUIREMENTS + 1 + 4


@dataclass(frozen=True)
class RewardConfig:
    """r = 1[whole SLA met] - cost_weight * resource cost in [0, 1].

    The cost follows the user's tie-break rule (lowest UL MCS, then lowest UL RBs, then lowest DL PRB quota) through
    decreasing weights; cost_weight < 1 keeps the SLA dominant, so the cost only ranks actions that meet it equally.
    The optional margin term rewards how comfortably the tightest requirement is met: with four samples per action,
    an action that met the SLA by a hair and one that met it with room look the same to the binary term alone.
    """

    cost_weight: float = 0.3
    margin_weight: float = 0.0  # + margin_weight * (margin of the tightest requirement, in [-1, 1])
    w_mcs: float = 0.5
    w_rb: float = 0.3
    w_prb: float = 0.2


def _unit(value: float, lo: float, hi: float) -> float:
    return 0.0 if hi <= lo else max(0.0, min(1.0, (value - lo) / (hi - lo)))


def resource_cost(service: ServiceId, cell: Cell, cfg: RewardConfig) -> float:
    """Cost of the radio resources of the cell, normalized to the service bounds. Link adaptation is charged what
    the BS actually used (observed MCS and RBs read over E2)."""

    b = SERVICE_CATALOG[service].radio_bounds
    a = cell.action
    if a.link_adaptation:
        mcs = cell.raw.get("un3gpp_ul_mcs_observed") or b.ul_mcs_max
        rb = cell.raw.get("un3gpp_ul_rb_observed_max") or max(b.ul_rb)
    else:
        mcs, rb = a.ul_mcs, a.ul_rb
    return (cfg.w_mcs * _unit(mcs, b.ul_mcs_min, min(b.ul_mcs_max, MAX_SAFE_UL_MCS))
            + cfg.w_rb * _unit(rb, min(b.ul_rb), max(b.ul_rb))
            + cfg.w_prb * _unit(a.dl_prb, min(b.dl_prb), max(b.dl_prb)))


def reward(service: ServiceId, cell: Cell, cfg: RewardConfig) -> float:
    tightest = min(cell.margins) if cell.margins else 0.0
    return float(cell.sla_met) - cfg.cost_weight * resource_cost(service, cell, cfg) + cfg.margin_weight * tightest


def observe(service: ServiceId, cell: Cell) -> np.ndarray:
    """State after a cell: service, RAN telemetry, SLA margins, SLA met, the action in force."""

    one_hot = [1.0 if s == service else 0.0 for s in SERVICES]
    tele = [cell.telemetry.get(k, 0.0) / scale for k, scale in TELEMETRY.items()]
    margins = list(cell.margins) + [0.0] * (MAX_REQUIREMENTS - len(cell.margins))
    a = cell.action
    act = [1.0, 0.0, 0.0, a.dl_prb / 106.0] if a.link_adaptation else [0.0, a.ul_mcs / 17.0, a.ul_rb / 132.0,
                                                                        a.dl_prb / 106.0]
    return np.asarray(one_hot + tele + margins + [float(cell.sla_met)] + act, dtype=np.float64)


def action_features(actions: list[Action]) -> np.ndarray:
    """phi(a): link adaptation flag, normalized UL MCS, UL RBs and DL PRB quota (0 for the UL of link adaptation)."""

    return np.asarray([[1.0, 0.0, 0.0, a.dl_prb / 106.0] if a.link_adaptation
                       else [0.0, a.ul_mcs / 17.0, a.ul_rb / 132.0, a.dl_prb / 106.0] for a in actions])


class CalibrationEnv:
    def __init__(self, data: Dataset, passes: dict[ServiceId, list[int]], cfg: RewardConfig | None = None,
                 steps: int = 20, drift: float = 0.15, seed: int = 0) -> None:
        self.data, self.passes, self.cfg, self.steps, self.drift = data, passes, cfg or RewardConfig(), steps, drift
        self.rng = random.Random(seed)
        self.cells = data.index()
        defaults = {s: Action(-1, -1, min(SERVICE_CATALOG[s].radio_bounds.dl_prb)) for s in SERVICES}
        self.default = {s: data.actions.index(a) for s, a in defaults.items() if a in data.actions}
        self.service = SERVICES[0]
        self.pass_no = 0
        self.t = 0

    def mask(self, service: ServiceId | None = None) -> np.ndarray:
        m = np.zeros(len(self.data.actions), dtype=bool)
        m[self.data.allowed[service or self.service]] = True
        return m

    def outcome(self, action: int) -> Cell:
        cell = self.cells.get((self.service, action, self.pass_no))
        if cell is None:  # not measured in this pass (invalidated cell): the same action in another pass
            alternatives = [p for p in self.passes[self.service] if (self.service, action, p) in self.cells]
            cell = self.cells[(self.service, action, self.rng.choice(alternatives))]
        return cell

    def reset(self, service: ServiceId | None = None, pass_no: int | None = None) -> np.ndarray:
        self.service = service or self.rng.choice([s for s in SERVICES if self.passes.get(s)])
        self.pass_no = pass_no if pass_no is not None else self.rng.choice(self.passes[self.service])
        self.t = 0
        return observe(self.service, self.outcome(self.default[self.service]))

    def step(self, action: int) -> tuple[np.ndarray, float, bool, Cell]:
        if action not in self.data.allowed[self.service]:
            raise ValueError(f"action {self.data.actions[action].label()} is masked for {self.service.value}")
        cell = self.outcome(action)
        r = reward(self.service, cell, self.cfg)
        self.t += 1
        if self.drift and len(self.passes[self.service]) > 1 and self.rng.random() < self.drift:
            self.pass_no = self.rng.choice([p for p in self.passes[self.service] if p != self.pass_no])
        return observe(self.service, cell), r, self.t >= self.steps, cell
