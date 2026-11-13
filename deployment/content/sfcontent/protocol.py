"""Wire format of the Smart Farming demo content (docs/design/smartfarm-app-protocol.md, contract v1.1). Stdlib only.

The common datagram header, the crop object header, ports and default rates are imported from
deployment/traffic/sf_traffic.py, so the content traffic is measured exactly like the calibration traffic.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE.parent, _HERE.parent.parent / "traffic"):  # PC3 install dir, or the repo layout
    if (_candidate / "sf_traffic.py").exists() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))
        break

import sf_traffic as T  # noqa: E402

DGRAM = T.DGRAM
OBJECT = T.OBJECT
TIMEQ, TIMER = T.TIMEQ, T.TIMER
SERVICES = T.SERVICES
DEFAULT_RATE = T.DEFAULT_RATE
CROP_OBJECT_BYTES = T.CROP_OBJECT_BYTES
MAGIC_DGRAM = b"SFT1"
MAGIC_OBJECT = b"SFO1"
VERSION = 1

PORTS = {"time": T.TIME_PORT, "weather": T.PORTS["weather"], "crop": T.PORTS["crop"], "drone": T.PORTS["drone"],
         "control": 47010}

WEATHER_DGRAM_BYTES = T.DEFAULT_SIZE["weather"]  # 1250
DRONE_DGRAM_BYTES = T.DEFAULT_SIZE["drone"]  # 1200
FRAME = struct.Struct("!IHHI")  # frame_id, frag_index, frag_count, frame_len
FRAG_PAYLOAD = DRONE_DGRAM_BYTES - DGRAM.size - FRAME.size  # 1150
FILLER_FRAME_ID = 0xFFFFFFFF

SFIM_MAGIC = b"SFIM"
SFIM_HEAD = struct.Struct("!4sII")  # magic, version, n_parts
SFIM_NAME_LEN = struct.Struct("!I")
SFIM_PART_LEN = struct.Struct("!Q")
SFIM_PART_ORDER = ("meta.json", "rgb.jpg", "ndvi.jpg", "bands.raw")


def ports_with_offset(offset: int = 0) -> dict[str, int]:
    """Contract ports; a non-zero offset is only for loopback tests on a shared machine."""

    return {name: port + offset for name, port in PORTS.items()}


def now_ns() -> int:
    return time.time_ns()


# ---------------------------------------------------------------- weather

def weather_datagram(run_id: bytes, seq: int, generated_at_ns: int, document: dict) -> bytes:
    """Common header | UTF-8 JSON | NUL padding, exactly 1250 B."""

    body = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    room = WEATHER_DGRAM_BYTES - DGRAM.size
    if len(body) > room:
        raise ValueError(f"weather JSON is {len(body)} B, only {room} B fit")
    header = DGRAM.pack(MAGIC_DGRAM, VERSION, SERVICES["weather"], run_id, seq, generated_at_ns)
    return header + body + bytes(room - len(body))


# ---------------------------------------------------------------- drone (MJPEG fragments)

def frame_tails(frame_id: int, jpeg: bytes) -> list[bytes]:
    """Frame header + fragment + zero padding for every fragment of one JPEG (the common header goes in front)."""

    length = len(jpeg)
    count = max(1, -(-length // FRAG_PAYLOAD))
    if count > 0xFFFF:
        raise ValueError("frame too large")
    room = DRONE_DGRAM_BYTES - DGRAM.size - FRAME.size
    tails = []
    for index in range(count):
        chunk = jpeg[index * FRAG_PAYLOAD:(index + 1) * FRAG_PAYLOAD]
        tails.append(FRAME.pack(frame_id, index, count, length) + chunk + bytes(room - len(chunk)))
    return tails


FILLER_TAIL = FRAME.pack(FILLER_FRAME_ID, 0, 0, 0) + bytes(DRONE_DGRAM_BYTES - DGRAM.size - FRAME.size)


def drone_datagram(run_id: bytes, seq: int, generated_at_ns: int, tail: bytes) -> bytes:
    return DGRAM.pack(MAGIC_DGRAM, VERSION, SERVICES["drone"], run_id, seq, generated_at_ns) + tail


# ---------------------------------------------------------------- crop (SFIM container inside an SFO1 object)

def sfim_overhead(parts: list[tuple[str, int]]) -> int:
    return SFIM_HEAD.size + sum(SFIM_NAME_LEN.size + len(name.encode()) + SFIM_PART_LEN.size for name, _ in parts)


def build_sfim(meta: dict, rgb_jpeg: bytes, ndvi_jpeg: bytes, bands: bytes, size: int) -> bytes:
    """Container of exactly `size` bytes: bands.raw is zero-padded (or cropped) to fill the remainder."""

    meta_bytes = json.dumps(meta, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    fixed = [("meta.json", meta_bytes), ("rgb.jpg", rgb_jpeg), ("ndvi.jpg", ndvi_jpeg)]
    overhead = sfim_overhead([(n, len(b)) for n, b in fixed] + [("bands.raw", 0)])
    bands_len = size - overhead - sum(len(b) for _, b in fixed)
    if bands_len < 0:
        raise ValueError("images do not fit into the object size")
    if len(bands) > bands_len:
        bands = bands[:bands_len]
    pieces = [SFIM_HEAD.pack(SFIM_MAGIC, VERSION, 4)]
    for name, data in fixed:
        encoded = name.encode()
        pieces += [SFIM_NAME_LEN.pack(len(encoded)), encoded, SFIM_PART_LEN.pack(len(data)), data]
    encoded = b"bands.raw"
    pieces += [SFIM_NAME_LEN.pack(len(encoded)), encoded, SFIM_PART_LEN.pack(bands_len), bands,
               bytes(bands_len - len(bands))]
    payload = b"".join(pieces)
    assert len(payload) == size
    return payload


def replace_meta(payload: bytes, meta: dict) -> bytes:
    """The same SFIM container with a new meta.json (the first part), still exactly `len(payload)` bytes.

    rgb.jpg and ndvi.jpg are kept byte for byte; bands.raw (the last part) absorbs the size change by losing or
    gaining zero padding at its end, as `build_sfim` does. Used to stamp send-time fields (the TCP retransmission
    ratio so far) into an object that was built ahead of time."""

    magic, version, n_parts = SFIM_HEAD.unpack_from(payload, 0)
    if magic != SFIM_MAGIC or n_parts != len(SFIM_PART_ORDER):
        raise ValueError("not an SFIM container with the contract parts")
    spans = []  # (name, part start, data start, data length)
    offset = SFIM_HEAD.size
    for _ in range(n_parts):
        (name_len,) = SFIM_NAME_LEN.unpack_from(payload, offset)
        name = bytes(payload[offset + SFIM_NAME_LEN.size:offset + SFIM_NAME_LEN.size + name_len]).decode()
        (length,) = SFIM_PART_LEN.unpack_from(payload, offset + SFIM_NAME_LEN.size + name_len)
        start = offset + SFIM_NAME_LEN.size + name_len + SFIM_PART_LEN.size
        spans.append((name, offset, start, length))
        offset = start + length
    if tuple(name for name, *_ in spans) != SFIM_PART_ORDER or offset != len(payload):
        raise ValueError("SFIM parts out of the contract order or size")
    meta_bytes = json.dumps(meta, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    _, meta_part, meta_start, meta_len = spans[0]
    _, bands_part, bands_start, bands_len = spans[-1]
    new_bands_len = bands_len - (len(meta_bytes) - meta_len)
    if new_bands_len < 0:
        raise ValueError("meta.json does not fit into the object size")
    view = memoryview(payload)
    kept = min(bands_len, new_bands_len)
    meta_name, bands_name = b"meta.json", b"bands.raw"
    pieces = [view[:meta_part], SFIM_NAME_LEN.pack(len(meta_name)), meta_name, SFIM_PART_LEN.pack(len(meta_bytes)),
              meta_bytes, view[meta_start + meta_len:bands_part], SFIM_NAME_LEN.pack(len(bands_name)), bands_name,
              SFIM_PART_LEN.pack(new_bands_len), view[bands_start:bands_start + kept], bytes(new_bands_len - kept)]
    out = b"".join(pieces)
    assert len(out) == len(payload)
    return out


def object_header(run_id: bytes, object_id: int, payload_size: int, sha256: bytes, generated_at_ns: int) -> bytes:
    return OBJECT.pack(MAGIC_OBJECT, run_id, object_id, payload_size, sha256, generated_at_ns)


# ---------------------------------------------------------------- clock service (same as sf_traffic.time_responder)

def time_responder(stop: threading.Event, port: int, bind: str = "0.0.0.0") -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind, port))
    sock.settimeout(0.5)
    while not stop.is_set():
        try:
            data, peer = sock.recvfrom(64)
        except TimeoutError:
            continue
        except OSError:
            break
        if len(data) == TIMEQ.size and data[:4] == b"SFTQ":
            sock.sendto(TIMER.pack(b"SFTR", TIMEQ.unpack(data)[1], now_ns()), peer)
    sock.close()


def default_node_socket() -> str:
    return "/run/smartfarm-content.sock" if os.geteuid() == 0 else f"/tmp/smartfarm-content-{os.getuid()}.sock"
