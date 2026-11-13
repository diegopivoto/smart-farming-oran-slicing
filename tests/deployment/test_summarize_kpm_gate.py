import importlib.util
import json
from pathlib import Path
from types import ModuleType


FIXTURE = Path("tests/fixtures/kpm/valid.jsonl")
SCRIPT = Path("deployment/scripts/summarize_kpm_gate.py")


def load_summarizer() -> ModuleType:
    specification = importlib.util.spec_from_file_location("kpm_gate_summarizer", SCRIPT)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def envelope(sequence: int, timestamp_ns: int) -> bytes:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["sequence"] = sequence
    document["collected_at_ns"] = timestamp_ns
    return json.dumps(document).encode()


def test_summary_accepts_contiguous_one_second_complete_kpm() -> None:
    report = load_summarizer().summarize(
        [
            envelope(1, 1_800_000_000_000_000_000),
            envelope(2, 1_800_000_001_000_000_000),
            envelope(3, 1_800_000_002_000_000_000),
        ]
    )

    assert report["valid_envelopes"] == 3
    assert report["invalid_envelopes"] == 0
    assert report["missing_sequences"] == 0
    assert report["sequence_gap_ratio"] == 0.0
    assert report["connection_epochs"] == 1
    assert report["missing_desired_measurements"] == []
    assert report["period_summary"]["DRB.UEThpDl"]["median_s"] == 1.0
    assert report["criteria"]["kpm_criteria_pass"] is True


def test_summary_exposes_gap_invalid_and_missing_measurements() -> None:
    unknown = json.loads(Path("tests/fixtures/kpm/edge-cases.jsonl").read_text().splitlines()[0])
    unknown["connection_epoch"] = "11111111-1111-4111-8111-111111111111"
    unknown["sequence"] = 4

    report = load_summarizer().summarize(
        [
            envelope(1, 1_800_000_000_000_000_000),
            json.dumps(unknown).encode(),
            b"not-json",
        ]
    )

    assert report["invalid_envelopes"] == 1
    assert report["missing_sequences"] == 2
    assert report["unsupported_measurements"] == {"VENDOR.Magic": 1}
    assert "DRB.UEThpDl" not in report["missing_desired_measurements"]
    assert "RRU.PrbTotDl" not in report["missing_desired_measurements"]
    assert report["criteria"]["schema_valid"] is False
    assert report["criteria"]["gap_ratio_below_one_percent"] is False
    assert report["criteria"]["kpm_criteria_pass"] is False
