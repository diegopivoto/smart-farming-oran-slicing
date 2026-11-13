"""Argument-vector-only local and SSH command execution."""

import re
import shlex
import subprocess
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class CommandResult(BaseModel):
    """Complete, structured result including timeout as first-class state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int = Field(ge=0)
    timed_out: bool


class CommandRunner(Protocol):
    """Injectable read-only command transport."""

    def run(self, host: str, argv: Sequence[str], timeout_s: float) -> CommandResult:
        """Execute argv on a configured host without a local shell."""
        ...


class UnsafeCommand(ValueError):
    """Raised before execution when argv could expose a credential."""


class LocalSshCommandRunner:
    """Run commands locally or through an SSH-config host alias."""

    _SENSITIVE_ASSIGNMENT = re.compile(
        r"(?i)\b(?:password|passwd|passphrase|private[_-]?key|opc|supi|imsi)\b\s*[:=]"
    )

    def __init__(
        self,
        *,
        ssh_binary: str | Path = "ssh",
        forbidden_secrets: Iterable[str] = (),
    ) -> None:
        self._ssh_binary = str(ssh_binary)
        self._forbidden_secrets = tuple(secret for secret in forbidden_secrets if secret)

    def run(self, host: str, argv: Sequence[str], timeout_s: float) -> CommandResult:
        """Execute with `shell=False`; remote routing comes only from SSH config."""

        command_argv = tuple(str(argument) for argument in argv)
        self._validate(host, command_argv, timeout_s)
        if host == "local":
            process_argv = list(command_argv)
        else:
            process_argv = [
                self._ssh_binary,
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                host,
                shlex.join(command_argv),
            ]

        started_ns = time.monotonic_ns()
        try:
            completed = subprocess.run(  # noqa: S603 - argv is validated and shell is disabled
                process_argv,
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout_s,
                shell=False,
            )
        except subprocess.TimeoutExpired as error:
            duration_ms = (time.monotonic_ns() - started_ns) // 1_000_000
            stdout = self._timeout_text(error.stdout)
            stderr = self._timeout_text(error.stderr)
            message = f"command timed out after {timeout_s:g}s"
            stderr = f"{stderr.rstrip()}\n{message}\n" if stderr else f"{message}\n"
            return CommandResult(
                host=host,
                argv=command_argv,
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                duration_ms=duration_ms,
                timed_out=True,
            )

        duration_ms = (time.monotonic_ns() - started_ns) // 1_000_000
        return CommandResult(
            host=host,
            argv=command_argv,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_ms=duration_ms,
            timed_out=False,
        )

    def _validate(self, host: str, argv: tuple[str, ...], timeout_s: float) -> None:
        if not argv:
            raise UnsafeCommand("argv must not be empty")
        if timeout_s <= 0:
            raise UnsafeCommand("timeout_s must be positive")
        if not host or host.startswith("-"):
            raise UnsafeCommand("host must be a configured SSH alias or 'local'")
        for argument in argv:
            if self._SENSITIVE_ASSIGNMENT.search(argument):
                raise UnsafeCommand("command argument contains a sensitive assignment")
            if any(secret in argument for secret in self._forbidden_secrets):
                raise UnsafeCommand("command argument contains a known secret")

    @staticmethod
    def _timeout_text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode(errors="replace")
        return value
