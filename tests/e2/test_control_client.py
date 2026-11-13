import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID

import pytest

from smartfarm.contracts.radio import PrbQuotaAction, QuotaStatus
from smartfarm.contracts.service import Snssai
from smartfarm.e2.control_client import (
    ActionIdAllocator,
    E2ControlClient,
    E2ControlRejected,
    E2ControlTimeout,
    E2EpochChanged,
)

RUN = UUID("01234567-89ab-cdef-0123-456789abcdef")
Responder = Callable[[str, asyncio.StreamWriter], Awaitable[None]]


def action(action_id: int, prb: int = 52) -> PrbQuotaAction:
    return PrbQuotaAction(
        action_id=action_id,
        run_id=RUN,
        snssai=Snssai(sst=1, sd="000001"),
        prb_quota=prb,
        ttl_ms=30000,
        issued_at_ns=time.time_ns(),
    )


def outcome_label(action_id: int, status: str = "APPLIED", prb: int = 52, at_ns: int | None = None) -> str:
    return (
        f"sf=0.1;kind=outcome;action_id={action_id};status={status};dup=0;sst=1;sd=000001;rnti=0;"
        f"requested={prb};applied={prb};previous=106;token=00000000000000aa;expires_at_ns=1;version=1;"
        f"at_ns={at_ns if at_ns is not None else time.time_ns()}"
    )


class FakeXapp:
    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.lines: list[str] = []
        self.seq = 0
        self.epoch = "epoch-a"

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while raw := await reader.readline():
                line = raw.decode().rstrip("\n")
                self.lines.append(line)
                await self.responder(line, writer)
        finally:
            writer.close()

    def indication(self, labels: list[str]) -> bytes:
        self.seq += 1
        event = {"type": "indication", "epoch": self.epoch, "seq": self.seq, "received_at_ns": 1, "labels": labels}
        return (json.dumps(event) + "\n").encode()


def label_of(line: str) -> str:
    assert line.startswith("CTRL ")
    return line[5:]


def action_id_of(line: str) -> int:
    return int(dict(p.split("=", 1) for p in label_of(line).split(";"))["action_id"])


async def serve(tmp_path: Path, responder: Responder) -> tuple[FakeXapp, asyncio.Server, Path]:
    fake = FakeXapp(responder)
    path = tmp_path / "sf.sock"
    server = await asyncio.start_unix_server(fake.handle, path=str(path))
    return fake, server, path


def control_ok(line: str) -> bytes:
    return (json.dumps({"type": "control", "accepted": True, "e2_success": True, "label": label_of(line)}) + "\n").encode()


@pytest.mark.asyncio
async def test_apply_returns_outcome_with_matching_action_id(tmp_path: Path) -> None:
    async def responder(line: str, writer: asyncio.StreamWriter) -> None:
        writer.write(control_ok(line))
        # unrelated outcome first, then the matching one
        writer.write(fake.indication([outcome_label(999), "sf=0.1;kind=rule;sst=1;sd=000001;rnti=0;prb=52;action_id=7;expires_at_ns=9;version=1"]))
        writer.write(fake.indication([outcome_label(action_id_of(line))]))
        await writer.drain()

    fake, server, path = await serve(tmp_path, responder)
    events: list[dict[str, object]] = []
    async with server, E2ControlClient(path, sink=events.append) as client:
        outcome = await client.apply(action(7))
        assert outcome.action_id == 7 and outcome.status is QuotaStatus.APPLIED
        assert fake.lines[0].startswith("CTRL sf=0.1;op=prb_quota;action_id=7;")
        snapshot = client.snapshot
        assert snapshot is not None and snapshot.outcomes[0].action_id == 7
    assert any(e.get("type") == "action_sent" for e in events)


@pytest.mark.asyncio
async def test_rejected_outcome_is_returned_not_raised(tmp_path: Path) -> None:
    async def responder(line: str, writer: asyncio.StreamWriter) -> None:
        writer.write(control_ok(line))
        writer.write(fake.indication([outcome_label(action_id_of(line), status="REJECTED_STALE")]))
        await writer.drain()

    fake, server, path = await serve(tmp_path, responder)
    async with server, E2ControlClient(path) as client:
        outcome = await client.apply(action(8))
        assert outcome.status is QuotaStatus.REJECTED_STALE and not outcome.succeeded


@pytest.mark.asyncio
async def test_timeout_when_readback_never_shows_the_action(tmp_path: Path) -> None:
    async def responder(line: str, writer: asyncio.StreamWriter) -> None:
        writer.write(control_ok(line))
        writer.write(fake.indication([outcome_label(1)]))
        await writer.drain()

    fake, server, path = await serve(tmp_path, responder)
    async with server, E2ControlClient(path) as client:
        with pytest.raises(E2ControlTimeout):
            await client.apply(action(9), timeout_s=0.3)


@pytest.mark.asyncio
async def test_undelivered_control_is_rejected(tmp_path: Path) -> None:
    async def responder(line: str, writer: asyncio.StreamWriter) -> None:
        writer.write(b'{"type":"control","accepted":true,"e2_success":false}\n')
        await writer.drain()

    _, server, path = await serve(tmp_path, responder)
    async with server, E2ControlClient(path) as client:
        with pytest.raises(E2ControlRejected):
            await client.apply(action(10), timeout_s=0.5)


@pytest.mark.asyncio
async def test_stale_outcome_of_replayed_action_is_ignored(tmp_path: Path) -> None:
    async def responder(line: str, writer: asyncio.StreamWriter) -> None:
        writer.write(control_ok(line))
        writer.write(fake.indication([outcome_label(action_id_of(line), at_ns=1)]))  # from an earlier request
        await writer.drain()

    fake, server, path = await serve(tmp_path, responder)
    async with server, E2ControlClient(path) as client:
        with pytest.raises(E2ControlTimeout):
            await client.apply(action(11), timeout_s=0.3)


@pytest.mark.asyncio
async def test_epoch_change_fails_pending_action(tmp_path: Path) -> None:
    async def responder(line: str, writer: asyncio.StreamWriter) -> None:
        writer.write(control_ok(line))
        fake.epoch = "epoch-b"
        writer.write(fake.indication([]))
        await writer.drain()

    fake, server, path = await serve(tmp_path, responder)
    async with server, E2ControlClient(path) as client:
        client.handle_line(fake.indication([]))  # learn epoch-a first
        with pytest.raises(E2EpochChanged):
            await client.apply(action(12), timeout_s=1.0)


@pytest.mark.asyncio
async def test_malformed_lines_are_counted_and_ignored(tmp_path: Path) -> None:
    async def responder(line: str, writer: asyncio.StreamWriter) -> None:
        writer.write(b"not json\n")
        writer.write(b'{"type":"indication","epoch":"e","seq":1,"labels":["sf=0.1;kind=bogus"]}\n')
        writer.write(control_ok(line))
        writer.write(fake.indication([outcome_label(action_id_of(line))]))
        await writer.drain()

    fake, server, path = await serve(tmp_path, responder)
    async with server, E2ControlClient(path) as client:
        fake.epoch = "e"
        fake.seq = 1  # the injected malformed indication already used seq 1
        outcome = await client.apply(action(13))
        assert outcome.action_id == 13
        assert client.malformed_lines == 2


def test_action_ids_strictly_increase() -> None:
    allocator = ActionIdAllocator()
    ids = [allocator.next() for _ in range(1000)]
    assert ids == sorted(set(ids))


@pytest.mark.asyncio
async def test_un3gpp_action_resolves_and_waits_for_final_status(tmp_path: Path) -> None:
    from smartfarm.contracts.radio import Un3gppUlAction

    async def responder(line: str, writer: asyncio.StreamWriter) -> None:
        aid = action_id_of(line)
        writer.write(control_ok(line))
        for status in ("SENT", "CONFIRMED"):
            event = {"type": "indication", "epoch": fake.epoch, "seq": fake.seq + 1, "node": "un3gpp", "labels": [
                f"sf=0.1;kind=un3gpp_outcome;action_id={aid};mac_seq=5;status={status};dup=0;ue=1;mcs=9;rb=66;"
                f"at_ns={time.time_ns()}",
                "sf=0.1;kind=un3gpp_ue;ue=1;override=1;mcs_cmd=9;rb_first=0;rb_cmd=66;mcs_obs=9;rb_obs=66;la_mcs=11;"
                "snr_db=24.10;pdus=180;mac_mbps=12.500;age_ms=300"]}
            fake.seq += 1
            writer.write((json.dumps(event) + "\n").encode())
            await writer.drain()
            await asyncio.sleep(0.2)

    fake, server, path = await serve(tmp_path, responder)
    async with server, E2ControlClient(path) as client:
        action = Un3gppUlAction(action_id=55, run_id=RUN, ue=1, mcs=9, number_rb=66, ttl_ms=30000,
                                issued_at_ns=time.time_ns())
        first = await client.apply(action)
        assert first.action_id == 55
        final = await client.wait_un3gpp_final(55, timeout_s=3)
        assert final.status == "CONFIRMED"
        snap = client.snapshot_for("un3gpp")
        assert snap is not None and snap.un3gpp_ues[0].rb_observed == 66
        assert client.snapshot_for("oai") is None
