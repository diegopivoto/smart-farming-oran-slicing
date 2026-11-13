import hashlib
import io
import json
import os
import struct

import pytest
from PIL import Image

import sf_content_ref_parser as ref
from sfcontent import protocol as P
from sfcontent.crop import OUT_H, OUT_W, TCP_SO_FAR_KEYS, CropFactory, bands_from, restamp

RUN = bytes.fromhex("0123456789abcdef0123456789abcdef")


def test_common_header_matches_sf_traffic() -> None:
    assert P.DGRAM.format == "!4sBB16sQQ" and P.DGRAM.size == 38
    assert P.OBJECT.format == "!4s16sQQ32sQ"
    assert P.MAGIC_DGRAM == b"SFT1" and P.MAGIC_OBJECT == b"SFO1"
    assert P.PORTS == {"time": 47000, "weather": 47001, "crop": 47002, "drone": 47003, "control": 47010}
    assert P.DEFAULT_RATE == {"weather": 100_000, "crop": 20_000_000, "drone": 40_000_000}
    assert P.CROP_OBJECT_BYTES == 26_214_400


def test_weather_datagram_layout() -> None:
    doc = {"v": 1, "type": "weather", "station": "field-A", "state": "stable", "readings": {}, "thresholds": {},
           "alerts": [{"id": "x", "text": "Soil moisture below 30 % — irrigation recommended"}],
           "irrigation": {"active": False, "remaining_s": 0}}
    data = P.weather_datagram(RUN, 42, 1_793_000_000_000_000_000, doc)
    header, parsed = ref.parse_weather(data)
    assert len(data) == 1250 and parsed == doc
    assert header == {"service": 1, "run_id": RUN.hex(), "seq": 42, "generated_at_ns": 1_793_000_000_000_000_000,
                      "size": 1250}
    magic, version, service, run_id, seq, ts = struct.unpack_from("!4sBB16sQQ", data)
    assert (magic, version, service, run_id, seq) == (b"SFT1", 1, 1, RUN, 42)
    with pytest.raises(ValueError):
        P.weather_datagram(RUN, 0, 0, {"pad": "x" * 1300})


@pytest.mark.parametrize("length", [1, 1149, 1150, 1151, 2300, 163_457])
def test_mjpeg_fragmentation_and_reassembly(length: int) -> None:
    jpeg = os.urandom(length)
    tails = P.frame_tails(77, jpeg)
    assert len(tails) == -(-length // 1150)
    datagrams = [P.drone_datagram(RUN, i, 1000 + i, t) for i, t in enumerate(tails)]
    assert all(len(d) == 1200 for d in datagrams)
    assembler = ref.FrameAssembler()
    frames = []
    for d in reversed(datagrams):  # any order
        header, fragment = ref.parse_drone(d)
        assert header["service"] == 3 and not fragment["filler"]
        assert (fragment["frame_id"], fragment["count"], fragment["length"]) == (77, len(tails), length)
        frame = assembler.add(fragment)
        if frame:
            frames.append(frame)
    assert frames == [jpeg]
    frame_id, index, count, frame_len = struct.unpack_from("!IHHI", datagrams[-1], 38)
    assert (frame_id, index, count, frame_len) == (77, len(tails) - 1, len(tails), length)
    last_piece = length - (len(tails) - 1) * 1150
    assert not any(datagrams[-1][50 + last_piece:])  # zero padding


def test_filler_and_lost_fragment() -> None:
    filler = P.drone_datagram(RUN, 9, 0, P.FILLER_TAIL)
    assert len(filler) == 1200 and ref.parse_drone(filler)[1] == {"filler": True}
    assert struct.unpack_from("!IHHI", filler, 38) == (0xFFFFFFFF, 0, 0, 0)
    assembler = ref.FrameAssembler(keep=2)
    for fid in range(6):
        tails = P.frame_tails(fid, os.urandom(3000))
        for i, t in enumerate(tails):
            if fid == 1 and i == 1:
                continue  # lost datagram: that one frame is dropped, the others are complete
            assembler.add(ref.parse_drone(P.drone_datagram(RUN, 0, 0, t))[1])
    assert assembler.complete == 5 and assembler.dropped == 1


def test_sfim_container_exact_size_and_sha(media) -> None:  # type: ignore[no-untyped-def]
    factory = CropFactory(media, seed=1)
    for state in ("healthy", "pest", "water_stress"):
        obj = factory.build(state)
        assert len(obj.payload) == 26_214_400
        assert obj.sha256 == hashlib.sha256(obj.payload).digest()
        header = P.object_header(RUN, 3, len(obj.payload), obj.sha256, 99)
        parsed_header = ref.parse_object_header(header)
        assert parsed_header["size"] == 26_214_400 and parsed_header["object_id"] == 3
        container = ref.parse_sfim(obj.payload)
        meta = container["meta"]
        assert meta["state"] == state and meta["bands"] == ["blue", "green", "red", "red_edge", "nir"]
        assert (meta["width"], meta["height"]) == (OUT_W, OUT_H)
        assert ref.jpeg_size(container["parts"]["rgb.jpg"]) == (OUT_W, OUT_H) and OUT_W >= 2048
        assert ref.jpeg_size(container["parts"]["ndvi.jpg"]) == (OUT_W, OUT_H)
        assert len(container["parts"]["bands.raw"]) >= 5 * OUT_W * OUT_H
        assert (len(meta["zones"]) > 0) == (state != "healthy")
        for zone in meta["zones"]:
            assert 0 <= zone["x"] <= 1 and 0 <= zone["y"] <= 1 and 0 < zone["w"] <= 1 and 0 < zone["h"] <= 1
            assert zone["x"] + zone["w"] <= 1.0001 and zone["y"] + zone["h"] <= 1.0001
        assert meta["captured_at"].endswith("Z") and meta["capture_id"].startswith("field-A-")
        Image.open(io.BytesIO(container["parts"]["rgb.jpg"])).verify()
    assert len({factory.build("healthy").meta["capture_id"] for _ in range(3)}) == 3  # every capture differs


def test_restamp_puts_send_time_fields_in_meta_and_keeps_the_object_valid(media) -> None:  # type: ignore[no-untyped-def]
    obj = CropFactory(media, seed=2).build("pest")
    assert all(obj.meta[key] is None for key in TCP_SO_FAR_KEYS)  # contract v1.1: null until the run sent bytes
    assert restamp(obj, tcp_retrans_ratio_so_far=None) is obj  # nothing to change: no rebuild

    stamped = restamp(obj, tcp_retrans_ratio_so_far=0.012345, tcp_bytes_sent_so_far=52_428_800,
                      tcp_bytes_retrans_so_far=647_219)

    assert len(stamped.payload) == len(obj.payload) == 26_214_400
    assert stamped.sha256 == hashlib.sha256(stamped.payload).digest() != obj.sha256
    container = ref.parse_sfim(stamped.payload)
    assert container["meta"]["tcp_retrans_ratio_so_far"] == 0.012345
    assert container["meta"]["tcp_bytes_sent_so_far"] == 52_428_800
    assert {k: v for k, v in container["meta"].items() if k not in TCP_SO_FAR_KEYS} == \
        {k: v for k, v in obj.meta.items() if k not in TCP_SO_FAR_KEYS}
    original = ref.parse_sfim(obj.payload)["parts"]
    assert container["parts"]["rgb.jpg"] == original["rgb.jpg"] and container["parts"]["ndvi.jpg"] == original["ndvi.jpg"]
    grown = len(container["parts"]["meta.json"]) - len(original["meta.json"])
    assert len(container["parts"]["bands.raw"]) == len(original["bands.raw"]) - grown
    assert container["parts"]["bands.raw"] == original["bands.raw"][:len(original["bands.raw"]) - grown]


def test_replace_meta_shrinks_and_grows_within_the_size() -> None:
    meta = {"v": 1, "capture_id": "c", "field": "f", "state": "healthy", "bands": ["blue"], "width": 2, "height": 2,
            "ndvi_mean": 0.7, "zones": [], "captured_at": "t", "pad": "x" * 40}
    payload = P.build_sfim(meta, b"\xff\xd8x", b"\xff\xd8y", b"\x01" * 4, 1000)
    shorter = P.replace_meta(payload, {**meta, "pad": ""})
    longer = P.replace_meta(payload, {**meta, "pad": "y" * 300})
    for out, pad in ((shorter, ""), (longer, "y" * 300)):
        parsed = ref.parse_sfim(out)
        assert len(out) == 1000 and json.loads(parsed["parts"]["meta.json"])["pad"] == pad
        assert parsed["parts"]["bands.raw"][:4] == b"\x01" * 4 and parsed["parts"]["rgb.jpg"] == b"\xff\xd8x"
    with pytest.raises(ValueError):
        P.replace_meta(payload, {**meta, "pad": "z" * 2000})
    with pytest.raises(ValueError):
        P.replace_meta(b"XXXX" + payload[4:], meta)


def test_sfim_rejects_corruption() -> None:
    meta = {"v": 1, "capture_id": "c", "field": "f", "state": "healthy", "bands": ["blue"], "width": 2, "height": 2,
            "ndvi_mean": 0.7, "zones": [], "captured_at": "t"}
    payload = P.build_sfim(meta, b"\xff\xd8x", b"\xff\xd8y", b"\x01" * 4, 1000)
    assert len(payload) == 1000 and ref.parse_sfim(payload)["parts"]["bands.raw"][:4] == b"\x01" * 4
    with pytest.raises(ref.ProtocolError):
        ref.parse_sfim(payload[:-1])
    with pytest.raises(ValueError):
        P.build_sfim(meta, b"x" * 2000, b"", b"", 1000)
    assert json.loads(ref.parse_sfim(payload)["parts"]["meta.json"]) == meta


def test_bands_are_consistent_with_ndvi() -> None:
    import numpy as np

    rgb = np.full((4, 4, 3), (50, 90, 40), np.uint8)
    ndvi_q = np.full((4, 4), int(round((0.6 + 1) * 127.5)), np.uint8)
    raw = bands_from(rgb, ndvi_q)
    assert len(raw) == 5 * 16
    red, nir = raw[2 * 16], raw[4 * 16]
    assert abs((nir - red) / (nir + red) - 0.6) < 0.05
