import importlib.util
import socket
import threading
import time
from ipaddress import IPv4Address
from pathlib import Path

import pytest

from smartfarm.contracts.service import ServiceId
from smartfarm.orchestrator.un3gpp_slice import (
    Un3gppSliceClient,
    Un3gppSliceError,
    Un3gppSliceUnreachable,
    agent_transport,
    sign_request,
)

SCRIPT = Path("deployment/pc3/smartfarm_node_agent.py")
KEY = b"k" * 48
STATUS = "STATUS slice=2 sst=1 sd=000002 pdu_session_id=2 vrf=vrf-n3iwue iface=gretun-id-3-1 ip=10.45.0.93 status=accepted"


def load_agent():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("smartfarm_node_agent", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
def lab() -> tuple[int, list[str]]:
    """A fake N3IWUE listener plus the real agent (no VRF binding) on loopback."""

    received: list[str] = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)

    def fake_n3iwue() -> None:
        while True:
            conn, _ = listener.accept()
            with conn:
                command = conn.recv(64).decode().strip()
                received.append(command)
                conn.sendall((STATUS if command == "STATUS" else "OK requested").encode() + b"\n")

    threading.Thread(target=fake_n3iwue, daemon=True).start()
    agent = load_agent()
    port = free_port()
    threading.Thread(
        target=agent.serve, args=(port, None, KEY, listener.getsockname()), daemon=True
    ).start()
    time.sleep(0.2)
    return port, received


def raw(port: int, line: str) -> str:
    with socket.create_connection(("127.0.0.1", port), timeout=3) as conn:
        conn.sendall(line.encode() + b"\n")
        return conn.makefile().readline().strip()


@pytest.mark.asyncio
async def test_client_reaches_agent_and_relays_allowed_commands(lab: tuple[int, list[str]]) -> None:
    port, received = lab
    client = Un3gppSliceClient(agent_transport(lambda: IPv4Address("127.0.0.1"), KEY, port=port), poll_s=0)

    status = await client.status()
    await client.request(ServiceId.DRONE)

    assert status is not None and status.ipv4 == IPv4Address("10.45.0.93")
    assert received == ["STATUS", "STATUS", "SLICE 3"]


def test_agent_refuses_bad_signature_replay_stale_and_unknown_commands(lab: tuple[int, list[str]]) -> None:
    port, received = lab
    now = time.time_ns()
    good = sign_request(KEY, "status", now, "a" * 32)

    assert raw(port, good).startswith("STATUS slice=2")
    assert raw(port, good) == "ERROR replayed request"
    assert raw(port, sign_request(b"x" * 48, "status", now, "b" * 32)) == "ERROR bad signature"
    assert raw(port, sign_request(KEY, "status", now - 60_000_000_000, "c" * 32)) == "ERROR stale or future request"
    assert raw(port, sign_request(KEY, "reboot", now, "d" * 32)) == "ERROR malformed request"
    assert raw(port, "status; rm -rf /") == "ERROR malformed request"
    assert raw(port, sign_request(KEY, "ping-8.8.8.8", now, "e" * 32)) == "ERROR malformed request"
    assert raw(port, sign_request(KEY, "ping-10.45.0.1;id", now, "f" * 32)) == "ERROR malformed request"
    assert raw(port, sign_request(KEY, "ping-10.45.300.1", now, "0" * 32)) == "ERROR invalid ping target"
    assert raw(port, sign_request(KEY, "traffic-video-10.45.160.4-30-" + "a" * 32, now, "1" * 32)) == "ERROR malformed request"
    assert raw(port, sign_request(KEY, "traffic-drone-8.8.8.8-30-" + "a" * 32, now, "2" * 32)) == "ERROR malformed request"
    assert raw(port, sign_request(KEY, "traffic-drone-10.45.160.4-9999-" + "a" * 32, now, "3" * 32)) == (
        "ERROR traffic target or duration out of range"
    )
    assert received == ["STATUS"]


@pytest.mark.asyncio
async def test_unreachable_agent_is_reported_as_unreachable() -> None:
    transport = agent_transport(lambda: IPv4Address("127.0.0.1"), KEY, port=free_port(), timeout_s=1)

    with pytest.raises(Un3gppSliceUnreachable):
        await transport(["status"])
    assert issubclass(Un3gppSliceUnreachable, Un3gppSliceError)
