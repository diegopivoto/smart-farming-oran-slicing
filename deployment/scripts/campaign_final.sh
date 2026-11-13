#!/bin/bash
# Final campaign (2026-11-04), after the N3IWF fixes (pinned to the PC2 OS cores 14,15; GRE rcvbuf 32 MiB), the gNB
# pusch_FailureThres=100 and the fixed xApp: UN3GPP UE to the service slice (the phone must already be on the service
# APN) -> calibration (PASSES x full action space) -> raw export + analyses -> DDQN retrain -> online A/B session.
# Usage: campaign_final.sh <drone|crop|weather> <passes> <crop calibration dir for the retrain>
# The retrain uses the new calibration for SVC and the final calibrations of the other two services.
# MODEL=<model.npz> MODEL_RUNS="<runs it was trained on>" skips the retrain and runs the online session with MODEL.
# RESUME_DIR=<calibration dir> continues an interrupted calibration there (calibration_sweep.py --resume).
set -u
cd /home/inatel-crr/5gc-un3gpp/smart-farming-oran
export PYTHONPATH=src
SVC=$1; PASSES=$2; CROP_RUN=$3
TS=$(date +%Y%m%d_%H%M%S)
OUT=${RESUME_DIR:-experiments/calibration/${TS}-${SVC}-final-${PASSES}passes}; mkdir -p "$OUT"
RESUME=; [ -n "${RESUME_DIR:-}" ] && RESUME=--resume
LOG=experiments/calibration/final-campaign.log
pc2_probe() { ssh -o BatchMode=yes smartfarm-pc2 'P=$(pgrep -x n3iwf); echo n3iwf_pid=$P gre_drops=$(sudo -n ss -wmH | grep -o "d[0-9]*)" | tr -d "d)") affinity=$(taskset -cp $P | awk "{print \$NF}") $(sudo -n ss -wmH | grep -o "rb[0-9]*" | head -1); for t in /proc/$P/task/*; do read r w x < $t/schedstat; echo $r $w; done | awk "{r+=\$1;w+=\$2} END{print \"run_ns=\" r, \"wait_ns=\" w}"'; }
echo "$(date +%T) [$SVC] PC2 before: $(pc2_probe | tr '\n' ' ')" | tee -a $LOG $OUT/pc2-n3iwf-probe.txt
echo "$(date +%T) UN3GPP UE -> $SVC slice" | tee -a $LOG
.venv/bin/python - "$SVC" <<'PY' 2>&1 | tee -a $LOG
import asyncio, sys
sys.path.insert(0, "deployment/scripts")
from slice_switch import AGENT_KEY, un3gpp_address
from smartfarm.contracts.service import ServiceId
from smartfarm.orchestrator.un3gpp_slice import Un3gppSliceClient, agent_transport, load_agent_key
SVC = ServiceId(sys.argv[1])
async def main():
    c = Un3gppSliceClient(agent_transport(un3gpp_address, load_agent_key(AGENT_KEY)))
    before = await c.status()
    if await c.request(SVC):
        st = await c.await_status(SVC, before.pdu_session_id if before else None, timeout_s=60)
        print("un3gpp in slice", st.sd, st.ipv4)
    else:
        print("un3gpp already in the slice", SVC.value)
asyncio.run(main())
PY
echo "$(date +%T) calibration -> $OUT" | tee -a $LOG
.venv/bin/python -u deployment/scripts/calibration_sweep.py --service $SVC --passes $PASSES --cell-s 30 --output "$OUT" $RESUME 2>&1 | tee -a "$OUT/console.log"
echo "$(date +%T) calibration exited; raw export + analyses" | tee -a $LOG
read S E < <(.venv/bin/python -c "
import json
r=[json.loads(l) for l in open('$OUT/cells.jsonl')]
from datetime import datetime
f=lambda x: datetime.fromisoformat(x).astimezone().strftime('%Y-%m-%d %H:%M:%S')
print(f(r[0]['started_at']).replace(' ','T'), f([x for x in r if x.get('finished_at')][-1]['finished_at']).replace(' ','T'))")
S=${S/T/ }; E=${E/T/ }
mkdir -p $OUT/raw
X=experiments/gates/20261102_192902-un3gpp-e2-proxy/sf-control.jsonl
.venv/bin/python - "$X" "$(date -d "$S" +%s)" "$(date -d "$E 1 min" +%s)" "$OUT/raw/xapp-sf-control.jsonl.gz" <<'PY'
import sys, gzip
src, s, e, dst = sys.argv[1], int(sys.argv[2])*10**9, int(sys.argv[3])*10**9, sys.argv[4]
n=0
with open(src, encoding="utf-8", errors="replace") as f, gzip.open(dst, "wt", encoding="utf-8") as g:
    for line in f:
        i=line.find('"received_at_ns":')
        if i<0: continue
        t=int(line[i+17:i+17+19])
        if s<=t<=e: g.write(line); n+=1
print("xapp lines", n)
PY
journalctl -u smartfarm-kpm-bridge --since "$S" --until "$E" -o cat --no-pager | gzip > $OUT/raw/kpm-bridge-journal.jsonl.gz
journalctl -u smartfarm-flexric --since "$S" --until "$E" -o short-iso-precise --no-pager | gzip > $OUT/raw/flexric-journal.log.gz
nice -n 5 .venv/bin/python deployment/scripts/analyze_calibration.py $OUT >> $LOG 2>&1
nice -n 5 .venv/bin/python deployment/scripts/analyze_oran_evidence.py $OUT >> $LOG 2>&1
echo "$(date +%T) [$SVC] PC2 after: $(pc2_probe | tr '\n' ' ')" | tee -a $LOG $OUT/pc2-n3iwf-probe.txt
DRONE_RUN=$OUT; [ "$SVC" != drone ] && DRONE_RUN=$(cat experiments/calibration/final-drone-dir)
WEATHER_RUN=experiments/calibration/20261103_123134-weather-4passes
[ -f experiments/calibration/final-weather-dir ] && WEATHER_RUN=$(cat experiments/calibration/final-weather-dir)
[ "$SVC" = drone ] && echo "$OUT" > experiments/calibration/final-drone-dir
[ "$SVC" = weather ] && echo "$OUT" > experiments/calibration/final-weather-dir && WEATHER_RUN=$OUT
[ "$SVC" = crop ] && CROP_RUN=$OUT
echo "$(date +%T) retrain DDQN ($DRONE_RUN + $CROP_RUN + $WEATHER_RUN)" | tee -a $LOG
RL=experiments/rl/$(date +%Y%m%d_%H%M%S)-ddqn-phi-g0.5-m0.0-d1.0-final-$SVC; mkdir -p $RL
RUNS="$PWD/$DRONE_RUN $PWD/$CROP_RUN $PWD/$WEATHER_RUN"
if [ -n "${MODEL:-}" ]; then  # no retrain: keep MODEL and the calibration runs it was trained on (its action index)
    rmdir $RL; RUNS=$MODEL_RUNS
    echo "$(date +%T) retrain skipped: online session with $MODEL" | tee -a $LOG
else
OMP_NUM_THREADS=4 nice -n 5 .venv/bin/python -u deployment/scripts/rl_offline.py --output $RL --episodes 3000 --action-features \
    --hidden 64,64 --batch 32 --gamma 0.5 --margin-weight 0.0 --drift 1.0 --runs $RUNS > $RL/console.log 2>&1
MODEL=$RL/model-final.npz
fi
echo "$(date +%T) online A/B $SVC session" | tee -a $LOG
ON=experiments/rl-online/$(date +%Y%m%d_%H%M%S)-$SVC-final-alternate-majority; mkdir -p $ON
.venv/bin/python -u deployment/scripts/rl_online.py --service $SVC --policy alternate --steps 40 --elimination-rule majority \
    --margin-weight 0.0 --model $MODEL --runs $RUNS --output $ON 2>&1 | tee -a $ON/console.log
.venv/bin/python deployment/scripts/rl_online_report.py $ON >> $LOG 2>&1
echo "$(date +%T) [$SVC] PC2 end: $(pc2_probe | tr '\n' ' ')" | tee -a $LOG $ON/pc2-n3iwf-probe.txt
echo "$(date +%T) final campaign $SVC done: $OUT $RL $ON" | tee -a $LOG
