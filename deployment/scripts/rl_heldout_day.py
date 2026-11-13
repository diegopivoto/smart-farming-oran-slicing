#!/usr/bin/env python3
"""Evaluates trained DDQN models, unchanged, on a calibration of one service measured on another day (held-out day).

The models keep the action index of the runs they were trained on (--trained-runs); the held-out calibration is mapped
onto that index by action label, and only the actions accepted on the held-out day are allowed. Each held-out pass is
one 20-step session per policy, as in rl_offline.py: DDQN (+ elimination) of every model, the oracle of the pass, the
fixed tie-break winner of the other held-out passes, the RAN defaults (BS link adaptation, smallest DL quota) and a
random safe action.

    rl_heldout_day.py --service weather --heldout experiments/calibration/<new weather run> \
        --models experiments/rl/<final>/ experiments/rl/ablation-final-*/<variant>/ ... --output experiments/rl/<out>
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rl_offline import RUNS, STEPS, oracle, session, tie_break_winner  # noqa: E402

from smartfarm.contracts.service import ServiceId  # noqa: E402
from smartfarm.rl.dataset import Dataset  # noqa: E402
from smartfarm.rl.ddqn import DdqnAgent  # noqa: E402
from smartfarm.rl.env import OBS_DIM, CalibrationEnv, RewardConfig  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]


def agent_policy(agent: DdqnAgent, to_model: np.ndarray, to_env: dict[int, int], mask_model: np.ndarray):
    """Greedy choice of a model trained on another action index, returned in the held-out dataset's index."""

    def choose(obs: np.ndarray, blocked: set[int] | None = None) -> int:
        m = mask_model.copy()
        if blocked:
            m[[to_model[b] for b in blocked]] = False
        return to_env[int(np.argmax(agent.q(obs, m)))]

    return choose


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--service", choices=[s.value for s in ServiceId], required=True)
    parser.add_argument("--heldout", type=Path, required=True, help="calibration run of the service on another day")
    parser.add_argument("--trained-runs", nargs="+", default=[str(PROJECT / "experiments/calibration" / r) for r in RUNS],
                        help="calibration runs the models were trained on (they define the models' action index)")
    parser.add_argument("--models", nargs="+", type=Path, required=True, help="training dirs holding model-final.npz")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--margin-weight", type=float, default=0.0)
    args = parser.parse_args()
    service = ServiceId(args.service)
    trained = Dataset.load([Path(r) for r in args.trained_runs])
    others = [Path(r) for r in args.trained_runs if Dataset.load([Path(r)]).cells[0].service != service]
    data = Dataset.load([*others, args.heldout])
    model_index = {a.label(): i for i, a in enumerate(trained.actions)}
    missing = [a.label() for a in data.actions if a.label() not in model_index]
    if missing:
        sys.exit(f"held-out actions unknown to the models: {missing}")
    to_model = np.array([model_index[a.label()] for a in data.actions])
    to_env = {int(m): i for i, m in enumerate(to_model)}
    rcfg = RewardConfig(margin_weight=args.margin_weight)
    passes = data.passes[service]
    mask_model = np.zeros(len(trained.actions), dtype=bool)
    mask_model[to_model[data.allowed[service]]] = True

    results: dict[str, list[dict[str, Any]]] = {}
    chosen: dict[str, Counter[str]] = {}
    models = {m.name: DdqnAgent.load(m / "model-final.npz", OBS_DIM, len(trained.actions)) for m in args.models}
    for p in passes:
        test = CalibrationEnv(data, {s: [p] for s in data.passes}, rcfg, steps=STEPS, drift=0.0, seed=100 + p)
        rng = random.Random(p)
        others_p = [q for q in passes if q != p]
        runs: dict[str, list[dict[str, Any]]] = {}
        for name, agent in models.items():
            choose = agent_policy(agent, to_model, to_env, mask_model)
            runs[f"{name} | DDQN + elimination"] = session(test, service, p, choose, eliminate=True)
            runs[f"{name} | DDQN"] = session(test, service, p, choose)
        best, fixed = oracle(test, service, p), tie_break_winner(test, service, others_p)
        runs["oracle"] = session(test, service, p, lambda o, b=best: b)
        runs["fixed winner"] = session(test, service, p, lambda o, f=fixed: f)
        runs["link adapt."] = session(test, service, p, lambda o: test.default[service])
        runs["random"] = session(test, service, p, lambda o: rng.choice(data.allowed[service]))
        for name, steps in runs.items():
            results.setdefault(name, []).extend({**x, "pass": p} for x in steps)
            chosen.setdefault(name, Counter()).update(data.actions[x["action"]].label() for x in steps)
        print(f"pass {p} " + "  ".join(f"{n}: {sum(x['sla_met'] for x in r)}/{len(r)}" for n, r in runs.items()
                                       if "|" not in n or n.startswith(next(iter(models)))), flush=True)
    summary = {n: {"sla_met_ratio": round(statistics.mean(x["sla_met"] for x in v), 3),
                   "mean_cost": round(statistics.mean(x["cost"] for x in v), 3),
                   "mean_reward": round(statistics.mean(x["reward"] for x in v), 3), "steps": len(v),
                   "actions": chosen[n].most_common(3)} for n, v in results.items()}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "eval.json").write_text(json.dumps({
        "schema": "smartfarm.rl_heldout_day.v1", "service": service.value, "heldout": str(args.heldout),
        "trained_runs": args.trained_runs, "models": [str(m) for m in args.models], "passes": passes,
        "allowed_actions": len(data.allowed[service]), "summary": summary}, indent=2) + "\n")
    for n, s in summary.items():
        print(f"{n:70s} SLA {s['sla_met_ratio']:.3f}  cost {s['mean_cost']:.3f}  {s['actions'][:1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
