#!/usr/bin/env python3
"""PC2 clock discipline: keeps PC2's clock on PC1's by slewing (adjtime), never stepping.

Usage (root): clock_slew.py [--server 192.168.56.101] [--port 12300] [--interval 60]
Every interval: 8 requests to PC1's time_server.py, keep the minimum-RTT sample, offset = PC1 - PC2, then adjtime()
with that offset. The kernel slews at most 0.5 ms/s, so a 5.7 s lag closes in ~3.2 h with no jump: the radio, the
MAC and the E2 proxy on PC2 never see time go backwards or leap. One English line per round.
"""
import argparse
import ctypes
import socket
import struct
import time

REQ = struct.Struct("!4sQ")
REP = struct.Struct("!4sQQQ")


class Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


libc = ctypes.CDLL("libc.so.6", use_errno=True)


def measure(server: str, port: int, n: int = 8) -> tuple[float, float] | None:
    best = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)
    for _ in range(n):
        t0 = time.time_ns()
        try:
            sock.sendto(REQ.pack(b"SFTQ", t0), (server, port))
            data, _ = sock.recvfrom(64)
        except OSError:
            continue
        t3 = time.time_ns()
        magic, echo, t1, t2 = REP.unpack(data)
        if magic != b"SFTR" or echo != t0:
            continue
        rtt = (t3 - t0) - (t2 - t1)
        offset = ((t1 - t0) + (t2 - t3)) / 2  # NTP formula: server - client
        if best is None or rtt < best[1]:
            best = (offset / 1e9, rtt / 1e9)
        time.sleep(0.05)
    return best


def slew(offset_s: float) -> float:
    delta = Timeval(int(offset_s), int(round((offset_s - int(offset_s)) * 1e6)))
    old = Timeval()
    if libc.adjtime(ctypes.byref(delta), ctypes.byref(old)) != 0:
        raise OSError(ctypes.get_errno(), "adjtime failed (run as root)")
    return old.tv_sec + old.tv_usec / 1e6


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", default="192.168.56.101")
    parser.add_argument("--port", type=int, default=12300)
    parser.add_argument("--interval", type=float, default=60.0)
    args = parser.parse_args()
    while True:
        m = measure(args.server, args.port)
        if m is None:
            print(f"{time.strftime('%H:%M:%S')} no reply from {args.server}:{args.port}", flush=True)
        else:
            offset, rtt = m
            pending = slew(offset)  # replaces any slew still in progress with the fresh measurement
            print(f"{time.strftime('%H:%M:%S')} offset PC1-PC2 {offset:+.4f} s (rtt {rtt * 1e3:.2f} ms); "
                  f"slewing, previous remaining {pending:+.4f} s", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
