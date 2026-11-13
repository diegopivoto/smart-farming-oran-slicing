#!/usr/bin/env python3
"""Operator view of what the O-RAN side receives: UN3GPP UL readback, OAI E2SM-SF readback and OAI KPM.

Follows the smartfarm_sf_control indication JSONL (both RANs) and the KPM bridge journal, and prints one
compact block every --period seconds. Read-only: it never talks to the RIC, the xApp or the RANs.
End-to-end service metrics (PDR, AoI, goodput, jitter, completion time) come from the traffic generators
and are not part of this stream yet.
"""

import argparse
import json
import subprocess
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

RSRP_DBM = staticmethod(lambda level: level - 157 + 0.5)  # TS 38.133 SS-RSRP level n
SINR_DB = staticmethod(lambda level: level / 2 - 23 + 0.25)  # TS 38.133 SS-SINR level n
DISTRIBUTIONS = {"L1M.SS-RSRP": RSRP_DBM.__func__, "MR.NRScSSSINR": SINR_DB.__func__}

state: dict[str, Any] = {"un3gpp": {}, "un3gpp_outcome": None, "oai_ue": {}, "oai_outcome": None,
                         "oai_counters": None, "kpm_ue": {}, "kpm_dist": {}, "kpm_mcs": None,
                         "xapp_at": None, "kpm_at": None}
lock = threading.Lock()


def fields(label: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in label.split(";") if "=" in part)


def follow(path: Path):
    while not path.exists():
        time.sleep(1)
    handle = path.open(encoding="utf-8")
    handle.seek(0, 2)
    while True:
        line = handle.readline()
        if line:
            yield line
            continue
        if path.stat().st_size < handle.tell():  # the xApp was restarted and truncated its output
            handle.close()
            handle = path.open(encoding="utf-8")
        time.sleep(0.2)


def xapp_reader(path: Path) -> None:
    for line in follow(path):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "indication":
            continue
        with lock:
            state["xapp_at"] = time.monotonic()
            for label in event.get("labels", []):
                f = fields(label)
                kind = f.get("kind")
                if kind == "un3gpp_ue":
                    state["un3gpp"][f.get("ue")] = f
                elif kind == "un3gpp_outcome":
                    state["un3gpp_outcome"] = f
                elif kind == "ue":
                    state["oai_ue"][f.get("rnti")] = f
                elif kind == "outcome":
                    state["oai_outcome"] = f
                elif kind == "counters":
                    state["oai_counters"] = f


def kpm_reader() -> None:
    proc = subprocess.Popen(("journalctl", "-u", "smartfarm-kpm-bridge", "-f", "-n", "0", "-o", "cat"),  # noqa: S603
                            stdout=subprocess.PIPE, text=True)
    pending: dict[str, tuple[int, dict[int, float]]] = {}
    assert proc.stdout is not None
    for line in proc.stdout:
        if not line.startswith("{"):
            continue
        try:
            sample = json.loads(line)
        except json.JSONDecodeError:
            continue
        at = sample.get("collected_at_ns", 0)
        with lock:
            state["kpm_at"] = time.monotonic()
            mcs: dict[int, float] = defaultdict(float)
            for m in sample.get("measurements", []):
                name, value, labels = m.get("name"), m.get("value"), m.get("labels", {})
                if name in DISTRIBUTIONS and labels.get("dist_bin_x") is not None:
                    last_at, weights = pending.get(name, (at, defaultdict(float)))
                    if at != last_at:  # previous report period complete: publish its mean
                        total = sum(weights.values())
                        state["kpm_dist"][name] = (sum(DISTRIBUTIONS[name](k) * w for k, w in weights.items()) / total
                                                   if total else None)
                        weights = defaultdict(float)
                    weights[int(labels["dist_bin_x"])] += float(value or 0)
                    pending[name] = (at, weights)
                elif name == "CARR.PDSCHMCSDist" and labels.get("dist_bin_z") is not None and value:
                    mcs[int(labels["dist_bin_z"])] += float(value)
                elif sample.get("ue_id") and labels.get("subscription_sd") is not None:
                    key = (sample["ue_id"], int(labels["subscription_sd"]))
                    state["kpm_ue"].setdefault(key, {})[name] = value
            if mcs:
                total = sum(mcs.values())
                state["kpm_mcs"] = sum(k * v for k, v in mcs.items()) / total


def age(t: float | None) -> str:
    return "no data yet" if t is None else f"last {time.monotonic() - t:.1f} s ago"


def fmt(v: Any, digits: int = 1) -> str:
    return "n/a" if v is None else f"{v:.{digits}f}"


def render() -> str:
    """One block per period; every field is described in docs/reference/live-metrics-glossary.md."""
    with lock:
        lines = [time.strftime("===== %H:%M:%S  O-RAN view (what the RIC/xApp receives) =====")]
        lines.append(f"[UN3GPP uplink | BS on PC2 via E2 research proxy]  readback {age(state['xapp_at'])}")
        for ue, f in sorted(state["un3gpp"].items()):
            lines.append(
                f"  UE {ue}: UL SNR {f.get('snr_db')} dB | MCS commanded {f.get('mcs_cmd')} / observed {f.get('mcs_obs')} / "
                f"link-adaptation {f.get('la_mcs')} | RBs commanded {f.get('rb_cmd')} (startRB {f.get('rb_first')}) / "
                f"observed max {f.get('rb_obs')}")
            lines.append(
                f"         MAC UL throughput {f.get('mac_mbps')} Mbit/s | UL data PDUs {f.get('pdus')} per s | "
                f"MCS override {'ON (O-RAN)' if f.get('override') == '1' else 'OFF (link adaptation)'} | "
                f"status age {f.get('age_ms')} ms")
        if state["un3gpp_outcome"]:
            o = state["un3gpp_outcome"]
            lines.append(f"  last action: status {o.get('status')} | mac_seq {o.get('mac_seq')} | "
                         f"duplicate {'yes' if o.get('dup') == '1' else 'no'}")
        lines.append(f"[OAI downlink | gNB on PC1, E2SM-SF + KPM]  KPM {age(state['kpm_at'])}")
        for rnti, f in sorted(state["oai_ue"].items()):
            lines.append(f"  RNTI {rnti}: slice SST {f.get('sst')} SD {f.get('sd')} | DL PRB quota {f.get('prb')} PRB")
        for (ue, sd), m in sorted(state["kpm_ue"].items()):
            thp = m.get("DRB.UEThpDl")
            delay = m.get("DRB.RlcSduDelayDl")
            lines.append(
                f"  KPM {ue} SD {sd:06d}: DL throughput {fmt(None if thp is None else thp / 1000, 2)} Mbit/s | "
                f"PDCP DL volume {m.get('DRB.PdcpSduVolumeDL')} Mbit | RLC DL delay "
                f"{fmt(None if delay is None else delay / 1000, 2)} ms | DL PRB usage {m.get('RRU.PrbTotDl')} %")
        lines.append(f"  cell: mean SS-RSRP {fmt(state['kpm_dist'].get('L1M.SS-RSRP'))} dBm | "
                     f"mean SS-SINR {fmt(state['kpm_dist'].get('MR.NRScSSSINR'))} dB | "
                     f"mean PDSCH MCS {fmt(state['kpm_mcs'])}")
        if state["oai_outcome"]:
            o = state["oai_outcome"]
            lines.append(f"  last action: status {o.get('status')} | SD {o.get('sd')} | applied {o.get('applied')} PRB "
                         f"(previous {o.get('previous')})")
        if state["oai_counters"]:
            c = state["oai_counters"]
            lines.append(f"  scheduler counters: capped {c.get('capped')} | expired {c.get('expired')} | "
                         f"fallback {c.get('fallback')} | unmapped {c.get('unmapped')}")
        lines.append("[End-to-end per service] PDR / AoI / goodput / jitter: added with the traffic generators (plan 6)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--xapp-jsonl", type=Path, required=True, help="smartfarm_sf_control stdout (JSONL)")
    parser.add_argument("--period", type=float, default=2.0)
    args = parser.parse_args()
    threading.Thread(target=xapp_reader, args=(args.xapp_jsonl,), daemon=True).start()
    threading.Thread(target=kpm_reader, daemon=True).start()
    while True:
        time.sleep(args.period)
        print(render() + "\n", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
