import numpy as np
import pytest

from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId, SlaMetric
from smartfarm.rl.dataset import LA, Action, Cell, Dataset, margin, safe
from smartfarm.rl.ddqn import DdqnAgent, DdqnConfig
from smartfarm.rl.env import OBS_DIM, CalibrationEnv, RewardConfig, observe, resource_cost, reward


def metric(op: str, t: float) -> SlaMetric:
    return SlaMetric(name="x", operator=op, threshold=t, unit="u", window_s=1)  # type: ignore[arg-type]


def test_margin_sign_scale_and_missing() -> None:
    assert margin(metric("ge", 38e6), 41.8e6) == pytest.approx(0.1)
    assert margin(metric("le", 0.05), 0.10) == pytest.approx(-1.0)
    assert margin(metric("le", 2.0), 0.0) == pytest.approx(1.0)
    assert margin(metric("eq", 0), 0) == 1.0 and margin(metric("eq", 1.0), 0.98) == -1.0
    assert margin(metric("ge", 1.0), None) == -1.0


def test_safety_mask_excludes_rejected_and_decoding_loss_mcs() -> None:
    rejected = {Action(1, 33, 24)}
    assert not safe(Action(1, 33, 24), rejected)
    assert not safe(Action(15, 99, 24), set())
    assert safe(Action(14, 99, 24), set()) and safe(Action(LA, LA, 24), set())


def cell(service: ServiceId, action: Action, p: int, met: bool) -> Cell:
    n = len(SERVICE_CATALOG[service].sla)
    return Cell(service, action, p, met, (0.5 if met else -0.5,) * n, {"kpm_ss_sinr_db": 23.7}, {})


def toy() -> Dataset:
    """Weather only: the cheap action meets the SLA in pass 1 only, the expensive one always."""

    la, cheap, dear = Action(LA, LA, 24), Action(2, 66, 24), Action(10, 66, 52)
    cells = [cell(ServiceId.WEATHER, la, p, False) for p in (1, 2)]
    cells += [cell(ServiceId.WEATHER, cheap, 1, True), cell(ServiceId.WEATHER, cheap, 2, False)]
    cells += [cell(ServiceId.WEATHER, dear, p, True) for p in (1, 2)]
    actions = sorted({c.action for c in cells})
    allowed = {s: [] for s in ServiceId} | {ServiceId.WEATHER: list(range(len(actions)))}
    return Dataset(cells, actions, allowed, {s: [] for s in ServiceId} | {ServiceId.WEATHER: [1, 2]})


def test_reward_prefers_cheaper_action_at_equal_sla() -> None:
    cfg = RewardConfig()
    cheap = cell(ServiceId.WEATHER, Action(2, 66, 24), 1, True)
    dear = cell(ServiceId.WEATHER, Action(10, 66, 52), 1, True)
    assert resource_cost(ServiceId.WEATHER, cheap, cfg) < resource_cost(ServiceId.WEATHER, dear, cfg)
    assert reward(ServiceId.WEATHER, cheap, cfg) > reward(ServiceId.WEATHER, dear, cfg) > 0
    assert observe(ServiceId.WEATHER, cheap).shape == (OBS_DIM,)


def test_env_refuses_masked_action() -> None:
    data = toy()
    env = CalibrationEnv(data, data.passes, steps=3)
    env.reset(ServiceId.WEATHER, 1)
    data.allowed[ServiceId.WEATHER] = [0]
    with pytest.raises(ValueError):
        env.step(1)


def test_ddqn_learns_the_context_dependent_action() -> None:
    data = toy()
    env = CalibrationEnv(data, data.passes, steps=5, drift=0.0, seed=3)
    agent = DdqnAgent(OBS_DIM, len(data.actions), DdqnConfig(hidden=(32,), warmup=64, batch=32,
                                                              eps_decay_steps=1500, target_sync=100, seed=1))
    from smartfarm.rl.ddqn import train

    train(agent, env, 600)
    cheap, dear = data.actions.index(Action(2, 66, 24)), data.actions.index(Action(10, 66, 52))
    mask = env.mask(ServiceId.WEATHER)
    # after observing the cheap action succeed (pass 1) keep it; after it fails (pass 2) pay for the safe one
    assert agent.act(observe(ServiceId.WEATHER, data.cells[2]), mask, explore=False) == cheap
    assert agent.act(observe(ServiceId.WEATHER, data.cells[3]), mask, explore=False) == dear


def test_save_and_load_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    agent = DdqnAgent(OBS_DIM, 4, DdqnConfig(hidden=(8,)))
    agent.save(tmp_path / "m.npz", {"actions": ["a", "b", "c", "d"]})
    other = DdqnAgent.load(tmp_path / "m.npz", OBS_DIM, 4)
    x = np.ones(OBS_DIM)
    assert np.allclose(agent.online(x[None]), other.online(x[None]))
