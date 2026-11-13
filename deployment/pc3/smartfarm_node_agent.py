#!/usr/bin/env python3
"""PC3 node agent: the orchestrator's only control path to the UN3GPP UE, reachable through the 5G network only.

Listens on TCP :9700 bound to the N3IWUE VRF (SO_BINDTODEVICE vrf-n3iwue), so it is reached at the UE's PDU-session
address through UPF -> N3IWF -> IPsec, never on the lab links. Each request is one line

    <issued_at_ns> <nonce_hex> <command> <hmac_sha256_hex>

with command `status`, `slice-<1..9>`, `ping-<10.45.x.y>`, `traffic-<service>-<10.45.x.y>-<seconds>-<run_id hex32>[-<rate_bps>]`
or `traffic-stop`, and the HMAC (shared key file, mode 0600) over "<issued_at_ns> <nonce> <command>".
Requests older than 30 s, from the future, replayed or badly signed are refused. `traffic-*` starts the Smart Farming generator
(/opt/smartfarm/sf_traffic.py, or the demo content generator with `--traffic-script .../sf_content.py`, which takes the
same `send` options) inside the VRF towards the phone, reporting to the PC1 collector over the 5G network;
`traffic-stop` terminates running generators. `status`/`slice-N` are relayed to the
N3IWUE slice listener on 127.0.0.1:9600 and its reply line is returned; `ping-<ip>` pings another UE of the PDU network
from inside the VRF (5 echoes, 200 ms apart) and returns `PING sent=.. received=.. rtt_min_ms=.. rtt_avg_ms=.. rtt_max_ms=..`. Run as root (SO_BINDTODEVICE):

    sudo python3 smartfarm_node_agent.py --key /etc/smartfarm/node-agent.key \
        [--traffic-script /home/inatel-crr/src/smartfarm-content/sf_content.py]
"""

import argparse
import hashlib
import hmac
import ipaddress
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import time

MAX_AGE_NS = 30_000_000_000
_REQUEST = re.compile(r"^(?P<ts>\d{1,20}) (?P<nonce>[0-9a-f]{32}) (?P<cmd>status|slice-[1-9]|ping-10\.45\.\d{1,3}\.\d{1,3}|traffic-stop|traffic-(?:weather|crop|drone)-10\.45\.\d{1,3}\.\d{1,3}-\d{1,4}-[0-9a-f]{32}(?:-\d{4,9})?) (?P<mac>[0-9a-f]{64})$")
_seen: dict[str, int] = {}
_lock = threading.Lock()


def log(message: str) -> None:
    print(time.strftime("%H:%M:%S ") + message, flush=True)


def load_key(path: str) -> bytes:
    info = os.stat(path)
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise SystemExit(f"key file must be private (chmod 600): {path}")
    key = open(path, "rb").read().strip()
    if len(key) < 32:
        raise SystemExit("key too short")
    return key


def verify(line: str, key: bytes, now_ns: int) -> str:
    """Return the allowed command or raise ValueError with the refusal reason."""

    m = _REQUEST.match(line)
    if m is None:
        raise ValueError("malformed request")
    issued = int(m["ts"])
    if not (now_ns - MAX_AGE_NS <= issued <= now_ns + 5_000_000_000):
        raise ValueError("stale or future request")
    expected = hmac.new(key, f"{m['ts']} {m['nonce']} {m['cmd']}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, m["mac"]):
        raise ValueError("bad signature")
    with _lock:
        for nonce, ts in list(_seen.items()):
            if ts < now_ns - MAX_AGE_NS:
                del _seen[nonce]
        if m["nonce"] in _seen:
            raise ValueError("replayed request")
        _seen[m["nonce"]] = issued
    return m["cmd"]


PDU_NETWORK = ipaddress.IPv4Network("10.45.0.0/16")
_PING_SUMMARY = re.compile(r"(\d+) packets transmitted, (\d+) received")
_PING_RTT = re.compile(r"= ([\d.]+)/([\d.]+)/([\d.]+)/")


def ping(target: str, device: str) -> str:
    try:
        address = ipaddress.IPv4Address(target)
    except ValueError:
        return "ERROR invalid ping target"
    if address not in PDU_NETWORK or address in (PDU_NETWORK.network_address, PDU_NETWORK.broadcast_address):
        return "ERROR ping target outside the PDU network"
    done = subprocess.run(
        ["ip", "vrf", "exec", device, "ping", "-n", "-q", "-c", "5", "-i", "0.2", "-W", "1", str(address)],
        capture_output=True, text=True, timeout=15, check=False,
    )
    summary = _PING_SUMMARY.search(done.stdout)
    if summary is None:
        return f"ERROR ping failed: {done.stderr.strip()[-120:]}"
    rtt = _PING_RTT.search(done.stdout)
    rtt_text = " ".join(f"{name}={value}" for name, value in zip(("rtt_min_ms", "rtt_avg_ms", "rtt_max_ms"), rtt.groups())) if rtt else "rtt_min_ms=nan rtt_avg_ms=nan rtt_max_ms=nan"
    return f"PING sent={summary[1]} received={summary[2]} {rtt_text}"


TRAFFIC_SCRIPT = "/opt/smartfarm/sf_traffic.py"
COLLECTOR = "http://10.45.0.1:8088/report"
_generators: list[subprocess.Popen] = []


def traffic(command: str, device: str, script: str = TRAFFIC_SCRIPT) -> str:
    if command == "traffic-stop":
        stopped = 0
        for process in _generators:
            if process.poll() is None:
                process.terminate()
                stopped += 1
        _generators.clear()
        return f"TRAFFIC stopped={stopped}"
    parts = command.split("-")
    _, service, target, seconds, run_id = parts[:5]
    rate = parts[5] if len(parts) == 6 else None  # offered rate in bit/s (default: the service's)
    try:
        address = ipaddress.IPv4Address(target)
    except ValueError:
        return "ERROR invalid traffic target"
    if address not in PDU_NETWORK or not 1 <= int(seconds) <= 3600:
        return "ERROR traffic target or duration out of range"
    log = open(f"/tmp/sf-traffic-{run_id}-{service}.log", "ab")
    process = subprocess.Popen(
        ["ip", "vrf", "exec", device, "python3", "-u", script, "send", "--service", service,
         "--target", str(address), "--duration", seconds, "--run-id", run_id, "--report", COLLECTOR,
         *(["--rate-bps", rate] if rate else [])],
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    _generators.append(process)
    return f"TRAFFIC started service={service} target={address} duration_s={seconds} run_id={run_id} pid={process.pid}"


def relay(command: str, listener: tuple[str, int]) -> str:
    wire = "STATUS" if command == "status" else f"SLICE {command.split('-')[1]}"
    try:
        with socket.create_connection(listener, timeout=5) as conn:
            conn.sendall(wire.encode() + b"\n")
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                reply += chunk
        return reply.decode(errors="replace").strip()
    except OSError as error:
        return f"ERROR n3iwue listener unreachable: {error}"


def handle(conn: socket.socket, peer: tuple[str, int], key: bytes, listener: tuple[str, int], device: str,
           script: str = TRAFFIC_SCRIPT) -> None:
    with conn:
        conn.settimeout(5)
        try:
            line = conn.makefile("r", encoding="ascii", errors="replace").readline(512).strip()
            command = verify(line, key, time.time_ns())
        except (ValueError, OSError) as error:
            log(f"refused {peer[0]}: {error}")
            try:
                conn.sendall(f"ERROR {error}\n".encode())
            except OSError:
                pass
            return
        if command.startswith("ping-"):
            reply = ping(command[5:], device)
        elif command.startswith("traffic-"):
            reply = traffic(command, device, script)
        else:
            reply = relay(command, listener)
        log(f"{peer[0]} {command} -> {reply}")
        conn.sendall(reply.encode() + b"\n")


def ifindex(device: str) -> int | None:
    try:
        return int(open(f"/sys/class/net/{device}/ifindex").read())
    except (OSError, ValueError):
        return None


def listen(port: int, device: str | None) -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if device:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, device.encode())
    server.bind(("0.0.0.0", port))
    server.listen(16)
    server.settimeout(1.0)
    return server


def serve(port: int, device: str | None, key: bytes, listener: tuple[str, int], script: str = TRAFFIC_SCRIPT) -> None:
    """Accept loop; re-binds when the VRF is deleted and recreated (N3IWUE restart / clean.sh)."""

    server, bound = None, None
    while True:
        current = ifindex(device) if device else 0
        if server is not None and current != bound:
            log(f"{device} changed (ifindex {bound} -> {current}), re-binding")
            server.close()
            server = None
        if server is None:
            if current is None:
                time.sleep(1)  # VRF not there yet: wait for the N3IWUE
                continue
            server, bound = listen(port, device), current
            log(f"Smart Farming node agent listening on :{port} device={device or 'any'} "
                f"relay={listener[0]}:{listener[1]} traffic={script}")
        try:
            conn, peer = server.accept()
        except TimeoutError:
            continue
        except OSError as error:
            log(f"accept failed ({error}), re-binding")
            server.close()
            server = None
            continue
        threading.Thread(target=handle, args=(conn, peer, key, listener, device or "vrf-n3iwue", script),
                         daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(description="Smart Farming PC3 node agent (5G-only control path)")
    parser.add_argument("--key", required=True)
    parser.add_argument("--port", type=int, default=9700)
    parser.add_argument("--device", default="vrf-n3iwue", help="bind to this VRF; '' only for local tests")
    parser.add_argument("--listener", default="127.0.0.1:9600")
    parser.add_argument("--traffic-script", default=TRAFFIC_SCRIPT,
                        help="generator run by traffic-* (sf_traffic.py, or sf_content.py for the demo content)")
    args = parser.parse_args()
    if not os.path.isfile(args.traffic_script):
        raise SystemExit(f"traffic script not found: {args.traffic_script}")
    host, port = args.listener.rsplit(":", 1)
    serve(args.port, args.device or None, load_key(args.key), (host, int(port)), args.traffic_script)
    return 0


if __name__ == "__main__":
    sys.exit(main())
