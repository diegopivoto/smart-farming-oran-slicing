#!/usr/bin/env python3
"""Smart Farming service traffic: generator (UN3GPP UE, PC3) and receiver (OnePlus, Termux). Python stdlib only.

Services (all from the UN3GPP UE to the phone, through the 5G core):
  weather  UDP 47001  periodic sensor readings (10/s x 1250 B = 100 kbit/s)  -> PDR, AoI, freshness gap, one-way delay
  crop     TCP 47002  multispectral image objects (SHA-256 checked)          -> goodput, object transfer time, SHA-256
  drone    UDP 47003  video-like CBR stream (default 45 Mbit/s, 1200 B)       -> goodput, loss, jitter, receive gap

  generator:  python3 sf_traffic.py send --service drone --target 10.45.160.4 --duration 30 --run-id <hex32>
              [--rate-bps 40000000] [--report http://10.45.0.1:8088/report]
  receiver:   python3 sf_traffic.py receive --report http://10.45.0.1:8088/report

Clock offset (receiver - generator) is estimated by an NTP-like exchange with the generator's time responder
(UDP 47000, minimum-RTT sample, refreshed every 10 s), so one-way delay and AoI use the generator's timestamps.
The receiver prints one line per service per second and POSTs the same JSON records to --report (PC1 collector,
reachable only inside the 5G network). All output is in English.
"""

import argparse
import contextlib
import hashlib
import json
import os
import queue
import socket
import struct
import sys
import threading
import time
import urllib.request
import uuid

SERVICES = {"weather": 1, "crop": 2, "drone": 3}
NAMES = {code: name for name, code in SERVICES.items()}
PORTS = {"weather": 47001, "crop": 47002, "drone": 47003}
TIME_PORT = 47000
DGRAM = struct.Struct("!4sBB16sQQ")  # magic, version, service, run_id, seq, generated_at_ns (generator clock)
OBJECT = struct.Struct("!4s16sQQ32sQ")  # magic, run_id, object_id, size, sha256, generated_at_ns
TIMEQ = struct.Struct("!4sQ")
TIMER = struct.Struct("!4sQQ")
DEFAULT_RATE = {"weather": 100_000, "crop": 20_000_000, "drone": 40_000_000}
DEFAULT_SIZE = {"weather": 1250, "drone": 1200}
CROP_OBJECT_BYTES = 25 * 1024 * 1024  # one multispectral capture (5 bands x 5 MB)


def now_ns() -> int:
    return time.time_ns()


_reports: "queue.Queue[tuple[str, dict]]" = queue.Queue(maxsize=10000)
_reporter_started = False


def _reporter() -> None:
    while True:
        url, record = _reports.get()
        try:
            request = urllib.request.Request(url, data=json.dumps(record).encode(), method="POST",
                                             headers={"Content-Type": "application/json"})
            urllib.request.urlopen(request, timeout=3).read()
        except OSError:
            pass  # reporting must never stop the measurement


def post(url: str | None, record: dict, wait: bool = False) -> None:
    """Queue a record for the collector; a background thread sends it (measurement loops never block)."""

    global _reporter_started
    if not url:
        return
    if not _reporter_started:
        _reporter_started = True
        threading.Thread(target=_reporter, daemon=True).start()
    try:
        _reports.put_nowait((url, record))
    except queue.Full:
        return
    if wait:  # generator: deliver its summary before exiting
        deadline = time.monotonic() + 5
        while not _reports.empty() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


# ---------------------------------------------------------------- generator (PC3, inside vrf-n3iwue)

def time_responder(stop: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", TIME_PORT))
    sock.settimeout(0.5)
    while not stop.is_set():
        try:
            data, peer = sock.recvfrom(64)
        except TimeoutError:
            continue
        if len(data) == TIMEQ.size and data[:4] == b"SFTQ":
            sock.sendto(TIMER.pack(b"SFTR", TIMEQ.unpack(data)[1], now_ns()), peer)
    sock.close()


def send_udp(service: str, target: str, duration: float, run_id: bytes, rate_bps: int, size: int) -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    interval = size * 8 / rate_bps
    padding = os.urandom(max(0, size - DGRAM.size))
    code, seq, errors = SERVICES[service], 0, 0
    start = time.monotonic()
    deadline = start + duration
    next_send = start
    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        if now < next_send:
            time.sleep(min(next_send - now, 0.002))
            continue
        burst = max(1, int((now - next_send) / interval) + 1)  # catch up in bursts at high rates
        for _ in range(min(burst, 64)):
            try:
                sock.sendto(DGRAM.pack(b"SFT1", 1, code, run_id, seq, now_ns()) + padding, (target, PORTS[service]))
            except OSError:
                errors += 1
            seq += 1
            next_send += interval
    elapsed = time.monotonic() - start
    return {"sent_packets": seq, "send_errors": errors, "sent_bytes": seq * size,
            "offered_bps": round(seq * size * 8 / elapsed), "elapsed_s": round(elapsed, 3)}


def tcp_retrans(sock: socket.socket) -> dict:
    """Linux TCP_INFO: total retransmitted segments and bytes sent (best effort)."""

    try:
        info = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 256)
        total_retrans = struct.unpack_from("I", info, 100)[0]
        bytes_sent = struct.unpack_from("Q", info, 200)[0] if len(info) >= 208 else None
        bytes_retrans = struct.unpack_from("Q", info, 208)[0] if len(info) >= 216 else None
        return {"tcp_total_retrans_segments": total_retrans, "tcp_bytes_sent": bytes_sent,
                "tcp_bytes_retrans": bytes_retrans}
    except (OSError, struct.error):
        return {}


def send_crop(target: str, duration: float, run_id: bytes, rate_bps: int, object_bytes: int) -> dict:
    """Objects back to back until `duration`, a hard deadline: the object in flight then is aborted (the receiver
    drops incomplete objects) and the connection reset, so no traffic spills into whatever runs next."""

    sock = socket.create_connection((target, PORTS["crop"]), timeout=10)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    chunk = 64 * 1024
    start = time.monotonic()
    deadline = start + duration
    sent_objects, sent_bytes, aborted = 0, 0, 0
    try:
        while time.monotonic() < deadline:
            body = os.urandom(object_bytes)
            header = OBJECT.pack(b"SFO1", run_id, sent_objects, object_bytes, hashlib.sha256(body).digest(), now_ns())
            view = memoryview(body)
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            sock.sendall(header)
            for offset in range(0, object_bytes, chunk):  # paced at the offered rate
                if time.monotonic() >= deadline:
                    raise TimeoutError
                sock.settimeout(max(0.05, deadline - time.monotonic()))
                sock.sendall(view[offset:offset + chunk])
                sent_bytes += min(chunk, object_bytes - offset)
                ahead = sent_bytes * 8 / rate_bps - (time.monotonic() - start)
                if ahead > 0:
                    time.sleep(min(ahead, max(0.0, deadline - time.monotonic())))
            sent_objects += 1
    except (TimeoutError, socket.timeout):
        aborted = 1
    stats = tcp_retrans(sock)
    if aborted:  # RST: drop what is still queued in the kernel instead of draining it after the deadline
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()
    elapsed = time.monotonic() - start
    return {"sent_objects": sent_objects, "aborted_objects": aborted, "sent_bytes": sent_bytes,
            "offered_bps": round(sent_bytes * 8 / elapsed), "elapsed_s": round(elapsed, 3), **stats}


def cmd_send(args: argparse.Namespace) -> int:
    run_id = bytes.fromhex(args.run_id) if args.run_id else uuid.uuid4().bytes
    rate = args.rate_bps or DEFAULT_RATE[args.service]
    stop = threading.Event()
    threading.Thread(target=time_responder, args=(stop,), daemon=True).start()
    time.sleep(args.warmup_s)  # let the receiver synchronise its clock first
    started = now_ns()
    print(f"generator start service={args.service} target={args.target} rate_bps={rate} duration_s={args.duration}",
          flush=True)
    if args.service == "crop":
        result = send_crop(args.target, args.duration, run_id, rate, args.object_bytes)
    else:
        result = send_udp(args.service, args.target, args.duration, run_id, rate, args.size or DEFAULT_SIZE[args.service])
    time.sleep(1.0)
    stop.set()
    record = {"kind": "generator_summary", "run_id": run_id.hex(), "service": args.service, "target": args.target,
              "rate_bps_requested": rate, "started_at_ns": started, **result}
    print(json.dumps(record), flush=True)
    post(args.report, record, wait=True)
    return 0


# ---------------------------------------------------------------- receiver (OnePlus, Termux)

class ClockSync:
    """Offset (receiver clock - generator clock) from the generator's time responder."""

    def __init__(self) -> None:
        self.offset_ns: int | None = None
        self.rtt_ns: int | None = None
        self.peer: str | None = None
        self._last = 0.0
        self._lock = threading.Lock()

    def observe_peer(self, address: str) -> None:
        if address != self.peer:
            self.peer, self._last = address, 0.0
        if time.monotonic() - self._last > 10:
            self._last = time.monotonic()
            threading.Thread(target=self.sync, args=(address,), daemon=True).start()

    def sync(self, address: str) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        best: tuple[int, int] | None = None
        for _ in range(8):
            t0 = now_ns()
            try:
                sock.sendto(TIMEQ.pack(b"SFTQ", t0), (address, TIME_PORT))
                data, _ = sock.recvfrom(64)
            except OSError:
                continue
            t1 = now_ns()
            if len(data) == TIMER.size and data[:4] == b"SFTR":
                _, echoed, server = TIMER.unpack(data)
                if echoed == t0 and (best is None or t1 - t0 < best[0]):
                    best = (t1 - t0, (t0 + t1) // 2 - server)
            time.sleep(0.05)
        sock.close()
        if best:
            with self._lock:
                self.rtt_ns, self.offset_ns = best

    def to_receiver_clock(self, generated_at_ns: int) -> int | None:
        return None if self.offset_ns is None else generated_at_ns + self.offset_ns


class UdpStats:
    def __init__(self, service: str) -> None:
        self.service = service
        self.lock = threading.Lock()
        self.run_id: str | None = None
        self.first_seq: int | None = None
        self.max_seq = -1
        self.total_received = 0
        self.reset_window()
        self.transit_prev: float | None = None
        self.jitter_s = 0.0
        self.last_generated_rx_ns: int | None = None  # newest update, receiver clock (AoI)
        self.idle_windows = 0

    def reset_window(self) -> None:
        self.w_received, self.w_bytes, self.w_delays, self.w_gaps, self.w_aoi = 0, 0, [], [], []
        self.w_last_arrival: float | None = None
        self.w_seq_lo: int | None = None
        self.w_seq_hi = -1

    def add(self, run_id: str, seq: int, generated_rx_ns: int | None, size: int, arrival_ns: int) -> None:
        with self.lock:
            if run_id != self.run_id:  # new run: start over
                self.__init__(self.service)
                self.run_id = run_id
            if self.first_seq is None:
                self.first_seq = seq
            self.max_seq = max(self.max_seq, seq)
            self.total_received += 1
            self.w_received += 1
            self.w_bytes += size
            self.w_seq_lo = seq if self.w_seq_lo is None else min(self.w_seq_lo, seq)
            self.w_seq_hi = max(self.w_seq_hi, seq)
            arrival = arrival_ns / 1e9
            if self.w_last_arrival is not None:
                self.w_gaps.append(arrival - self.w_last_arrival)
            self.w_last_arrival = arrival
            if generated_rx_ns is not None:
                delay = (arrival_ns - generated_rx_ns) / 1e9
                self.w_delays.append(delay)
                if self.transit_prev is not None:  # RFC 3550 interarrival jitter
                    self.jitter_s += (abs(delay - self.transit_prev) - self.jitter_s) / 16
                self.transit_prev = delay
                if self.last_generated_rx_ns is None or generated_rx_ns > self.last_generated_rx_ns:
                    self.last_generated_rx_ns = generated_rx_ns

    def sample_aoi(self) -> None:
        with self.lock:
            if self.last_generated_rx_ns is not None:
                self.w_aoi.append((now_ns() - self.last_generated_rx_ns) / 1e9)

    def report(self, window_s: float) -> dict | None:
        with self.lock:
            if self.run_id is None:
                return None
            self.idle_windows = 0 if self.w_received else self.idle_windows + 1
            if self.idle_windows > 3:  # stream ended: stay quiet until packets arrive again
                self.reset_window()
                return None
            expected_total = self.max_seq - (self.first_seq or 0) + 1
            expected_window = (self.w_seq_hi - self.w_seq_lo + 1) if self.w_seq_lo is not None else 0
            record = {
                "kind": "receiver_window", "service": self.service, "run_id": self.run_id, "at_ns": now_ns(),
                "window_s": window_s, "received": self.w_received, "expected_in_window": expected_window,
                "goodput_bps": round(self.w_bytes * 8 / window_s),
                "total_received": self.total_received, "total_expected": expected_total,
                "pdr_total": round(self.total_received / expected_total, 5) if expected_total > 0 else None,
                "loss_ratio_window": round(1 - self.w_received / expected_window, 5) if expected_window else None,
                "jitter_s": round(self.jitter_s, 6),
                "delay_mean_s": round(sum(self.w_delays) / len(self.w_delays), 6) if self.w_delays else None,
                "delay_p95_s": percentile(self.w_delays, 0.95),
                "max_receive_gap_s": round(max(self.w_gaps), 6) if self.w_gaps else None,
                "aoi_p95_s": percentile(self.w_aoi, 0.95),
                "aoi_max_s": max(self.w_aoi) if self.w_aoi else None,
            }
            self.reset_window()
            return record


def udp_listener(service: str, stats: UdpStats, clock: ClockSync) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    sock.bind(("0.0.0.0", PORTS[service]))
    while True:
        data, peer = sock.recvfrom(65535)
        arrival = now_ns()
        if len(data) < DGRAM.size or data[:4] != b"SFT1":
            continue
        _, _, code, run_id, seq, generated = DGRAM.unpack_from(data)
        if NAMES.get(code) != service:
            continue
        clock.observe_peer(peer[0])
        stats.add(run_id.hex(), seq, clock.to_receiver_clock(generated), len(data), arrival)


def crop_listener(report_url: str | None, clock: ClockSync) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", PORTS["crop"]))
    server.listen(4)
    while True:
        conn, peer = server.accept()
        clock.observe_peer(peer[0])
        threading.Thread(target=crop_connection, args=(conn, report_url, clock), daemon=True).start()


def read_exact(conn: socket.socket, size: int) -> bytes | None:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = conn.recv(min(1 << 20, size - len(buffer)))
        if not chunk:
            return None
        buffer += chunk
    return bytes(buffer)


def crop_connection(conn: socket.socket, report_url: str | None, clock: ClockSync) -> None:
    with conn, contextlib.suppress(ConnectionResetError):  # the generator resets at its deadline
        while True:
            header = read_exact(conn, OBJECT.size)
            if header is None or header[:4] != b"SFO1":
                return
            _, run_id, object_id, size, digest, generated = OBJECT.unpack(header)
            first = now_ns()
            hasher, remaining = hashlib.sha256(), size
            while remaining:
                chunk = conn.recv(min(1 << 20, remaining))
                if not chunk:
                    return
                hasher.update(chunk)
                remaining -= len(chunk)
            last = now_ns()
            generated_rx = clock.to_receiver_clock(generated)
            record = {
                "kind": "receiver_object", "service": "crop", "run_id": run_id.hex(), "object_id": object_id,
                "at_ns": last, "size_bytes": size, "sha256_valid": hasher.digest() == digest,
                "receive_time_s": round((last - first) / 1e9, 4),
                "object_transfer_time_s": round((last - generated_rx) / 1e9, 4) if generated_rx else None,
                "goodput_bps": round(size * 8 / max((last - first) / 1e9, 1e-9)),
            }
            print(json.dumps(record), flush=True)
            post(report_url, record)


def cmd_receive(args: argparse.Namespace) -> int:
    clock = ClockSync()
    stats = {name: UdpStats(name) for name in ("weather", "drone")}
    for name in stats:
        threading.Thread(target=udp_listener, args=(name, stats[name], clock), daemon=True).start()
    threading.Thread(target=crop_listener, args=(args.report, clock), daemon=True).start()
    print(f"receiver listening weather=udp/{PORTS['weather']} crop=tcp/{PORTS['crop']} drone=udp/{PORTS['drone']} "
          f"report={args.report}", flush=True)
    next_report = time.monotonic() + 1.0
    while True:
        time.sleep(0.1)
        stats["weather"].sample_aoi()
        if time.monotonic() >= next_report:
            next_report += 1.0
            for s in stats.values():
                record = s.report(1.0)
                if record and (record["received"] or record["total_received"]):
                    record["clock_offset_ns"], record["clock_rtt_ns"] = clock.offset_ns, clock.rtt_ns
                    if record["received"]:
                        print(f"{record['service']}: goodput {record['goodput_bps'] / 1e6:.2f} Mbit/s | "
                              f"received {record['received']}/{record['expected_in_window']} | "
                              f"PDR total {record['pdr_total']} | delay mean {record['delay_mean_s']} s | "
                              f"jitter {record['jitter_s']} s | max gap {record['max_receive_gap_s']} s | "
                              f"AoI p95 {record['aoi_p95_s']} s", flush=True)
                    post(args.report, record)


def main() -> int:
    parser = argparse.ArgumentParser(description="Smart Farming service traffic generator/receiver")
    sub = parser.add_subparsers(dest="command", required=True)
    send = sub.add_parser("send")
    send.add_argument("--service", choices=SERVICES, required=True)
    send.add_argument("--target", required=True)
    send.add_argument("--duration", type=float, default=30)
    send.add_argument("--run-id")
    send.add_argument("--rate-bps", type=int)
    send.add_argument("--size", type=int)
    send.add_argument("--object-bytes", type=int, default=CROP_OBJECT_BYTES)
    send.add_argument("--warmup-s", type=float, default=2.0)
    send.add_argument("--report")
    receive = sub.add_parser("receive")
    receive.add_argument("--report")
    args = parser.parse_args()
    return cmd_send(args) if args.command == "send" else cmd_receive(args)


if __name__ == "__main__":
    sys.exit(main())
