"""Read-only VRF connectivity sentinel from the UN3GPP UE."""

import re
import time
from collections.abc import Sequence
from enum import StrEnum
from ipaddress import IPv4Address

from pydantic import BaseModel, ConfigDict, Field

from smartfarm.inventory.runner import CommandResult, CommandRunner
from smartfarm.inventory.snapshot import HostSpec


class FrozenModel(BaseModel):
    """Immutable probe result base."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class PingResult(FrozenModel):
    """Parsed packet and latency evidence from iputils ping."""

    transmitted: int = Field(ge=0)
    received: int = Field(ge=0)
    packet_loss_percent: float = Field(ge=0, le=100)
    elapsed_ms: int | None = Field(default=None, ge=0)
    rtt_min_ms: float | None = Field(default=None, ge=0)
    rtt_avg_ms: float | None = Field(default=None, ge=0)
    rtt_max_ms: float | None = Field(default=None, ge=0)
    rtt_mdev_ms: float | None = Field(default=None, ge=0)


class ProbeStatus(StrEnum):
    """Disjoint outcomes retained in baseline evidence."""

    PASSED = "passed"
    PACKET_LOSS = "packet_loss"
    TIMEOUT = "timeout"
    COMMAND_ERROR = "command_error"
    PARSE_ERROR = "parse_error"


class TargetProbeResult(FrozenModel):
    """Raw command plus interpreted outcome for one target."""

    target: IPv4Address
    status: ProbeStatus
    command: CommandResult
    ping: PingResult | None = None
    detail: str | None = None


class BaselineResult(FrozenModel):
    """Aggregate gate result for all required endpoints."""

    host: str
    target: str
    started_at_ns: int
    completed_at_ns: int
    targets: tuple[TargetProbeResult, ...]
    passed: bool


class PingParseError(ValueError):
    """Raised when command output does not contain an iputils summary."""


_PACKETS = re.compile(
    r"(?P<transmitted>\d+)\s+packets transmitted,\s+"
    r"(?P<received>\d+)\s+(?:packets\s+)?received,\s+"
    r"(?P<loss>\d+(?:\.\d+)?)%\s+packet loss"
    r"(?:,\s+time\s+(?P<elapsed>\d+)ms)?"
)
_RTT = re.compile(
    r"(?:rtt|round-trip) min/avg/max/(?:mdev|stddev)\s*=\s*"
    r"(?P<minimum>\d+(?:\.\d+)?)/"
    r"(?P<average>\d+(?:\.\d+)?)/"
    r"(?P<maximum>\d+(?:\.\d+)?)/"
    r"(?P<mdev>\d+(?:\.\d+)?)\s+ms"
)


def parse_ping_summary(text: str) -> PingResult:
    """Parse iputils output without discarding partial-loss evidence."""

    packets = _PACKETS.search(text)
    if packets is None:
        raise PingParseError("ping packet summary not found")
    rtt = _RTT.search(text)
    return PingResult(
        transmitted=int(packets.group("transmitted")),
        received=int(packets.group("received")),
        packet_loss_percent=float(packets.group("loss")),
        elapsed_ms=int(packets.group("elapsed")) if packets.group("elapsed") else None,
        rtt_min_ms=float(rtt.group("minimum")) if rtt else None,
        rtt_avg_ms=float(rtt.group("average")) if rtt else None,
        rtt_max_ms=float(rtt.group("maximum")) if rtt else None,
        rtt_mdev_ms=float(rtt.group("mdev")) if rtt else None,
    )


class BaselineProbe:
    """Run the known-good, read-only reachability gate through PC3's VRF."""

    def __init__(self, runner: CommandRunner, *, timeout_s: float = 8.0) -> None:
        self._runner = runner
        self._timeout_s = timeout_s

    @staticmethod
    def command_for(target: IPv4Address) -> tuple[str, ...]:
        """Return the reviewed command vector for a target."""

        return (
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
            str(target),
        )

    def run(self, host: HostSpec, targets: Sequence[IPv4Address]) -> BaselineResult:
        """Probe every target and require three replies with exactly zero loss."""

        started_at_ns = time.time_ns()
        results: list[TargetProbeResult] = []
        for target in targets:
            command = self._runner.run(host.target, self.command_for(target), self._timeout_s)
            results.append(self._interpret(target, command))
        completed_at_ns = time.time_ns()
        return BaselineResult(
            host=host.name,
            target=host.target,
            started_at_ns=started_at_ns,
            completed_at_ns=completed_at_ns,
            targets=tuple(results),
            passed=bool(results) and all(result.status is ProbeStatus.PASSED for result in results),
        )

    @staticmethod
    def _interpret(target: IPv4Address, command: CommandResult) -> TargetProbeResult:
        if command.timed_out:
            return TargetProbeResult(
                target=target,
                status=ProbeStatus.TIMEOUT,
                command=command,
                detail=command.stderr or "command timed out",
            )
        try:
            ping = parse_ping_summary(f"{command.stdout}\n{command.stderr}")
        except PingParseError as error:
            status = ProbeStatus.COMMAND_ERROR if command.exit_code else ProbeStatus.PARSE_ERROR
            return TargetProbeResult(
                target=target,
                status=status,
                command=command,
                detail=str(error),
            )
        if (
            ping.transmitted != 3
            or ping.received != 3
            or ping.packet_loss_percent > 0
        ):
            return TargetProbeResult(
                target=target,
                status=ProbeStatus.PACKET_LOSS,
                command=command,
                ping=ping,
                detail=(
                    "expected 3/3 replies with 0% packet loss; "
                    f"observed {ping.received}/{ping.transmitted} with "
                    f"{ping.packet_loss_percent:g}% loss"
                ),
            )
        if command.exit_code != 0:
            return TargetProbeResult(
                target=target,
                status=ProbeStatus.COMMAND_ERROR,
                command=command,
                ping=ping,
                detail=f"ping exited with code {command.exit_code}",
            )
        return TargetProbeResult(
            target=target,
            status=ProbeStatus.PASSED,
            command=command,
            ping=ping,
        )
