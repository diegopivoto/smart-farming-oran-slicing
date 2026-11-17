#!/usr/bin/env python3
"""Calibration dataset for the RL agent: the service's full radio action mask x service traffic, through the O-RAN.

Each cell applies one action through the near-RT RIC (UN3GPP UL MCS/RB via the research proxy, OAI DL PRB quota for
the drone slice, renewed every 10 s), runs the drone service (UN3GPP UE -> phone, catalog rate, 40 Mbit/s) and records the service
metrics together with the RAN observations of the cell (UN3GPP UL SNR / observed MCS / RBs / MAC throughput sampled
every second from E2, OAI KPM from the bridge journal). Link-adaptation baseline cells (UL override released, DL quota
106) are interleaved every pass. Cell order is shuffled per pass so radio drift over the night is not confounded with
the action. Passes repeat until --until.

Safety: after every cell the UL override is released; before every cell both UEs must be reachable and in the service
slice (UN3GPP node agent through 5G, in the service S-NSSAI; phone answering ping from PC1, on the service DNN). If one
stays unhealthy, the UL override and DL quota are rolled back, a UN3GPP UE outside the service slice is moved back to it
through the node agent, the N3IWUE is restarted once (management link) and, if that does not help within
--max-outage-s, the sweep stops with everything at the default configuration.
Output: <out>/cells.jsonl, <out>/events.jsonl, <out>/summary.{json,md}.

--resume continues an interrupted sweep in the same --output: the per-pass order is deterministic (seeded by the pass
number), so cells already recorded without error are skipped and failed ones are run again; run-metadata.json is kept
and every resume is logged as a `resume` event. Cells listed in <out>/invalidated.json ({"indices": [...], "reason":
"..."}; e.g. measured while something else loaded the link) count as errors, so a resume measures them again.
"""

import argparse
import asyncio
import json
import os
import random
import statistics
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from smartfarm.baseline.endpoints import EndpointRole
from smartfarm.baseline.open5gs_sessions import load_identities
from smartfarm.contracts.radio import PrbQuotaAction, Un3gppRelease, Un3gppUlAction
from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId
from smartfarm.core.evidence import Open5gsLogEvidenceProvider
from smartfarm.e2.control_client import ActionIdAllocator, E2ControlClient
from smartfarm.orchestrator.un3gpp_slice import Un3gppSliceClient, Un3gppSliceError, agent_transport, load_agent_key

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "deployment/scripts"))
from slice_switch import AGENT_KEY, un3gpp_address  # noqa: E402
from analyze_calibration import read_cells  # noqa: E402
from traffic_run import COLLECTED, evaluate  # noqa: E402

SOCKET = Path("/run/user/1000/smartfarm-sf.sock")
# BS default UL reservation restored before every release: full band (132) in the O-RAN paper; the DLT slice market
# work uses SF_LA_DEFAULT_RB=33 (one TV channel) as the RAN default of a subscriber without a purchased slice
LA_DEFAULT_RB = int(os.environ.get("SF_LA_DEFAULT_RB", "132"))
UN3GPP_CLOCK_OFFSET_NS = 0  # BS (PC2) clock minus PC1 clock; UL actions are stamped in the BS clock (measure_pc2_offset)


def measure_pc2_offset_ns(samples: int = 5) -> int:
    """PC2 clock minus PC1 clock, from the minimum-RTT `date` sample over the PC1-PC2 management cable (not the
    UN3GPP link). The BS E2 proxy refuses an action stamped more than 5 s ahead of its own clock as REJECTED_STALE,
    so with PC2 behind PC1 (no NTP on PC2) a PC1 timestamp would be rejected."""

    best: tuple[int, int] | None = None
    for _ in range(samples):
        t0 = time.time_ns()
        done = subprocess.run(("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "smartfarm-pc2", "date +%s%N"),
                              capture_output=True, text=True, timeout=15, check=False)
        t1 = time.time_ns()
        if done.returncode == 0 and done.stdout.strip().isdigit():
            sample = (t1 - t0, int(done.stdout.strip()) - (t0 + t1) // 2)
            best = sample if best is None or sample[0] < best[0] else best
    if best is None:
        raise RuntimeError("could not read the PC2 clock over SSH")
    return best[1]
SERVICE = ServiceId.DRONE  # replaced by --service
KPM_UE = ("DRB.UEThpDl", "RRU.PrbTotDl", "DRB.RlcSduDelayDl", "DRB.PdcpSduVolumeDL")


def mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 4) if values else None


def kpm_window(since_s: float, until_s: float, sd: int) -> dict[str, Any]:
    out = subprocess.run(
        ("journalctl", "-u", "smartfarm-kpm-bridge", "--since", f"@{since_s:.0f}", "--until", f"@{until_s:.0f}",
         "-o", "cat", "--no-pager"), capture_output=True, text=True, timeout=60, check=False).stdout
    ue: dict[str, list[float]] = defaultdict(list)
    rsrp: dict[int, float] = defaultdict(float)
    sinr: dict[int, float] = defaultdict(float)
    mcs: dict[int, float] = defaultdict(float)
    for line in out.splitlines():
        if not line.startswith("{"):
            continue
        try:
            sample = json.loads(line)
        except json.JSONDecodeError:
            continue
        for m in sample.get("measurements", []):
            name, value, labels = m.get("name"), m.get("value"), m.get("labels", {})
            if value is None:
                continue
            if name in KPM_UE and labels.get("subscription_sd") == sd:
                ue[name].append(float(value))
            elif name == "L1M.SS-RSRP" and labels.get("dist_bin_x") is not None:
                rsrp[int(labels["dist_bin_x"])] += float(value)
            elif name == "MR.NRScSSSINR" and labels.get("dist_bin_x") is not None:
                sinr[int(labels["dist_bin_x"])] += float(value)
            elif name == "CARR.PDSCHMCSDist" and labels.get("dist_bin_z") is not None:
                mcs[int(labels["dist_bin_z"])] += float(value)

    def dist_mean(weights: dict[int, float], mapping: Any) -> float | None:
        total = sum(weights.values())
        return round(sum(mapping(k) * w for k, w in weights.items()) / total, 3) if total else None

    return {
        "kpm_ue_thp_dl_mbps": mean([v / 1000 for v in ue["DRB.UEThpDl"]]),
        "kpm_prb_tot_dl_pct": mean(ue["RRU.PrbTotDl"]),
        "kpm_rlc_delay_dl_ms": mean([v / 1000 for v in ue["DRB.RlcSduDelayDl"]]),
        "kpm_samples": len(ue["DRB.UEThpDl"]),
        "kpm_ss_rsrp_dbm": dist_mean(rsrp, lambda n: n - 157 + 0.5),
        "kpm_ss_sinr_db": dist_mean(sinr, lambda n: n / 2 - 23 + 0.25),
        "kpm_pdsch_mcs_mean": dist_mean(mcs, lambda n: n),
    }


# Admission criterion (docs/design/calibration-action-spaces.md): an action enters a service's dataset only if its
# capacity reaches the service's goodput target. UN3GPP UL capacity: net bytes per PDU published by the BS
# (capacity_bytes.mcsN, 33/66/99/132 RB) x 1.4562 kbit/s per byte, fitted on the actuator gate (132 RB x MCS 9 =
# 37631 B -> 54.8 Mbit/s iperf3). OAI DL capacity: PRB quota gate (24 PRB -> 31.0, 106 PRB -> 123.5 Mbit/s), linear.
UL_CAPACITY_BYTES = {
    0: (447, 895, 1343, 1791), 1: (447, 895, 1343, 1791), 2: (896, 1792, 2688, 3584), 3: (2016, 4032, 6048, 8064),
    4: (3584, 7168, 10752, 14336), 5: (4927, 9855, 14783, 19711), 6: (6271, 12543, 18815, 25087),
    7: (6720, 13440, 20160, 26880), 8: (7615, 15231, 22847, 30463), 9: (9407, 18815, 28223, 37631),
    10: (10752, 21504, 32256, 43008), 11: (11423, 22847, 34271, 45695), 12: (12768, 25536, 38304, 51072),
    13: (14112, 28224, 42336, 56448), 14: (14784, 29568, 44352, 59136), 15: (17024, 34048, 51072, 68096),
    16: (17919, 35839, 53759, 71679), 17: (19712, 39424, 59136, 78848),
}
UL_RBS = (33, 66, 99, 132)
UL_MBPS_PER_BYTE = 54.8 / 37631
TARGET_MBPS = {ServiceId.WEATHER: 0.1, ServiceId.CROP: 15.0, ServiceId.DRONE: 34.0}  # drone was 38 at 45 Mbit/s


def ul_capacity_mbps(mcs: int, rb: int) -> float:
    return UL_CAPACITY_BYTES[mcs][UL_RBS.index(rb)] * UL_MBPS_PER_BYTE


def dl_capacity_mbps(prb: int) -> float:
    return 31.0 + (prb - 24) * (123.5 - 31.0) / (106 - 24)


def grid() -> list[dict[str, Any]]:
    """Admissible actions of the service mask (capacity >= target), plus link-adaptation baselines per DL quota."""

    bounds = SERVICE_CATALOG[SERVICE].radio_bounds
    target = TARGET_MBPS[SERVICE]
    prbs = [prb for prb in bounds.dl_prb if dl_capacity_mbps(prb) >= target]
    cells = [{"ul_mode": "override", "ul_mcs": m, "ul_rb": rb, "dl_prb": prb,
              "ul_capacity_mbps_est": round(ul_capacity_mbps(m, rb), 1), "dl_capacity_mbps_est": round(dl_capacity_mbps(prb), 1)}
             for m in range(bounds.ul_mcs_min, bounds.ul_mcs_max + 1) for rb in bounds.ul_rb
             if ul_capacity_mbps(m, rb) >= target for prb in prbs]
    cells += [{"ul_mode": "link-adaptation", "ul_mcs": None, "ul_rb": None, "dl_prb": prb} for prb in prbs]
    return cells


class Sweep:
    def __init__(self, args: argparse.Namespace, e2: E2ControlClient) -> None:
        self.args = args
        self.e2 = e2
        self.ids = ActionIdAllocator()
        self.run_id = uuid4()
        self.evidence = Open5gsLogEvidenceProvider(load_identities())
        self.un3gpp = Un3gppSliceClient(agent_transport(un3gpp_address, load_agent_key(AGENT_KEY)))
        self.out: Path = args.output
        self.events = (self.out / "events.jsonl").open("a", encoding="utf-8")
        self.cells_log = (self.out / "cells.jsonl").open("a", encoding="utf-8")
        self.last_quota: Any = None

    def event(self, kind: str, **fields: Any) -> None:
        record = {"at": datetime.now(UTC).isoformat(), "event": kind, **fields}
        self.events.write(json.dumps(record, default=str) + "\n")
        self.events.flush()
        print(f"{time.strftime('%H:%M:%S')} {kind} {json.dumps(fields, default=str)[:240]}", flush=True)

    async def ul(self, mcs: int | None, rb: int | None) -> str:
        if mcs is None:
            # A RELEASE only gives the MCS back to the BS link adaptation: the RB reservation of the last command stays
            # (MacController::applySmartFarmUl). Re-assert the BS default reservation first, at the MCS the link
            # adaptation is using now, so "link adaptation" always means LA MCS on the full band (found 2026-11-03).
            snap = self.e2.snapshot_for("un3gpp")
            la = snap.un3gpp_ues[0].la_mcs if snap and snap.un3gpp_ues and snap.un3gpp_ues[0].la_mcs > 0 else 10
            reset = Un3gppUlAction(action_id=self.ids.next(), run_id=self.run_id, ue=1, mcs=la, number_rb=LA_DEFAULT_RB,
                                   ttl_ms=30000, issued_at_ns=time.time_ns() + UN3GPP_CLOCK_OFFSET_NS)
            await self.e2.apply(reset)
            await self.e2.wait_un3gpp_final(reset.action_id, timeout_s=15)
            action: Any = Un3gppRelease(action_id=self.ids.next(), run_id=self.run_id, ue=1, ttl_ms=30000,
                                        issued_at_ns=time.time_ns() + UN3GPP_CLOCK_OFFSET_NS)
        else:
            action = Un3gppUlAction(action_id=self.ids.next(), run_id=self.run_id, ue=1, mcs=mcs, number_rb=rb,
                                    ttl_ms=30000, issued_at_ns=time.time_ns() + UN3GPP_CLOCK_OFFSET_NS)
        await self.e2.apply(action)
        return (await self.e2.wait_un3gpp_final(action.action_id, timeout_s=15)).status

    async def quota(self, prb: int) -> str:
        action = PrbQuotaAction(action_id=self.ids.next(), run_id=self.run_id,
                                snssai=SERVICE_CATALOG[SERVICE].snssai, prb_quota=prb, ttl_ms=30000,
                                issued_at_ns=time.time_ns())
        outcome = await self.e2.apply(action)
        self.last_quota = outcome
        return str(getattr(outcome, "status", outcome))

    async def healthy(self) -> tuple[bool, str]:
        try:
            status = await self.un3gpp.status()
        except Un3gppSliceError as error:
            return False, f"un3gpp unreachable: {error}"
        phone = self.evidence.current_session(EndpointRole.ONEPLUS_8T)
        if status is None or phone is None:
            return False, f"missing PDU session (un3gpp: {status is not None}, phone: {phone is not None})"
        profile = SERVICE_CATALOG[SERVICE]
        if status.sd != profile.snssai.sd.lower():
            return False, f"un3gpp in SD {status.sd}, service slice is SD {profile.snssai.sd}"
        if phone.dnn != profile.handset_dnn:
            return False, f"phone on DNN {phone.dnn}, service DNN is {profile.handset_dnn}"
        # SF_SKIP_PHONE_PING=1: the handset drops ICMP (seen 2026-11-14); the session/DNN checks above still apply and
        # the service traffic of the step proves reachability
        if os.environ.get("SF_SKIP_PHONE_PING") != "1":
            ping = subprocess.run(("ping", "-n", "-q", "-c", "2", "-W", "1", str(phone.ipv4)), capture_output=True,
                                  text=True, check=False)
            if " 0 received" in ping.stdout:
                return False, f"phone {phone.ipv4} not answering"
        return True, f"un3gpp {status.ipv4} SD {status.sd}; phone {phone.ipv4} {phone.dnn}"

    async def recover(self) -> bool:
        await self.safe_defaults("recovery")
        deadline = time.monotonic() + self.args.max_outage_s
        restarted = False
        while time.monotonic() < deadline:
            ok, detail = await self.healthy()
            if ok:
                self.event("recovered", detail=detail)
                return True
            if detail.startswith("un3gpp in SD"):  # e.g. after an N3IWUE restart (default slice): back through 5G
                try:
                    if await self.un3gpp.request(SERVICE):
                        status = await self.un3gpp.await_status(SERVICE, None, timeout_s=30)
                        self.event("un3gpp_slice_restored", sd=status.sd, ipv4=str(status.ipv4))
                        continue
                except Un3gppSliceError as error:
                    self.event("un3gpp_slice_restore_failed", detail=str(error))
            if not restarted and time.monotonic() > deadline - self.args.max_outage_s / 2:
                restarted = True
                self.event("restart_n3iwue", reason=detail)
                subprocess.run(("ssh", "-o", "BatchMode=yes", "smartfarm-pc3",
                                "cd ~/src/Diego/n3iwue && sudo -n tmux kill-session -t n3iwue; sleep 2; "
                                "sudo -n ./clean.sh >/tmp/n3iwue_clean.log 2>&1; "
                                "sudo -n tmux new-session -d -s n3iwue -c /home/inatel-crr/src/Diego/n3iwue ./run.sh; "
                                "DISPLAY=:0 XAUTHORITY=/home/inatel-crr/.Xauthority setsid nohup x-terminal-emulator "
                                "-T n3iwue -e sudo tmux attach-session -t n3iwue >/dev/null 2>&1 </dev/null &"),
                               timeout=60, check=False)
            await asyncio.sleep(15)
        return False

    async def safe_defaults(self, reason: str) -> None:
        try:
            ul = await self.ul(None, None)
        except Exception as error:  # noqa: BLE001
            ul = f"error: {error}"
        try:
            dl = await self.quota(106)
        except Exception as error:  # noqa: BLE001
            dl = f"error: {error}"
        self.event("safe_defaults", reason=reason, ul=ul, dl=dl)

    async def cell(self, index: int, pass_no: int, cell: dict[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {"schema": "smartfarm.calibration_cell.v1", "pass": pass_no, "index": index,
                                  "service": SERVICE.value, **cell, "started_at": datetime.now(UTC).isoformat()}
        record["ul_status"] = await self.ul(cell["ul_mcs"], cell["ul_rb"])
        if record["ul_status"].startswith("REJECTED"):
            # the BS safety mask refuses combinations whose capacity cannot carry a 1500 B packet: kept as a data point
            record.update(skipped="ul action rejected by the BS safety mask", sla_met=False,
                          finished_at=datetime.now(UTC).isoformat())
            return record
        record["dl_status"] = await self.quota(cell["dl_prb"])
        await asyncio.sleep(self.args.settle_s)
        phone = self.evidence.current_session(EndpointRole.ONEPLUS_8T)
        assert phone is not None
        run_id = uuid.uuid4().hex
        t0 = time.time()
        record.update(traffic_run_id=run_id, handset_ipv4=str(phone.ipv4), handset_dnn=phone.dnn,
                      handset_sd=phone.snssai.sd)
        for attempt in range(3):  # the UE can be briefly unreachable right after a UL reconfiguration
            try:
                await self.un3gpp.start_traffic(SERVICE, phone.ipv4, self.args.cell_s, run_id, self.args.rate_bps)
                break
            except Un3gppSliceError:
                if attempt == 2:
                    raise
                await asyncio.sleep(2)
        record["traffic_start_attempts"] = attempt + 1
        ran: list[Any] = []
        end = time.monotonic() + self.args.cell_s + 3
        next_quota = time.monotonic() + 10
        while time.monotonic() < end:
            await asyncio.sleep(1)
            snap = self.e2.snapshot_for("un3gpp")
            if snap and snap.un3gpp_ues:
                ran.append(snap.un3gpp_ues[0])
            if time.monotonic() >= next_quota:
                next_quota += 10
                await self.quota(cell["dl_prb"])
        t1 = time.time()
        await asyncio.sleep(4)  # receiver flushes its last windows
        collected = COLLECTED / f"{run_id}.jsonl"
        for _ in range(15):  # the generator's summary (TCP retransmissions for crop) arrives once it has exited
            records = [json.loads(x) for x in collected.read_text().splitlines()] if collected.exists() else []
            if any(r.get("kind") == "generator_summary" for r in records):
                break
            await asyncio.sleep(1)
        record["generator_summary_received"] = any(r.get("kind") == "generator_summary" for r in records)
        evaluation = evaluate(SERVICE, records)
        record.update(
            traffic_records=len(records),
            **{f"svc_{k}": v for k, v in evaluation["measured"].items()},
            sla_met=evaluation["sla_met"],
            un3gpp_ul_snr_db=mean([v.snr_db for v in ran]),
            un3gpp_ul_mcs_observed=mean([float(v.mcs_observed) for v in ran]),
            un3gpp_ul_rb_observed_max=max((v.rb_observed for v in ran), default=None),
            un3gpp_la_mcs=mean([float(v.la_mcs) for v in ran]),
            un3gpp_mac_mbps=mean([v.mac_mbps for v in ran]),
            un3gpp_samples=len(ran),
            **kpm_window(t0, t1, int(SERVICE_CATALOG[SERVICE].snssai.sd, 16)),
            finished_at=datetime.now(UTC).isoformat(),
        )
        await self.ul(None, None)  # back to link adaptation after every cell
        record["ul_released"] = True
        return record

    async def run(self) -> int:
        until = datetime.fromisoformat(self.args.until).timestamp()
        cells = grid()
        pass_no, index = 0, 0
        done: dict[int, list[dict[str, Any]]] = defaultdict(list)  # pass -> cells recorded without error
        if self.args.resume:
            for record in read_cells(self.out):
                index = max(index, record["index"])
                if "error" not in record:
                    done[record["pass"]].append({k: record.get(k) for k in ("ul_mode", "ul_mcs", "ul_rb", "dl_prb")})
            self.event("resume", recorded_cells=index, valid_per_pass={p: len(v) for p, v in sorted(done.items())},
                       passes=self.args.passes, cells_per_pass=len(cells))
        else:
            self.event("start", until=self.args.until, passes=self.args.passes, cells_per_pass=len(cells),
                       cell_s=self.args.cell_s, rate_bps=self.args.rate_bps)
        try:
            while time.time() < until and pass_no < self.args.passes:
                pass_no += 1
                order = cells[:]
                random.Random(pass_no).shuffle(order)
                self.event("pass_start", pass_no=pass_no)
                for cell in order:
                    if time.time() >= until:
                        break
                    key = {k: cell[k] for k in ("ul_mode", "ul_mcs", "ul_rb", "dl_prb")}
                    if key in done[pass_no]:
                        done[pass_no].remove(key)
                        continue
                    ok, detail = await self.healthy()
                    if not ok:
                        self.event("unhealthy", detail=detail)
                        if not await self.recover():
                            self.event("abort", detail="UEs not recovered within the outage budget")
                            return 2
                    index += 1
                    try:
                        record = await self.cell(index, pass_no, cell)
                    except Exception as error:  # noqa: BLE001 - one bad cell must not end the night
                        record = {"schema": "smartfarm.calibration_cell.v1", "pass": pass_no, "index": index,
                                  **cell, "error": repr(error)[:300]}
                        await self.safe_defaults("cell error")
                    self.cells_log.write(json.dumps(record, default=str) + "\n")
                    self.cells_log.flush()
                    self.event("cell", index=index, pass_no=pass_no, ul=(cell["ul_mcs"], cell["ul_rb"]),
                               dl=cell["dl_prb"], goodput=record.get("svc_goodput"),
                               loss=record.get("svc_packet_loss_ratio"), sla=record.get("sla_met"),
                               snr=record.get("un3gpp_ul_snr_db"), error=record.get("error"))
                    await asyncio.sleep(self.args.pause_s)
        finally:
            await self.safe_defaults("end of sweep")
            summarize(self.out)
        return 0


def summarize(out: Path) -> None:
    cells = read_cells(out)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in cells:
        if "error" in c or c.get("skipped"):
            continue
        key = "LA" if c["ul_mode"] == "link-adaptation" else f"MCS{c['ul_mcs']}/RB{c['ul_rb']}"
        groups[f"{key} DL{c['dl_prb']}"].append(c)
    rows = []
    for key, items in sorted(groups.items()):
        rows.append({"action": key, "n": len(items),
                     "sla_met_ratio": round(sum(bool(c.get("sla_met")) for c in items) / len(items), 3),
                     "goodput_mbps_median": round(statistics.median([(c.get("svc_goodput") or 0) / 1e6 for c in items]), 2),
                     "loss_median": round(statistics.median([c.get("svc_packet_loss_ratio") or 1 for c in items]), 4),
                     "ul_snr_db_mean": mean([c["un3gpp_ul_snr_db"] for c in items if c.get("un3gpp_ul_snr_db")])})
    summary = {"cells": len(cells), "errors": sum("error" in c for c in cells),
               "rejected_by_safety_mask": sorted({f"MCS{c['ul_mcs']}/RB{c['ul_rb']}" for c in cells if c.get("skipped")}),
               "actions": rows}
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    lines = ["| Action | n | SLA met ratio | Goodput median (Mbit/s) | Loss median | UL SNR mean (dB) |",
             "|---|---|---|---|---|---|"]
    lines += [f"| {r['action']} | {r['n']} | {r['sla_met_ratio']} | {r['goodput_mbps_median']} | {r['loss_median']} | "
              f"{r['ul_snr_db_mean']} |" for r in rows]
    (out / "summary.md").write_text(f"Cells: {summary['cells']} (errors: {summary['errors']}; rejected by the BS safety "
                                    f"mask: {', '.join(summary['rejected_by_safety_mask']) or 'none'})\n\n" + "\n".join(lines) + "\n",
                                    encoding="utf-8")


async def main_async(args: argparse.Namespace) -> int:
    global SERVICE
    SERVICE = ServiceId(args.service)
    if args.rate_bps is None:
        args.rate_bps = SERVICE_CATALOG[SERVICE].offered_rate_bps
    args.output.mkdir(parents=True, exist_ok=True)
    metadata_path = args.output / ("run-metadata-resume-" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json"
                                   if args.resume else "run-metadata.json")
    metadata_path.write_text(json.dumps({
        "schema": "smartfarm.calibration_run.v1", "started_at": datetime.now(UTC).isoformat(), "service": SERVICE.value,
        "radio_bounds": SERVICE_CATALOG[SERVICE].radio_bounds.model_dump(), "sla": [m.model_dump() for m in SERVICE_CATALOG[SERVICE].sla],
        "rate_bps": args.rate_bps, "cell_s": args.cell_s, "settle_s": args.settle_s, "until": args.until, "passes": args.passes,
        "admission": {"target_mbps": TARGET_MBPS[SERVICE], "ul_mbps_per_capacity_byte": UL_MBPS_PER_BYTE,
                      "doc": "docs/design/calibration-action-spaces.md"},
        "grid": grid()}, indent=2) + "\n", encoding="utf-8")
    global UN3GPP_CLOCK_OFFSET_NS
    UN3GPP_CLOCK_OFFSET_NS = measure_pc2_offset_ns()
    print(f"PC2 clock offset vs PC1: {UN3GPP_CLOCK_OFFSET_NS / 1e9:+.3f} s (UL actions stamped in the BS clock)", flush=True)
    async with E2ControlClient(SOCKET) as e2:
        for _ in range(50):
            if e2.snapshot_for("un3gpp") and e2.snapshot_for("oai"):
                break
            await asyncio.sleep(0.2)
        return await Sweep(args, e2).run()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--service", choices=[s.value for s in ServiceId], default="drone")
    parser.add_argument("--until", default="2099-01-01T00:00", help="local ISO time to stop at the latest")
    parser.add_argument("--passes", type=int, default=4, help="complete shuffled passes over the action space")
    parser.add_argument("--cell-s", type=int, default=30)
    parser.add_argument("--settle-s", type=float, default=3.0)
    parser.add_argument("--pause-s", type=float, default=3.0)
    parser.add_argument("--rate-bps", type=int, help="default: the service's catalog rate")
    parser.add_argument("--max-outage-s", type=float, default=600)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true", help="continue an interrupted sweep in --output")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
