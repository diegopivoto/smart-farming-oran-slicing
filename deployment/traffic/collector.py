#!/usr/bin/env python3
"""PC1 traffic collector, reachable only inside the 5G network (bound to the UPF gateway 10.45.0.1).

    POST /report        JSON record from the phone receiver or the PC3 generator -> <out>/<run_id>.jsonl
    GET  /sf_traffic.py the traffic script, so the phone can fetch it over the 5G network

Every record gets `collected_at_ns` and the reporter address. One English line per record is printed.
"""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).with_name("sf_traffic.py")


def make_handler(out: Path) -> type[BaseHTTPRequestHandler]:
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
            pass

        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/sf_traffic.py":
                self.send_error(404)
                return
            body = SCRIPT.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/x-python")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            print(f"{time.strftime('%H:%M:%S')} served sf_traffic.py to {self.client_address[0]}", flush=True)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/report":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                record = json.loads(self.rfile.read(min(length, 65536)))
                run_id = str(record["run_id"])
                if not (len(run_id) == 32 and all(c in "0123456789abcdef" for c in run_id)):
                    raise ValueError("bad run_id")
            except (ValueError, KeyError, TypeError):
                self.send_error(400)
                return
            record["collected_at_ns"] = time.time_ns()
            record["reporter"] = self.client_address[0]
            with lock, (out / f"{run_id}.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
            self.send_response(204)
            self.end_headers()
            kind, service = record.get("kind"), record.get("service")
            if kind == "receiver_window":
                print(f"{time.strftime('%H:%M:%S')} {service:7s} goodput {record['goodput_bps'] / 1e6:7.2f} Mbit/s "
                      f"rx {record['received']}/{record['expected_in_window']} PDR {record['pdr_total']} "
                      f"delay {record['delay_mean_s']} s jitter {record['jitter_s']} s gap {record['max_receive_gap_s']} s "
                      f"AoI p95 {record['aoi_p95_s']} s", flush=True)
            elif kind == "receiver_object":
                print(f"{time.strftime('%H:%M:%S')} crop    object {record['object_id']} {record['size_bytes']} B "
                      f"sha256 {'OK' if record['sha256_valid'] else 'BAD'} transfer {record['object_transfer_time_s']} s "
                      f"goodput {record['goodput_bps'] / 1e6:.2f} Mbit/s", flush=True)
            else:
                print(f"{time.strftime('%H:%M:%S')} {kind} {service}: {json.dumps(record)[:300]}", flush=True)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bind", default="10.45.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(args.out))
    print(f"Smart Farming traffic collector on http://{args.bind}:{args.port} -> {args.out}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
