#!/bin/bash
# Pre-run gate: refuses to start a measurement unless KPM, xApp, USRP and both UE sessions are healthy
cd /home/inatel-crr/5gc-un3gpp/smart-farming-oran
fail=0
r1=$(journalctl -u smartfarm-kpm-bridge --since "-3 s" --no-pager | grep -o "received=[0-9]*" | tail -1 | cut -d= -f2)
sleep 4
r2=$(journalctl -u smartfarm-kpm-bridge --since "-3 s" --no-pager | grep -o "received=[0-9]*" | tail -1 | cut -d= -f2)
[ -n "$r1" ] && [ -n "$r2" ] && [ "$r2" -gt "$r1" ] && echo "OK  KPM samples flowing ($r1 -> $r2)" || { echo "FAIL KPM bridge not receiving"; fail=1; }
pgrep -f "smartfarm_sf_control -c" >/dev/null && echo "OK  control xApp running" || { echo "FAIL control xApp"; fail=1; }
L=$(ls -t experiments/maintenance/*-oai-gnb.log | head -1)
o=$(tail -2000 "$L" | grep -c ERROR_CODE_OVERFLOW); [ "$o" -lt 5 ] && echo "OK  USRP overflows in recent log: $o" || { echo "FAIL USRP overflows: $o"; fail=1; }
g=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor); [ "$g" = performance ] && echo "OK  CPU governor $g" || { echo "FAIL CPU governor $g"; fail=1; }
.venv/bin/python deployment/scripts/resolve_ue_endpoints.py --shell >/dev/null 2>&1 && echo "OK  both UE sessions in the core" || { echo "FAIL UE sessions"; fail=1; }
exit $fail
