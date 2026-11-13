#!/usr/bin/env python3
"""Smart Farming demo content generator for the UN3GPP UE (PC3). Contract: docs/design/smartfarm-app-protocol.md.

Real content (weather telemetry, multispectral crop captures, MJPEG drone video) with the calibration traffic's
measurement properties (sf_traffic.py headers, ports, rates, clock service and generator summary record).

  render   pre-render the media pool (PC1, numpy + Pillow):
             nice -n 19 python3 sf_content.py render --media media --workers 12
  node     the content node: control server TCP 47010, clock UDP 47000, the three senders and the local web panel
           (http://127.0.0.1:8090). On PC3, inside the N3IWUE VRF:
             sudo ip vrf exec vrf-n3iwue python3 sf_content.py node   # phone address from each app request
  send     drop-in for `sf_traffic.py send` (same options; used by the node agent). It hands the run to a running
           node (so the panel shows it and the panel/phone state applies), or runs it standalone otherwise:
             python3 sf_content.py send --service drone --target <phone IP> --duration 30 --run-id <hex32>
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

DEFAULT_MEDIA = HERE / "media"


def cmd_render(args: argparse.Namespace) -> int:
    if args.nice:
        try:
            os.nice(args.nice)
        except OSError:
            pass
    media = Path(args.media)
    media.mkdir(parents=True, exist_ok=True)
    if args.what in ("all", "crop"):
        from sfcontent import crop

        crop.render_pool(media, args.crop_per_state, args.workers)
    if args.what in ("all", "drone"):
        from sfcontent import drone

        manifest = drone.render_loops(media, args.workers, seconds=args.seconds, fps=args.fps, quality=args.quality,
                                      frames=args.frames, max_bytes=args.max_frame_bytes)
        for variant, stats in manifest["stats"].items():
            print(f"drone {variant}: {stats['frames']} frames, mean {stats['bytes_mean'] / 1000:.0f} kB "
                  f"(min {stats['bytes_min'] / 1000:.0f}, max {stats['bytes_max'] / 1000:.0f}), "
                  f"{stats['mbit_s_at_fps']} Mbit/s at {manifest['fps']} fps, JPEG quality mean "
                  f"{stats['quality_mean']} (min {stats['quality_min']})", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Smart Farming demo content generator")
    sub = parser.add_subparsers(dest="command", required=True)

    render = sub.add_parser("render", help="pre-render the media pool")
    render.add_argument("--media", default=str(DEFAULT_MEDIA))
    render.add_argument("--what", choices=("all", "crop", "drone"), default="all")
    render.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    render.add_argument("--crop-per-state", type=int, default=4)
    render.add_argument("--seconds", type=float, default=24.0)
    render.add_argument("--fps", type=int, default=30)
    render.add_argument("--quality", type=int, default=90)
    render.add_argument("--max-frame-bytes", type=int, default=150_000)
    render.add_argument("--frames", type=int, help="render only the first N frames (preview)")
    render.add_argument("--nice", type=int, default=19)

    from sfcontent import node as node_mod

    node_mod.add_arguments(sub)
    args = parser.parse_args()
    if args.command == "render":
        return cmd_render(args)
    return node_mod.dispatch(args)


if __name__ == "__main__":
    sys.exit(main())
