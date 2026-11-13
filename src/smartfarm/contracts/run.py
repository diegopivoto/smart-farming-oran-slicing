"""Contracts for one auditable end-to-end experiment run."""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from smartfarm.contracts.service import ServiceId


class FrozenModel(BaseModel):
    """Immutable base for persisted run value objects."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class RunState(StrEnum):
    """Persisted states exposed to the Android application and operators."""

    REQUESTED = "requested"
    PREPARING_SLICE = "preparing_slice"
    WAITING_UN3GPP = "waiting_un3gpp"
    WAITING_5G = "waiting_5g"
    SLICE_CONFIRMED = "slice_confirmed"
    TRAFFIC_READY = "traffic_ready"
    WARMUP = "warmup"
    OPTIMIZING = "optimizing"
    SLA_STABLE = "sla_stable"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED_SAFE = "failed_safe"


class RunRequest(FrozenModel):
    """Idempotent request to start a service experiment."""

    request_id: UUID
    service_id: ServiceId
    device_ref: str = Field(min_length=1, max_length=128)


class RunRecord(FrozenModel):
    """Current persisted state and compare-and-set version of a run."""

    run_id: UUID
    request: RunRequest
    state: RunState
    created_at_ns: int = Field(ge=0)
    updated_at_ns: int = Field(ge=0)
    version: int = Field(ge=0)


class RunEvent(FrozenModel):
    """Idempotent event associated with a run."""

    event_id: UUID
    run_id: UUID
    sequence: int = Field(ge=0)
    kind: str = Field(min_length=1, max_length=128)
    occurred_at_ns: int = Field(ge=0)
    payload: dict[str, object]
