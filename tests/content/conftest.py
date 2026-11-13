"""Shared fixtures of the content generator tests: import path and a small synthetic media pool.

The synthetic pool (simple gradients, a few frames) keeps the tests independent of the ~260 MB rendered media.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

CONTENT = Path(__file__).resolve().parents[2] / "deployment" / "content"
sys.path.insert(0, str(CONTENT))


def make_media(root: Path, frames: int = 45) -> Path:
    from sfcontent import crop

    rng = np.random.default_rng(7)
    yy, xx = np.mgrid[0:crop.BASE_H, 0:crop.BASE_W]
    for state in crop.STATES:
        folder = root / "crop" / state / "00"
        folder.mkdir(parents=True)
        rows = (np.sin(xx / 40.0) > 0.2)
        rgb = np.stack([np.where(rows, 40, 140), np.where(rows, 90, 95), np.where(rows, 30, 70)], -1)
        rgb = np.clip(rgb + rng.integers(0, 12, rgb.shape), 0, 255).astype(np.uint8)
        ndvi = np.where(rows, 0.8, 0.12)
        zones = []
        if state != "healthy":
            ndvi[600:900, 700:1200] = 0.3
            zones = [{"box": [700, 600, 1200, 900], "label": "Pest hotspot" if state == "pest" else "Water stress",
                      "severity": "high"}]
        Image.fromarray(rgb).save(folder / "rgb.jpg", quality=92)
        Image.fromarray(np.round((ndvi + 1) * 127.5).astype(np.uint8)).save(folder / "ndvi.png")
        (folder / "info.json").write_text(json.dumps({"state": state, "zones": zones}))
    sizes = []
    for variant in ("normal", "alert"):
        (root / "drone" / variant).mkdir(parents=True)
        for k in range(frames):
            image = Image.new("RGB", (1920, 1080), (30 + k, 90, 40 if variant == "normal" else 140))
            draw = ImageDraw.Draw(image)
            for i in range(0, 2200, 160):
                draw.line((i + k * 4, 0, i + k * 4 - 300, 1080), fill=(200, 120, 80), width=12)
            draw.text((40, 40), f"{variant} {k}", fill=(255, 255, 255))
            path = root / "drone" / variant / f"{k:06d}.jpg"
            image.save(path, quality=80)
            sizes.append(path.stat().st_size)
    stats = {"frames": frames, "bytes_mean": int(sum(sizes) / len(sizes))}
    (root / "drone" / "manifest.json").write_text(json.dumps(
        {"v": 1, "fps": 30, "frames": frames, "width": 1920, "height": 1080, "variants": ["normal", "alert"],
         "stats": {"normal": stats, "alert": stats}}))
    return root


@pytest.fixture(scope="session")
def media(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_media(tmp_path_factory.mktemp("media"))


def free_port_offset() -> int:
    """A port offset under which every contract port (47000-47010) is free on loopback."""

    for offset in range(11000, 17000, 37):
        ok = True
        for port in (47000, 47001, 47002, 47003, 47010):
            for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
                with socket.socket(socket.AF_INET, kind) as s:
                    try:
                        s.bind(("127.0.0.1", port + offset))
                    except OSError:
                        ok = False
        if ok:
            return offset
    raise RuntimeError("no free port range")


@pytest.fixture()
def port_offset() -> int:
    return free_port_offset()
