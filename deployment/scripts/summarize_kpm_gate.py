#!/usr/bin/env python3
"""Summarize a captured KPM JSONL gate without modifying live services."""

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import cast


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from smartfarm.e2.kpm import (  # noqa: E402
    DESIRED_KPM_MEASUREMENTS,
    KpmSequenceTracker,
    decode_kpm_line,
)


def percentile_95(values: list[float]) -> float:
    """Return the nearest-rank p95 for a non-empty sample."""

    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def near_one_second(value: int | float | None) -> bool:
    """Return whether a computed period is within the gate tolerance."""

    return isinstance(value, (int, float)) and 0.5 <= value <= 1.5


def summarize(lines: Iterable[bytes]) -> dict[str, object]:
    """Validate, sequence-check, and summarize bridge JSONL records."""

    tracker = KpmSequenceTracker()
    invalid = 0
    valid = 0
    accepted = 0
    missing_sequences = 0
    statuses: Counter[str] = Counter()
    measurements: Counter[str] = Counter()
    unsupported: Counter[str] = Counter()
    epochs: set[str] = set()
    series_timestamps: dict[tuple[str, str | None, str], set[int]] = defaultdict(set)

    for line in lines:
        try:
            envelope = decode_kpm_line(line)
        except ValueError:
            invalid += 1
            continue
        valid += 1
        epochs.add(str(envelope.connection_epoch))
        decision = tracker.accept(envelope)
        statuses[decision.status] += 1
        missing_sequences += decision.missing_count
        if not decision.accepted:
            continue
        accepted += 1
        for measurement in envelope.measurements:
            measurements[measurement.name] += 1
            if not measurement.supported:
                unsupported[measurement.name] += 1
            series_timestamps[(envelope.e2_node_id, envelope.ue_id, measurement.name)].add(
                envelope.collected_at_ns
            )

    period_deltas: dict[str, list[float]] = defaultdict(list)
    period_samples: Counter[str] = Counter()
    period_series: Counter[str] = Counter()
    for (_, _, name), timestamps in series_timestamps.items():
        ordered = sorted(timestamps)
        period_samples[name] += len(ordered)
        period_series[name] += 1
        period_deltas[name].extend(
            (right - left) / 1_000_000_000
            for left, right in zip(ordered, ordered[1:], strict=False)
            if right > left
        )

    period_summary: dict[str, dict[str, int | float | None]] = {}
    for name in sorted(measurements):
        deltas = period_deltas.get(name, [])
        period_summary[name] = {
            "sample_count": period_samples[name],
            "series_count": period_series[name],
            "delta_count": len(deltas),
            "median_s": round(statistics.median(deltas), 6) if deltas else None,
            "p95_s": round(percentile_95(deltas), 6) if deltas else None,
        }

    missing_desired = sorted(set(DESIRED_KPM_MEASUREMENTS) - set(measurements))
    sequence_denominator = accepted + missing_sequences
    gap_ratio = missing_sequences / sequence_denominator if sequence_denominator > 0 else 1.0
    period_near_one_second = not missing_desired and all(
        near_one_second(period_summary[name]["median_s"]) for name in DESIRED_KPM_MEASUREMENTS
    )
    criteria = {
        "has_valid_envelopes": valid > 0,
        "schema_valid": invalid == 0,
        "single_connection_epoch": len(epochs) == 1,
        "sequence_order_clean": (
            statuses["duplicate"] == 0
            and statuses["out_of_order"] == 0
            and statuses["stale_epoch"] == 0
        ),
        "gap_ratio_below_one_percent": gap_ratio < 0.01,
        "all_desired_measurements_seen": not missing_desired,
        "median_period_near_one_second": period_near_one_second,
    }
    criteria["kpm_criteria_pass"] = all(criteria.values())

    return {
        "input_lines": valid + invalid,
        "valid_envelopes": valid,
        "invalid_envelopes": invalid,
        "accepted_envelopes": accepted,
        "rejected_envelopes": valid - accepted,
        "sequence_status_counts": dict(sorted(statuses.items())),
        "missing_sequences": missing_sequences,
        "sequence_gap_ratio": round(gap_ratio, 9),
        "connection_epochs": len(epochs),
        "measurement_counts": dict(sorted(measurements.items())),
        "unsupported_measurements": dict(sorted(unsupported.items())),
        "missing_desired_measurements": missing_desired,
        "period_summary": period_summary,
        "criteria": criteria,
    }


def sha256(path: Path) -> str:
    """Hash the source capture used to derive the report."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, document: dict[str, object]) -> None:
    """Write a report atomically and durably."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="captured KPM JSONL")
    parser.add_argument("--output", type=Path, required=True, help="gate report JSON")
    arguments = parser.parse_args()

    report = summarize(arguments.input.read_bytes().splitlines())
    report.update(
        {
            "schema_version": 1,
            "generated_at_ns": time.time_ns(),
            "input_path": str(arguments.input),
            "input_sha256": sha256(arguments.input),
        }
    )
    atomic_json(arguments.output, report)
    criteria = cast(dict[str, bool], report["criteria"])
    print(json.dumps(criteria, sort_keys=True))
    return 0 if criteria["kpm_criteria_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
