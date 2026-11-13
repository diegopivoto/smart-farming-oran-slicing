"""The node agent's `--traffic-script` option starts sf_content.py with the same `send` arguments as sf_traffic.py."""

import importlib.util
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[2] / "deployment" / "pc3" / "smartfarm_node_agent.py"


def load_agent():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("smartfarm_node_agent_content", AGENT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("script", [None, "/home/inatel-crr/src/smartfarm-content/sf_content.py"])
def test_traffic_command_uses_the_configured_script(monkeypatch: pytest.MonkeyPatch, script: str | None) -> None:
    agent = load_agent()
    started: list[list[str]] = []

    class FakeProcess:
        pid = 4242

        def __init__(self, argv: list[str], **_: object) -> None:
            started.append(argv)

        def poll(self) -> None:
            return None

    monkeypatch.setattr(agent.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(agent, "open", lambda *a, **k: None, raising=False)
    command = "traffic-drone-10.45.0.2-30-" + "a" * 32 + "-45000000"
    reply = agent.traffic(command, "vrf-n3iwue", *([script] if script else []))
    assert reply.startswith("TRAFFIC started service=drone target=10.45.0.2")
    expected = script or "/opt/smartfarm/sf_traffic.py"
    assert started == [["ip", "vrf", "exec", "vrf-n3iwue", "python3", "-u", expected, "send", "--service", "drone",
                        "--target", "10.45.0.2", "--duration", "30", "--run-id", "a" * 32, "--report",
                        "http://10.45.0.1:8088/report", "--rate-bps", "45000000"]]
