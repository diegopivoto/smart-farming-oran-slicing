from ipaddress import IPv4Address, IPv4Network

import pytest

from smartfarm.baseline.endpoints import (
    EndpointConfigurationError,
    EndpointRole,
    EndpointSource,
    resolve_manual_endpoints,
)


PDU_NETWORK = IPv4Network("10.45.0.0/16")
GATEWAY = IPv4Address("10.45.0.1")


def test_manual_resolution_assigns_stable_roles_without_identity_secrets() -> None:
    selection = resolve_manual_endpoints(
        un3gpp_ip=IPv4Address("10.45.0.2"),
        oneplus_ip=IPv4Address("10.45.0.4"),
        pdu_network=PDU_NETWORK,
        gateway=GATEWAY,
    )

    assert selection.source is EndpointSource.MANUAL_CLI
    assert selection.by_role(EndpointRole.UN3GPP_UE).ipv4 == IPv4Address("10.45.0.2")
    assert selection.by_role(EndpointRole.ONEPLUS_8T).ipv4 == IPv4Address("10.45.0.4")
    assert selection.by_role(EndpointRole.UN3GPP_UE).device_ref == "un3gpp-ue-lab"
    assert selection.by_role(EndpointRole.ONEPLUS_8T).device_ref == "oneplus-8t-lab"
    assert "supi" not in selection.model_dump_json().lower()
    assert "imsi" not in selection.model_dump_json().lower()


@pytest.mark.parametrize(
    ("un3gpp_ip", "oneplus_ip"),
    [
        ("10.45.0.2", "10.45.0.2"),
        ("10.45.0.1", "10.45.0.4"),
        ("10.45.0.2", "10.45.0.1"),
        ("10.45.0.0", "10.45.0.4"),
        ("10.45.0.2", "10.45.255.255"),
        ("192.0.2.10", "10.45.0.4"),
        ("10.45.0.2", "192.0.2.10"),
    ],
)
def test_manual_resolution_rejects_invalid_role_addresses(
    un3gpp_ip: str, oneplus_ip: str
) -> None:
    with pytest.raises(EndpointConfigurationError):
        resolve_manual_endpoints(
            un3gpp_ip=IPv4Address(un3gpp_ip),
            oneplus_ip=IPv4Address(oneplus_ip),
            pdu_network=PDU_NETWORK,
            gateway=GATEWAY,
        )


def test_manual_resolution_rejects_gateway_outside_network() -> None:
    with pytest.raises(EndpointConfigurationError):
        resolve_manual_endpoints(
            un3gpp_ip=IPv4Address("10.45.0.2"),
            oneplus_ip=IPv4Address("10.45.0.4"),
            pdu_network=PDU_NETWORK,
            gateway=IPv4Address("192.0.2.1"),
        )
