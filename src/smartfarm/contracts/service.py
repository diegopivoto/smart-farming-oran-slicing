"""Immutable service, SLA, and radio action contracts."""

from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar, Literal, Mapping, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrozenModel(BaseModel):
    """Base configuration for value objects published across components."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ServiceId(StrEnum):
    """Stable identifiers exposed by the service API."""

    WEATHER = "weather"
    CROP = "crop"
    DRONE = "drone"


class Snssai(FrozenModel):
    """3GPP S-NSSAI used by both access sessions for a service."""

    sst: int = Field(ge=0, le=255)
    sd: str = Field(pattern=r"^[0-9A-Fa-f]{6}$")


class SlaMetric(FrozenModel):
    """A measurable SLA predicate evaluated over a fixed window."""

    name: str = Field(min_length=1)
    operator: Literal["ge", "le", "eq"]
    threshold: float
    unit: str = Field(min_length=1)
    window_s: int = Field(gt=0)


class RadioBounds(FrozenModel):
    """Per-service action mask for safe experimental radio control."""

    DL_PRB_ACTIONS: ClassVar[frozenset[int]] = frozenset({24, 52, 78, 106})
    UL_RB_ACTIONS: ClassVar[frozenset[int]] = frozenset({33, 66, 99, 132})
    UL_MCS_MIN: ClassVar[int] = 0
    UL_MCS_MAX: ClassVar[int] = 17

    dl_prb: tuple[int, ...]
    ul_rb: tuple[int, ...]
    ul_mcs_min: int
    ul_mcs_max: int

    @field_validator("dl_prb")
    @classmethod
    def validate_dl_prb(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        return cls._validate_discrete_actions(values, cls.DL_PRB_ACTIONS, "dl_prb")

    @field_validator("ul_rb")
    @classmethod
    def validate_ul_rb(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        return cls._validate_discrete_actions(values, cls.UL_RB_ACTIONS, "ul_rb")

    @field_validator("ul_mcs_min", "ul_mcs_max")
    @classmethod
    def validate_mcs(cls, value: int) -> int:
        if not cls.UL_MCS_MIN <= value <= cls.UL_MCS_MAX:
            raise ValueError(f"UL MCS must be in [{cls.UL_MCS_MIN}, {cls.UL_MCS_MAX}]")
        return value

    @model_validator(mode="after")
    def validate_mcs_range(self) -> Self:
        if self.ul_mcs_min > self.ul_mcs_max:
            raise ValueError("ul_mcs_min must not exceed ul_mcs_max")
        return self

    @staticmethod
    def _validate_discrete_actions(
        values: tuple[int, ...], allowed: frozenset[int], field_name: str
    ) -> tuple[int, ...]:
        if not values:
            raise ValueError(f"{field_name} must contain at least one action")
        if tuple(sorted(set(values))) != values:
            raise ValueError(f"{field_name} actions must be unique and ascending")
        invalid = set(values) - allowed
        if invalid:
            raise ValueError(f"unsupported {field_name} actions: {sorted(invalid)}")
        return values


class RadioSetpoint(FrozenModel):
    """Absolute radio configuration requested by the controller."""

    dl_prb: int
    ul_rb: int
    ul_mcs: int

    @field_validator("dl_prb")
    @classmethod
    def validate_dl_prb(cls, value: int) -> int:
        if value not in RadioBounds.DL_PRB_ACTIONS:
            raise ValueError(f"unsupported dl_prb action: {value}")
        return value

    @field_validator("ul_rb")
    @classmethod
    def validate_ul_rb(cls, value: int) -> int:
        if value not in RadioBounds.UL_RB_ACTIONS:
            raise ValueError(f"unsupported ul_rb action: {value}")
        return value

    @field_validator("ul_mcs")
    @classmethod
    def validate_ul_mcs(cls, value: int) -> int:
        if not RadioBounds.UL_MCS_MIN <= value <= RadioBounds.UL_MCS_MAX:
            raise ValueError(
                f"UL MCS must be in [{RadioBounds.UL_MCS_MIN}, {RadioBounds.UL_MCS_MAX}]"
            )
        return value


class ServiceProfile(FrozenModel):
    """Versioned experimental assumptions for one Smart Farming service."""

    service_id: ServiceId
    snssai: Snssai
    dnn: Literal["internet"]  # UN3GPP UE: N3IWUE sends the S-NSSAI explicitly with this DNN
    # OnePlus APN: without URSP the DNN selects the slice (AMF lab patch, deployment/open5gs)
    handset_dnn: str = Field(pattern=r"^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$")
    offered_rate_bps: int = Field(gt=0)
    radio_bounds: RadioBounds
    initial_radio: RadioSetpoint
    sla: tuple[SlaMetric, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_initial_radio(self) -> Self:
        initial = self.initial_radio
        bounds = self.radio_bounds
        if initial.dl_prb not in bounds.dl_prb:
            raise ValueError("initial dl_prb is outside the service action mask")
        if initial.ul_rb not in bounds.ul_rb:
            raise ValueError("initial ul_rb is outside the service action mask")
        if not bounds.ul_mcs_min <= initial.ul_mcs <= bounds.ul_mcs_max:
            raise ValueError("initial ul_mcs is outside the service action mask")
        return self


SERVICE_CATALOG: Mapping[ServiceId, ServiceProfile] = MappingProxyType(
    {
        ServiceId.WEATHER: ServiceProfile(
            service_id=ServiceId.WEATHER,
            snssai=Snssai(sst=1, sd="000001"),
            dnn="internet",
            handset_dnn="weather",
            offered_rate_bps=100_000,
            radio_bounds=RadioBounds(dl_prb=(24, 52), ul_rb=(33, 66), ul_mcs_min=0, ul_mcs_max=10),
            initial_radio=RadioSetpoint(dl_prb=24, ul_rb=33, ul_mcs=3),
            sla=(
                SlaMetric(name="pdr", operator="ge", threshold=0.99, unit="ratio", window_s=10),
                SlaMetric(name="aoi_p95", operator="le", threshold=2.0, unit="s", window_s=10),
                SlaMetric(
                    name="max_freshness_gap",
                    operator="le",
                    threshold=5.0,
                    unit="s",
                    window_s=10,
                ),
            ),
        ),
        ServiceId.CROP: ServiceProfile(
            service_id=ServiceId.CROP,
            snssai=Snssai(sst=1, sd="000002"),
            dnn="internet",
            handset_dnn="crop",
            offered_rate_bps=20_000_000,
            radio_bounds=RadioBounds(
                dl_prb=(24, 52, 78), ul_rb=(33, 66, 99), ul_mcs_min=0, ul_mcs_max=15
            ),
            initial_radio=RadioSetpoint(dl_prb=52, ul_rb=66, ul_mcs=6),
            sla=(
                SlaMetric(
                    name="goodput", operator="ge", threshold=15_000_000, unit="bit/s", window_s=10
                ),
                SlaMetric(
                    name="object_transfer_time",
                    operator="le",
                    threshold=40.0,
                    unit="s",
                    window_s=40,
                ),
                SlaMetric(
                    name="sha256_valid_ratio",
                    operator="eq",
                    threshold=1.0,
                    unit="ratio",
                    window_s=40,
                ),
                SlaMetric(
                    name="retransmitted_bytes_ratio",
                    operator="le",
                    threshold=0.02,
                    unit="ratio",
                    window_s=40,
                ),
                SlaMetric(
                    name="accepted_corrupt_objects",
                    operator="eq",
                    threshold=0,
                    unit="count",
                    window_s=40,
                ),
            ),
        ),
        ServiceId.DRONE: ServiceProfile(
            service_id=ServiceId.DRONE,
            snssai=Snssai(sst=1, sd="000003"),
            dnn="internet",
            handset_dnn="drone",
            offered_rate_bps=40_000_000,  # 45 Mbit/s saturated the UN3GPP UL (calibration 2026-11-02/03): 40 keeps headroom
            radio_bounds=RadioBounds(
                dl_prb=(52, 78, 106), ul_rb=(66, 99, 132), ul_mcs_min=0, ul_mcs_max=14  # MCS >= 15: decoding loss, IKE DPD lost
            ),
            initial_radio=RadioSetpoint(dl_prb=78, ul_rb=99, ul_mcs=9),
            sla=(
                # met in 90% of the 1 s windows (p10), not in every second
                SlaMetric(
                    name="goodput_p10", operator="ge", threshold=34_000_000, unit="bit/s", window_s=5
                ),
                SlaMetric(
                    name="packet_loss_ratio",
                    operator="le",
                    threshold=0.05,
                    unit="ratio",
                    window_s=5,
                ),
                SlaMetric(name="jitter_p95", operator="le", threshold=0.02, unit="s", window_s=5),
                SlaMetric(
                    name="max_receive_gap",
                    operator="le",
                    threshold=0.5,
                    unit="s",
                    window_s=5,
                ),
            ),
        ),
    }
)


def get_service_profile(service_id: ServiceId | str) -> ServiceProfile:
    """Return the immutable profile for a public service identifier."""

    try:
        normalized = ServiceId(service_id)
    except ValueError as error:
        raise ValueError(f"unknown service_id: {service_id!r}") from error
    return SERVICE_CATALOG[normalized]
