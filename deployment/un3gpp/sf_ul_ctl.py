#!/usr/bin/env python3
"""Local control of the Smart Farming UL actuator in the UN3GPP BS MAC (runs on PC2, stdlib only).

  sudo python3 sf_ul_ctl.py apply --ue 1 --mcs 9 --rb 66 [--wait 8]
  sudo python3 sf_ul_ctl.py release --ue 1
  python3 sf_ul_ctl.py status

Commands are datagrams on the MAC's existing subscriber socket (types 4/5, see SmartFarmUl.h);
the outcome is read back from /tmp/smartfarm_ul_status, which the BS rewrites once per second.
Every invocation prints one JSON line so callers can log it verbatim.
"""

import argparse
import json
import socket
import struct
import sys
import time

COMMAND_SOCKET = "/tmp/sync_response.sock"
STATUS_FILE = "/tmp/smartfarm_ul_status"
FINAL = {"CONFIRMED", "CONSISTENT", "UNCONFIRMED", "RELEASED", "REJECTED_UE", "REJECTED_RB", "REJECTED_MCS",
         "REJECTED_CAPACITY", "MALFORMED"}


def read_status() -> dict:
    status: dict = {}
    try:
        with open(STATUS_FILE, encoding="ascii") as handle:
            for line in handle:
                key, sep, value = line.strip().partition("=")
                if sep:
                    status[key] = value
    except FileNotFoundError:
        return {"available": False}
    status["available"] = True
    return status


def send(datagram: bytes) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.sendto(datagram, COMMAND_SOCKET)


def wait_outcome(seq: int, timeout_s: float, wanted_final: set[str]) -> dict:
    deadline = time.monotonic() + timeout_s
    status = read_status()
    while time.monotonic() < deadline:
        status = read_status()
        if status.get("last_seq") == str(seq) and status.get("last_status") in wanted_final:
            return status
        time.sleep(0.2)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("--ue", type=int, required=True)
    apply.add_argument("--mcs", type=int, required=True)
    apply.add_argument("--rb", type=int, required=True, choices=(33, 66, 99, 132))
    apply.add_argument("--seq", type=int, default=None)
    apply.add_argument("--wait", type=float, default=0.0, help="seconds to wait for CONFIRMED/rejection")
    release = sub.add_parser("release")
    release.add_argument("--ue", type=int, required=True)
    release.add_argument("--seq", type=int, default=None)
    release.add_argument("--wait", type=float, default=0.0)
    sub.add_parser("status")
    args = parser.parse_args()

    if args.cmd == "status":
        print(json.dumps({"type": "status", "at_ns": time.time_ns(), "status": read_status()}, sort_keys=True))
        return 0

    seq = args.seq if args.seq is not None else int(time.time() * 1000) & 0xFFFFFFFF
    if args.cmd == "apply":
        if not 0 <= args.mcs <= 17:
            print(json.dumps({"type": "apply", "seq": seq, "error": "mcs outside 0..17"}))
            return 2
        send(struct.pack("<BIBBB", 4, seq, args.ue, args.mcs, args.rb))
        final = FINAL
    else:
        send(struct.pack("<BIB", 5, seq, args.ue))
        final = {"RELEASED", "REJECTED_UE"}
    sent_ns = time.time_ns()
    outcome = wait_outcome(seq, args.wait, final) if args.wait > 0 else {}
    print(json.dumps({"type": args.cmd, "seq": seq, "sent_at_ns": sent_ns, "request": vars(args),
                      "outcome": outcome}, sort_keys=True, default=str))
    reached = outcome.get("last_seq") == str(seq) and outcome.get("last_status") in final
    return 0 if args.wait <= 0 or reached else 1


if __name__ == "__main__":
    sys.exit(main())
