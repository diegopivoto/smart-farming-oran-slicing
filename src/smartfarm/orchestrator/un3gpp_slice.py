"""Drive the existing N3IWUE live slice switch (PDU session release/establish, IKE kept) from PC1.

Control goes through the 5G network: the PC1 orchestrator reaches the PC3 node agent at the UN3GPP UE's PDU-session
address (PC1 -> UPF -> N3IWF -> IPsec -> UE), resolved from the SMF log for every call, since a slice switch gives the
UE a new address. The SSH transport is only a lab management fallback.
"""

import asyncio
import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path

from smartfarm.contracts.service import SERVICE_CATALOG, ServiceId, ServiceProfile

# N3IWUE SmPolicy order in PC3 n3ue.yaml: slice N = SmPolicy[N-1] = SD 00000N
SLICE_INDEX: Mapping[ServiceId, int] = {ServiceId.WEATHER: 1, ServiceId.CROP: 2, ServiceId.DRONE: 3}
PC3_HELPER = "/opt/smartfarm/n3iwue_slice_client.py"

_STATUS = re.compile(
    r"^STATUS slice=(?P<slice>\d+) sst=(?P<sst>\d+) sd=(?P<sd>[0-9A-Fa-f]{6}) pdu_session_id=(?P<psi>\d+) "
    r"vrf=(?P<vrf>\S+) iface=(?P<iface>\S+) ip=(?P<ip>[0-9.]+) status=(?P<status>\w+)$"
)

Transport = Callable[[list[str]], Awaitable[str]]  # ["status"] | ["slice", "N"] | ["ping", ip] -> one reply line
AGENT_PORT = 9700


class Un3gppSliceError(RuntimeError):
    """The N3IWUE refused, rejected or did not finish the slice switch."""


class Un3gppSliceUnreachable(Un3gppSliceError):
    """The UE could not be reached (expected while its PDU session is being replaced)."""


@dataclass(frozen=True)
class Un3gppSliceStatus:
    slice_index: int
    sst: int
    sd: str
    pdu_session_id: int
    vrf: str
    iface: str
    ipv4: IPv4Address
    status: str  # accepted | pending | rejected


@dataclass(frozen=True)
class PingResult:
    """UE-to-UE echo test run by the UN3GPP UE inside its 5G PDU session."""

    target: IPv4Address
    sent: int
    received: int
    rtt_min_ms: float
    rtt_avg_ms: float
    rtt_max_ms: float

    @property
    def loss_ratio(self) -> float:
        return 1.0 - self.received / self.sent if self.sent else 1.0


_PING = re.compile(
    r"^PING sent=(?P<sent>\d+) received=(?P<received>\d+) rtt_min_ms=(?P<min>[\d.]+|nan) "
    r"rtt_avg_ms=(?P<avg>[\d.]+|nan) rtt_max_ms=(?P<max>[\d.]+|nan)$"
)


def parse_status(line: str) -> Un3gppSliceStatus | None:
    """None while the UE has no active slice (`STATUS none status=...`)."""

    if line.startswith("STATUS none"):
        return None
    m = _STATUS.match(line.strip())
    if m is None:
        raise Un3gppSliceError(f"unexpected N3IWUE reply: {line!r}")
    return Un3gppSliceStatus(
        slice_index=int(m["slice"]),
        sst=int(m["sst"]),
        sd=m["sd"].lower(),
        pdu_session_id=int(m["psi"]),
        vrf=m["vrf"],
        iface=m["iface"],
        ipv4=IPv4Address(m["ip"]),
        status=m["status"],
    )


def ssh_transport(host: str = "smartfarm-pc3", helper: str = PC3_HELPER, timeout_s: float = 15.0) -> Transport:
    """Run the PC3 helper over SSH (BatchMode, key auth)."""

    async def run(argv: list[str]) -> str:
        process = await asyncio.create_subprocess_exec(
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "python3", helper, *argv,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout_s)
        except TimeoutError:
            process.kill()
            raise Un3gppSliceError(f"PC3 helper timed out after {timeout_s} s") from None
        reply = out.decode(errors="replace").strip()
        if not reply:
            raise Un3gppSliceError(f"PC3 helper failed: {err.decode(errors='replace').strip()[-200:]}")
        return reply

    return run


def load_agent_key(path: Path) -> bytes:
    if path.stat().st_mode & 0o077:
        raise PermissionError(f"node agent key must be private (chmod 600): {path}")
    key = path.read_bytes().strip()
    if len(key) < 32:
        raise ValueError("node agent key too short")
    return key


def sign_request(key: bytes, command: str, issued_at_ns: int, nonce: str) -> str:
    mac = hmac.new(key, f"{issued_at_ns} {nonce} {command}".encode(), hashlib.sha256).hexdigest()
    return f"{issued_at_ns} {nonce} {command} {mac}"


def agent_transport(
    resolve_ip: Callable[[], IPv4Address],
    key: bytes,
    *,
    port: int = AGENT_PORT,
    connect_timeout_s: float = 2.0,
    timeout_s: float = 8.0,
) -> Transport:
    """Talk to the PC3 node agent at the UE's current 5G address (`resolve_ip` is called on every request)."""

    async def run(argv: list[str]) -> str:
        command = "status" if argv == ["status"] else "-".join(argv)
        line = sign_request(key, command, time.time_ns(), secrets.token_hex(16))
        try:
            address = str(resolve_ip())
            # short connect timeout: during a slice switch the previous address silently disappears
            reader, writer = await asyncio.wait_for(asyncio.open_connection(address, port), connect_timeout_s)
            try:
                writer.write(line.encode() + b"\n")
                await writer.drain()
                reply = (await asyncio.wait_for(reader.readline(), timeout_s)).decode(errors="replace").strip()
            finally:
                writer.close()
        except (OSError, TimeoutError, ValueError) as error:
            raise Un3gppSliceUnreachable(f"node agent unreachable through the 5G network: {error}") from error
        if not reply or reply.startswith("ERROR"):
            raise Un3gppSliceError(f"node agent refused: {reply!r}")
        return reply

    return run


class Un3gppSliceClient:
    def __init__(
        self,
        transport: Transport,
        *,
        catalog: Mapping[ServiceId, ServiceProfile] = SERVICE_CATALOG,
        poll_s: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._catalog = catalog
        self._poll_s = poll_s
        self._clock = clock

    async def status(self) -> Un3gppSliceStatus | None:
        return parse_status(await self._transport(["status"]))

    async def start_traffic(
        self, service: ServiceId, target: IPv4Address, seconds: int, run_id: str, rate_bps: int | None = None
    ) -> str:
        """Start the service generator on the UE towards the phone (through the 5G network)."""

        argv = ["traffic", service.value, str(target), str(seconds), run_id]
        reply = await self._transport(argv + ([str(rate_bps)] if rate_bps else []))
        if not reply.startswith("TRAFFIC started"):
            raise Un3gppSliceError(f"traffic start refused: {reply!r}")
        return reply

    async def stop_traffic(self) -> str:
        return await self._transport(["traffic", "stop"])

    async def ping(self, target: IPv4Address) -> PingResult:
        reply = await self._transport(["ping", str(target)])
        m = _PING.match(reply)
        if m is None:
            raise Un3gppSliceError(f"unexpected ping reply: {reply!r}")
        return PingResult(
            target=target, sent=int(m["sent"]), received=int(m["received"]),
            rtt_min_ms=float(m["min"]), rtt_avg_ms=float(m["avg"]), rtt_max_ms=float(m["max"]),
        )

    def _is_target(self, status: Un3gppSliceStatus | None, service: ServiceId) -> bool:
        snssai = self._catalog[service].snssai
        return (
            status is not None
            and status.status == "accepted"
            and (status.sst, status.sd) == (snssai.sst, snssai.sd.lower())
        )

    async def request(self, service: ServiceId) -> bool:
        """Ask for the service slice; False (no request sent) when it is already active and accepted."""

        if self._is_target(await self.status(), service):
            return False
        reply = await self._transport(["slice", str(SLICE_INDEX[service])])
        if reply != "OK requested":
            raise Un3gppSliceError(f"N3IWUE refused the slice request: {reply!r}")
        return True

    async def await_status(
        self, service: ServiceId, previous_pdu_id: int | None, *, timeout_s: float = 20.0
    ) -> Un3gppSliceStatus:
        """Wait until the service slice is accepted on a PDU session other than `previous_pdu_id`."""

        deadline = self._clock() + timeout_s
        status: Un3gppSliceStatus | None = None
        while True:
            try:
                status = await self.status()
            except Un3gppSliceUnreachable:
                status = None  # old session released, new one not reachable yet
                if self._clock() >= deadline:
                    raise
                await asyncio.sleep(self._poll_s)
                continue
            if status is not None and status.status == "rejected":
                raise Un3gppSliceError(f"N3IWUE slice switch rejected (active SD {status.sd})")
            if status is not None and self._is_target(status, service) and (
                previous_pdu_id is None or status.pdu_session_id != previous_pdu_id
            ):
                return status
            if self._clock() >= deadline:
                raise Un3gppSliceError(f"N3IWUE did not reach {service.value} within {timeout_s} s: {status}")
            await asyncio.sleep(self._poll_s)
