"""Live evidence that a UE holds a PDU session in a given slice, from the Open5GS AMF and SMF logs.

The AMF logs the S-NSSAI and DNN of every PDU session it routes (`UE SUPI[..] DNN[..] S_NSSAI[..]`, after its slice
selection) but no address; the SMF logs the address (`UE SUPI[..] DNN[..] IPv4[..]`) but no S-NSSAI. A session is
attributed to a slice by pairing, for the same SUPI and DNN, an AMF request with the next SMF establishment, and is
only accepted after it stayed up for a hold time (the OnePlus once released sessions ~100 ms after setup). SUPIs are
compared in memory only; evidence carries the endpoint role and device reference.
"""

import asyncio
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from ipaddress import IPv4Address
from pathlib import Path

from smartfarm.baseline.endpoints import EndpointRole
from smartfarm.baseline.open5gs_sessions import UeIdentity, read_current_run
from smartfarm.contracts.service import Snssai

DEFAULT_AMF_LOG = Path("/var/log/open5gs/amf.log")
DEFAULT_SMF_LOG = Path("/var/log/open5gs/smf.log")

_TS = r"(?P<ts>\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{3})"
_AMF_REQUEST = re.compile(
    _TS + r": \[gmm\] INFO: UE SUPI\[(?P<supi>[^\]]+)\] DNN\[(?P<dnn>[^\]]*)\] "
    r"S_NSSAI\[SST:(?P<sst>\d+) SD:0x(?P<sd>[0-9a-fA-F]+)\]"
)
_SMF_ADDED = re.compile(
    _TS + r": \[smf\] INFO: UE SUPI\[(?P<supi>[^\]]+)\] DNN\[(?P<dnn>[^\]]*)\] IPv4\[(?P<ipv4>[0-9.]+)\]"
)
_SMF_REMOVED = re.compile(
    _TS + r": \[smf\] INFO: Removed Session: UE IMSI:\[(?P<supi>[^\]]+)\] "
    r"DNN:\[(?P<dnn>[^\]:]*):(?P<psi>\d+)\] IPv4:\[(?P<ipv4>[0-9.]+)\]"
)


def log_time_to_epoch(ts: str, year: int | None = None) -> float:
    """Open5GS log timestamp (`MM/DD HH:MM:SS.mmm`, local time, no year) as a Unix epoch."""

    year = year or datetime.now().year
    return datetime.strptime(f"{year}/{ts}", "%Y/%m/%d %H:%M:%S.%f").timestamp()


class EvidenceTimeout(TimeoutError):
    """The expected core evidence did not appear in time."""


@dataclass(frozen=True)
class LogCursor:
    """Position in a log file; survives one logrotate (`<file>.1`) between reads."""

    path: Path
    inode: int
    offset: int

    @classmethod
    def at_end(cls, path: Path) -> "LogCursor":
        stat = path.stat()
        return cls(path=path, inode=stat.st_ino, offset=stat.st_size)

    def read(self) -> tuple[list[str], "LogCursor"]:
        """Complete lines appended since this cursor and the cursor after them."""

        chunks: list[bytes] = []
        stat = self.path.stat()
        offset = self.offset
        if stat.st_ino != self.inode or stat.st_size < offset:
            rotated = self.path.with_name(self.path.name + ".1")
            if rotated.exists() and rotated.stat().st_ino == self.inode:
                with rotated.open("rb") as handle:
                    handle.seek(offset)
                    chunks.append(handle.read())
            offset = 0
        with self.path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
        end = data.rfind(b"\n") + 1  # keep a partial last line for the next read
        chunks.append(data[:end])
        text = b"".join(chunks).decode("utf-8", errors="replace")
        return text.splitlines(), LogCursor(path=self.path, inode=stat.st_ino, offset=offset + end)


@dataclass(frozen=True)
class EvidenceCursor:
    amf: LogCursor
    smf: LogCursor


@dataclass(frozen=True)
class SessionEvidence:
    """A PDU session of one UE in one slice, confirmed by the core."""

    role: EndpointRole
    device_ref: str
    snssai: Snssai
    dnn: str
    ipv4: IPv4Address
    requested_at: str  # AMF log timestamp of the routed PDU session request
    established_at: str  # SMF log timestamp of the address allocation
    held_s: float  # how long it was observed up without removal


@dataclass(frozen=True)
class CurrentSession:
    """Latest still-active session of a UE according to the whole current logs."""

    snssai: Snssai
    dnn: str
    ipv4: IPv4Address
    established_at: str
    requested_at: str | None = None  # AMF log timestamp of the routed request


@dataclass(frozen=True)
class _Candidate:
    dnn: str
    snssai: tuple[int, str]
    requested_at: str
    ipv4: IPv4Address | None = None
    established_at: str | None = None
    removed: bool = False


def correlate(amf_lines: list[str], smf_lines: list[str], supi: str) -> list[_Candidate]:
    """Pair, in time order, each AMF request of `supi` with its SMF establishment and later removal."""

    events: list[tuple[str, int, re.Match[str]]] = []
    for line in amf_lines:
        if (m := _AMF_REQUEST.search(line)) and m["supi"] == supi:
            events.append((m["ts"], 0, m))
    for line in smf_lines:
        for kind, pattern in ((1, _SMF_ADDED), (2, _SMF_REMOVED)):
            if (m := pattern.search(line)) and m["supi"] == supi:
                events.append((m["ts"], kind, m))
    events.sort(key=lambda event: (event[0], event[1]))  # AMF request before SMF add at the same ms

    candidates: list[_Candidate] = []
    for _ts, kind, m in events:
        if kind == 0:
            candidates.append(
                _Candidate(dnn=m["dnn"], snssai=(int(m["sst"]), f"{int(m['sd'], 16):06x}"), requested_at=m["ts"])
            )
        elif kind == 1:
            for i in range(len(candidates) - 1, -1, -1):
                c = candidates[i]
                if c.dnn == m["dnn"] and c.ipv4 is None:
                    candidates[i] = replace(c, ipv4=IPv4Address(m["ipv4"]), established_at=m["ts"])
                    break
        else:
            ipv4 = IPv4Address(m["ipv4"])
            candidates = [replace(c, removed=True) if c.ipv4 == ipv4 else c for c in candidates]
    return candidates


class Open5gsLogEvidenceProvider:
    """Waits for slice evidence of the lab UEs by tailing the AMF and SMF logs."""

    def __init__(
        self,
        identities: Mapping[EndpointRole, UeIdentity],
        *,
        amf_log: Path = DEFAULT_AMF_LOG,
        smf_log: Path = DEFAULT_SMF_LOG,
        poll_s: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        for path in (amf_log, smf_log):
            if not os.access(path, os.R_OK):
                raise PermissionError(f"cannot read {path} (restart_core.sh grant_session_log_read)")
        self._identities = identities
        self._amf_log = amf_log
        self._smf_log = smf_log
        self._poll_s = poll_s
        self._clock = clock

    def cursor(self) -> EvidenceCursor:
        """Mark "now": only events logged after this point count as evidence."""

        return EvidenceCursor(amf=LogCursor.at_end(self._amf_log), smf=LogCursor.at_end(self._smf_log))

    def active_sessions(self, role: EndpointRole) -> list[CurrentSession]:
        """Every session of `role` established and not removed since the daemons last started, oldest first.

        Reads through the daily logrotate: a session set up before midnight lives in `<log>.1[.gz]`. The Smart Farm
        app keeps the default `internet` session and opens one per service, so the phone can hold several.
        """

        amf = read_current_run(self._amf_log)
        smf = read_current_run(self._smf_log)
        sessions = [
            CurrentSession(
                snssai=Snssai(sst=c.snssai[0], sd=c.snssai[1]), dnn=c.dnn, ipv4=c.ipv4,
                established_at=c.established_at, requested_at=c.requested_at,
            )
            for c in correlate(amf, smf, self._identities[role].supi)
            if c.ipv4 is not None and c.established_at is not None and not c.removed
        ]
        return sorted(sessions, key=lambda session: session.established_at)

    def current_session(self, role: EndpointRole) -> CurrentSession | None:
        """Most recent session of `role` that was established and not removed since the daemons last started."""

        active = self.active_sessions(role)
        return active[-1] if active else None

    async def await_active(
        self,
        role: EndpointRole,
        snssai: Snssai,
        dnn: str,
        ipv4: IPv4Address,
        *,
        timeout_s: float,
        hold_s: float = 2.0,
    ) -> SessionEvidence:
        """Confirm that `role` holds (or gets within `timeout_s`) the session `ipv4` in `snssai`/`dnn`, up `hold_s`.

        Unlike `await_session`, the session may predate the call (the app opened it before asking the orchestrator);
        its age from the SMF timestamp (same host clock) counts towards the hold.
        """

        target = (snssai.sst, snssai.sd.lower())
        first: float | None = None
        deadline = self._clock() + timeout_s
        while True:
            match = next(
                (s for s in self.active_sessions(role)
                 if s.ipv4 == ipv4 and s.dnn == dnn and (s.snssai.sst, s.snssai.sd.lower()) == target),
                None,
            )
            now = self._clock()
            if match is None:
                first = None
            else:
                first = now if first is None else first
                try:
                    age = time.time() - log_time_to_epoch(match.established_at)
                except ValueError:
                    age = 0.0
                held = max(now - first, min(age, hold_s))
                if held >= hold_s:
                    return SessionEvidence(
                        role=role,
                        device_ref=self._identities[role].device_ref,
                        snssai=snssai,
                        dnn=dnn,
                        ipv4=ipv4,
                        requested_at=match.requested_at or match.established_at,
                        established_at=match.established_at,
                        held_s=round(held, 3),
                    )
            if now >= deadline:
                raise EvidenceTimeout(
                    f"{role.value} has no active {dnn} session {ipv4} in SST {target[0]} SD {target[1]} "
                    f"(held {hold_s} s) within {timeout_s} s"
                )
            await asyncio.sleep(self._poll_s)

    async def await_session(
        self,
        role: EndpointRole,
        snssai: Snssai,
        dnn: str,
        since: EvidenceCursor,
        *,
        timeout_s: float,
        hold_s: float = 2.0,
    ) -> SessionEvidence:
        """First session of `role` in `snssai`/`dnn` logged after `since` that stays up for `hold_s`."""

        supi = self._identities[role].supi
        target = (snssai.sst, snssai.sd.lower())
        amf_lines: list[str] = []
        smf_lines: list[str] = []
        amf_cursor, smf_cursor = since.amf, since.smf
        seen_at: dict[IPv4Address, float] = {}
        deadline = self._clock() + timeout_s
        while True:
            new_amf, amf_cursor = amf_cursor.read()
            new_smf, smf_cursor = smf_cursor.read()
            amf_lines += new_amf
            smf_lines += new_smf
            now = self._clock()
            for c in correlate(amf_lines, smf_lines, supi):
                if c.snssai != target or c.dnn != dnn or c.ipv4 is None or c.removed:
                    continue
                first = seen_at.setdefault(c.ipv4, now)
                if now - first >= hold_s:
                    assert c.established_at is not None
                    return SessionEvidence(
                        role=role,
                        device_ref=self._identities[role].device_ref,
                        snssai=snssai,
                        dnn=dnn,
                        ipv4=c.ipv4,
                        requested_at=c.requested_at,
                        established_at=c.established_at,
                        held_s=round(now - first, 3),
                    )
            if now >= deadline:
                raise EvidenceTimeout(
                    f"no stable {dnn} session in SST {target[0]} SD {target[1]} for {role.value} within {timeout_s} s"
                )
            await asyncio.sleep(self._poll_s)

    async def await_absent(
        self, role: EndpointRole, ipv4: IPv4Address, since: EvidenceCursor, *, timeout_s: float
    ) -> str | None:
        """SMF log timestamp of the removal of `role`'s session with `ipv4` after `since`, or None on timeout."""

        supi = self._identities[role].supi
        cursor = since.smf
        deadline = self._clock() + timeout_s
        while True:
            lines, cursor = cursor.read()
            for line in lines:
                m = _SMF_REMOVED.search(line)
                if m and m["supi"] == supi and IPv4Address(m["ipv4"]) == ipv4:
                    return m["ts"]
            if self._clock() >= deadline:
                return None
            await asyncio.sleep(self._poll_s)
