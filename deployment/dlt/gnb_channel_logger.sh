#!/bin/bash
# Timestamps the per-UE channel lines (RSRP, DL/UL SNR and BLER) of the newest OAI gNB log, which has no timestamps.
# Run from the smart-farming-oran root after the gNB is up:  systemd-run --user --unit sf-gnb-channel deployment/dlt/gnb_channel_logger.sh
cd "$(dirname "$0")/../.."
L=$(ls -t experiments/maintenance/*-oai-gnb.log | head -1)
mkdir -p experiments/dlt
tail -n0 -F "$L" | while IFS= read -r ln; do
  case "$ln" in *"average RSRP"*|*dlsch_rounds*|*ulsch_rounds*) printf '%s\t%s\n' "$(date +%Y-%m-%dT%H:%M:%S.%3N)" "$ln";; esac
done >> experiments/dlt/gnb-channel.tsv
