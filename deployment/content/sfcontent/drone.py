"""Drone video: offline pre-rendering of two seamless MJPEG loops (`normal`, `alert`) and the runtime frame source.

Rendering (PC1): a procedural coffee plantation orthomosaic, periodic along the flight direction with period equal to
the distance flown in one loop, is sampled by a nadir camera (PIL affine transform: forward flight, a gentle S-turn
of +-12 deg heading, +-3 m altitude change), then a HUD is drawn (altitude, speed, heading tape, battery, GPS). The
`alert` variant adds two stress areas to the orthomosaic and highlights them ("Anomaly detected"). Both variants share
the camera path and their first/last frames show no stress area, so switching at the loop boundary is seamless.

Output: media/drone/<variant>/000000.jpg ... (1920x1080, baseline JPEG) and media/drone/manifest.json.
"""

from __future__ import annotations

import io
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

VARIANTS = ("normal", "alert")
W, H = 1920, 1080
RES = 28.0  # orthomosaic px per metre
HFOV_DEG = 73.4
BASE_ALT = 46.0
DETAIL = 0.30  # micro-texture amplitude: keeps q90 frames within the 40 Mbit/s budget
BLUR_PX = 1.3  # optics/motion softness applied to the orthomosaic
MAX_FRAME_BYTES = 150_000  # 40 Mbit/s / 30 fps = 139 datagrams = 160 kB of JPEG per frame slot (was 165 kB at 45)
HOME_LAT, HOME_LON = -22.25612, -45.70285  # Sul de Minas (Inatel area)

_G: dict = {}  # per-worker globals (forked from the parent)


# ---------------------------------------------------------------- orthomosaic

def flight_plan(seconds: float, fps: int, speed: float) -> dict:
    n = int(round(seconds * fps))
    period_m = speed * seconds
    period_px = int(round(period_m * RES))
    period_m = period_px / RES
    t = np.arange(n) / fps
    phase = 2 * np.pi * t / seconds
    psi = 0.21 * np.sin(phase)  # heading offset (rad), S-turn
    fine = np.linspace(0, seconds, n * 20 + 1)
    psi_f = 0.21 * np.sin(2 * np.pi * fine / seconds)
    dx = np.cos(psi_f)
    dy = np.sin(psi_f)
    x_f = np.concatenate([[0], np.cumsum((dx[1:] + dx[:-1]) / 2)]) * (fine[1] - fine[0])
    y_f = np.concatenate([[0], np.cumsum((dy[1:] + dy[:-1]) / 2)]) * (fine[1] - fine[0])
    scale = period_m / x_f[-1]  # exactly one period per loop
    x = np.interp(t, fine, x_f) * scale
    y = np.interp(t, fine, y_f) * scale
    y -= (y.max() + y.min()) / 2
    alt = BASE_ALT + 3.0 * np.sin(phase + 0.8)
    vz = 3.0 * np.cos(phase + 0.8) * 2 * np.pi / seconds
    ground_speed = scale * np.ones(n)
    return {"n": n, "t": t, "x": x, "y": y, "psi": psi, "alt": alt, "vz": vz, "speed": ground_speed,
            "period_m": period_m, "period_px": period_px}


def ortho_layout(period_m: float, height_m: float) -> dict:
    yc = height_m / 2
    return {"road_x": 0.40 * period_m, "trees_x": 0.83 * period_m, "yc": yc,
            "stress": [  # (kind, cx, cy, rx, ry, label, ndvi)
                ("pest", 0.24 * period_m, yc - 7.0, 6.5, 5.0, "Pest hotspot", 0.29),
                ("water", 0.63 * period_m, yc + 6.0, 10.0, 7.0, "Water stress", 0.36)]}


def build_ortho(period_px: int, height_px: int, alert: bool, seed: int, detail: float = DETAIL) -> np.ndarray:
    from . import field as fl

    sc = fl.Scene(height_px, period_px, RES, seed=seed, detail=detail)
    X, Y = fl.grids(sc)
    pm, hm = period_px / RES, height_px / RES
    lay = ortho_layout(pm, hm)
    rx = lay["road_x"] + 2.0 * np.sin(Y / 13.0)
    d_road = np.abs(X - rx)
    sc.road = fl.smoothstep(3.2, 2.7, d_road)
    sc.road_tracks = fl.smoothstep(0.35, 0.1, np.abs(d_road - 0.95))
    tx = lay["trees_x"]
    d_trees = np.abs(X - tx)
    sc.grass = np.maximum(fl.smoothstep(5.8, 4.8, d_road) * (1 - sc.road), fl.smoothstep(6.0, 5.0, d_trees))
    plant = pm / round(pm / 0.75)
    mask_a = np.maximum(fl.smoothstep(6.2, 7.0, rx - X), fl.smoothstep(6.2, 7.0, X - tx))
    mask_b = fl.smoothstep(6.2, 7.0, X - rx) * fl.smoothstep(6.2, 7.0, tx - X)
    sc.plots = [fl.Plot(mask_a, 0.0, 3.6, 2.05, plant_m=plant, phase_m=1.8 - lay["yc"] % 3.6),
                fl.Plot(mask_b, 62.0, 3.4, 1.85, plant_m=0.72, curv_amp_m=2.2, curv_len_m=70.0)]
    rng = np.random.default_rng(seed + 1)
    for yy in np.arange(rng.uniform(0, 4), hm, 7.5):
        sc.trees.append((tx + float(rng.uniform(-1.2, 1.2)), float(yy + rng.uniform(-1.5, 1.5)),
                         float(rng.uniform(3.0, 4.4))))
    sc.sun_azimuth_deg, sc.sun_elevation_deg, sc.exposure = 215.0, 50.0, 1.02
    if alert:
        for kind, cx, cy, rxs, rys, _, _ in lay["stress"]:
            sc.stress.append(fl.Stress(fl.blob(sc, cx, cy, rxs, rys, rng, wobble=0.35, soft_m=2.0), kind))
    rgb, _ = fl.render(sc)
    return rgb


# ---------------------------------------------------------------- HUD

def _font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    names = (["DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"] if mono else
             ["Ubuntu-B.ttf" if bold else "Ubuntu-R.ttf", "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"])
    for name in names:
        for folder in ("/usr/share/fonts/truetype/ubuntu", "/usr/share/fonts/truetype/dejavu"):
            path = os.path.join(folder, name)
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def _panel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], alpha: int = 120) -> None:
    draw.rounded_rectangle(box, radius=14, fill=(8, 14, 12, alpha), outline=(255, 255, 255, 40), width=1)


WHITE = (245, 248, 246, 255)
DIM = (190, 205, 198, 255)
GREEN = (110, 231, 160, 255)
AMBER = (255, 184, 48, 255)
RED = (255, 76, 76, 255)


def draw_hud(frame: Image.Image, k: int, plan: dict, variant: str, boxes: list[dict], fps: int) -> Image.Image:
    f = _G.setdefault("fonts", {"s": _font(22), "m": _font(28), "b": _font(30, bold=True), "big": _font(44, bold=True),
                                "mono": _font(26, mono=True), "monob": _font(30, bold=True, mono=True),
                                "tiny": _font(18)})
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    t = plan["t"][k]
    heading = (90.0 + math.degrees(plan["psi"][k])) % 360
    alt, spd, vz = plan["alt"][k], plan["speed"][k], plan["vz"][k]
    xm, ym = plan["x"][k], plan["y"][k]
    lat = HOME_LAT - (ym + 18.0) / 111_320.0
    lon = HOME_LON + (xm + 120.0) / (111_320.0 * math.cos(math.radians(HOME_LAT)))
    battery = 78.0 - 1.5 * t / plan["t"][-1]
    blink = (k // (fps // 2)) % 2 == 0

    # top-left: identity / recording
    _panel(d, (28, 26, 560, 120))
    if blink:
        d.ellipse((50, 50, 72, 72), fill=RED)
    d.text((84, 44), "REC", font=f["b"], fill=WHITE)
    d.text((158, 46), "SF-DRONE-01  ·  FIELD-A", font=f["m"], fill=WHITE)
    d.text((52, 84), "Coffee block mapping  ·  1080p30 MJPEG  ·  UN3GPP uplink", font=f["tiny"], fill=DIM)

    # top-right: battery, links
    _panel(d, (1424, 26, 1892, 120))
    bx, by = 1450, 48
    d.rounded_rectangle((bx, by, bx + 92, by + 42), radius=7, outline=WHITE, width=3)
    d.rectangle((bx + 92, by + 13, bx + 99, by + 29), fill=WHITE)
    level = int(84 * battery / 100)
    d.rounded_rectangle((bx + 4, by + 4, bx + 4 + level, by + 38), radius=4, fill=GREEN)
    d.text((bx + 116, by - 2), f"{battery:4.1f} %", font=f["b"], fill=WHITE)
    d.text((bx + 116, by + 34), f"{22.4 + battery * 0.028:4.1f} V", font=f["tiny"], fill=DIM)
    for i in range(4):  # link bars
        h = 10 + 8 * i
        d.rectangle((1748 + 14 * i, 86 - h, 1757 + 14 * i, 86), fill=WHITE if i < 4 else DIM)
    d.text((1812, 50), "5G", font=f["b"], fill=WHITE)
    d.text((1752, 92), "GNSS 17 sats", font=f["tiny"], fill=DIM)

    # heading tape
    cx, tw = W // 2, 640
    _panel(d, (cx - tw // 2, 26, cx + tw // 2, 104), alpha=95)
    px_per_deg = 6.0
    labels = {0: "N", 45: "NE", 90: "E", 135: "SE", 180: "S", 225: "SW", 270: "W", 315: "NW"}
    start = int(heading - tw / 2 / px_per_deg) - 1
    for deg in range(start, start + int(tw / px_per_deg) + 3):
        x = cx + (deg - heading) * px_per_deg
        if not cx - tw // 2 + 12 < x < cx + tw // 2 - 12:
            continue
        dn = deg % 360
        if dn % 5 == 0:
            tall = 14 if dn % 15 == 0 else 7
            d.line((x, 104 - tall, x, 104), fill=WHITE if dn % 15 == 0 else DIM, width=2)
        if dn in labels:
            txt = labels[dn]
            wtxt = d.textlength(txt, font=f["s"])
            d.text((x - wtxt / 2, 38), txt, font=f["s"], fill=GREEN if txt == "E" else WHITE)
    d.polygon([(cx - 10, 112), (cx + 10, 112), (cx, 100)], fill=GREEN)
    hdg = f"{heading:05.1f}°"
    d.rounded_rectangle((cx - 62, 116, cx + 62, 152), radius=8, fill=(8, 14, 12, 150))
    d.text((cx - d.textlength(hdg, font=f["mono"]) / 2, 119), hdg, font=f["mono"], fill=WHITE)

    # reticle
    rc = (W // 2, H // 2)
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        d.line((rc[0] + dx * 18, rc[1] + dy * 18, rc[0] + dx * 46, rc[1] + dy * 46), fill=(255, 255, 255, 170), width=2)
    d.ellipse((rc[0] - 4, rc[1] - 4, rc[0] + 4, rc[1] + 4), outline=(255, 255, 255, 170), width=2)

    # bottom-left: flight data
    _panel(d, (28, 904, 640, 1054))
    rows = [("ALT", f"{alt:5.1f} m AGL"), ("SPD", f"{spd:4.1f} m/s  ({spd * 3.6:4.1f} km/h)"),
            ("V/S", f"{vz:+4.1f} m/s"), ("GSD", f"{2 * alt * math.tan(math.radians(HFOV_DEG / 2)) / W * 100:4.2f} cm/px")]
    for i, (key, value) in enumerate(rows):
        d.text((54, 918 + i * 33), key, font=f["s"], fill=DIM)
        d.text((122, 914 + i * 33), value, font=f["mono"], fill=WHITE)

    # bottom-right: GPS and status
    _panel(d, (1280, 904, 1892, 1054))
    d.text((1306, 918), "LAT", font=f["s"], fill=DIM)
    d.text((1376, 914), f"{lat:11.6f}°", font=f["mono"], fill=WHITE)
    d.text((1306, 951), "LON", font=f["s"], fill=DIM)
    d.text((1376, 947), f"{lon:11.6f}°", font=f["mono"], fill=WHITE)
    if variant == "alert":
        status, colour = f"ALERT  ·  {len(plan['zones'])} anomalies flagged on this pass", AMBER
    else:
        status, colour = "NOMINAL  ·  canopy NDVI 0.78  ·  no anomaly", GREEN
    d.ellipse((1306, 1000, 1324, 1018), fill=colour)
    d.text((1336, 994), status, font=f["s"], fill=colour)

    # anomaly boxes
    visible = [b for b in boxes if b["visible"]]
    for b in visible:
        x0, y0, x1, y1 = b["box"]
        c = 34
        for (ax, ay, sx, sy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
            d.line((ax, ay, ax + sx * c, ay), fill=AMBER, width=5)
            d.line((ax, ay, ax, ay + sy * c), fill=AMBER, width=5)
        d.rectangle((x0, y0, x1, y1), outline=(255, 184, 48, 90), width=2)
        label = f"{b['label']}  ·  NDVI {b['ndvi']:.2f}  ·  {b['area_m2']:.0f} m²"
        lw = d.textlength(label, font=f["s"])
        ly = y0 - 44 if y0 > 170 else y1 + 10
        d.rounded_rectangle((x0, ly, x0 + lw + 28, ly + 36), radius=8, fill=(30, 20, 0, 190))
        d.text((x0 + 14, ly + 5), label, font=f["s"], fill=AMBER)
    if visible:
        text = "ANOMALY DETECTED"
        tw2 = d.textlength(text, font=f["big"])
        alpha = 235 if blink else 175
        d.rounded_rectangle((cx - tw2 / 2 - 70, 176, cx + tw2 / 2 + 30, 244), radius=14, fill=(160, 40, 20, alpha))
        d.polygon([(cx - tw2 / 2 - 50, 228), (cx - tw2 / 2 - 22, 228), (cx - tw2 / 2 - 36, 194)], outline=WHITE,
                  width=3)
        d.text((cx - tw2 / 2 - 39, 205), "!", font=f["tiny"], fill=WHITE)
        d.text((cx - tw2 / 2 + 2, 184), text, font=f["big"], fill=WHITE)
    out = frame.convert("RGBA")
    out.alpha_composite(overlay)
    return out.convert("RGB")


# ---------------------------------------------------------------- frames

def camera_affine(k: int, plan: dict, margin_px: int) -> tuple[float, ...]:
    psi = float(plan["psi"][k])
    scale = 2 * plan["alt"][k] * math.tan(math.radians(HFOV_DEG / 2)) * RES / W
    cc = plan["x"][k] * RES + margin_px
    cr = plan["y"][k] * RES + _G["height_px"] / 2
    a, b = -scale * math.sin(psi), -scale * math.cos(psi)
    d, e = scale * math.cos(psi), -scale * math.sin(psi)
    return (a, b, cc - a * W / 2 - b * H / 2, d, e, cr - d * W / 2 - e * H / 2)


def zone_boxes(k: int, plan: dict) -> list[dict]:
    a, b, c, d, e, f = camera_affine(k, plan, 0)
    det = a * e - b * d
    out = []
    period = plan["period_px"]
    for kind, cx, cy, rx, ry, label, ndvi in plan["zones"]:
        cam_col = plan["x"][k] * RES
        center = cam_col + ((cx * RES - cam_col + period / 2) % period) - period / 2  # nearest periodic copy
        pts = []
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            col = center + sx * rx * 1.15 * RES
            row = (cy + sy * ry * 1.15) * RES
            u, v = col - c, row - f
            pts.append(((e * u - b * v) / det, (-d * u + a * v) / det))
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        box = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
        mx, my = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        visible = 120 < mx < W - 120 and 200 < my < H - 220
        out.append({"kind": kind, "label": label, "ndvi": ndvi, "area_m2": math.pi * rx * ry, "visible": visible,
                    "box": (max(box[0], 20), max(box[1], 20), min(box[2], W - 20), min(box[3], H - 20))})
    return out


def _vignette() -> Image.Image:
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    r2 = ((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2
    v = np.clip(1.0 - 0.16 * r2 ** 1.4, 0, 1) * 255
    return Image.fromarray(v.astype(np.uint8)).convert("RGB")


def render_frame(job: tuple[str, int]) -> tuple[str, int, int]:
    variant, k = job
    plan = _G["plan"]
    image = _G["ortho"][variant].transform((W, H), Image.AFFINE, camera_affine(k, plan, _G["margin"]),
                                           resample=Image.BILINEAR)
    image = ImageChops.multiply(image, _G["vignette"])
    boxes = zone_boxes(k, plan) if variant == "alert" else []
    image = draw_hud(image, k, plan, variant, boxes, _G["fps"])
    data, quality = encode_within(image, _G["quality"], _G["max_bytes"])
    path = Path(_G["out"]) / variant / f"{k:06d}.jpg"
    path.write_bytes(data)
    return variant, k, len(data), quality


def encode_within(image: Image.Image, quality: int, max_bytes: int, floor: int = 40) -> tuple[bytes, int]:
    """Baseline JPEG at the highest quality <= `quality` whose size fits `max_bytes` (binary search)."""

    def encode(q: int) -> bytes:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=q, optimize=True, progressive=False)
        return buffer.getvalue()

    data = encode(quality)
    if len(data) <= max_bytes:
        return data, quality
    lo, hi, best = floor, quality - 1, (encode(floor), floor)
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = encode(mid)
        if len(candidate) <= max_bytes:
            best, lo = (candidate, mid), mid + 1
        else:
            hi = mid - 1
    return best


def render_loops(media: Path, workers: int, seconds: float = 24.0, fps: int = 30, quality: int = 90,
                 speed: float = 6.5, seed: int = 4242, frames: int | None = None,
                 max_bytes: int = MAX_FRAME_BYTES) -> dict:
    import multiprocessing as mp

    out = media / "drone"
    plan = flight_plan(seconds, fps, speed)
    height_px = int(round(104 * RES))
    margin = int(46 * RES)
    lay = ortho_layout(plan["period_m"], height_px / RES)
    plan["zones"] = lay["stress"]
    for variant in VARIANTS:
        (out / variant).mkdir(parents=True, exist_ok=True)
        for old in (out / variant).glob("*.jpg"):
            old.unlink()
    with mp.get_context("fork").Pool(2) as pool:
        orthos = pool.starmap(build_ortho, [(plan["period_px"], height_px, v == "alert", seed) for v in VARIANTS])
    _G.update({"plan": plan, "height_px": height_px, "margin": margin, "fps": fps, "quality": quality,
               "max_bytes": max_bytes, "out": str(out), "vignette": _vignette(),
               "ortho": {v: Image.fromarray(np.pad(o, ((0, 0), (margin, margin), (0, 0)), mode="wrap"))
                         .filter(ImageFilter.GaussianBlur(BLUR_PX)) for v, o in zip(VARIANTS, orthos)}})
    Image.fromarray(orthos[1]).reduce(4).save(out / "orthomosaic_alert_preview.jpg", quality=85)
    del orthos
    n = plan["n"] if frames is None else min(frames, plan["n"])
    jobs = [(v, k) for v in VARIANTS for k in range(n)]
    sizes: dict[str, list[int]] = {v: [0] * n for v in VARIANTS}
    qualities: dict[str, list[int]] = {v: [0] * n for v in VARIANTS}
    with mp.get_context("fork").Pool(workers) as pool:
        for i, (variant, k, size, q) in enumerate(pool.imap_unordered(render_frame, jobs, chunksize=8)):
            sizes[variant][k], qualities[variant][k] = size, q
            if i % 120 == 0:
                print(f"drone frames rendered: {i}/{len(jobs)}", flush=True)
    stats = {v: {"frames": n, "bytes_total": sum(s), "bytes_mean": round(sum(s) / n), "bytes_min": min(s),
                 "bytes_max": max(s), "mbit_s_at_fps": round(sum(s) / n * fps * 8 / 1e6, 2),
                 "quality_mean": round(sum(qualities[v]) / n, 1), "quality_min": min(qualities[v]),
                 "quality_max": max(qualities[v])} for v, s in sizes.items()}
    manifest = {"v": 1, "fps": fps, "frames": n, "width": W, "height": H, "quality": quality, "seconds": n / fps,
                "max_frame_bytes": max_bytes, "variants": list(VARIANTS), "stats": stats, "speed_m_s": speed, "altitude_m": BASE_ALT,
                "period_m": plan["period_m"], "generated_at": datetime.now(timezone.utc).isoformat()}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


# ---------------------------------------------------------------- runtime

class DroneFrames:
    """Frame files of the pre-rendered loops (read on demand; the page cache keeps them hot)."""

    def __init__(self, media: Path) -> None:
        self.root = Path(media) / "drone"
        manifest_path = self.root / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"{manifest_path} missing (run: sf_content.py render)")
        self.manifest = json.loads(manifest_path.read_text())
        self.fps = int(self.manifest["fps"])
        self.count = int(self.manifest["frames"])
        for variant in VARIANTS:
            if not (self.root / variant / f"{self.count - 1:06d}.jpg").exists():
                raise FileNotFoundError(f"drone loop {variant} incomplete under {self.root}")

    def path(self, variant: str, index: int) -> Path:
        return self.root / variant / f"{index % self.count:06d}.jpg"

    def read(self, variant: str, index: int) -> bytes:
        return self.path(variant, index).read_bytes()

    def thumbnail(self, variant: str, index: int, width: int = 480) -> bytes:
        image = Image.open(self.path(variant, index))
        image.draft("RGB", (width, width * 9 // 16))
        image = image.convert("RGB")
        if image.width != width:
            image = image.resize((width, width * image.height // image.width), Image.BILINEAR)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=80)
        return buffer.getvalue()
