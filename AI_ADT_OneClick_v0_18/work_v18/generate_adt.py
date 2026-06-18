#!/usr/bin/env python3
"""
AI ADT One-Click v0.18
Generates WotLK 3.3.5a-style ADT/WDT terrain files directly, now with:
  - MH2O liquid mesh data for a simple generated river/lowland water mask
  - WDL low-resolution distant terrain for the custom map folder
  - 256x256 minimap BLP tiles + TGA previews + md5translate.trs fragment

This is still deliberately no-M2/no-WMO. Object placement is a separate, much harder
problem and should not block terrain/water/minimap iteration.

New through v0.18:
  - Automatic multi-layer texture painting: MCLY + MCAL alphamaps
  - Optional learned_blizzlike_rules.json support from learn_blizzlike_rules.py
  - Terrain/water generated from AI-planned ridges, valleys, roads, lakes, coastlines, and settlements
  - Noggit-source recheck: raw MCNK xpos/zpos now use the zero-point-relative chunk base directly
  - v0.18: optional config-path CLI argument and explicit config keys now override zone_spec defaults
"""
import json
import math
import os
import shutil
import struct
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ADT_SIZE = 533.3333333333334
CHUNK_SIZE = ADT_SIZE / 16.0
UNIT = CHUNK_SIZE / 8.0
WORLD_SIZE = ADT_SIZE * 64.0
WORLD_HALF = WORLD_SIZE / 2.0
MINIMAP_SIZE = 256

DEFAULT_CONFIG = {
    "map_name": "aigen",
    "tiles_x": [32, 33],
    "tiles_y": [32, 33],
    "base_texture": "tileset\\elwynn\\elwynngrassbase.blp",
    "texture_painting": {
        "enabled": True,
        "learned_rules_path": "learned_blizzlike_rules.json",
        "max_layers_per_chunk": 4,
        "alpha_mode": "raw_4096",
        "write_texture_debug": True,
        "alpha_sample_step": 32
    },
    "wdt": {
        "mphd_flags": "auto"
    },
    "texture_layers": [],
    "mode": "both",  # both | custom | azeroth_override
    "height_scale": 42.0,
    "hill_height": 85.0,
    "valley_depth": 18.0,
    "seed": 1337,
    "area_id": 0,
    "output_dir": "output_loose",
    "water": {
        "enabled": True,
        "level": 38.0,
        "liquid_type": 1,
        "liquid_vertex_format": 0,
        "river_width": 27.0,
        "shore_tolerance": 11.0
    },
    "wdl": {
        "enabled": True,
        "custom_only": True
    },
    "minimap": {
        "enabled": True,
        "tile_size": 256,
        "write_tga_previews": True,
        "write_world_minimaps_copy": True,
        "write_md5translate": True,
        "sample_step": 32
    }
}


def u32(x): return struct.pack('<I', int(x) & 0xFFFFFFFF)
def u64(x): return struct.pack('<Q', int(x) & 0xFFFFFFFFFFFFFFFF)
def i32(x): return struct.pack('<i', int(x))
def u16(x): return struct.pack('<H', int(x) & 0xFFFF)
def i16(x): return struct.pack('<h', max(-32768, min(32767, int(round(x)))))
def u8(x): return struct.pack('<B', int(x) & 0xFF)
def f32(x): return struct.pack('<f', float(x))


def magic(name: str) -> bytes:
    """WoW ADT/WDT/WDL chunk fourCCs are stored reversed on disk.
    Documentation says MVER, file bytes are REVM.
    """
    assert len(name) == 4
    return name[::-1].encode('ascii')

def chunk(name: str, data: bytes) -> bytes:
    return magic(name) + u32(len(data)) + data


def pad4(b: bytes) -> bytes:
    return b + (b'\0' * ((4 - (len(b) % 4)) % 4))


def write_file(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def deep_update(default: dict, user: dict) -> dict:
    out = dict(default)
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_update(out[k], v)
        else:
            out[k] = v
    return out



# -----------------------------------------------------------------------------
# AI zone spec helpers
# -----------------------------------------------------------------------------

def load_zone_spec(path: str | Path) -> Optional[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'Warning: could not read zone spec {p}: {e}')
        return None


def _top_level_user_keys(user: dict) -> set[str]:
    return set(user.keys()) if isinstance(user, dict) else set()


def attach_zone_spec(cfg: dict, explicit_keys: Optional[set[str]] = None) -> dict:
    """Attach zone_spec.json without trampling explicit config overrides.

    Earlier versions always replaced tiles_x/tiles_y/map_name/base_texture with
    values from zone_spec.json. That made command-line smoke/small configs and
    user configs silently generate the full spec map anyway. Defaults are still
    allowed to come from the spec, but explicit values in config.json or an
    alternate config file win.
    """
    explicit_keys = explicit_keys or set()
    spec_path = cfg.get('zone_spec_path', 'zone_spec.json')
    spec = load_zone_spec(spec_path)
    if not spec:
        return cfg
    cfg['_zone_spec'] = spec

    def use_spec_scalar(key: str, value: Any):
        if value is not None and key not in explicit_keys:
            cfg[key] = value

    use_spec_scalar('map_name', spec.get('map_name'))
    use_spec_scalar('tiles_x', spec.get('tiles_x'))
    use_spec_scalar('tiles_y', spec.get('tiles_y'))
    tex = spec.get('textures', {}) if isinstance(spec.get('textures', {}), dict) else {}
    if 'base_texture' not in explicit_keys and tex.get('base_texture'):
        cfg['base_texture'] = tex.get('base_texture')

    gen = spec.get('generator_config', {}) if isinstance(spec.get('generator_config', {}), dict) else {}
    for k in ['mode', 'area_id', 'output_dir', 'height_scale', 'hill_height', 'valley_depth']:
        if k in gen and k not in explicit_keys:
            cfg[k] = gen[k]
    if 'water' in gen:
        water = dict(cfg.get('water', {}))
        if 'water' not in explicit_keys:
            water.update(gen.get('water', {}))
        cfg['water'] = water
    if 'wdl' in gen:
        wdl = dict(cfg.get('wdl', {}))
        if 'wdl' not in explicit_keys:
            wdl.update(gen.get('wdl', {}))
        cfg['wdl'] = wdl
    if 'minimap' in gen:
        mini = dict(cfg.get('minimap', {}))
        if 'minimap' not in explicit_keys:
            mini.update(gen.get('minimap', {}))
        cfg['minimap'] = mini
    return cfg

def spec_bounds(cfg: dict) -> Tuple[float, float, float, float]:
    spec = cfg.get('_zone_spec') or {}
    ox, oy = spec.get('origin_tile', [min(cfg.get('tiles_x', [32])), min(cfg.get('tiles_y', [32]))])
    sx, sy = spec.get('size_adts', [len(cfg.get('tiles_x', [32])), len(cfg.get('tiles_y', [32]))])
    return float(ox), float(oy), float(sx), float(sy)


def world_to_spec_xy(world_x: float, world_y: float, cfg: dict) -> Tuple[float, float]:
    ox, oy, _sx, _sy = spec_bounds(cfg)
    tile_fx = (WORLD_HALF - world_x) / ADT_SIZE
    tile_fy = (WORLD_HALF - world_y) / ADT_SIZE
    return tile_fx - ox, tile_fy - oy


def dist_point_to_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> Tuple[float, float]:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    denom = vx * vx + vy * vy
    if denom <= 1e-9:
        return math.hypot(px - ax, py - ay), 0.0
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
    cx, cy = ax + t * vx, ay + t * vy
    return math.hypot(px - cx, py - cy), t


def dist_point_to_path(px: float, py: float, path: List[List[float]]) -> float:
    if not path:
        return 1e9
    if len(path) == 1:
        return math.hypot(px - float(path[0][0]), py - float(path[0][1]))
    best = 1e9
    for i in range(len(path) - 1):
        ax, ay = float(path[i][0]), float(path[i][1])
        bx, by = float(path[i+1][0]), float(path[i+1][1])
        d, _ = dist_point_to_segment(px, py, ax, ay, bx, by)
        if d < best:
            best = d
    return best


def gaussian(d: float, width: float) -> float:
    width = max(0.001, width)
    return math.exp(-(d * d) / (2.0 * width * width))


def spec_height_func(world_x: float, world_y: float, cfg: dict) -> Optional[float]:
    spec = cfg.get('_zone_spec')
    if not spec:
        return None
    lx, ly = world_to_spec_xy(world_x, world_y, cfg)
    _ox, _oy, sx, sy = spec_bounds(cfg)
    terrain = spec.get('terrain', {})
    base = float(terrain.get('base_elevation', 42.0))

    # restrained procedural broad forms; enough to prevent dead-flat generated specs.
    h = base
    h += math.sin(lx * 1.85 + 0.7) * float(cfg.get('height_scale', 18.0)) * 0.33
    h += math.cos(ly * 1.55 - 0.2) * float(cfg.get('height_scale', 18.0)) * 0.25
    h += math.sin((lx + ly) * 1.05) * float(cfg.get('height_scale', 18.0)) * 0.16

    for f in terrain.get('features', []):
        ftype = str(f.get('type', '')).lower()
        width = float(f.get('width', f.get('radius', 0.5)))
        if 'path' in f:
            d = dist_point_to_path(lx, ly, f.get('path') or [])
        else:
            cx, cy = f.get('center', [sx/2, sy/2])
            d = math.hypot(lx - float(cx), ly - float(cy))
        g = gaussian(d, width)
        if ftype in ['ridge', 'hill', 'plateau']:
            h += float(f.get('height', 25.0)) * g
        elif ftype in ['valley', 'basin', 'coast']:
            h -= float(f.get('depth', 20.0)) * g
        elif ftype == 'cliff_band':
            h += float(f.get('height', 40.0)) * min(1.0, g * 1.4)

    # Roads gently carve/flatten local terrain. This is not a full path solver yet,
    # but it creates readable corridors the minimap and terrain can share.
    for r in spec.get('roads', []):
        d = dist_point_to_path(lx, ly, r.get('path') or [])
        width = float(r.get('width', 0.06))
        g = gaussian(d, max(width * 1.8, 0.035))
        # mild road cut, strongest on hills/ridges.
        h -= 4.5 * g

    # Settlement pads get broad, slightly raised, smoother-feeling terrain.
    for sflat in terrain.get('settlement_flattening', []):
        cx, cy = sflat.get('center', [sx/2, sy/2])
        radius = float(sflat.get('radius', 0.3))
        d = math.hypot(lx - float(cx), ly - float(cy))
        g = gaussian(d, radius)
        target = base + float(sflat.get('height', 5.0))
        if g > 0.02:
            h = h * (1.0 - 0.72 * g) + target * (0.72 * g)

    return h


def spec_water_distance(world_x: float, world_y: float, cfg: dict) -> Tuple[float, Optional[Dict[str, Any]]]:
    spec = cfg.get('_zone_spec')
    if not spec:
        return 1e9, None
    lx, ly = world_to_spec_xy(world_x, world_y, cfg)
    best = 1e9
    best_f = None
    for f in spec.get('water', {}).get('features', []):
        ftype = str(f.get('type', '')).lower()
        if 'path' in f:
            d = dist_point_to_path(lx, ly, f.get('path') or [])
        else:
            cx, cy = f.get('center', [0, 0])
            d = math.hypot(lx - float(cx), ly - float(cy))
        width = float(f.get('width', f.get('radius', 0.12)))
        margin = d - width
        if margin < best:
            best = margin
            best_f = f
    return best, best_f

# -----------------------------------------------------------------------------
# World-space procedural fields
# -----------------------------------------------------------------------------


def height_func(world_x: float, world_y: float, cfg: dict) -> float:
    """Procedural low-frequency terrain. If zone_spec.json is present, use its planned terrain primitives."""
    spec_h = spec_height_func(world_x, world_y, cfg)
    if spec_h is not None:
        return spec_h

    s = float(cfg.get('height_scale', 42.0))
    hill = float(cfg.get('hill_height', 85.0))
    valley = float(cfg.get('valley_depth', 18.0))

    # local center around (0,0) if testing 32/32 tiles
    r = math.sqrt(world_x * world_x + world_y * world_y)
    radial = math.exp(-(r * r) / (2 * 420.0 * 420.0)) * hill
    waves = (math.sin(world_x / 120.0) * 0.65 + math.cos(world_y / 155.0) * 0.55 + math.sin((world_x + world_y) / 210.0) * 0.35) * s
    # soft diagonal valley / river depression
    d = river_distance(world_x, world_y)
    river_valley = -valley * math.exp(-(d * d) / (2 * 58.0 * 58.0))
    return 40.0 + radial + waves + river_valley


def river_distance(world_x: float, world_y: float, cfg: Optional[dict] = None) -> float:
    """Distance to the generated river/water feature. Returns world yards for legacy mode, ADT units for spec mode."""
    if cfg and cfg.get('_zone_spec'):
        d, _ = spec_water_distance(world_x, world_y, cfg)
        return d
    return abs((world_y * 0.65) - (world_x * 0.18) + 35.0) / math.sqrt(0.65 * 0.65 + 0.18 * 0.18)


def is_water_at(world_x: float, world_y: float, cfg: dict) -> bool:
    water = cfg.get('water', {})
    if not water.get('enabled', True):
        return False
    h = height_func(world_x, world_y, cfg)

    if cfg.get('_zone_spec'):
        d, feature = spec_water_distance(world_x, world_y, cfg)
        if feature is None:
            return False
        level = float(feature.get('level', cfg.get('_zone_spec', {}).get('water', {}).get('level', water.get('level', 38.0))))
        # d <= 0 means inside planned water width/radius, plus small shore tolerance in height.
        return d <= 0.0 and h <= level + float(water.get('shore_tolerance', 11.0))

    water_level = float(water.get('level', 38.0))
    river_width = float(water.get('river_width', 27.0))
    shore_tolerance = float(water.get('shore_tolerance', 11.0))
    # Constrain water to the generated valley. This prevents "flood the whole tile"
    # behavior while still giving MH2O data that is visible and testable in-game.
    return river_distance(world_x, world_y) <= river_width and h <= water_level + shore_tolerance


def chunk_base_world(tile_x: int, tile_y: int, chunk_x: int, chunk_y: int) -> Tuple[float, float]:
    # ADT tile 32/32 maps around world origin. X decreases as tile_x/chunk_x increases in WoW.
    # Y decreases as tile_y/chunk_y increases. This matches the ordinary 64x64 ADT grid layout.
    wx = WORLD_HALF - (tile_x * ADT_SIZE) - (chunk_x * CHUNK_SIZE)
    wy = WORLD_HALF - (tile_y * ADT_SIZE) - (chunk_y * CHUNK_SIZE)
    return wx, wy


def tile_base_world(tile_x: int, tile_y: int) -> Tuple[float, float]:
    return chunk_base_world(tile_x, tile_y, 0, 0)


def sample_world_in_tile(tile_x: int, tile_y: int, local_x: float, local_y: float) -> Tuple[float, float]:
    base_x, base_y = tile_base_world(tile_x, tile_y)
    return base_x - local_x, base_y - local_y


# -----------------------------------------------------------------------------
# Automatic MCLY/MCAL alphamap texture painting
# -----------------------------------------------------------------------------

DEFAULT_TEXTURE_LAYERS = [
    {"role": "base", "label": "grass base", "path": "tileset\\elwynn\\elwynngrassbase.blp", "priority": 0.05},
    {"role": "road", "label": "dirt road", "path": "tileset\\elwynn\\elwynndirtbase.blp", "priority": 0.95},
    {"role": "rock", "label": "slope rock", "path": "tileset\\elwynn\\elwynnrock.blp", "priority": 0.70},
    {"role": "forest", "label": "forest floor", "path": "tileset\\ashenvale\\ashenvaledarkgrass.blp", "priority": 0.35},
    {"role": "shore", "label": "muddy shore", "path": "tileset\\elwynn\\elwynndirtbase.blp", "priority": 0.55},
    {"role": "snow", "label": "snow patch", "path": "tileset\\northrend\\snow\\snow01.blp", "priority": 0.45},
]

ROLE_COLORS = {
    "base": (76, 108, 52),
    "road": (112, 88, 55),
    "rock": (116, 116, 112),
    "forest": (42, 76, 38),
    "shore": (94, 78, 49),
    "snow": (185, 190, 185),
    "sand": (153, 132, 82),
    "plague": (92, 98, 44),
}


def _role_from_label(label: str) -> str:
    l = label.lower()
    if any(w in l for w in ['road', 'dirt', 'trail', 'path']):
        return 'road'
    if any(w in l for w in ['rock', 'cliff', 'stone', 'slope']):
        return 'rock'
    if any(w in l for w in ['mud', 'shore', 'sand', 'beach', 'wet']):
        return 'shore' if 'sand' not in l else 'sand'
    if any(w in l for w in ['snow', 'ice', 'frozen']):
        return 'snow'
    if any(w in l for w in ['forest', 'leaf', 'pine', 'needle', 'moss']):
        return 'forest'
    if any(w in l for w in ['plague', 'sick', 'rot']):
        return 'plague'
    return 'base'


def load_learned_rules(cfg: dict) -> Optional[Dict[str, Any]]:
    tp = cfg.get('texture_painting', {})
    path = tp.get('learned_rules_path') or 'learned_blizzlike_rules.json'
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'Warning: could not read learned rules {p}: {e}')
        return None



def detect_texture_theme(cfg: dict) -> str:
    spec = cfg.get('_zone_spec') or {}
    text_parts = [
        str(spec.get('source_prompt', '')),
        str(spec.get('zone_name', '')),
        str(spec.get('creative_intent', '')),
        str(spec.get('textures', {}).get('palette', '')),
    ]
    text = ' '.join(text_parts).lower()
    if any(w in text for w in ['jungle', 'stranglethorn', 'tropical', 'rainforest', 'lush']): return 'jungle'
    if any(w in text for w in ['swamp', 'marsh', 'bog', 'wetlands']): return 'swamp'
    if any(w in text for w in ['snow', 'ice', 'frozen', 'winter']): return 'snow'
    if any(w in text for w in ['desert', 'tanaris', 'uldum', 'sand', 'canyon']): return 'desert'
    if any(w in text for w in ['plague', 'undead', 'haunted', 'crypt', 'lordaeron']): return 'plague'
    if any(w in text for w in ['northrend', 'fjord', 'grizzly', 'vrykul', 'borean']): return 'northrend'
    return 'forest'

def texture_catalog(cfg: dict) -> List[Dict[str, Any]]:
    """Return ordered global MTEX texture entries with role metadata."""
    if '_texture_catalog_cached' in cfg:
        return cfg['_texture_catalog_cached']
    seen = set()
    out: List[Dict[str, Any]] = []

    def add(role: str, path: str, label: Optional[str] = None, priority: float = 0.0):
        if not path:
            return
        norm = path.replace('/', '\\')
        key = norm.lower()
        if key in seen:
            return
        seen.add(key)
        out.append({"role": role, "path": norm, "label": label or role, "priority": float(priority)})

    # learned rules win: they are taken from the user's local ADTs, so those paths should exist.
    # v0.7 first tries a theme-specific learned texture pack, e.g. jungle textures
    # from Stranglethorn ADTs after learning all of Azeroth.
    learned = load_learned_rules(cfg)
    if learned:
        theme = detect_texture_theme(cfg)
        themed = (learned.get('recommended_texture_layers_by_theme') or {}).get(theme) or []
        for item in themed[:12]:
            role = str(item.get('role') or _role_from_label(item.get('label','')))
            add(role, str(item.get('path','')), item.get('label') or role, float(item.get('priority', 0.4)))
        for item in learned.get('recommended_texture_layers', [])[:12]:
            role = str(item.get('role') or _role_from_label(item.get('label','')))
            add(role, str(item.get('path','')), item.get('label') or role, float(item.get('priority', 0.4)))

    spec = cfg.get('_zone_spec') or {}
    tex = spec.get('textures', {})
    for item in tex.get('texture_layers', []) or tex.get('layers', []) or []:
        role = str(item.get('role') or _role_from_label(item.get('label','')))
        add(role, str(item.get('path') or item.get('texture') or ''), item.get('label') or role, float(item.get('priority', 0.3)))

    # Keep old base texture if present.
    add('base', cfg.get('base_texture', DEFAULT_CONFIG['base_texture']), 'configured base', 0.1)

    # Built-in fallback paths. Users can override with zone_spec/learned rules if a path is wrong.
    for item in cfg.get('texture_layers', []) or []:
        role = str(item.get('role') or _role_from_label(item.get('label','')))
        add(role, str(item.get('path','')), item.get('label') or role, float(item.get('priority', 0.3)))
    for item in DEFAULT_TEXTURE_LAYERS:
        add(item['role'], item['path'], item['label'], item['priority'])

    # WoW terrain chunks can only use a few layers at a time, but MTEX may list more.
    cfg['_texture_catalog_cached'] = out[:32]
    return cfg['_texture_catalog_cached']


def texture_id_map(cfg: dict) -> Dict[str, int]:
    return {t['role']: i for i, t in enumerate(texture_catalog(cfg)) if t.get('role') not in {} }


def estimate_slope(world_x: float, world_y: float, cfg: dict) -> float:
    step = 6.0
    hx1 = height_func(world_x - step, world_y, cfg)
    hx2 = height_func(world_x + step, world_y, cfg)
    hy1 = height_func(world_x, world_y - step, cfg)
    hy2 = height_func(world_x, world_y + step, cfg)
    grad = math.sqrt(((hx1 - hx2) / (2 * step)) ** 2 + ((hy1 - hy2) / (2 * step)) ** 2)
    return math.degrees(math.atan(grad))


def distance_to_roads_spec(world_x: float, world_y: float, cfg: dict) -> Tuple[float, float]:
    spec = cfg.get('_zone_spec')
    if not spec:
        d_world = river_distance(world_x, world_y)  # legacy proxy only
        return 999.0, 0.0
    lx, ly = world_to_spec_xy(world_x, world_y, cfg)
    best = 1e9
    best_w = 0.05
    for r in spec.get('roads', []):
        d = dist_point_to_path(lx, ly, r.get('path') or [])
        if d < best:
            best = d
            best_w = float(r.get('width', 0.055))
    return best, best_w


def distance_to_settlement(world_x: float, world_y: float, cfg: dict) -> float:
    spec = cfg.get('_zone_spec')
    if not spec:
        return 999.0
    lx, ly = world_to_spec_xy(world_x, world_y, cfg)
    best = 1e9
    for s in spec.get('settlements', []):
        c = s.get('center', [0, 0])
        d = math.hypot(lx - float(c[0]), ly - float(c[1])) - float(s.get('radius', 0.25))
        best = min(best, d)
    return best


def smoothstep(edge0: float, edge1: float, x: float) -> float:
    if edge0 == edge1:
        return 1.0 if x >= edge1 else 0.0
    t = max(0.0, min(1.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3.0 - 2.0 * t)


def texture_role_weights(world_x: float, world_y: float, cfg: dict) -> Dict[str, float]:
    h = height_func(world_x, world_y, cfg)
    slope = estimate_slope(world_x, world_y, cfg)
    road_d, road_w = distance_to_roads_spec(world_x, world_y, cfg)
    water_d, _wf = spec_water_distance(world_x, world_y, cfg) if cfg.get('_zone_spec') else (river_distance(world_x, world_y, cfg) / ADT_SIZE, None)
    settle_d = distance_to_settlement(world_x, world_y, cfg)

    # broad stable masks. They are deliberately non-noisy: Blizzlike terrain usually has big readable shapes, not speckles.
    road = 1.0 - smoothstep(road_w * 0.55, road_w * 1.55, road_d)
    rock = smoothstep(21.0, 38.0, slope)
    shore = 1.0 - smoothstep(0.00, 0.105, abs(water_d))
    snow = smoothstep(112.0, 155.0, h) * (1.0 - road)
    forest_noise = 0.5 + 0.5 * math.sin(world_x / 68.0 + math.cos(world_y / 91.0))
    forest = max(0.0, min(1.0, (0.32 + 0.38 * forest_noise) * (1.0 - road) * (1.0 - shore) * (1.0 - smoothstep(-0.08, 0.20, settle_d))))
    plague = 0.0
    spec = cfg.get('_zone_spec') or {}
    if '_is_plague_theme' not in cfg:
        text = (str(spec.get('creative_intent', '')) + ' ' + str(spec.get('object_intent', '')) + ' ' + str(spec.get('zone_name', ''))).lower()
        cfg['_is_plague_theme'] = ('plague' in text or 'undead' in text or 'haunted' in text or 'crypt' in text)
    if cfg.get('_is_plague_theme'):
        lx, ly = world_to_spec_xy(world_x, world_y, cfg) if cfg.get('_zone_spec') else (0, 0)
        # favor northern/eastern hostile areas.
        _ox,_oy,sx,sy = spec_bounds(cfg) if cfg.get('_zone_spec') else (0,0,1,1)
        plague = smoothstep(0.55, 0.85, (lx / max(0.001, sx) + (1.0 - ly / max(0.001, sy))) * 0.5) * (1-road) * 0.55

    base = max(0.15, 1.0 - max(road, rock * 0.65, shore * 0.7, snow * 0.75, plague * 0.7))
    return {
        'base': base,
        'road': road,
        'rock': rock,
        'shore': shore,
        'forest': forest,
        'snow': snow,
        'sand': shore * 0.75,
        'plague': plague,
    }


def choose_chunk_layers(tile_x: int, tile_y: int, chunk_x: int, chunk_y: int, cfg: dict) -> List[Dict[str, Any]]:
    catalog = texture_catalog(cfg)
    role_to_item: Dict[str, Dict[str, Any]] = {}
    for i, item in enumerate(catalog):
        role_to_item.setdefault(item['role'], dict(item, id=i))
    base_item = role_to_item.get('base') or dict(catalog[0], id=0)
    scores = {r: 0.0 for r in role_to_item.keys()}
    samples = 0
    base_x, base_y = chunk_base_world(tile_x, tile_y, chunk_x, chunk_y)
    # v0.7 speed: layer choice only needs broad masks. Full 8x8 sampling
    # made 4x4 ADT prompt runs painfully slow; 4x4 samples per MCNK is enough
    # to pick which <=4 roles belong in the chunk, while MCAL still carries the blend.
    choice_stride = int(cfg.get('texture_painting', {}).get('layer_choice_stride', 4))
    choice_stride = max(1, min(4, choice_stride))
    for sy in range(0, 8, choice_stride):
        for sx in range(0, 8, choice_stride):
            wx = base_x - (sx + 0.5) * UNIT
            wy = base_y - (sy + 0.5) * UNIT
            w = texture_role_weights(wx, wy, cfg)
            for r, val in w.items():
                if r in scores:
                    scores[r] += float(val)
            samples += 1
    for r in list(scores):
        scores[r] /= max(1, samples)
    extras = []
    for role, score in scores.items():
        if role == 'base' or role not in role_to_item:
            continue
        item = role_to_item[role]
        priority = float(item.get('priority', 0.0))
        # road is kept more aggressively because broken roads look awful.
        keep_score = score + priority * 0.07 + (0.08 if role == 'road' and score > 0.025 else 0.0)
        if keep_score > 0.035:
            extras.append((keep_score, role, item))
    extras.sort(reverse=True, key=lambda x: x[0])
    max_layers = int(cfg.get('texture_painting', {}).get('max_layers_per_chunk', 4))
    max_layers = max(1, min(4, max_layers))
    chosen_items = [base_item] + [it for _, _, it in extras[:max_layers-1]]
    order = {'base': 0, 'forest': 1, 'sand': 1, 'shore': 2, 'plague': 2, 'snow': 2, 'rock': 3, 'road': 4}
    chosen_items[1:] = sorted(chosen_items[1:], key=lambda it: order.get(it['role'], 2))
    return chosen_items


def alpha_rle(data: bytes) -> bytes:
    """Compress one 64x64 8-bit MCAL alpha map using WotLK RLE control bytes."""
    if len(data) != 4096:
        raise ValueError('alpha_rle expects 4096 bytes')
    out = bytearray(); i = 0; n = len(data)
    while i < n:
        b = data[i]
        run = 1
        while i + run < n and data[i + run] == b and run < 127:
            run += 1
        if run >= 3:
            out.append(0x80 | run); out.append(b); i += run; continue
        start = i; copy = 0
        while i < n and copy < 127:
            b2 = data[i]
            run2 = 1
            while i + run2 < n and data[i + run2] == b2 and run2 < 3:
                run2 += 1
            if run2 >= 3:
                break
            i += 1; copy += 1
        if copy:
            out.append(copy); out.extend(data[start:start+copy])
    return bytes(out)


def alpha_value_for_role(world_x: float, world_y: float, role: str, cfg: dict) -> int:
    w = texture_role_weights(world_x, world_y, cfg).get(role, 0.0)
    # Boost important masks but clamp broad texture so it does not blot out the whole chunk.
    if role == 'road':
        w = smoothstep(0.05, 0.72, w)
    elif role == 'rock':
        w = smoothstep(0.08, 0.82, w) * 0.92
    elif role in ('shore', 'sand'):
        w = smoothstep(0.10, 0.74, w) * 0.78
    elif role == 'forest':
        w = smoothstep(0.18, 0.85, w) * 0.55
    else:
        w = smoothstep(0.12, 0.85, w) * 0.70
    return max(0, min(255, int(round(w * 255.0))))


def alpha_map_for_role(tile_x: int, tile_y: int, chunk_x: int, chunk_y: int, role: str, cfg: dict) -> bytes:
    base_x, base_y = chunk_base_world(tile_x, tile_y, chunk_x, chunk_y)
    raw = bytearray(4096)
    step = int(cfg.get('texture_painting', {}).get('alpha_sample_step', 4))
    step = max(1, min(64, step))
    # 64x64 alpha texels across one MCNK. We sample in small blocks for speed;
    # this still produces smooth broad masks after the weight functions.
    for ay in range(0, 64, step):
        for ax in range(0, 64, step):
            wx = base_x - ((ax + step * 0.5) / 64.0) * CHUNK_SIZE
            wy = base_y - ((ay + step * 0.5) / 64.0) * CHUNK_SIZE
            val = alpha_value_for_role(wx, wy, role, cfg)
            for yy in range(ay, min(64, ay + step)):
                row = yy * 64
                for xx in range(ax, min(64, ax + step)):
                    raw[row + xx] = val
    return bytes(raw)


def build_texture_chunks_for_mcnk(tile_x: int, tile_y: int, chunk_x: int, chunk_y: int, cfg: dict) -> Tuple[bytes, bytes, int, int, int, bytes]:
    if not cfg.get('texture_painting', {}).get('enabled', True):
        mcly_data = u32(0) + u32(0) + u32(0) + u32(0xFFFF)
        return chunk('MCLY', mcly_data), chunk('MCAL', b''), 1, 8, 0, bytes(16)
    layers = choose_chunk_layers(tile_x, tile_y, chunk_x, chunk_y, cfg)
    extra_roles = [item['role'] for item in layers[1:]]

    # Build all selected alpha maps in one sampling pass. This is much faster than
    # recomputing terrain/slope/road fields once per layer.
    alpha_raw: Dict[str, bytearray] = {role: bytearray(4096) for role in extra_roles}
    if extra_roles:
        base_x, base_y = chunk_base_world(tile_x, tile_y, chunk_x, chunk_y)
        step = int(cfg.get('texture_painting', {}).get('alpha_sample_step', 4))
        step = max(1, min(64, step))
        for ay in range(0, 64, step):
            for ax in range(0, 64, step):
                wx = base_x - ((ax + step * 0.5) / 64.0) * CHUNK_SIZE
                wy = base_y - ((ay + step * 0.5) / 64.0) * CHUNK_SIZE
                weights = texture_role_weights(wx, wy, cfg)
                vals = {}
                for role in extra_roles:
                    w = weights.get(role, 0.0)
                    if role == 'road':
                        w = smoothstep(0.05, 0.72, w)
                    elif role == 'rock':
                        w = smoothstep(0.08, 0.82, w) * 0.92
                    elif role in ('shore', 'sand'):
                        w = smoothstep(0.10, 0.74, w) * 0.78
                    elif role == 'forest':
                        w = smoothstep(0.18, 0.85, w) * 0.55
                    else:
                        w = smoothstep(0.12, 0.85, w) * 0.70
                    vals[role] = max(0, min(255, int(round(w * 255.0))))
                # Noggit's painting code keeps total alpha around 255. Keep some
                # base layer visible and prevent extra alpha layers from summing
                # far beyond 255, which causes muddy overblending in game.
                total_extra = sum(vals.values())
                if total_extra > 245:
                    scale = 245.0 / max(1, total_extra)
                    vals = {role: int(round(val * scale)) for role, val in vals.items()}
                for yy in range(ay, min(64, ay + step)):
                    row = yy * 64
                    for xx in range(ax, min(64, ax + step)):
                        off = row + xx
                        for role, val in vals.items():
                            alpha_raw[role][off] = val

    mcal_payload = bytearray()
    mcly_payload = bytearray()
    for li, item in enumerate(layers):
        tex_id = int(item.get('id', 0))
        flags = 0
        offset = 0
        if li > 0:
            alpha = bytes(alpha_raw.get(item['role'], bytearray(4096)))
            mode = str(cfg.get('texture_painting', {}).get('alpha_mode', 'raw_4096')).lower()
            flags |= 0x100  # use alpha map
            offset = len(mcal_payload)
            if mode in ('rle', 'compressed'):
                alpha = alpha_rle(alpha)
                flags |= 0x200  # compressed alpha map
            mcal_payload.extend(alpha)
        # Noggit's ENTRY_MCLY default effectID is 0xFFFF.
        mcly_payload.extend(u32(tex_id) + u32(flags) + u32(offset) + u32(0xFFFF))
    # Build the MCNK low-quality texture map. Noggit stores 64 two-bit entries
    # packed into 16 bytes, highest bits first per byte. This controls distant/LQ
    # terrain texturing. v0.10 left it all zero, making distant terrain use only
    # layer 0; v0.11 derives the dominant layer per 8x8 mini-cell.
    lq = bytearray(16)
    base_x, base_y = chunk_base_world(tile_x, tile_y, chunk_x, chunk_y)
    for ly in range(8):
        for lx in range(8):
            wx = base_x - (lx + 0.5) * (CHUNK_SIZE / 8.0)
            wy = base_y - (ly + 0.5) * (CHUNK_SIZE / 8.0)
            weights = texture_role_weights(wx, wy, cfg)
            vals = [0] * len(layers)
            for li, item in enumerate(layers):
                role = item['role']
                if li == 0:
                    continue
                w = weights.get(role, 0.0)
                if role == 'road':
                    w = smoothstep(0.05, 0.72, w)
                elif role == 'rock':
                    w = smoothstep(0.08, 0.82, w) * 0.92
                elif role in ('shore', 'sand'):
                    w = smoothstep(0.10, 0.74, w) * 0.78
                elif role == 'forest':
                    w = smoothstep(0.18, 0.85, w) * 0.55
                else:
                    w = smoothstep(0.12, 0.85, w) * 0.70
                vals[li] = max(0, min(255, int(round(w * 255.0))))
            extra_sum = sum(vals[1:])
            vals[0] = max(0, 255 - min(245, extra_sum))
            best = max(range(min(4, len(vals))), key=lambda ii: vals[ii]) if vals else 0
            idx = ly * 8 + lx
            array_index = idx // 4
            bit_index = (3 - (idx % 4)) * 2
            lq[array_index] |= (best & 3) << bit_index

    # Noggit saves an MCAL chunk even when the payload is empty. header.sizeAlpha
    # includes the 8-byte MCAL chunk header, not just the payload.
    mcal_chunk = chunk('MCAL', bytes(mcal_payload))
    return chunk('MCLY', bytes(mcly_payload)), mcal_chunk, len(layers), 8 + len(mcal_payload), len(layers)-1, bytes(lq)


# -----------------------------------------------------------------------------
# ADT terrain chunks
# -----------------------------------------------------------------------------


MCNK_HEADER_SIZE = 128


def pack_mcnk_position(base_x: float, base_y: float, base_height: float) -> bytes:
    """Pack MCNK raw position as zpos, xpos, ypos.

    Noggit's MapHeaders.h defines the fields as zpos, xpos, ypos. On load,
    Noggit uses header.ypos as the vertical base height and adds MCVT deltas
    to it, then rewrites header.xpos/zpos to ZEROPOINT - chunk_origin.

    Therefore: ypos = generated vertical base height, while xpos/zpos are
    world-coordinate anchor values, not height values.
    """
    # chunk_base_world() already returns Noggit's zero-point-relative terrain
    # coordinates: ZEROPOINT - (tile_index * TILESIZE + chunk_index * CHUNKSIZE).
    # Do NOT subtract these from WORLD_HALF again; that would turn tile 32 into
    # raw 17066-ish coordinates instead of the expected near-zero origin.
    zpos = base_y
    xpos = base_x
    ypos = base_height
    return f32(zpos) + f32(xpos) + f32(ypos)


def mcnk_vertex_positions(tile_x: int, tile_y: int, chunk_x: int, chunk_y: int) -> List[Tuple[float, float]]:
    base_x, base_y = chunk_base_world(tile_x, tile_y, chunk_x, chunk_y)
    out=[]
    for row in range(17):
        if row % 2 == 0:
            y_off = (row // 2) * UNIT
            for col in range(9):
                x_off = col * UNIT
                out.append((base_x - x_off, base_y - y_off))
        else:
            y_off = (row // 2) * UNIT + UNIT / 2.0
            for col in range(8):
                x_off = col * UNIT + UNIT / 2.0
                out.append((base_x - x_off, base_y - y_off))
    assert len(out)==145
    return out


def build_mcnr_normals_from_heights(heights: List[float]) -> bytes:
    """Write approximate per-vertex MCNR normals from the already-sampled MCVT data.

    This avoids millions of extra height_func calls on 4x4 ADT generations while
    still producing per-vertex, terrain-following normals instead of the old one
    repeated normal per MCNK. Disk order follows Noggit: normal.x, normal.z, normal.y.
    """
    rows: List[List[float]] = []
    idx = 0
    for row in range(17):
        count = 9 if row % 2 == 0 else 8
        rows.append(heights[idx:idx+count])
        idx += count
    assert idx == 145

    def h_at(r: int, c: int) -> float:
        r = max(0, min(16, r))
        c = max(0, min(len(rows[r]) - 1, c))
        return rows[r][c]

    out = bytearray()
    for r, row_vals in enumerate(rows):
        for c, h in enumerate(row_vals):
            left = h_at(r, c - 1)
            right = h_at(r, c + 1)
            up = h_at(r - 1, c)
            down = h_at(r + 1, c)
            # Horizontal neighbors are roughly UNIT apart; row neighbors are
            # half-UNIT apart in the 17-row MCVT layout. This is approximate but
            # source-compatible in byte layout and much better than flat normals.
            dhdx = (right - left) / (2.0 * UNIT)
            dhdy = (down - up) / max(UNIT, 1e-6)
            nx, ny, nz = -dhdx, 1.0, -dhdy
            inv = 1.0 / max(1e-6, math.sqrt(nx*nx + ny*ny + nz*nz))
            for v in (nx * inv, nz * inv, ny * inv):
                out.extend(struct.pack('<b', max(-127, min(127, int(round(v * 127.0))))))
    assert len(out) == 145 * 3
    return bytes(out)

def sample_mcvtheights(tile_x: int, tile_y: int, chunk_x: int, chunk_y: int, cfg: dict):
    base_x, base_y = chunk_base_world(tile_x, tile_y, chunk_x, chunk_y)
    h0 = height_func(base_x, base_y, cfg)
    heights = []
    for row in range(17):
        if row % 2 == 0:
            count = 9
            y_off = (row // 2) * UNIT
            for col in range(count):
                x_off = col * UNIT
                h = height_func(base_x - x_off, base_y - y_off, cfg)
                heights.append(h - h0)
        else:
            count = 8
            y_off = (row // 2) * UNIT + UNIT / 2.0
            for col in range(count):
                x_off = col * UNIT + UNIT / 2.0
                h = height_func(base_x - x_off, base_y - y_off, cfg)
                heights.append(h - h0)
    assert len(heights) == 145
    return h0, heights


def build_mcnk(tile_x: int, tile_y: int, chunk_x: int, chunk_y: int, cfg: dict) -> bytes:
    base_x, base_y = chunk_base_world(tile_x, tile_y, chunk_x, chunk_y)
    base_z, heights = sample_mcvtheights(tile_x, tile_y, chunk_x, chunk_y, cfg)

    mcvt_data = b''.join(f32(h) for h in heights)
    mcvt = chunk('MCVT', mcvt_data)

    # Terrain-derived normals. MCNR payload is 145 * int8[3]. Noggit writes
    # 13 unknown padding bytes after the MCNR chunk.
    mcnr = chunk('MCNR', build_mcnr_normals_from_heights(heights))
    mcnr_unknown = b'\0' * 13

    # Automatic terrain texture layers and alphamaps.
    mcly, mcal, n_layers, size_alpha, alpha_layers, low_quality_texture_map = build_texture_chunks_for_mcnk(tile_x, tile_y, chunk_x, chunk_y, cfg)

    subchunks = []
    ofs_mcvt = 8 + MCNK_HEADER_SIZE
    subchunks.append(mcvt)
    ofs_mcnr = ofs_mcvt + len(mcvt)
    subchunks.append(mcnr)
    subchunks.append(mcnr_unknown)
    ofs_mcly = ofs_mcnr + len(mcnr) + len(mcnr_unknown)
    subchunks.append(mcly)
    mcrf = chunk('MCRF', b'')
    ofs_mcrf = ofs_mcly + len(mcly)
    subchunks.append(mcrf)
    ofs_mcal = ofs_mcrf + len(mcrf)
    subchunks.append(mcal)
    mcse = chunk('MCSE', b'')
    ofs_mcse = ofs_mcal + len(mcal)
    subchunks.append(mcse)
    sub_data = b''.join(subchunks)

    flags = (1 << 15)  # do_not_fix_alpha_map, matching Noggit MH2O/big-alpha saves
    n_doodad_refs = 0
    # ofs_mcrf/ofs_mcse/ofs_mcal are computed in the subchunk layout above.
    ofs_mcsh = 0
    size_shadow = 0
    area_id = int(cfg.get('area_id', 0))
    n_map_obj_refs = 0
    holes = 0
    n_snd_emitters = 0
    ofs_mclq = 0
    size_liquid = 8  # convention for no legacy MCLQ liquid; WotLK water comes from MH2O.
    position = pack_mcnk_position(base_x, base_y, base_z)
    ofs_mccv = 0
    ofs_mclv = 0
    unused = 0

    header = b''.join([
        u32(flags),
        u32(chunk_x),
        u32(chunk_y),
        u32(n_layers),
        u32(n_doodad_refs),
        u32(ofs_mcvt),
        u32(ofs_mcnr),
        u32(ofs_mcly),
        u32(ofs_mcrf),
        u32(ofs_mcal),
        u32(size_alpha),
        u32(ofs_mcsh),
        u32(size_shadow),
        u32(area_id),
        u32(n_map_obj_refs),
        u32(holes),
        low_quality_texture_map,  # 16-byte ReallyLowQualityTextureingMap placeholder
        u32(0),                 # predTex
        u32(0),                 # noEffectDoodad
        u32(ofs_mcse),
        u32(n_snd_emitters),
        u32(ofs_mclq),
        u32(size_liquid),
        position,
        u32(ofs_mccv),
        u32(ofs_mclv),
        u32(unused),
    ])
    # WotLK/3.3.5a MCNK header is 128 bytes, matching Noggit's MapChunkHeader.
    # The holes field is a full uint32; do not split it into halfwords.
    assert len(header) == MCNK_HEADER_SIZE, len(header)
    data = header + sub_data
    return chunk('MCNK', data)


# -----------------------------------------------------------------------------
# MH2O water generation
# -----------------------------------------------------------------------------


def water_bitmap_for_chunk(tile_x: int, tile_y: int, chunk_x: int, chunk_y: int, cfg: dict) -> int:
    """Return an 8x8 exists-bitmap for MH2O liquid quads in this MCNK."""
    bits = 0
    base_x, base_y = chunk_base_world(tile_x, tile_y, chunk_x, chunk_y)
    for qy in range(8):
        for qx in range(8):
            # Quad center within the 8x8 liquid grid.
            wx = base_x - (qx + 0.5) * UNIT
            wy = base_y - (qy + 0.5) * UNIT
            if is_water_at(wx, wy, cfg):
                bits |= 1 << (qy * 8 + qx)
    return bits


def pack_bitmap_u64(bits: int) -> bytes:
    return bytes((bits >> (8 * i)) & 0xFF for i in range(8))


def build_mh2o(tile_x: int, tile_y: int, cfg: dict) -> bytes:
    """Build a WotLK MH2O chunk in the same broad layout Noggit saves.

    v0.10 wrote a very sparse MH2O: per wet MCNK it had an info mask but no
    render attributes and no height/depth payload. Noggit's liquid_tile/liquid
    save path writes a 256-entry MH2O header table, optional render attributes,
    MH2O_Information, then per-layer height/depth payloads. v0.11 follows that
    simpler Noggit-compatible form for one flat river/ocean layer per wet MCNK.
    Empty tiles return b'' so MHDR.mh2o can be zero instead of pointing at an
    empty 8-byte MH2O chunk that loaders may try to parse as 256 headers.
    """
    water = cfg.get('water', {})
    if not water.get('enabled', True):
        return b''

    water_level = float(water.get('level', 38.0))
    liquid_type = int(water.get('liquid_type', 1))
    lvf = int(water.get('liquid_vertex_format', 0))

    header_entries: List[Tuple[int, int, int]] = []
    tail = bytearray()
    header_size = 256 * 12
    wet_any = False

    for cy in range(16):
        for cx in range(16):
            bits = water_bitmap_for_chunk(tile_x, tile_y, cx, cy, cfg)
            if bits == 0:
                header_entries.append((0, 0, 0))
                continue
            wet_any = True

            # Header ofsRenderMask: Noggit writes MH2O_Attributes for normal
            # non-fatigue water. Use the visible bits as fishable and no fatigue.
            attr_offset = header_size + len(tail)
            tail.extend(u64(bits) + u64(0))

            info_offset = header_size + len(tail)
            info_pos_in_tail = len(tail)
            tail.extend(b'\0' * 24)  # MH2O_Information, patched below

            # For a fully filled 8x8 chunk Noggit uses ofsInfoMask=0. For partial
            # chunks, write the 64-bit mask and use width/height 8 for simplicity.
            info_mask_offset = 0
            if bits != 0xFFFFFFFFFFFFFFFF:
                info_mask_offset = header_size + len(tail)
                tail.extend(pack_bitmap_u64(bits))

            heightmap_offset = header_size + len(tail)
            # Match Noggit's liquid_layer::save payload rules:
            #   lvf 0: float heights followed by one byte depth per vertex
            #   lvf 1: float heights followed by mh2o_uv per vertex
            #   lvf 2: depth bytes only, no float height grid
            # The default generator uses lvf 0. Supporting the other two keeps
            # hand-edited configs structurally closer to Noggit.
            vertex_count = 9 * 9
            if lvf in (0, 1):
                for _ in range(vertex_count):
                    tail.extend(f32(water_level))
            if lvf == 1:
                for vz in range(9):
                    for vx in range(9):
                        # Noggit stores mh2o_uv as two uint16 values, roughly uv*255.
                        tail.extend(u16(int((vx / 4.0) * 255)))
                        tail.extend(u16(int((vz / 4.0) * 255)))
            if lvf in (0, 2):
                for _ in range(vertex_count):
                    tail.extend(u8(255))

            info = b''.join([
                u16(liquid_type),
                u16(lvf),
                f32(water_level),
                f32(water_level),
                u8(0), u8(0), u8(8), u8(8),
                u32(info_mask_offset),
                u32(heightmap_offset),
            ])
            assert len(info) == 24
            tail[info_pos_in_tail:info_pos_in_tail+24] = info
            header_entries.append((info_offset, 1, attr_offset))

    if not wet_any:
        return b''
    headers = b''.join(u32(off) + u32(count) + u32(attr) for off, count, attr in header_entries)
    assert len(headers) == header_size
    return chunk('MH2O', headers + bytes(tail))


# -----------------------------------------------------------------------------
# ADT/WDT/WDL containers
# -----------------------------------------------------------------------------


def build_adt(tile_x: int, tile_y: int, cfg: dict) -> bytes:
    # MTEX contains all candidate terrain textures used by MCLY layers.
    tex_blob = b''.join(t['path'].encode('ascii', errors='replace') + b'\0' for t in texture_catalog(cfg))

    mver = chunk('MVER', u32(18))
    mcin_placeholder = chunk('MCIN', b'\0' * (256 * 16))
    # Do NOT pad MTEX: Noggit reads NUL-terminated texture strings until
    # the exact MTEX chunk size. Extra NUL padding can be interpreted as
    # empty texture filenames. Other chunks do not require 4-byte alignment here.
    mtex = chunk('MTEX', tex_blob)
    mmdx = chunk('MMDX', b'')
    mmid = chunk('MMID', b'')
    mwmo = chunk('MWMO', b'')
    mwid = chunk('MWID', b'')
    mddf = chunk('MDDF', b'')
    modf = chunk('MODF', b'')
    mh2o = build_mh2o(tile_x, tile_y, cfg)
    mtfx = b''  # WotLK does not need a texture-effects chunk for this generator.

    # Layout with blank MHDR first.
    mhdr = chunk('MHDR', b'\0' * 64)
    pre = mver + mhdr
    mhdr_data_start = len(mver) + 8  # offset to MHDR payload start

    # Compute offsets relative to MHDR data start, pointing at chunk headers.
    pos = len(pre)
    offsets = {}
    metadata_chunks = [('MCIN', mcin_placeholder), ('MTEX', mtex), ('MMDX', mmdx), ('MMID', mmid), ('MWMO', mwmo), ('MWID', mwid), ('MDDF', mddf), ('MODF', modf)]
    if mh2o:
        metadata_chunks.append(('MH2O', mh2o))
    for name, c in metadata_chunks:
        offsets[name] = pos - mhdr_data_start
        pos += len(c)

    # MCNKs start after metadata chunks; need MCIN entries.
    mcnks = []
    mcin_entries = []
    mcnk_pos = pos
    for cy in range(16):
        for cx in range(16):
            c = build_mcnk(tile_x, tile_y, cx, cy, cfg)
            mcin_entries.append((mcnk_pos, len(c), 0, 0))
            mcnks.append(c)
            mcnk_pos += len(c)

    mcin_data = b''.join(u32(off) + u32(size) + u32(flags) + u32(async_id) for off, size, flags, async_id in mcin_entries)
    mcin = chunk('MCIN', mcin_data)

    mhdr_payload = b''.join([
        u32(0),
        u32(offsets['MCIN']),
        u32(offsets['MTEX']),
        u32(offsets['MMDX']),
        u32(offsets['MMID']),
        u32(offsets['MWMO']),
        u32(offsets['MWID']),
        u32(offsets['MDDF']),
        u32(offsets['MODF']),
        u32(0),                # MFBO none
        u32(offsets.get('MH2O', 0)),
        u32(0),                # MTXF/MTFX omitted; not required for this terrain-only WotLK output,
        u32(0), u32(0), u32(0), u32(0)
    ])
    assert len(mhdr_payload) == 64
    mhdr = chunk('MHDR', mhdr_payload)
    return mver + mhdr + mcin + mtex + mmdx + mmid + mwmo + mwid + mddf + modf + mh2o + b''.join(mcnks)


def build_wdt(map_name: str, present_tiles: set[Tuple[int, int]], cfg: Optional[dict] = None) -> bytes:
    mver = chunk('MVER', u32(18))
    # Noggit treats MPHD flag 0x04 as the big-alpha WDT flag and also
    # ensures FLAG_SHADING (0x02) is enabled when loading/saving a WDT.
    # In auto mode, generated textured maps should therefore use 0x06.
    mphd_cfg = (cfg or {}).get('wdt', {}).get('mphd_flags', 'auto')
    if isinstance(mphd_cfg, str) and mphd_cfg.lower() == 'auto':
        mphd_flags = 0x02
        if (cfg or {}).get('texture_painting', {}).get('enabled', True):
            mphd_flags |= 0x04
    else:
        mphd_flags = int(mphd_cfg or 0)
        # Keep Noggit-compatible shading on unless the caller explicitly disables it
        # by also setting wdt.force_exact_mphd_flags=true.
        if not (cfg or {}).get('wdt', {}).get('force_exact_mphd_flags', False):
            mphd_flags |= 0x02
    mphd = chunk('MPHD', b''.join([u32(mphd_flags)] + [u32(0)] * 7))
    main_entries = []
    for y in range(64):
        for x in range(64):
            flags = 1 if (x, y) in present_tiles else 0
            main_entries.append(u32(flags) + u32(0))
    main = chunk('MAIN', b''.join(main_entries))
    return mver + mphd + main


def build_mare(tile_x: int, tile_y: int, cfg: dict) -> bytes:
    outer: List[bytes] = []
    inner: List[bytes] = []
    # 17x17 outer low-res grid at ADT chunk corners.
    for oy in range(17):
        for ox in range(17):
            wx, wy = sample_world_in_tile(tile_x, tile_y, ox * CHUNK_SIZE, oy * CHUNK_SIZE)
            outer.append(i16(height_func(wx, wy, cfg)))
    # 16x16 inner low-res grid at chunk centers.
    for oy in range(16):
        for ox in range(16):
            wx, wy = sample_world_in_tile(tile_x, tile_y, (ox + 0.5) * CHUNK_SIZE, (oy + 0.5) * CHUNK_SIZE)
            inner.append(i16(height_func(wx, wy, cfg)))
    return chunk('MARE', b''.join(outer + inner))


def build_wdl(present_tiles: set[Tuple[int, int]], cfg: dict) -> bytes:
    """Build a minimal WDL: MVER + MAOF + MARE/MAHO for every present ADT."""
    mver = chunk('MVER', u32(18))
    maof_payload = bytearray(4096 * 4)
    area_chunks = bytearray()

    # MAOF is absolute file offsets to each MARE chunk header. The MARE chunks start
    # immediately after MVER + MAOF.
    base_after_maof = len(mver) + 8 + len(maof_payload)
    for y in range(64):
        for x in range(64):
            if (x, y) not in present_tiles:
                continue
            abs_off = base_after_maof + len(area_chunks)
            struct.pack_into('<I', maof_payload, (y * 64 + x) * 4, abs_off)
            area_chunks.extend(build_mare(x, y, cfg))
            # MAHO is optional WDL hole data. This generator does not create
            # terrain holes, so omit MAHO entirely instead of writing a
            # potentially inverted all-ones mask. Noggit's horizon loader reads
            # MARE by MAOF offsets and explicitly leaves MAHO as a TODO, while
            # external WDL parsers document MAHO as optional hole bitmasks.

    return mver + chunk('MAOF', bytes(maof_payload)) + bytes(area_chunks)


# -----------------------------------------------------------------------------
# Minimap generation: BLP2 RAW1 + TGA previews + md5translate.trs
# -----------------------------------------------------------------------------


def lerp(a: int, b: int, t: float) -> int:
    return int(round(a + (b - a) * max(0.0, min(1.0, t))))


def rgb_lerp(c1: Tuple[int, int, int], c2: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    return tuple(lerp(c1[i], c2[i], t) for i in range(3))  # type: ignore


def make_minimap_palette() -> List[Tuple[int, int, int, int]]:
    """Return 256 BGRA palette entries."""
    pal: List[Tuple[int, int, int, int]] = []
    # Terrain gradient: low muddy green -> grass -> high tan/rock.
    stops = [
        (0.00, (52, 70, 40)),
        (0.35, (72, 105, 49)),
        (0.62, (112, 126, 70)),
        (0.82, (120, 105, 83)),
        (1.00, (154, 150, 135)),
    ]
    for i in range(128):
        t = i / 127.0
        for si in range(len(stops) - 1):
            if stops[si][0] <= t <= stops[si + 1][0]:
                local = (t - stops[si][0]) / (stops[si + 1][0] - stops[si][0])
                r, g, b = rgb_lerp(stops[si][1], stops[si + 1][1], local)
                pal.append((b, g, r, 255))
                break
    # Water palette 128-159.
    for i in range(32):
        t = i / 31.0
        r, g, b = rgb_lerp((35, 70, 96), (76, 120, 145), t)
        pal.append((b, g, r, 255))
    # Shore/sand/mud 160-191.
    for i in range(32):
        t = i / 31.0
        r, g, b = rgb_lerp((85, 78, 55), (135, 122, 80), t)
        pal.append((b, g, r, 255))
    # Reserved/extra 192-255.
    while len(pal) < 256:
        pal.append((0, 0, 0, 255))
    return pal


def minimap_index_and_rgb(tile_x: int, tile_y: int, px: int, py: int, cfg: dict, palette: List[Tuple[int, int, int, int]]) -> Tuple[int, Tuple[int, int, int]]:
    local_x = (px + 0.5) / MINIMAP_SIZE * ADT_SIZE
    local_y = (py + 0.5) / MINIMAP_SIZE * ADT_SIZE
    wx, wy = sample_world_in_tile(tile_x, tile_y, local_x, local_y)
    h = height_func(wx, wy, cfg)

    # v0.4 speed note: minimap uses single-sample height shading instead of 5x
    # derivative sampling. This keeps 4x4/6x6 zone generation practical in Python.
    shade = (math.sin(wx / 95.0) + math.cos(wy / 115.0)) * 0.025

    if is_water_at(wx, wy, cfg):
        # Deeper/darker in lower parts.
        depth = max(0.0, min(1.0, (float(cfg.get('water', {}).get('level', 38.0)) - h + 3.0) / 18.0))
        idx = 128 + int(depth * 31)
        b, g, r, _ = palette[idx]
        return idx, (r, g, b)

    # Muddy shore pixels near water line/river, even if not liquid.
    if (river_distance(wx, wy, cfg) <= 0.08 if cfg.get('_zone_spec') else river_distance(wx, wy) <= float(cfg.get('water', {}).get('river_width', 27.0)) + 10.0):
        t = max(0.0, min(1.0, (h - 25.0) / 35.0))
        idx = 160 + int(t * 31)
        b, g, r, _ = palette[idx]
        return idx, (r, g, b)

    # Map rough generated height range to terrain palette.
    t = max(0.0, min(1.0, (h + 20.0) / 160.0 + shade))
    idx = int(t * 127)
    b, g, r, _ = palette[idx]
    return idx, (r, g, b)


def write_blp_raw1(path: Path, indices: bytes, width: int, height: int, palette: List[Tuple[int, int, int, int]]):
    """Write simple BLP2 RAW1/paletted texture with no mipmaps and no alpha."""
    assert len(indices) == width * height
    assert len(palette) == 256
    header_size = 4 + 4 + 1 + 1 + 1 + 1 + 4 + 4 + 16 * 4 + 16 * 4
    palette_size = 256 * 4
    data_offset = header_size + palette_size
    offsets = [0] * 16
    lengths = [0] * 16
    offsets[0] = data_offset
    lengths[0] = len(indices)
    header = bytearray()
    header.extend(b'BLP2')
    header.extend(u32(1))      # type: Direct/BLP/DXTC/uncompressed
    header.extend(u8(1))       # compression: RAW1 paletted
    header.extend(u8(0))       # alpha bits
    header.extend(u8(0))       # alpha type
    header.extend(u8(0))       # no mips
    header.extend(u32(width))
    header.extend(u32(height))
    for o in offsets: header.extend(u32(o))
    for l in lengths: header.extend(u32(l))
    for b, g, r, a in palette:
        header.extend(bytes([b, g, r, a]))
    assert len(header) == data_offset
    write_file(path, bytes(header) + indices)


def write_tga(path: Path, pixels_rgb: List[Tuple[int, int, int]], width: int, height: int):
    """Write uncompressed 24-bit top-left-origin TGA preview."""
    header = bytearray(18)
    header[2] = 2  # uncompressed true-color
    header[12:14] = struct.pack('<H', width)
    header[14:16] = struct.pack('<H', height)
    header[16] = 24
    header[17] = 0x20  # top-left origin
    data = bytearray()
    for r, g, b in pixels_rgb:
        data.extend(bytes([b, g, r]))
    write_file(path, bytes(header) + bytes(data))


def build_minimap_tile(tile_x: int, tile_y: int, cfg: dict, palette: List[Tuple[int, int, int, int]]) -> Tuple[bytes, List[Tuple[int, int, int]]]:
    # Fast block sampling. 256x256 output is preserved, but we sample the terrain
    # every N pixels and fill a block. This makes large AI-planned regions practical.
    step = int(cfg.get('minimap', {}).get('sample_step', 4))
    step = max(1, min(64, step))
    idx_grid = [[0 for _ in range(MINIMAP_SIZE)] for __ in range(MINIMAP_SIZE)]
    rgb_grid: List[List[Tuple[int, int, int]]] = [[(0, 0, 0) for _ in range(MINIMAP_SIZE)] for __ in range(MINIMAP_SIZE)]
    for py in range(0, MINIMAP_SIZE, step):
        for px in range(0, MINIMAP_SIZE, step):
            idx, rgb = minimap_index_and_rgb(tile_x, tile_y, min(px + step // 2, MINIMAP_SIZE - 1), min(py + step // 2, MINIMAP_SIZE - 1), cfg, palette)
            for yy in range(py, min(py + step, MINIMAP_SIZE)):
                row_idx = idx_grid[yy]
                row_rgb = rgb_grid[yy]
                for xx in range(px, min(px + step, MINIMAP_SIZE)):
                    row_idx[xx] = idx
                    row_rgb[xx] = rgb
    indices = bytearray()
    rgb_pixels: List[Tuple[int, int, int]] = []
    for py in range(MINIMAP_SIZE):
        indices.extend(idx_grid[py])
        rgb_pixels.extend(rgb_grid[py])
    return bytes(indices), rgb_pixels


def minimap_output_name(map_name: str, tile_x: int, tile_y: int) -> str:
    # Simple deterministic non-hash filename. The md5translate.trs maps the logical
    # <Map>\mapX_Y.blp request to this physical file in Textures\Minimap.
    return f'{map_name}{tile_x:02d}{tile_y:02d}.blp'


def generate_minimaps(out: Path, map_name: str, tiles_x: Iterable[int], tiles_y: Iterable[int], cfg: dict):
    mini_cfg = cfg.get('minimap', {})
    if not mini_cfg.get('enabled', True):
        return []

    palette = make_minimap_palette()
    trs_lines = [f'dir: {map_name}']

    for y in tiles_y:
        for x in tiles_x:
            indices, rgb_pixels = build_minimap_tile(x, y, cfg, palette)
            physical_name = minimap_output_name(map_name, x, y)
            write_blp_raw1(out / 'Textures' / 'Minimap' / physical_name, indices, MINIMAP_SIZE, MINIMAP_SIZE, palette)
            if mini_cfg.get('write_world_minimaps_copy', True):
                write_blp_raw1(out / 'World' / 'Minimaps' / map_name / f'map{x}_{y}.blp', indices, MINIMAP_SIZE, MINIMAP_SIZE, palette)
            if mini_cfg.get('write_tga_previews', True):
                write_tga(out / 'minimap_previews' / map_name / f'map{x}_{y}.tga', rgb_pixels, MINIMAP_SIZE, MINIMAP_SIZE)
            trs_lines.append(f'{map_name}\\map{x}_{y}.blp\t{physical_name}\t')

    return trs_lines


def write_md5translate(out: Path, fragments: List[str]):
    # This is a minimal test md5translate. For a real patch, merge these lines into
    # an extracted original md5translate.trs instead of replacing the whole file.
    if not fragments:
        return
    text = '\r\n'.join(fragments) + '\r\n'
    write_file(out / 'Textures' / 'Minimap' / 'md5translate.trs', text.encode('ascii', errors='replace'))
    write_file(out / 'Textures' / 'Minimap' / 'md5translate_AIGEN_FRAGMENT.trs', text.encode('ascii', errors='replace'))


# -----------------------------------------------------------------------------
# Generation orchestration
# -----------------------------------------------------------------------------


def generate(cfg: dict):
    out = Path(cfg.get('output_dir', 'output_loose'))
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    tiles_x = list(cfg.get('tiles_x', [32, 33]))
    tiles_y = list(cfg.get('tiles_y', [32, 33]))
    map_name = cfg.get('map_name', 'aigen')
    present = {(x, y) for y in tiles_y for x in tiles_x}
    mode = cfg.get('mode', 'both')

    trs_fragments: List[str] = []

    if mode in ('both', 'custom'):
        base = out / 'world' / 'maps' / map_name
        write_file(base / f'{map_name}.wdt', build_wdt(map_name, present, cfg))
        if cfg.get('wdl', {}).get('enabled', True):
            write_file(base / f'{map_name}.wdl', build_wdl(present, cfg))
        for y in tiles_y:
            for x in tiles_x:
                write_file(base / f'{map_name}_{x}_{y}.adt', build_adt(x, y, cfg))
        trs_fragments.extend(generate_minimaps(out, map_name, tiles_x, tiles_y, cfg))

    if mode in ('both', 'azeroth_override'):
        base = out / 'world' / 'maps' / 'azeroth'
        for y in tiles_y:
            for x in tiles_x:
                write_file(base / f'azeroth_{x}_{y}.adt', build_adt(x, y, cfg))
        # Minimap tiles for the override are useful, but we deliberately do NOT generate
        # a replacement Azeroth.wdl by default because that would replace the full continent WDL.
        trs_fragments.extend(generate_minimaps(out, 'Azeroth', tiles_x, tiles_y, cfg))

    if cfg.get('minimap', {}).get('write_md5translate', True):
        write_md5translate(out, trs_fragments)

    wet_chunks = 0
    if cfg.get('water', {}).get('enabled', True):
        # In spec mode, exact recount is redundant because MH2O was already written.
        # Avoid an extra large pass through the AI terrain function at the end.
        if cfg.get('_zone_spec'):
            wet_chunks = -1
        else:
            for y in tiles_y:
                for x in tiles_x:
                    for cy in range(16):
                        for cx in range(16):
                            if water_bitmap_for_chunk(x, y, cx, cy, cfg):
                                wet_chunks += 1

    (out / 'GENERATION_REPORT.txt').write_text(
        f"AI ADT One-Click v0.18\n"
        f"Generated {len(present)} ADT tile(s) in mode={mode}\n"
        f"Tiles: {sorted(present)}\n"
        f"Texture theme: {detect_texture_theme(cfg)}\n"
        f"Texture layers: {len(texture_catalog(cfg))} MTEX entries, auto alphamaps={cfg.get('texture_painting', {}).get('enabled', True)}\n"
        f"Water enabled: {cfg.get('water', {}).get('enabled', True)}\n"
        f"Wet MCNK chunks across custom tile set: {wet_chunks}\n"
        f"WDL enabled for custom map: {cfg.get('wdl', {}).get('enabled', True)}\n"
        f"Minimap enabled: {cfg.get('minimap', {}).get('enabled', True)}\n\n"
        "Fast visual test: pack output_loose into an MPQ patch and fly/teleport near map 0 x=0 y=0 z=100.\n"
        f"Custom map: add your own Map.dbc row whose Directory field is {map_name}, then use world/maps/{map_name} files.\n"
        "Minimap note: md5translate.trs is a minimal test file. For a real client patch, merge the generated fragment into your extracted original Textures\\Minimap\\md5translate.trs.\n",
        encoding='utf-8'
    )


def load_config(config_path: str | Path = 'config.json') -> dict:
    cfg_path = Path(config_path)
    explicit_keys: set[str] = set()
    if not cfg_path.exists():
        if cfg_path.name == 'config.json':
            cfg_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding='utf-8')
            user = {}
        else:
            raise FileNotFoundError(f'Config file not found: {cfg_path}')
    else:
        user = json.loads(cfg_path.read_text(encoding='utf-8'))
        explicit_keys = _top_level_user_keys(user)
    cfg = deep_update(DEFAULT_CONFIG, user)
    cfg['_config_path'] = str(cfg_path)
    return attach_zone_spec(cfg, explicit_keys)


def main():
    # Optional CLI form: python generate_adt.py custom_config.json
    # Previous builds ignored this argument, so small smoke configs and user
    # test configs could silently run config.json/zone_spec.json instead.
    import sys as _sys
    cfg = load_config(_sys.argv[1] if len(_sys.argv) > 1 else 'config.json')
    generate(cfg)
    print('AI ADT One-Click v0.18 finished.')
    print(f"Config: {cfg.get('_config_path','config.json')}")
    print(f"Output: {Path(cfg.get('output_dir','output_loose')).resolve()}")
    print('Generated loose patch folder: ADT/WDT + MCLY/MCAL alphamaps + MH2O water + custom WDL + minimap BLP/TRS files. If zone_spec.json exists, it supplies defaults without overriding explicit config keys.')


if __name__ == '__main__':
    main()
