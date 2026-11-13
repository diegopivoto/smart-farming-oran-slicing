"""Command-line orchestration for the explicit endpoint sentinel."""

import argparse
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

from smartfarm.baseline.endpoints import (
    EndpointConfigurationError,
    EndpointRole,
    EndpointSelection,
    EndpointSource,
    resolve_manual_endpoints,
)
from smartfarm.baseline.open5gs_sessions import (
    DEFAULT_IDENTITIES,
    DEFAULT_SMF_LOG,
    load_identities,
    read_smf_log,
    resolve_open5gs_endpoints,
)
from smartfarm.baseline.sentinel import (
    ActiveEndpointContext,
    EndpointSentinel,
    EndpointValidationSummary,
)
from smartfarm.inventory.runner import CommandRunner, LocalSshCommandRunner
from smartfarm.inventory.snapshot import HostSpec, load_inventory


class EndpointInputError(ValueError):
    """Safe, field-specific endpoint configuration failure."""

    def __init__(self, field: str, detail: str) -> None:
        super().__init__(detail)
        self.field = field
        self.detail = detail


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or execute the read-only PC3 endpoint connectivity sentinel."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="run the reviewed SSH commands")
    mode.add_argument("--dry-run", action="store_true", help="print commands only (default)")
    parser.add_argument(
        "--un3gpp-ip",
        help="manual override; omit both UE addresses to resolve them from the Open5GS SMF",
    )
    parser.add_argument("--oneplus-ip", help="manual override, used together with --un3gpp-ip")
    parser.add_argument("--smf-log", type=Path, default=DEFAULT_SMF_LOG)
    parser.add_argument("--identities", type=Path, default=DEFAULT_IDENTITIES)
    parser.add_argument(
        "--pdu-network",
        default="10.45.0.0/16",
    )
    parser.add_argument(
        "--gateway-ip",
        default="10.45.0.1",
    )
    parser.add_argument("--inventory", type=Path, default=Path("deployment/inventory/lab.yaml"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--endpoint-context", type=Path)
    return parser


def _pc3(inventory_path: Path) -> HostSpec:
    inventory = load_inventory(inventory_path)
    for host in inventory.hosts:
        if host.name == "pc3":
            return host
    raise ValueError("inventory does not define pc3")


def _atomic_write(path: Path, document: object) -> None:
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


def _project_commit() -> str:
    repository = Path(__file__).resolve().parents[3]
    completed = subprocess.run(  # noqa: S603 - constant reviewed argv
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def _effective_arguments(selection: EndpointSelection) -> tuple[str, ...]:
    if selection.source is EndpointSource.OPEN5GS_SMF_LOG:
        assert selection.resolution is not None
        return (
            "--smf-log",
            selection.resolution[0].log_path,
            "--pdu-network",
            str(selection.pdu_network),
            "--gateway-ip",
            str(selection.gateway),
        )
    return (
        "--un3gpp-ip",
        str(selection.by_role(EndpointRole.UN3GPP_UE).ipv4),
        "--oneplus-ip",
        str(selection.by_role(EndpointRole.ONEPLUS_8T).ipv4),
        "--pdu-network",
        str(selection.pdu_network),
        "--gateway-ip",
        str(selection.gateway),
    )


def _parse_network(arguments: argparse.Namespace) -> tuple[IPv4Network, IPv4Address]:
    try:
        pdu_network = IPv4Network(arguments.pdu_network)
    except ValueError as error:
        raise EndpointInputError(
            "pdu_network", "--pdu-network is not a valid IPv4 network"
        ) from error
    try:
        gateway = IPv4Address(arguments.gateway_ip)
    except ValueError as error:
        raise EndpointInputError("gateway_ip", "--gateway-ip is not a valid IPv4 address") from error
    return pdu_network, gateway


def _resolve_from_core(arguments: argparse.Namespace) -> EndpointSelection:
    pdu_network, gateway = _parse_network(arguments)
    try:
        return resolve_open5gs_endpoints(
            lines=read_smf_log(arguments.smf_log),
            identities=load_identities(arguments.identities),
            pdu_network=pdu_network,
            gateway=gateway,
            log_path=arguments.smf_log,
        )
    except (EndpointConfigurationError, OSError, ValueError) as error:
        raise EndpointInputError("open5gs_resolution", str(error)) from error


def _parse_selection(arguments: argparse.Namespace) -> EndpointSelection:
    if arguments.un3gpp_ip is None and arguments.oneplus_ip is None:
        return _resolve_from_core(arguments)
    try:
        un3gpp_ip = IPv4Address(arguments.un3gpp_ip)
    except ValueError as error:
        raise EndpointInputError("un3gpp_ip", "--un3gpp-ip is not a valid IPv4 address") from error
    try:
        oneplus_ip = IPv4Address(arguments.oneplus_ip)
    except ValueError as error:
        raise EndpointInputError("oneplus_ip", "--oneplus-ip is not a valid IPv4 address") from error
    pdu_network, gateway = _parse_network(arguments)
    try:
        return resolve_manual_endpoints(
            un3gpp_ip=un3gpp_ip,
            oneplus_ip=oneplus_ip,
            pdu_network=pdu_network,
            gateway=gateway,
        )
    except EndpointConfigurationError as error:
        raise EndpointInputError("endpoint_selection", str(error)) from error


def _sanitized_candidate_arguments(arguments: argparse.Namespace) -> dict[str, str]:
    converters = {
        "un3gpp_ip": IPv4Address,
        "oneplus_ip": IPv4Address,
        "pdu_network": IPv4Network,
        "gateway_ip": IPv4Address,
    }
    sanitized: dict[str, str] = {}
    for field, converter in converters.items():
        value = getattr(arguments, field)
        if value is None:
            sanitized[field] = "<resolved-by-core>"
            continue
        try:
            sanitized[field] = str(converter(value))
        except ValueError:
            sanitized[field] = "<invalid>"
    return sanitized


def _write_attempt(output: Path, document: object) -> Path:
    attempt_directory = output / str(time.time_ns())
    attempt_directory.mkdir(parents=True, exist_ok=False)
    evidence_path = attempt_directory / "baseline-probe.json"
    _atomic_write(evidence_path, document)
    return evidence_path


def _configuration_failure_document(
    arguments: argparse.Namespace, error: EndpointInputError
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "passed": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "observed_monotonic_ns": time.monotonic_ns(),
        "project_commit": _project_commit(),
        "candidate_arguments": _sanitized_candidate_arguments(arguments),
        "failure": {
            "stage": "endpoint_configuration",
            "field": error.field,
            "detail": error.detail,
        },
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: CommandRunner | None = None,
) -> int:
    """Plan or execute the sentinel and return a process exit code."""

    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.execute and arguments.output is None:
        parser.error("--output is required with --execute")
    if arguments.execute and arguments.endpoint_context is None:
        parser.error("--endpoint-context is required with --execute")
    if (arguments.un3gpp_ip is None) != (arguments.oneplus_ip is None):
        parser.error("manual override needs both --un3gpp-ip and --oneplus-ip (or neither)")
    try:
        selection = _parse_selection(arguments)
    except EndpointInputError as error:
        if not arguments.execute:
            parser.error(error.detail)
        assert arguments.output is not None
        document = _configuration_failure_document(arguments, error)
        print(json.dumps(document, indent=2, sort_keys=True))
        _write_attempt(arguments.output, document)
        return 2

    host = _pc3(arguments.inventory)
    if not arguments.execute:
        plan = {
            "execute": False,
            "host": host.target,
            "selection": selection.model_dump(mode="json"),
            "commands": [list(command) for command in EndpointSentinel.planned_commands(selection)],
        }
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0

    assert arguments.output is not None
    assert arguments.endpoint_context is not None
    effective_runner = runner if runner is not None else LocalSshCommandRunner()
    evidence = EndpointSentinel(effective_runner).run(host, selection)
    document = evidence.model_dump(mode="json")
    print(json.dumps(document, indent=2, sort_keys=True))

    evidence_path = _write_attempt(arguments.output, document)

    if evidence.passed:
        context = ActiveEndpointContext(
            observed_at=datetime.now(timezone.utc),
            observed_monotonic_ns=time.monotonic_ns(),
            selection=selection,
            validation=EndpointValidationSummary(
                passed=True,
                pc3_address_status=evidence.pc3_address.status,
                connectivity_passed=evidence.connectivity is not None
                and evidence.connectivity.passed,
            ),
            project_commit=_project_commit(),
            effective_arguments=_effective_arguments(selection),
            evidence_path=str(evidence_path.resolve()),
        )
        _atomic_write(arguments.endpoint_context, context.model_dump(mode="json"))
        return 0
    return 2
