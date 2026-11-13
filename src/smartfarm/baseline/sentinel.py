"""Read-only endpoint and connectivity sentinel for the live lab."""

import json
from datetime import datetime
from enum import StrEnum
from ipaddress import IPv4Address
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from smartfarm.baseline.endpoints import EndpointRole, EndpointSelection
from smartfarm.baseline.probes import BaselineProbe, BaselineResult
from smartfarm.inventory.runner import CommandResult, CommandRunner
from smartfarm.inventory.snapshot import HostSpec


class FrozenModel(BaseModel):
    """Immutable sentinel model with a closed schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class AddressInventoryParseError(ValueError):
    """Raised when `ip -j` output is not a trustworthy IPv4 inventory."""


class AddressCheckStatus(StrEnum):
    """Disjoint outcomes of checking the requested UN3GPP address on PC3."""

    PRESENT = "present"
    ABSENT = "absent"
    TIMEOUT = "timeout"
    COMMAND_ERROR = "command_error"
    PARSE_ERROR = "parse_error"


class SentinelTargetRole(StrEnum):
    """Explicit meaning of every data-plane connectivity probe."""

    OPEN5GS_GATEWAY = "open5gs_gateway"
    ONEPLUS_8T = "oneplus_8t"


class SentinelTarget(FrozenModel):
    """One role-labelled connectivity target."""

    role: SentinelTargetRole
    ipv4: IPv4Address


class Pc3AddressCheck(FrozenModel):
    """Evidence that PC3 owns the operator-supplied UN3GPP PDU address."""

    requested: IPv4Address
    status: AddressCheckStatus
    observed: tuple[IPv4Address, ...] = ()
    command: CommandResult
    detail: str | None = None

    @property
    def passed(self) -> bool:
        """Return whether the requested address was present."""

        return self.status is AddressCheckStatus.PRESENT


class EndpointSentinelEvidence(FrozenModel):
    """Complete evidence for one endpoint-validation attempt."""

    schema_version: Literal[1] = 1
    selection: EndpointSelection
    pc3_address: Pc3AddressCheck
    connectivity_targets: tuple[SentinelTarget, SentinelTarget]
    connectivity: BaselineResult | None = None
    passed: bool


class EndpointValidationSummary(FrozenModel):
    """Compact validation outcome embedded in the active context."""

    passed: bool
    pc3_address_status: AddressCheckStatus
    connectivity_passed: bool


class ActiveEndpointContext(FrozenModel):
    """Promoted endpoint context that downstream consumers may trust."""

    schema_version: Literal[1] = 1
    observed_at: datetime
    observed_monotonic_ns: int = Field(ge=0)
    selection: EndpointSelection
    validation: EndpointValidationSummary
    project_commit: str
    effective_arguments: tuple[str, ...]
    evidence_path: str


def parse_host_ipv4_addresses(text: str) -> frozenset[IPv4Address]:
    """Parse all IPv4 `local` fields from `ip -j -4 address show`."""

    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise AddressInventoryParseError("PC3 address inventory is not valid JSON") from error
    if not isinstance(document, list):
        raise AddressInventoryParseError("PC3 address inventory root must be a list")

    addresses: set[IPv4Address] = set()
    for interface in document:
        if not isinstance(interface, dict):
            raise AddressInventoryParseError("PC3 address inventory contains a non-object interface")
        entries = interface.get("addr_info", [])
        if not isinstance(entries, list):
            raise AddressInventoryParseError("PC3 address inventory has invalid addr_info")
        for entry in entries:
            if not isinstance(entry, dict):
                raise AddressInventoryParseError("PC3 address inventory contains a non-object address")
            if entry.get("family") != "inet":
                continue
            local = entry.get("local")
            if not isinstance(local, str):
                raise AddressInventoryParseError("PC3 IPv4 entry has no local address")
            try:
                addresses.add(IPv4Address(local))
            except ValueError as error:
                raise AddressInventoryParseError("PC3 IPv4 entry has an invalid address") from error
    return frozenset(addresses)


class EndpointSentinel:
    """Verify explicit endpoints without modifying any network state."""

    def __init__(self, runner: CommandRunner, *, timeout_s: float = 8.0) -> None:
        self._runner = runner
        self._timeout_s = timeout_s

    @staticmethod
    def address_inventory_command() -> tuple[str, ...]:
        """Return the reviewed, read-only PC3 inventory command."""

        return ("ip", "-j", "-4", "address", "show")

    @staticmethod
    def ping_command(address: IPv4Address | str) -> tuple[str, ...]:
        """Return the reviewed VRF ping command for an address."""

        return BaselineProbe.command_for(IPv4Address(address))

    @classmethod
    def planned_commands(cls, selection: EndpointSelection) -> tuple[tuple[str, ...], ...]:
        """Return the exact commands that execution would run."""

        oneplus = selection.by_role(EndpointRole.ONEPLUS_8T).ipv4
        return (
            cls.address_inventory_command(),
            cls.ping_command(selection.gateway),
            cls.ping_command(oneplus),
        )

    def run(self, host: HostSpec, selection: EndpointSelection) -> EndpointSentinelEvidence:
        """Validate PC3's address, then probe gateway and OnePlus from its VRF."""

        requested = selection.by_role(EndpointRole.UN3GPP_UE).ipv4
        command = self._runner.run(
            host.target,
            self.address_inventory_command(),
            self._timeout_s,
        )
        address_check = self._interpret_address_check(requested, command)
        oneplus = selection.by_role(EndpointRole.ONEPLUS_8T).ipv4
        targets = (
            SentinelTarget(role=SentinelTargetRole.OPEN5GS_GATEWAY, ipv4=selection.gateway),
            SentinelTarget(role=SentinelTargetRole.ONEPLUS_8T, ipv4=oneplus),
        )
        if not address_check.passed:
            return EndpointSentinelEvidence(
                selection=selection,
                pc3_address=address_check,
                connectivity_targets=targets,
                passed=False,
            )

        connectivity = BaselineProbe(self._runner, timeout_s=self._timeout_s).run(
            host,
            tuple(target.ipv4 for target in targets),
        )
        return EndpointSentinelEvidence(
            selection=selection,
            pc3_address=address_check,
            connectivity_targets=targets,
            connectivity=connectivity,
            passed=connectivity.passed,
        )

    @staticmethod
    def _interpret_address_check(
        requested: IPv4Address, command: CommandResult
    ) -> Pc3AddressCheck:
        if command.timed_out:
            return Pc3AddressCheck(
                requested=requested,
                status=AddressCheckStatus.TIMEOUT,
                command=command,
                detail=command.stderr or "command timed out",
            )
        if command.exit_code != 0:
            return Pc3AddressCheck(
                requested=requested,
                status=AddressCheckStatus.COMMAND_ERROR,
                command=command,
                detail=command.stderr or f"address inventory exited with code {command.exit_code}",
            )
        try:
            addresses = parse_host_ipv4_addresses(command.stdout)
        except AddressInventoryParseError as error:
            return Pc3AddressCheck(
                requested=requested,
                status=AddressCheckStatus.PARSE_ERROR,
                command=command,
                detail=str(error),
            )
        observed = tuple(sorted(addresses, key=int))
        if requested not in addresses:
            return Pc3AddressCheck(
                requested=requested,
                status=AddressCheckStatus.ABSENT,
                observed=observed,
                command=command,
                detail="requested UN3GPP address is not configured on PC3",
            )
        return Pc3AddressCheck(
            requested=requested,
            status=AddressCheckStatus.PRESENT,
            observed=observed,
            command=command,
        )
