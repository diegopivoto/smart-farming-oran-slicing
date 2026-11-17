#!/bin/bash
# E3: untrained phone position, adaptive purchase, DDQN with anchored online adaptation (same settings as the O-RAN paper)
cd /home/inatel-crr/5gc-un3gpp/smart-farming-oran
deployment/dlt/gate.sh || { echo "GATE FAILED: run not started"; exit 3; }
SVC=${1:-drone}; STEPS=${2:-16}
RUNS="$PWD/experiments/calibration/20261104_071216-drone-final-4passes $PWD/experiments/calibration/20261104_121141-crop-final-4passes $PWD/experiments/calibration/20261103_123134-weather-4passes"
MODEL=experiments/rl/20261104_170128-ddqn-phi-g0.5-m0.0-d1.0-final-crop/model-final.npz
OUT=experiments/dlt/$(date +%Y%m%d_%H%M%S)-E3-$SVC-adaptive-learn; mkdir -p "$OUT"

# first-step watchdog: abort at once when the phone receiver does not report (no receiver windows)
( while [ ! -s "$OUT/steps.jsonl" ]; do sleep 3; done
  n=$(head -1 "$OUT/steps.jsonl" | python3 -c "import json,sys;print(json.loads(sys.stdin.read())['cell'].get('traffic_records') or 0)")
  if [ "$n" -lt 2 ]; then echo "WATCHDOG: only $n record(s) (no receiver report) in step 0, aborting" | tee -a "$OUT/console.log"
    pkill -INT -f "rl_online.py --service $SVC"; fi ) &

SF_SKIP_PHONE_PING=1 .venv/bin/python -u deployment/scripts/rl_online.py --service $SVC --policy ddqn --steps $STEPS \
  --elimination-rule cause --margin-weight 0.0 --learn --learn-steps 2 --anchor-replay 1000 --epsilon 0.0 --target-sync 20 \
  --converge-k 10 --min-syncs 3 --model "$MODEL" --runs $RUNS --output "$OUT" > "$OUT/console.log" 2>&1
echo "exit $? $OUT"
