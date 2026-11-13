"""Crop multispectral captures: offline pool rendering (PC1) and the per-object builder used by the sender (PC3).

Pool (media/crop/<state>/<k>/): `rgb.jpg` (quality 95) and `ndvi.png` (NDVI quantised to uint8) of a 2432x1824 base
scene plus `info.json` with the stress zones in base pixels. Each object takes a random 2304x1728 window of a pool
entry (so every capture differs: id, timestamps, framing, exposure), encodes rgb.jpg and the NDVI false-colour
ndvi.jpg, derives the 5 raw bands from the same pixels and packs the SFIM container to exactly the object size.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from . import protocol as P

STATES = ("healthy", "pest", "water_stress")
BASE_W, BASE_H = 2432, 1824
OUT_W, OUT_H = 2304, 1728
RES = 36.0  # px per metre (~2.8 cm ground sampling distance)
BANDS = ["blue", "green", "red", "red_edge", "nir"]
FIELD = "field-A"
# contract v1.1 meta.json fields, cumulative over the generator run up to the moment the object starts (TCP_INFO of
# every connection of the run, as sf_traffic.py reads it): ratio = bytes retransmitted / bytes sent
TCP_SO_FAR_KEYS = ("tcp_retrans_ratio_so_far", "tcp_bytes_sent_so_far", "tcp_bytes_retrans_so_far")


# ---------------------------------------------------------------- offline pool rendering (numpy-heavy)

def capture_scene(state: str, seed: int):  # type: ignore[no-untyped-def]
    from . import field as fl

    rng = np.random.default_rng(seed)
    sc = fl.Scene(BASE_H, BASE_W, RES, seed=seed)
    X, Y = fl.grids(sc)
    wm, hm = BASE_W / RES, BASE_H / RES
    plot_mask = np.ones((BASE_H, BASE_W), np.float32)
    if rng.random() < 0.5:  # farm road along one side
        vertical = rng.random() < 0.5
        coord, extent = (X, wm) if vertical else (Y, hm)
        other = Y if vertical else X
        center = (0.12 if rng.random() < 0.5 else 0.88) * extent + 1.5 * np.sin(other / 11.0 + rng.random() * 6)
        dist = np.abs(coord - center)
        sc.road = fl.smoothstep(3.1, 2.6, dist)
        sc.road_tracks = fl.smoothstep(0.35, 0.1, np.abs(dist - 0.95))
        sc.grass = fl.smoothstep(5.5, 4.5, dist) * (1 - sc.road)
        plot_mask = fl.smoothstep(5.8, 6.6, dist)
        if rng.random() < 0.6:  # windbreak trees between the road and the image edge
            near_low = float(np.mean(center)) < extent / 2
            edge = 1.8 if near_low else extent - 1.8
            for along in np.arange(rng.uniform(0, 6), hm if vertical else wm, rng.uniform(6.5, 8.5)):
                pos, r = edge + float(rng.uniform(-1.0, 1.0)), float(rng.uniform(2.8, 4.2))
                sc.trees.append((pos, float(along), r) if vertical else (float(along), pos, r))
    if rng.random() < 0.5:
        angle = float(rng.uniform(-25, 25))
    else:
        angle = float(rng.uniform(65, 115))
    sc.plots = [fl.Plot(plot_mask, angle, float(rng.uniform(3.3, 3.8)), float(rng.uniform(1.8, 2.2)),
                        plant_m=float(rng.uniform(0.65, 0.85)), curv_amp_m=float(rng.choice([0.0, rng.uniform(1, 3)])),
                        curv_len_m=float(rng.uniform(45, 90)), phase_m=float(rng.uniform(0, 4)))]
    sc.sun_azimuth_deg = float(rng.uniform(0, 360))
    sc.sun_elevation_deg = float(rng.uniform(38, 68))
    sc.cloud_shadow = float(rng.choice([0.0, rng.uniform(0.12, 0.3)]))
    sc.exposure = float(rng.uniform(0.95, 1.06))

    zones: list[dict] = []
    if state == "pest":
        for _ in range(int(rng.integers(2, 5))):
            r = float(rng.uniform(4.0, 7.0))
            cx, cy = float(rng.uniform(0.15, 0.85) * wm), float(rng.uniform(0.15, 0.85) * hm)
            mask = fl.blob(sc, cx, cy, r * rng.uniform(0.9, 1.3), r, rng, wobble=0.35) * plot_mask
            box = fl.bbox(mask)
            if box is None:
                continue
            sc.stress.append(fl.Stress(mask, "pest"))
            zones.append({"box": box, "label": "Pest hotspot", "severity": "high" if r > 5.5 else "medium"})
    elif state == "water_stress":
        cx, cy = float(rng.uniform(0.3, 0.7) * wm), float(rng.uniform(0.3, 0.7) * hm)
        mask = fl.blob(sc, cx, cy, float(rng.uniform(17, 26)), float(rng.uniform(12, 18)), rng, wobble=0.4,
                       soft_m=5.0)
        sc.stress.append(fl.Stress(mask, "water"))
        box = fl.bbox(mask)
        if box:
            zones.append({"box": box, "label": "Water stress", "severity": "high"})
    return sc, zones


def render_capture(job: tuple[str, int, str]) -> str:
    from . import field as fl

    state, seed, out = job
    sc, zones = capture_scene(state, seed)
    rgb, ndvi = fl.render(sc)
    Path(out).mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb).save(Path(out) / "rgb.jpg", quality=95)
    Image.fromarray(fl.ndvi_quantize(ndvi)).save(Path(out) / "ndvi.png", optimize=False, compress_level=6)
    info = {"state": state, "seed": seed, "width": BASE_W, "height": BASE_H, "res_px_m": RES, "zones": zones}
    (Path(out) / "info.json").write_text(json.dumps(info, indent=1))
    return out


def render_pool(media: Path, per_state: int, workers: int, seed: int = 20261103) -> None:
    import multiprocessing as mp

    jobs = [(state, seed + 97 * i + 1000 * s, str(media / "crop" / state / f"{i:02d}"))
            for s, state in enumerate(STATES) for i in range(per_state)]
    with mp.get_context("fork").Pool(min(workers, len(jobs))) as pool:
        for done in pool.imap_unordered(render_capture, jobs):
            print(f"crop capture rendered: {done}", flush=True)
    manifest = {"states": {s: per_state for s in STATES}, "base": [BASE_W, BASE_H], "out": [OUT_W, OUT_H],
                "res_px_m": RES, "generated_at": datetime.now(timezone.utc).isoformat()}
    (media / "crop" / "manifest.json").write_text(json.dumps(manifest, indent=1))


# ---------------------------------------------------------------- runtime builder (numpy + PIL encode only)

@dataclass
class CropObject:
    payload: bytes
    sha256: bytes
    meta: dict
    thumb_rgb: bytes
    thumb_ndvi: bytes
    build_s: float


def restamp(obj: CropObject, **fields: object) -> CropObject:
    """`obj` with `fields` merged into its meta.json: new payload (same size) and SHA-256, same images."""

    meta = {**obj.meta, **fields}
    if meta == obj.meta:
        return obj
    payload = P.replace_meta(obj.payload, meta)
    return dataclasses.replace(obj, payload=payload, sha256=hashlib.sha256(payload).digest(), meta=meta)


def _jpeg(image: Image.Image, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def bands_from(rgb: np.ndarray, ndvi_q: np.ndarray) -> bytes:
    """Five uint8 band planes (band-sequential) consistent with the RGB and NDVI of the same pixels.

    Digital numbers are reflectance / 0.6 * 255, so NDVI recomputed from the red and NIR planes matches ndvi.jpg."""

    scale = np.float32(255.0 / 0.6)
    red = rgb[..., 0].astype(np.float32) * np.float32(0.25 / 255.0)
    n = np.clip(ndvi_q.astype(np.float32) * np.float32(2.0 / 255.0) - 1.0, -0.95, 0.95)
    nir = np.clip(red * (1.0 + n) / (1.0 - n), 0.0, 0.6)
    red_edge = red + np.float32(0.45) * (nir - red)
    planes = [rgb[..., 2].astype(np.float32) * np.float32(0.20 / 255.0), rgb[..., 1].astype(np.float32) *
              np.float32(0.25 / 255.0), red, red_edge, nir]
    return b"".join(np.clip(p * scale + 0.5, 0, 255).astype(np.uint8).tobytes() for p in planes)


class CropFactory:
    def __init__(self, media: Path, seed: int | None = None, object_bytes: int = P.CROP_OBJECT_BYTES) -> None:
        from .field import ndvi_lut

        self.media = Path(media)
        self.rng = random.Random(seed)
        self.object_bytes = object_bytes
        self.lut = ndvi_lut()
        self.pool: dict[str, list[dict]] = {}
        self.counter = 0
        self.lock = threading.Lock()

    def entries(self, state: str) -> list[dict]:
        if state not in STATES:
            raise ValueError(f"unknown crop state {state!r}")
        if state not in self.pool:
            entries = []
            for folder in sorted((self.media / "crop" / state).glob("*/")):
                if not (folder / "rgb.jpg").exists():
                    continue
                info = json.loads((folder / "info.json").read_text())
                rgb = np.asarray(Image.open(folder / "rgb.jpg").convert("RGB"))
                ndvi_q = np.asarray(Image.open(folder / "ndvi.png").convert("L"))
                entries.append({"rgb": rgb, "ndvi_q": ndvi_q, "info": info, "name": folder.name})
            if not entries:
                raise FileNotFoundError(f"no crop captures for {state} under {self.media / 'crop'}"
                                        " (run: sf_content.py render)")
            self.pool[state] = entries
        return self.pool[state]

    def build(self, state: str, object_id: int | None = None) -> CropObject:
        started = time.monotonic()
        entries = self.entries(state)
        with self.lock:
            self.counter += 1
            number = self.counter
        entry = entries[self.rng.randrange(len(entries))]
        base_h, base_w = entry["rgb"].shape[:2]
        out_w, out_h = min(OUT_W, base_w), min(OUT_H, base_h)
        ox, oy = self.rng.randint(0, base_w - out_w), self.rng.randint(0, base_h - out_h)
        gain = self.rng.uniform(0.96, 1.04)
        rgb = entry["rgb"][oy:oy + out_h, ox:ox + out_w]
        rgb = np.clip(rgb.astype(np.float32) * np.float32(gain) + 0.5, 0, 255).astype(np.uint8)
        ndvi_q = np.ascontiguousarray(entry["ndvi_q"][oy:oy + out_h, ox:ox + out_w])
        rgb_image = Image.fromarray(rgb)
        ndvi_image = Image.fromarray(self.lut[ndvi_q])
        ndvi_values = ndvi_q.astype(np.float32) / 127.5 - 1.0
        vegetated = ndvi_values[ndvi_values > 0.2]
        zones = []
        for zone in entry["info"]["zones"]:
            x0, y0, x1, y1 = zone["box"]
            cx0, cy0, cx1, cy1 = max(x0 - ox, 0), max(y0 - oy, 0), min(x1 - ox, out_w), min(y1 - oy, out_h)
            if cx1 - cx0 < 0.3 * (x1 - x0) or cy1 - cy0 < 0.3 * (y1 - y0):
                continue  # mostly outside this framing
            zones.append({"x": round(cx0 / out_w, 4), "y": round(cy0 / out_h, 4), "w": round((cx1 - cx0) / out_w, 4),
                          "h": round((cy1 - cy0) / out_h, 4), "label": zone["label"], "severity": zone["severity"]})
        now = datetime.now(timezone.utc)
        meta = {"v": 1, "capture_id": f"{FIELD}-{now:%Y%m%dT%H%M%S}-{number:05d}", "field": FIELD, "state": state,
                "bands": BANDS, "width": out_w, "height": out_h,
                "ndvi_mean": round(float(vegetated.mean()) if vegetated.size else 0.0, 2), "zones": zones,
                "captured_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                # contract v1.1: stamped by the sender just before the object goes out (see restamp); null until
                # the run has sent bytes
                **{key: None for key in TCP_SO_FAR_KEYS}}
        quality = 90
        rgb_jpeg, ndvi_jpeg = _jpeg(rgb_image, quality), _jpeg(ndvi_image, 90)
        bands = bands_from(rgb, ndvi_q)
        while True:
            try:
                payload = P.build_sfim(meta, rgb_jpeg, ndvi_jpeg, bands, self.object_bytes)
                break
            except ValueError:
                if quality <= 60:
                    raise
                quality -= 8
                rgb_jpeg = _jpeg(rgb_image, quality)
        digest = hashlib.sha256(payload).digest()
        thumb_rgb = _jpeg(rgb_image.reduce(6), 82)
        thumb_ndvi = _jpeg(ndvi_image.reduce(6), 82)
        return CropObject(payload, digest, meta, thumb_rgb, thumb_ndvi, round(time.monotonic() - started, 3))
