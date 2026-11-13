"""Versioned, fail-closed KPM telemetry envelope and sequence tracking."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


DESIRED_KPM_MEASUREMENTS = (
    "DRB.PdcpSduVolumeDL",
    "DRB.RlcSduDelayDl",
    "DRB.UEThpDl",
    "RRU.PrbTotDl",
    "CARR.PDSCHMCSDist",
    "L1M.SS-RSRP",
    "MR.NRScSSSINR",
)
_DESIRED_KPM_SET = frozenset(DESIRED_KPM_MEASUREMENTS)

MeasurementKind = Literal["integer", "real", "no_value"]
LabelValue = str | int | float | bool
SequenceStatus = Literal[
    "accepted",
    "new_epoch",
    "gap",
    "duplicate",
    "out_of_order",
    "stale_epoch",
]


class FrozenModel(BaseModel):
    """Immutable wire value with unknown fields rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class KpmMeasurement(FrozenModel):
    """One named KPM record, including explicit absence and support state."""

    name: Annotated[str, Field(min_length=1)]
    kind: MeasurementKind
    value: int | float | None
    unit: Annotated[str, Field(min_length=1)] | None
    supported: bool
    labels: dict[str, LabelValue]

    @model_validator(mode="after")
    def validate_semantics(self) -> "KpmMeasurement":
        if self.kind == "no_value" and self.value is not None:
            raise ValueError("no_value measurement must carry null")
        if self.kind == "integer" and type(self.value) is not int:
            raise ValueError("integer measurement must carry an integer")
        if self.kind == "real" and (
            isinstance(self.value, bool) or not isinstance(self.value, (int, float))
        ):
            raise ValueError("real measurement must carry a number")
        expected_support = self.name in _DESIRED_KPM_SET
        if self.supported is not expected_support:
            raise ValueError(f"supported must be {expected_support} for measurement {self.name}")
        return self


class KpmEnvelope(FrozenModel):
    """One JSONL record emitted by a specific E2 connection epoch."""

    schema_version: Literal[1]
    connection_epoch: UUID
    sequence: Annotated[int, Field(ge=0)]
    collected_at_ns: Annotated[int, Field(gt=0)]
    e2_node_id: Annotated[str, Field(min_length=1)]
    ue_id: Annotated[str, Field(min_length=1)] | None
    measurements: Annotated[tuple[KpmMeasurement, ...], Field(min_length=1)]

    def is_fresh(self, *, now_ns: int, max_age_ns: int) -> bool:
        """Return freshness without treating future timestamps as valid samples."""

        if max_age_ns < 0:
            raise ValueError("max_age_ns must be non-negative")
        age_ns = now_ns - self.collected_at_ns
        return 0 <= age_ns <= max_age_ns


class SequenceDecision(FrozenModel):
    """Explicit acceptance and gap decision for one envelope."""

    accepted: bool
    status: SequenceStatus
    missing_count: Annotated[int, Field(ge=0)]
    previous_sequence: Annotated[int, Field(ge=0)] | None


def decode_kpm_line(line: bytes) -> KpmEnvelope:
    """Decode one bounded UTF-8 JSONL record with no coercion or trailing data."""

    if len(line) > 1024 * 1024:
        raise ValueError("KPM envelope exceeds 1 MiB")
    try:
        document = line.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ValueError("KPM envelope is not UTF-8") from error
    if not document:
        raise ValueError("KPM envelope is blank")
    return KpmEnvelope.model_validate_json(document)


class KpmSequenceTracker:
    """Track sequence continuity per E2 node while rejecting stale epochs."""

    def __init__(self) -> None:
        self._current_epoch: dict[str, UUID] = {}
        self._seen_epochs: dict[str, set[UUID]] = {}
        self._last_sequence: dict[tuple[str, UUID], int] = {}

    def accept(self, envelope: KpmEnvelope) -> SequenceDecision:
        """Classify and, when safe, advance continuity state."""

        node = envelope.e2_node_id
        epoch = envelope.connection_epoch
        current = self._current_epoch.get(node)
        seen = self._seen_epochs.setdefault(node, set())
        key = (node, epoch)

        if current is None or epoch not in seen:
            seen.add(epoch)
            self._current_epoch[node] = epoch
            self._last_sequence[key] = envelope.sequence
            return SequenceDecision(
                accepted=True,
                status="new_epoch",
                missing_count=0,
                previous_sequence=None,
            )
        if epoch != current:
            return SequenceDecision(
                accepted=False,
                status="stale_epoch",
                missing_count=0,
                previous_sequence=self._last_sequence.get(key),
            )

        previous = self._last_sequence[key]
        if envelope.sequence == previous:
            return SequenceDecision(
                accepted=False,
                status="duplicate",
                missing_count=0,
                previous_sequence=previous,
            )
        if envelope.sequence < previous:
            return SequenceDecision(
                accepted=False,
                status="out_of_order",
                missing_count=0,
                previous_sequence=previous,
            )

        missing = envelope.sequence - previous - 1
        self._last_sequence[key] = envelope.sequence
        return SequenceDecision(
            accepted=True,
            status="gap" if missing else "accepted",
            missing_count=missing,
            previous_sequence=previous,
        )
