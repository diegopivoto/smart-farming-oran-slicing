"""Resolve current UE IPv4 addresses from the Open5GS SMF session log.

Open5GS v2.7.5 has no queryable PDU-session API, but its SMF logs every
session it creates and removes together with the SUPI and the allocated IPv4.
Replaying those events since the last SMF start yields the sessions the core
considers active right now. Permanent identifiers stay in memory and in the
protected identity file; resolved selections only carry pseudonymous roles.
"""

import gzip
import json
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from smartfarm.baseline.endpoints import (
    EndpointAssignment,
    EndpointConfigurationError,
    EndpointRole,
    EndpointSelection,
    EndpointSource,
    SessionResolution,
    validate_ue_addresses,
)

DEFAULT_SMF_LOG = Path("/var/log/open5gs/smf.log")
DEFAULT_IDENTITIES = Path.home() / ".config" / "smartfarm" / "ue-identities.json"

_TIMESTAMP = r"(?P<timestamp>\d\d/\d\d \d\d:\d\d:\d\d\.\d+)"
_ADDED = re.compile(
    _TIMESTAMP + r": \[smf\] INFO: UE SUPI\[(?P<supi>[^\]]+)\] DNN\[(?P<dnn>[^\]]*)\] "
    r"IPv4\[(?P<ipv4>[^\]]*)\]"
)
_REMOVED = re.compile(
    _TIMESTAMP + r": \[smf\] INFO: Removed Session: UE IMSI:\[(?P<supi>[^\]]+)\] "
    r"DNN:\[(?P<dnn>[^\]:]*):(?P<psi>\d+)\] IPv4:\[(?P<ipv4>[^\]]*)\]"
)
_DAEMON_START = re.compile(r"^Open5GS daemon v")
_DAEMON_STOP = re.compile(r"\[app\] INFO: Open5GS daemon terminating")


class SessionResolutionError(EndpointConfigurationError):
    """Raised when the core state does not identify each UE unambiguously."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class UeIdentity(FrozenModel):
    """Secret role-to-subscriber mapping entry; never written to artifacts."""

    supi: str
    device_ref: str


@dataclass(frozen=True)
class ActiveSession:
    supi: str
    dnn: str
    ipv4: IPv4Address
    established_at: str
    order: int = 0  # position of the establishment event in the replayed log


def load_identities(path: Path = DEFAULT_IDENTITIES) -> dict[EndpointRole, UeIdentity]:
    """Load the protected role-to-SUPI mapping, refusing world/group-readable files."""

    try:
        mode = path.stat().st_mode
    except FileNotFoundError as error:
        raise SessionResolutionError(f"UE identity file not found: {path}") from error
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SessionResolutionError(f"UE identity file must be private (chmod 600): {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    identities = {EndpointRole(role): UeIdentity.model_validate(entry) for role, entry in document.items()}
    if set(identities) != set(EndpointRole):
        raise SessionResolutionError("UE identity file must define every endpoint role")
    if len({identity.supi for identity in identities.values()}) != len(identities):
        raise SessionResolutionError("UE identity file maps two roles to the same subscriber")
    return identities


def _read_lines(path: Path) -> list[str]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def read_current_run(path: Path) -> list[str]:
    """Return the lines of an Open5GS log since the daemon's last start, reaching into the rotated file if needed.

    The daily logrotate (00:00) leaves the current file without the start banner; the predecessor is `<file>.1`
    right after rotation or `<file>.1.gz` once compressed.
    """

    try:
        lines = _read_lines(path)
    except PermissionError as error:
        raise SessionResolutionError(
            f"cannot read {path}; run open5gs-core-lab/restart_core.sh to grant the ACL"
        ) from error
    if not any(_DAEMON_START.search(line) for line in lines):
        for rotated in (path.with_name(path.name + ".1"), path.with_name(path.name + ".1.gz")):
            if rotated.exists():
                try:
                    lines = _read_lines(rotated) + lines
                except PermissionError as error:
                    raise SessionResolutionError(f"cannot read rotated log {rotated}") from error
                break
    starts = [i for i, line in enumerate(lines) if _DAEMON_START.search(line)]
    return lines[starts[-1]:] if starts else lines


def read_smf_log(path: Path = DEFAULT_SMF_LOG) -> list[str]:
    """Return SMF log lines covering the current SMF run, including the rotated file if needed."""

    return read_current_run(path)


def active_sessions(lines: Iterable[str]) -> dict[str, list[ActiveSession]]:
    """Replay SMF events since the last daemon start; return active sessions per SUPI."""

    active: dict[tuple[str, IPv4Address], ActiveSession] = {}
    for order, line in enumerate(lines):
        if _DAEMON_START.search(line) or _DAEMON_STOP.search(line):
            active.clear()
            continue
        if (match := _ADDED.search(line)) is not None:
            if not match["ipv4"]:
                continue
            session = ActiveSession(
                supi=match["supi"],
                dnn=match["dnn"],
                ipv4=IPv4Address(match["ipv4"]),
                established_at=match["timestamp"],
                order=order,
            )
            active[(session.supi, session.ipv4)] = session
        elif (match := _REMOVED.search(line)) is not None and match["ipv4"]:
            active.pop((match["supi"], IPv4Address(match["ipv4"])), None)

    by_supi: dict[str, list[ActiveSession]] = {}
    for session in active.values():
        by_supi.setdefault(session.supi, []).append(session)
    return by_supi


def resolve_open5gs_endpoints(
    *,
    lines: Iterable[str],
    identities: Mapping[EndpointRole, UeIdentity],
    pdu_network: IPv4Network,
    gateway: IPv4Address,
    log_path: Path,
    dnn: str | None = None,
) -> EndpointSelection:
    """Select the most recent active session per role from the SMF event history.

    A slice switch is a new PDU session (any DNN: internet, crop, drone), and a UE restarted without a clean
    release can leave its old session in the SMF, so the latest establishment wins; older ones are counted in
    the resolution evidence. `dnn` restricts the candidates to one DNN.
    """

    sessions = active_sessions(lines)
    chosen: dict[EndpointRole, ActiveSession] = {}
    others: dict[EndpointRole, int] = {}
    for role in EndpointRole:
        candidates = [
            session
            for session in sessions.get(identities[role].supi, [])
            if (dnn is None or session.dnn == dnn) and session.ipv4 in pdu_network
        ]
        if not candidates:
            raise SessionResolutionError(
                f"Open5GS SMF has no active {dnn or 'PDU'} session for {role.value}"
            )
        chosen[role] = max(candidates, key=lambda session: session.order)
        others[role] = len(candidates) - 1

    validate_ue_addresses(
        un3gpp_ip=chosen[EndpointRole.UN3GPP_UE].ipv4,
        oneplus_ip=chosen[EndpointRole.ONEPLUS_8T].ipv4,
        pdu_network=pdu_network,
        gateway=gateway,
    )
    return EndpointSelection(
        source=EndpointSource.OPEN5GS_SMF_LOG,
        pdu_network=pdu_network,
        gateway=gateway,
        assignments=tuple(  # type: ignore[arg-type]
            EndpointAssignment(
                role=role,
                device_ref=identities[role].device_ref,
                ipv4=chosen[role].ipv4,
            )
            for role in (EndpointRole.UN3GPP_UE, EndpointRole.ONEPLUS_8T)
        ),
        resolution=tuple(  # type: ignore[arg-type]
            SessionResolution(
                role=role,
                dnn=chosen[role].dnn,
                established_at=chosen[role].established_at,
                log_path=str(log_path),
                other_active_sessions=others[role],
            )
            for role in (EndpointRole.UN3GPP_UE, EndpointRole.ONEPLUS_8T)
        ),
    )
