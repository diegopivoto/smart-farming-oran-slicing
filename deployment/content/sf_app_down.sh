#!/bin/bash
# PC3: stops the app content node and puts the node agent back on the measurement generator (sf_traffic.py), the
# configuration calibrations and RL sessions expect.
set -u
export DISPLAY=${DISPLAY:-:0} XAUTHORITY=${XAUTHORITY:-/home/inatel-crr/.Xauthority}
sudo -v || exit 1
for p in $(pgrep -f '[s]f_content.py node'); do sudo kill -INT "$p"; done
sleep 2
sudo tmux kill-session -t sf-content 2>/dev/null
sudo tmux kill-session -t sf-agent 2>/dev/null
sudo tmux new-session -d -s sf-agent 'python3 /opt/smartfarm/smartfarm_node_agent.py --key /etc/smartfarm/node-agent.key'
setsid nohup x-terminal-emulator -T "Smart Farming node agent (5G)" -e sudo tmux attach-session -t sf-agent \
    >/dev/null 2>&1 < /dev/null &
sleep 2
sudo tmux capture-pane -p -t sf-agent | grep -v '^$' | tail -1
echo "$(date +%T) app content stopped; node agent back on sf_traffic.py (measurements)"
