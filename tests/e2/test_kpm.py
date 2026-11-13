import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from smartfarm.e2.kpm import (
    DESIRED_KPM_MEASUREMENTS,
    KpmEnvelope,
    KpmSequenceTracker,
    decode_kpm_line,
)


FIXTURES = Path("tests/fixtures/kpm")


def fixture_lines(name: str) -> list[bytes]:
    return (FIXTURES / name).read_bytes().splitlines()


def envelope(
    *,
    epoch: str = "11111111-1111-4111-8111-111111111111",
    sequence: int = 1,
    collected_at_ns: int = 1_793_477_000_000_000_000,
) -> KpmEnvelope:
    document = json.loads(fixture_lines("valid.jsonl")[0])
    document["connection_epoch"] = epoch
    document["sequence"] = sequence
    document["collected_at_ns"] = collected_at_ns
    return decode_kpm_line(json.dumps(document).encode())


def test_decode_preserves_all_seven_desired_measurements() -> None:
    decoded = decode_kpm_line(fixture_lines("valid.jsonl")[0])

    assert decoded.connection_epoch == UUID("11111111-1111-4111-8111-111111111111")
    assert tuple(measurement.name for measurement in decoded.measurements) == tuple(
        DESIRED_KPM_MEASUREMENTS
    )
    assert all(measurement.supported for measurement in decoded.measurements)
    assert decoded.measurements[1].value == 3.25
    assert decoded.measurements[4].labels == {"bin": 17}


def test_unknown_measurement_is_preserved_as_unsupported() -> None:
    decoded = decode_kpm_line(fixture_lines("edge-cases.jsonl")[0])

    assert decoded.measurements[0].name == "VENDOR.Magic"
    assert decoded.measurements[0].value == 7
    assert decoded.measurements[0].supported is False


def test_no_value_and_absent_ue_are_not_converted_to_zero() -> None:
    decoded = decode_kpm_line(fixture_lines("edge-cases.jsonl")[1])

    assert decoded.ue_id is None
    assert decoded.measurements[0].kind == "no_value"
    assert decoded.measurements[0].value is None


@pytest.mark.parametrize(
    "mutation",
    (
        {"collected_at_ns": 0},
        {"collected_at_ns": -1},
        {"sequence": -1},
        {"ue_id": ""},
        {"measurements": []},
    ),
)
def test_invalid_envelope_fields_are_rejected(mutation: dict[str, object]) -> None:
    document = json.loads(fixture_lines("valid.jsonl")[0])
    document.update(mutation)

    with pytest.raises(ValidationError):
        decode_kpm_line(json.dumps(document).encode())


def test_measurement_kind_value_and_support_must_agree() -> None:
    document = json.loads(fixture_lines("valid.jsonl")[0])
    measurement = document["measurements"][0]

    for update in (
        {"kind": "no_value", "value": 0},
        {"kind": "integer", "value": 1.5},
        {"name": "UNKNOWN", "supported": True},
        {"name": "DRB.UEThpDl", "supported": False},
    ):
        invalid = document.copy()
        invalid["measurements"] = [{**measurement, **update}]
        with pytest.raises(ValidationError):
            decode_kpm_line(json.dumps(invalid).encode())


def test_decoder_rejects_blank_non_utf8_and_trailing_json() -> None:
    for line in (b"", b"\xff", b"{} {}"):
        with pytest.raises(ValueError):
            decode_kpm_line(line)


def test_freshness_is_explicit_and_future_samples_are_not_fresh() -> None:
    sample = envelope(collected_at_ns=1_000_000_000)

    assert sample.is_fresh(now_ns=1_500_000_000, max_age_ns=1_000_000_000) is True
    assert sample.is_fresh(now_ns=2_500_000_001, max_age_ns=1_000_000_000) is False
    assert sample.is_fresh(now_ns=999_999_999, max_age_ns=1_000_000_000) is False
    with pytest.raises(ValueError):
        sample.is_fresh(now_ns=2_000_000_000, max_age_ns=-1)


def test_sequence_tracker_accepts_contiguous_and_records_gaps() -> None:
    tracker = KpmSequenceTracker()

    first = tracker.accept(envelope(sequence=10))
    contiguous = tracker.accept(envelope(sequence=11))
    gap = tracker.accept(envelope(sequence=14))

    assert (first.accepted, first.status, first.missing_count) == (True, "new_epoch", 0)
    assert (contiguous.accepted, contiguous.status, contiguous.missing_count) == (
        True,
        "accepted",
        0,
    )
    assert (gap.accepted, gap.status, gap.missing_count, gap.previous_sequence) == (
        True,
        "gap",
        2,
        11,
    )


def test_sequence_tracker_rejects_duplicate_and_out_of_order() -> None:
    tracker = KpmSequenceTracker()
    tracker.accept(envelope(sequence=10))

    duplicate = tracker.accept(envelope(sequence=10))
    out_of_order = tracker.accept(envelope(sequence=9))

    assert (duplicate.accepted, duplicate.status) == (False, "duplicate")
    assert (out_of_order.accepted, out_of_order.status) == (False, "out_of_order")


def test_new_epoch_resets_sequence_and_old_epoch_becomes_stale() -> None:
    tracker = KpmSequenceTracker()
    old_epoch = "11111111-1111-4111-8111-111111111111"
    new_epoch = "22222222-2222-4222-8222-222222222222"
    tracker.accept(envelope(epoch=old_epoch, sequence=20))

    reset = tracker.accept(envelope(epoch=new_epoch, sequence=0))
    stale = tracker.accept(envelope(epoch=old_epoch, sequence=21))

    assert (reset.accepted, reset.status, reset.previous_sequence) == (
        True,
        "new_epoch",
        None,
    )
    assert (stale.accepted, stale.status) == (False, "stale_epoch")


def test_json_schema_documents_wire_constants_and_required_fields() -> None:
    schema = json.loads(Path("schemas/kpm-envelope.schema.json").read_text(encoding="utf-8"))

    assert schema["properties"]["schema_version"]["const"] == 1
    assert set(schema["required"]) == {
        "schema_version",
        "connection_epoch",
        "sequence",
        "collected_at_ns",
        "e2_node_id",
        "ue_id",
        "measurements",
    }
    assert schema["$defs"]["KpmMeasurement"]["properties"]["name"]["minLength"] == 1
