"""Read-only audit of the UN3GPP BS MAC repository before any runtime patch.

Freezes the evidence the plan requires: repository identity and local diff,
working-tree feature flags, the UL control-path symbols, the RB allocation
type, IPC entry points and the running binary. Nothing on the host changes.
"""

import hashlib
import re
from collections.abc import Sequence
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict

from smartfarm.inventory.runner import CommandResult, CommandRunner

CONTROL_SYMBOLS = (
    "setMcsUplink",
    "getMcsUplink",
    "setUlReservation",
    "getUlReservation",
    "getUlReservations",
    "RECONFIG",
    "isOutdated",
    "setOutdated",
)
IPC_PATTERNS = ("AF_UNIX", "bind(", "recvfrom(", "listen(", "pub_mac_data")
_DEFINE = re.compile(r"^\s*#\s*define\s+([A-Z][A-Z0-9_]+)\s+([^\s/]+)", re.MULTILINE)


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SymbolHit(FrozenModel):
    symbol: str
    path: str
    line: int
    text: str


class MacAudit(FrozenModel):
    """Evidence captured from the BS MAC repository and running process."""

    host: str
    repository: str
    branch: str
    head: str
    status_short: tuple[str, ...]
    diff_sha256: str
    diff_stat: tuple[str, ...]
    feature_flags: dict[str, str]
    control_hits: tuple[SymbolHit, ...]
    ipc_hits: tuple[SymbolHit, ...]
    allocation_type: tuple[str, ...]
    binary_sha256: str | None
    running_processes: tuple[str, ...]
    errors: tuple[str, ...]


def parse_flags(header: str) -> dict[str, str]:
    """Extract `#define NAME VALUE` pairs (last definition wins, as the preprocessor does)."""

    return {name: value for name, value in _DEFINE.findall(header)}


def parse_grep(output: str, symbols: Sequence[str]) -> tuple[SymbolHit, ...]:
    """Turn `grep -rn` lines into hits attributed to the first matching symbol."""

    hits: list[SymbolHit] = []
    for line in output.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        path, number, text = parts
        symbol = next((s for s in symbols if s in text), None)
        if symbol is None:
            continue
        hits.append(SymbolHit(symbol=symbol, path=path, line=int(number), text=text.strip()[:200]))
    return tuple(hits)


def _grep(patterns: Sequence[str], path: str) -> tuple[str, ...]:
    """Fixed-string recursive grep over C/C++ sources for every pattern."""

    pattern_args = tuple(arg for pattern in patterns for arg in ("-e", pattern))
    return ("grep", "-rnF", "--include=*.cpp", "--include=*.h", *pattern_args, path)


class MacAuditCollector:
    """Collects a MacAudit through an injectable command runner."""

    def __init__(self, runner: CommandRunner, *, timeout_s: float = 30.0) -> None:
        self._runner = runner
        self._timeout_s = timeout_s
        self._errors: list[str] = []

    def _run(self, host: str, argv: Sequence[str]) -> CommandResult:
        result = self._runner.run(host, argv, self._timeout_s)
        if result.timed_out or result.exit_code not in (0, 1):  # grep exits 1 on no match
            self._errors.append(f"{' '.join(argv)[:80]}: exit={result.exit_code} timed_out={result.timed_out}")
        return result

    def collect(self, host: str, repo: PurePosixPath) -> MacAudit:
        self._errors = []
        git = ("git", "--no-pager", "-C", str(repo))
        sources = str(repo / "build/src")
        branch = self._run(host, (*git, "branch", "--show-current")).stdout.strip()
        head = self._run(host, (*git, "rev-parse", "HEAD")).stdout.strip()
        status = self._run(host, (*git, "status", "--short")).stdout.splitlines()
        diff = self._run(host, (*git, "diff")).stdout
        stat = self._run(host, (*git, "diff", "--stat")).stdout.splitlines()
        flags = self._run(host, ("cat", str(repo / "build/src/coreL2/mac_feature_flags.h"))).stdout
        control = self._run(host, _grep(CONTROL_SYMBOLS, f"{sources}/coreL2")).stdout
        ipc = self._run(host, _grep(IPC_PATTERNS, f"{sources}/coreL2")).stdout
        allocation = self._run(host, ("grep", "-rn", "-A8", "struct allocation_cfg_t\\|} allocation_cfg_t",
                                      sources)).stdout.splitlines()
        binary = self._run(host, ("sha256sum", str(repo / "build/coreL2.o"))).stdout.split()
        processes = self._run(host, ("pgrep", "-a", "coreL2")).stdout.splitlines()
        return MacAudit(
            host=host,
            repository=str(repo),
            branch=branch,
            head=head,
            status_short=tuple(status),
            diff_sha256=hashlib.sha256(diff.encode()).hexdigest(),
            diff_stat=tuple(stat),
            feature_flags=parse_flags(flags),
            control_hits=parse_grep(control, CONTROL_SYMBOLS),
            ipc_hits=parse_grep(ipc, IPC_PATTERNS),
            allocation_type=tuple(allocation[:40]),
            binary_sha256=binary[0] if binary else None,
            running_processes=tuple(processes),
            errors=tuple(self._errors),
        )
