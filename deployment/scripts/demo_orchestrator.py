#!/usr/bin/env python3
"""Smart Farming demo orchestrator API on PC1: a tap on a service in the Smart Farm app drives the whole demo.

    .venv/bin/python -u deployment/scripts/demo_orchestrator.py                 # lab: http://10.45.0.1:8090
    .venv/bin/python -u deployment/scripts/demo_orchestrator.py --dry-run       # fakes, http://127.0.0.1:8090

API (JSON; only clients in the 5G UE pool 10.45.0.0/16 or loopback; the phone reaches it over its default
`internet` session):

    POST   /api/v1/service        {"service": "weather|crop|drone", "phone_ip": "10.45.x.y", "app_session": "<id>"}
                                  -> 202 {"session": "<id>", "state": "accepted", ...}  (200: same app_session again)
    GET    /api/v1/session/<id>   progress: accepted -> un3gpp_switching -> un3gpp_in_slice (un3gpp_ip)
                                  -> generator_started -> streaming | failed (cause) | stopped | superseded
                                  (each poll renews the session lease)
    DELETE /api/v1/session/<id>   stop the generator (the UN3GPP UE stays in the slice)
    GET    /api/v1/health
    GET    /api/v1/oran[?service=]  read-only O-RAN view of the slice (default: the active session's service)

While a session is active its view also carries `oran`: the radio action in force at both nodes (UN3GPP UL MCS/RBs or
the BS link adaptation; OAI DL PRB quota of the slice), the latest E2 outcomes read back and a short history, read
from the end of the E2 control xApp's JSONL journal (--xapp-journal), plus the radio-control mode of the session
(--no-oran-view drops the field).

O-RAN radio control, selectable per session (the app's `oran_mode`, else --oran-mode, default ran-default):
  ran-default  no O-RAN action: BS link adaptation on the full band, no DL quota; the xApp socket is never opened
  ddqn         the frozen DDQN agent of the paper (--oran-model, no learning) in 30 s steps while the session streams,
               through the sf-control xApp as rl_online.py (deployment/scripts/demo_oran_lab.py); safe configuration
               at the end; if the xApp is busy (an experiment) or down, the session stays at the RAN defaults
Steps are logged in <log root>/<YYYYMMDD>/oran-<session>/ (steps.jsonl, events.jsonl, run-metadata.json).

Lab mode: core evidence from the Open5GS AMF/SMF logs, UN3GPP UE through the PC3 node agent at its 5G address
(same path as slice_switch.py), generator = the node agent's --traffic-script (sf_content.py for the demo content),
renewed every --segment-s (agent limit 3600 s). Dry-run: in-memory UE/core (smartfarm.orchestrator.demo_dryrun),
nothing in the lab is touched. Every request and transition goes to experiments/demo/<YYYYMMDD>/orchestrator.jsonl
(dry-run: experiments/demo-dry-run/...). Terminal and file output are in English.
"""

import argparse
import asyncio
import functools
import signal
import subprocess
import sys
from ipaddress import IPv4Address
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[2]
LAB_BIND = "10.45.0.1"

from smartfarm.orchestrator.demo import AGENT_MAX_SEGMENT_S, DemoApi, DemoConfig, DemoOrchestrator, JsonlLog  # noqa: E402
from smartfarm.orchestrator.oran_agent import MODES, RAN_DEFAULT, AgentConfig, OranAgentSupervisor  # noqa: E402
from smartfarm.orchestrator.oran_view import JournalTail, OranMonitor, default_journal  # noqa: E402
from smartfarm.rl.policy import FINAL_MODEL, FINAL_RUNS, FrozenModel  # noqa: E402


def model_loader(args: argparse.Namespace) -> Callable[[], FrozenModel]:
    """The frozen DDQN model, loaded at the first DDQN session and kept."""

    @functools.cache
    def load() -> FrozenModel:
        return FrozenModel.load(args.oran_model, [PROJECT / r for r in FINAL_RUNS])

    return load


def build(args: argparse.Namespace) -> tuple[DemoOrchestrator, JsonlLog, OranMonitor | None, OranAgentSupervisor]:
    commit = subprocess.run(("git", "-C", str(PROJECT), "rev-parse", "--short", "HEAD"), capture_output=True,
                            text=True, check=False).stdout.strip() or None
    root = args.output or PROJECT / ("experiments/demo-dry-run" if args.dry_run else "experiments/demo")
    log = JsonlLog(root, static={"dry_run": args.dry_run})
    config = DemoConfig(
        segment_s=args.segment_s, lease_s=args.lease_s, hold_s=args.hold_s, phone_timeout_s=args.phone_timeout_s,
        un3gpp_timeout_s=args.un3gpp_timeout_s, flow_timeout_s=args.flow_timeout_s, settle_s=args.settle_s,
        tolerate_probe_failure=args.tolerate_probe_failure, ip_conflict_hops=args.ip_conflict_hops,
        require_phone_client=not (args.allow_any_client or args.dry_run),
    )
    if args.dry_run:
        from smartfarm.contracts.service import ServiceId
        from smartfarm.orchestrator.demo_dryrun import DryRunLab, DryRunRadioPort, FakeOranJournal, FakeRadio

        lab = DryRunLab()
        if args.dry_run_phone_ip:
            lab.auto_phone = False
            for item in args.dry_run_phone_ip:
                dnn, ip = item.split("=", 1)
                lab.add_phone_session(ip, dnn)
        orchestrator = DemoOrchestrator(lab, lab, log, config)  # type: ignore[arg-type]
        radio = FakeRadio()
        oran = None if args.no_oran_view else OranMonitor(FakeOranJournal(lab, radio), source_name="dry-run (fake radio)")
        load = model_loader(args)

        def port_factory(service: str, out: Path) -> DryRunRadioPort:
            return DryRunRadioPort(radio, load(), ServiceId(service))

        agent_config = AgentConfig(settle_s=0.5, cell_s=args.dry_run_oran_step_s, flush_s=0.2)
    else:
        from smartfarm.baseline.open5gs_sessions import load_identities
        from smartfarm.core.evidence import Open5gsLogEvidenceProvider
        from smartfarm.orchestrator.un3gpp_slice import Un3gppSliceClient, agent_transport, load_agent_key

        sys.path.insert(0, str(PROJECT / "deployment/scripts"))
        from slice_switch import AGENT_KEY, un3gpp_address

        evidence = Open5gsLogEvidenceProvider(load_identities())
        client = Un3gppSliceClient(agent_transport(un3gpp_address, load_agent_key(AGENT_KEY)))
        orchestrator = DemoOrchestrator(evidence, client, log, config)
        # read-only: the tail of the xApp journal, never the xApp control socket
        oran = None if args.no_oran_view else OranMonitor(JournalTail(args.xapp_journal), source_name="xapp-journal")
        load = model_loader(args)

        def port_factory(service: str, out: Path) -> Any:
            from demo_oran_lab import LabRadioPort  # E2 client only when a DDQN session starts

            return LabRadioPort(service, out, log_dir=args.xapp_journal.parent)

        agent_config = AgentConfig()
    agent = OranAgentSupervisor(orchestrator, default_mode=args.oran_mode, port_factory=port_factory, model=load,
                                log_root=root, config=agent_config)
    log.write("started", git_commit=commit, bind=f"{args.bind}:{args.port}", config=vars(config),
              log_file=str(log.path()),
              oran_view=None if args.no_oran_view else ("dry-run" if args.dry_run else str(args.xapp_journal)),
              oran_mode_default=args.oran_mode, oran_model=str(args.oran_model))
    return orchestrator, log, oran, agent


async def run(args: argparse.Namespace) -> int:
    orchestrator, log, oran, agent = build(args)
    api = DemoApi(orchestrator, log, dry_run=args.dry_run, oran=oran, agent=agent)
    agent.start()
    server = await api.serve(args.bind, args.port)
    print(f"Smart Farming demo orchestrator on http://{args.bind}:{args.port}/api/v1/ "
          f"({'DRY RUN: fake UN3GPP UE and core' if args.dry_run else 'lab: core logs + PC3 node agent over 5G'}); "
          f"log {log.path()}", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with server:
        await stop.wait()
        print("stopping: the active demo session's generator is stopped first", flush=True)
        server.close()
        await asyncio.wait_for(agent.close(), 60)  # the DDQN controller restores the safe configuration first
        await asyncio.wait_for(orchestrator.shutdown(), 30)
    log.write("exited")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="fake UN3GPP UE and core evidence (no lab access)")
    parser.add_argument("--bind", default=None, help=f"listen address (lab: only {LAB_BIND}; dry-run default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--segment-s", type=int, default=AGENT_MAX_SEGMENT_S,
                        help="generator run length before renewal (node agent limit 3600)")
    parser.add_argument("--lease-s", type=float, default=90.0, help="stop the generator without app polls (0: never)")
    parser.add_argument("--hold-s", type=float, default=2.0)
    parser.add_argument("--phone-timeout-s", type=float, default=10.0)
    parser.add_argument("--un3gpp-timeout-s", type=float, default=30.0)
    parser.add_argument("--flow-timeout-s", type=float, default=120.0)
    parser.add_argument("--settle-s", type=float, default=2.0)
    parser.add_argument("--tolerate-probe-failure", action="store_true",
                        help="start the generator even if the UE-to-UE ping through the core got no reply")
    parser.add_argument("--ip-conflict-hops", type=int, default=4,
                        help="extra UN3GPP slice hops when its address falls inside the /30 of a phone session")
    parser.add_argument("--allow-any-client", action="store_true",
                        help="accept clients that are not one of the phone's PDU sessions (curl tests from PC1)")
    parser.add_argument("--dry-run-phone-ip", action="append", metavar="DNN=IP",
                        help="dry-run: only these phone sessions exist (default: any address in the DNN pool)")
    parser.add_argument("--output", type=Path, help="log root (default experiments/demo or experiments/demo-dry-run)")
    parser.add_argument("--xapp-journal", type=Path, default=default_journal(PROJECT),
                        help="E2 control xApp JSONL journal read (tail only) for the O-RAN view "
                             "(default: $SF_CONTROL_LOG_DIR/sf-control.jsonl as run_sf_control_supervised.sh)")
    parser.add_argument("--no-oran-view", action="store_true",
                        help="leave the read-only `oran` object out of the session view")
    parser.add_argument("--oran-mode", choices=MODES, default=RAN_DEFAULT,
                        help="radio control of sessions whose request carries no oran_mode (default ran-default)")
    parser.add_argument("--no-oran-agent", action="store_const", const=RAN_DEFAULT, dest="oran_mode",
                        help="alias of --oran-mode ran-default")
    parser.add_argument("--oran-model", type=Path, default=PROJECT / FINAL_MODEL,
                        help="frozen DDQN model of the ddqn mode (action index from the final calibration runs)")
    parser.add_argument("--dry-run-oran-step-s", type=float, default=5.0,
                        help="dry-run: observation window of a fake DDQN step (lab: 30 s)")
    args = parser.parse_args()
    if args.bind is None:
        args.bind = "127.0.0.1" if args.dry_run else LAB_BIND
    if not args.dry_run and args.bind != LAB_BIND:
        parser.error(f"lab mode listens only on {LAB_BIND} (the UPF gateway seen by the phone)")
    IPv4Address(args.bind)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
