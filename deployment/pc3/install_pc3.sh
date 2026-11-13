#!/usr/bin/env bash
# PC3 (UN3GPP UE): install the Smart Farming UE side from a clone of this repository. Step by step: deployment/pc3/README.md.
#
#   deployment/pc3/install_pc3.sh                    # code: node agent + measurement generator + content generator
#   deployment/pc3/install_pc3.sh --render           # also render the demo media on this machine (radio stopped!)
#   deployment/pc3/install_pc3.sh --media DIR        # also copy already rendered media from DIR
#   deployment/pc3/install_pc3.sh --start-agent      # also (re)start the node agent in measurement mode (tmux sf-agent)
#
#   /opt/smartfarm/               smartfarm_node_agent.py, n3iwue_slice_client.py, sf_traffic.py (root, used by PC1)
#   ~/src/smartfarm-content/      content generator, panel, sf_app_up.sh / sf_app_down.sh, media/ (DEST=... to change)
#
# Run as the normal user (it asks for sudo). It only copies files: it does not touch the radio, IKE, the N3IWUE or a
# running content node, and it restarts the node agent only with --start-agent. Same result as the PC1-side
# deployment/content/deploy_pc3.sh, which stays available.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CONTENT="$REPO/deployment/content"
DEST="${DEST:-$HOME/src/smartfarm-content}"
OPT=/opt/smartfarm
KEY=/etc/smartfarm/node-agent.key
RENDER=0 MEDIA_FROM="" START_AGENT=0

usage() { sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
    case "$1" in
        --render) RENDER=1 ;;
        --media) MEDIA_FROM="${2:?--media needs a directory}"; shift ;;
        --start-agent) START_AGENT=1 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option: $1" >&2; usage 1 ;;
    esac
    shift
done
say() { echo "$(date +%T) $*"; }
warn() { echo "$(date +%T) WARNING: $*" >&2; }

say "[1/5] prerequisites"
missing=()
for c in python3 tmux rsync ip; do command -v "$c" >/dev/null || missing+=("$c"); done
python3 -c 'import numpy, PIL' 2>/dev/null || missing+=("python3-numpy python3-pil")
if [ ${#missing[@]} -gt 0 ]; then
    echo "missing: ${missing[*]} (sudo apt install tmux rsync python3-numpy python3-pil)" >&2
    exit 1
fi
sudo true   # asks for the password once (sudo -v would ask even with NOPASSWD)

say "[2/5] node agent and measurement generator -> $OPT"
sudo install -d -m 755 "$OPT"
sudo install -m 755 "$HERE/smartfarm_node_agent.py" "$HERE/n3iwue_slice_client.py" \
    "$REPO/deployment/traffic/sf_traffic.py" "$OPT/"

say "[3/5] content generator and panel -> $DEST"
mkdir -p "$DEST/sfcontent"
rsync -a --exclude __pycache__ \
    "$CONTENT/sf_content.py" "$CONTENT/sf_content_ref_parser.py" "$CONTENT/run_node.sh" "$CONTENT/sf_app_up.sh" \
    "$CONTENT/sf_app_down.sh" "$REPO/deployment/traffic/sf_traffic.py" "$HERE/smartfarm_node_agent.py" "$DEST/"
rsync -a --delete --exclude __pycache__ "$CONTENT/sfcontent/" "$DEST/sfcontent/"
chmod +x "$DEST/run_node.sh" "$DEST/sf_content.py" "$DEST/sf_app_up.sh" "$DEST/sf_app_down.sh"

say "[4/5] demo media -> $DEST/media"
if [ -n "$MEDIA_FROM" ]; then
    [ -f "$MEDIA_FROM/drone/manifest.json" ] || { echo "$MEDIA_FROM is not a rendered media folder" >&2; exit 1; }
    rsync -a --delete "$MEDIA_FROM/" "$DEST/media/"
elif [ "$RENDER" = 1 ]; then
    if pgrep -f '[u]e_phy.py|[b]s_phy.py|[c]oreL2.o' >/dev/null; then
        warn "the radio (PHY/MAC) is running: rendering competes with its real-time threads; it runs at nice 19"
    fi
    (cd "$DEST" && python3 sf_content.py render --media media --nice 19)
fi
if [ -f "$DEST/media/drone/manifest.json" ]; then
    (cd "$DEST" && python3 -c '
import sys; sys.path.insert(0, ".")
from pathlib import Path
from sfcontent import crop, drone
frames = drone.DroneFrames(Path("media"))
obj = crop.CropFactory(Path("media")).build("pest")
print("   content generator OK: drone loop", frames.count, "frames, crop object", len(obj.payload), "B")
')
else
    warn "no media in $DEST/media: measurement mode works, the app mode (sf_app_up.sh) needs it (--render or --media DIR)"
fi

say "[5/5] node agent key $KEY"
if sudo test -s "$KEY"; then
    sudo chmod 600 "$KEY"
    say "   present (must have the same content as ~/.config/smartfarm/node-agent.key on PC1)"
else
    warn "missing: copy the key of PC1 (~/.config/smartfarm/node-agent.key) to $KEY, mode 600 (README step 4)"
fi

if [ "$START_AGENT" = 1 ]; then
    sudo test -s "$KEY" || { echo "cannot start the node agent without $KEY" >&2; exit 1; }
    sudo tmux kill-session -t sf-agent 2>/dev/null || true
    sudo tmux new-session -d -s sf-agent "python3 $OPT/smartfarm_node_agent.py --key $KEY"
    sleep 2
    sudo tmux capture-pane -p -t sf-agent | grep -v '^$' | tail -1 || true
    say "node agent running in measurement mode (sudo tmux attach -t sf-agent)"
fi
say "DONE"
