#!/bin/bash
# Puts the measurement configuration back after the app demo (run on PC1): stops the PC1 demo orchestrator and the
# PC3 content node, and restarts the PC3 node agent with the calibration traffic generator (sf_traffic.py).
set -euo pipefail
cd "$(dirname "$0")/../.."
X='DISPLAY=:0 XAUTHORITY=/home/inatel-crr/.Xauthority setsid nohup'

for pid in $(pgrep -f '[d]emo_orchestrator.py'); do kill -INT "$pid"; done
ssh -o BatchMode=yes smartfarm-pc3 "for p in \$(pgrep -f '[s]f_content.py node'); do sudo -n kill -INT \$p; done; \
  sudo -n tmux kill-session -t sf-agent 2>/dev/null; \
  sudo -n tmux new-session -d -s sf-agent 'python3 /opt/smartfarm/smartfarm_node_agent.py --key /etc/smartfarm/node-agent.key'; \
  sleep 1; $X x-terminal-emulator -T 'Smart Farming node agent (5G)' -e sudo tmux attach-session -t sf-agent >/dev/null 2>&1 < /dev/null & \
  sleep 2; sudo -n tmux capture-pane -p -t sf-agent | grep -v '^\$' | tail -2"
echo "$(date +%T) demo down: node agent back on sf_traffic.py (calibration/RL traffic)"
