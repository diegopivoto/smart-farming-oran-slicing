#!/usr/bin/env python3
"""Live gate of the UN3GPP UL runtime actuator (plan 4): RB x MCS matrix under UL load, rejections, release.

Runs on PC1. PC2 (BS) is driven with `sf_ul_ctl.py` over SSH; PC3 (UE) generates UL load with iperf3
towards the BS tun0 address. Every step is appended to <output>/un3gpp-ul-gate.jsonl. Nothing is
restarted: the actuator MAC must already be running on PC2 and the UE attached.
"""

import argparse
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

PC2, PC3 = "smartfarm-pc2", "smartfarm-pc3"
CTL = "/tmp/sf_ul_ctl.py"
BS_TUN, UE_TUN = "20.20.0.10", "20.20.0.11"


class Log:
    def __init__(self, path: Path) -> None:
        self.handle = path.open("a", encoding="utf-8")

    def __call__(self, event: dict[str, Any]) -> None:
        self.handle.write(json.dumps({"logged_at_ns": time.time_ns(), **event}, sort_keys=True) + "\n")
        self.handle.flush()


def ssh(host: str, command: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, command),  # noqa: S603
                          capture_output=True, text=True, timeout=timeout, check=False)


def ctl(args: str, timeout: float = 30) -> dict[str, Any]:
    done = ssh(PC2, f"sudo -n python3 {CTL} {args}", timeout)
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"error": done.stderr.strip()[-300:], "rc": done.returncode}


def status() -> dict[str, str]:
    return ctl("status").get("status", {})


def ping_ue(log: Log, phase: str) -> bool:
    done = ssh(PC2, f"ping -c 3 -W 2 {UE_TUN}", 30)
    ok = done.returncode == 0
    log({"type": "sentinel", "phase": phase, "passed": ok, "stdout": done.stdout[-300:]})
    return ok


def run_iperf(seconds: int, result: dict[str, Any]) -> None:
    done = ssh(PC3, f"iperf3 -c {BS_TUN} -B {UE_TUN} -t {seconds} -J", seconds + 30)
    try:
        report = json.loads(done.stdout)
        result["mbps"] = report["end"]["sum_received"]["bits_per_second"] / 1e6
        result["sent_mbps"] = report["end"]["sum_sent"]["bits_per_second"] / 1e6
        result["retransmits"] = report["end"]["sum_sent"].get("retransmits")
    except (json.JSONDecodeError, KeyError) as error:
        result["error"] = f"{type(error).__name__}: {done.stdout[-200:]} {done.stderr[-200:]}"


def step(log: Log, ue: int, rb: int, mcs: int, load_s: int) -> dict[str, Any]:
    load: dict[str, Any] = {}
    worker = threading.Thread(target=run_iperf, args=(load_s, load))
    worker.start()
    time.sleep(2)                                    # data PDUs on air before the change
    applied = ctl(f"apply --ue {ue} --mcs {mcs} --rb {rb} --wait 10", 40)
    snapshots = []
    while worker.is_alive():
        snapshots.append(status())
        time.sleep(1)
    worker.join()
    outcome = applied.get("outcome", {})
    tail = [s for s in snapshots[-5:] if s]
    record = {
        "type": "step", "ue": ue, "rb": rb, "mcs": mcs, "seq": applied.get("seq"),
        "status": outcome.get("last_status"), "apply": applied, "load": load,
        "observed_mcs": [s.get(f"ue.{ue}.observed_mcs") for s in tail],
        "observed_rb_max": [s.get(f"ue.{ue}.observed_rb_max") for s in tail],
        "committed_mcs": tail[-1].get(f"ue.{ue}.committed_mcs") if tail else None,
        "committed_rb": tail[-1].get(f"ue.{ue}.committed_number_rb") if tail else None,
        "la_mcs": [s.get(f"ue.{ue}.la_mcs") for s in tail],
        "ul_snr_db": [s.get(f"ue.{ue}.ul_snr_db") for s in tail],
        "ul_ber": [s.get(f"ue.{ue}.ul_ber") for s in tail],
        "snapshots": snapshots,
    }
    log(record)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ue", type=int, default=1)
    parser.add_argument("--rbs", default="33,66,99,132")
    parser.add_argument("--mcs", default="3,6,9,12,15,17")
    parser.add_argument("--allow-above-la", action="store_true", help="also test MCS above the link adaptation choice")
    parser.add_argument("--load-s", type=int, default=15)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    log = Log(args.output / "un3gpp-ul-gate.jsonl")
    here = Path(__file__).with_name("sf_ul_ctl.py")
    subprocess.run(("scp", "-q", "-o", "BatchMode=yes", str(here), f"{PC2}:{CTL}"), check=True)  # noqa: S603

    initial = status()
    log({"type": "gate", "event": "start", "initial_status": initial, "args": vars(args) | {"output": str(args.output)}})
    if not initial.get("available"):
        print("actuator status file missing: is the Smart Farming MAC running on PC2?")
        return 2
    if not ping_ue(log, "pre"):
        return 2
    la = int(initial.get(f"ue.{args.ue}.la_mcs", "-1"))
    mcs_list = [int(m) for m in args.mcs.split(",") if args.allow_above_la or la < 0 or int(m) <= la]
    ssh(PC2, f"pkill -x iperf3; nohup iperf3 -s -B {BS_TUN} -D")

    results = []
    for rb in (int(r) for r in args.rbs.split(",")):
        for mcs in mcs_list:
            results.append(step(log, args.ue, rb, mcs, args.load_s))
            if not ping_ue(log, f"rb{rb}-mcs{mcs}"):
                log({"type": "gate", "event": "abort", "reason": "UE unreachable"})
                ctl(f"release --ue {args.ue} --wait 5")
                return 1

    checks = []
    for label, args_text, expected in (
        ("mcs_out_of_range", f"apply --ue {args.ue} --mcs 18 --rb 33 --wait 4", "REJECTED_MCS"),
        ("capacity_below_packet", f"apply --ue {args.ue} --mcs 1 --rb 33 --wait 4", "REJECTED_CAPACITY"),
        ("unknown_ue", "apply --ue 9 --mcs 9 --rb 66 --wait 4", "REJECTED_UE"),
    ):
        answer = ctl(args_text)
        got = answer.get("outcome", {}).get("last_status")
        if got is None and "error" in answer:
            got = f"CLIENT_REJECTED: {answer['error']}"   # refused before reaching the BS
        passed = got == expected or (expected == "REJECTED_MCS" and str(got).startswith("CLIENT_REJECTED"))
        checks.append({"check": label, "expected": expected, "got": got, "passed": passed})
    released = ctl(f"release --ue {args.ue} --wait 5")
    time.sleep(3)
    after = status()
    checks.append({"check": "release_returns_to_la", "passed": after.get(f"ue.{args.ue}.override") == "0",
                   "committed_mcs": after.get(f"ue.{args.ue}.committed_mcs"), "la_mcs": after.get(f"ue.{args.ue}.la_mcs")})
    for c in checks:
        log({"type": "check", **c})
    ssh(PC2, "pkill -x iperf3")
    final_ok = ping_ue(log, "final")
    passed = final_ok and all(c["passed"] for c in checks) and all(r["status"] in ("CONFIRMED", "CONSISTENT") for r in results)
    log({"type": "gate", "event": "end", "passed": passed, "released": released})
    print(json.dumps({"passed": passed, "steps": [(r["rb"], r["mcs"], r["status"], r["load"].get("mbps")) for r in results],
                      "checks": checks}, default=str))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
