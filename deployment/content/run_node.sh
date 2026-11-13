#!/usr/bin/env bash
# PC3: start the Smart Farming content node inside the N3IWUE VRF (as root, like `sf_traffic.py send`).
#
#   ~/src/smartfarm-content/run_node.sh [PHONE_IP] [extra `sf_content.py node` options]
#
# PHONE_IP is optional and only used by runs started from the panel: app/orchestrator requests always carry the
# phone's current address (UE addresses are dynamic, never hard-code one).
#
# The control panel is then at http://127.0.0.1:8090/ (open it as the desktop user: xdg-open / firefox).
# Ctrl-C stops the running senders (their generator summaries are posted to the collector) and exits.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_ARGS=()
if [[ "${1:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    TARGET_ARGS=(--target "$1")
    shift
fi
VRF="${VRF:-vrf-n3iwue}"
if ! ip link show "$VRF" >/dev/null 2>&1; then
    echo "VRF $VRF not found: is the N3IWUE registered with a PDU session?" >&2
    exit 1
fi
if [ ! -f "$DIR/media/drone/manifest.json" ]; then
    echo "media missing under $DIR/media (deployment/pc3/README.md, step 5)" >&2
    exit 1
fi
echo "Smart Farming content node (VRF $VRF; phone address from each app request); panel: http://127.0.0.1:8090/"
exec sudo ip vrf exec "$VRF" python3 -u "$DIR/sf_content.py" node "${TARGET_ARGS[@]}" "$@"
