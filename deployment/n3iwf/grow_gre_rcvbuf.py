#!/usr/bin/env python3
"""Grow the receive buffer of the running N3IWF's raw GRE socket, without restarting it.

Usage (root, on PC2): grow_gre_rcvbuf.py [--bytes 16777216]
The N3IWF reads the UE's decrypted GRE packets from a raw "ip4:gre" socket created with the kernel default buffer
(net.core.rmem_default, 208 KiB). At 40 Mbit/s that overflows whenever the Go reader is descheduled, and the kernel
drops the packet (the `drops` column of /proc/net/raw). This duplicates the socket into this process with
pidfd_getfd(2) and sets SO_RCVBUFFORCE on it, which changes the shared socket itself.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import socket
import subprocess
import sys

SYS_PIDFD_GETFD = 438
SO_RCVBUFFORCE = 33


def find_gre_socket() -> tuple[int, int]:
    """(pid, fd) of the n3iwf raw socket bound to protocol 47."""
    inodes = {line.split()[9] for line in open("/proc/net/raw").read().splitlines()[1:]
              if line.split()[1].endswith(":002F")}
    pids = subprocess.run(["pgrep", "-x", "n3iwf"], capture_output=True, text=True).stdout.split()
    for pid in map(int, pids):
        for fd in os.listdir(f"/proc/{pid}/fd"):
            m = re.fullmatch(r"socket:\[(\d+)\]", os.readlink(f"/proc/{pid}/fd/{fd}"))
            if m and m.group(1) in inodes:
                return pid, int(fd)
    raise SystemExit("no n3iwf raw GRE socket found")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bytes", type=int, default=16 * 1024 * 1024)
    args = parser.parse_args()
    pid, fd = find_gre_socket()
    libc = ctypes.CDLL(None, use_errno=True)
    pidfd = os.pidfd_open(pid)
    local = libc.syscall(SYS_PIDFD_GETFD, pidfd, fd, 0)
    if local < 0:
        raise SystemExit(f"pidfd_getfd failed: {os.strerror(ctypes.get_errno())}")
    sock = socket.socket(fileno=local)
    before = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    sock.setsockopt(socket.SOL_SOCKET, SO_RCVBUFFORCE, args.bytes)
    after = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    sock.detach()
    os.close(local)
    os.close(pidfd)
    print(f"n3iwf pid {pid} fd {fd}: SO_RCVBUF {before} -> {after} bytes")
    return 0 if after >= args.bytes else 1


if __name__ == "__main__":
    sys.exit(main())
