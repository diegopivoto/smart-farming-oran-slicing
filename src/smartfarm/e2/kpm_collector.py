"""Correlate, validate, batch, and persist native FlexRIC KPM envelopes."""

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import RLock
from types import TracebackType
from typing import Literal
from uuid import UUID

from smartfarm.e2.kpm import KpmEnvelope, KpmSequenceTracker, decode_kpm_line
from smartfarm.store.artifacts import ArtifactStore, MetricStream


AttributionStatus = Literal["attributed", "unattributed"]
IngestStatus = Literal[
    "accepted",
    "new_epoch",
    "gap",
    "duplicate",
    "out_of_order",
    "stale_epoch",
    "invalid",
]
RunResolver = Mapping[str, UUID] | Callable[[str], UUID | None]


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Decision for one input envelope, including attribution and flush state."""

    accepted: bool
    sequence_status: IngestStatus
    missing_count: int
    run_id: UUID | None
    attribution_status: AttributionStatus | None
    buffered_rows: int
    flushed: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class KpmHealth:
    """Monotonic collector counters plus the current pending row count."""

    received_lines: int
    accepted_envelopes: int
    rejected_envelopes: int
    invalid_envelopes: int
    duplicate_envelopes: int
    out_of_order_envelopes: int
    stale_epoch_envelopes: int
    new_epoch_envelopes: int
    gap_envelopes: int
    missing_sequences: int
    attributed_envelopes: int
    unattributed_envelopes: int
    unsupported_measurements: int
    persisted_rows: int
    pending_rows: int
    flushes: int
    persistence_errors: int


class KpmCollector:
    """Validate bridge JSONL and persist flattened KPM rows by experiment run."""

    def __init__(
        self,
        artifacts: ArtifactStore,
        *,
        capture_run_id: UUID,
        ue_runs: RunResolver | None = None,
        batch_size: int = 10,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        artifacts.paths_for(capture_run_id)
        self._artifacts = artifacts
        self._capture_run_id = capture_run_id
        self._ue_runs = ue_runs
        self._batch_size = batch_size
        self._clock_ns = clock_ns
        self._tracker = KpmSequenceTracker()
        self._pending: dict[UUID, list[dict[str, object]]] = {}
        self._pending_envelopes: dict[UUID, int] = {}
        self._lock = RLock()
        self._closed = False

        self._received_lines = 0
        self._accepted_envelopes = 0
        self._rejected_envelopes = 0
        self._invalid_envelopes = 0
        self._duplicate_envelopes = 0
        self._out_of_order_envelopes = 0
        self._stale_epoch_envelopes = 0
        self._new_epoch_envelopes = 0
        self._gap_envelopes = 0
        self._missing_sequences = 0
        self._attributed_envelopes = 0
        self._unattributed_envelopes = 0
        self._unsupported_measurements = 0
        self._persisted_rows = 0
        self._flushes = 0
        self._persistence_errors = 0

    def ingest(self, line: bytes) -> IngestResult:
        """Validate one JSONL record and persist it once sequence-safe."""

        with self._lock:
            if self._closed:
                raise RuntimeError("KPM collector is closed")
            self._received_lines += 1
            try:
                envelope = decode_kpm_line(line)
            except ValueError as error:
                self._invalid_envelopes += 1
                self._rejected_envelopes += 1
                return IngestResult(
                    accepted=False,
                    sequence_status="invalid",
                    missing_count=0,
                    run_id=None,
                    attribution_status=None,
                    buffered_rows=0,
                    flushed=False,
                    error=str(error),
                )

            decision = self._tracker.accept(envelope)
            if not decision.accepted:
                self._rejected_envelopes += 1
                if decision.status == "duplicate":
                    self._duplicate_envelopes += 1
                elif decision.status == "out_of_order":
                    self._out_of_order_envelopes += 1
                elif decision.status == "stale_epoch":
                    self._stale_epoch_envelopes += 1
                return IngestResult(
                    accepted=False,
                    sequence_status=decision.status,
                    missing_count=decision.missing_count,
                    run_id=None,
                    attribution_status=None,
                    buffered_rows=0,
                    flushed=False,
                )

            if decision.status == "new_epoch":
                self._new_epoch_envelopes += 1
            elif decision.status == "gap":
                self._gap_envelopes += 1
                self._missing_sequences += decision.missing_count

            mapped_run = self._resolve_run(envelope.ue_id)
            if mapped_run is None:
                target_run = self._capture_run_id
                attribution: AttributionStatus = "unattributed"
                self._unattributed_envelopes += 1
            else:
                self._artifacts.paths_for(mapped_run)
                target_run = mapped_run
                attribution = "attributed"
                self._attributed_envelopes += 1

            ingested_at_ns = self._clock_ns()
            if ingested_at_ns < 0:
                raise ValueError("collector clock returned a negative timestamp")
            rows = self._rows_for(
                envelope,
                target_run=target_run,
                mapped_run=mapped_run,
                attribution=attribution,
                sequence_status=decision.status,
                missing_count=decision.missing_count,
                ingested_at_ns=ingested_at_ns,
            )
            self._unsupported_measurements += sum(
                not measurement.supported for measurement in envelope.measurements
            )
            self._accepted_envelopes += 1
            pending = self._pending.setdefault(target_run, [])
            pending.extend(rows)
            pending_count = self._pending_envelopes.get(target_run, 0) + 1
            self._pending_envelopes[target_run] = pending_count
            flushed = pending_count >= self._batch_size
            if flushed:
                self._flush_run(target_run)
            return IngestResult(
                accepted=True,
                sequence_status=decision.status,
                missing_count=decision.missing_count,
                run_id=target_run,
                attribution_status=attribution,
                buffered_rows=len(rows),
                flushed=flushed,
            )

    def flush(self) -> None:
        """Durably write every partial run batch, retaining data on failure."""

        with self._lock:
            for run_id in tuple(self._pending):
                self._flush_run(run_id)

    def close(self) -> None:
        """Flush the final partial batch and reject subsequent ingestion."""

        with self._lock:
            if self._closed:
                return
            self.flush()
            self._closed = True

    def health(self) -> KpmHealth:
        """Return an immutable point-in-time health snapshot."""

        with self._lock:
            return KpmHealth(
                received_lines=self._received_lines,
                accepted_envelopes=self._accepted_envelopes,
                rejected_envelopes=self._rejected_envelopes,
                invalid_envelopes=self._invalid_envelopes,
                duplicate_envelopes=self._duplicate_envelopes,
                out_of_order_envelopes=self._out_of_order_envelopes,
                stale_epoch_envelopes=self._stale_epoch_envelopes,
                new_epoch_envelopes=self._new_epoch_envelopes,
                gap_envelopes=self._gap_envelopes,
                missing_sequences=self._missing_sequences,
                attributed_envelopes=self._attributed_envelopes,
                unattributed_envelopes=self._unattributed_envelopes,
                unsupported_measurements=self._unsupported_measurements,
                persisted_rows=self._persisted_rows,
                pending_rows=sum(len(rows) for rows in self._pending.values()),
                flushes=self._flushes,
                persistence_errors=self._persistence_errors,
            )

    def __enter__(self) -> "KpmCollector":
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback
        self.close()

    def _resolve_run(self, ue_id: str | None) -> UUID | None:
        if ue_id is None or self._ue_runs is None:
            return None
        if callable(self._ue_runs):
            return self._ue_runs(ue_id)
        return self._ue_runs.get(ue_id)

    def _flush_run(self, run_id: UUID) -> None:
        rows = self._pending.get(run_id)
        if not rows:
            self._pending.pop(run_id, None)
            self._pending_envelopes.pop(run_id, None)
            return
        try:
            self._artifacts.append_metrics(run_id, MetricStream.RAN_KPM, rows)
        except BaseException:
            self._persistence_errors += 1
            raise
        self._persisted_rows += len(rows)
        self._flushes += 1
        del self._pending[run_id]
        self._pending_envelopes.pop(run_id, None)

    @staticmethod
    def _rows_for(
        envelope: KpmEnvelope,
        *,
        target_run: UUID,
        mapped_run: UUID | None,
        attribution: AttributionStatus,
        sequence_status: str,
        missing_count: int,
        ingested_at_ns: int,
    ) -> list[dict[str, object]]:
        common: dict[str, object] = {
            "run_id": str(target_run),
            "timestamp_ns": envelope.collected_at_ns,
            "source": "flexric-kpm-v2.03",
            "schema_version": envelope.schema_version,
            "connection_epoch": str(envelope.connection_epoch),
            "sequence": envelope.sequence,
            "sequence_status": sequence_status,
            "sequence_missing_count": missing_count,
            "e2_node_id": envelope.e2_node_id,
            "ue_id": envelope.ue_id,
            "attribution_status": attribution,
            "mapped_run_id": str(mapped_run) if mapped_run is not None else None,
            "ingested_at_ns": ingested_at_ns,
        }
        rows: list[dict[str, object]] = []
        for measurement in envelope.measurements:
            integer_value: int | None = None
            real_value: float | None = None
            if measurement.kind == "integer":
                if type(measurement.value) is not int:
                    raise ValueError("validated integer KPM value changed type")
                integer_value = measurement.value
            elif measurement.kind == "real":
                if isinstance(measurement.value, bool) or not isinstance(
                    measurement.value, (int, float)
                ):
                    raise ValueError("validated real KPM value changed type")
                real_value = float(measurement.value)
            rows.append(
                {
                    **common,
                    "measurement_name": measurement.name,
                    "measurement_kind": measurement.kind,
                    "integer_value": integer_value,
                    "real_value": real_value,
                    "measurement_unit": measurement.unit,
                    "measurement_supported": measurement.supported,
                    "labels_json": json.dumps(
                        measurement.labels,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            )
        return rows
