"""Redacted inventory snapshots for preserving the known-good lab baseline."""

import argparse
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from smartfarm.inventory.runner import CommandResult, CommandRunner, LocalSshCommandRunner


class FrozenModel(BaseModel):
    """Immutable inventory configuration and result base."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class FileSpec(FrozenModel):
    """File whose existence and digest may be captured without reading content."""

    path: str = Field(min_length=1)


class RepositorySpec(FrozenModel):
    """Git working tree to identify without mutating it."""

    path: str = Field(min_length=1)


class HostSpec(FrozenModel):
    """Public host metadata loaded from the versioned lab inventory."""

    name: str = Field(min_length=1)
    target: str = Field(min_length=1)
    role: str = Field(min_length=1)
    files: tuple[FileSpec, ...] = ()
    repositories: tuple[RepositorySpec, ...] = ()
    commands: tuple[tuple[str, ...], ...] = ()


class Inventory(FrozenModel):
    """Versioned three-PC inventory document."""

    version: Literal[1]
    hosts: tuple[HostSpec, ...]


class FileSnapshot(FrozenModel):
    """Presence and checksum evidence for one configured path."""

    path: str
    status: Literal["present", "missing", "error"]
    sha256: str | None = None
    detail: str | None = None


class RepositorySnapshot(FrozenModel):
    """Commit and dirty-state evidence for one repository."""

    path: str
    status: Literal["present", "missing", "error"]
    commit: str | None = None
    dirty: bool | None = None
    detail: str | None = None


class HostSnapshot(FrozenModel):
    """Fully redacted evidence collected from one host."""

    host: str
    target: str
    role: str
    collected_at_ns: int
    commands: tuple[CommandResult, ...]
    files: tuple[FileSnapshot, ...]
    repositories: tuple[RepositorySnapshot, ...]
    redactions: int = Field(ge=0)


class SnapshotCollector:
    """Collect only read-only host facts, then redact before returning."""

    _BASE_COMMANDS: tuple[tuple[str, ...], ...] = (
        ("hostname",),
        ("uname", "-a"),
        ("ip", "-json", "address", "show"),
        ("ip", "-json", "route", "show"),
    )
    _ASSIGNMENT = re.compile(
        r"(?im)\b(K|OPC|password|passwd|SUPI|IMSI)\b(\s*(?::|=|\s)\s*)([^\s,;]+)"
    )
    _SUPI = re.compile(r"(?i)\b(?:imsi-?)?\d{15}\b")

    def __init__(self, runner: CommandRunner, *, timeout_s: float = 5.0) -> None:
        self._runner = runner
        self._timeout_s = timeout_s

    def collect(self, host: HostSpec) -> HostSnapshot:
        """Collect a snapshot without executing any state-changing command."""

        results: list[CommandResult] = []
        redactions = 0
        for argv in (*self._BASE_COMMANDS, *host.commands):
            result = self._runner.run(host.target, argv, self._timeout_s)
            redacted, count = self._redact_result(result)
            results.append(redacted)
            redactions += count

        files: list[FileSnapshot] = []
        for file_spec in host.files:
            file_snapshot, file_results, count = self._collect_file(host.target, file_spec)
            files.append(file_snapshot)
            results.extend(file_results)
            redactions += count

        repositories: list[RepositorySnapshot] = []
        for repository in host.repositories:
            repository_snapshot, repository_results, count = self._collect_repository(
                host.target, repository
            )
            repositories.append(repository_snapshot)
            results.extend(repository_results)
            redactions += count

        return HostSnapshot(
            host=host.name,
            target=host.target,
            role=host.role,
            collected_at_ns=time.time_ns(),
            commands=tuple(results),
            files=tuple(files),
            repositories=tuple(repositories),
            redactions=redactions,
        )

    def _collect_file(
        self, target: str, file_spec: FileSpec
    ) -> tuple[FileSnapshot, list[CommandResult], int]:
        existence = self._runner.run(target, ("test", "-e", file_spec.path), self._timeout_s)
        redacted_existence, count = self._redact_result(existence)
        results = [redacted_existence]
        if existence.timed_out or existence.exit_code not in (0, 1):
            return (
                FileSnapshot(
                    path=file_spec.path,
                    status="error",
                    detail=redacted_existence.stderr or "existence check failed",
                ),
                results,
                count,
            )
        if existence.exit_code == 1:
            return FileSnapshot(path=file_spec.path, status="missing"), results, count

        checksum = self._runner.run(target, ("sha256sum", "--", file_spec.path), self._timeout_s)
        redacted_checksum, checksum_count = self._redact_result(checksum)
        results.append(redacted_checksum)
        count += checksum_count
        digest = checksum.stdout.split(maxsplit=1)[0] if checksum.exit_code == 0 else None
        if digest is None or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            return (
                FileSnapshot(
                    path=file_spec.path,
                    status="error",
                    detail=redacted_checksum.stderr or "invalid sha256sum output",
                ),
                results,
                count,
            )
        return (
            FileSnapshot(path=file_spec.path, status="present", sha256=digest.lower()),
            results,
            count,
        )

    def _collect_repository(
        self, target: str, repository: RepositorySpec
    ) -> tuple[RepositorySnapshot, list[CommandResult], int]:
        exists = self._runner.run(target, ("test", "-d", repository.path), self._timeout_s)
        redacted_exists, count = self._redact_result(exists)
        results = [redacted_exists]
        if exists.exit_code == 1 and not exists.timed_out:
            return RepositorySnapshot(path=repository.path, status="missing"), results, count
        if exists.exit_code != 0 or exists.timed_out:
            return (
                RepositorySnapshot(
                    path=repository.path,
                    status="error",
                    detail=redacted_exists.stderr or "repository existence check failed",
                ),
                results,
                count,
            )

        head = self._runner.run(
            target, ("git", "-C", repository.path, "rev-parse", "HEAD"), self._timeout_s
        )
        status = self._runner.run(
            target,
            ("git", "-C", repository.path, "status", "--porcelain", "--untracked-files=no"),
            self._timeout_s,
        )
        redacted_head, head_count = self._redact_result(head)
        redacted_status, status_count = self._redact_result(status)
        results.extend((redacted_head, redacted_status))
        count += head_count + status_count
        commit = head.stdout.strip() if head.exit_code == 0 else None
        if commit is None or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
            return (
                RepositorySnapshot(
                    path=repository.path,
                    status="error",
                    detail=redacted_head.stderr or "invalid git HEAD",
                ),
                results,
                count,
            )
        if status.exit_code != 0:
            return (
                RepositorySnapshot(
                    path=repository.path,
                    status="error",
                    commit=commit.lower(),
                    detail=redacted_status.stderr or "git status failed",
                ),
                results,
                count,
            )
        return (
            RepositorySnapshot(
                path=repository.path,
                status="present",
                commit=commit.lower(),
                dirty=bool(status.stdout.strip()),
            ),
            results,
            count,
        )

    @classmethod
    def _redact_result(cls, result: CommandResult) -> tuple[CommandResult, int]:
        stdout, stdout_count = cls._redact(result.stdout)
        stderr, stderr_count = cls._redact(result.stderr)
        return (
            result.model_copy(update={"stdout": stdout, "stderr": stderr}),
            stdout_count + stderr_count,
        )

    @classmethod
    def _redact(cls, value: str) -> tuple[str, int]:
        value, assignment_count = cls._ASSIGNMENT.subn(
            lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value
        )
        value, supi_count = cls._SUPI.subn("[REDACTED]", value)
        return value, assignment_count + supi_count


def load_inventory(path: Path) -> Inventory:
    """Load the public inventory document without environment interpolation."""

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Inventory.model_validate(document)


def _atomic_write_json(path: Path, document: object) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    """Collect all configured hosts into one timestamped output directory."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    inventory = load_inventory(arguments.inventory)
    output = arguments.output / str(time.time_ns())
    output.mkdir(parents=True, exist_ok=False)
    collector = SnapshotCollector(LocalSshCommandRunner())
    reports: list[dict[str, object]] = []
    for host in inventory.hosts:
        snapshot = collector.collect(host)
        _atomic_write_json(output / f"{host.name}.json", snapshot.model_dump(mode="json"))
        reports.append({"host": host.name, "redactions": snapshot.redactions})
    _atomic_write_json(output / "redaction-report.json", reports)
    print(output)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a deployment command
    raise SystemExit(main())
