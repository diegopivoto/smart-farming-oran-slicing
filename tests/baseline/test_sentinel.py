import json
from collections.abc import Sequence
from ipaddress import IPv4Address, IPv4Network

import pytest

from smartfarm.baseline.endpoints import resolve_manual_endpoints
from smartfarm.baseline.sentinel import (
    AddressCheckStatus,
    AddressInventoryParseError,
    EndpointSentinel,
    SentinelTargetRole,
    parse_host_ipv4_addresses,
)
from smartfarm.inventory.runner import CommandResult
from smartfarm.inventory.snapshot import HostSpec


HOST = HostSpec(name="pc3", target="smartfarm-pc3", role="un3gpp-ue-n3iwue")
SELECTION = resolve_manual_endpoints(
    un3gpp_ip=IPv4Address("10.45.0.2"),
    oneplus_ip=IPv4Address("10.45.0.4"),
    pdu_network=IPv4Network("10.45.0.0/16"),
    gateway=IPv4Address("10.45.0.1"),
)


def result(
    argv: tuple[str, ...],
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int | None = 0,
    timed_out: bool = False,
) -> CommandResult:
    return CommandResult(
        host="smartfarm-pc3",
        argv=argv,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=5,
        timed_out=timed_out,
    )


def address_inventory(*addresses: str) -> str:
    return json.dumps(
        [
            {
                "ifname": "lo",
                "addr_info": [{"family": "inet", "local": "127.0.0.1"}],
            },
            {
                "ifname": "uesimtun0",
                "addr_info": [
                    {"family": "inet", "local": address, "prefixlen": 16}
                    for address in addresses
                ],
            },
        ]
    )


def ping_output(
    target: str, *, transmitted: int = 3, received: int = 3, loss: str = "0"
) -> str:
    return (
        f"PING {target} ({target}) 56(84) bytes of data.\n"
        f"\n--- {target} ping statistics ---\n"
        f"{transmitted} packets transmitted, {received} received, {loss}% packet loss, "
        "time 2002ms\n"
        "rtt min/avg/max/mdev = 17.948/20.550/22.157/1.861 ms\n"
    )


class SequenceRunner:
    def __init__(self, responses: Sequence[CommandResult]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, tuple[str, ...], float]] = []

    def run(self, host: str, argv: Sequence[str], timeout_s: float) -> CommandResult:
        self.calls.append((host, tuple(argv), timeout_s))
        return self.responses.pop(0)


def test_parse_host_ipv4_addresses_extracts_all_local_fields() -> None:
    document = address_inventory("10.45.0.2", "192.0.2.10")

    assert parse_host_ipv4_addresses(document) == frozenset(
        {
            IPv4Address("127.0.0.1"),
            IPv4Address("10.45.0.2"),
            IPv4Address("192.0.2.10"),
        }
    )


@pytest.mark.parametrize(
    "document",
    [
        "not-json",
        '{"ifname":"uesimtun0"}',
        '[{"ifname":"uesimtun0","addr_info":[{"family":"inet","local":"bad"}]}]',
    ],
)
def test_parse_host_ipv4_addresses_rejects_malformed_inventory(document: str) -> None:
    with pytest.raises(AddressInventoryParseError):
        parse_host_ipv4_addresses(document)


def test_pc3_address_check_reports_requested_address_present() -> None:
    runner = SequenceRunner(
        [
            result(
                EndpointSentinel.address_inventory_command(),
                stdout=address_inventory("10.45.0.2"),
            ),
            result(("ping", "10.45.0.1"), stdout=ping_output("10.45.0.1")),
            result(("ping", "10.45.0.4"), stdout=ping_output("10.45.0.4")),
        ]
    )

    evidence = EndpointSentinel(runner).run(HOST, SELECTION)

    assert evidence.pc3_address.status is AddressCheckStatus.PRESENT
    assert evidence.pc3_address.requested == IPv4Address("10.45.0.2")
    assert evidence.pc3_address.observed == (
        IPv4Address("10.45.0.2"),
        IPv4Address("127.0.0.1"),
    )
    assert runner.calls[0][1] == ("ip", "-j", "-4", "address", "show")


def test_pc3_address_check_reports_requested_address_absent() -> None:
    runner = SequenceRunner(
        [
            result(
                EndpointSentinel.address_inventory_command(),
                stdout=address_inventory("10.45.0.9"),
            )
        ]
    )

    evidence = EndpointSentinel(runner).run(HOST, SELECTION)

    assert evidence.pc3_address.status is AddressCheckStatus.ABSENT
    assert evidence.passed is False


def test_pc3_address_check_preserves_timeout() -> None:
    runner = SequenceRunner(
        [
            result(
                EndpointSentinel.address_inventory_command(),
                stderr="command timed out after 8s\n",
                exit_code=None,
                timed_out=True,
            )
        ]
    )

    evidence = EndpointSentinel(runner).run(HOST, SELECTION)

    assert evidence.pc3_address.status is AddressCheckStatus.TIMEOUT
    assert evidence.pc3_address.command.timed_out is True
    assert evidence.passed is False


def test_pc3_address_check_preserves_nonzero_exit() -> None:
    runner = SequenceRunner(
        [
            result(
                EndpointSentinel.address_inventory_command(),
                stderr="ip failed\n",
                exit_code=1,
            )
        ]
    )

    evidence = EndpointSentinel(runner).run(HOST, SELECTION)

    assert evidence.pc3_address.status is AddressCheckStatus.COMMAND_ERROR
    assert evidence.pc3_address.command.exit_code == 1
    assert evidence.passed is False


def test_sentinel_checks_un3gpp_address_before_gateway_and_oneplus() -> None:
    runner = SequenceRunner(
        [
            result(
                EndpointSentinel.address_inventory_command(),
                stdout=address_inventory("10.45.0.2"),
            ),
            result(("ping", "10.45.0.1"), stdout=ping_output("10.45.0.1")),
            result(("ping", "10.45.0.4"), stdout=ping_output("10.45.0.4")),
        ]
    )

    evidence = EndpointSentinel(runner).run(HOST, SELECTION)

    assert evidence.passed is True
    assert [call[1] for call in runner.calls] == [
        ("ip", "-j", "-4", "address", "show"),
        EndpointSentinel.ping_command(IPv4Address("10.45.0.1")),
        EndpointSentinel.ping_command(IPv4Address("10.45.0.4")),
    ]


def test_sentinel_fails_without_connectivity_calls_when_un3gpp_address_is_absent() -> None:
    runner = SequenceRunner(
        [
            result(
                EndpointSentinel.address_inventory_command(),
                stdout=address_inventory("10.45.0.9"),
            )
        ]
    )

    evidence = EndpointSentinel(runner).run(HOST, SELECTION)

    assert evidence.passed is False
    assert evidence.connectivity is None
    assert len(runner.calls) == 1


def test_sentinel_preserves_ping_loss_and_marks_evidence_failed() -> None:
    runner = SequenceRunner(
        [
            result(
                EndpointSentinel.address_inventory_command(),
                stdout=address_inventory("10.45.0.2"),
            ),
            result(("ping", "10.45.0.1"), stdout=ping_output("10.45.0.1")),
            result(
                ("ping", "10.45.0.4"),
                stdout=ping_output("10.45.0.4", received=2, loss="33.3333"),
                exit_code=1,
            ),
        ]
    )

    evidence = EndpointSentinel(runner).run(HOST, SELECTION)

    assert evidence.passed is False
    assert evidence.connectivity is not None
    assert evidence.connectivity.targets[1].ping is not None
    assert evidence.connectivity.targets[1].ping.packet_loss_percent == 33.3333


def test_sentinel_rejects_zero_loss_with_fewer_than_three_replies() -> None:
    runner = SequenceRunner(
        [
            result(
                EndpointSentinel.address_inventory_command(),
                stdout=address_inventory("10.45.0.2"),
            ),
            result(("ping", "10.45.0.1"), stdout=ping_output("10.45.0.1")),
            result(
                ("ping", "10.45.0.4"),
                stdout=ping_output("10.45.0.4", transmitted=1, received=1),
            ),
        ]
    )

    evidence = EndpointSentinel(runner).run(HOST, SELECTION)

    assert evidence.passed is False
    assert evidence.connectivity is not None
    assert evidence.connectivity.targets[1].status.value == "packet_loss"
    assert evidence.connectivity.targets[1].detail == (
        "expected 3/3 replies with 0% packet loss; observed 1/1 with 0% loss"
    )


def test_sentinel_evidence_maps_gateway_and_oneplus_roles_explicitly() -> None:
    runner = SequenceRunner(
        [
            result(
                EndpointSentinel.address_inventory_command(),
                stdout=address_inventory("10.45.0.2"),
            ),
            result(("ping", "10.45.0.1"), stdout=ping_output("10.45.0.1")),
            result(("ping", "10.45.0.4"), stdout=ping_output("10.45.0.4")),
        ]
    )

    evidence = EndpointSentinel(runner).run(HOST, SELECTION)

    assert [(target.role, target.ipv4) for target in evidence.connectivity_targets] == [
        (SentinelTargetRole.OPEN5GS_GATEWAY, IPv4Address("10.45.0.1")),
        (SentinelTargetRole.ONEPLUS_8T, IPv4Address("10.45.0.4")),
    ]
