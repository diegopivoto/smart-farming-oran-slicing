import json
import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from smartfarm.contracts.run import RunEvent, RunRequest, RunState
from smartfarm.contracts.service import ServiceId
from smartfarm.store.run_store import InvalidTransition, SqliteRunStore, StateConflict


def make_request() -> RunRequest:
    return RunRequest(
        request_id=uuid4(),
        service_id=ServiceId.WEATHER,
        device_ref="oneplus-8t-lab",
    )


def make_event(run_id: UUID, *, kind: str = "state_changed", sequence: int = 1) -> RunEvent:
    return RunEvent(
        event_id=uuid4(),
        run_id=run_id,
        sequence=sequence,
        kind=kind,
        occurred_at_ns=123_456_789,
        payload={"source": "test"},
    )


def test_create_run_starts_requested_and_is_idempotent(tmp_path: Path) -> None:
    store = SqliteRunStore(tmp_path / "runs.sqlite3")
    request = make_request()

    created = store.create_run(request)
    retried = store.create_run(request)

    assert created.state is RunState.REQUESTED
    assert created.version == 0
    assert created.request == request
    assert retried == created


def test_valid_transition_and_event_persist_atomically(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite3"
    store = SqliteRunStore(database)
    created = store.create_run(make_request())
    event = make_event(created.run_id)

    transitioned = store.transition(
        created.run_id,
        expected=RunState.REQUESTED,
        target=RunState.PREPARING_SLICE,
        event=event,
    )

    assert transitioned.state is RunState.PREPARING_SLICE
    assert transitioned.version == 1
    assert transitioned.updated_at_ns >= transitioned.created_at_ns
    assert store.get_run(created.run_id) == transitioned

    with sqlite3.connect(database) as connection:
        persisted = connection.execute(
            "SELECT kind, payload_json FROM run_events WHERE run_id = ?",
            (str(created.run_id),),
        ).fetchone()
    assert persisted is not None
    assert persisted[0] == "state_changed"
    assert json.loads(persisted[1]) == {"source": "test"}


def test_transition_rejects_state_machine_jump(tmp_path: Path) -> None:
    store = SqliteRunStore(tmp_path / "runs.sqlite3")
    created = store.create_run(make_request())

    with pytest.raises(InvalidTransition):
        store.transition(
            created.run_id,
            expected=RunState.REQUESTED,
            target=RunState.OPTIMIZING,
            event=make_event(created.run_id),
        )

    assert store.get_run(created.run_id) == created


def test_duplicate_event_is_stored_once(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite3"
    store = SqliteRunStore(database)
    created = store.create_run(make_request())
    event = make_event(created.run_id, kind="health")

    assert store.append_event(event) is True
    assert store.append_event(event) is False

    with sqlite3.connect(database) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id = ? AND event_id = ?",
            (str(created.run_id), str(event.event_id)),
        ).fetchone()
    assert count == (1,)


def test_compare_and_set_conflict_does_not_change_run_or_add_event(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite3"
    store = SqliteRunStore(database)
    created = store.create_run(make_request())

    with pytest.raises(StateConflict):
        store.transition(
            created.run_id,
            expected=RunState.WAITING_5G,
            target=RunState.SLICE_CONFIRMED,
            event=make_event(created.run_id),
        )

    assert store.get_run(created.run_id) == created
    with sqlite3.connect(database) as connection:
        count = connection.execute("SELECT COUNT(*) FROM run_events").fetchone()
    assert count == (0,)


def test_store_enables_wal_and_foreign_keys(tmp_path: Path) -> None:
    store = SqliteRunStore(tmp_path / "runs.sqlite3")

    assert store.journal_mode == "wal"
    assert store.foreign_keys_enabled is True
