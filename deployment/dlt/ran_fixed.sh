#!/bin/bash
# RAN default with fixed UL reservations: BS link adaptation on 33, 66, 99 and 132 RB (10 steps each; RBS overrides),
# smallest DL quota, no purchase (CHF stopped). Usage: deployment/dlt/ran_fixed.sh <service> [tag]
cd "$(dirname "$0")/../.."
SVC=$1; TAG=${2:-}
for spec in ${RBS:-33:9 66:9 99:9 132:9}; do
  RB=${spec%%:*} TAG=$TAG deployment/dlt/e1.sh "$SVC" "${spec##*:}" || exit $?
done
