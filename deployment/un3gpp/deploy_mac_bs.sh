#!/usr/bin/env bash
# Install (or roll back) the Smart Farming UL actuator MAC on the UN3GPP BS (PC2) and restart BS + N3IWF.
#   sudo setsid nohup bash /tmp/deploy_mac_bs.sh install  > /tmp/sfdeploy.log 2>&1 < /dev/null &
#   sudo setsid nohup bash /tmp/deploy_mac_bs.sh rollback > /tmp/sfdeploy.log 2>&1 < /dev/null &
# Run detached (setsid/nohup) so a dropped SSH session cannot cut the stop/start sequence in half, and
# NOT inside tmux: start_bs.sh then sends the PHY commands to the wrong session and the PHY never starts.
# The UE (PC3) is not touched; if its link does not come back, restart its radio and n3iwue locally.
# The BS and N3IWF consoles are opened on PC2's monitor at the end, as in the manual workflow.
set -euo pipefail
unset TMUX TMUX_PANE

ACTION=${1:?install|rollback}
MAC_BUILD=/home/inatel-crr/src/inatel5g_range_mac/build
NEW_BINARY=/home/inatel-crr/src/inatel5g_range_mac-sf/build/coreL2_sf.o
BACKUP="$MAC_BUILD/coreL2.o.pre-smartfarm-0e74c2e"
PHY=/home/inatel-crr/src/gr-inatelxg
N3IWF=/home/inatel-crr/src/Diego/n3iwf
UL_FREQ=680142857
DL_FREQ=560142857

stamp() {   # start_bs.sh refuses a coreL2.o newer than modo_compilado.txt; keep FDD as the first line
    echo "# $(date '+%F %T') coreL2.o: $1 (sha256 $(sha256sum "$MAC_BUILD/coreL2.o" | cut -c1-16))" >> "$MAC_BUILD/modo_compilado.txt"
}

case "$ACTION" in
  install)
    [ -f "$BACKUP" ] || cp -p "$MAC_BUILD/coreL2.o" "$BACKUP"
    install -m 0755 "$NEW_BINARY" "$MAC_BUILD/coreL2.o"
    stamp "smartfarm/ul-actuator build"
    ;;
  rollback)
    [ -f "$BACKUP" ] || { echo "no backup at $BACKUP"; exit 1; }
    install -m 0755 "$BACKUP" "$MAC_BUILD/coreL2.o"
    stamp "restored baseline 0e74c2e"
    ;;
  *) echo "usage: $0 install|rollback"; exit 2 ;;
esac
head -1 "$MAC_BUILD/modo_compilado.txt"
sha256sum "$MAC_BUILD/coreL2.o"

echo "[sfdeploy] stopping N3IWF and BS"
(cd "$N3IWF" && ./force_kill.sh) || true
(cd "$PHY" && ./scripts/stopmodem.sh) || true
sleep 3

echo "[sfdeploy] starting BS"
(cd "$PHY" && ./scripts/start_bs.sh "$UL_FREQ" "$DL_FREQ" < /dev/null) || true   # its final tmux attach needs a tty
for _ in $(seq 1 90); do
    pgrep -f bs_phy.py >/dev/null && ip -4 addr show tun0 2>/dev/null | grep -q "20.20.0.10" && break
    sleep 2
done
echo "[sfdeploy] phy=$(pgrep -f bs_phy.py | head -1) mac=$(pgrep -x coreL2.o) $(ip -br -4 addr show tun0)"

echo "[sfdeploy] starting N3IWF"
tmux kill-session -t n3iwf 2>/dev/null || true
tmux new-session -d -s n3iwf -c "$N3IWF" "./run.sh"
sleep 5
pgrep -a n3iwf | head -2

# show the consoles on PC2's monitor (sessions are root's: attach through sudo)
for session in modem n3iwf; do
    sudo -u inatel-crr env DISPLAY=:0 XAUTHORITY=/home/inatel-crr/.Xauthority \
        setsid nohup x-terminal-emulator -T "$session" -e sudo tmux attach-session -t "$session" \
        >/dev/null 2>&1 < /dev/null &
done
echo "[sfdeploy] done ($ACTION)"
