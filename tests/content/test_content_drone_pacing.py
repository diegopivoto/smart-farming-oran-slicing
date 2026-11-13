"""Drone pacing: frames leave at 30 fps of wall clock even after a sender stall (no catch-up burst of frames)."""

from __future__ import annotations

import queue
import socket
import threading
import time
from pathlib import Path

import sf_content_ref_parser as ref
from sfcontent import protocol as P
from sfcontent import senders
from sfcontent.drone import DroneFrames


def test_frames_stay_at_30_fps_after_a_stall(media: Path, monkeypatch, port_offset: int) -> None:  # type: ignore[no-untyped-def]
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
    sink.bind(("127.0.0.1", 47003 + port_offset))
    sink.settimeout(0.5)
    done = threading.Event()
    completed: list[float] = []
    datagrams = [0]

    def receive() -> None:
        assembler = ref.FrameAssembler()
        while not done.is_set():
            try:
                data = sink.recv(2048)
            except TimeoutError:
                continue
            datagrams[0] += 1
            _, fragment = ref.parse_drone(data)
            if not fragment["filler"] and assembler.add(fragment) is not None:
                completed.append(time.monotonic())

    receiver = threading.Thread(target=receive, daemon=True)
    receiver.start()
    original = DroneFrames.read
    calls = [0]

    def stalling_read(self, variant: str, index: int) -> bytes:  # type: ignore[no-untyped-def]
        calls[0] += 1
        if calls[0] == 20:
            time.sleep(0.6)  # e.g. CPU starvation of the sender process
        return original(self, variant, index)

    monkeypatch.setattr(DroneFrames, "read", stalling_read)
    params = senders.RunParams(service="drone", target="127.0.0.1", run_id="f" * 32, duration=3.0,
                               rate_bps=45_000_000, report=None, media=str(media),
                               ports=P.ports_with_offset(port_offset), state="normal")
    result = senders.drone_loop(params, queue.Queue(), threading.Event(), lambda _: None)
    time.sleep(0.3)
    done.set()
    receiver.join()
    sink.close()

    assert result["pacing_stalls"] >= 1 and result["pacing_dropped_packets"] > 2000  # ~0.6 s of debt dropped
    assert result["frames_late"] >= 14  # the frames due during the stall are skipped, not burst out
    assert result["frames_sent"] + result["frames_late"] + result["frames_skipped"] in range(89, 92)
    assert len(completed) <= 91
    worst = max(sum(1 for t in completed if start <= t < start + 1.0) for start in completed)
    assert worst <= 31, f"{worst} frames within one second"
    assert datagrams[0] >= 0.95 * result["sent_packets"]
