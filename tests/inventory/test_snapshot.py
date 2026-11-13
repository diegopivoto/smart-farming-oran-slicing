import json
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from smartfarm.inventory.runner import (
    CommandResult,
    LocalSshCommandRunner,
    UnsafeCommand,
)
from smartfarm.inventory.snapshot import FileSpec, HostSpec, SnapshotCollector


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...], float]] = []

    def run(self, host: str, argv: Sequence[str], timeout_s: float) -> CommandResult:
        command = tuple(argv)
        self.calls.append((host, command, timeout_s))
        if command == ("hostname",):
            return CommandResult(
                host=host,
                argv=command,
                exit_code=0,
                stdout=(
                    "pc2\nK = aabbcc\nOPC: 112233\npassword=do-not-store\n"
                    "SUPI: imsi-001010000000001\n"
                ),
                stderr="",
                duration_ms=2,
                timed_out=False,
            )
        if command[:2] == ("test", "-e"):
            return CommandResult(
                host=host,
                argv=command,
                exit_code=1,
                stdout="",
                stderr="",
                duration_ms=1,
                timed_out=False,
            )
        return CommandResult(
            host=host,
            argv=command,
            exit_code=0,
            stdout="ok\n",
            stderr="",
            duration_ms=1,
            timed_out=False,
        )


def test_local_runner_preserves_stdout_stderr_and_exit_code() -> None:
    runner = LocalSshCommandRunner()

    result = runner.run(
        "local",
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(7)",
        ],
        timeout_s=2,
    )

    assert result.exit_code == 7
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"
    assert result.timed_out is False


def test_local_runner_reports_timeout_as_structured_result() -> None:
    runner = LocalSshCommandRunner()

    result = runner.run(
        "local",
        [sys.executable, "-c", "import time; time.sleep(1)"],
        timeout_s=0.01,
    )

    assert result.exit_code is None
    assert result.timed_out is True
    assert "timed out" in result.stderr.lower()


def test_runner_rejects_argument_containing_known_secret() -> None:
    runner = LocalSshCommandRunner(forbidden_secrets=("lab-password",))

    with pytest.raises(UnsafeCommand, match="secret"):
        runner.run("local", ["printf", "prefix-lab-password-suffix"], timeout_s=1)


def test_ssh_runner_quotes_remote_arguments_as_one_command(tmp_path: Path) -> None:
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text(
        "#!/usr/bin/env python3\n" "import json, sys\n" "print(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o700)
    runner = LocalSshCommandRunner(ssh_binary=fake_ssh)

    result = runner.run(
        "smartfarm-pc2",
        ["printf", "%s", "value; touch /tmp/should-not-run"],
        timeout_s=1,
    )

    process_arguments = json.loads(result.stdout)
    assert process_arguments[-2] == "smartfarm-pc2"
    assert process_arguments[-1] == "printf %s 'value; touch /tmp/should-not-run'"


def test_snapshot_marks_missing_files_and_redacts_sensitive_patterns(tmp_path: Path) -> None:
    runner = FakeRunner()
    collector = SnapshotCollector(runner)
    host = HostSpec(
        name="pc2",
        target="smartfarm-pc2",
        role="un3gpp-bs-n3iwf",
        files=(FileSpec(path=str(tmp_path / "missing.conf")),),
    )

    snapshot = collector.collect(host)

    assert snapshot.host == "pc2"
    assert snapshot.files[0].status == "missing"
    rendered = snapshot.model_dump_json()
    assert "aabbcc" not in rendered
    assert "112233" not in rendered
    assert "do-not-store" not in rendered
    assert "001010000000001" not in rendered
    assert rendered.count("[REDACTED]") >= 4
    assert ("smartfarm-pc2", ("test", "-e", str(tmp_path / "missing.conf")), 5.0) in runner.calls
