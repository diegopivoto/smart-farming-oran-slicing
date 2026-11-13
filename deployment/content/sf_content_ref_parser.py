#!/usr/bin/env python3
"""Reference parser/receiver of the Smart Farming demo content (contract v1, docs/design/smartfarm-app-protocol.md).

Python stdlib only and deliberately independent of the generator code, so the Android app can be compared against
it field by field. It decodes:

  weather  UDP: 38 B header (magic SFT1, version 1, service 1, run_id, seq, generated_at_ns) | JSON up to the first NUL
  drone    UDP: header (service 3) | frame header !IHHI (frame_id, frag_index, frag_count, frame_len) | fragment;
           a frame is shown only when all its fragments arrived; frame_id 0xFFFFFFFF = filler
  crop     TCP: 96 B object header !4s16sQQ32sQ (SFO1, run_id, object_id, size, sha256, generated_at_ns) | payload;
           payload = SFIM container: "SFIM" | u32 version | u32 n_parts | n x (u32 name_len | name | u64 len | bytes)

  python3 sf_content_ref_parser.py listen [--bind 127.0.0.1] [--port-offset 0] [--out DIR] [--duration S]

prints one line per second per service (and one per crop object), and with --out keeps the last complete drone frame,
the last weather JSON and the parts of the last crop object for inspection. Loopback/lab use only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import struct
import sys
import threading
import time
from pathlib import Path

DGRAM = struct.Struct("!4sBB16sQQ")
OBJECT = struct.Struct("!4s16sQQ32sQ")
FRAME = struct.Struct("!IHHI")
DGRAM_MAGIC, OBJECT_MAGIC, SFIM_MAGIC = b"SFT1", b"SFO1", b"SFIM"
SERVICE_CODE = {"weather": 1, "crop": 2, "drone": 3}
PORTS = {"weather": 47001, "crop": 47002, "drone": 47003, "time": 47000, "control": 47010}
FILLER = 0xFFFFFFFF
FRAG = 1150
SFIM_PARTS = ("meta.json", "rgb.jpg", "ndvi.jpg", "bands.raw")


class ProtocolError(ValueError):
    pass


def parse_header(data: bytes) -> dict:
    if len(data) < DGRAM.size:
        raise ProtocolError("datagram shorter than the common header")
    magic, version, service, run_id, seq, generated = DGRAM.unpack_from(data)
    if magic != DGRAM_MAGIC or version != 1:
        raise ProtocolError(f"bad magic/version {magic!r}/{version}")
    return {"service": service, "run_id": run_id.hex(), "seq": seq, "generated_at_ns": generated, "size": len(data)}


def parse_weather(data: bytes) -> tuple[dict, dict]:
    header = parse_header(data)
    if header["service"] != 1:
        raise ProtocolError("not a weather datagram")
    if len(data) != 1250:
        raise ProtocolError(f"weather datagram is {len(data)} B, expected 1250")
    body = data[DGRAM.size:]
    end = body.find(b"\0")
    text = body if end < 0 else body[:end]
    if end >= 0 and any(body[end:]):
        raise ProtocolError("non-zero padding after the JSON")
    document = json.loads(text.decode("utf-8"))
    for key in ("v", "type", "station", "state", "readings", "thresholds", "alerts", "irrigation"):
        if key not in document:
            raise ProtocolError(f"weather JSON misses {key!r}")
    return header, document


def parse_drone(data: bytes) -> tuple[dict, dict]:
    header = parse_header(data)
    if header["service"] != 3:
        raise ProtocolError("not a drone datagram")
    if len(data) != 1200:
        raise ProtocolError(f"drone datagram is {len(data)} B, expected 1200")
    frame_id, index, count, length = FRAME.unpack_from(data, DGRAM.size)
    if frame_id == FILLER:
        return header, {"filler": True}
    start = DGRAM.size + FRAME.size
    if count == 0 or index >= count or count != -(-length // FRAG):
        raise ProtocolError(f"inconsistent fragment {index}/{count} of {length} B")
    piece = min(FRAG, length - index * FRAG)
    return header, {"filler": False, "frame_id": frame_id, "index": index, "count": count, "length": length,
                    "fragment": data[start:start + piece]}


class FrameAssembler:
    """Reassembles MJPEG frames; a frame with any missing fragment is dropped once newer frames are complete."""

    def __init__(self, keep: int = 6) -> None:
        self.partial: dict[int, dict] = {}
        self.keep = keep
        self.complete = 0
        self.dropped = 0
        self.newest = -1

    def add(self, fragment: dict) -> bytes | None:
        fid = fragment["frame_id"]
        entry = self.partial.setdefault(fid, {"count": fragment["count"], "length": fragment["length"], "parts": {}})
        entry["parts"][fragment["index"]] = fragment["fragment"]
        self.newest = max(self.newest, fid)
        frame = None
        if len(entry["parts"]) == entry["count"]:
            frame = b"".join(entry["parts"][i] for i in range(entry["count"]))
            del self.partial[fid]
            if len(frame) != entry["length"]:
                raise ProtocolError("reassembled frame length mismatch")
            self.complete += 1
        for old in [f for f in self.partial if f < self.newest - self.keep]:
            del self.partial[old]
            self.dropped += 1
        return frame


def read_exact(conn: socket.socket, size: int) -> bytes | None:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = conn.recv(min(1 << 20, size - len(buffer)))
        if not chunk:
            return None
        buffer += chunk
    return bytes(buffer)


def parse_object_header(header: bytes) -> dict:
    magic, run_id, object_id, size, digest, generated = OBJECT.unpack(header)
    if magic != OBJECT_MAGIC:
        raise ProtocolError(f"bad object magic {magic!r}")
    return {"run_id": run_id.hex(), "object_id": object_id, "size": size, "sha256": digest,
            "generated_at_ns": generated}


def parse_sfim(payload: bytes) -> dict:
    if payload[:4] != SFIM_MAGIC:
        raise ProtocolError("payload is not an SFIM container")
    version, n_parts = struct.unpack_from("!II", payload, 4)
    if version != 1:
        raise ProtocolError(f"SFIM version {version}")
    offset, parts = 12, {}
    names = []
    for _ in range(n_parts):
        (name_len,) = struct.unpack_from("!I", payload, offset)
        offset += 4
        name = payload[offset:offset + name_len].decode("utf-8")
        offset += name_len
        (length,) = struct.unpack_from("!Q", payload, offset)
        offset += 8
        if offset + length > len(payload):
            raise ProtocolError(f"part {name} overruns the container")
        parts[name] = payload[offset:offset + length]
        names.append(name)
        offset += length
    if offset != len(payload):
        raise ProtocolError(f"{len(payload) - offset} trailing bytes after the last part")
    if tuple(names) != SFIM_PARTS:
        raise ProtocolError(f"unexpected parts/order {names}")
    meta = json.loads(parts["meta.json"].decode("utf-8"))
    for key in ("v", "capture_id", "field", "state", "bands", "width", "height", "ndvi_mean", "zones", "captured_at"):
        if key not in meta:
            raise ProtocolError(f"meta.json misses {key!r}")
    for name in ("rgb.jpg", "ndvi.jpg"):
        if parts[name][:2] != b"\xff\xd8":
            raise ProtocolError(f"{name} is not a JPEG")
    if len(parts["bands.raw"]) < len(meta["bands"]) * meta["width"] * meta["height"]:
        raise ProtocolError("bands.raw smaller than bands x width x height")
    return {"meta": meta, "parts": parts}


def jpeg_size(data: bytes) -> tuple[int, int]:
    """(width, height) from the first SOF marker of a JPEG."""

    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            raise ProtocolError("bad JPEG marker")
        marker = data[i + 1]
        length = struct.unpack_from("!H", data, i + 2)[0]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack_from("!HH", data, i + 5)
            return width, height
        i += 2 + length
    raise ProtocolError("no SOF marker")


class Listener:
    """Loopback receiver used by the end-to-end test and by hand (`listen`)."""

    def __init__(self, bind: str = "127.0.0.1", port_offset: int = 0, out: Path | None = None) -> None:
        self.bind = bind
        self.ports = {k: v + port_offset for k, v in PORTS.items()}
        self.out = out
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.weather: list[tuple[dict, dict]] = []
        self.drone_datagrams = self.drone_fillers = self.drone_bytes = 0
        self.drone_seq: list[int] = []
        self.frames: list[tuple[int, int]] = []  # (arrival_ns, frame bytes)
        self.last_frame: bytes | None = None
        self.assembler = FrameAssembler()
        self.objects: list[dict] = []
        self.errors: list[str] = []
        self.sockets: list[socket.socket] = []

    def start(self) -> "Listener":
        for name, target in (("weather", self._weather), ("drone", self._drone), ("crop", self._crop)):
            threading.Thread(target=target, name=f"ref-{name}", daemon=True).start()
        time.sleep(0.2)
        return self

    def close(self) -> None:
        self.stop.set()
        for s in self.sockets:
            try:
                s.close()
            except OSError:
                pass

    def _udp(self, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        sock.bind((self.bind, port))
        sock.settimeout(0.3)
        self.sockets.append(sock)
        return sock

    def _weather(self) -> None:
        sock = self._udp(self.ports["weather"])
        while not self.stop.is_set():
            try:
                data = sock.recv(65535)
            except (TimeoutError, OSError):
                continue
            try:
                parsed = parse_weather(data)
            except (ProtocolError, ValueError) as error:
                self.errors.append(f"weather: {error}")
                continue
            with self.lock:
                self.weather.append(parsed)
            if self.out:
                (self.out / "weather_last.json").write_text(json.dumps(parsed[1], indent=1, ensure_ascii=False))

    def _drone(self) -> None:
        sock = self._udp(self.ports["drone"])
        while not self.stop.is_set():
            try:
                data = sock.recv(65535)
            except (TimeoutError, OSError):
                continue
            try:
                header, fragment = parse_drone(data)
            except ProtocolError as error:
                self.errors.append(f"drone: {error}")
                continue
            with self.lock:
                self.drone_datagrams += 1
                self.drone_bytes += len(data)
                self.drone_seq.append(header["seq"])
                if fragment["filler"]:
                    self.drone_fillers += 1
                    continue
                frame = self.assembler.add(fragment)
                if frame is not None:
                    self.frames.append((time.time_ns(), len(frame)))
                    self.last_frame = frame
            if frame is not None and self.out and len(self.frames) % 15 == 0:
                (self.out / "drone_last.jpg").write_bytes(frame)

    def _crop(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.bind, self.ports["crop"]))
        server.listen(4)
        server.settimeout(0.3)
        self.sockets.append(server)
        while not self.stop.is_set():
            try:
                conn, _ = server.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._crop_conn, args=(conn,), daemon=True).start()

    def _crop_conn(self, conn: socket.socket) -> None:
        with conn:
            while not self.stop.is_set():
                try:
                    raw = read_exact(conn, OBJECT.size)
                    if raw is None:
                        return
                    header = parse_object_header(raw)
                    first = time.time_ns()
                    payload = read_exact(conn, header["size"])
                except ConnectionResetError:
                    return  # the generator resets at its hard deadline: the incomplete object is dropped
                except (OSError, ProtocolError) as error:
                    self.errors.append(f"crop: {error}")
                    return
                if payload is None:
                    return
                record = {**header, "sha256_valid": hashlib.sha256(payload).digest() == header["sha256"],
                          "receive_time_s": (time.time_ns() - first) / 1e9}
                record.pop("sha256")
                try:
                    container = parse_sfim(payload)
                    record["meta"] = container["meta"]
                    record["rgb_size"] = jpeg_size(container["parts"]["rgb.jpg"])
                    record["ndvi_size"] = jpeg_size(container["parts"]["ndvi.jpg"])
                    record["bands_bytes"] = len(container["parts"]["bands.raw"])
                    if self.out:
                        for name, data in container["parts"].items():
                            if name != "bands.raw":
                                (self.out / f"crop_last_{name}").write_bytes(data)
                except (ProtocolError, ValueError) as error:
                    record["error"] = str(error)
                with self.lock:
                    self.objects.append(record)
                print(json.dumps({"crop_object": {k: v for k, v in record.items() if k != "meta"},
                                  "capture_id": record.get("meta", {}).get("capture_id"),
                                  "state": record.get("meta", {}).get("state")}), flush=True)


def cmd_listen(args: argparse.Namespace) -> int:
    out = Path(args.out) if args.out else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    listener = Listener(args.bind, args.port_offset, out).start()
    print(f"reference parser listening on {args.bind}: {listener.ports}", flush=True)
    end = time.monotonic() + args.duration if args.duration else float("inf")
    last = (0, 0, 0, 0)
    try:
        while time.monotonic() < end:
            time.sleep(1.0)
            with listener.lock:
                w = len(listener.weather)
                state = listener.weather[-1][1]["state"] if listener.weather else "-"
                alerts = [a["id"] for a in listener.weather[-1][1]["alerts"]] if listener.weather else []
                now = (w, listener.drone_datagrams, listener.drone_bytes, len(listener.frames))
                fillers, dropped = listener.drone_fillers, listener.assembler.dropped
            print(f"weather {now[0] - last[0]}/s state={state} alerts={alerts} | drone "
                  f"{(now[2] - last[2]) * 8 / 1e6:.2f} Mbit/s, {now[1] - last[1]} dgrams, {now[3] - last[3]} fps "
                  f"(fillers total {fillers}, dropped frames {dropped}) | crop objects {len(listener.objects)} | "
                  f"errors {len(listener.errors)}", flush=True)
            last = now
    except KeyboardInterrupt:
        pass
    listener.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Smart Farming content reference parser")
    sub = parser.add_subparsers(dest="command", required=True)
    listen = sub.add_parser("listen")
    listen.add_argument("--bind", default="127.0.0.1")
    listen.add_argument("--port-offset", type=int, default=0)
    listen.add_argument("--out")
    listen.add_argument("--duration", type=float)
    args = parser.parse_args()
    return cmd_listen(args)


if __name__ == "__main__":
    sys.exit(main())
