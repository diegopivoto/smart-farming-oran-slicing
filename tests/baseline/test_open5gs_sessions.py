import gzip
import json
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

import pytest

from smartfarm.baseline.endpoints import EndpointRole, EndpointSource
from smartfarm.baseline.open5gs_sessions import (
    SessionResolutionError,
    UeIdentity,
    active_sessions,
    load_identities,
    read_smf_log,
    resolve_open5gs_endpoints,
)

UN3GPP = "imsi-0010100007488"
ONEPLUS = "imsi-001010123456789"
IDENTITIES = {
    EndpointRole.UN3GPP_UE: UeIdentity(supi=UN3GPP, device_ref="un3gpp-ue-lab"),
    EndpointRole.ONEPLUS_8T: UeIdentity(supi=ONEPLUS, device_ref="oneplus-8t-lab"),
}
START = "Open5GS daemon v2.7.5"
STOP = "11/02 15:15:40.568: [app] INFO: Open5GS daemon terminating... (../src/main.c:231)"


def added(supi: str, ipv4: str, at: str = "11/02 15:30:27.765") -> str:
    return (
        f"{at}: [smf] INFO: UE SUPI[{supi}] DNN[internet] IPv4[{ipv4}] IPv6[] "
        "(../src/smf/npcf-handler.c:580)"
    )


def removed(supi: str, ipv4: str, psi: int = 1) -> str:
    return (
        f"11/02 15:40:00.000: [smf] INFO: Removed Session: UE IMSI:[{supi}] "
        f"DNN:[internet:{psi}] IPv4:[{ipv4}] IPv6:[] (../src/smf/context.c:1693)"
    )


def resolve(lines: list[str]):
    return resolve_open5gs_endpoints(
        lines=lines,
        identities=IDENTITIES,
        pdu_network=IPv4Network("10.45.0.0/16"),
        gateway=IPv4Address("10.45.0.1"),
        log_path=Path("/var/log/open5gs/smf.log"),
    )


def test_resolves_current_addresses_by_subscriber_not_allocation_order() -> None:
    selection = resolve([START, added(ONEPLUS, "10.45.0.2"), added(UN3GPP, "10.45.0.3")])

    assert selection.source is EndpointSource.OPEN5GS_SMF_LOG
    assert selection.by_role(EndpointRole.UN3GPP_UE).ipv4 == IPv4Address("10.45.0.3")
    assert selection.by_role(EndpointRole.ONEPLUS_8T).ipv4 == IPv4Address("10.45.0.2")
    assert selection.resolution is not None
    assert "imsi" not in selection.model_dump_json()


def test_removed_session_is_replaced_by_new_allocation() -> None:
    selection = resolve(
        [
            START,
            added(UN3GPP, "10.45.0.2"),
            added(ONEPLUS, "10.45.0.3"),
            removed(ONEPLUS, "10.45.0.3"),
            added(ONEPLUS, "10.45.0.4", at="11/02 15:41:00.000"),
        ]
    )

    oneplus = selection.by_role(EndpointRole.ONEPLUS_8T)
    assert oneplus.ipv4 == IPv4Address("10.45.0.4")
    assert selection.resolution is not None
    assert selection.resolution[1].established_at == "11/02 15:41:00.000"


def test_smf_restart_discards_sessions_from_previous_run() -> None:
    lines = [START, added(UN3GPP, "10.45.0.9"), added(ONEPLUS, "10.45.0.8"), STOP, START]

    assert active_sessions(lines) == {}
    with pytest.raises(SessionResolutionError, match="no active"):
        resolve(lines)


def test_latest_of_simultaneous_sessions_wins_and_older_are_counted() -> None:
    lines = [START, added(UN3GPP, "10.45.0.5"), added(UN3GPP, "10.45.0.2"), added(ONEPLUS, "10.45.0.3")]

    selection = resolve(lines)

    assert selection.by_role(EndpointRole.UN3GPP_UE).ipv4 == IPv4Address("10.45.0.2")
    assert selection.resolution is not None
    assert selection.resolution[0].other_active_sessions == 1
    assert selection.resolution[1].other_active_sessions == 0


def test_slice_session_with_other_dnn_is_resolved() -> None:
    crop = added(ONEPLUS, "10.45.0.89", at="11/02 20:43:53.880").replace("DNN[internet]", "DNN[crop]")
    selection = resolve([START, added(UN3GPP, "10.45.0.88"), added(ONEPLUS, "10.45.0.2"), crop])

    assert selection.by_role(EndpointRole.ONEPLUS_8T).ipv4 == IPv4Address("10.45.0.89")
    assert selection.resolution is not None
    assert selection.resolution[1].dnn == "crop"


def test_rotated_log_is_prepended_when_current_file_lacks_daemon_start(tmp_path: Path) -> None:
    current = tmp_path / "smf.log"
    current.write_text(added(ONEPLUS, "10.45.0.3") + "\n", encoding="utf-8")
    with gzip.open(tmp_path / "smf.log.1.gz", "wt", encoding="utf-8") as handle:
        handle.write(START + "\n" + added(UN3GPP, "10.45.0.2") + "\n")

    selection = resolve(read_smf_log(current))

    assert selection.by_role(EndpointRole.UN3GPP_UE).ipv4 == IPv4Address("10.45.0.2")


def test_identity_file_must_be_private_and_complete(tmp_path: Path) -> None:
    path = tmp_path / "ue-identities.json"
    document = {
        "un3gpp_ue": {"supi": UN3GPP, "device_ref": "un3gpp-ue-lab"},
        "oneplus_8t": {"supi": ONEPLUS, "device_ref": "oneplus-8t-lab"},
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(SessionResolutionError, match="private"):
        load_identities(path)

    path.chmod(0o600)
    assert load_identities(path) == IDENTITIES

    path.write_text(json.dumps({"un3gpp_ue": document["un3gpp_ue"]}), encoding="utf-8")
    with pytest.raises(SessionResolutionError, match="every endpoint role"):
        load_identities(path)
