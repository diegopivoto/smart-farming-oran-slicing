#!/bin/bash
# Installs the Open5GS SMF with the Nchf_ConvergedCharging consumer (branch dlt-slice-market of open5gs),
# keeping the original binary as /usr/bin/open5gs-smfd.oran-tvws-5g. Revert: --revert.
set -euo pipefail
SRC=${SMF_BUILD:-/home/inatel-crr/src/open5gs-oran-tvws-5g/build/src/smf/open5gs-smfd}
if [ "${1:-}" = "--revert" ]; then
  install -m 755 /usr/bin/open5gs-smfd.oran-tvws-5g /usr/bin/open5gs-smfd
else
  [ -f /usr/bin/open5gs-smfd.oran-tvws-5g ] || cp /usr/bin/open5gs-smfd /usr/bin/open5gs-smfd.oran-tvws-5g
  install -m 755 "$SRC" /usr/bin/open5gs-smfd
fi
systemctl restart open5gs-smfd
sleep 2
systemctl is-active open5gs-smfd
