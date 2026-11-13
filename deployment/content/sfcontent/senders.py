"""The three content senders. Pacing, headers and the generator summary follow sf_traffic.py `send` exactly:

- weather: 1250 B datagrams at rate/size per second (10/s at 100 kbit/s), wall-clock paced with burst catch-up;
- drone: 1200 B datagrams CBR at the offered rate (40 Mbit/s); 30 fps MJPEG fragments, fillers in between;
- crop: SFO1 objects back to back over one TCP connection, paced at the offered rate, hard deadline (the object in
  flight at the deadline is aborted and the connection reset), TCP_INFO retransmissions in the summary and, so far
  in the run, in each object's meta.json (contract v1.1, `tcp_retrans_ratio_so_far`).

Crop and drone run in their own process (spawned by the node) so their pacing does not share the GIL with the panel.
"""

from __future__ import annotations

import collections
import contextlib
import math
import queue
import socket
import struct
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import protocol as P

T = P.T
MAX_PACING_DEBT_S = 0.02  # drone: catch up at most 20 ms (~94 datagrams) after a stall, drop the rest


@dataclass
class RunParams:
    service: str
    target: str
    run_id: str  # hex32
    duration: float | None  # None: until stopped
    rate_bps: int
    report: str | None
    media: str
    ports: dict[str, int] = field(default_factory=lambda: dict(P.PORTS))
    warmup_s: float = 2.0
    object_bytes: int = P.CROP_OBJECT_BYTES
    state: str = ""
    origin: str = "panel"  # panel | agent | standalone

    def to_dict(self) -> dict:
        return asdict(self)


def summary_record(p: RunParams, started_ns: int, result: dict) -> dict:
    """The sf_traffic.py generator summary (same keys), plus a few content-only fields."""

    return {"kind": "generator_summary", "run_id": p.run_id, "service": p.service, "target": p.target,
            "rate_bps_requested": p.rate_bps, "started_at_ns": started_ns, **result, "generator": "sf_content",
            "origin": p.origin}


class Clock:
    """Deadline / stop helper shared by the loops."""

    def __init__(self, duration: float | None, stop: threading.Event | None) -> None:
        self.start = time.monotonic()
        self.deadline = math.inf if duration is None else self.start + duration
        self.stop = stop

    def done(self) -> bool:
        return time.monotonic() >= self.deadline or (self.stop is not None and self.stop.is_set())

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


def wait(seconds: float, clock: Clock) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end and not clock.done():
        time.sleep(min(0.05, max(0.0, end - time.monotonic())))


# ---------------------------------------------------------------- weather (thread in the node)

def weather_loop(p: RunParams, document, stop: threading.Event, status) -> dict:  # type: ignore[no-untyped-def]
    size = P.WEATHER_DGRAM_BYTES
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    run_id = bytes.fromhex(p.run_id)
    interval = size * 8 / p.rate_bps
    seq = errors = 0
    clock = Clock(p.duration, stop)
    next_send = clock.start
    last_status = 0.0
    while not clock.done():
        now = time.monotonic()
        if now < next_send:
            time.sleep(min(next_send - now, 0.002))
            continue
        burst = max(1, int((now - next_send) / interval) + 1)
        for _ in range(min(burst, 64)):
            try:
                sock.sendto(P.weather_datagram(run_id, seq, P.now_ns(), document()), (p.target, p.ports["weather"]))
            except OSError:
                errors += 1
            seq += 1
            next_send += interval
        if now - last_status >= 0.25:
            last_status = now
            status({"sent_packets": seq, "sent_bytes": seq * size, "send_errors": errors})
    sock.close()
    elapsed = time.monotonic() - clock.start
    return {"sent_packets": seq, "send_errors": errors, "sent_bytes": seq * size,
            "offered_bps": round(seq * size * 8 / max(elapsed, 1e-9)), "elapsed_s": round(elapsed, 3)}


# ---------------------------------------------------------------- drone (child process)

def drone_loop(p: RunParams, ctrl: "queue.Queue", stop, status) -> dict:  # type: ignore[no-untyped-def]
    from .drone import VARIANTS, DroneFrames

    frames = DroneFrames(Path(p.media))
    size = P.DRONE_DGRAM_BYTES
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    run_id = bytes.fromhex(p.run_id)
    address = (p.target, p.ports["drone"])
    interval = size * 8 / p.rate_bps
    frame_interval = 1.0 / frames.fps
    desired = p.state if p.state in VARIANTS else "normal"
    variant, index, loops = desired, -1, 0
    pending: collections.deque[bytes] = collections.deque()
    seq = errors = frame_id = frames_sent = fillers = skipped = late = 0
    frame_bytes = stalls = debt_dropped = 0
    clock = Clock(p.duration, stop)
    next_send = next_frame = clock.start
    last_poll = last_status = 0.0
    while not clock.done():
        now = time.monotonic()
        if now - last_poll >= 0.1:
            last_poll = now
            with contextlib.suppress(queue.Empty):
                while True:
                    message = ctrl.get_nowait()
                    if message.get("state") in VARIANTS:
                        desired = message["state"]
        if now < next_send:
            time.sleep(min(next_send - now, 0.002))
            continue
        if now - next_send > MAX_PACING_DEBT_S:  # stalled (CPU starvation): drop the debt, never burst it out
            missed = int((now - next_send - MAX_PACING_DEBT_S) / interval) + 1
            next_send += missed * interval
            debt_dropped += missed
            stalls += 1
        while now >= next_frame:  # video frames are released on the wall clock, 30 per second at most
            overdue = now - next_frame >= frame_interval  # a newer frame is already due: this one is stale
            next_frame += frame_interval
            index += 1
            if index >= frames.count:  # loop boundary: the only place where the variant changes
                index, loops, variant = 0, loops + 1, desired
            if index == 0 and loops == 0:
                variant = desired
            if overdue:
                late += 1
            elif pending:  # offered rate below the video bitrate: drop at the source, keep real time
                skipped += 1
            else:
                jpeg = frames.read(variant, index)
                pending.extend(P.frame_tails(frame_id, jpeg))
                frame_id = (frame_id + 1) % P.FILLER_FRAME_ID
                frames_sent += 1
                frame_bytes += len(jpeg)
        burst = max(1, int((now - next_send) / interval) + 1)
        for _ in range(min(burst, 64)):
            if pending:
                tail = pending.popleft()
            else:
                tail = P.FILLER_TAIL
                fillers += 1
            try:
                sock.sendto(P.drone_datagram(run_id, seq, P.now_ns(), tail), address)
            except OSError:
                errors += 1
            seq += 1
            next_send += interval
        if now - last_status >= 0.25:
            last_status = now
            status({"sent_packets": seq, "sent_bytes": seq * size, "send_errors": errors, "frames_sent": frames_sent,
                    "filler_packets": fillers, "frames_skipped": skipped, "frames_late": late, "variant": variant,
                    "desired": desired, "loop_index": max(index, 0), "loop_frames": frames.count, "loops": loops,
                    "fps": frames.fps, "video_bytes": frame_bytes, "pacing_stalls": stalls,
                    "pacing_dropped_packets": debt_dropped})
    sock.close()
    elapsed = time.monotonic() - clock.start
    return {"sent_packets": seq, "send_errors": errors, "sent_bytes": seq * size,
            "offered_bps": round(seq * size * 8 / max(elapsed, 1e-9)), "elapsed_s": round(elapsed, 3),
            "frames_sent": frames_sent, "frames_skipped": skipped, "frames_late": late, "filler_packets": fillers,
            "video_bps": round(frame_bytes * 8 / max(elapsed, 1e-9)), "loops": loops, "variant": variant,
            "pacing_stalls": stalls, "pacing_dropped_packets": debt_dropped}


# ---------------------------------------------------------------- crop (child process)

class _Producer:
    """Keeps one ready object of the current state (built in a background thread; numpy/PIL release the GIL)."""

    def __init__(self, factory, state: str) -> None:  # type: ignore[no-untyped-def]
        self.factory = factory
        self.state = state
        self.ready = None
        self.cond = threading.Condition()
        self.closed = False
        self.build_s: list[float] = []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while not self.closed:
            with self.cond:
                while not self.closed and self.ready is not None and self.ready.meta["state"] == self.state:
                    self.cond.wait(0.2)
                state = self.state
            if self.closed:
                return
            built = self.factory.build(state)
            with self.cond:
                self.build_s.append(built.build_s)
                if built.meta["state"] == self.state:
                    self.ready = built
                    self.cond.notify_all()

    def take(self, clock: Clock):  # type: ignore[no-untyped-def]
        with self.cond:
            while not clock.done():
                if self.ready is not None and self.ready.meta["state"] == self.state:
                    obj, self.ready = self.ready, None
                    self.cond.notify_all()
                    return obj
                self.cond.wait(0.05)
        return None

    def set_state(self, state: str) -> None:
        with self.cond:
            self.state = state
            self.cond.notify_all()


def crop_loop(p: RunParams, ctrl: "queue.Queue", stop, status) -> dict:  # type: ignore[no-untyped-def]
    from .crop import STATES, CropFactory, restamp

    factory = CropFactory(Path(p.media), object_bytes=p.object_bytes)
    producer = _Producer(factory, p.state if p.state in STATES else "healthy")
    run_id = bytes.fromhex(p.run_id)
    chunk = 64 * 1024
    clock = Clock(p.duration, stop)
    sent_objects = sent_bytes = aborted = connects = connect_failures = object_id = 0
    tcp_totals: dict[str, int] = {}
    last_meta: dict | None = None

    def poll() -> None:
        with contextlib.suppress(queue.Empty):
            while True:
                message = ctrl.get_nowait()
                if message.get("state") in STATES:
                    producer.set_state(message["state"])

    def add_tcp(stats: dict) -> None:
        for key, value in stats.items():
            if value is not None:
                tcp_totals[key] = tcp_totals.get(key, 0) + value

    def tcp_so_far(sock: socket.socket) -> dict:
        """Run totals so far (closed connections + the open one), the generator summary's tcp_* fields."""

        current = T.tcp_retrans(sock)
        totals = {key: tcp_totals.get(key, 0) + (current.get(key) or 0)
                  for key in ("tcp_bytes_sent", "tcp_bytes_retrans")}
        known = current.get("tcp_bytes_sent") is not None or "tcp_bytes_sent" in tcp_totals
        sent, retrans = totals["tcp_bytes_sent"], totals["tcp_bytes_retrans"]
        return {"tcp_retrans_ratio_so_far": round(retrans / sent, 6) if known and sent else None,
                "tcp_bytes_sent_so_far": sent if known else None,
                "tcp_bytes_retrans_so_far": retrans if known else None}

    def report(progress: int = 0, size: int = 0, phase: str = "sending") -> None:
        status({"sent_objects": sent_objects, "aborted_objects": aborted, "sent_bytes": sent_bytes,
                "object_progress": progress, "object_size": size, "phase": phase, "connects": connects,
                "connect_failures": connect_failures, "meta": last_meta, "desired": producer.state})

    while not clock.done():
        poll()
        report(phase="connecting")
        try:
            sock = socket.create_connection((p.target, p.ports["crop"]), timeout=max(0.2, min(10.0, clock.remaining())))
        except OSError:
            connect_failures += 1
            wait(2.0, clock)
            continue
        connects += 1
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        pace_start, pace_bytes = time.monotonic(), 0
        in_flight = False
        try:
            while not clock.done():
                poll()
                report(phase="preparing")
                waited = time.monotonic()
                obj = producer.take(clock)
                if obj is None:
                    break
                pace_start += time.monotonic() - waited  # do not bank credit while the capture was being built
                # contract v1.1: live retransmissions for the app (~80 ms for 25 MiB: re-pack + SHA-256). Its time
                # stays in the pacing budget, so the offered average is unchanged (~200 kB catch-up at 20 Mbit/s)
                obj = restamp(obj, **tcp_so_far(sock))
                payload, size = obj.payload, len(obj.payload)
                header = P.object_header(run_id, object_id, size, obj.sha256, P.now_ns())
                object_id += 1
                last_meta = dict(obj.meta)
                status({"thumb_rgb": obj.thumb_rgb, "thumb_ndvi": obj.thumb_ndvi, "meta": last_meta})
                view = memoryview(payload)
                in_flight = True
                sock.settimeout(max(0.05, min(30.0, clock.remaining())))
                sock.sendall(header)
                last_report = time.monotonic()
                for offset in range(0, size, chunk):
                    if clock.done():
                        raise TimeoutError
                    sock.settimeout(max(0.05, min(30.0, clock.remaining())))
                    sock.sendall(view[offset:offset + chunk])
                    step = min(chunk, size - offset)
                    sent_bytes += step
                    pace_bytes += step
                    ahead = pace_bytes * 8 / p.rate_bps - (time.monotonic() - pace_start)
                    if ahead > 0:
                        time.sleep(min(ahead, max(0.0, clock.remaining())))
                    if time.monotonic() - last_report > 0.25:
                        last_report = time.monotonic()
                        poll()
                        report(offset + step, size)
                in_flight = False
                sent_objects += 1
                report(size, size)
        except (TimeoutError, socket.timeout):
            if in_flight:
                aborted += 1
        except OSError:  # receiver went away: count the partial object, reconnect
            if in_flight:
                aborted += 1
            in_flight = False
        add_tcp(T.tcp_retrans(sock))
        if in_flight or clock.done():  # RST: drop what is still queued instead of draining it after the deadline
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.close()
        if not clock.done():
            wait(1.0, clock)
    producer.closed = True
    elapsed = time.monotonic() - clock.start
    return {"sent_objects": sent_objects, "aborted_objects": aborted, "sent_bytes": sent_bytes,
            "offered_bps": round(sent_bytes * 8 / max(elapsed, 1e-9)), "elapsed_s": round(elapsed, 3), **tcp_totals,
            "object_bytes": p.object_bytes, "connects": connects, "connect_failures": connect_failures,
            "capture_build_s_mean": round(sum(producer.build_s) / len(producer.build_s), 3) if producer.build_s else None}


# ---------------------------------------------------------------- child process entry point

def child_main(params: dict, ctrl, status_q, stop) -> None:  # type: ignore[no-untyped-def]
    """Runs one crop/drone run in a spawned process; results go back through `status_q`."""

    p = RunParams(**params)

    def status(update: dict) -> None:
        with contextlib.suppress(queue.Full):
            status_q.put_nowait(("status", p.run_id, update))

    try:
        time.sleep(p.warmup_s)  # let the receiver synchronise its clock first (as sf_traffic send)
        started = P.now_ns()
        status({"phase": "running", "started_at_ns": started})
        loop = crop_loop if p.service == "crop" else drone_loop
        result = loop(p, ctrl, stop, status)
        time.sleep(0.5 if stop.is_set() else 1.0)
        record = summary_record(p, started, result)
        print(json_line(record), flush=True)
        T.post(p.report, record, wait=True)
        status_q.put(("summary", p.run_id, record))
    except Exception as error:  # noqa: BLE001 - report every failure to the node
        status_q.put(("error", p.run_id, {"error": f"{type(error).__name__}: {error}"}))
        raise


def json_line(record: dict) -> str:
    import json

    return json.dumps(record)

