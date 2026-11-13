"""Double DQN with action masking, in numpy (PC1 has no GPU and the network is small).

Online network chooses the next action, target network evaluates it (van Hasselt et al., 2016):
    y = r + gamma * Q_target(s', argmax_{a' allowed} Q_online(s', a'))
Masked actions are never chosen, explored or bootstrapped from.
"""

from __future__ import annotations

import ast
import contextlib
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

import numpy as np

NEG = -1e9


@dataclass(frozen=True)
class DdqnConfig:
    hidden: tuple[int, ...] = (128, 128)
    lr: float = 1e-3
    gamma: float = 0.9
    batch: int = 64
    replay: int = 50_000
    warmup: int = 1_000
    target_sync: int = 500  # gradient steps between hard target updates
    eps_start: float = 1.0
    eps_end: float = 0.05
    eps_decay_steps: int = 20_000
    huber_delta: float = 1.0
    seed: int = 0


class Mlp:
    """ReLU MLP with an Adam optimizer; parameters are plain arrays so they save to and load from .npz."""

    def __init__(self, sizes: list[int], rng: np.random.Generator) -> None:
        self.w = [rng.normal(0, np.sqrt(2 / a), (a, b)) for a, b in zip(sizes[:-1], sizes[1:], strict=True)]
        self.b = [np.zeros(b) for b in sizes[1:]]
        self.m = [np.zeros_like(p) for p in self.w + self.b]
        self.v = [np.zeros_like(p) for p in self.w + self.b]
        self.k = 0

    def forward(self, x: np.ndarray) -> tuple[np.ndarray, list[np.ndarray]]:
        acts = [x]
        for i, (w, b) in enumerate(zip(self.w, self.b, strict=True)):
            x = x @ w + b
            if i < len(self.w) - 1:
                x = np.maximum(x, 0.0)
            acts.append(x)
        return x, acts

    def __call__(self, x: np.ndarray) -> np.ndarray:
        return self.forward(x)[0]

    def backward(self, acts: list[np.ndarray], grad: np.ndarray, lr: float) -> None:
        gw, gb = [], []
        for i in reversed(range(len(self.w))):
            gw.append(acts[i].T @ grad)
            gb.append(grad.sum(axis=0))
            if i:
                grad = (grad @ self.w[i].T) * (acts[i] > 0)
        grads = list(reversed(gw)) + list(reversed(gb))
        params = self.w + self.b
        self.k += 1
        b1, b2 = 0.9, 0.999
        for j, (p, g) in enumerate(zip(params, grads, strict=True)):
            self.m[j] = b1 * self.m[j] + (1 - b1) * g
            self.v[j] = b2 * self.v[j] + (1 - b2) * g * g
            mh, vh = self.m[j] / (1 - b1 ** self.k), self.v[j] / (1 - b2 ** self.k)
            p -= lr * mh / (np.sqrt(vh) + 1e-8)

    def copy_from(self, other: Mlp) -> None:
        self.w = [w.copy() for w in other.w]
        self.b = [b.copy() for b in other.b]


class Replay:
    def __init__(self, capacity: int, obs_dim: int, n_actions: int) -> None:
        self.s = np.zeros((capacity, obs_dim))
        self.a = np.zeros(capacity, dtype=np.int64)
        self.r = np.zeros(capacity)
        self.s2 = np.zeros((capacity, obs_dim))
        self.done = np.zeros(capacity)
        self.mask2 = np.zeros((capacity, n_actions), dtype=bool)
        self.n = self.i = 0
        self.capacity = capacity

    def add(self, s: np.ndarray, a: int, r: float, s2: np.ndarray, done: bool, mask2: np.ndarray) -> None:
        self.s[self.i], self.a[self.i], self.r[self.i], self.s2[self.i] = s, a, r, s2
        self.done[self.i], self.mask2[self.i] = float(done), mask2
        self.i = (self.i + 1) % self.capacity
        self.n = min(self.n + 1, self.capacity)

    def sample(self, rng: np.random.Generator, k: int) -> tuple[np.ndarray, ...]:
        j = rng.integers(0, self.n, k)
        return self.s[j], self.a[j], self.r[j], self.s2[j], self.done[j], self.mask2[j]


class DdqnAgent:
    """Two heads. Without action features: Q(s) -> one value per action (classic DQN). With action features
    phi(a) (n_actions x F): Q(s, phi(a)) -> one value, so neighbouring actions (MCS, RBs, PRB quota) share what is
    learned; with only a few measured outcomes per action this is what lets the agent generalize."""

    def __init__(self, obs_dim: int, n_actions: int, cfg: DdqnConfig | None = None,
                 action_features: np.ndarray | None = None) -> None:
        self.cfg = cfg or DdqnConfig()
        self.rng = np.random.default_rng(self.cfg.seed)
        self.phi = None if action_features is None else np.asarray(action_features, dtype=np.float64)
        if self.phi is not None:
            sizes = [obs_dim + self.phi.shape[1], *self.cfg.hidden, 1]
        else:
            sizes = [obs_dim, *self.cfg.hidden, n_actions]
        self.online, self.target = Mlp(sizes, self.rng), Mlp(sizes, self.rng)
        self.target.copy_from(self.online)
        self.replay = Replay(self.cfg.replay, obs_dim, n_actions)
        self.obs_dim, self.n_actions = obs_dim, n_actions
        self.env_steps = self.grad_steps = 0

    def epsilon(self) -> float:
        frac = min(1.0, self.env_steps / self.cfg.eps_decay_steps)
        return self.cfg.eps_start + frac * (self.cfg.eps_end - self.cfg.eps_start)

    def _all(self, net: Mlp, states: np.ndarray) -> np.ndarray:
        """Q of every action for each state: (batch, n_actions)."""

        if self.phi is None:
            return net(states)
        b, n = len(states), len(self.phi)
        x = np.concatenate([np.repeat(states, n, axis=0), np.tile(self.phi, (b, 1))], axis=1)
        return net(x).reshape(b, n)

    def _pairs(self, states: np.ndarray, actions: np.ndarray) -> np.ndarray:
        assert self.phi is not None
        return np.concatenate([states, self.phi[actions]], axis=1)

    def q(self, s: np.ndarray, mask: np.ndarray) -> np.ndarray:
        return np.where(mask, self._all(self.online, s[None])[0], NEG)

    def act(self, s: np.ndarray, mask: np.ndarray, explore: bool = True) -> int:
        if explore and self.rng.random() < self.epsilon():
            return int(self.rng.choice(np.flatnonzero(mask)))
        return int(np.argmax(self.q(s, mask)))

    def learn(self) -> float | None:
        cfg = self.cfg
        if self.replay.n < max(cfg.warmup, cfg.batch):
            return None
        s, a, r, s2, done, mask2 = self.replay.sample(self.rng, cfg.batch)
        rows = np.arange(cfg.batch)
        y = r
        if cfg.gamma:
            best = np.argmax(np.where(mask2, self._all(self.online, s2), NEG), axis=1)  # online net selects ...
            q_next = (self._all(self.target, s2)[rows, best] if self.phi is None  # ... target net evaluates
                      else self.target(self._pairs(s2, best))[:, 0])
            y = r + cfg.gamma * (1.0 - done) * q_next
        if self.phi is None:
            q, acts = self.online.forward(s)
            err = q[rows, a] - y
            grad_q = np.zeros_like(q)
            grad_q[rows, a] = np.clip(err, -cfg.huber_delta, cfg.huber_delta) / cfg.batch
        else:
            q, acts = self.online.forward(self._pairs(s, a))
            err = q[:, 0] - y
            grad_q = (np.clip(err, -cfg.huber_delta, cfg.huber_delta) / cfg.batch)[:, None]
        self.online.backward(acts, grad_q, cfg.lr)
        self.grad_steps += 1
        if self.grad_steps % cfg.target_sync == 0:
            self.target.copy_from(self.online)
        e, d = np.abs(err), cfg.huber_delta
        return float(np.mean(np.where(e <= d, 0.5 * e * e, d * (e - 0.5 * d))))

    def save(self, path: Path, meta: dict[str, Any]) -> None:
        arrays = {f"w{i}": w for i, w in enumerate(self.online.w)} | {f"b{i}": b for i, b in enumerate(self.online.b)}
        if self.phi is not None:
            arrays["phi"] = self.phi
        arrays["meta"] = np.asarray(repr({"config": asdict(self.cfg)} | meta))
        np.savez(path, **arrays)  # type: ignore[arg-type]

    @classmethod
    def load(cls, path: Path, obs_dim: int, n_actions: int) -> DdqnAgent:
        data = np.load(path, allow_pickle=False)
        layers = sum(1 for k in data.files if k.startswith("w"))
        hidden = tuple(int(data[f"w{i}"].shape[1]) for i in range(layers - 1))
        saved: dict[str, Any] = {}
        if "meta" in data.files:  # the training config (gamma, batch, lr...) so online updates use the same one
            with contextlib.suppress(ValueError, SyntaxError):
                saved = ast.literal_eval(str(data["meta"])).get("config", {})
        known = {f.name for f in fields(DdqnConfig)}
        cfg = DdqnConfig(**{k: (tuple(v) if k == "hidden" else v) for k, v in saved.items() if k in known} | {"hidden": hidden})
        agent = cls(obs_dim, n_actions, cfg, data["phi"] if "phi" in data.files else None)
        agent.online.w = [data[f"w{i}"] for i in range(layers)]
        agent.online.b = [data[f"b{i}"] for i in range(layers)]
        agent.target.copy_from(agent.online)
        return agent


def train(agent: DdqnAgent, env: Any, episodes: int,
          on_episode: Callable[[int, float, float], None] | None = None) -> list[dict[str, float]]:
    """Run episodes of `env` (CalibrationEnv API); one gradient step per environment step."""

    curve = []
    for ep in range(episodes):
        s = env.reset()
        mask = env.mask()
        total, met, done = 0.0, 0, False
        while not done:
            a = agent.act(s, mask)
            s2, r, done, cell = env.step(a)
            # sessions end on a time limit the state does not encode: a truncation, not a terminal state, so the
            # target keeps bootstrapping from s2 (Pardo et al., 2018)
            agent.replay.add(s, a, r, s2, False, mask)
            agent.env_steps += 1
            agent.learn()
            total += r
            met += cell.sla_met
            s = s2
        curve.append({"episode": ep, "service": env.service.value, "return": total, "sla_met_ratio": met / env.t,
                      "epsilon": agent.epsilon()})
        if on_episode:
            on_episode(ep, total, met / env.t)
    return curve
