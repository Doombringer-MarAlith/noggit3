#!/usr/bin/env python3
"""
terrain_fields.py - per-tile terrain/water/texture field cache for generate_adt.py

Everything the ADT writer needs for one tile (MCVT heights, MCNR normals, MH2O
surfaces and depths, MCAL alpha masks, the low-quality texture map, WDL MARE
samples and the minimap) is derived from ONE deterministic field grid computed
here, so all of those outputs agree with each other and with the neighbouring
tiles (the grid samples exact world positions, and heights are a pure function
of world position, so shared tile borders are bit-identical).

Grid layout: samples every half UNITSIZE (UNIT/2 = 2.0833 yards). A tile is
128 units wide, so 257 samples per axis cover it; a margin of extra samples on
each side supports gradients/normals at the tile border.

    grid index g -> local offset (g - margin) * HALF_UNIT from the tile origin
    world position  = tile_origin - offset          (WoW axes decrease with index)

Vertex mapping (noggit/MCVT order): outer vertex (col, row) of chunk (cx, cy)
sits at grid (16*cx + 2*col, 16*cy + 2*row); inner vertices at +1/+1.
Alpha texel (ax, ay) of a chunk sits at grid coordinate 16*cx + (ax + 0.5)/4.

Terrain synthesis (all deterministic from cfg['seed'] and the zone spec):
  1. macro relief: layered value-noise (fBm) around the spec's base elevation,
     plus spec features (ridge / hill / plateau / valley / basin / cliff_band
     / coast) with gaussian or plateau profiles.
  2. rivers / lakes / ocean edges carve a channel below their (optionally
     sloping) water surface with parabolic beds and smooth banks; the water
     surface height is stored per sample so MH2O can be sloped and per-vertex
     depth/opacity bytes can fade at the shore.
  3. roads are cut/filled to a low-passed profile height along their path.
  4. settlements are flattened toward the natural height at their centre.
  5. micro relief: small noise scaled by slope, suppressed on roads, water
     beds and settlement pads.
Texture role weights (base/forest/shore/sand/rock/road/snow/plague) are
computed per grid sample from height, slope, water/road/settlement distance
and low-frequency noise, then bilinearly upsampled to 64x64 alpha texels with
a small deterministic breakup noise for organic edges.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

ADT_SIZE = 533.3333333333334
CHUNK_SIZE = ADT_SIZE / 16.0
UNIT = CHUNK_SIZE / 8.0
HALF_UNIT = UNIT / 2.0
WORLD_SIZE = ADT_SIZE * 64.0
WORLD_HALF = WORLD_SIZE / 2.0
GRID_N = 257          # samples across one tile (inclusive of both edges)
MARGIN = 2            # extra samples outside the tile on each side
GRID_TOTAL = GRID_N + 2 * MARGIN

ROLE_ORDER = ['base', 'forest', 'sand', 'shore', 'plague', 'snow', 'rock', 'road']


# -----------------------------------------------------------------------------
# Deterministic noise (stdlib only)
# -----------------------------------------------------------------------------

def _hash01(ix: int, iy: int, seed: int) -> float:
    h = (ix * 374761393 + iy * 668265263 + seed * 1442695041) & 0xFFFFFFFF
    h = (h ^ (h >> 13)) * 1274126177 & 0xFFFFFFFF
    h ^= h >> 16
    return (h & 0xFFFFFF) / 16777216.0


_GRAD = [(math.cos(a), math.sin(a)) for a in [k * math.pi / 8.0 for k in range(16)]]


def value_noise(x: float, y: float, seed: int) -> float:
    """Gradient (Perlin-style) noise in about [-1, 1].

    Gradient noise has no bulges centred on lattice points, so hills do not
    line up with the sample grid the way plain value noise does.
    """
    ix = math.floor(x); iy = math.floor(y)
    fx = x - ix; fy = y - iy
    # quintic fade for C2-continuous interpolation
    ux = fx * fx * fx * (fx * (fx * 6.0 - 15.0) + 10.0)
    uy = fy * fy * fy * (fy * (fy * 6.0 - 15.0) + 10.0)
    g = _GRAD
    ga = g[int(_hash01(ix, iy, seed) * 16.0) & 15]
    gb = g[int(_hash01(ix + 1, iy, seed) * 16.0) & 15]
    gc = g[int(_hash01(ix, iy + 1, seed) * 16.0) & 15]
    gd = g[int(_hash01(ix + 1, iy + 1, seed) * 16.0) & 15]
    a = ga[0] * fx + ga[1] * fy
    b = gb[0] * (fx - 1.0) + gb[1] * fy
    c = gc[0] * fx + gc[1] * (fy - 1.0)
    d = gd[0] * (fx - 1.0) + gd[1] * (fy - 1.0)
    top = a + (b - a) * ux
    bot = c + (d - c) * ux
    return (top + (bot - top) * uy) * 1.9


_ROT_C = math.cos(0.65); _ROT_S = math.sin(0.65)


def fbm(x: float, y: float, seed: int, octaves: int = 4, lacunarity: float = 2.03, gain: float = 0.5) -> float:
    """Fractal Brownian motion in roughly [-1, 1]."""
    amp = 1.0; total = 0.0; norm = 0.0
    for o in range(octaves):
        total += value_noise(x, y, seed + o * 101) * amp
        norm += amp
        amp *= gain
        x *= lacunarity; y *= lacunarity
        # rotate ~37 degrees per octave so lattice directions never line up
        x, y = x * _ROT_C - y * _ROT_S, x * _ROT_S + y * _ROT_C
    return total / norm


def ridged(x: float, y: float, seed: int, octaves: int = 3) -> float:
    """Ridged multifractal in [0, 1] (sharp crests)."""
    amp = 1.0; total = 0.0; norm = 0.0
    for o in range(octaves):
        n = 1.0 - abs(value_noise(x, y, seed + o * 131))
        total += n * n * amp
        norm += amp
        amp *= 0.5
        x *= 2.1; y *= 2.1
    return total / norm


def smoothstep(e0: float, e1: float, x: float) -> float:
    if e0 == e1:
        return 1.0 if x >= e1 else 0.0
    t = (x - e0) / (e1 - e0)
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return t * t * (3.0 - 2.0 * t)


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


# -----------------------------------------------------------------------------
# Geometry helpers in spec-local ADT units
# -----------------------------------------------------------------------------

def project_on_path(px: float, py: float, path: Sequence[Sequence[float]]) -> Tuple[float, float]:
    """Return (distance, arc-length fraction 0..1) of the closest point on a polyline."""
    if not path:
        return 1e9, 0.0
    if len(path) == 1:
        return math.hypot(px - float(path[0][0]), py - float(path[0][1])), 0.0
    seg_len: List[float] = []
    total = 0.0
    for i in range(len(path) - 1):
        l = math.hypot(float(path[i + 1][0]) - float(path[i][0]), float(path[i + 1][1]) - float(path[i][1]))
        seg_len.append(l)
        total += l
    best = 1e9; best_s = 0.0; acc = 0.0
    for i in range(len(path) - 1):
        ax, ay = float(path[i][0]), float(path[i][1])
        bx, by = float(path[i + 1][0]), float(path[i + 1][1])
        vx, vy = bx - ax, by - ay
        denom = vx * vx + vy * vy
        if denom <= 1e-12:
            t = 0.0
        else:
            t = ((px - ax) * vx + (py - ay) * vy) / denom
            t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
        dx = px - (ax + t * vx); dy = py - (ay + t * vy)
        d = math.hypot(dx, dy)
        if d < best:
            best = d
            best_s = (acc + t * seg_len[i]) / total if total > 1e-9 else 0.0
        acc += seg_len[i]
    return best, best_s


def smooth_path(path: Sequence[Sequence[float]], subdiv: int = 6) -> List[List[float]]:
    """Resample a polyline through a Catmull-Rom spline so roads/rivers curve
    naturally instead of showing straight segments and sharp corners."""
    pts = [[float(p[0]), float(p[1])] for p in path]
    if len(pts) < 3:
        return pts
    out: List[List[float]] = []
    n = len(pts)
    for i in range(n - 1):
        p0 = pts[max(0, i - 1)]; p1 = pts[i]; p2 = pts[i + 1]; p3 = pts[min(n - 1, i + 2)]
        for k in range(subdiv):
            t = k / subdiv
            t2 = t * t; t3 = t2 * t
            x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            out.append([x, y])
    out.append(pts[-1])
    return out


def point_on_path(path: Sequence[Sequence[float]], s: float) -> Tuple[float, float]:
    """Point at arc-length fraction s along a polyline."""
    if len(path) == 1:
        return float(path[0][0]), float(path[0][1])
    total = 0.0
    lens = []
    for i in range(len(path) - 1):
        l = math.hypot(float(path[i + 1][0]) - float(path[i][0]), float(path[i + 1][1]) - float(path[i][1]))
        lens.append(l); total += l
    target = clamp(s, 0.0, 1.0) * total
    acc = 0.0
    for i, l in enumerate(lens):
        if acc + l >= target or i == len(lens) - 1:
            t = (target - acc) / l if l > 1e-9 else 0.0
            t = clamp(t, 0.0, 1.0)
            return (float(path[i][0]) + t * (float(path[i + 1][0]) - float(path[i][0])),
                    float(path[i][1]) + t * (float(path[i + 1][1]) - float(path[i][1])))
        acc += l
    return float(path[-1][0]), float(path[-1][1])


# -----------------------------------------------------------------------------
# Zone spec normalisation
# -----------------------------------------------------------------------------

class ZoneModel:
    """Spec-derived, tile-independent description of the terrain (built once per run)."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        spec = cfg.get('_zone_spec') or {}
        self.spec = spec
        tiles_x = list(cfg.get('tiles_x', [32]))
        tiles_y = list(cfg.get('tiles_y', [32]))
        ox, oy = spec.get('origin_tile', [min(tiles_x), min(tiles_y)])
        sx, sy = spec.get('size_adts', [len(tiles_x), len(tiles_y)])
        self.ox, self.oy, self.sx, self.sy = float(ox), float(oy), float(sx), float(sy)
        self.seed = int(cfg.get('seed', 1337))
        terrain = spec.get('terrain', {}) if spec else {}
        self.base = float(terrain.get('base_elevation', cfg.get('base_elevation', 42.0)))
        gn = terrain.get('global_noise', {}) if isinstance(terrain.get('global_noise', {}), dict) else {}
        self.large_wave = float(gn.get('large_wave_height', cfg.get('height_scale', 24.0)))
        self.medium_noise = float(gn.get('medium_noise_height', 7.0))
        self.micro_noise = float(gn.get('micro_noise_height', 1.2))
        # Path-based features get spline-smoothed so ridges, roads and rivers curve.
        self.features = []
        for f in (terrain.get('features', []) or []):
            f = dict(f)
            if f.get('path'):
                f['path'] = smooth_path(f['path'])
            self.features.append(f)
        self.settlements = list(terrain.get('settlement_flattening', []) or [])
        self.roads = []
        for r in (spec.get('roads', []) or []):
            r = dict(r)
            if r.get('path'):
                r['path'] = smooth_path(r['path'])
            self.roads.append(r)
        water = spec.get('water', {}) if spec else {}
        cfg_water = cfg.get('water', {})
        self.water_enabled = bool(cfg_water.get('enabled', True)) and bool(water.get('enabled', True) if spec else True)
        self.default_level = float(water.get('level', cfg_water.get('level', 38.0)))
        self.water_features = [dict(f) for f in (water.get('features', []) or [])]
        if not spec:
            # Legacy (no zone spec): a diagonal river across the tile block.
            self.water_features = [{'id': 'legacy_river', 'type': 'river',
                                    'path': [[0.0, 0.15 * self.sy], [self.sx, 0.85 * self.sy]],
                                    'width': float(cfg_water.get('river_width', 27.0)) / ADT_SIZE,
                                    'level': self.default_level, 'depth': 6.0}]
        self._prepare_water()
        self._road_profiles: Dict[int, List[float]] = {}
        self._settle_targets: Dict[int, float] = {}
        # Texture thresholds, optionally calibrated by learned rules.
        learned = cfg.get('_learned_rules') or {}
        hints = learned.get('generator_hints', {}) if isinstance(learned, dict) else {}
        # The learner reports the p25 of per-chunk mean slopes for chunks that carry
        # a rock layer, which underestimates the slope rock actually sits on; clamp
        # it so learned rules bias the threshold without flooding gentle hills.
        self.rock_slope = min(40.0, max(26.0, float(hints.get('rock_slope_threshold_degrees', 30.0))))
        theme = str(cfg.get('_texture_theme', 'forest'))
        if 'snowline' in terrain:
            self.snowline = float(terrain['snowline'])
        elif theme == 'snow':
            self.snowline = self.base - 40.0
        elif theme == 'northrend':
            self.snowline = self.base + 75.0
        elif theme in ('jungle', 'swamp', 'desert'):
            self.snowline = self.base + 190.0
        else:
            self.snowline = self.base + 120.0
        text = ' '.join(str(spec.get(k, '')) for k in ('creative_intent', 'object_intent', 'zone_name')).lower()
        self.plague_theme = theme == 'plague' or any(w in text for w in ('plague', 'undead', 'haunted', 'crypt'))
        self.sand_theme = theme in ('desert',)

    # -- water ---------------------------------------------------------------
    def _prepare_water(self):
        for f in self.water_features:
            f['_type'] = str(f.get('type', 'river')).lower()
            f['_width'] = float(f.get('width', f.get('radius', 0.12)))
            f['_depth'] = float(f.get('depth', 7.0 if f['_type'] == 'river' else 12.0 if f['_type'] == 'lake' else 20.0))
            lvl = float(f.get('level', self.default_level))
            f['_level0'] = lvl
            f['_level1'] = float(f.get('level_end', lvl))
            if 'path' in f and f.get('path'):
                f['_path'] = smooth_path(f['path'])
            else:
                cx, cy = f.get('center', [self.sx / 2, self.sy / 2])
                f['_center'] = (float(cx), float(cy))
        # Lakes/ponds touching a river take the river's surface height there so
        # the junction does not show two water planes at different heights.
        rivers = [f for f in self.water_features if '_path' in f and f['_type'] != 'ocean_edge']
        for f in self.water_features:
            if '_center' not in f:
                continue
            for r in rivers:
                d, s = project_on_path(f['_center'][0], f['_center'][1], r['_path'])
                if d <= f['_width'] + r['_width'] * 1.5:
                    lvl = r['_level0'] + (r['_level1'] - r['_level0']) * s
                    f['_level0'] = f['_level1'] = lvl
                    f['_snapped_to'] = r.get('id', 'river')
                    break

    def water_at(self, lx: float, ly: float) -> Tuple[Optional[dict], float, float]:
        """Nearest water feature: (feature, signed distance in yards from its edge, surface height)."""
        best = None; best_margin = 1e9; best_surface = 0.0
        for f in self.water_features:
            if '_path' in f:
                d, s = project_on_path(lx, ly, f['_path'])
                surface = f['_level0'] + (f['_level1'] - f['_level0']) * s
            else:
                d = math.hypot(lx - f['_center'][0], ly - f['_center'][1])
                surface = f['_level0']
            margin = (d - f['_width']) * ADT_SIZE
            if margin < best_margin:
                best_margin = margin; best = f; best_surface = surface
        return best, best_margin, best_surface

    # -- roads ---------------------------------------------------------------
    def road_at(self, lx: float, ly: float) -> Tuple[int, float, float, float]:
        """Nearest road: (index, distance yards, half width yards, arc fraction)."""
        best_i = -1; best_d = 1e9; best_w = 0.0; best_s = 0.0
        for i, r in enumerate(self.roads):
            path = r.get('path') or []
            if not path:
                continue
            d, s = project_on_path(lx, ly, path)
            if d < best_d:
                best_d = d; best_i = i; best_s = s
                best_w = float(r.get('width', 0.055)) * ADT_SIZE
        return best_i, best_d * ADT_SIZE, best_w, best_s

    def road_profile_height(self, i: int, s: float) -> float:
        """Low-passed natural terrain height along road i at arc fraction s."""
        prof = self._road_profiles.get(i)
        if prof is None:
            path = self.roads[i].get('path') or []
            n = 96
            raw = [self.natural_height_local(*point_on_path(path, k / (n - 1))) for k in range(n)]
            # moving average over ~8% of the road length, then a second pass for smoothness
            win = 4
            sm = raw[:]
            for _ in range(2):
                nxt = []
                for k in range(n):
                    lo = max(0, k - win); hi = min(n, k + win + 1)
                    nxt.append(sum(sm[lo:hi]) / (hi - lo))
                sm = nxt
            prof = sm
            self._road_profiles[i] = prof
        n = len(prof)
        fs = clamp(s, 0.0, 1.0) * (n - 1)
        k = int(fs); t = fs - k
        if k >= n - 1:
            return prof[-1]
        return prof[k] * (1.0 - t) + prof[k + 1] * t

    # -- settlements ----------------------------------------------------------
    def settlement_at(self, lx: float, ly: float) -> Tuple[int, float, float]:
        best_i = -1; best_d = 1e9; best_r = 0.0
        for i, s in enumerate(self.settlements):
            c = s.get('center', [self.sx / 2, self.sy / 2])
            r = float(s.get('radius', 0.3))
            d = math.hypot(lx - float(c[0]), ly - float(c[1]))
            if d - r < best_d - best_r:
                best_i = i; best_d = d; best_r = r
        return best_i, best_d * ADT_SIZE, best_r * ADT_SIZE

    def settlement_target(self, i: int) -> float:
        t = self._settle_targets.get(i)
        if t is None:
            c = self.settlements[i].get('center', [self.sx / 2, self.sy / 2])
            r = float(self.settlements[i].get('radius', 0.3))
            # average the natural height over a few points inside the pad
            pts = [(float(c[0]), float(c[1]))]
            for k in range(6):
                a = k / 6.0 * 2.0 * math.pi
                pts.append((float(c[0]) + math.cos(a) * r * 0.5, float(c[1]) + math.sin(a) * r * 0.5))
            t = sum(self.natural_height_local(px, py) for px, py in pts) / len(pts)
            t += float(self.settlements[i].get('height', 0.0)) * 0.25
            self._settle_targets[i] = t
        return t

    # -- height ----------------------------------------------------------------
    def natural_height_local(self, lx: float, ly: float) -> float:
        """Macro terrain (relief + features) without water/road/settlement edits, spec-local coords."""
        wx = WORLD_HALF - (self.ox + lx) * ADT_SIZE
        wy = WORLD_HALF - (self.oy + ly) * ADT_SIZE
        return self.macro_height(wx, wy, lx, ly)

    def macro_height(self, wx: float, wy: float, lx: float, ly: float) -> float:
        seed = self.seed
        h = self.base
        # broad rolling relief + medium detail
        h += fbm(wx / 760.0, wy / 760.0, seed, 3) * self.large_wave
        h += fbm(wx / 190.0, wy / 190.0, seed + 7, 4) * self.medium_noise
        for f in self.features:
            ftype = str(f.get('type', '')).lower()
            width = float(f.get('width', f.get('radius', 0.5)))
            if f.get('path'):
                d, _s = project_on_path(lx, ly, f['path'])
            else:
                cx, cy = f.get('center', [self.sx / 2, self.sy / 2])
                d = math.hypot(lx - float(cx), ly - float(cy))
            if d > width * 3.2:
                continue
            g = math.exp(-(d * d) / (2.0 * width * width))
            if ftype == 'ridge':
                # gaussian body with a ridged-noise crest so it reads as a mountain wall
                crest = 0.65 + 0.35 * ridged(wx / 140.0, wy / 140.0, seed + 31, 3)
                h += float(f.get('height', 25.0)) * g * crest
            elif ftype == 'hill':
                h += float(f.get('height', 25.0)) * g
            elif ftype == 'plateau':
                target = self.base + float(f.get('height', 8.0))
                t = 1.0 - smoothstep(width * 0.75, width * 1.35, d)
                h = h * (1.0 - 0.9 * t) + target * 0.9 * t
            elif ftype in ('valley', 'basin'):
                h -= float(f.get('depth', 20.0)) * g
            elif ftype == 'coast':
                h -= float(f.get('depth', 40.0)) * (1.0 - smoothstep(width * 0.4, width * 1.6, d))
            elif ftype == 'cliff_band':
                # a step: one side raised, sharp edge softened over a narrow band
                h += float(f.get('height', 40.0)) * (1.0 - smoothstep(width * 0.15, width * 0.55, d)) * 0.5 \
                     + float(f.get('height', 40.0)) * 0.5 * g
        return h

    def theme_role_weights(self, h: float, slope: float, road_t: float, water_margin: float,
                           h_above_water: Optional[float], water_type: str, settle_t: float,
                           wx: float, wy: float) -> Dict[str, float]:
        seed = self.seed
        # rock only where the macro terrain is genuinely steep (cliffs, ridge
        # crests, cut banks); a gentle threshold wobble keeps the edge organic
        rock_n = fbm(wx / 170.0, wy / 170.0, seed + 401, 2) * 3.0
        rock = smoothstep(self.rock_slope + 2.0 + rock_n, self.rock_slope + 16.0 + rock_n, slope)
        road = road_t
        # shoreline mud/sand: on and just above the water surface, near a water feature
        shore = 0.0
        if h_above_water is not None:
            shore = (1.0 - smoothstep(0.4, 6.0, h_above_water)) * (1.0 - smoothstep(4.0, 22.0, max(0.0, water_margin)))
        sand = 0.0
        if water_type in ('lake', 'ocean_edge') or self.sand_theme:
            sand = shore * (1.0 - smoothstep(12.0, 26.0, slope))
            shore *= 0.35
        forest_n = fbm(wx / 240.0, wy / 240.0, seed + 211, 3)
        forest = smoothstep(-0.02, 0.38, forest_n) * (1.0 - road) * (1.0 - shore) * (1.0 - sand) * (1.0 - settle_t) * (1.0 - rock) * (1.0 - smoothstep(24.0, 34.0, slope))
        snow_n = fbm(wx / 160.0, wy / 160.0, seed + 307, 2) * 9.0
        snow = smoothstep(self.snowline, self.snowline + 30.0, h + snow_n) * (1.0 - road * 0.75)
        plague = 0.0
        if self.plague_theme:
            n = fbm(wx / 330.0, wy / 330.0, seed + 509, 3)
            plague = smoothstep(-0.1, 0.45, n) * (1.0 - road) * (1.0 - shore) * 0.85
        base = max(0.08, 1.0 - max(road, rock * 0.8, shore * 0.8, sand * 0.8, snow * 0.85, plague * 0.7, forest * 0.4))
        return {'base': base, 'forest': forest, 'sand': sand, 'shore': shore, 'plague': plague,
                'snow': snow, 'rock': rock, 'road': road}


# -----------------------------------------------------------------------------
# Per-tile field cache
# -----------------------------------------------------------------------------

class TileFields:
    def __init__(self, tile_x: int, tile_y: int, zone: ZoneModel):
        self.tx, self.ty = tile_x, tile_y
        self.zone = zone
        self.base_x = WORLD_HALF - tile_x * ADT_SIZE
        self.base_y = WORLD_HALF - tile_y * ADT_SIZE
        n = GRID_TOTAL
        self.n = n
        self.h: List[List[float]] = [[0.0] * n for _ in range(n)]
        self.surface: List[List[Optional[float]]] = [[None] * n for _ in range(n)]
        self.water_margin: List[List[float]] = [[1e9] * n for _ in range(n)]
        self.water_type: List[List[str]] = [[''] * n for _ in range(n)]
        self.road_t: List[List[float]] = [[0.0] * n for _ in range(n)]
        self.settle_t: List[List[float]] = [[0.0] * n for _ in range(n)]
        self.slope: List[List[float]] = [[0.0] * n for _ in range(n)]
        self._weights: Dict[str, List[List[float]]] = {}
        self._build()

    # -- coordinates ----------------------------------------------------------
    def world_at(self, gx: float, gy: float) -> Tuple[float, float]:
        return (self.base_x - (gx - MARGIN) * HALF_UNIT, self.base_y - (gy - MARGIN) * HALF_UNIT)

    def local_at(self, wx: float, wy: float) -> Tuple[float, float]:
        z = self.zone
        return ((WORLD_HALF - wx) / ADT_SIZE - z.ox, (WORLD_HALF - wy) / ADT_SIZE - z.oy)

    # -- construction -----------------------------------------------------------
    def _build(self):
        z = self.zone
        n = self.n
        seed = z.seed
        water_on = z.water_enabled and bool(z.water_features)
        has_roads = bool(z.roads)
        has_settle = bool(z.settlements)
        for gy in range(n):
            wy = self.base_y - (gy - MARGIN) * HALF_UNIT
            ly = (WORLD_HALF - wy) / ADT_SIZE - z.oy
            row_h = self.h[gy]; row_s = self.surface[gy]; row_m = self.water_margin[gy]
            row_t = self.water_type[gy]; row_r = self.road_t[gy]; row_st = self.settle_t[gy]
            for gx in range(n):
                wx = self.base_x - (gx - MARGIN) * HALF_UNIT
                lx = (WORLD_HALF - wx) / ADT_SIZE - z.ox
                h = z.macro_height(wx, wy, lx, ly)
                # roads: cut/fill toward the smoothed profile height
                if has_roads:
                    ri, rd, rw, rs = z.road_at(lx, ly)
                    if ri >= 0 and rd < rw * 2.6:
                        prof = z.road_profile_height(ri, rs)
                        t = 1.0 - smoothstep(rw * 0.9, rw * 2.6, rd)
                        h = h * (1.0 - 0.94 * t) + prof * 0.94 * t
                        row_r[gx] = 1.0 - smoothstep(rw * 0.75, rw * 1.3, rd)
                # settlements: flatten toward their natural centre height
                if has_settle:
                    si, sd, sr = z.settlement_at(lx, ly)
                    if si >= 0 and sd < sr * 1.4:
                        target = z.settlement_target(si)
                        t = 1.0 - smoothstep(sr * 0.75, sr * 1.4, sd)
                        h = h * (1.0 - 0.9 * t) + target * 0.9 * t
                        row_st[gx] = t
                # water: carve channel/basin below the surface, banks up to terrain
                if water_on:
                    f, margin, surface = z.water_at(lx, ly)
                    if f is not None:
                        w_yd = f['_width'] * ADT_SIZE
                        # banks climb from the water edge to the terrain at no more
                        # than ~27 degrees, so high ground makes wide slopes, not gorges
                        bank = max(w_yd * 0.7, 8.0, (h - surface) * 2.0)
                        plain = max(bank, 45.0)
                        if margin < plain:
                            depth = f['_depth']
                            if margin <= 0.0:
                                # inside: parabolic bed, deepest at the centre line
                                inside = (-margin) / max(w_yd, 1e-6)  # 0 at edge .. 1 at centre
                                bed = surface - depth * (1.0 - (1.0 - min(1.0, inside)) ** 2)
                                h = min(h, bed)
                            else:
                                # bank band: rise from the water edge to the terrain
                                if margin < bank and h > surface:
                                    h = surface + (h - surface) * smoothstep(0.0, bank, margin)
                                # floodplain floor: valleys must not dip below the water
                                # surface outside the channel, or the river floods in blobs
                                floor = surface + 0.4 + 2.5 * smoothstep(0.0, plain, margin)
                                if h < floor + 2.0:
                                    # smooth maximum (C1) so the clamp leaves no visible crease
                                    h = 0.5 * (h + floor + math.sqrt((h - floor) * (h - floor) + 4.0))
                            if margin < bank:
                                row_m[gx] = margin
                                row_t[gx] = f['_type']
                                row_s[gx] = surface
                row_h[gx] = h
        # slope from the macro/carved height (kept for texture masks so rock follows
        # real cliffs and ridges instead of micro-noise speckle), then micro relief
        self._compute_slope(step=2)
        self.macro_slope = self._box_blur(self.slope)
        self._compute_slope()
        micro_amp = z.micro_noise
        for gy in range(n):
            wy = self.base_y - (gy - MARGIN) * HALF_UNIT
            for gx in range(n):
                wx = self.base_x - (gx - MARGIN) * HALF_UNIT
                s = self.slope[gy][gx]
                amp = micro_amp * (0.35 + 0.65 * smoothstep(8.0, 35.0, s))
                amp *= (1.0 - self.road_t[gy][gx]) * (1.0 - 0.8 * self.settle_t[gy][gx])
                surf = self.surface[gy][gx]
                if surf is not None and self.h[gy][gx] < surf + 0.6:
                    amp *= 0.25
                if amp > 1e-4:
                    self.h[gy][gx] += fbm(wx / 21.0, wy / 21.0, seed + 977, 3) * amp
        self._compute_slope()

    def _compute_slope(self, step: int = 1):
        n = self.n; h = self.h
        for gy in range(n):
            y0 = max(0, gy - step); y1 = min(n - 1, gy + step)
            for gx in range(n):
                x0 = max(0, gx - step); x1 = min(n - 1, gx + step)
                dx = (h[gy][x1] - h[gy][x0]) / ((x1 - x0) * HALF_UNIT)
                dy = (h[y1][gx] - h[y0][gx]) / ((y1 - y0) * HALF_UNIT)
                self.slope[gy][gx] = math.degrees(math.atan(math.sqrt(dx * dx + dy * dy)))

    def _box_blur(self, grid: List[List[float]]) -> List[List[float]]:
        """3x3 box blur (edge-clamped); used to keep the rock mask coherent."""
        n = self.n
        out = [[0.0] * n for _ in range(n)]
        for gy in range(n):
            y0 = max(0, gy - 1); y1 = min(n - 1, gy + 1)
            for gx in range(n):
                x0 = max(0, gx - 1); x1 = min(n - 1, gx + 1)
                total = 0.0; cnt = 0
                for yy in range(y0, y1 + 1):
                    row = grid[yy]
                    for xx in range(x0, x1 + 1):
                        total += row[xx]; cnt += 1
                out[gy][gx] = total / cnt
        return out

    # -- sampling ---------------------------------------------------------------
    def height(self, gx: int, gy: int) -> float:
        return self.h[gy + MARGIN][gx + MARGIN]

    def height_f(self, gx: float, gy: float) -> float:
        """Bilinear height at fractional tile-grid coordinates."""
        fx = gx + MARGIN; fy = gy + MARGIN
        ix = int(math.floor(fx)); iy = int(math.floor(fy))
        ix = max(0, min(self.n - 2, ix)); iy = max(0, min(self.n - 2, iy))
        tx = fx - ix; ty = fy - iy
        h = self.h
        a = h[iy][ix] + (h[iy][ix + 1] - h[iy][ix]) * tx
        b = h[iy + 1][ix] + (h[iy + 1][ix + 1] - h[iy + 1][ix]) * tx
        return a + (b - a) * ty

    def normal(self, gx: int, gy: int) -> Tuple[float, float, float]:
        """Unit normal (x, y_up, z) in noggit's frame at a tile-grid sample.

        +x is the column direction, +z the row direction, y up. For a surface
        y = h(x, z) the normal is (-dh/dx, 1, -dh/dz) normalised.
        """
        x = gx + MARGIN; y = gy + MARGIN; h = self.h
        dhdx = (h[y][x + 1] - h[y][x - 1]) / (2.0 * HALF_UNIT)
        dhdz = (h[y + 1][x] - h[y - 1][x]) / (2.0 * HALF_UNIT)
        nx, ny, nz = -dhdx, 1.0, -dhdz
        inv = 1.0 / math.sqrt(nx * nx + ny * ny + nz * nz)
        return nx * inv, ny * inv, nz * inv

    def surface_at(self, gx: int, gy: int) -> Optional[float]:
        return self.surface[gy + MARGIN][gx + MARGIN]

    def chunk_heights(self, cx: int, cy: int) -> Tuple[float, List[float]]:
        """(ypos base, 145 relative MCVT heights in noggit order)."""
        gx0 = 16 * cx; gy0 = 16 * cy
        out: List[float] = []
        for row in range(17):
            if row % 2 == 0:
                for col in range(9):
                    out.append(self.height(gx0 + 2 * col, gy0 + row))
            else:
                for col in range(8):
                    out.append(self.height(gx0 + 2 * col + 1, gy0 + row))
        base = out[0]
        return base, [v - base for v in out]

    def chunk_normals(self, cx: int, cy: int) -> List[Tuple[float, float, float]]:
        gx0 = 16 * cx; gy0 = 16 * cy
        out = []
        for row in range(17):
            if row % 2 == 0:
                for col in range(9):
                    out.append(self.normal(gx0 + 2 * col, gy0 + row))
            else:
                for col in range(8):
                    out.append(self.normal(gx0 + 2 * col + 1, gy0 + row))
        return out

    # -- water ---------------------------------------------------------------------
    def quad_wet(self, cx: int, cy: int, qx: int, qy: int, min_depth: float = 0.05) -> bool:
        gx = 16 * cx + 2 * qx + 1; gy = 16 * cy + 2 * qy + 1
        s = self.surface_at(gx, gy)
        if s is None:
            return False
        # a quad is wet if its centre and at least two corners are below the surface
        if s - self.height(gx, gy) <= min_depth:
            return False
        below = 0
        for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            sc = self.surface_at(gx + dx, gy + dy)
            if sc is not None and sc - self.height(gx + dx, gy + dy) > min_depth:
                below += 1
        return below >= 2

    def chunk_water(self, cx: int, cy: int) -> Optional[Dict[str, Any]]:
        """Wet-quad bitmap, per-vertex surface heights and depth (opacity) bytes for one chunk."""
        bits = 0
        for qy in range(8):
            for qx in range(8):
                if self.quad_wet(cx, cy, qx, qy):
                    bits |= 1 << (qy * 8 + qx)
        if bits == 0:
            return None
        gx0 = 16 * cx; gy0 = 16 * cy
        heights: List[List[float]] = [[0.0] * 9 for _ in range(9)]
        depths: List[List[int]] = [[0] * 9 for _ in range(9)]
        # feature type of the first wet quad decides ocean vs river opacity/liquid type
        first = next((qx, qy) for qy in range(8) for qx in range(8) if bits & (1 << (qy * 8 + qx)))
        wtype = self.water_type[gy0 + 2 * first[1] + 1 + MARGIN][gx0 + 2 * first[0] + 1 + MARGIN]
        # Noggit's update_vertex_opacity: depth = clamp((water - ground + 1) * factor, 0, 1),
        # factor 0.0337 for rivers/lakes and 0.007 for oceans ("values found by experimenting"
        # against Blizzard data), so shallow shores are nearly transparent.
        factor = 0.007 if wtype == 'ocean_edge' else 0.0337
        # surface for vertices of wet quads; dry vertices reuse the nearest wet quad's surface
        for vy in range(9):
            for vx in range(9):
                gx = gx0 + 2 * vx; gy = gy0 + 2 * vy
                s = self.surface_at(gx, gy)
                if s is None:
                    best = None; bd = 1e9
                    for qy in range(max(0, vy - 1), min(8, vy + 1)):
                        for qx in range(max(0, vx - 1), min(8, vx + 1)):
                            if bits & (1 << (qy * 8 + qx)):
                                sc = self.surface_at(gx0 + 2 * qx + 1, gy0 + 2 * qy + 1)
                                d = abs(qx + 0.5 - vx) + abs(qy + 0.5 - vy)
                                if sc is not None and d < bd:
                                    bd = d; best = sc
                    s = best if best is not None else self.zone.default_level
                heights[vy][vx] = s
                bed = self.height(gx, gy)
                depths[vy][vx] = int(round(255.0 * clamp((s - bed + 1.0) * factor, 0.0, 1.0)))
        return {'bits': bits, 'heights': heights, 'depths': depths, 'type': wtype}

    # -- texture weights -------------------------------------------------------------
    def weights(self, role: str) -> List[List[float]]:
        if not self._weights:
            self._compute_weights()
        return self._weights[role]

    def _compute_weights(self):
        z = self.zone; n = self.n
        w = {r: [[0.0] * n for _ in range(n)] for r in ROLE_ORDER}
        for gy in range(n):
            wy = self.base_y - (gy - MARGIN) * HALF_UNIT
            for gx in range(n):
                wx = self.base_x - (gx - MARGIN) * HALF_UNIT
                h = self.h[gy][gx]
                s = self.surface[gy][gx]
                above = (h - s) if s is not None else None
                rw = z.theme_role_weights(h, self.macro_slope[gy][gx], self.road_t[gy][gx], self.water_margin[gy][gx],
                                          above, self.water_type[gy][gx], self.settle_t[gy][gx], wx, wy)
                for r, v in rw.items():
                    w[r][gy][gx] = v
        self._weights = w

    def chunk_role_scores(self, cx: int, cy: int) -> Dict[str, float]:
        """Mean weight per role over the chunk (for layer selection)."""
        out: Dict[str, float] = {}
        gx0 = 16 * cx + MARGIN; gy0 = 16 * cy + MARGIN
        for role in ROLE_ORDER:
            grid = self.weights(role)
            total = 0.0
            for gy in range(gy0, gy0 + 17, 2):
                row = grid[gy]
                for gx in range(gx0, gx0 + 17, 2):
                    total += row[gx]
            out[role] = total / 81.0
        return out

    # -- distant / minimap sampling ---------------------------------------------------
    def mare_heights(self) -> Tuple[List[float], List[float]]:
        outer = [self.height(16 * i, 16 * j) for j in range(17) for i in range(17)]
        inner = [self.height(16 * i + 8, 16 * j + 8) for j in range(16) for i in range(16)]
        return outer, inner


# -----------------------------------------------------------------------------
# Alpha map rasterisation
# -----------------------------------------------------------------------------

_BREAKUP: Optional[List[List[float]]] = None
_BREAKUP_SEED = 0


def breakup_table(seed: int) -> List[List[float]]:
    """128x128 tileable-ish low-frequency noise in [-0.5, 0.5] for organic alpha edges."""
    global _BREAKUP, _BREAKUP_SEED
    if _BREAKUP is None or _BREAKUP_SEED != seed:
        tbl = []
        for y in range(128):
            row = []
            for x in range(128):
                v = fbm(x / 19.0, y / 19.0, seed + 8080, 3) * 0.5
                row.append(v)
            tbl.append(row)
        _BREAKUP = tbl; _BREAKUP_SEED = seed
    return _BREAKUP


ROLE_SHAPING = {
    # role: (edge0, edge1, gain) applied to the interpolated weight
    'road':   (0.10, 0.70, 1.00),
    'rock':   (0.12, 0.80, 0.95),
    'shore':  (0.12, 0.75, 0.85),
    'sand':   (0.12, 0.75, 0.90),
    'forest': (0.20, 0.85, 0.70),
    'snow':   (0.15, 0.80, 1.00),
    'plague': (0.15, 0.80, 0.80),
}

_TEXEL_IX: List[int] = []
_TEXEL_FX: List[float] = []
for _a in range(64):
    _g = (_a + 0.5) / 4.0
    _i = int(math.floor(_g))
    _TEXEL_IX.append(min(15, _i))
    _TEXEL_FX.append(_g - _i)


def rasterize_alpha(fields: TileFields, cx: int, cy: int, role: str, seed: int, breakup: float = 0.22) -> bytes:
    """64x64 8-bit alpha map for one role in one chunk (row-major, y outer)."""
    grid = fields.weights(role)
    gx0 = 16 * cx + MARGIN; gy0 = 16 * cy + MARGIN
    e0, e1, gain = ROLE_SHAPING.get(role, (0.12, 0.85, 0.7))
    tbl = breakup_table(seed)
    # world-anchored texel index so the breakup pattern is continuous across chunks/tiles
    tex_x0 = (fields.tx * 16 + cx) * 64
    tex_y0 = (fields.ty * 16 + cy) * 64
    out = bytearray(4096)
    inv = 1.0 / (e1 - e0)
    for ay in range(64):
        iy = _TEXEL_IX[ay]; fy = _TEXEL_FX[ay]
        rowA = grid[gy0 + iy]; rowB = grid[gy0 + iy + 1]
        brow = tbl[(tex_y0 + ay) & 127]
        o = ay * 64
        for ax in range(64):
            ix = _TEXEL_IX[ax]; fx = _TEXEL_FX[ax]
            a = rowA[gx0 + ix]; b = rowA[gx0 + ix + 1]
            c = rowB[gx0 + ix]; d = rowB[gx0 + ix + 1]
            w = (a + (b - a) * fx) * (1.0 - fy) + (c + (d - c) * fx) * fy
            w += brow[(tex_x0 + ax) & 127] * breakup * (0.35 + w)
            t = (w - e0) * inv
            if t <= 0.0:
                continue
            if t >= 1.0:
                v = gain
            else:
                v = t * t * (3.0 - 2.0 * t) * gain
            out[o + ax] = int(v * 255.0 + 0.5)
    return bytes(out)
