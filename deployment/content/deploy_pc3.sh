#!/usr/bin/env bash
# PC1 -> PC3: install the content generator (code + rendered media) under ~/src/smartfarm-content/.
#
#   deployment/content/deploy_pc3.sh            # code + media (~270 MB the first time, rsync deltas afterwards)
#   CODE_ONLY=1 deployment/content/deploy_pc3.sh
#   BWLIMIT=4000 deployment/content/deploy_pc3.sh   # KiB/s cap for the media copy
#
# PC3's SSH rides the UN3GPP link under test: NEVER run this while a measurement is in progress.
# It only copies files; it does not touch the radio, IKE, the N3IWUE, the node agent or a running content node.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
HOST="${HOST:-smartfarm-pc3}"
DEST="${DEST:-src/smartfarm-content}"

if [ -z "${CODE_ONLY:-}" ] && [ ! -f "$HERE/media/drone/manifest.json" ]; then
    echo "render the media first: nice -n 19 .venv/bin/python deployment/content/sf_content.py render" >&2
    exit 1
fi
ssh "$HOST" "mkdir -p $DEST/sfcontent"
rsync -a --exclude __pycache__ \
    "$HERE/sf_content.py" "$HERE/sf_content_ref_parser.py" "$HERE/run_node.sh" "$HERE/sf_app_up.sh" \
    "$HERE/sf_app_down.sh" "$REPO/deployment/traffic/sf_traffic.py" "$REPO/deployment/pc3/smartfarm_node_agent.py" \
    "$HOST:$DEST/"
rsync -a --delete --exclude __pycache__ "$HERE/sfcontent/" "$HOST:$DEST/sfcontent/"
if [ -z "${CODE_ONLY:-}" ]; then
    rsync -a --delete ${BWLIMIT:+--bwlimit="$BWLIMIT"} "$HERE/media/" "$HOST:$DEST/media/"
fi
ssh "$HOST" "cd $DEST && chmod +x run_node.sh sf_content.py sf_app_up.sh sf_app_down.sh && python3 -c '
import sys; sys.path.insert(0, \".\")
from pathlib import Path
from sfcontent import node, crop, drone
frames = drone.DroneFrames(Path(\"media\"))
obj = crop.CropFactory(Path(\"media\")).build(\"pest\")
print(\"content generator OK on\", __import__(\"socket\").gethostname(), \"- drone loop\", frames.count, \"frames,\",
      \"crop object\", len(obj.payload), \"B built in\", obj.build_s, \"s\")
'"
