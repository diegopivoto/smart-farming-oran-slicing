#!/usr/bin/env python3
"""Minimal time server on PC1 for the lab's clock discipline (PC2 has no internet and no NTP source).

Usage: time_server.py [--bind 192.168.56.101] [--port 12300]
Request: b"SFTQ" + 8-byte client send time (ns). Reply: b"SFTR" + echoed client time + server receive ns + server send ns.
Unprivileged port, PC1-PC2 management cable only.
"""
import argparse
import socket
import struct
import time

REQ = struct.Struct("!4sQ")
REP = struct.Struct("!4sQQQ")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bind", default="192.168.56.101")
    parser.add_argument("--port", type=int, default=12300)
    args = parser.parse_args()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.bind, args.port))
    print(f"{time.strftime('%H:%M:%S')} time server on {args.bind}:{args.port}", flush=True)
    while True:
        data, peer = sock.recvfrom(64)
        rx = time.time_ns()
        if len(data) == REQ.size and data[:4] == b"SFTQ":
            sock.sendto(REP.pack(b"SFTR", REQ.unpack(data)[1], rx, time.time_ns()), peer)


if __name__ == "__main__":
    raise SystemExit(main())
