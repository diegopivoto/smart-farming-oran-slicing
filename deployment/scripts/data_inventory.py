#!/usr/bin/env python3
"""Inventory of the data behind the paper: duration and volume of every calibration, RL training and online session.

Usage: data_inventory.py [--output docs/results/data-inventory-20261105]
Writes <output>.json and <output>.md. Per calibration: wall-clock window, cells (valid / rejected by the BS mask),
traffic time, bytes sent by the generator (collector generator_summary), 1 s receiver windows, KPM samples, UN3GPP E2
readbacks and the raw journals (lines, MiB). Per online session: steps per policy, window, the same volumes. Per RL
training: folds, simulated steps, wall time.
"""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[2]
COLLECTED = PROJECT / "experiments/traffic/collected"
CALIBRATIONS = {
    "weather (training day, 2026-11-03)": "experiments/calibration/20261103_123134-weather-4passes",
    "weather (independent day, 2026-11-05)": "experiments/calibration/20261105_082951-weather-final-4passes",
    "crop": "experiments/calibration/20261104_121141-crop-final-4passes",
    "drone": "experiments/calibration/20261104_071216-drone-final-4passes",
}
ONLINE = {
    "weather": "experiments/rl-online/20261105_102922-weather-final-alternate-majority",
    "crop": "experiments/rl-online/20261104_171528-crop-final-alternate-majority",
    "drone": "experiments/rl-online/20261104_112556-drone-final-alternate-majority",
    "drone (phone moved, online adaptation)": "experiments/rl-online/20261105-drone-mobility-learn-v4",
    "drone (phone moved, frozen agent)": "experiments/rl-online/20261105-drone-mobility-frozen",
}
RL_OFFLINE = "experiments/rl/20261104_170128-ddqn-phi-g0.5-m0.0-d1.0-final-crop"


def jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def local(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone()


def sent_bytes(run_id: str | None) -> int:
    if not run_id or not (f := COLLECTED / f"{run_id}.jsonl").exists():
        return 0
    for rec in jsonl(f):
        if rec.get("kind") == "generator_summary":
            return int(rec.get("sent_bytes") or rec.get("tcp_bytes_sent") or 0)
    return 0


def received_windows(cell: dict[str, Any]) -> int:
    """1 s receiver windows with packets in the collector file of the cell; in a long DL queue some reports reach the
    collector after the cell was evaluated, so this can exceed the cell's svc_windows."""
    f = COLLECTED / f"{cell.get('traffic_run_id')}.jsonl"
    if not cell.get("traffic_run_id") or not f.exists():
        return cell.get("svc_windows") or 0
    return sum(1 for r in jsonl(f) if r.get("kind") == "receiver_window" and r.get("received"))


def volumes(cells: list[dict[str, Any]]) -> dict[str, Any]:
    with_traffic = [c for c in cells if c.get("traffic_run_id")]
    return {
        "traffic_s": 30 * len(with_traffic),
        "generator_bytes": sum(sent_bytes(c.get("traffic_run_id")) for c in with_traffic),
        "receiver_windows": sum(received_windows(c) for c in cells),
        "objects_received": sum(c.get("svc_objects_received") or 0 for c in cells),
        "kpm_samples": sum(c.get("kpm_samples") or 0 for c in cells),
        "un3gpp_readbacks": sum(c.get("un3gpp_samples") or 0 for c in cells),
    }


def raw_files(run: Path) -> dict[str, Any]:
    out = {}
    for f in sorted((run / "raw").glob("*.gz")):
        with gzip.open(f, "rt", encoding="utf-8", errors="replace") as fh:
            lines = sum(1 for _ in fh)
        out[f.name] = {"lines": lines, "mib": round(f.stat().st_size / 2**20, 1)}
    return out


def window(starts: list[str], ends: list[str]) -> dict[str, Any]:
    a, b = local(min(starts)), local(max(ends))
    return {"start": a.strftime("%Y-%m-%d %H:%M"), "end": b.strftime("%H:%M"),
            "duration_min": round((b - a).total_seconds() / 60)}


def calibration(service: str, rel: str) -> dict[str, Any]:
    run = PROJECT / rel
    cells = jsonl(run / "cells.jsonl")
    meta = json.loads((run / "run-metadata.json").read_text())
    inv_file = run / "invalidated.json"
    invalid = set(json.loads(inv_file.read_text())["indices"]) if inv_file.exists() else set()
    valid = [c for c in cells if not c.get("error") and c.get("index") not in invalid]
    evidence = json.loads((run / "analysis/oran-evidence.json").read_text())
    rejected = [c for c in valid if str(c.get("ul_status", "")).startswith("REJECTED")]
    measured = [c for c in valid if c not in rejected]
    passes = meta.get("passes") or 4
    return {"service": service, "run": rel, "passes": passes,
            "cells_per_pass": len(valid) // passes, "cell_traffic_s": meta.get("cell_s", 30),
            "cells_recorded": len(cells), "cells_invalidated_and_remeasured": len(invalid),
            "cells_measured": len(measured), "cells_rejected_by_bs_mask": len(rejected),
            "e2_un3gpp_readbacks_raw": evidence.get("e2_indications_un3gpp_readback"),
            "kpm_samples_service_slice_raw": evidence.get("kpm_samples_service_slice"),
            "e2_control_outcomes": evidence.get("e2_control_outcomes_by_node_status"),
            **window([c["started_at"] for c in cells], [c.get("finished_at") or c["started_at"] for c in cells]),
            **volumes(measured), "raw": raw_files(run)}


def online(name: str, rel: str) -> dict[str, Any]:
    run = PROJECT / rel
    steps = jsonl(run / "steps.jsonl")
    by: dict[str, int] = {}
    for s in steps:
        by[s["policy"]] = by.get(s["policy"], 0) + 1
    cells = [s["cell"] for s in steps if isinstance(s.get("cell"), dict)]
    return {"session": name, "run": rel, "steps": len(steps), "steps_by_policy": by,
            **window([s["decided_at"] for s in steps], [c.get("finished_at") or c["started_at"] for c in cells]),
            **volumes(cells)}


def offline(rel: str) -> dict[str, Any]:
    run = PROJECT / rel
    report = (run / "report.md").read_text(encoding="utf-8")
    start = datetime.strptime(run.name[:15], "%Y%m%d_%H%M%S")
    end = datetime.fromtimestamp((run / "model-final.npz").stat().st_mtime)
    folds = sorted(p.name for p in run.glob("model-fold*.npz"))
    return {"run": rel, "folds": len(folds), "episodes_per_model": 3000, "steps_per_episode": 20,
            "simulated_steps_total": 3000 * 20 * (len(folds) + 1),
            "wall_time_min": round((end - start).total_seconds() / 60),
            "report_excerpt": report.splitlines()[:6]}


def recv(x: dict[str, Any]) -> str:
    return f"{x['objects_received']:,} objetos" if x["objects_received"] else f"{x['receiver_windows']:,} janelas"


def markdown(inv: dict[str, Any]) -> str:
    gb = lambda b: f"{b / 1e9:.1f}"  # noqa: E731
    lines = ["# Inventário de dados (2026-11-05)", "",
             "## Calibrações", "",
             "| Serviço | Janela (local) | Duração | Passadas × ações | Combinações medidas (recusadas) | Tráfego | "
             "Dados enviados (GB) | Janelas de 1 s / objetos | Amostras KPM do slice | Leituras E2 da BS |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for c in inv["calibrations"]:
        lines.append(f"| {c['service']} | {c['start']}–{c['end']} | {c['duration_min'] // 60} h {c['duration_min'] % 60:02d} min | "
                     f"{c['passes']} × {c['cells_per_pass']} | {c['cells_measured']} ({c['cells_rejected_by_bs_mask']}) | "
                     f"{c['traffic_s'] / 3600:.1f} h | {gb(c['generator_bytes'])} | {recv(c)} | "
                     f"{c['kpm_samples_service_slice_raw']:,} | {c['e2_un3gpp_readbacks_raw']:,} |")
    lines += ["", "Arquivos brutos por calibração:", ""]
    for c in inv["calibrations"]:
        lines.append(f"- {c['service']}: " + "; ".join(f"`{k}` {v['lines']:,} linhas, {v['mib']} MiB" for k, v in c["raw"].items()))
    lines += ["", "## Sessões online (A/B no laboratório)", "",
              "| Sessão | Janela (local) | Duração | Passos (por política) | Tráfego | Dados enviados (GB) | Janelas de 1 s / objetos | Amostras KPM (nas combinações) | Leituras E2 da BS (nas combinações) |",
              "|---|---|---|---|---|---|---|---|---|"]
    for s in inv["online"]:
        pol = ", ".join(f"{k} {v}" for k, v in s["steps_by_policy"].items())
        lines.append(f"| {s['session']} | {s['start']}–{s['end']} | {s['duration_min']} min | {s['steps']} ({pol}) | "
                     f"{s['traffic_s'] / 60:.0f} min | {gb(s['generator_bytes'])} | {recv(s)} | "
                     f"{s['kpm_samples']:,} | {s['un3gpp_readbacks']:,} |")
    o = inv["rl_offline"]
    lines += ["", "## Treino offline do DDQN", "",
              f"- Execução: `{o['run']}`; {o['folds']} modelos de validação (deixando uma passada de fora) + 1 modelo final;",
              f"- {o['episodes_per_model']} episódios × {o['steps_per_episode']} passos por modelo = "
              f"{o['simulated_steps_total']:,} passos simulados no total; tempo de parede ≈ {o['wall_time_min']} min.",
              "", "## Totais", "",
              f"- Combinações de calibração medidas: {inv['totals']['calibration_cells']:,} "
              f"({inv['totals']['calibration_traffic_h']:.1f} h de tráfego real);",
              f"- Passos online: {inv['totals']['online_steps']:,} ({inv['totals']['online_traffic_h']:.1f} h de tráfego real);",
              f"- Dados enviados pelos geradores: {inv['totals']['generator_gb']:.1f} GB;",
              f"- Medido no celular: {inv['totals']['receiver_windows']:,} janelas de 1 s (weather e drone) e "
              f"{inv['totals']['objects_received']:,} imagens de 25 MiB (crop);",
              f"- Telemetria E2 nas calibrações: {inv['totals']['e2_readbacks_calibration']:,} leituras da BS UN3GPP e "
              f"{inv['totals']['kpm_calibration']:,} amostras KPM do slice; linhas brutas guardadas (KPM, xApp, RIC): "
              f"{inv['totals']['raw_lines']:,}."]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=PROJECT / "docs/results/data-inventory-20261105")
    args = parser.parse_args()
    cal = [calibration(s, r) for s, r in CALIBRATIONS.items()]
    onl = [online(n, r) for n, r in ONLINE.items()]
    inv = {"calibrations": cal, "online": onl, "rl_offline": offline(RL_OFFLINE)}
    inv["totals"] = {
        "calibration_cells": sum(c["cells_measured"] for c in cal),
        "calibration_traffic_h": sum(c["traffic_s"] for c in cal) / 3600,
        "online_steps": sum(s["steps"] for s in onl), "online_traffic_h": sum(s["traffic_s"] for s in onl) / 3600,
        "generator_gb": sum(x["generator_bytes"] for x in cal + onl) / 1e9,
        **{k: sum(x[k] for x in cal + onl) for k in ("receiver_windows", "objects_received", "kpm_samples",
                                                       "un3gpp_readbacks")},
        "e2_readbacks_calibration": sum(c["e2_un3gpp_readbacks_raw"] for c in cal),
        "kpm_calibration": sum(c["kpm_samples_service_slice_raw"] for c in cal),
        "raw_lines": sum(v["lines"] for c in cal for v in c["raw"].values()),
    }
    args.output.with_suffix(".json").write_text(json.dumps(inv, indent=2) + "\n", encoding="utf-8")
    args.output.with_suffix(".md").write_text(markdown(inv), encoding="utf-8")
    print(markdown(inv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
