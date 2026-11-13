import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pyarrow.parquet as pq
import pytest

from smartfarm.contracts.run import RunRecord, RunRequest, RunState
from smartfarm.contracts.service import ServiceId
from smartfarm.e2.kpm_collector import KpmCollector
from smartfarm.store.artifacts import ArtifactStore


FIXTURES = Path("tests/fixtures/kpm")
UE_ID = "amf:1:42"
EPOCH_1 = "11111111-1111-4111-8111-111111111111"
EPOCH_2 = "22222222-2222-4222-8222-222222222222"


def start_run(store: ArtifactStore) -> RunRecord:
    timestamp_ns = int(datetime(2026, 10, 31, tzinfo=UTC).timestamp() * 1_000_000_000)
    record = RunRecord(
        run_id=uuid4(),
        request=RunRequest(
            request_id=uuid4(),
            service_id=ServiceId.DRONE,
            device_ref="oneplus-8t-lab",
        ),
        state=RunState.OPTIMIZING,
        created_at_ns=timestamp_ns,
        updated_at_ns=timestamp_ns,
        version=7,
    )
    store.start_run(record, {"software": {"smart_farming_oran": "test"}})
    return record


def valid_document(
    *,
    sequence: int = 1,
    epoch: str = EPOCH_1,
    ue_id: str | None = UE_ID,
) -> dict[str, object]:
    document: dict[str, object] = json.loads((FIXTURES / "valid.jsonl").read_text(encoding="utf-8"))
    document["sequence"] = sequence
    document["connection_epoch"] = epoch
    document["ue_id"] = ue_id
    return document


def line(document: dict[str, object]) -> bytes:
    return json.dumps(document, separators=(",", ":")).encode()


def read_rows(store: ArtifactStore, run_id: UUID) -> list[dict[str, object]]:
    return cast(
        list[dict[str, object]],
        pq.read_table(store.paths_for(run_id).ran_kpm).to_pylist(),
    )


def test_batches_envelopes_and_flattens_measurements_per_mapped_run(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "experiments")
    run = start_run(store)
    collector = KpmCollector(
        store,
        capture_run_id=run.run_id,
        ue_runs={UE_ID: run.run_id},
        batch_size=2,
        clock_ns=lambda: 1_800_000_000_000_000_000,
    )

    first = collector.ingest(line(valid_document(sequence=1)))
    assert first.accepted is True
    assert first.sequence_status == "new_epoch"
    assert first.attribution_status == "attributed"
    assert not store.paths_for(run.run_id).ran_kpm.exists()

    second = collector.ingest(line(valid_document(sequence=2)))
    assert second.accepted is True
    assert second.flushed is True

    rows = read_rows(store, run.run_id)
    assert len(rows) == 14
    assert {row["sequence"] for row in rows} == {1, 2}
    assert {row["measurement_name"] for row in rows} >= {
        "DRB.UEThpDl",
        "RRU.PrbTotDl",
        "MR.NRScSSSINR",
    }
    throughput = next(
        row for row in rows if row["sequence"] == 1 and row["measurement_name"] == "DRB.UEThpDl"
    )
    assert throughput["real_value"] == 58_000.0
    assert throughput["integer_value"] is None
    assert throughput["measurement_unit"] == "kbit/s"
    assert throughput["attribution_status"] == "attributed"
    assert throughput["ingested_at_ns"] == 1_800_000_000_000_000_000


def test_missing_mapping_is_persisted_as_unattributed_not_last_ue(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "experiments")
    run = start_run(store)
    collector = KpmCollector(store, capture_run_id=run.run_id, batch_size=1)

    result = collector.ingest(line(valid_document()))

    assert result.accepted is True
    assert result.run_id == run.run_id
    assert result.attribution_status == "unattributed"
    rows = read_rows(store, run.run_id)
    assert {row["attribution_status"] for row in rows} == {"unattributed"}
    assert {row["ue_id"] for row in rows} == {UE_ID}
    assert collector.health().unattributed_envelopes == 1
    assert collector.health().attributed_envelopes == 0


def test_absent_ue_is_unattributed_and_no_value_stays_null(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "experiments")
    run = start_run(store)
    edge_lines = (FIXTURES / "edge-cases.jsonl").read_bytes().splitlines()
    collector = KpmCollector(
        store,
        capture_run_id=run.run_id,
        ue_runs={UE_ID: run.run_id},
        batch_size=1,
    )

    result = collector.ingest(edge_lines[1])

    assert result.attribution_status == "unattributed"
    row = read_rows(store, run.run_id)[0]
    assert row["ue_id"] is None
    assert row["measurement_kind"] == "no_value"
    assert row["integer_value"] is None
    assert row["real_value"] is None


def test_duplicate_out_of_order_and_stale_epoch_are_rejected(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "experiments")
    run = start_run(store)
    collector = KpmCollector(
        store,
        capture_run_id=run.run_id,
        ue_runs={UE_ID: run.run_id},
        batch_size=10,
    )

    assert collector.ingest(line(valid_document(sequence=10))).accepted is True
    duplicate = collector.ingest(line(valid_document(sequence=10)))
    out_of_order = collector.ingest(line(valid_document(sequence=9)))
    new_epoch = collector.ingest(line(valid_document(sequence=0, epoch=EPOCH_2)))
    stale_epoch = collector.ingest(line(valid_document(sequence=11, epoch=EPOCH_1)))

    assert (duplicate.accepted, duplicate.sequence_status) == (False, "duplicate")
    assert (out_of_order.accepted, out_of_order.sequence_status) == (
        False,
        "out_of_order",
    )
    assert (new_epoch.accepted, new_epoch.sequence_status) == (True, "new_epoch")
    assert (stale_epoch.accepted, stale_epoch.sequence_status) == (False, "stale_epoch")
    health = collector.health()
    assert health.accepted_envelopes == 2
    assert health.duplicate_envelopes == 1
    assert health.out_of_order_envelopes == 1
    assert health.stale_epoch_envelopes == 1


def test_gap_and_unknown_measurement_are_explicit_and_persisted(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "experiments")
    run = start_run(store)
    collector = KpmCollector(
        store,
        capture_run_id=run.run_id,
        ue_runs={UE_ID: run.run_id},
        batch_size=2,
    )
    unknown = json.loads((FIXTURES / "edge-cases.jsonl").read_text().splitlines()[0])
    unknown["connection_epoch"] = EPOCH_1
    unknown["sequence"] = 4

    collector.ingest(line(valid_document(sequence=1)))
    result = collector.ingest(line(unknown))

    assert result.sequence_status == "gap"
    assert result.missing_count == 2
    row = next(row for row in read_rows(store, run.run_id) if row["sequence"] == 4)
    assert row["measurement_name"] == "VENDOR.Magic"
    assert row["measurement_supported"] is False
    health = collector.health()
    assert health.gap_envelopes == 1
    assert health.missing_sequences == 2
    assert health.unsupported_measurements == 1


def test_close_flushes_last_partial_batch_without_loss(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "experiments")
    run = start_run(store)
    collector = KpmCollector(
        store,
        capture_run_id=run.run_id,
        ue_runs={UE_ID: run.run_id},
        batch_size=100,
    )

    collector.ingest(line(valid_document()))
    assert collector.health().pending_rows == 7
    collector.close()

    assert len(read_rows(store, run.run_id)) == 7
    health = collector.health()
    assert health.pending_rows == 0
    assert health.persisted_rows == 7
    assert health.flushes == 1


def test_invalid_json_updates_health_without_persistence(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "experiments")
    run = start_run(store)
    collector = KpmCollector(store, capture_run_id=run.run_id)

    result = collector.ingest(b"not-json")

    assert result.accepted is False
    assert result.sequence_status == "invalid"
    assert result.error is not None
    assert collector.health().invalid_envelopes == 1
    assert not store.paths_for(run.run_id).ran_kpm.exists()


def test_missing_only_first_batch_accepts_later_numeric_schema(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "experiments")
    run = start_run(store)
    edge_lines = (FIXTURES / "edge-cases.jsonl").read_bytes().splitlines()
    collector = KpmCollector(
        store,
        capture_run_id=run.run_id,
        ue_runs={UE_ID: run.run_id},
        batch_size=1,
    )

    collector.ingest(edge_lines[1])
    collector.ingest(line(valid_document(epoch=EPOCH_1)))

    rows = read_rows(store, run.run_id)
    assert len(rows) == 8
    assert rows[0]["measurement_kind"] == "no_value"
    assert any(row["integer_value"] == 4096 for row in rows)
    assert any(row["real_value"] == 58_000.0 for row in rows)


def test_failed_flush_retains_pending_rows_for_operator_recovery(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "experiments")
    run = start_run(store)
    collector = KpmCollector(
        store,
        capture_run_id=run.run_id,
        ue_runs={UE_ID: run.run_id},
        batch_size=100,
    )
    collector.ingest(line(valid_document()))
    store.finalize(run.run_id, "fault-injection")

    with pytest.raises(RuntimeError, match="finalized"):
        collector.flush()

    health = collector.health()
    assert health.pending_rows == 7
    assert health.persisted_rows == 0
    assert health.persistence_errors == 1
