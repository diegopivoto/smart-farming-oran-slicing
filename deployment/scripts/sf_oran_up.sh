#!/bin/bash
# Brings up the whole O-RAN side and the app back end on PC1 with one command, in dependency order, waiting by
# itself for the RAN nodes. Run it on PC1 right after the core (open5gs-core-lab/restart_core.sh) and BEFORE the gNB:
#
#   deployment/scripts/sf_oran_up.sh            # O-RAN + app back end
#   deployment/scripts/sf_oran_up.sh --no-app   # without the demo orchestrator (measurement campaigns)
#
# Steps (each one is skipped when it is already up, so the script can be run again at any time):
#   1. near-RT RIC (smartfarm-flexric) + window "FlexRIC + KPM bridge" (journal)
#   2. traffic collector (10.45.0.1:8088) and APK server (10.45.0.1:8089) windows
#   3. waits for the gNB E2 Setup (start the gNB now)            -> KPM bridge (restarted if older than the gNB)
#   4. waits for the PC2 BS MAC (start start_bs.sh + N3IWF on PC2) -> PC2 UN3GPP E2 proxy over SSH (tmux e2proxy +
#      window on the PC2 monitor) -> waits for its E2 Setup
#   5. control xApp supervisor + O-RAN monitor windows; waits until both E2 nodes are subscribed
#   6. demo orchestrator (10.45.0.1:8090) window, unless --no-app
# Then start the UE side on PC3 (startmodem.sh, N3IWUE) and, for the app, ~/src/smartfarm-content/sf_app_up.sh on PC3.
set -u
cd "$(dirname "$0")/../.."
REPO=$PWD
APP=1
[ "${1:-}" = "--no-app" ] && APP=0
SUDO=sudo
[ -n "${SUDO_ASKPASS:-}" ] && SUDO="sudo -A"
XAPP_LOG=${SF_CONTROL_LOG_DIR:-experiments/gates/20261102_192902-un3gpp-e2-proxy}/sf-control.stderr
PC2_PROXY=/home/inatel-crr/src/smartfarm/un3gpp_e2_proxy
PC2_PEER=192.168.56.103

say() { echo "$(date +%T) $*"; }
ok() { echo "$(date +%T)   OK  $*"; }
window() {  # window <title> <command run from the repo root>
    systemd-run --user --collect --quiet gnome-terminal --title="$1" -- bash -c "cd $REPO && $2; exec bash"
}
wait_for() {  # wait_for <description> <hint after 90 s> <check command...>
    local what=$1 hint=$2 t=0
    shift 2
    until "$@"; do
        [ $((t % 30)) -eq 0 ] && say "   ... waiting for $what"
        [ "$t" -eq 90 ] && say "   hint: $hint"
        sleep 3
        t=$((t + 3))
    done
    ok "$what"
}
e2_peers() { ss -nA sctp 2>/dev/null | grep -oE ':36421[[:space:]]+[0-9.]+:' | awk '{print $2}' | tr -d ':' | awk '$0 != "0.0.0.0"'; }
gnb_connected() { e2_peers | awk -v pc2="$PC2_PEER" '$0 != pc2 {f=1} END {exit !f}'; }
pc2_connected() { e2_peers | awk -v pc2="$PC2_PEER" '$0 == pc2 {f=1} END {exit !f}'; }
pc2() { ssh -o BatchMode=yes -o ConnectTimeout=5 smartfarm-pc2 "$@"; }
bs_mac_up() { pc2 'pgrep -x coreL2.o >/dev/null && test -S /tmp/sync_response.sock' 2>/dev/null; }
listening() { ss -ltn | grep -q "10.45.0.1:$1 "; }
xapp_subscribed() {  # both nodes subscribed since the last xApp start
    awk '/=== sf_control start/{u=0;o=0} /role=un3gpp subscribed=1/{u=1} /role=oai subscribed=1/{o=1} END{exit !(u&&o)}' \
        < <(tail -c 2000000 "$XAPP_LOG" 2>/dev/null)
}

say "Smart Farming O-RAN bring-up (PC1); leave this window open until it says DONE"
$SUDO -v || exit 1
ip -4 addr show ogstun 2>/dev/null | grep -q 10.45.0.1 || { say "ogstun has no 10.45.0.1: start the core first (restart_core.sh)"; exit 1; }

# 1. RIC
say "[1/6] near-RT RIC"
if systemctl is-active -q smartfarm-flexric; then ok "RIC already running"; else
    $SUDO systemctl reset-failed smartfarm-flexric.service 2>/dev/null
    $SUDO systemctl start smartfarm-flexric.service
    wait_for "RIC listening on SCTP 36421" "check: journalctl -u smartfarm-flexric -n 50" \
        bash -c 'ss -nlA sctp | grep -q ":36421"'
fi
pgrep -f '[j]ournalctl -u smartfarm-flexric -u smartfarm-kpm-bridge -f' >/dev/null || \
    window "FlexRIC + KPM bridge" "journalctl -u smartfarm-flexric -u smartfarm-kpm-bridge -f -o cat | grep --line-buffered -v '^{'"

# 2. collector + APK server
say "[2/6] traffic collector and APK server"
listening 8088 && ok "collector already on :8088" || \
    window "Smart Farming traffic collector" ".venv/bin/python -u deployment/traffic/collector.py --out experiments/traffic/collected"
listening 8089 && ok "APK server already on :8089" || window "APK server (10.45.0.1:8089)" "python3 -u android/tools/apk_server.py"

# 3. gNB E2 Setup -> KPM bridge
say "[3/6] gNB (start it now if it is not running: section 1.6 of the runbook)"
wait_for "gNB E2 Setup with the RIC" \
    "if the gNB started before the RIC, it never connects: stop it (Ctrl+C) and start it again" gnb_connected
gnb_start=$(ps -o lstart= -C nr-softmodem 2>/dev/null | head -1)
bridge_start=$(systemctl show -p ActiveEnterTimestamp --value smartfarm-kpm-bridge)
if systemctl is-active -q smartfarm-kpm-bridge && [ -n "$gnb_start" ] && \
   [ "$(date -d "$bridge_start" +%s 2>/dev/null || echo 0)" -ge "$(date -d "$gnb_start" +%s)" ]; then
    ok "KPM bridge already running (started after the gNB)"
else
    $SUDO systemctl reset-failed smartfarm-kpm-bridge.service 2>/dev/null
    $SUDO systemctl restart smartfarm-kpm-bridge.service
    wait_for "KPM bridge subscribed to the slices" "check the FlexRIC + KPM bridge window" bash -c \
        "journalctl -u smartfarm-kpm-bridge -o cat --no-pager --since '2 min ago' | grep -q 'UE-level KPM subscribed'"
fi

# 4. PC2 UN3GPP E2 proxy
say "[4/6] PC2 E2 proxy (start start_bs.sh and the N3IWF on PC2 now if they are not running)"
if pc2_connected; then ok "PC2 E2 proxy already connected"; else
    wait_for "BS MAC on PC2" "on PC2: start_bs.sh (never inside tmux), then the N3IWF" bs_mac_up
    R=$PC2_PROXY/runtime
    pc2 "sudo -n tmux kill-session -t e2proxy 2>/dev/null; \
      sudo -n tmux new-session -d -s e2proxy -c $R '../build/smartfarm_un3gpp_e2_proxy -c $R/flexric.conf -p $R/sm/ 2>&1 | tee $R/proxy.log'; \
      sleep 1; DISPLAY=:0 XAUTHORITY=/home/inatel-crr/.Xauthority setsid nohup x-terminal-emulator -T e2proxy \
      -e sudo tmux attach-session -t e2proxy >/dev/null 2>&1 < /dev/null &"
    wait_for "PC2 E2 proxy E2 Setup with the RIC" "on PC2: sudo tmux attach -t e2proxy" pc2_connected
fi

# 5. control xApp + monitor
say "[5/6] control xApp"
xapp_older_than_gnb() {  # the xApp only subscribes to the E2 nodes present when it starts
    local x g
    x=$(ps -o lstart= -p "$(pgrep -f '[f]lexric-bridge/smartfarm_sf_control -c' | head -1)" 2>/dev/null) || return 1
    g=$(ps -o lstart= -C nr-softmodem 2>/dev/null | head -1)
    [ -n "$x" ] && [ -n "$g" ] && [ "$(date -d "$x" +%s)" -lt "$(date -d "$g" +%s)" ]
}
if pgrep -f '[r]un_sf_control_supervised' >/dev/null; then
    if xapp_subscribed && ! xapp_older_than_gnb; then ok "xApp already running with both nodes subscribed"; else
        say "   xApp without both nodes or older than the gNB: relaunching it (the supervisor restarts it)"
        pid=$(pgrep -f '[f]lexric-bridge/smartfarm_sf_control -c' | head -1)
        [ -n "$pid" ] && { kill -INT "$pid"; sleep 4; kill -0 "$pid" 2>/dev/null && kill -KILL "$pid"; }
    fi
else
    window "O-RAN control xApp (OAI + UN3GPP)" "deployment/scripts/run_sf_control_supervised.sh"
fi
wait_for "xApp subscribed to both E2 nodes (role=oai, role=un3gpp)" "check: tail $XAPP_LOG" xapp_subscribed
pgrep -f '[o]ran_live_monitor.py' >/dev/null || window "O-RAN monitor (UN3GPP UL + OAI DL)" \
    ".venv/bin/python deployment/scripts/oran_live_monitor.py --xapp-jsonl ${XAPP_LOG%.stderr}.jsonl"

# 6. demo orchestrator
if [ "$APP" = 1 ]; then
    say "[6/6] demo orchestrator"
    listening 8090 && ok "orchestrator already on :8090" || {
        window "SF demo orchestrator (10.45.0.1:8090)" \
            ".venv/bin/python -u deployment/scripts/demo_orchestrator.py --tolerate-probe-failure"
        wait_for "orchestrator on 10.45.0.1:8090" "check the orchestrator window" listening 8090
    }
else
    say "[6/6] demo orchestrator skipped (--no-app)"
fi

say "DONE. Next: PC3 startmodem.sh + N3IWUE; for the app, on PC3: ~/src/smartfarm-content/sf_app_up.sh"
