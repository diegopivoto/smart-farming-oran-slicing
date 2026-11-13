#!/usr/bin/env python3
"""PC1 helper for the Android slice probe, reachable only inside the 5G network (bound to the UPF gateway 10.45.0.1).

Separate process and port from the traffic collector (8088), so the collector never needs a restart.

    GET /                      download page (links to every APK in --dist)
    GET /apk/<name>.apk        an APK from --dist
    GET /probe?proto=udp|tcp&port=47001..47003&count=N
                               sends N probe packets (UDP datagrams, or one TCP connection with N lines) back to the
                               requesting address -- the phone's address on the network the request came in on

Probe packets start with b"SFCP" so the app can tell them from sf_traffic.py traffic. Targets are limited to the
requester itself inside 10.45.0.0/16 and to the three service ports. One English line per event is printed.
"""

import argparse
import html
import ipaddress
import json
import socket
import struct
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

UE_POOL = ipaddress.ip_network("10.45.0.0/16")
SERVICE_PORTS = {47001: "weather", 47002: "crop", 47003: "drone"}
PROBE = struct.Struct("!4sIIQ")  # magic, seq, total, sent_at_ns
PROBE_SIZE = 200
MAX_COUNT = 100


def stamp() -> str:
    return time.strftime("%H:%M:%S")


def send_probe(proto: str, host: str, port: int, count: int) -> dict:
    sent = 0
    started = time.monotonic()
    if proto == "udp":
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for seq in range(count):
                payload = PROBE.pack(b"SFCP", seq, count, time.time_ns())
                sock.sendto(payload.ljust(PROBE_SIZE, b"\0"), (host, port))
                sent += 1
                time.sleep(0.02)
        return {"ok": True, "sent": sent, "elapsed_s": round(time.monotonic() - started, 3)}
    try:
        with socket.create_connection((host, port), timeout=3) as sock:
            for seq in range(count):
                sock.sendall(f"SFCP {seq} {count} {time.time_ns()}\n".encode())
                sent += 1
        return {"ok": True, "sent": sent, "elapsed_s": round(time.monotonic() - started, 3)}
    except OSError as exc:
        return {"ok": False, "sent": sent, "error": str(exc), "elapsed_s": round(time.monotonic() - started, 3)}


def make_handler(dist: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
            pass

        def reply(self, code: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            client = self.client_address[0]
            if ipaddress.ip_address(client) not in UE_POOL and client != "10.45.0.1":
                self.send_error(403)
                return
            url = urlsplit(self.path)
            if url.path == "/":
                apks = sorted(dist.glob("*.apk"))
                items = "".join(
                    f'<li><a href="/apk/{html.escape(p.name)}">{html.escape(p.name)}</a> ({p.stat().st_size // 1024} KiB)</li>'
                    for p in apks
                )
                page = ("<!doctype html><meta name=viewport content='width=device-width'><title>SF Slice Probe</title>"
                        f"<h2>Smart Farming slice probe</h2><ul>{items or '<li>no APK built yet</li>'}</ul>")
                self.reply(200, page.encode(), "text/html; charset=utf-8")
                print(f"{stamp()} served download page to {client}", flush=True)
            elif url.path.startswith("/apk/"):
                name = url.path[len("/apk/"):]
                target = dist / name
                if "/" in name or not name.endswith(".apk") or not target.is_file():
                    self.send_error(404)
                    return
                body = target.read_bytes()
                self.reply(200, body, "application/vnd.android.package-archive",
                           {"Content-Disposition": f'attachment; filename="{name}"'})
                print(f"{stamp()} served {name} ({len(body)} B) to {client}", flush=True)
            elif url.path == "/probe":
                query = parse_qs(url.query)
                try:
                    proto = query["proto"][0]
                    port = int(query["port"][0])
                    count = int(query.get("count", ["20"])[0])
                    if proto not in ("udp", "tcp") or port not in SERVICE_PORTS or not 1 <= count <= MAX_COUNT:
                        raise ValueError
                except (KeyError, ValueError, IndexError):
                    self.send_error(400)
                    return
                result = send_probe(proto, client, port, count)
                result.update({"target": f"{client}:{port}", "proto": proto, "service_port": SERVICE_PORTS[port]})
                self.reply(200, json.dumps(result).encode(), "application/json")
                print(f"{stamp()} probe {proto} {count} -> {client}:{port} ({SERVICE_PORTS[port]}): {json.dumps(result)}",
                      flush=True)
            else:
                self.send_error(404)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bind", default="10.45.0.1")
    parser.add_argument("--port", type=int, default=8089)
    parser.add_argument("--dist", type=Path, default=Path(__file__).resolve().parent.parent / "dist")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(args.dist))
    print(f"Smart Farming slice-probe helper on http://{args.bind}:{args.port}/ serving {args.dist}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
