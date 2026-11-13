"""Lab radio port of the demo's DDQN controller (smartfarm.orchestrator.oran_agent.RadioPort), loaded by
demo_orchestrator.py only when a `ddqn` demo session starts streaming.

It drives the radios exactly as the online agent does, by reusing its code: a `calibration_sweep.Sweep` holds the E2
control client and applies the joint action (`Sweep.ul`: release = BS default 132-RB reservation at the current LA MCS
then release; override = MCS/RBs, both waiting for the BS proxy's final verdict; `Sweep.quota`: 30 s TTL quota of
the service slice), `Sweep.safe_defaults` restores UL link adaptation + 106-PRB quota, `rl_online.reconnect_e2`
follows an xApp restart, `calibration_sweep.kpm_window` reads the slice KPM from the bridge journal and the PC2 clock
offset is measured as in rl_online (UL actions are stamped in the BS clock). What differs from a calibration cell is
only the traffic: the demo's content stream keeps flowing, and the step's SLA metrics come from the records the app
reports to the collector for the session's generator run (`oran_agent.step_records` + `evaluate`).

The xApp serves one control client at a time. Before connecting, the xApp's stderr log tells whether a client is
connected now (`client connected` after the last `client disconnected`/start); after connecting, no read-back of both
nodes within 10 s means the connection is queued behind another client (or no E2 node): the port closes it at once and
reports the reason, so an experiment holding the xApp is never disturbed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "deployment/scripts"))
import calibration_sweep as sweep_mod  # noqa: E402
from calibration_sweep import SOCKET, Sweep, kpm_window  # noqa: E402
from traffic_run import COLLECTED  # noqa: E402

from smartfarm.contracts.evaluation import evaluate  # noqa: E402
from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId  # noqa: E402
from smartfarm.e2.control_client import E2ControlClient, E2ControlError  # noqa: E402
from smartfarm.orchestrator.oran_agent import step_records  # noqa: E402
from smartfarm.orchestrator.oran_view import DEFAULT_JOURNAL_DIR  # noqa: E402
from smartfarm.rl.dataset import Action  # noqa: E402

STDERR_NAME = "sf-control.stderr"


def xapp_client_connected(stderr: Path, nbytes: int = 65536) -> bool:
    """True when the xApp's log says a control client is connected right now."""

    try:
        with stderr.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - nbytes))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return False
    marks = {m: text.rfind(m) for m in ("client connected", "client disconnected", "=== sf_control start")}
    return marks["client connected"] > max(marks["client disconnected"], marks["=== sf_control start"])


class LabRadioPort:
    def __init__(self, service: str, out: Path, *, log_dir: Path | None = None, socket: Path = SOCKET,
                 collected: Path = COLLECTED) -> None:
        self.service = ServiceId(service)
        self.out = out
        self.log_dir = log_dir or PROJECT / os.environ.get("SF_CONTROL_LOG_DIR", DEFAULT_JOURNAL_DIR)
        self.socket = socket
        self.collected = collected
        self.sw: Sweep | None = None

    async def open(self, service: ServiceId) -> str | None:
        if xapp_client_connected(self.log_dir / STDERR_NAME):
            return "xApp busy: another control client is connected (an experiment or rl_online?)"
        if not self.socket.exists():
            return "xApp not running (no control socket)"
        sweep_mod.SERVICE = service  # Sweep.quota targets this slice (one demo session at a time)
        try:
            offset = await asyncio.to_thread(sweep_mod.measure_pc2_offset_ns)
            offset_note = None
        except Exception as error:  # noqa: BLE001 - PC2 is kept aligned by sf_clock_slew: go on with 0
            offset, offset_note = 0, repr(error)[:160]
        sweep_mod.UN3GPP_CLOCK_OFFSET_NS = offset
        e2 = E2ControlClient(self.socket)
        try:
            await asyncio.wait_for(e2.connect(), 5)
        except (OSError, TimeoutError) as error:
            return f"xApp not running ({error!r})"[:200]
        for _ in range(50):
            if e2.snapshot_for("un3gpp") and e2.snapshot_for("oai"):
                break
            await asyncio.sleep(0.2)
        else:
            await e2.close()
            return "xApp busy or silent: no E2 read-back of both nodes within 10 s"
        try:
            self.out.mkdir(parents=True, exist_ok=True)
            args = argparse.Namespace(output=self.out, max_outage_s=0, cell_s=30, settle_s=3.0,
                                      rate_bps=SERVICE_CATALOG[service].offered_rate_bps)
            self.sw = Sweep(args, e2)
        except Exception as error:  # noqa: BLE001
            await e2.close()
            return f"O-RAN control setup failed: {error!r}"[:200]
        self.sw.event("demo_oran_start", service=service.value, pc2_clock_offset_s=offset / 1e9,
                      pc2_clock_offset_error=offset_note)
        return None

    async def apply(self, action: Action) -> dict[str, Any]:
        assert self.sw is not None
        if action.link_adaptation:
            ul = await self.sw.ul(None, None)
        else:
            ul = await self.sw.ul(action.ul_mcs, action.ul_rb)
        if ul.startswith("REJECTED"):
            return {"ul_status": ul}
        return {"ul_status": ul, "dl_status": await self.sw.quota(action.dl_prb)}

    async def renew_quota(self, prb: int) -> str:
        assert self.sw is not None
        return await self.sw.quota(prb)

    def ul_sample(self) -> dict[str, float] | None:
        snap = self.sw.e2.snapshot_for("un3gpp") if self.sw else None
        if not snap or not snap.un3gpp_ues:
            return None
        v = snap.un3gpp_ues[0]
        return {"snr_db": v.snr_db, "mcs_observed": v.mcs_observed, "rb_observed": v.rb_observed,
                "la_mcs": v.la_mcs, "mac_mbps": v.mac_mbps}

    async def measure(self, run_ids: list[str], t0: float, t1: float) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        for run_id in run_ids:
            path = self.collected / f"{run_id}.jsonl"
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        step = step_records(records, int(t0 * 1e9), int(t1 * 1e9), self.service)
        evaluation = evaluate(self.service, step)
        kpm = await asyncio.to_thread(kpm_window, t0, t1, int(SERVICE_CATALOG[self.service].snssai.sd, 16))
        return {"traffic_records": len(step), **{f"svc_{k}": v for k, v in evaluation["measured"].items()},
                "sla_met": evaluation["sla_met"], "sla_checks": evaluation["sla"], **kpm}

    async def after_error(self, error: BaseException) -> bool:
        assert self.sw is not None
        if isinstance(error, (E2ControlError, OSError)):  # xApp gone: reconnect as rl_online does
            self.sw.event("e2_lost", detail=repr(error)[:200])
            from rl_online import reconnect_e2

            if not await reconnect_e2(self.sw, attempts=10):
                return False
        await self.sw.safe_defaults("step error")
        return True

    async def safe_defaults(self, reason: str) -> dict[str, Any]:
        assert self.sw is not None
        await self.sw.safe_defaults(reason)
        return {"reason": reason, "ul": "release to BS link adaptation (132 RB)", "dl": "106 PRB quota"}

    async def close(self) -> None:
        if self.sw is None:
            return
        try:
            await self.sw.e2.close()
        finally:
            self.sw.events.close()
            self.sw.cells_log.close()
            self.sw = None
