"""Append-only scientific artifacts for reproducible experiment runs."""

import fcntl
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from smartfarm.contracts.run import RunEvent, RunRecord


class MetricStream(StrEnum):
    """Bounded set of time-series files in every run directory."""

    ENDPOINT = "endpoint_metrics"
    RAN_KPM = "ran_kpm"
    RAN_UN3GPP = "ran_un3gpp"
    ACTIONS = "actions"


@dataclass(frozen=True, slots=True)
class RunPaths:
    """Canonical paths owned by one experiment run."""

    root: Path
    manifest: Path
    events: Path
    endpoint_metrics: Path
    ran_kpm: Path
    ran_un3gpp: Path
    actions: Path
    core_evidence: Path
    configs: Path
    models: Path
    raw_logs: Path
    report: Path
    checksums: Path

    def metric_path(self, stream: MetricStream) -> Path:
        """Resolve a metric stream without accepting arbitrary filenames."""

        return {
            MetricStream.ENDPOINT: self.endpoint_metrics,
            MetricStream.RAN_KPM: self.ran_kpm,
            MetricStream.RAN_UN3GPP: self.ran_un3gpp,
            MetricStream.ACTIONS: self.actions,
        }[stream]


class ArtifactStore:
    """Persist immutable evidence separately from orchestration state."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._runs: dict[UUID, RunPaths] = {}

    def start_run(self, record: RunRecord, manifest: dict[str, object]) -> RunPaths:
        """Create a new dated artifact directory and its initial manifest."""

        date = datetime.fromtimestamp(record.created_at_ns / 1_000_000_000, tz=UTC).date()
        run_root = self._root / date.isoformat() / str(record.run_id)
        run_root.mkdir(parents=True, exist_ok=False)
        paths = self._build_paths(run_root)
        for directory in (paths.configs, paths.models, paths.raw_logs, paths.report):
            directory.mkdir()
        paths.events.touch(exist_ok=False)
        paths.core_evidence.touch(exist_ok=False)

        document: dict[str, object] = {
            **manifest,
            "schema_version": 1,
            "run_id": str(record.run_id),
            "request_id": str(record.request.request_id),
            "service_id": record.request.service_id.value,
            "device_ref": record.request.device_ref,
            "initial_state": record.state.value,
            "created_at_ns": record.created_at_ns,
        }
        self._atomic_write_text(
            paths.manifest,
            yaml.safe_dump(document, sort_keys=True, allow_unicode=True),
            overwrite=False,
        )
        self._runs[record.run_id] = paths
        return paths

    def append_event(self, run_id: UUID, event: RunEvent) -> None:
        """Append one canonical JSON event without rewriting prior lines."""

        if event.run_id != run_id:
            raise ValueError("event run_id does not match artifact run_id")
        paths = self.paths_for(run_id)
        serialized = json.dumps(
            event.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        with self._run_lock(paths):
            self._assert_open(paths)
            self._append_line(paths.events, serialized)

    def append_metrics(
        self,
        run_id: UUID,
        stream: MetricStream,
        rows: Sequence[Mapping[str, object]],
    ) -> None:
        """Validate and atomically append metric rows to a Parquet stream."""

        if not rows:
            return
        normalized = [self._validate_metric_row(run_id, row) for row in rows]
        paths = self.paths_for(run_id)
        destination = paths.metric_path(stream)
        with self._run_lock(paths):
            self._assert_open(paths)
            lock_path = destination.with_name(f".{destination.name}.lock")
            with lock_path.open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                new_table = pa.Table.from_pylist(normalized)
                if destination.exists():
                    current = pq.read_table(destination)
                    table = pa.concat_tables([current, new_table], promote_options="default")
                else:
                    table = new_table
                temporary = self._temporary_path(destination)
                try:
                    pq.write_table(table, temporary)
                    self._fsync_file(temporary)
                    os.replace(temporary, destination)
                    self._fsync_directory(destination.parent)
                finally:
                    temporary.unlink(missing_ok=True)
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def finalize(self, run_id: UUID, reason: str) -> Path:
        """Seal a run with an atomic SHA-256 inventory, once only."""

        if not reason:
            raise ValueError("finalization reason must not be empty")
        paths = self.paths_for(run_id)
        with self._run_lock(paths):
            if paths.checksums.exists():
                raise FileExistsError(paths.checksums)

            loaded = yaml.safe_load(paths.manifest.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("manifest must contain a mapping")
            loaded["finalized_at_ns"] = time.time_ns()
            loaded["finalization_reason"] = reason
            self._atomic_write_text(
                paths.manifest,
                yaml.safe_dump(loaded, sort_keys=True, allow_unicode=True),
                overwrite=True,
            )

            entries: list[str] = []
            for artifact in sorted(paths.root.rglob("*")):
                if not artifact.is_file() or self._is_internal_file(artifact):
                    continue
                relative = artifact.relative_to(paths.root).as_posix()
                entries.append(f"{self._sha256(artifact)}  {relative}")
            self._atomic_write_text(paths.checksums, "\n".join(entries) + "\n", overwrite=False)
        return paths.checksums

    def paths_for(self, run_id: UUID) -> RunPaths:
        """Return known paths, discovering a prior run after process restart."""

        known = self._runs.get(run_id)
        if known is not None:
            return known
        matches = list(self._root.glob(f"*/{run_id}"))
        if len(matches) != 1 or not matches[0].is_dir():
            raise KeyError(f"unknown artifact run: {run_id}")
        paths = self._build_paths(matches[0])
        self._runs[run_id] = paths
        return paths

    @staticmethod
    def _build_paths(root: Path) -> RunPaths:
        return RunPaths(
            root=root,
            manifest=root / "manifest.yaml",
            events=root / "events.jsonl",
            endpoint_metrics=root / "endpoint_metrics.parquet",
            ran_kpm=root / "ran_kpm.parquet",
            ran_un3gpp=root / "ran_un3gpp.parquet",
            actions=root / "actions.parquet",
            core_evidence=root / "core_evidence.jsonl",
            configs=root / "configs",
            models=root / "models",
            raw_logs=root / "raw-logs",
            report=root / "report",
            checksums=root / "checksums.sha256",
        )

    @staticmethod
    def _validate_metric_row(run_id: UUID, row: Mapping[str, object]) -> dict[str, object]:
        expected = str(run_id)
        if row.get("run_id") != expected:
            raise ValueError(f"metric run_id must equal {expected}")
        timestamp = row.get("timestamp_ns")
        if type(timestamp) is not int or timestamp < 0:
            raise ValueError("metric timestamp_ns must be a non-negative integer")
        source = row.get("source")
        if not isinstance(source, str) or not source:
            raise ValueError("metric source must be a non-empty string")
        return dict(row)

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        encoded = f"{line}\n".encode()
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:  # pragma: no cover - defensive OS failure guard
                    raise OSError("append write made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @staticmethod
    @contextmanager
    def _run_lock(paths: RunPaths) -> Iterator[None]:
        lock_path = paths.root / ".run.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _assert_open(paths: RunPaths) -> None:
        if paths.checksums.exists():
            raise RuntimeError(f"artifact run {paths.root.name} is finalized")

    @classmethod
    def _atomic_write_text(cls, path: Path, text: str, *, overwrite: bool) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            if overwrite:
                os.replace(temporary, path)
            else:
                os.link(temporary, path)
                temporary.unlink()
            cls._fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _temporary_path(destination: Path) -> Path:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.close(descriptor)
        return Path(name)

    @staticmethod
    def _fsync_file(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _is_internal_file(path: Path) -> bool:
        return path.name.endswith((".lock", ".tmp")) or path.name == "checksums.sha256"
