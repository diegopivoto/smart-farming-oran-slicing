"""Loopback end-to-end: content node + agent-style `send` runs -> reference parser. Never touches the lab network."""

from __future__ import annotations

import io
import json
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from PIL import Image

import sf_content_ref_parser as ref

CONTENT = Path(__file__).resolve().parents[2] / "deployment" / "content"
SCRIPT = CONTENT / "sf_content.py"
SUMMARY_KEYS_UDP = {"kind", "run_id", "service", "target", "rate_bps_requested", "started_at_ns", "sent_packets",
                    "send_errors", "sent_bytes", "offered_bps", "elapsed_s"}
SUMMARY_KEYS_CROP = {"kind", "run_id", "service", "target", "rate_bps_requested", "started_at_ns", "sent_objects",
                     "aborted_objects", "sent_bytes", "offered_bps", "elapsed_s", "tcp_total_retrans_segments"}


def free_tcp_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for(predicate, timeout: float = 20.0) -> None:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("timeout")


def last_json(text: str) -> dict:
    return json.loads([line for line in text.splitlines() if line.startswith("{")][-1])


@pytest.fixture()
def lab(media: Path, tmp_path: Path, port_offset: int):  # type: ignore[no-untyped-def]
    offset = port_offset
    listener = ref.Listener("127.0.0.1", offset).start()
    ui_port = free_tcp_port()
    sock = tmp_path / "node.sock"
    log = (tmp_path / "node.log").open("w")
    node = subprocess.Popen([sys.executable, str(SCRIPT), "node", "--target", "127.0.0.1", "--report", "",
                             "--port-offset", str(offset), "--ui-port", str(ui_port), "--node-socket", str(sock),
                             "--state-file", str(tmp_path / "state.json"), "--bind", "127.0.0.1", "--media", str(media),
                             "--seed", "1"], stdout=log, stderr=subprocess.STDOUT)
    try:
        wait_for(lambda: sock.exists())
        wait_for(lambda: _http_ok(ui_port))
        yield {"offset": offset, "listener": listener, "ui": ui_port, "sock": sock, "media": media, "tmp": tmp_path}
    finally:
        node.terminate()
        node.wait(timeout=20)
        listener.close()
        log.close()


def _http_ok(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1).read()
        return True
    except OSError:
        return False


def control(offset: int, *requests: dict | str) -> list[dict]:
    with socket.create_connection(("127.0.0.1", 47010 + offset), timeout=5) as conn:
        reader = conn.makefile("rb")
        replies = []
        for request in requests:
            line = request if isinstance(request, str) else json.dumps(request)
            conn.sendall(line.encode() + b"\n")
            replies.append(json.loads(reader.readline()))
        return replies


def send(lab: dict, service: str, duration: float, run_id: str, rate: int | None = None,
         extra: tuple[str, ...] = ()) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(SCRIPT), "send", "--service", service, "--target", "127.0.0.1",
                             "--duration", str(duration), "--run-id", run_id, "--warmup-s", "0.5",
                             "--node-socket", str(lab["sock"]), "--media", str(lab["media"]),
                             "--port-offset", str(lab["offset"]), *(["--rate-bps", str(rate)] if rate else []),
                             *extra], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def test_loopback_end_to_end(lab: dict) -> None:
    offset, listener = lab["offset"], lab["listener"]

    # control (phone -> generator)
    replies = control(offset, {"v": 1, "cmd": "state", "service": "weather", "state": "drought"},
                      {"v": 1, "cmd": "state", "service": "crop", "state": "pest"},
                      {"v": 1, "cmd": "state", "service": "drone", "state": "alert"},
                      {"v": 1, "cmd": "irrigation", "action": "activate", "duration_s": 60, "source": "app"},
                      {"v": 1, "cmd": "state", "service": "weather", "state": "rain"}, "not json")
    assert replies[3] == {"v": 1, "ok": True, "cmd": "irrigation", "detail": "irrigation on for 60 s"}
    assert all(r["ok"] for r in replies[:4]) and not replies[4]["ok"] and not replies[5]["ok"]
    assert replies[0]["detail"] == "weather state set to drought"

    # clock service (sf_traffic TIMEQ/TIMER)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.settimeout(2)
        udp.sendto(struct.pack("!4sQ", b"SFTQ", 12345), ("127.0.0.1", 47000 + offset))
        magic, echoed, server_ns = struct.unpack("!4sQQ", udp.recv(64))
    assert magic == b"SFTR" and echoed == 12345 and abs(server_ns - time.time_ns()) < 5e9

    # agent-style runs, handed to the node
    runs = {"weather": "a" * 32, "drone": "b" * 32, "crop": "c" * 32}
    procs = {"weather": send(lab, "weather", 4, runs["weather"]), "drone": send(lab, "drone", 4, runs["drone"]),
             "crop": send(lab, "crop", 4, runs["crop"], rate=400_000_000)}
    time.sleep(2.5)
    status = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{lab['ui']}/api/status", timeout=3).read())
    assert all(status["services"][s]["running"] for s in runs)
    assert status["services"]["drone"]["origin"] == "agent"
    assert status["commands"][0]["text"].startswith("Refused") or status["commands"][1]["text"].startswith("Refused")
    assert any(c["text"] == "Irrigation requested from phone · 60 s" for c in status["commands"])
    thumb = urllib.request.urlopen(f"http://127.0.0.1:{lab['ui']}/api/drone/frame.jpg", timeout=3).read()
    assert Image.open(io.BytesIO(thumb)).size[0] == 480
    page = urllib.request.urlopen(f"http://127.0.0.1:{lab['ui']}/", timeout=3).read().decode()
    assert "window.INITIAL = {" in page

    outputs = {s: p.communicate(timeout=40)[0] for s, p in procs.items()}
    summaries = {s: last_json(out) for s, out in outputs.items()}
    for s, out in outputs.items():
        assert procs[s].returncode == 0, out
        assert "delegated to the content node" in out
        assert summaries[s]["kind"] == "generator_summary" and summaries[s]["run_id"] == runs[s]
    assert SUMMARY_KEYS_UDP <= set(summaries["weather"]) and SUMMARY_KEYS_UDP <= set(summaries["drone"])
    assert SUMMARY_KEYS_CROP <= set(summaries["crop"])
    drone = summaries["drone"]
    assert abs(drone["offered_bps"] - 40_000_000) / 40_000_000 < 0.02
    assert drone["filler_packets"] > 0 and drone["frames_skipped"] == 0
    assert abs(summaries["weather"]["offered_bps"] - 100_000) / 100_000 < 0.05
    time.sleep(0.5)

    with listener.lock:
        weather = [w for w in listener.weather if w[0]["run_id"] == runs["weather"]]
        frames = list(listener.frames)
        drone_datagrams, last_frame = listener.drone_datagrams, listener.last_frame
        objects = [o for o in listener.objects if o["run_id"] == runs["crop"]]
    assert listener.errors == []
    assert 36 <= len(weather) <= 44  # 10/s for 4 s
    assert all(h["size"] == 1250 for h, _ in weather)
    assert [h["seq"] for h, _ in weather] == list(range(len(weather)))
    assert weather[-1][1]["state"] == "stable"  # drought, then irrigation -> stable
    assert weather[-1][1]["irrigation"]["active"]
    assert drone_datagrams >= 0.95 * drone["sent_packets"]
    assert 100 <= len(frames) <= 125  # 30 fps for 4 s
    assert Image.open(io.BytesIO(last_frame)).size == (1920, 1080)
    assert len(objects) >= 2 and all(o["sha256_valid"] and o["size"] == 26_214_400 for o in objects)
    assert objects[0]["meta"]["state"] == "pest" and objects[0]["meta"]["zones"]
    # contract v1.1: cumulative TCP retransmissions of the run in every object (null before any byte was sent)
    assert objects[0]["meta"]["tcp_retrans_ratio_so_far"] is None
    later = objects[1]["meta"]
    assert later["tcp_bytes_sent_so_far"] >= 26_214_400 and 0.0 <= later["tcp_retrans_ratio_so_far"] <= 1.0
    assert later["tcp_bytes_retrans_so_far"] <= later["tcp_bytes_sent_so_far"]
    assert summaries["crop"]["sent_objects"] >= 2 and summaries["crop"]["aborted_objects"] <= 1


def test_standalone_send_and_stop(lab: dict) -> None:
    proc = send(lab, "weather", 2, "d" * 32, extra=("--standalone",))
    out = proc.communicate(timeout=30)[0]
    summary = last_json(out)
    assert proc.returncode == 0 and summary["origin"] == "standalone" and summary["sent_packets"] >= 18

    # traffic-stop: terminating the delegated `send` stops the node's run (with a summary on the node side)
    proc = send(lab, "drone", 60, "e" * 32)
    time.sleep(2.0)
    proc.terminate()
    proc.wait(timeout=10)

    def stopped() -> bool:
        status = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{lab['ui']}/api/status", timeout=3).read())
        drone = status["services"]["drone"]
        return not drone["running"] and (drone["last_summary"] or {}).get("run_id") == "e" * 32

    wait_for(stopped, timeout=15)


def test_ref_parser_cli_runs() -> None:
    out = subprocess.run([sys.executable, str(CONTENT / "sf_content_ref_parser.py"), "--help"], capture_output=True,
                         text=True, check=True).stdout
    assert "listen" in out
