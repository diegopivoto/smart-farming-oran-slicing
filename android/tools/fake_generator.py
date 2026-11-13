#!/usr/bin/env python3
"""Desktop fake of the PC3 content generator, written from docs/design/smartfarm-app-protocol.md (contract v1) only.

It exists to sanity-check the Android app's parsers on a desktop (JVM test FakeGeneratorIT) without the phone or the
lab: it sends protocol-conformant weather / crop / drone traffic to 127.0.0.1 and runs the clock (UDP) and control
(TCP) services. It is NOT the lab generator (that one is deployment/content/sf_content.py) and must never be pointed
at the 5G network.

    python3 android/tools/fake_generator.py --service all --duration 8 --port-base 57000

Ports are port-base + 0 (clock), +1 (weather), +2 (crop), +3 (drone), +10 (control), so the default 47000 matches the
contract. Needs Pillow for the JPEG frames/captures. English output, one line per event.
"""

import argparse
import hashlib
import io
import json
import math
import os
import socket
import struct
import threading
import time
import uuid

DGRAM = struct.Struct("!4sBB16sQQ")
FRAME = struct.Struct("!IHHI")
OBJECT = struct.Struct("!4s16sQQ32sQ")
TIMEQ = struct.Struct("!4sQ")
TIMER = struct.Struct("!4sQQ")
FRAGMENT = 1150
DRONE_DGRAM = 1200
WEATHER_DGRAM = 1250
FILLER_ID = 0xFFFFFFFF


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class World:
    """Weather state machine shared by the sender and the control server."""

    def __init__(self, state: str) -> None:
        self.lock = threading.Lock()
        self.state = state
        self.irrigation_until = 0.0
        self.r = {"air_temperature_c": 24.3, "air_humidity_pct": 66.0, "soil_moisture_pct": 38.5, "rain_rate_mm_h": 0.0,
                  "rain_today_mm": 4.2, "wind_speed_kmh": 7.8, "wind_direction_deg": 135.0,
                  "solar_radiation_w_m2": 610.0, "uv_index": 5.1, "pressure_hpa": 1012.6, "leaf_wetness_pct": 12.0}

    def step(self, dt: float) -> dict:
        with self.lock:
            irrigating = time.monotonic() < self.irrigation_until
            goal = {"stable": {"soil_moisture_pct": 38, "air_humidity_pct": 62, "rain_rate_mm_h": 0, "wind_speed_kmh": 8,
                               "pressure_hpa": 1013},
                    "drought": {"soil_moisture_pct": 22, "air_humidity_pct": 32, "rain_rate_mm_h": 0, "wind_speed_kmh": 12,
                                "pressure_hpa": 1015},
                    "storm": {"soil_moisture_pct": 48, "air_humidity_pct": 92, "rain_rate_mm_h": 35, "wind_speed_kmh": 55,
                              "pressure_hpa": 998}}[self.state if not irrigating else "stable"]
            if irrigating:
                goal = dict(goal, soil_moisture_pct=42)
            for k, v in goal.items():  # smooth drift (~20-30 s to settle)
                self.r[k] += (v - self.r[k]) * min(1.0, dt / 8)
            self.r["rain_today_mm"] += self.r["rain_rate_mm_h"] * dt / 3600
            self.r["wind_direction_deg"] = (self.r["wind_direction_deg"] + dt * 6) % 360
            alerts = []
            if self.r["soil_moisture_pct"] < 30:
                alerts.append({"id": "dry_soil", "severity": "warning",
                               "text": "Soil moisture below 30 % — irrigation recommended", "action": "irrigation"})
            if self.r["rain_rate_mm_h"] > 20:
                alerts.append({"id": "excess_rain", "severity": "warning", "text": "Heavy rain above 20 mm/h"})
            state = self.state
            remaining = max(0.0, self.irrigation_until - time.monotonic())
            if irrigating and remaining <= 0:
                self.state = "stable"
        return {"v": 1, "type": "weather", "station": "field-A", "state": state,
                "readings": {k: round(v, 1) for k, v in self.r.items()},
                "thresholds": {"soil_moisture_pct": {"min": 30}, "air_humidity_pct": {"min": 40}, "rain_rate_mm_h": {"max": 20}},
                "alerts": alerts, "irrigation": {"active": irrigating, "remaining_s": round(remaining)}}


def time_responder(bind: str, port: int, stop: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind, port))
    sock.settimeout(0.3)
    while not stop.is_set():
        try:
            data, peer = sock.recvfrom(64)
        except (TimeoutError, socket.timeout):
            continue
        if len(data) == TIMEQ.size and data[:4] == b"SFTQ":
            sock.sendto(TIMER.pack(b"SFTR", TIMEQ.unpack(data)[1], time.time_ns()), peer)


def control_server(bind: str, port: int, world: World, stop: threading.Event) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind, port))
    srv.listen(4)
    srv.settimeout(0.3)
    while not stop.is_set():
        try:
            conn, peer = srv.accept()
        except (TimeoutError, socket.timeout):
            continue
        with conn, conn.makefile("rwb") as f:
            for line in f:
                try:
                    req = json.loads(line)
                except ValueError:
                    reply = {"v": 1, "ok": False, "cmd": None, "detail": "bad json"}
                else:
                    cmd = req.get("cmd")
                    with world.lock:
                        if cmd == "irrigation" and req.get("action") == "activate":
                            world.irrigation_until = time.monotonic() + float(req.get("duration_s", 60))
                            reply = {"v": 1, "ok": True, "cmd": cmd, "detail": f"irrigation on for {int(req.get('duration_s', 60))} s"}
                        elif cmd == "state" and req.get("state") in ("stable", "drought", "storm"):
                            world.state = req["state"]
                            reply = {"v": 1, "ok": True, "cmd": cmd, "detail": f"weather state {world.state}"}
                        else:
                            reply = {"v": 1, "ok": False, "cmd": cmd, "detail": "unsupported"}
                log(f"control {peer[0]}: {line.strip().decode(errors='replace')} -> {reply['detail']}")
                f.write((json.dumps(reply) + "\n").encode())
                f.flush()


def send_weather(target: str, port: int, duration: float, run_id: bytes, world: World) -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq, start = 0, time.monotonic()
    while time.monotonic() - start < duration:
        payload = json.dumps(world.step(0.1)).encode()
        dgram = (DGRAM.pack(b"SFT1", 1, 1, run_id, seq, time.time_ns()) + payload).ljust(WEATHER_DGRAM, b"\0")
        assert len(dgram) == WEATHER_DGRAM
        sock.sendto(dgram, (target, port))
        seq += 1
        time.sleep(max(0.0, start + seq * 0.1 - time.monotonic()))
    return {"sent_packets": seq}


def make_frames(count: int, width: int, height: int, quality: int) -> list[bytes]:
    from PIL import Image, ImageDraw

    frames = []
    for i in range(count):
        img = Image.new("RGB", (width, height), (95, 158, 69))
        d = ImageDraw.Draw(img)
        shift = i * width // (count * 4)
        for x in range(-width, width * 2, max(8, width // 24)):
            d.line([(x + shift, height), (x + shift + width // 3, 0)], fill=(63, 122, 53), width=max(2, width // 60))
        d.rectangle([width * 0.6, height * 0.45, width * 0.78, height * 0.7], outline=(255, 210, 74), width=3)
        d.text((12, height - 20), f"ALT 42 m  SPD 8.4 m/s  BAT 76 %  frame {i}", fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        frames.append(buf.getvalue())
    return frames


def send_drone(target: str, port: int, duration: float, run_id: bytes, rate_bps: int, frames: list[bytes], fps: int,
               drop_every: int) -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    interval = DRONE_DGRAM * 8 / rate_bps
    seq, frame_id, sent_frames, fillers, dropped = 0, 0, 0, 0, 0
    start = time.monotonic()
    queue: list[bytes] = []
    next_frame = start
    next_send = start
    while time.monotonic() - start < duration:
        now = time.monotonic()
        if now >= next_frame:  # a new frame every 1/fps: queue its fragments
            jpeg = frames[frame_id % len(frames)]
            count = (len(jpeg) + FRAGMENT - 1) // FRAGMENT
            for i in range(count):
                body = FRAME.pack(frame_id, i, count, len(jpeg)) + jpeg[i * FRAGMENT:(i + 1) * FRAGMENT]
                queue.append(body)
            frame_id += 1
            sent_frames += 1
            next_frame += 1 / fps
        if now < next_send:
            time.sleep(min(next_send - now, 0.001))
            continue
        burst = max(1, int((now - next_send) / interval) + 1)
        for _ in range(min(burst, 64)):
            if queue:
                body = queue.pop(0)
            else:  # nothing left of the current frame: filler keeps the CBR
                body = FRAME.pack(FILLER_ID, 0, 0, 0)
                fillers += 1
            dgram = (DGRAM.pack(b"SFT1", 1, 3, run_id, seq, time.time_ns()) + body).ljust(DRONE_DGRAM, b"\0")
            seq += 1
            next_send += interval
            if drop_every and seq % drop_every == 0:
                dropped += 1  # simulated loss: counts as sent, never reaches the socket
                continue
            sock.sendto(dgram, (target, port))
    return {"sent_packets": seq, "frames": sent_frames, "fillers": fillers, "simulated_drops": dropped}


def make_capture(object_id: int, size: int, state: str) -> bytes:
    from PIL import Image, ImageDraw

    w, h = 2048, 1152
    rgb = Image.new("RGB", (w, h), (94, 158, 69))
    d = ImageDraw.Draw(rgb)
    for y in range(12, h, 56):
        d.rectangle([0, y, w, y + 14], fill=(122, 90, 58))
    zones = []
    if state == "pest":
        d.ellipse([w * 0.55, h * 0.18, w * 0.75, h * 0.45], fill=(195, 160, 68))
        zones.append({"x": 0.55, "y": 0.18, "w": 0.2, "h": 0.27, "label": "Pest hotspot", "severity": "high"})
    ndvi = rgb.convert("L").point(lambda v: 255 - v).convert("RGB")
    parts = []
    for name, img in (("rgb.jpg", rgb), ("ndvi.jpg", ndvi)):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        parts.append((name, buf.getvalue()))
    meta = json.dumps({"v": 1, "capture_id": object_id, "field": "field-A", "state": state,
                       "bands": ["blue", "green", "red", "red_edge", "nir"], "width": w, "height": h,
                       "ndvi_mean": 0.71 if state == "healthy" else 0.48, "zones": zones,
                       "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}).encode()
    parts.insert(0, ("meta.json", meta))

    def part(name: str, data: bytes) -> bytes:
        n = name.encode()
        return struct.pack("!I", len(n)) + n + struct.pack("!Q", len(data)) + data

    head = b"SFIM" + struct.pack("!II", 1, 4) + b"".join(part(n, b) for n, b in parts)
    bands_header = 4 + len(b"bands.raw") + 8
    bands_len = size - len(head) - bands_header
    if bands_len < 0:
        raise ValueError(f"object size {size} too small for the images ({len(head)} B)")
    return head + part("bands.raw", os.urandom(bands_len))


def send_crop(target: str, port: int, duration: float, run_id: bytes, rate_bps: int, size: int, corrupt_every: int) -> dict:
    sock = socket.create_connection((target, port), timeout=10)
    start = time.monotonic()
    sent, sent_bytes = 0, 0
    states = ["healthy", "pest", "water_stress"]
    while time.monotonic() - start < duration:
        body = make_capture(sent, size, states[sent % 3])
        digest = hashlib.sha256(body).digest()
        if corrupt_every and (sent + 1) % corrupt_every == 0:
            body = body[:-1] + bytes([body[-1] ^ 0xFF])  # the receiver must reject this one
        sock.sendall(OBJECT.pack(b"SFO1", run_id, sent, size, digest, time.time_ns()))
        for off in range(0, size, 64 * 1024):
            sock.sendall(body[off:off + 64 * 1024])
            sent_bytes += min(64 * 1024, size - off)
            ahead = sent_bytes * 8 / rate_bps - (time.monotonic() - start)
            if ahead > 0:
                time.sleep(ahead)
        sent += 1
        log(f"crop object {sent - 1} sent ({size} B)")
    sock.close()
    return {"sent_objects": sent}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", default="127.0.0.1")
    ap.add_argument("--bind", default="127.0.0.1", help="address of the clock/control services")
    ap.add_argument("--service", choices=["weather", "crop", "drone", "all"], default="all")
    ap.add_argument("--duration", type=float, default=8)
    ap.add_argument("--port-base", type=int, default=47000)
    ap.add_argument("--state", default="drought", choices=["stable", "drought", "storm"])
    ap.add_argument("--drone-rate-bps", type=int, default=20_000_000)
    ap.add_argument("--drone-size", default="640x360")
    ap.add_argument("--drone-fps", type=int, default=30)
    ap.add_argument("--drone-drop-every", type=int, default=0, help="simulate the loss of every N-th datagram")
    ap.add_argument("--crop-rate-bps", type=int, default=200_000_000)
    ap.add_argument("--crop-object-bytes", type=int, default=3 * 1024 * 1024)
    ap.add_argument("--crop-corrupt-every", type=int, default=0)
    ap.add_argument("--warmup-s", type=float, default=0.5)
    args = ap.parse_args()
    if args.target not in ("127.0.0.1", "localhost"):
        raise SystemExit("fake_generator.py only sends to 127.0.0.1 (desktop sanity checks)")
    base = args.port_base
    stop = threading.Event()
    world = World(args.state)
    threading.Thread(target=time_responder, args=(args.bind, base, stop), daemon=True).start()
    threading.Thread(target=control_server, args=(args.bind, base + 10, world, stop), daemon=True).start()
    time.sleep(args.warmup_s)
    services = ["weather", "crop", "drone"] if args.service == "all" else [args.service]
    results: dict[str, dict] = {}
    threads = []
    for svc in services:
        run_id = uuid.uuid4().bytes
        if svc == "weather":
            fn = lambda r=run_id: results.__setitem__("weather", send_weather(args.target, base + 1, args.duration, r, world))
        elif svc == "crop":
            fn = lambda r=run_id: results.__setitem__("crop", send_crop(args.target, base + 2, args.duration, r, args.crop_rate_bps,
                                                                          args.crop_object_bytes, args.crop_corrupt_every))
        else:
            w, h = (int(v) for v in args.drone_size.split("x"))
            frames = make_frames(30, w, h, 85)
            fn = lambda r=run_id, fr=frames: results.__setitem__("drone", send_drone(args.target, base + 3, args.duration, r,
                                                                                     args.drone_rate_bps, fr, args.drone_fps,
                                                                                     args.drone_drop_every))
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    time.sleep(0.5)
    stop.set()
    print(json.dumps({"kind": "fake_generator_summary", **results}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
