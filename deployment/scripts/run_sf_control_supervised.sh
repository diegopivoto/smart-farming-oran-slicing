#!/bin/bash
# Keeps the Smart Farming E2 control xApp (smartfarm_sf_control) running: relaunches it after any exit (the FlexRIC SDK
# segfaulted in assoc_rb_tree_extract_node on 2026-11-03), appending to the same JSONL/stderr the analyses read.
set -u
cd "$(dirname "$0")/../.."
G=${SF_CONTROL_LOG_DIR:-experiments/gates/20261102_192902-un3gpp-e2-proxy}
while true; do
    echo "=== sf_control start $(date -Is) ===" >> "$G/sf-control.stderr"
    SMARTFARM_SF_SOCKET=/run/user/1000/smartfarm-sf.sock .build/flexric-bridge/smartfarm_sf_control \
        -c /usr/local/etc/flexric/flexric.conf -p /usr/local/lib/flexric/ >> "$G/sf-control.jsonl" 2>> "$G/sf-control.stderr" < /dev/null
    rc=$?
    echo "$(date +%T) smartfarm_sf_control exited rc=$rc; restarting in 2 s" | tee -a "$G/sf-control.stderr"
    sleep 2
done
