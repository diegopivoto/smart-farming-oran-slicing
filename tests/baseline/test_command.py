import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from smartfarm.baseline.command import main
from smartfarm.baseline.sentinel import EndpointSentinel
from smartfarm.inventory.runner import CommandResult


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


def address_inventory(address: str) -> str:
    return json.dumps(
        [
            {
                "ifname": "uesimtun0",
                "addr_info": [{"family": "inet", "local": address, "prefixlen": 16}],
            }
        ]
    )


def ping_output(target: str, *, received: int = 3, loss: str = "0") -> str:
    return (
        f"PING {target} ({target}) 56(84) bytes of data.\n"
        f"\n--- {target} ping statistics ---\n"
        f"3 packets transmitted, {received} received, {loss}% packet loss, time 2002ms\n"
        "rtt min/avg/max/mdev = 17.948/20.550/22.157/1.861 ms\n"
    )


class SequenceRunner:
    def __init__(self, responses: Sequence[CommandResult] = ()) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, tuple[str, ...], float]] = []

    def run(self, host: str, argv: Sequence[str], timeout_s: float) -> CommandResult:
        self.calls.append((host, tuple(argv), timeout_s))
        return self.responses.pop(0)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--dry-run", "--oneplus-ip", "10.45.0.4"],
        ["--dry-run", "--un3gpp-ip", "10.45.0.2"],
    ],
)
def test_command_requires_both_ue_addresses(arguments: list[str]) -> None:
    runner = SequenceRunner()

    with pytest.raises(SystemExit) as error:
        main(arguments, runner=runner)

    assert error.value.code == 2
    assert runner.calls == []


def test_dry_run_prints_exact_read_only_plan_without_execution(capsys: pytest.CaptureFixture[str]) -> None:
    runner = SequenceRunner()

    exit_code = main(
        [
            "--dry-run",
            "--un3gpp-ip",
            "10.45.0.2",
            "--oneplus-ip",
            "10.45.0.4",
        ],
        runner=runner,
    )

    document = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert document["execute"] is False
    assert document["commands"] == [
        ["ip", "-j", "-4", "address", "show"],
        list(EndpointSentinel.ping_command("10.45.0.1")),
        list(EndpointSentinel.ping_command("10.45.0.4")),
    ]
    assert runner.calls == []


@pytest.mark.parametrize(
    "path_arguments",
    [
        ["--endpoint-context", "active-endpoints.json"],
        ["--output", "attempts"],
    ],
)
def test_execute_requires_output_and_endpoint_context(path_arguments: list[str]) -> None:
    runner = SequenceRunner()
    arguments = [
        "--execute",
        "--un3gpp-ip",
        "10.45.0.2",
        "--oneplus-ip",
        "10.45.0.4",
        *path_arguments,
    ]

    with pytest.raises(SystemExit) as error:
        main(arguments, runner=runner)

    assert error.value.code == 2
    assert runner.calls == []


def successful_runner() -> SequenceRunner:
    return SequenceRunner(
        [
            result(
                EndpointSentinel.address_inventory_command(),
                stdout=address_inventory("10.45.0.2"),
            ),
            result(("ping", "10.45.0.1"), stdout=ping_output("10.45.0.1")),
            result(("ping", "10.45.0.4"), stdout=ping_output("10.45.0.4")),
        ]
    )


def execute_arguments(output: Path, context: Path) -> list[str]:
    return [
        "--execute",
        "--un3gpp-ip",
        "10.45.0.2",
        "--oneplus-ip",
        "10.45.0.4",
        "--output",
        str(output),
        "--endpoint-context",
        str(context),
    ]


def test_success_persists_attempt_and_promotes_active_context(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "attempt-parent" / "post-restart-sentinel"
    context = tmp_path / "context-parent" / "active-endpoints.json"

    exit_code = main(execute_arguments(output, context), runner=successful_runner())

    stdout_document = json.loads(capsys.readouterr().out)
    attempts = list(output.glob("*/baseline-probe.json"))
    assert exit_code == 0
    assert stdout_document["passed"] is True
    assert len(attempts) == 1
    attempt = json.loads(attempts[0].read_text(encoding="utf-8"))
    active = json.loads(context.read_text(encoding="utf-8"))
    assert attempt["passed"] is True
    assert active["schema_version"] == 1
    assert active["selection"]["source"] == "manual_cli"
    assert active["validation"]["passed"] is True
    assert active["project_commit"]
    assert active["evidence_path"].endswith("baseline-probe.json")


@pytest.mark.parametrize("failure", ["address_absent", "packet_loss"])
def test_failed_attempt_does_not_replace_existing_active_context(
    failure: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "attempts"
    context = tmp_path / "active-endpoints.json"
    previous = b'{"known":"previous validated context"}\n'
    context.write_bytes(previous)
    if failure == "address_absent":
        runner = SequenceRunner(
            [
                result(
                    EndpointSentinel.address_inventory_command(),
                    stdout=address_inventory("10.45.0.9"),
                )
            ]
        )
    else:
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

    exit_code = main(execute_arguments(output, context), runner=runner)

    stdout_document = json.loads(capsys.readouterr().out)
    attempts = list(output.glob("*/baseline-probe.json"))
    assert exit_code == 2
    assert stdout_document["passed"] is False
    assert len(attempts) == 1
    assert json.loads(attempts[0].read_text(encoding="utf-8"))["passed"] is False
    assert context.read_bytes() == previous


@pytest.mark.parametrize(
    ("un3gpp_ip", "oneplus_ip", "expected_field"),
    [
        ("10.45.0.2", "10.45.0.2", "endpoint_selection"),
        ("not-an-ip", "10.45.0.4", "un3gpp_ip"),
    ],
)
def test_invalid_configuration_persists_failed_attempt_without_promotion(
    un3gpp_ip: str,
    oneplus_ip: str,
    expected_field: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "attempts"
    context = tmp_path / "active-endpoints.json"
    previous = b'{"known":"previous validated context"}\n'
    context.write_bytes(previous)
    runner = SequenceRunner()

    exit_code = main(
        [
            "--execute",
            "--un3gpp-ip",
            un3gpp_ip,
            "--oneplus-ip",
            oneplus_ip,
            "--output",
            str(output),
            "--endpoint-context",
            str(context),
        ],
        runner=runner,
    )

    stdout_document = json.loads(capsys.readouterr().out)
    attempts = list(output.glob("*/baseline-probe.json"))
    assert exit_code == 2
    assert stdout_document["passed"] is False
    assert stdout_document["failure"]["stage"] == "endpoint_configuration"
    assert stdout_document["failure"]["field"] == expected_field
    assert len(attempts) == 1
    assert json.loads(attempts[0].read_text(encoding="utf-8")) == stdout_document
    assert context.read_bytes() == previous
    assert runner.calls == []
