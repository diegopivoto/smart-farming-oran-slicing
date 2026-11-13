"""Procedural top-down coffee plantation (numpy): hedge rows with shrub texture, red latosol between rows with weeds and
tyre tracks, farm roads, windbreak trees, cast shadows, cloud shadows, and stress overlays (water stress, pest damage).

Used offline (on PC1) to render the drone orthomosaic and the crop capture pool. Every noise field is FFT-filtered white
noise, hence periodic, so a scene whose width is the drone loop length tiles seamlessly along the flight direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

F = np.float32


def blur(a: np.ndarray, sigma_px: float) -> np.ndarray:
    """Periodic Gaussian blur through the FFT."""

    h, w = a.shape
    fy = np.fft.fftfreq(h).astype(F)[:, None]
    fx = np.fft.rfftfreq(w).astype(F)[None, :]
    g = np.exp(-2.0 * (np.pi * sigma_px) ** 2 * (fx * fx + fy * fy))
    return np.fft.irfft2(np.fft.rfft2(a) * g, s=(h, w)).astype(F)


def noise(shape: tuple[int, int], sigma_px: float, rng: np.random.Generator) -> np.ndarray:
    """Zero-mean, unit-variance smooth noise with correlation length ~sigma_px (periodic)."""

    n = blur(rng.standard_normal(shape, dtype=F), sigma_px)
    n -= n.mean()
    return (n / (n.std() + 1e-9)).astype(F)


def smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return (t * t * (3.0 - 2.0 * t)).astype(F)


def mix(a: np.ndarray, b: np.ndarray, t: np.ndarray) -> np.ndarray:
    return a + (b - a) * t[..., None]


def color(rgb: tuple[float, float, float], shape: tuple[int, int]) -> np.ndarray:
    return np.broadcast_to(np.asarray(rgb, dtype=F), (*shape, 3)).copy()


@dataclass
class Plot:
    mask: np.ndarray  # float [0,1] where this plot's rows exist
    angle_deg: float = 0.0  # row direction, from +X towards +Y
    spacing_m: float = 3.6
    width_m: float = 2.0
    plant_m: float = 0.75  # shrub spacing along the row
    curv_amp_m: float = 0.0  # contour planting
    curv_len_m: float = 60.0
    phase_m: float = 0.0
    vigor: float = 1.0


@dataclass
class Stress:
    mask: np.ndarray  # float [0,1] intensity
    kind: str  # "water" or "pest"


@dataclass
class Scene:
    height: int
    width: int
    res: float  # px per metre
    plots: list[Plot] = field(default_factory=list)
    road: np.ndarray | None = None  # float [0,1] dirt road
    road_tracks: np.ndarray | None = None  # float [0,1] tyre tracks on the road
    grass: np.ndarray | None = None  # float [0,1] grass strips (headlands, verges)
    trees: list[tuple[float, float, float]] = field(default_factory=list)  # (x_m, y_m, crown radius m)
    stress: list[Stress] = field(default_factory=list)
    sun_azimuth_deg: float = 225.0
    sun_elevation_deg: float = 48.0
    cloud_shadow: float = 0.0  # 0..1 strength
    exposure: float = 1.0
    detail: float = 1.0  # amplitude of the leaf/soil micro-texture (lower = smaller JPEG frames)
    seed: int = 0


def grids(scene: Scene) -> tuple[np.ndarray, np.ndarray]:
    ys = ((np.arange(scene.height, dtype=F) + 0.5) / scene.res)[:, None]
    xs = ((np.arange(scene.width, dtype=F) + 0.5) / scene.res)[None, :]
    return np.broadcast_to(xs, (scene.height, scene.width)), np.broadcast_to(ys, (scene.height, scene.width))


def render(scene: Scene) -> tuple[np.ndarray, np.ndarray]:
    """Return (rgb uint8 HxWx3, ndvi float32 HxW in [-1, 1])."""

    rng = np.random.default_rng(scene.seed)
    shape = (scene.height, scene.width)
    res = scene.res
    X, Y = grids(scene)

    n_low = noise(shape, 9.0 * res, rng)  # field-scale vigour / soil variation
    n_mid = noise(shape, 1.2 * res, rng)
    n_leaf = noise(shape, 0.11 * res, rng)  # leaf clumps
    n_fine = noise(shape, 0.035 * res, rng)  # leaves
    dt = F(scene.detail)
    n_grass = noise(shape, 0.9 * res, rng)
    n_gap = noise(shape, 2.5 * res, rng)

    # ---------------------------------------------------------------- ground: red latosol, weeds
    soil = color((0.55, 0.35, 0.26), shape)
    soil *= (1.0 + 0.07 * n_low + 0.06 * n_mid + dt * 0.05 * n_fine)[..., None]
    soil = mix(soil, color((0.64, 0.47, 0.36), shape), smoothstep(0.6, 1.8, n_low) * 0.5)  # dry, lighter soil
    weeds = smoothstep(0.1, 1.3, n_grass + 0.35 * n_low) * 0.75
    grass_col = color((0.40, 0.47, 0.20), shape) * (1.0 + dt * 0.10 * n_fine + 0.08 * n_mid)[..., None]
    grass_col = mix(grass_col, color((0.56, 0.54, 0.30), shape), smoothstep(0.3, 1.5, n_mid) * 0.5)  # dry grass
    ground = mix(soil, grass_col, weeds)
    ndvi = (0.10 + 0.03 * n_mid + weeds * 0.38).astype(F)

    if scene.grass is not None:
        ground = mix(ground, grass_col * F(1.04), scene.grass)
        ndvi = ndvi + (0.55 - ndvi) * scene.grass
    if scene.road is not None:
        dirt = color((0.70, 0.54, 0.42), shape) * (1.0 + 0.05 * n_mid + dt * 0.04 * n_fine)[..., None]
        if scene.road_tracks is not None:
            dirt = dirt * (1.0 - 0.13 * scene.road_tracks)[..., None]
        ground = mix(ground, dirt, scene.road)
        ndvi = ndvi + (0.08 - ndvi) * scene.road

    # ---------------------------------------------------------------- coffee hedgerows
    height = np.zeros(shape, F)
    canopy = np.zeros(shape, F)
    vigor = np.ones(shape, F)
    for plot in scene.plots:
        a = np.deg2rad(plot.angle_deg)
        u = X * F(np.cos(a)) + Y * F(np.sin(a))
        v = -X * F(np.sin(a)) + Y * F(np.cos(a)) + plot.phase_m
        if plot.curv_amp_m:
            v = v + F(plot.curv_amp_m) * np.sin(u * F(2 * np.pi / plot.curv_len_m))
        frac = v / F(plot.spacing_m)
        d = np.abs(frac - np.round(frac)) * F(plot.spacing_m)
        shrub = np.sin(u * F(2 * np.pi / plot.plant_m) + 1.7 * n_mid)
        hw = F(plot.width_m / 2) * (1.0 + 0.10 * n_mid + 0.045 * shrub + 0.05 * n_leaf)
        hw = hw * (1.0 - 0.7 * smoothstep(1.9, 2.5, n_gap))  # missing plants
        edge = np.clip((hw - d) / F(0.10) + 0.5 * dt * n_leaf, 0.0, 1.0)
        prof = np.sqrt(np.clip(1.0 - (d / np.maximum(hw, 1e-3)) ** 2, 0.0, 1.0))
        c = (edge * plot.mask).astype(F)
        height = np.maximum(height, c * prof * F(2.3))
        canopy = np.maximum(canopy, c)
        vigor = np.where(c > 0, F(plot.vigor), vigor)

    # tree crowns (windbreak)
    tree = np.zeros(shape, F)
    for tx, ty, tr in scene.trees:
        r2 = ((X - tx) ** 2 + (Y - ty) ** 2) / F(tr * tr)
        lump = 1.0 + 0.18 * n_mid + 0.10 * n_leaf
        crown = np.clip(lump - r2, 0.0, 1.0)
        height = np.maximum(height, np.sqrt(crown) * F(9.0))
        tree = np.maximum(tree, np.clip(crown * 6.0, 0.0, 1.0))

    leaf_col = color((0.15, 0.29, 0.10), shape)
    leaf_col = mix(leaf_col, color((0.23, 0.37, 0.13), shape), smoothstep(-0.5, 1.5, n_low) * 0.8)
    leaf_tex = (1.0 + dt * (0.22 * n_leaf + 0.14 * n_fine)).astype(F)
    leaf_col *= leaf_tex[..., None]
    ndvi_canopy = (0.80 + 0.05 * n_low + 0.03 * n_leaf).astype(F) * vigor

    # ---------------------------------------------------------------- stress overlays
    for st in scene.stress:
        m = st.mask
        if st.kind == "water":
            pale = color((0.56, 0.52, 0.26), shape) * leaf_tex[..., None]
            leaf_col = mix(leaf_col, pale, m * 0.92)
            canopy = canopy * (1.0 - 0.35 * m * smoothstep(-0.6, 0.6, n_leaf))
            height = height * (1.0 - 0.25 * m)
            ndvi_canopy = ndvi_canopy + (0.36 + 0.04 * n_leaf - ndvi_canopy) * m
        else:  # pest: brown/yellow leaf spots and defoliation
            spots = smoothstep(-0.9, 0.1, n_leaf + 0.6 * n_mid) * m
            brown = color((0.46, 0.30, 0.13), shape) * leaf_tex[..., None]
            yellow = color((0.70, 0.62, 0.22), shape) * leaf_tex[..., None]
            leaf_col = mix(leaf_col, mix(yellow, brown, smoothstep(-0.2, 0.8, n_fine)), spots)
            canopy = canopy * (1.0 - 0.7 * m * smoothstep(-0.2, 0.9, n_leaf + 0.5 * n_fine))
            ndvi_canopy = ndvi_canopy + (0.24 + 0.05 * n_fine - ndvi_canopy) * np.clip(spots * 1.2, 0, 1)

    tree_col = color((0.11, 0.22, 0.08), shape) * (1.0 + dt * (0.25 * n_leaf + 0.12 * n_fine))[..., None]
    leaf_col = mix(leaf_col, tree_col, tree)
    ndvi_canopy = ndvi_canopy + (0.84 - ndvi_canopy) * tree
    canopy = np.maximum(canopy, tree)

    # ---------------------------------------------------------------- lighting
    az, el = np.deg2rad(scene.sun_azimuth_deg), np.deg2rad(scene.sun_elevation_deg)
    sun = np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)], dtype=F)
    bump = height + F(0.10) * dt * n_leaf * canopy
    gy, gx = np.gradient(bump, F(1.0 / res))
    norm = np.sqrt(gx * gx + gy * gy + 1.0)
    lambert = np.clip((-gx * sun[0] - gy * sun[1] + sun[2]) / norm, 0.0, 1.0)
    shade = (0.42 + 0.68 * lambert / max(float(sun[2]), 0.3)).astype(F)

    tan_el = F(np.tan(el))
    shadow = np.zeros(shape, F)
    for dist in (0.15, 0.3, 0.5, 0.75, 1.0, 1.4, 1.9, 2.5, 3.2, 4.0, 5.0, 6.2, 7.6, 9.2, 11.0):
        sy, sx = int(round(-np.sin(az) * dist * res)), int(round(-np.cos(az) * dist * res))
        rolled = np.roll(height, (sy, sx), axis=(0, 1))
        shadow = np.maximum(shadow, np.clip((rolled - height - F(dist) * tan_el) / F(0.25), 0.0, 1.0))
    shadow = blur(shadow, 0.06 * res)

    rgb = mix(ground, leaf_col * shade[..., None], canopy)
    rgb *= (1.0 - 0.52 * shadow)[..., None]
    ndvi = ndvi + (ndvi_canopy - ndvi) * canopy + F(0.015) * n_fine

    if scene.cloud_shadow > 0:
        cloud = smoothstep(0.0, 1.4, noise(shape, 14.0 * res, rng))
        rgb *= (1.0 - F(scene.cloud_shadow) * cloud)[..., None]

    # ---------------------------------------------------------------- camera response
    rgb = np.clip(rgb * F(scene.exposure), 0.0, 1.0)
    rgb = rgb ** F(0.92)  # gentle lift of the shadows
    rgb[..., 0] *= F(1.02)
    rgb[..., 2] *= F(0.96)
    out = np.clip(rgb * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return out, np.clip(ndvi, -1.0, 1.0).astype(F)


def blob(scene: Scene, cx_m: float, cy_m: float, rx_m: float, ry_m: float, rng: np.random.Generator,
         wobble: float = 0.25, soft_m: float = 1.5) -> np.ndarray:
    """Irregular soft ellipse in [0, 1]."""

    X, Y = grids(scene)
    r = np.sqrt(((X - cx_m) / rx_m) ** 2 + ((Y - cy_m) / ry_m) ** 2)
    n = noise((scene.height, scene.width), 3.0 * scene.res, rng)
    edge = soft_m / max(rx_m, ry_m)
    return smoothstep(1.0 + edge, 1.0 - edge, r + F(wobble) * n * smoothstep(0.3, 1.0, r))


def bbox(mask: np.ndarray, threshold: float = 0.35) -> tuple[int, int, int, int] | None:
    """(x0, y0, x1, y1) pixels of mask > threshold, or None."""

    on = mask > threshold
    rows, cols = np.any(on, axis=1), np.any(on, axis=0)
    if not rows.any():
        return None
    y0, y1 = np.argmax(rows), len(rows) - np.argmax(rows[::-1])
    x0, x1 = np.argmax(cols), len(cols) - np.argmax(cols[::-1])
    return int(x0), int(y0), int(x1), int(y1)


NDVI_STOPS = ((-1.0, (165, 0, 38)), (0.05, (215, 48, 39)), (0.20, (244, 109, 67)), (0.32, (253, 174, 97)),
              (0.45, (254, 224, 139)), (0.58, (217, 239, 139)), (0.68, (166, 217, 106)), (0.78, (102, 189, 99)),
              (0.86, (26, 152, 80)), (1.0, (0, 104, 55)))


def ndvi_lut() -> np.ndarray:
    """256-entry red-yellow-green ramp for the NDVI false-colour map (index q -> NDVI q/255*2-1)."""

    values = np.arange(256, dtype=np.float64) / 255.0 * 2.0 - 1.0
    xs = [s[0] for s in NDVI_STOPS]
    return np.stack([np.interp(values, xs, [s[1][c] for s in NDVI_STOPS]) for c in range(3)], axis=1).astype(np.uint8)


def ndvi_quantize(ndvi: np.ndarray) -> np.ndarray:
    return np.clip(np.round((ndvi + 1.0) * 127.5), 0, 255).astype(np.uint8)
