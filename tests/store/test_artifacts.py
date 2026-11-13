import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pyarrow.parquet as pq
import pytest
import yaml

from smartfarm.contracts.run import RunEvent, RunRecord, RunRequest, RunState
from smartfarm.contracts.service import ServiceId, get_service_profile
from smartfarm.store.artifacts import ArtifactStore, MetricStream


def make_record() -> RunRecord:
    timestamp_ns = int(datetime(2026, 10, 31, tzinfo=UTC).timestamp() * 1_000_000_000)
    request = RunRequest(
        request_id=uuid4(), service_id=ServiceId.DRONE, device_ref="oneplus-8t-lab"
    )
    return RunRecord(
        run_id=uuid4(),
        request=request,
        state=RunState.REQUESTED,
        created_at_ns=timestamp_ns,
        updated_at_ns=timestamp_ns,
        version=0,
    )


def start_store(tmp_path: Path) -> tuple[ArtifactStore, RunRecord]:
    store = ArtifactStore(tmp_path / "experiments")
    record = make_record()
    profile = get_service_profile(record.request.service_id)
    store.start_run(
        record,
        {
            "profile": profile.model_dump(mode="json"),
            "software": {"smart_farming_oran": "test-commit"},
        },
    )
    return store, record


def test_start_run_creates_dated_layout_and_atomic_manifest(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "experiments")
    record = make_record()

    paths = store.start_run(record, {"experiment_seed": 42})

    assert paths.root == tmp_path / "experiments" / "2026-10-31" / str(record.run_id)
    assert paths.manifest == paths.root / "manifest.yaml"
    assert paths.events == paths.root / "events.jsonl"
    assert paths.raw_logs.is_dir()
    assert paths.configs.is_dir()
    assert paths.models.is_dir()
    assert paths.report.is_dir()
    assert list(paths.root.glob("*.tmp")) == []

    manifest = yaml.safe_load(paths.manifest.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["run_id"] == str(record.run_id)
    assert manifest["request_id"] == str(record.request.request_id)
    assert manifest["service_id"] == "drone"
    assert manifest["experiment_seed"] == 42


def test_events_are_appended_as_json_lines(tmp_path: Path) -> None:
    store, record = start_store(tmp_path)
    first = RunEvent(
        event_id=uuid4(),
        run_id=record.run_id,
        sequence=1,
        kind="requested",
        occurred_at_ns=record.created_at_ns,
        payload={"state": "requested"},
    )
    second = first.model_copy(
        update={"event_id": uuid4(), "sequence": 2, "kind": "health", "payload": {"ok": True}}
    )

    store.append_event(record.run_id, first)
    store.append_event(record.run_id, second)

    event_path = store.paths_for(record.run_id).events
    lines = event_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["kind"] for line in lines] == ["requested", "health"]
    assert len(lines) == 2


def test_metrics_are_atomically_appended_to_stream_parquet(tmp_path: Path) -> None:
    store, record = start_store(tmp_path)
    common = {"run_id": str(record.run_id), "source": "oneplus"}

    store.append_metrics(
        record.run_id,
        MetricStream.ENDPOINT,
        [{**common, "timestamp_ns": 100, "goodput_bps": 50_000_000}],
    )
    store.append_metrics(
        record.run_id,
        MetricStream.ENDPOINT,
        [{**common, "timestamp_ns": 200, "goodput_bps": 55_000_000}],
    )

    table = pq.read_table(store.paths_for(record.run_id).endpoint_metrics)
    assert table.column_names == ["run_id", "source", "timestamp_ns", "goodput_bps"]
    assert table.to_pylist() == [
        {**common, "timestamp_ns": 100, "goodput_bps": 50_000_000},
        {**common, "timestamp_ns": 200, "goodput_bps": 55_000_000},
    ]
    assert list(store.paths_for(record.run_id).root.glob("*.tmp")) == []


def test_metrics_reject_mismatched_run_without_changing_stream(tmp_path: Path) -> None:
    store, record = start_store(tmp_path)
    metric_path = store.paths_for(record.run_id).ran_kpm

    with pytest.raises(ValueError, match="run_id"):
        store.append_metrics(
            record.run_id,
            MetricStream.RAN_KPM,
            [{"run_id": str(uuid4()), "timestamp_ns": 100, "source": "oai"}],
        )

    assert not metric_path.exists()


def test_finalize_hashes_artifacts_and_never_overwrites_raw_logs(tmp_path: Path) -> None:
    store, record = start_store(tmp_path)
    paths = store.paths_for(record.run_id)
    raw_log = paths.raw_logs / "oai.log"
    raw_contents = b"immutable radio evidence\n"
    raw_log.write_bytes(raw_contents)

    checksum_path = store.finalize(record.run_id, "operator_stop")

    assert raw_log.read_bytes() == raw_contents
    checksums = {
        name: digest
        for digest, name in (
            line.split("  ", maxsplit=1)
            for line in checksum_path.read_text(encoding="utf-8").splitlines()
        )
    }
    assert checksums["raw-logs/oai.log"] == hashlib.sha256(raw_contents).hexdigest()
    assert "manifest.yaml" in checksums
    manifest = yaml.safe_load(paths.manifest.read_text(encoding="utf-8"))
    assert manifest["finalization_reason"] == "operator_stop"
    assert isinstance(manifest["finalized_at_ns"], int)

    with pytest.raises(FileExistsError):
        store.finalize(record.run_id, "second_stop")

    late_event = RunEvent(
        event_id=uuid4(),
        run_id=record.run_id,
        sequence=99,
        kind="late_event",
        occurred_at_ns=record.created_at_ns + 1,
        payload={},
    )
    with pytest.raises(RuntimeError, match="finalized"):
        store.append_event(record.run_id, late_event)
    with pytest.raises(RuntimeError, match="finalized"):
        store.append_metrics(
            record.run_id,
            MetricStream.ENDPOINT,
            [{"run_id": str(record.run_id), "timestamp_ns": 999, "source": "late"}],
        )
