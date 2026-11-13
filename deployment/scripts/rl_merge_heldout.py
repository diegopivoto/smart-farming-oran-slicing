#!/usr/bin/env python3
"""Replaces one service's offline results with its held-out-day evaluation and redraws the offline figures.

For each training dir (the final model and the ablation variants), the service's entry of eval.json is replaced by the
rl_heldout_day.py results of that same, unchanged model (DDQN, DDQN + elimination) and of the reference policies on the
held-out day; the other services keep their leave-one-pass-out results. The merged eval.json files go to
<output>/<training dir>/, followed by the policy comparison of the final model and the ablation figure.

    rl_merge_heldout.py --heldout experiments/rl/<heldout>/eval.json --final experiments/rl/<final> \
        --variants experiments/rl/ablation-final-*/<variant> ... --data-note "..." --output docs/results/figures/<dir>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import figstyle as fs  # noqa: E402
import rl_ablation  # noqa: E402
import rl_offline  # noqa: E402

REFERENCES = ["oracle", "fixed winner", "link adapt.", "random"]


def merged(run: Path, held: dict, service: str) -> dict:
    ev = json.loads((run / "eval.json").read_text())
    summary = held["summary"]
    entry = {p: {k: v for k, v in summary[f"{run.name} | {p}"].items() if k != "actions"}
             for p in ("DDQN", "DDQN + elimination")}
    entry |= {r: {k: v for k, v in summary[r].items() if k != "actions"} for r in REFERENCES}
    ev["summary"][service] = entry
    ev.setdefault("heldout_day", {})[service] = held["heldout"]
    return ev


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--heldout", type=Path, required=True, help="eval.json of rl_heldout_day.py")
    parser.add_argument("--final", type=Path, required=True)
    parser.add_argument("--variants", nargs="+", type=Path, required=True)
    parser.add_argument("--data-note", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    held = json.loads(args.heldout.read_text())
    service = held["service"]
    dirs = []
    for run in [*args.variants, args.final]:
        out = args.output / run.name
        out.mkdir(parents=True, exist_ok=True)
        (out / "eval.json").write_text(json.dumps(merged(run, held, service), indent=2) + "\n")
        dirs.append(out)
    fs.apply()
    import matplotlib.pyplot as plt

    foot = [f"Data: {args.data_note}. Every outcome is a real cell measured end to end at the phone, with the radio "
            f"set over E2. {fs.SERVICE_NAMES[service]}: the trained models, unchanged, on a calibration of another day."]
    ev = json.loads((args.output / args.final.name / "eval.json").read_text())
    print(rl_offline.figure_comparison(ev, args.output, plt, foot))
    rows, refs = rl_ablation.collect(dirs)
    print(rl_ablation.figure(rows, refs, args.output, args.data_note))
    (args.output / "rl-ablation.json").write_text(json.dumps({"variants": rows, "references": refs}, indent=2) + "\n")
    (args.output / "eval.json").write_text(json.dumps(ev, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
