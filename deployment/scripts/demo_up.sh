#!/bin/bash
# Brings the Smart Farming app demo up (run on PC1, NEVER during an RL/calibration measurement: it copies over the
# UN3GPP link, swaps the PC3 node agent to the content generator and the orchestrator switches slices).
#   1. content generator code + media -> PC3 (rsync, bandwidth-capped)
#   2. PC3 node agent restarted with --traffic-script sf_content.py (tmux sf-agent, visible terminal)
#   3. PC3 content node (control TCP 47010, panel http://127.0.0.1:8090/) in a visible terminal + the panel opened
#   4. PC1 demo orchestrator (http://10.45.0.1:8090/api/v1) in a visible terminal
# deployment/scripts/demo_down.sh puts the measurement configuration back.
set -euo pipefail
cd "$(dirname "$0")/../.."
X='DISPLAY=:0 XAUTHORITY=/home/inatel-crr/.Xauthority setsid nohup'
CONTENT=/home/inatel-crr/src/smartfarm-content

echo "$(date +%T) [1/4] content generator -> PC3"
BWLIMIT=${BWLIMIT:-4000} deployment/content/deploy_pc3.sh

echo "$(date +%T) [2/4] PC3 node agent with the content generator"
scp -q -o BatchMode=yes deployment/pc3/smartfarm_node_agent.py smartfarm-pc3:/tmp/
ssh -o BatchMode=yes smartfarm-pc3 "sudo -n install -m 755 /tmp/smartfarm_node_agent.py /opt/smartfarm/ && \
  sudo -n tmux kill-session -t sf-agent 2>/dev/null; \
  sudo -n tmux new-session -d -s sf-agent 'python3 /opt/smartfarm/smartfarm_node_agent.py --key /etc/smartfarm/node-agent.key --traffic-script $CONTENT/sf_content.py'; \
  sleep 1; $X x-terminal-emulator -T 'Smart Farming node agent (5G, content)' -e sudo tmux attach-session -t sf-agent >/dev/null 2>&1 < /dev/null & \
  sleep 2; sudo -n tmux capture-pane -p -t sf-agent | grep -v '^\$' | tail -2"

echo "$(date +%T) [3/4] PC3 content node + control panel"
ssh -o BatchMode=yes smartfarm-pc3 "$X x-terminal-emulator -T 'SF content node' -e $CONTENT/run_node.sh --weather-hour 13.5 \
  >/dev/null 2>&1 < /dev/null & sleep 4; DISPLAY=:0 XAUTHORITY=/home/inatel-crr/.Xauthority setsid nohup xdg-open http://127.0.0.1:8090/ \
  >/dev/null 2>&1 < /dev/null & sleep 1; pgrep -af '[s]f_content.py node' | cut -c1-120"

echo "$(date +%T) [4/4] PC1 demo orchestrator"
systemd-run --user --collect --quiet gnome-terminal --title="SF demo orchestrator (10.45.0.1:8090)" -- bash -c \
  "cd $PWD && .venv/bin/python -u deployment/scripts/demo_orchestrator.py --tolerate-probe-failure; exec bash"
sleep 3
curl -s -m 3 http://10.45.0.1:8090/api/v1/health && echo
echo "$(date +%T) demo up: install android/dist/sf-farm-app-1.3.1.apk (Chrome, http://10.45.0.1:8089/) on the phone and tap a service"
