from collections.abc import Sequence
from ipaddress import IPv4Address
from pathlib import Path

from smartfarm.baseline.probes import BaselineProbe, ProbeStatus, parse_ping_summary
from smartfarm.inventory.runner import CommandResult
from smartfarm.inventory.snapshot import HostSpec


FIXTURES = Path(__file__).parents[1] / "fixtures"


class PingRunner:
    def __init__(self, responses: dict[str, CommandResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, tuple[str, ...], float]] = []

    def run(self, host: str, argv: Sequence[str], timeout_s: float) -> CommandResult:
        command = tuple(argv)
        self.calls.append((host, command, timeout_s))
        return self.responses[command[-1]]


def command_result(target: str, fixture: str, *, exit_code: int = 0) -> CommandResult:
    return CommandResult(
        host="smartfarm-pc3",
        argv=("ping", target),
        exit_code=exit_code,
        stdout=(FIXTURES / fixture).read_text(encoding="utf-8"),
        stderr="",
        duration_ms=2003,
        timed_out=False,
    )


def test_parse_ping_summary_preserves_loss_and_rtt() -> None:
    success = parse_ping_summary((FIXTURES / "ping_success.txt").read_text(encoding="utf-8"))
    loss = parse_ping_summary((FIXTURES / "ping_loss.txt").read_text(encoding="utf-8"))

    assert (success.transmitted, success.received, success.packet_loss_percent) == (3, 3, 0.0)
    assert (success.rtt_min_ms, success.rtt_avg_ms, success.rtt_max_ms, success.rtt_mdev_ms) == (
        17.948,
        20.55,
        22.157,
        1.861,
    )
    assert (loss.transmitted, loss.received, loss.packet_loss_percent) == (3, 2, 33.3333)


def test_probe_uses_pc3_vrf_for_core_and_oneplus_targets() -> None:
    responses = {
        "10.45.0.1": command_result("10.45.0.1", "ping_success.txt"),
        "10.45.0.4": command_result("10.45.0.4", "ping_success.txt"),
    }
    runner = PingRunner(responses)
    probe = BaselineProbe(runner)
    host = HostSpec(name="pc3", target="smartfarm-pc3", role="un3gpp-ue-n3iwue")

    result = probe.run(host, (IPv4Address("10.45.0.1"), IPv4Address("10.45.0.4")))

    assert result.passed is True
    assert [target.status for target in result.targets] == [ProbeStatus.PASSED, ProbeStatus.PASSED]
    assert [call[1] for call in runner.calls] == [
        (
            "sudo",
            "-n",
            "ip",
            "vrf",
            "exec",
            "vrf-n3iwue",
            "ping",
            "-c",
            "3",
            "-W",
            "2",
            "10.45.0.1",
        ),
        (
            "sudo",
            "-n",
            "ip",
            "vrf",
            "exec",
            "vrf-n3iwue",
            "ping",
            "-c",
            "3",
            "-W",
            "2",
            "10.45.0.4",
        ),
    ]


def test_probe_preserves_packet_loss_as_failed_target() -> None:
    runner = PingRunner({"10.45.0.4": command_result("10.45.0.4", "ping_loss.txt", exit_code=1)})
    probe = BaselineProbe(runner)
    host = HostSpec(name="pc3", target="smartfarm-pc3", role="un3gpp-ue-n3iwue")

    result = probe.run(host, (IPv4Address("10.45.0.4"),))

    assert result.passed is False
    assert result.targets[0].status is ProbeStatus.PACKET_LOSS
    assert result.targets[0].ping is not None
    assert result.targets[0].ping.packet_loss_percent == 33.3333
    assert result.targets[0].command.exit_code == 1


def test_probe_preserves_timeout_instead_of_generic_offline() -> None:
    timed_out = CommandResult(
        host="smartfarm-pc3",
        argv=("ping", "10.45.0.4"),
        exit_code=None,
        stdout="",
        stderr="command timed out after 8s\n",
        duration_ms=8000,
        timed_out=True,
    )
    runner = PingRunner({"10.45.0.4": timed_out})
    probe = BaselineProbe(runner)
    host = HostSpec(name="pc3", target="smartfarm-pc3", role="un3gpp-ue-n3iwue")

    result = probe.run(host, (IPv4Address("10.45.0.4"),))

    assert result.passed is False
    assert result.targets[0].status is ProbeStatus.TIMEOUT
    assert result.targets[0].ping is None
    assert result.targets[0].command == timed_out
