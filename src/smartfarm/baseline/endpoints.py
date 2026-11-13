"""Validated, identity-safe endpoint assignments for the Smart Farming lab."""

from enum import StrEnum
from ipaddress import IPv4Address, IPv4Network
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class EndpointConfigurationError(ValueError):
    """Raised when endpoint roles cannot be assigned unambiguously."""


class EndpointRole(StrEnum):
    """Stable logical roles that do not disclose subscriber identities."""

    UN3GPP_UE = "un3gpp_ue"
    ONEPLUS_8T = "oneplus_8t"


class EndpointSource(StrEnum):
    """How an endpoint selection was resolved."""

    MANUAL_CLI = "manual_cli"
    OPEN5GS_SMF_LOG = "open5gs_smf_log"


class FrozenModel(BaseModel):
    """Immutable endpoint model with a closed schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class EndpointAssignment(FrozenModel):
    """One pseudonymous role-to-address assignment."""

    role: EndpointRole
    device_ref: str
    ipv4: IPv4Address


class SessionResolution(FrozenModel):
    """Core-side evidence for one automatically resolved assignment."""

    role: EndpointRole
    dnn: str
    established_at: str
    log_path: str
    other_active_sessions: int = 0  # older sessions of the same UE still active in the SMF


class EndpointSelection(FrozenModel):
    """Complete, validated endpoint selection for one sentinel execution."""

    schema_version: Literal[1] = 1
    source: EndpointSource
    pdu_network: IPv4Network
    gateway: IPv4Address
    assignments: tuple[EndpointAssignment, EndpointAssignment]
    resolution: tuple[SessionResolution, SessionResolution] | None = None

    @model_validator(mode="after")
    def _require_each_role_once(self) -> "EndpointSelection":
        roles = {assignment.role for assignment in self.assignments}
        if roles != set(EndpointRole):
            raise ValueError("endpoint selection must assign each role exactly once")
        return self

    def by_role(self, role: EndpointRole) -> EndpointAssignment:
        """Return the unique assignment for a logical role."""

        return next(assignment for assignment in self.assignments if assignment.role is role)


def validate_ue_addresses(
    *,
    un3gpp_ip: IPv4Address,
    oneplus_ip: IPv4Address,
    pdu_network: IPv4Network,
    gateway: IPv4Address,
) -> None:
    """Reject UE addresses that cannot belong to two distinct live PDU sessions."""

    if gateway not in pdu_network:
        raise EndpointConfigurationError("gateway is outside the PDU network")
    if gateway in {pdu_network.network_address, pdu_network.broadcast_address}:
        raise EndpointConfigurationError("gateway is not a usable host address")
    if un3gpp_ip == oneplus_ip:
        raise EndpointConfigurationError("UE addresses must be distinct")

    reserved = {pdu_network.network_address, pdu_network.broadcast_address, gateway}
    for role, address in (
        (EndpointRole.UN3GPP_UE, un3gpp_ip),
        (EndpointRole.ONEPLUS_8T, oneplus_ip),
    ):
        if address not in pdu_network:
            raise EndpointConfigurationError(f"{role.value} is outside the PDU network")
        if address in reserved:
            raise EndpointConfigurationError(f"{role.value} is not a usable UE address")


def resolve_manual_endpoints(
    *,
    un3gpp_ip: IPv4Address,
    oneplus_ip: IPv4Address,
    pdu_network: IPv4Network,
    gateway: IPv4Address,
) -> EndpointSelection:
    """Validate explicit UE addresses and assign stable pseudonymous roles."""

    validate_ue_addresses(
        un3gpp_ip=un3gpp_ip,
        oneplus_ip=oneplus_ip,
        pdu_network=pdu_network,
        gateway=gateway,
    )
    return EndpointSelection(
        source=EndpointSource.MANUAL_CLI,
        pdu_network=pdu_network,
        gateway=gateway,
        assignments=(
            EndpointAssignment(
                role=EndpointRole.UN3GPP_UE,
                device_ref="un3gpp-ue-lab",
                ipv4=un3gpp_ip,
            ),
            EndpointAssignment(
                role=EndpointRole.ONEPLUS_8T,
                device_ref="oneplus-8t-lab",
                ipv4=oneplus_ip,
            ),
        ),
    )
