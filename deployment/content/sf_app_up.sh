#!/bin/bash
# PC3: brings the Smart Farming app content up with one command, after the UE radio (startmodem.sh) and the N3IWUE:
#
#   ~/src/smartfarm-content/sf_app_up.sh                  # weather model pinned to 13:30
#   ~/src/smartfarm-content/sf_app_up.sh --weather-hour 9 # any `sf_content.py node` option is passed on
#
#   1. node agent with the content generator (tmux sf-agent) + window "Smart Farming node agent (5G, content)"
#   2. content node inside the N3IWUE VRF (tmux sf-content) + window "SF content node"
#   3. the control panel http://127.0.0.1:8090/ in Firefox (states of each service, irrigation, previews)
# Then use the phone app; PC1 must be running deployment/scripts/sf_oran_up.sh (demo orchestrator).
# sf_app_down.sh puts the node agent back on the measurement generator (sf_traffic.py).
# Code and media come from PC1: deployment/content/deploy_pc3.sh (only when they change, never during a measurement).
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VRF=vrf-n3iwue
export DISPLAY=${DISPLAY:-:0} XAUTHORITY=${XAUTHORITY:-/home/inatel-crr/.Xauthority}
OPTS=("$@")
[ ${#OPTS[@]} -eq 0 ] && OPTS=(--weather-hour 13.5)

say() { echo "$(date +%T) $*"; }
win() { setsid nohup x-terminal-emulator -T "$1" -e sudo tmux attach-session -t "$2" >/dev/null 2>&1 < /dev/null & }

say "Smart Farming app content (PC3)"
sudo -v || exit 1
ip link show "$VRF" >/dev/null 2>&1 || { say "VRF $VRF not found: start the N3IWUE first"; exit 1; }
[ -f "$DIR/media/drone/manifest.json" ] || { say "media missing in $DIR (deployment/pc3/README.md, step 5)"; exit 1; }

say "[1/3] node agent with the content generator"
if [ -f "$DIR/smartfarm_node_agent.py" ] && ! cmp -s "$DIR/smartfarm_node_agent.py" /opt/smartfarm/smartfarm_node_agent.py; then
    sudo install -m 755 "$DIR/smartfarm_node_agent.py" /opt/smartfarm/ && say "   node agent updated from $DIR"
fi
sudo tmux kill-session -t sf-agent 2>/dev/null
sudo tmux new-session -d -s sf-agent \
    "python3 /opt/smartfarm/smartfarm_node_agent.py --key /etc/smartfarm/node-agent.key --traffic-script $DIR/sf_content.py"
win "Smart Farming node agent (5G, content)" sf-agent
sleep 2
sudo tmux capture-pane -p -t sf-agent | grep -v '^$' | tail -1

say "[2/3] content node"
for p in $(pgrep -f '[s]f_content.py node'); do sudo kill -INT "$p"; done
sudo tmux kill-session -t sf-content 2>/dev/null
sudo tmux new-session -d -s sf-content "$DIR/run_node.sh ${OPTS[*]}"
win "SF content node" sf-content
for _ in $(seq 1 30); do curl -s -m 1 -o /dev/null http://127.0.0.1:8090/ && break; sleep 1; done
curl -s -m 2 -o /dev/null http://127.0.0.1:8090/ || { say "panel not answering: see the SF content node window"; exit 1; }

say "[3/3] control panel in Firefox"
setsid nohup xdg-open http://127.0.0.1:8090/ >/dev/null 2>&1 < /dev/null &
say "DONE: open the app on the phone and tap a service; states can be changed in the panel"
