"""Asynchronous client of the E2SM-SF/0.1 control xApp (``smartfarm_sf_control``).

An action only counts as applied when the gNB readback (a SLICE indication)
contains the outcome for the same ``action_id``. The E2 control acknowledgement
alone proves delivery, not effect, so it is recorded but never trusted as the
outcome.
"""

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from smartfarm.contracts.radio import (
    E2smSfCodecError,
    QuotaCounters,
    QuotaOutcome,
    QuotaRule,
    RadioAction,
    RadioOutcome,
    UeQuota,
    Un3gppOutcome,
    Un3gppUeView,
    parse_readback,
)

EventSink = Callable[[dict[str, Any]], None]


class E2ControlError(RuntimeError):
    """Base class for control failures that leave the radio state unknown or unchanged."""


class E2ControlTimeout(E2ControlError):
    """No matching outcome was read back before the deadline."""


class E2ControlRejected(E2ControlError):
    """The xApp refused the request or the RIC did not deliver it."""


class E2EpochChanged(E2ControlError):
    """The xApp restarted while an action was pending; the outcome is unknown."""


@dataclass(frozen=True, slots=True)
class ReadbackSnapshot:
    """Latest gNB view of the quota table and the UEs it governs."""

    epoch: str
    seq: int
    received_at_ns: int
    rules: tuple[QuotaRule, ...]
    ues: tuple[UeQuota, ...]
    counters: QuotaCounters | None
    outcomes: tuple[QuotaOutcome, ...]
    node: str = "oai"
    un3gpp_outcomes: tuple[Un3gppOutcome, ...] = ()
    un3gpp_ues: tuple[Un3gppUeView, ...] = ()


@dataclass
class _Pending:
    action_id: int
    sent_at_ns: int
    sent_after_seq: int
    epoch: str | None
    control: asyncio.Future[dict[str, Any]]
    outcome: asyncio.Future[RadioOutcome]


@dataclass
class _State:
    epoch: str | None = None
    last_seq: int = -1
    snapshot: ReadbackSnapshot | None = None
    snapshots: dict[str, ReadbackSnapshot] = field(default_factory=dict)
    malformed: int = 0
    pending: dict[int, _Pending] = field(default_factory=dict)
    controls: list[asyncio.Future[dict[str, Any]]] = field(default_factory=list)


class ActionIdAllocator:
    """Strictly increasing uint64 action IDs derived from the wall clock."""

    def __init__(self) -> None:
        self._last = 0

    def next(self) -> int:
        candidate = time.time_ns()
        self._last = candidate if candidate > self._last else self._last + 1
        return self._last


class E2ControlClient:
    """Sends E2SM-SF actions and correlates them with the gNB readback."""

    def __init__(self, socket_path: Path, *, sink: EventSink | None = None) -> None:
        self._socket_path = socket_path
        self._sink = sink
        self._state = _State()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._first_readback = asyncio.Event()

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_unix_connection(str(self._socket_path))
        self._task = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            await self._writer.wait_closed()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._fail_all(E2ControlError("client closed"))

    async def __aenter__(self) -> "E2ControlClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def snapshot(self) -> ReadbackSnapshot | None:
        return self._state.snapshot

    def snapshot_for(self, node: str) -> ReadbackSnapshot | None:
        """Latest readback of one node ("oai" or "un3gpp")."""

        return self._state.snapshots.get(node)

    async def wait_un3gpp_final(self, action_id: int, timeout_s: float = 15.0) -> Un3gppOutcome:
        """Wait until the UN3GPP proxy reports a final status (CONFIRMED, CONSISTENT, REJECTED_*...)."""

        deadline = time.monotonic() + timeout_s
        last: Un3gppOutcome | None = None
        while time.monotonic() < deadline:
            snap = self._state.snapshots.get("un3gpp")
            for outcome in snap.un3gpp_outcomes if snap else ():
                if outcome.action_id == action_id:
                    last = outcome
                    if outcome.final:
                        return outcome
            await asyncio.sleep(0.1)
        raise E2ControlTimeout(f"no final UN3GPP outcome for action {action_id} (last: {last})")

    @property
    def malformed_lines(self) -> int:
        return self._state.malformed

    async def wait_readback(self, timeout_s: float = 5.0) -> ReadbackSnapshot:
        """Wait for a readback newer than the current one."""

        start_seq = self._state.last_seq
        deadline = time.monotonic() + timeout_s
        while self._state.snapshot is None or self._state.last_seq <= start_seq:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise E2ControlTimeout("no SLICE readback received")
            self._first_readback.clear()
            try:
                await asyncio.wait_for(self._first_readback.wait(), remaining)
            except TimeoutError as error:
                raise E2ControlTimeout("no SLICE readback received") from error
        assert self._state.snapshot is not None
        return self._state.snapshot

    async def apply(self, action: RadioAction, timeout_s: float = 2.0) -> RadioOutcome:
        """Send one action and return the gNB outcome carrying the same action ID."""

        if self._writer is None:
            raise E2ControlError("client is not connected")
        if action.action_id in self._state.pending:
            raise E2ControlError(f"action {action.action_id} is already pending")
        loop = asyncio.get_running_loop()
        pending = _Pending(
            action_id=action.action_id,
            sent_at_ns=time.time_ns(),
            sent_after_seq=self._state.last_seq,
            epoch=self._state.epoch,
            control=loop.create_future(),
            outcome=loop.create_future(),
        )
        label = action.to_label()
        async with self._send_lock:
            self._state.pending[action.action_id] = pending
            self._state.controls.append(pending.control)
            self._record({"type": "action_sent", "action_id": action.action_id, "label": label, "at_ns": time.time_ns()})
            self._writer.write(f"CTRL {label}\n".encode())
            await self._writer.drain()
        try:
            control = await asyncio.wait_for(asyncio.shield(pending.control), timeout_s)
            if not control.get("accepted", False) or not control.get("e2_success", False):
                raise E2ControlRejected(str(control.get("error", "E2 control not delivered")))
            return await asyncio.wait_for(asyncio.shield(pending.outcome), timeout_s)
        except TimeoutError as error:
            raise E2ControlTimeout(f"no outcome for action {action.action_id} within {timeout_s}s") from error
        finally:
            self._state.pending.pop(action.action_id, None)
            if pending.control in self._state.controls:
                self._state.controls.remove(pending.control)

    def _record(self, event: dict[str, Any]) -> None:
        if self._sink is not None:
            self._sink(event)

    def _fail_all(self, error: E2ControlError) -> None:
        for pending in self._state.pending.values():
            for future in (pending.control, pending.outcome):
                if not future.done():
                    future.set_exception(error)
                    future.exception()  # mark retrieved when nobody awaits it anymore
        self._state.controls.clear()

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            raw = await self._reader.readline()
            if not raw:
                self._fail_all(E2ControlError("xApp closed the control socket"))
                return
            self.handle_line(raw)

    def handle_line(self, raw: bytes) -> None:
        """Process one JSON line from the xApp (public for deterministic tests)."""

        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            self._state.malformed += 1
            return
        if not isinstance(event, dict):
            self._state.malformed += 1
            return
        self._record(event)
        kind = event.get("type")
        if kind == "control":
            if self._state.controls:
                future = self._state.controls.pop(0)
                if not future.done():
                    future.set_result(event)
        elif kind == "indication":
            self._handle_indication(event)
        else:
            self._state.malformed += 1

    def _handle_indication(self, event: dict[str, Any]) -> None:
        epoch, seq = event.get("epoch"), event.get("seq")
        labels = event.get("labels")
        if not isinstance(epoch, str) or not isinstance(seq, int) or not isinstance(labels, list):
            self._state.malformed += 1
            return
        if self._state.epoch is not None and epoch != self._state.epoch:
            self._fail_all(E2EpochChanged(f"xApp epoch changed {self._state.epoch} -> {epoch}"))
            self._state.last_seq = -1
        elif seq <= self._state.last_seq:
            return  # duplicate or out-of-order readback
        self._state.epoch = epoch

        rules: list[QuotaRule] = []
        ues: list[UeQuota] = []
        outcomes: list[QuotaOutcome] = []
        un3gpp_outcomes: list[Un3gppOutcome] = []
        un3gpp_ues: list[Un3gppUeView] = []
        counters: QuotaCounters | None = None
        for label in labels:
            try:
                item = parse_readback(str(label))
            except E2smSfCodecError:
                self._state.malformed += 1
                continue
            if isinstance(item, QuotaRule):
                rules.append(item)
            elif isinstance(item, UeQuota):
                ues.append(item)
            elif isinstance(item, QuotaCounters):
                counters = item
            elif isinstance(item, Un3gppOutcome):
                un3gpp_outcomes.append(item)
            elif isinstance(item, Un3gppUeView):
                un3gpp_ues.append(item)
            else:
                outcomes.append(item)

        self._state.last_seq = seq
        self._state.snapshot = ReadbackSnapshot(
            epoch=epoch,
            seq=seq,
            received_at_ns=int(event.get("received_at_ns", 0)),
            rules=tuple(rules),
            ues=tuple(ues),
            counters=counters,
            outcomes=tuple(outcomes),
            node=str(event.get("node", "oai")),
            un3gpp_outcomes=tuple(un3gpp_outcomes),
            un3gpp_ues=tuple(un3gpp_ues),
        )
        self._state.snapshots[self._state.snapshot.node] = self._state.snapshot
        self._first_readback.set()
        matched: list[RadioOutcome] = [*outcomes, *un3gpp_outcomes]
        for outcome in matched:
            pending = self._state.pending.get(outcome.action_id)
            if pending is None or pending.outcome.done():
                continue
            if pending.epoch is not None and pending.epoch != epoch:
                continue
            if isinstance(outcome, Un3gppOutcome):
                # the proxy runs on PC2, whose clock is not synchronized with PC1: order by local indications
                if seq <= pending.sent_after_seq:
                    continue
            elif outcome.at_ns < pending.sent_at_ns:
                continue  # an older outcome for a replayed action ID
            pending.outcome.set_result(outcome)
