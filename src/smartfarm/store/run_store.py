"""Transactional SQLite store for run state and idempotent events."""

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from smartfarm.contracts.run import RunEvent, RunRecord, RunRequest, RunState
from smartfarm.contracts.service import ServiceId


class RunStoreError(RuntimeError):
    """Base error for persisted run operations."""


class RunNotFound(RunStoreError):
    """Raised when a requested run identifier is absent."""


class InvalidTransition(RunStoreError):
    """Raised when an edge is not part of the approved state machine."""


class StateConflict(RunStoreError):
    """Raised when compare-and-set observes another persisted state."""


class DuplicateEvent(RunStoreError):
    """Raised when a transition tries to reuse an event identifier."""


_NEXT_STATE: dict[RunState, RunState] = {
    RunState.REQUESTED: RunState.PREPARING_SLICE,
    RunState.PREPARING_SLICE: RunState.WAITING_UN3GPP,
    RunState.WAITING_UN3GPP: RunState.WAITING_5G,
    RunState.WAITING_5G: RunState.SLICE_CONFIRMED,
    RunState.SLICE_CONFIRMED: RunState.TRAFFIC_READY,
    RunState.TRAFFIC_READY: RunState.WARMUP,
    RunState.WARMUP: RunState.OPTIMIZING,
    RunState.OPTIMIZING: RunState.SLA_STABLE,
    RunState.SLA_STABLE: RunState.STOPPING,
    RunState.STOPPING: RunState.COMPLETED,
}


class SqliteRunStore:
    """SQLite implementation with durable transitions and event deduplication."""

    def __init__(self, database: str | Path) -> None:
        self._database = Path(database)
        self._database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def create_run(self, request: RunRequest) -> RunRecord:
        """Create a REQUESTED run, returning the prior row on request retry."""

        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM runs WHERE request_id = ?", (str(request.request_id),)
            ).fetchone()
            if existing is not None:
                return self._row_to_record(existing)

            now_ns = time.time_ns()
            record = RunRecord(
                run_id=uuid4(),
                request=request,
                state=RunState.REQUESTED,
                created_at_ns=now_ns,
                updated_at_ns=now_ns,
                version=0,
            )
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, request_id, service_id, device_ref, state,
                    created_at_ns, updated_at_ns, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.run_id),
                    str(request.request_id),
                    request.service_id.value,
                    request.device_ref,
                    record.state.value,
                    record.created_at_ns,
                    record.updated_at_ns,
                    record.version,
                ),
            )
            return record

    def transition(
        self,
        run_id: UUID,
        expected: RunState,
        target: RunState,
        event: RunEvent,
    ) -> RunRecord:
        """Atomically append an event and compare-and-set the run state."""

        self._validate_transition(expected, target)
        if event.run_id != run_id:
            raise ValueError("transition event run_id does not match the target run")

        with self._transaction() as connection:
            row = self._select_run(connection, run_id)
            actual = RunState(row["state"])
            if actual is not expected:
                raise StateConflict(f"run {run_id} is {actual.value}, expected {expected.value}")
            if not self._insert_event(connection, event):
                raise DuplicateEvent(f"event {event.event_id} already exists for run {run_id}")

            updated_at_ns = max(time.time_ns(), int(row["updated_at_ns"]) + 1)
            result = connection.execute(
                """
                UPDATE runs
                SET state = ?, updated_at_ns = ?, version = version + 1
                WHERE run_id = ? AND state = ? AND version = ?
                """,
                (
                    target.value,
                    updated_at_ns,
                    str(run_id),
                    expected.value,
                    int(row["version"]),
                ),
            )
            if result.rowcount != 1:
                raise StateConflict(f"run {run_id} changed during compare-and-set")
            updated = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            if updated is None:  # pragma: no cover - guarded by the successful update
                raise RunNotFound(str(run_id))
            return self._row_to_record(updated)

    def append_event(self, event: RunEvent) -> bool:
        """Append an event once; return false for an idempotent retry."""

        with self._transaction() as connection:
            self._select_run(connection, event.run_id)
            return self._insert_event(connection, event)

    def get_run(self, run_id: UUID) -> RunRecord:
        """Read the current durable representation of a run."""

        connection = self._connect()
        try:
            return self._row_to_record(self._select_run(connection, run_id))
        finally:
            connection.close()

    @property
    def journal_mode(self) -> str:
        """Return the effective SQLite journal mode for diagnostics."""

        connection = self._connect()
        try:
            row = connection.execute("PRAGMA journal_mode").fetchone()
            return str(row[0]).lower()
        finally:
            connection.close()

    @property
    def foreign_keys_enabled(self) -> bool:
        """Report whether a newly configured store connection enforces FKs."""

        connection = self._connect()
        try:
            row = connection.execute("PRAGMA foreign_keys").fetchone()
            return bool(row[0])
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_schema(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL").fetchone()
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    service_id TEXT NOT NULL,
                    device_ref TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at_ns INTEGER NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    version INTEGER NOT NULL CHECK (version >= 0)
                );

                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK (sequence >= 0),
                    kind TEXT NOT NULL,
                    occurred_at_ns INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, event_id),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_run_events_order
                ON run_events(run_id, sequence, occurred_at_ns);
                """
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _validate_transition(expected: RunState, target: RunState) -> None:
        if target is RunState.FAILED_SAFE and expected is not RunState.FAILED_SAFE:
            return
        if _NEXT_STATE.get(expected) is not target:
            raise InvalidTransition(f"cannot transition {expected.value} to {target.value}")

    @staticmethod
    def _select_run(connection: sqlite3.Connection, run_id: UUID) -> sqlite3.Row:
        row: sqlite3.Row | None = connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (str(run_id),)
        ).fetchone()
        if row is None:
            raise RunNotFound(str(run_id))
        return row

    @staticmethod
    def _insert_event(connection: sqlite3.Connection, event: RunEvent) -> bool:
        result = connection.execute(
            """
            INSERT INTO run_events (
                run_id, event_id, sequence, kind, occurred_at_ns, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, event_id) DO NOTHING
            """,
            (
                str(event.run_id),
                str(event.event_id),
                event.sequence,
                event.kind,
                event.occurred_at_ns,
                json.dumps(event.payload, sort_keys=True, separators=(",", ":")),
            ),
        )
        return result.rowcount == 1

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> RunRecord:
        request = RunRequest(
            request_id=UUID(row["request_id"]),
            service_id=ServiceId(row["service_id"]),
            device_ref=str(row["device_ref"]),
        )
        return RunRecord(
            run_id=UUID(row["run_id"]),
            request=request,
            state=RunState(row["state"]),
            created_at_ns=int(row["created_at_ns"]),
            updated_at_ns=int(row["updated_at_ns"]),
            version=int(row["version"]),
        )
