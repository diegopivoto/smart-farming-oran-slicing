#!/usr/bin/env python3
"""Offline training and evaluation of the Double DQN joint radio controller on the calibration datasets.

Usage: rl_offline.py --output experiments/rl/<dir> [--episodes 4000] [--runs <calibration dirs>...]

Evaluation is leave-one-pass-out: for each calibration pass p, the agent is trained on the cells of the other three
passes and tested, greedily, on 20-step sessions of every service under the channel of pass p, which it never saw.
Reference policies on the same held-out pass:
  oracle        best single action of pass p in hindsight (upper bound, not realizable)
  fixed winner  the tie-break winner of the training passes, held for the whole session
  link adapt.   the RAN defaults: BS link adaptation, smallest DL quota
  random        a uniformly random safe action at every step
Then a final agent is trained on all passes (the one the xApp loads) and its learning curve is plotted.
Outputs: model-final.npz, model-fold<p>.npz, eval.json, report.md, fig-rl-*.png
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import figstyle as fs  # noqa: E402

from smartfarm.contracts.service import ServiceId  # noqa: E402
from smartfarm.rl.dataset import SERVICES, Dataset  # noqa: E402
from smartfarm.rl.ddqn import DdqnAgent, DdqnConfig, train  # noqa: E402
from smartfarm.rl.env import (  # noqa: E402
    OBS_DIM, CalibrationEnv, RewardConfig, action_features, resource_cost, reward,
)

PROJECT = Path(__file__).resolve().parents[2]
RUNS = ["20261102_222932-drone-4passes", "20261103_074053-crop-4passes", "20261103_123134-weather-4passes"]
POLICIES = ["DDQN + elimination", "DDQN", "oracle", "fixed winner", "link adapt.", "random"]
COLORS = {"DDQN + elimination": "#1c5cab", "DDQN": fs.BLUE, "oracle": "#0b0b0b", "fixed winner": fs.AQUA, "link adapt.": fs.ORANGE, "random": "#b9b8b3"}
STEPS = 20
PHI = None  # action features for the Q(s, phi(a)) head; set from --action-features
REUSE = False  # --reuse-models: evaluate the saved fold models instead of retraining


def tie_break_winner(env: CalibrationEnv, service: ServiceId, passes: list[int]) -> int:
    """Fixed action with the most SLA-met passes, then lowest UL MCS, UL RBs, DL PRB (link adaptation excluded)."""

    def key(i: int) -> tuple[int, int, int, int]:
        a = env.data.actions[i]
        met = sum(env.cells[(service, i, p)].sla_met for p in passes if (service, i, p) in env.cells)
        return (-met, a.ul_mcs, a.ul_rb, a.dl_prb)

    return min((i for i in env.data.allowed[service] if not env.data.actions[i].link_adaptation), key=key)


def oracle(env: CalibrationEnv, service: ServiceId, p: int) -> int:
    scored = [(reward(service, env.cells[(service, i, p)], env.cfg), -i) for i in env.data.allowed[service]
              if (service, i, p) in env.cells]
    return -max(scored)[1]


def session(env: CalibrationEnv, service: ServiceId, p: int, choose: Any, eliminate: bool = False) -> list[dict[str, Any]]:
    """One 20-step session. With eliminate, an action that misses the SLA is blocked for the rest of the session and
    the policy falls back to its next best action (online action elimination, also used by rl_online.py)."""

    s = env.reset(service, p)
    out, done = [], False
    blocked: set[int] = set()
    while not done:
        a = choose(s, blocked) if eliminate else choose(s)
        s, r, done, cell = env.step(a)
        if eliminate and not cell.sla_met and len(blocked) < len(env.data.allowed[service]) - 1:
            blocked.add(a)
        out.append({"action": a, "reward": r, "sla_met": cell.sla_met, "cost": resource_cost(service, cell, env.cfg)})
    return out


def masked_choice(agent: DdqnAgent, mask: np.ndarray, s: np.ndarray, blocked: set[int]) -> int:
    m = mask.copy()
    m[list(blocked)] = False
    return agent.act(s, m, explore=False)


def evaluate(data: Dataset, episodes: int, cfg: DdqnConfig, rcfg: RewardConfig, out: Path,
             drift: float) -> dict[str, Any]:
    results: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    chosen: dict[str, Counter[str]] = defaultdict(Counter)
    passes = sorted(set.intersection(*(set(v) for v in data.passes.values())))
    for p in passes:
        train_passes = {s: [q for q in data.passes[s] if q != p] for s in SERVICES}
        env = CalibrationEnv(data, train_passes, rcfg, steps=STEPS, drift=drift, seed=p)
        model = out / f"model-fold{p}.npz"
        if REUSE and model.exists():
            agent = DdqnAgent.load(model, OBS_DIM, len(data.actions))
        else:
            agent = DdqnAgent(OBS_DIM, len(data.actions), DdqnConfig(**{**cfg.__dict__, "seed": p}), PHI)
            train(agent, env, episodes)
            agent.save(model, {"held_out_pass": p, "actions": [a.label() for a in data.actions]})
        test = CalibrationEnv(data, {s: [p] for s in SERVICES}, rcfg, steps=STEPS, drift=0.0, seed=100 + p)
        rng = random.Random(p)
        for s in SERVICES:
            mask = test.mask(s)
            fixed = tie_break_winner(test, s, train_passes[s])
            best = oracle(test, s, p)
            runs = {
                "DDQN + elimination": session(test, s, p, lambda o, b, m=mask: masked_choice(agent, m, o, b),
                                              eliminate=True),
                "DDQN": session(test, s, p, lambda o, m=mask: agent.act(o, m, explore=False)),
                "oracle": session(test, s, p, lambda o, b=best: b),
                "fixed winner": session(test, s, p, lambda o, f=fixed: f),
                "link adapt.": session(test, s, p, lambda o, s=s: test.default[s]),
                "random": session(test, s, p, lambda o, s=s: rng.choice(data.allowed[s])),
            }
            for name, steps in runs.items():
                results[s.value][name] += [{**x, "pass": p} for x in steps]
            chosen[s.value].update(data.actions[x["action"]].label() for x in runs["DDQN"])
            print(f"fold {p} {s.value:7s} " + "  ".join(
                f"{n}: SLA {sum(x['sla_met'] for x in r)}/{len(r)}" for n, r in runs.items()), flush=True)
    summary = {s: {n: {"sla_met_ratio": round(statistics.mean(x["sla_met"] for x in v), 3),
                       "mean_cost": round(statistics.mean(x["cost"] for x in v), 3),
                       "mean_reward": round(statistics.mean(x["reward"] for x in v), 3),
                       "steps": len(v)} for n, v in per.items()} for s, per in results.items()}
    return {"summary": summary, "ddqn_actions": {s: c.most_common() for s, c in chosen.items()}, "passes": passes}


def final_model(data: Dataset, episodes: int, cfg: DdqnConfig, rcfg: RewardConfig, out: Path,
                drift: float) -> list[dict[str, float]]:
    env = CalibrationEnv(data, data.passes, rcfg, steps=STEPS, drift=drift, seed=7)
    if REUSE and (out / "model-final.npz").exists():
        agent = DdqnAgent.load(out / "model-final.npz", OBS_DIM, len(data.actions))
        curve = json.loads((out / "learning-curve.json").read_text())
    else:
        agent = DdqnAgent(OBS_DIM, len(data.actions), cfg, PHI)
        curve = train(agent, env, episodes)
        agent.save(out / "model-final.npz", {"actions": [a.label() for a in data.actions], "passes": "all",
                                              "obs_dim": OBS_DIM})
    greedy = {}
    for s in SERVICES:
        votes: Counter[str] = Counter()
        for p in data.passes[s]:
            obs = env.reset(s, p)
            votes[data.actions[agent.act(obs, env.mask(s), explore=False)].label()] += 1
        greedy[s.value] = votes.most_common()
    (out / "final-first-actions.json").write_text(json.dumps(greedy, indent=2) + "\n")
    return curve


def figure_comparison(ev: dict[str, Any], out: Path, plt: Any, foot: list[str]) -> str:
    fig, axes = plt.subplots(1, 2, figsize=(13.0, 4.4))
    width = 0.8 / len(POLICIES)
    for ax, key, title, fmt in ((axes[0], "sla_met_ratio", "Session steps meeting the whole SLA (%)", "{:.0f}"),
                                (axes[1], "mean_cost", "Radio resource cost (0 = least, 1 = most)", "{:.2f}")):
        for k, name in enumerate(POLICIES):
            xs = [i + (k - (len(POLICIES) - 1) / 2) * width for i in range(len(SERVICES))]
            vals = [ev["summary"][s.value][name][key] * (100 if key == "sla_met_ratio" else 1) for s in SERVICES]
            ax.bar(xs, vals, width * 0.92, color=COLORS[name], label=name, zorder=2)
            for x, v in zip(xs, vals, strict=True):
                ax.text(x, v, fmt.format(v), ha="center", va="bottom", fontsize=6.5, color=fs.INK_2)
        ax.set_xticks(range(len(SERVICES)), [fs.SERVICE_NAMES[s.value] for s in SERVICES])
        ax.set_title(title)
        ax.grid(axis="y")
        ax.set_axisbelow(True)
    axes[0].set_ylim(0, 110)
    axes[1].legend(loc="upper left", bbox_to_anchor=(1.01, 1.0))
    return fs.finish(fig, out / "fig-rl-policy-comparison.png",
                     "DDQN vs reference policies on calibration passes it never saw",
                     ["Leave-one-pass-out: the agent is trained on three calibration passes and tested on 20-step "
                      "sessions (30 s per step) under the fourth pass, for every pass and service.",
                      "Oracle = best single action of the held-out pass in hindsight (upper bound). Fixed winner = "
                      "tie-break winner of the training passes. Link adapt. = RAN defaults."], foot)


def figure_curve(curve: list[dict[str, float]], out: Path, plt: Any, foot: list[str]) -> str:
    fig, axes = plt.subplots(1, 2, figsize=(13.0, 3.9))
    colors = {"weather": fs.BLUE, "crop": fs.AQUA, "drone": fs.ORANGE}
    for s in SERVICES:
        pts = [c for c in curve if c["service"] == s.value]
        for ax, key in ((axes[0], "sla_met_ratio"), (axes[1], "return")):
            ys = np.asarray([c[key] for c in pts])
            k = 50
            smooth = np.convolve(ys, np.ones(k) / k, mode="valid") if len(ys) >= k else ys
            ax.plot([c["episode"] for c in pts][k - 1:] if len(ys) >= k else [c["episode"] for c in pts],
                    smooth * (100 if key == "sla_met_ratio" else 1), color=colors[s.value], lw=1.6,
                    label=fs.SERVICE_NAMES[s.value])
    axes[0].set_title("Steps meeting the whole SLA (%, moving mean of 50 sessions)")
    axes[1].set_title(f"Session return ({STEPS} steps; 1 per step meeting the SLA minus the resource cost)")
    for ax in axes:
        ax.set_xlabel("training session (episode)")
        ax.grid(axis="y")
    axes[1].legend(loc="lower right")
    return fs.finish(fig, out / "fig-rl-learning-curve.png", "DDQN learning curve on the calibration datasets",
                     ["Final agent, trained on all passes of the three services (one network for all slices, "
                      "per-service action mask). Exploration decays from 100 % to 5 % of the steps."], foot)


def report(ev: dict[str, Any], args: argparse.Namespace, data: Dataset, figs: list[str]) -> str:
    lines = ["# Double DQN offline evaluation", "",
             f"- Calibration runs: {', '.join(Path(r).name for r in args.runs)}",
             f"- Cells: {len(data.cells)}; joint actions: {len(data.actions)}; safe actions per service: "
             + ", ".join(f"{s.value} {len(data.allowed[s])}" for s in SERVICES),
             f"- Episodes per training run: {args.episodes}; steps per session: {STEPS} (30 s each)",
             f"- Reward: {ev['config']['reward']}; DDQN: {ev['config']['ddqn']}; drift {ev['config']['drift']}", "",
             "## Leave-one-pass-out results", "",
             "| Service | Policy | Steps meeting the SLA | Mean resource cost | Mean reward |", "|---|---|---|---|---|"]
    for s in SERVICES:
        for name in POLICIES:
            x = ev["summary"][s.value][name]
            lines.append(f"| {s.value} | {name} | {x['sla_met_ratio']:.1%} | {x['mean_cost']} | {x['mean_reward']} |")
    lines += ["", "## Actions chosen by the DDQN on the held-out passes", ""]
    for s in SERVICES:
        lines.append(f"- {s.value}: " + "; ".join(f"{a} ({n})" for a, n in ev["ddqn_actions"][s.value][:6]))
    lines += ["", "## Figures", ""] + [f"![{f}]({f})" for f in figs]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--episodes", type=int, default=4000)
    parser.add_argument("--gamma", type=float, default=0.9)
    parser.add_argument("--hidden", default="128,128", help="hidden layer sizes, comma-separated")
    parser.add_argument("--batch", type=int, default=64)
    parser.add_argument("--action-features", action="store_true", help="Q(s, phi(a)) head instead of Q(s) -> |A|")
    parser.add_argument("--reuse-models", action="store_true", help="evaluate saved model-fold*/final models")
    parser.add_argument("--margin-weight", type=float, default=0.0)
    parser.add_argument("--drift", type=float, default=1.0,
                        help="per-step probability that the training channel moves to another calibration pass")
    parser.add_argument("--runs", nargs="+", default=[str(PROJECT / "experiments/calibration" / r) for r in RUNS])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    data = Dataset.load([Path(r) for r in args.runs])
    global PHI, REUSE
    PHI = action_features(data.actions) if args.action_features else None
    REUSE = args.reuse_models
    cfg = DdqnConfig(eps_decay_steps=int(args.episodes * STEPS * 0.5), gamma=args.gamma, batch=args.batch,
                     hidden=tuple(int(x) for x in args.hidden.split(",")))
    rcfg = RewardConfig(margin_weight=args.margin_weight)
    ev = evaluate(data, args.episodes, cfg, rcfg, args.output, args.drift)
    curve = final_model(data, args.episodes, cfg, rcfg, args.output, args.drift)
    ev["config"] = {"ddqn": cfg.__dict__, "reward": rcfg.__dict__, "episodes": args.episodes, "steps": STEPS,
                    "drift": args.drift, "action_features": args.action_features}
    (args.output / "eval.json").write_text(json.dumps(ev, indent=2, default=list) + "\n")
    (args.output / "learning-curve.json").write_text(json.dumps(curve) + "\n")
    fs.apply()
    import matplotlib.pyplot as plt

    foot = [f"Data: calibration runs {', '.join(Path(r).name for r in args.runs)} ({len(data.cells)} measured cells, 4 passes per service). "
            "Every outcome is a real cell measured end to end at the phone, with the radio set over E2."]
    figs = [figure_comparison(ev, args.output, plt, foot), figure_curve(curve, args.output, plt, foot)]
    (args.output / "report.md").write_text(report(ev, args, data, figs))
    print(json.dumps(ev["summary"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
