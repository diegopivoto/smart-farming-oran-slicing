#!/bin/bash
# Shuts down everything sf_oran_up.sh (and the gNB) started on PC1 with one command, in reverse dependency order, and
# closes their windows. Run it on PC1:
#
#   deployment/scripts/sf_oran_down.sh            # O-RAN + app back end + gNB (the core stays up)
#   deployment/scripts/sf_oran_down.sh --core     # also stops the Open5GS core (open5gs-* services)
#   deployment/scripts/sf_oran_down.sh --dry-run  # only lists what would be stopped
#
# Steps (each one is skipped when it is not running, so the script can be run again at any time):
#   1. demo orchestrator, O-RAN monitor, traffic collector, APK server, time server
#   2. control xApp supervisor + smartfarm_sf_control (it ignores SIGTERM: SIGINT, then SIGKILL)
#   3. PC2 UN3GPP E2 proxy over SSH (tmux e2proxy + its window), when PC2 is reachable
#   4. gNB (nr-softmodem, root: SIGINT, then SIGKILL) and its window
#   5. KPM bridge and near-RT RIC (systemd) + the "FlexRIC + KPM bridge" journal window
#   6. Open5GS core, only with --core
# PC2 (BS, N3IWF) and PC3 (UE, N3IWUE, app) are shut down on those machines (runbook section 7.1).
set -u
cd "$(dirname "$0")/../.."
CORE=0
DRY=0
for a in "$@"; do
    case $a in
        --core) CORE=1 ;;
        --dry-run) DRY=1 ;;
        *) echo "usage: $0 [--core] [--dry-run]"; exit 2 ;;
    esac
done
SUDO=sudo
[ -n "${SUDO_ASKPASS:-}" ] && SUDO="sudo -A"

say() { echo "$(date +%T) $*"; }
ok() { echo "$(date +%T)   OK  $*"; }
alive() { local p; for p in "$@"; do kill -0 "$p" 2>/dev/null && return 0; done; return 1; }
window_shells() {  # the window shell (child of gnome-terminal-server) above each process in $@, at most 4 levels up
    local p pp n
    for p in "$@"; do
        for n in 1 2 3 4; do
            pp=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
            [ -z "$pp" ] || [ "$pp" -le 1 ] && break
            [ "$(ps -o comm= -p "$pp")" = gnome-terminal- ] && { echo "$p"; break; }
            p=$pp
        done
    done
}
stop() {  # stop <description> <pgrep -f pattern, with a [x] bracket so it never matches this script's shell>
    local what=$1 pids shells t=0
    pids=$(pgrep -f "$2")
    [ -z "$pids" ] && { ok "$what not running"; return; }
    say "   stopping $what (pid $(echo $pids))"
    [ "$DRY" = 1 ] && return
    shells=$(window_shells $pids)
    # Killing the wrapper first keeps it from falling back to "exec bash", so the window closes with the process.
    [ -n "$shells" ] && kill -KILL $shells 2>/dev/null
    kill -INT $pids 2>/dev/null
    while alive $pids && [ $t -lt 10 ]; do sleep 1; t=$((t + 1)); done
    alive $pids && kill -KILL $pids 2>/dev/null
    ok "$what stopped"
}

say "Smart Farming O-RAN shutdown (PC1)$([ "$DRY" = 1 ] && echo ', dry run')"
[ "$DRY" = 1 ] || $SUDO -v || exit 1

# 1. app back end and helper windows
say "[1/6] demo orchestrator, monitor, collector, APK server, time server"
stop "demo orchestrator" '[d]emo_orchestrator.py'
stop "O-RAN monitor" '[o]ran_live_monitor.py'
stop "traffic collector" '[d]eployment/traffic/collector.py'
stop "APK server" '[a]pk_server.py'
stop "time server" '[d]eployment/time/time_server.py'

# 2. control xApp: the supervisor first, or it relaunches the xApp
say "[2/6] control xApp"
stop "xApp supervisor" '[r]un_sf_control_supervised'
stop "smartfarm_sf_control" '[f]lexric-bridge/smartfarm_sf_control -c'

# 3. PC2 E2 proxy
say "[3/6] PC2 E2 proxy"
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 smartfarm-pc2 true 2>/dev/null; then
    ok "PC2 not reachable: skipped (stop it on PC2 with: sudo tmux kill-session -t e2proxy)"
elif ! ssh -o BatchMode=yes smartfarm-pc2 'sudo -n tmux has-session -t e2proxy' 2>/dev/null; then
    ok "PC2 E2 proxy not running"
else
    say "   stopping the PC2 E2 proxy (tmux e2proxy)"
    [ "$DRY" = 1 ] || { ssh -o BatchMode=yes smartfarm-pc2 'sudo -n tmux kill-session -t e2proxy'; ok "PC2 E2 proxy stopped"; }
fi

# 4. gNB (root, started by hand in the "OAI gNB (E2-SF)" window)
say "[4/6] gNB"
gnb=$(pgrep -x nr-softmodem)
if [ -z "$gnb" ]; then ok "gNB not running"; else
    say "   stopping the gNB (pid $(echo $gnb))"
    if [ "$DRY" = 0 ]; then
        # The window's interactive shell started the gNB through sudo; it is closed after the gNB exits.
        shells=$(window_shells $gnb)
        $SUDO kill -INT $gnb
        t=0
        while alive $gnb && [ $t -lt 15 ]; do sleep 1; t=$((t + 1)); done
        alive $gnb && $SUDO kill -KILL $gnb
        [ -n "$shells" ] && kill -HUP $shells 2>/dev/null
        ok "gNB stopped"
    fi
fi

# 5. KPM bridge + RIC
say "[5/6] KPM bridge and near-RT RIC"
for s in smartfarm-kpm-bridge smartfarm-flexric; do
    if systemctl is-active -q $s; then
        say "   stopping $s"
        [ "$DRY" = 1 ] || { $SUDO systemctl stop $s.service; ok "$s stopped"; }
    else ok "$s not running"; fi
done
stop "journal window" '[j]ournalctl -u smartfarm-flexric -u smartfarm-kpm-bridge -f'

# 6. core
if [ "$CORE" = 1 ]; then
    say "[6/6] Open5GS core"
    units=$(systemctl list-units --type=service --state=active --no-legend 'open5gs-*' | awk '{print $1}')
    if [ -z "$units" ]; then ok "core not running"; else
        say "   stopping $(echo $units)"
        [ "$DRY" = 1 ] || { $SUDO systemctl stop $units; ok "core stopped"; }
    fi
else
    say "[6/6] Open5GS core kept up (use --core to stop it)"
fi

say "DONE. PC2/PC3 are shut down on those machines (runbook section 7.1)."
