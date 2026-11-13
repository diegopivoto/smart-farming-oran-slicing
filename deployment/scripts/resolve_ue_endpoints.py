#!/usr/bin/env python3
"""Print the UE IPv4 addresses currently assigned by the Open5GS SMF.

Usage in shell scripts:
    eval "$(.venv/bin/python deployment/scripts/resolve_ue_endpoints.py --shell)"
    # -> UN3GPP_UE_IP=10.45.0.2 ONEPLUS_8T_IP=10.45.0.3
"""

import argparse
import json
import sys
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

from smartfarm.baseline.endpoints import EndpointConfigurationError, EndpointRole
from smartfarm.baseline.open5gs_sessions import (
    DEFAULT_IDENTITIES,
    DEFAULT_SMF_LOG,
    load_identities,
    read_smf_log,
    resolve_open5gs_endpoints,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--smf-log", type=Path, default=DEFAULT_SMF_LOG)
    parser.add_argument("--identities", type=Path, default=DEFAULT_IDENTITIES)
    parser.add_argument("--pdu-network", type=IPv4Network, default=IPv4Network("10.45.0.0/16"))
    parser.add_argument("--gateway-ip", type=IPv4Address, default=IPv4Address("10.45.0.1"))
    parser.add_argument("--shell", action="store_true", help="print KEY=value lines")
    arguments = parser.parse_args()
    try:
        selection = resolve_open5gs_endpoints(
            lines=read_smf_log(arguments.smf_log),
            identities=load_identities(arguments.identities),
            pdu_network=arguments.pdu_network,
            gateway=arguments.gateway_ip,
            log_path=arguments.smf_log,
        )
    except EndpointConfigurationError as error:
        print(f"resolve_ue_endpoints: {error}", file=sys.stderr)
        return 2
    if arguments.shell:
        for role in EndpointRole:
            print(f"{role.value.upper()}_IP={selection.by_role(role).ipv4}")
    else:
        print(json.dumps(selection.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
