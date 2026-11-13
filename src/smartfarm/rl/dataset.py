"""Calibration cells as an RL dataset: the joint action space, the safety mask, observations and rewards.

One calibration cell = one joint action (UN3GPP UL MCS/RBs, or the BS link adaptation, plus the OAI DL PRB quota)
held for 30 s of real service traffic, with the end-to-end SLA metrics measured at the phone and the RAN telemetry
read over E2. The same features are what the online xApp sees after each decision, so a policy trained here runs
unchanged on the lab.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId, SlaMetric

SERVICES: tuple[ServiceId, ...] = (ServiceId.WEATHER, ServiceId.CROP, ServiceId.DRONE)
MAX_SAFE_UL_MCS = 14  # MCS >= 15: decoding loss on this UN3GPP link (drone UDP loss 28 %, crop TCP collapse)
MAX_REQUIREMENTS = 5  # crop has the most SLA requirements
LA = -1  # ul_mcs / ul_rb of the BS link adaptation (UL override released)


@dataclass(frozen=True, order=True)
class Action:
    """Joint radio action. ul_mcs = ul_rb = LA means the BS link adaptation (no UL override)."""

    ul_mcs: int
    ul_rb: int
    dl_prb: int

    @property
    def link_adaptation(self) -> bool:
        return self.ul_mcs == LA

    def label(self) -> str:
        ul = "UL link adaptation" if self.link_adaptation else f"UL MCS {self.ul_mcs} / {self.ul_rb} RB"
        return f"{ul} + DL {self.dl_prb} PRB"


@dataclass
class Cell:
    service: ServiceId
    action: Action
    pass_no: int
    sla_met: bool
    margins: tuple[float, ...]  # per SLA requirement, catalog order; > 0 meets the target
    telemetry: dict[str, float]  # RAN observations of the cell (E2 readback + KPM)
    raw: dict[str, Any] = field(repr=False)


def margin(metric: SlaMetric, value: float | None) -> float:
    """Signed, normalized distance to the target, clipped to [-1, 1]; a missing value counts as a full miss."""

    if value is None:
        return -1.0
    t = metric.threshold
    if metric.operator == "eq":
        return 1.0 if value == t else -1.0
    scale = abs(t) if t else 1.0
    m = (value - t) / scale if metric.operator == "ge" else (t - value) / scale
    return max(-1.0, min(1.0, m))


def action_of(row: dict[str, Any]) -> Action:
    if row["ul_mode"] != "override":
        return Action(LA, LA, int(row["dl_prb"]))
    return Action(int(row["ul_mcs"]), int(row["ul_rb"]), int(row["dl_prb"]))


def load_run(run: Path) -> tuple[list[Cell], set[Action]]:
    """Valid measured cells of one calibration run and the actions the BS safety mask rejected."""

    invalid: set[int] = set()
    marker = run / "invalidated.json"
    if marker.exists():
        invalid = set(json.loads(marker.read_text())["indices"])
    cells: list[Cell] = []
    rejected: set[Action] = set()
    for line in (run / "cells.jsonl").read_text().splitlines():
        row = json.loads(line)
        if "error" in row or row["index"] in invalid:
            continue
        if row.get("skipped"):
            rejected.add(action_of(row))
            continue
        cells.append(cell_from_row(row))
    return cells, rejected


def cell_from_row(row: dict[str, Any]) -> Cell:
    """One measured cell record (calibration sweep or online agent step) as an RL transition outcome."""

    service = ServiceId(row["service"])
    return Cell(
        service=service, action=action_of(row), pass_no=int(row.get("pass", 0)),
        sla_met=row.get("sla_met") is True,  # undetermined (null) counts as not met
        margins=tuple(margin(m, row.get(f"svc_{m.name}")) for m in SERVICE_CATALOG[service].sla),
        telemetry={k: float(row[k]) for k in TELEMETRY if row.get(k) is not None}, raw=row)


# RAN telemetry in the state, with its normalization. The UN3GPP UL SNR is left out on purpose: the calibration showed
# it moves with the commanded MCS and RBs, so it is not a channel measurement (docs/results/2026-11-03-*.md).
TELEMETRY: dict[str, float] = {
    "kpm_ue_thp_dl_mbps": 50.0,
    "kpm_prb_tot_dl_pct": 100.0,
    "kpm_rlc_delay_dl_ms": 100.0,
    "kpm_ss_sinr_db": 30.0,
    "un3gpp_mac_mbps": 50.0,
    "un3gpp_ul_rb_observed_max": 132.0,
}


def safe(action: Action, rejected: set[Action]) -> bool:
    return action not in rejected and (action.link_adaptation or action.ul_mcs <= MAX_SAFE_UL_MCS)


@dataclass
class Dataset:
    cells: list[Cell]
    actions: list[Action]  # global action index, shared by every service
    allowed: dict[ServiceId, list[int]]  # safe, measured actions of each service (indices into actions)
    passes: dict[ServiceId, list[int]]

    @classmethod
    def load(cls, runs: list[Path]) -> Dataset:
        cells: list[Cell] = []
        rejected: dict[ServiceId, set[Action]] = {s: set() for s in SERVICES}
        for run in runs:
            got, rej = load_run(run)
            cells += got
            if got:
                rejected[got[0].service] |= rej
        actions = sorted({c.action for c in cells})
        index = {a: i for i, a in enumerate(actions)}
        allowed = {s: sorted({index[c.action] for c in cells if c.service == s and safe(c.action, rejected[s])})
                   for s in SERVICES}
        passes = {s: sorted({c.pass_no for c in cells if c.service == s}) for s in SERVICES}
        return cls(cells, actions, allowed, passes)

    def index(self) -> dict[tuple[ServiceId, int, int], Cell]:
        """(service, action index, pass) -> cell."""

        pos = {a: i for i, a in enumerate(self.actions)}
        return {(c.service, pos[c.action], c.pass_no): c for c in self.cells}
