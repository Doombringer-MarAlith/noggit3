#!/usr/bin/env python3
"""
AI ADT One-Click v0.19
Generates WotLK 3.3.5a ADT/WDT/WDL terrain files directly, with:
  - MH2O liquid (sloped rivers, lakes, ocean edges) with per-vertex depth fades
  - WDL low-resolution distant terrain for the custom map folder
  - 256x256 minimap BLP tiles + TGA previews + md5translate.trs fragment
  - MCLY/MCAL multi-layer texture painting with learned ground-effect ids

All terrain/water/texture data for a tile comes from one deterministic field
grid (terrain_fields.TileFields), so MCVT, MCNR, MH2O, MCAL, the low-quality
texture map, WDL MARE samples and the minimap all agree with each other and
with neighbouring tiles.

This is deliberately no-M2/no-WMO. Object placement is a separate, much harder
problem and should not block terrain/water/minimap iteration.
"""
import json
import math
import shutil
import struct
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from terrain_fields import (ADT_SIZE, CHUNK_SIZE, UNIT, WORLD_HALF, MARGIN, ROLE_ORDER,
                            ZoneModel, TileFields, rasterize_alpha, smoothstep)

MINIMAP_SIZE = 256

DEFAULT_CONFIG = {
    "map_name": "aigen",
    "tiles_x": [32, 33],
    "tiles_y": [32, 33],
    "base_texture": "Tileset\\Elwynn\\ElwynnGrassBase.blp",
    "texture_painting": {
        "enabled": True,
        "learned_rules_path": "learned_blizzlike_rules.json",
        "listfile_path": "tileset_listfile_335.txt",
        "max_layers_per_chunk": 4,
        "alpha_mode": "raw_4096",
        "breakup": 0.22
    },
    "wdt": {
        "mphd_flags": "auto"
    },
    "texture_layers": [],
    "mode": "both",  # both | custom | azeroth_override
    "height_scale": 24.0,
    "seed": 1337,
    "area_id": 0,
    "output_dir": "output_loose",
    "water": {
        "enabled": True,
        "level": 38.0,
        "liquid_type": 1,
        "ocean_liquid_type": 2,
        "liquid_vertex_format": 0,
        "river_width": 27.0
    },
    "wdl": {
        "enabled": True,
        "custom_only": True
    },
    "minimap": {
        "enabled": True,
        "tile_size": 256,
        "write_tga_previews": True,
        "write_world_minimaps_copy": False,
        "write_md5translate": True,
        "write_full_md5translate": False
    },
    "dbc": {
        "map_id": 0,
        "area_id": 0
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
    """WoW ADT/WDT/WDL chunk fourCCs are stored reversed on disk (MVER -> REVM)."""
    assert len(name) == 4
    return name[::-1].encode('ascii')


def chunk(name: str, data: bytes) -> bytes:
    return magic(name) + u32(len(data)) + data


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
# Zone spec / config plumbing
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

    Defaults may come from the spec, but explicit values in config.json or an
    alternate config file win (so smoke/small configs stay small).
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
    for k in ['mode', 'area_id', 'output_dir', 'height_scale', 'seed']:
        if k in gen and k not in explicit_keys:
            cfg[k] = gen[k]
    for section in ('water', 'wdl', 'minimap', 'dbc'):
        if section in gen:
            merged = dict(cfg.get(section, {}))
            if section not in explicit_keys:
                merged.update(gen.get(section, {}))
            cfg[section] = merged
    return cfg


# -----------------------------------------------------------------------------
# Texture catalog (MTEX) and roles
# -----------------------------------------------------------------------------

# Every path below exists in the 3.3.5a client MPQs (see tileset_listfile_335.txt);
# a texture the client cannot find renders as blank green ground.
DEFAULT_TEXTURE_LAYERS = [
    {"role": "base", "label": "grass base", "path": "Tileset\\Elwynn\\ElwynnGrassBase.blp", "priority": 0.05},
    {"role": "road", "label": "dirt road", "path": "Tileset\\Elwynn\\ElwynnDirtBase.blp", "priority": 0.95},
    {"role": "rock", "label": "slope rock", "path": "Tileset\\Elwynn\\ElwynnRockBase.blp", "priority": 0.70},
    {"role": "forest", "label": "forest floor", "path": "Tileset\\Elwynn\\ElwynnGrassShadow.blp", "priority": 0.35},
    {"role": "shore", "label": "muddy shore", "path": "Tileset\\Elwynn\\ElwynnRiverMudBase.blp", "priority": 0.55},
    {"role": "sand", "label": "sand", "path": "Tileset\\Ashenvale\\AshenvaleSand.blp", "priority": 0.50},
    {"role": "snow", "label": "snow patch", "path": "Tileset\\IronForge\\IronForgeSnow01solid.blp", "priority": 0.45},
    {"role": "plague", "label": "plague dirt", "path": "Tileset\\PlagueLandsEast\\EastPlaguedBaseDirt.blp", "priority": 0.52},
]

# Minimap tint per role (RGB) and per theme base ground.
ROLE_COLORS = {
    "base": (76, 108, 52),
    "road": (128, 104, 66),
    "rock": (118, 114, 106),
    "forest": (44, 78, 40),
    "shore": (98, 82, 54),
    "snow": (214, 220, 226),
    "sand": (176, 158, 104),
    "plague": (102, 106, 48),
}
THEME_BASE_COLORS = {
    'jungle': (58, 98, 40), 'forest': (72, 104, 50), 'northrend': (88, 106, 72), 'snow': (204, 210, 216),
    'desert': (172, 150, 98), 'swamp': (80, 94, 54), 'plague': (108, 110, 62),
}
WATER_COLOR = (34, 76, 118)


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
    if '_learned_rules' in cfg:
        return cfg['_learned_rules']
    tp = cfg.get('texture_painting', {})
    path = tp.get('learned_rules_path') or 'learned_blizzlike_rules.json'
    p = Path(path)
    rules = None
    if p.exists():
        try:
            rules = json.loads(p.read_text(encoding='utf-8'))
        except Exception as e:
            print(f'Warning: could not read learned rules {p}: {e}')
    cfg['_learned_rules'] = rules
    return rules


def detect_texture_theme(cfg: dict) -> str:
    if '_texture_theme' in cfg:
        return cfg['_texture_theme']
    spec = cfg.get('_zone_spec') or {}
    text_parts = [
        str(spec.get('source_prompt', '')),
        str(spec.get('zone_name', '')),
        str(spec.get('creative_intent', '')),
        str(spec.get('textures', {}).get('palette', '')),
    ]
    text = ' '.join(text_parts).lower()
    theme = 'forest'
    if any(w in text for w in ['jungle', 'stranglethorn', 'tropical', 'rainforest', 'lush']): theme = 'jungle'
    elif any(w in text for w in ['swamp', 'marsh', 'bog', 'wetlands']): theme = 'swamp'
    elif any(w in text for w in ['snow', 'ice', 'frozen', 'winter']): theme = 'snow'
    elif any(w in text for w in ['desert', 'tanaris', 'uldum', 'sand', 'canyon']): theme = 'desert'
    elif any(w in text for w in ['plague', 'undead', 'haunted', 'crypt', 'lordaeron']): theme = 'plague'
    elif any(w in text for w in ['northrend', 'fjord', 'grizzly', 'vrykul', 'borean']): theme = 'northrend'
    cfg['_texture_theme'] = theme
    return theme


def texture_catalog(cfg: dict) -> List[Dict[str, Any]]:
    """Return ordered global MTEX texture entries with role metadata."""
    if '_texture_catalog_cached' in cfg:
        return cfg['_texture_catalog_cached']
    seen: Dict[str, int] = {}
    out: List[Dict[str, Any]] = []
    role_alias: Dict[str, int] = {}

    def add(role: str, path: str, label: Optional[str] = None, priority: float = 0.0):
        if not path:
            return
        norm = path.replace('/', '\\')
        try:
            norm.encode('ascii')
        except UnicodeEncodeError:
            # MTEX paths are raw MPQ lookup keys; a '?'-mangled path can never
            # resolve in the client, so skip it loudly instead of corrupting MTEX.
            print(f'Warning: skipping non-ASCII texture path {norm!r} for role {role}')
            return
        key = norm.lower()
        if key in seen:
            # Same texture used by several roles (default road and shore both
            # use elwynndirtbase). Keep the role reachable by aliasing it to
            # the existing MTEX entry instead of silently dropping the role.
            if role not in role_alias:
                role_alias[role] = seen[key]
            return
        seen[key] = len(out)
        if role not in role_alias:
            role_alias[role] = len(out)
        out.append({"role": role, "path": norm, "label": label or role, "priority": float(priority)})

    # learned rules win: they are taken from the user's local ADTs, so those paths should exist.
    learned = load_learned_rules(cfg)
    if learned:
        theme = detect_texture_theme(cfg)
        themed = (learned.get('recommended_texture_layers_by_theme') or {}).get(theme) or []
        for item in themed[:12]:
            role = str(item.get('role') or _role_from_label(item.get('label', '')))
            add(role, str(item.get('path', '')), item.get('label') or role, float(item.get('priority', 0.4)))
        for item in learned.get('recommended_texture_layers', [])[:12]:
            role = str(item.get('role') or _role_from_label(item.get('label', '')))
            add(role, str(item.get('path', '')), item.get('label') or role, float(item.get('priority', 0.4)))

    spec = cfg.get('_zone_spec') or {}
    tex = spec.get('textures', {})
    for item in tex.get('texture_layers', []) or tex.get('layers', []) or []:
        role = str(item.get('role') or _role_from_label(item.get('label', '')))
        add(role, str(item.get('path') or item.get('texture') or ''), item.get('label') or role, float(item.get('priority', 0.3)))

    add('base', cfg.get('base_texture', DEFAULT_CONFIG['base_texture']), 'configured base', 0.1)

    for item in cfg.get('texture_layers', []) or []:
        role = str(item.get('role') or _role_from_label(item.get('label', '')))
        add(role, str(item.get('path', '')), item.get('label') or role, float(item.get('priority', 0.3)))
    for item in DEFAULT_TEXTURE_LAYERS:
        add(item['role'], item['path'], item['label'], item['priority'])

    # Optional client listfile check: any MTEX path missing from the client renders
    # as the missing-texture fallback, so warn early when a listfile is available.
    listfile = Path(str(cfg.get('texture_painting', {}).get('listfile_path') or 'tileset_listfile_335.txt'))
    if not listfile.exists():
        listfile = Path(__file__).resolve().parent / 'tileset_listfile_335.txt'
    if listfile.exists():
        try:
            known = {line.strip().replace('/', '\\').lower() for line in listfile.read_text(encoding='utf-8', errors='replace').splitlines()}
            for item in out:
                if item['path'].lower() not in known:
                    print(f"Warning: texture {item['path']} not found in {listfile.name}; the client will show a fallback texture")
        except OSError as e:
            print(f'Warning: could not read {listfile}: {e}')

    # WoW terrain chunks can only use a few layers at a time, but MTEX may list more.
    cfg['_texture_catalog_cached'] = out[:32]
    cfg['_texture_role_alias'] = {r: i for r, i in role_alias.items() if i < len(cfg['_texture_catalog_cached'])}
    # Ground-effect ids learned from real ADTs (texture path -> GroundEffectTexture id).
    effects: Dict[str, int] = {}
    if learned and isinstance(learned.get('texture_effect_ids'), dict):
        for path, eff in learned['texture_effect_ids'].items():
            try:
                effects[str(path).replace('/', '\\').lower()] = int(eff) & 0xFFFFFFFF
            except (TypeError, ValueError):
                continue
    cfg['_texture_effects'] = effects
    return cfg['_texture_catalog_cached']


NO_GROUND_EFFECT = 0xFFFFFFFF  # documented "no detail doodads" value (GroundEffectTexture has no row 0 or 65535)


def layer_effect_id(cfg: dict, path: str) -> int:
    """MCLY effectId: learned GroundEffectTexture id for this texture, else 'none'."""
    eff = int((cfg.get('_texture_effects') or {}).get(path.lower(), NO_GROUND_EFFECT))
    return NO_GROUND_EFFECT if eff in (0xFFFF, 0xFFFFFFFF) else eff


# -----------------------------------------------------------------------------
# Layer selection + alpha maps
# -----------------------------------------------------------------------------

def choose_chunk_layers(fields: TileFields, chunk_x: int, chunk_y: int, cfg: dict) -> List[Dict[str, Any]]:
    catalog = texture_catalog(cfg)
    role_to_item: Dict[str, Dict[str, Any]] = {}
    for i, item in enumerate(catalog):
        role_to_item.setdefault(item['role'], dict(item, id=i))
    for role, idx in (cfg.get('_texture_role_alias') or {}).items():
        if role not in role_to_item and 0 <= idx < len(catalog):
            role_to_item[role] = dict(catalog[idx], role=role, id=idx)
    base_item = role_to_item.get('base') or dict(catalog[0], id=0)
    scores = fields.chunk_role_scores(chunk_x, chunk_y)
    extras = []
    for role, score in scores.items():
        if role == 'base' or role not in role_to_item:
            continue
        item = role_to_item[role]
        priority = float(item.get('priority', 0.0))
        # road is kept more aggressively because broken roads look awful.
        keep_score = score + priority * 0.05 + (0.08 if role == 'road' and score > 0.02 else 0.0)
        if keep_score > 0.03:
            extras.append((keep_score, role, item))
    extras.sort(reverse=True, key=lambda x: x[0])
    max_layers = int(cfg.get('texture_painting', {}).get('max_layers_per_chunk', 4))
    max_layers = max(1, min(4, max_layers))
    chosen_items = [base_item]
    used_ids = {int(base_item.get('id', 0))}
    for _, _, it in extras:
        if len(chosen_items) >= max_layers:
            break
        # Aliased roles can share one MTEX id; a chunk must not list the same
        # texture in two MCLY layers.
        if int(it.get('id', 0)) in used_ids:
            continue
        used_ids.add(int(it.get('id', 0)))
        chosen_items.append(it)
    order = {r: i for i, r in enumerate(ROLE_ORDER)}
    chosen_items[1:] = sorted(chosen_items[1:], key=lambda it: order.get(it['role'], 2))
    return chosen_items


def alpha_rle(data: bytes) -> bytes:
    """Compress one 64x64 8-bit MCAL alpha map using WotLK RLE control bytes.

    Runs are restarted at every 64-byte row boundary, matching Noggit's own
    compressor; the decompressor tolerates crossing runs, but Blizzard-style
    files keep rows independent.
    """
    if len(data) != 4096:
        raise ValueError('alpha_rle expects 4096 bytes')
    out = bytearray()
    for row in range(64):
        out.extend(_alpha_rle_row(data[row * 64:(row + 1) * 64]))
    return bytes(out)


def _alpha_rle_row(data: bytes) -> bytes:
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
            out.append(copy); out.extend(data[start:start + copy])
    return bytes(out)


def low_quality_map(alphas: List[bytes]) -> bytes:
    """MCNK 8x8 uint2 map of the dominant layer per 8x8-texel cell.

    Mirrors Noggit's update_lod_texture_map (texture_set.cpp:1027-1078): per
    texel the base layer weighs 255 minus the other layers' alphas, the winner
    is the largest weight (ties to the lowest layer), and each cell takes the
    layer that wins most of its 64 texels. Packed like Noggit: element i in
    byte i//4 at bits (3 - i%4)*2, highest pair first.
    """
    lq = bytearray(16)
    n_layers = len(alphas) + 1
    for ly in range(8):
        for lx in range(8):
            wins = [0] * n_layers
            for ty in range(ly * 8, ly * 8 + 8):
                row = ty * 64 + lx * 8
                for tx in range(row, row + 8):
                    total = 0
                    best = 0
                    best_v = -1
                    for li, amap in enumerate(alphas):
                        v = amap[tx]
                        total += v
                        if v > best_v:
                            best = li + 1; best_v = v
                    base_v = max(0, 255 - total)
                    if base_v >= best_v:
                        best = 0
                    wins[best] += 1
            dominant = max(range(n_layers), key=lambda k: (wins[k], -k))
            idx = ly * 8 + lx
            lq[idx // 4] |= (dominant & 3) << ((3 - (idx % 4)) * 2)
    return bytes(lq)


def build_texture_chunks_for_mcnk(fields: TileFields, chunk_x: int, chunk_y: int, cfg: dict) -> Tuple[bytes, bytes, int, int, bytes]:
    """Return (MCLY chunk, MCAL chunk, nLayers, sizeAlpha, low_quality_texture_map)."""
    tp = cfg.get('texture_painting', {})
    if not tp.get('enabled', True):
        base = texture_catalog(cfg)[0]
        mcly_data = u32(0) + u32(0) + u32(0) + u32(layer_effect_id(cfg, base['path']))
        return chunk('MCLY', mcly_data), chunk('MCAL', b''), 1, 8, bytes(16)
    layers = choose_chunk_layers(fields, chunk_x, chunk_y, cfg)
    mode = str(tp.get('alpha_mode', 'raw_4096')).lower()
    breakup = float(tp.get('breakup', 0.22))
    seed = int(cfg.get('seed', 1337))
    raw_alphas: List[bytes] = []
    mcal_payload = bytearray()
    mcly_payload = bytearray()
    for li, item in enumerate(layers):
        tex_id = int(item.get('id', 0))
        flags = 0
        offset = 0
        if li > 0:
            alpha = rasterize_alpha(fields, chunk_x, chunk_y, item['role'], seed, breakup)
            raw_alphas.append(alpha)
            flags |= 0x100  # use alpha map
            offset = len(mcal_payload)
            if mode in ('rle', 'compressed'):
                alpha = alpha_rle(alpha)
                flags |= 0x200  # compressed alpha map
            mcal_payload.extend(alpha)
        mcly_payload.extend(u32(tex_id) + u32(flags) + u32(offset) + u32(layer_effect_id(cfg, item['path'])))
    lq = low_quality_map(raw_alphas) if raw_alphas else bytes(16)
    # Noggit saves an MCAL chunk even when the payload is empty. header.sizeAlpha
    # includes the 8-byte MCAL chunk header, not just the payload.
    return chunk('MCLY', bytes(mcly_payload)), chunk('MCAL', bytes(mcal_payload)), len(layers), 8 + len(mcal_payload), lq


# -----------------------------------------------------------------------------
# ADT terrain chunks
# -----------------------------------------------------------------------------

MCNK_HEADER_SIZE = 128


def chunk_base_world(tile_x: int, tile_y: int, chunk_x: int, chunk_y: int) -> Tuple[float, float]:
    # Zero-point-relative chunk origin: ZEROPOINT - (tile * TILESIZE + chunk * CHUNKSIZE).
    wx = WORLD_HALF - (tile_x * ADT_SIZE) - (chunk_x * CHUNK_SIZE)
    wy = WORLD_HALF - (tile_y * ADT_SIZE) - (chunk_y * CHUNK_SIZE)
    return wx, wy


def pack_mcnk_position(base_x: float, base_y: float, base_height: float) -> bytes:
    """MCNK position floats in file order (Noggit names them zpos, xpos, ypos).

    zpos (0x68) = ZEROPOINT - (tile_row*TILESIZE + chunk_row*CHUNKSIZE)
    xpos (0x6C) = ZEROPOINT - (tile_col*TILESIZE + chunk_col*CHUNKSIZE)
    ypos (0x70) = absolute height of vertex 0; MCVT values are relative to it.
    """
    return f32(base_y) + f32(base_x) + f32(base_height)


def build_mcnr(normals: List[Tuple[float, float, float]]) -> bytes:
    """MCNR payload: 145 * int8[3].

    Noggit's frame is x = column, z = row, y up, and its saved bytes work out
    to (+dh/dz, +dh/dx, 1)/|n| (MapChunk.cpp:1043 swizzle + 1739-1741 save),
    which is the standard upward normal in the client's (X north, Y west,
    Z up) order. fields.normal() gives (-dh/dx, 1, -dh/dz)/|n|, so the disk
    order is (-nz, -nx, ny).
    """
    out = bytearray()
    for nx, ny, nz in normals:
        for v in (-nz, -nx, ny):
            out.extend(struct.pack('<b', max(-127, min(127, int(round(v * 127.0))))))
    assert len(out) == 145 * 3
    return bytes(out)


def build_mcnk(fields: TileFields, chunk_x: int, chunk_y: int, cfg: dict) -> bytes:
    base_x, base_y = chunk_base_world(fields.tx, fields.ty, chunk_x, chunk_y)
    base_z, heights = fields.chunk_heights(chunk_x, chunk_y)
    mcvt = chunk('MCVT', b''.join(f32(h) for h in heights))
    # Noggit writes MCNR as a 435-byte chunk followed by 13 unknown padding bytes
    # that are outside the MCNR size but inside the MCNK size.
    mcnr = chunk('MCNR', build_mcnr(fields.chunk_normals(chunk_x, chunk_y)))
    mcnr_unknown = b'\0' * 13
    mcly, mcal, n_layers, size_alpha, low_quality_texture_map = build_texture_chunks_for_mcnk(fields, chunk_x, chunk_y, cfg)

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
    header = b''.join([
        u32(flags),
        u32(chunk_x),
        u32(chunk_y),
        u32(n_layers),
        u32(0),                 # nDoodadRefs
        u32(ofs_mcvt),
        u32(ofs_mcnr),
        u32(ofs_mcly),
        u32(ofs_mcrf),
        u32(ofs_mcal),
        u32(size_alpha),
        u32(0),                 # ofsShadow
        u32(0),                 # sizeShadow
        u32(int(cfg.get('area_id', 0))),
        u32(0),                 # nMapObjRefs
        u32(0),                 # holes
        low_quality_texture_map,
        u32(0),                 # predTex
        u32(0),                 # noEffectDoodad
        u32(ofs_mcse),
        u32(0),                 # nSndEmitters
        u32(0),                 # ofsLiquid (no legacy MCLQ)
        u32(8),                 # sizeLiquid: 8 = no MCLQ, WotLK water comes from MH2O
        pack_mcnk_position(base_x, base_y, base_z),
        u32(0),                 # ofsMCCV
        u32(0),                 # ofsMCLV / unused
        u32(0),                 # unused
    ])
    assert len(header) == MCNK_HEADER_SIZE, len(header)
    return chunk('MCNK', header + sub_data)


# -----------------------------------------------------------------------------
# MH2O water generation
# -----------------------------------------------------------------------------

def pack_bitmap_u64(bits: int) -> bytes:
    return bytes((bits >> (8 * i)) & 0xFF for i in range(8))


def build_mh2o(fields: TileFields, cfg: dict) -> Tuple[bytes, int]:
    """Build a WotLK MH2O chunk in exactly the layout Noggit saves.

    256 x 12-byte headers, then per wet chunk: MH2O_Attributes, MH2O_Information,
    the rect-relative exists bitmap (only when the bounding rect is not fully
    wet) and the vertex payload. Instances are shrunk to the bounding rectangle
    of wet 8x8 subchunks and carry per-vertex heights (sloped rivers) and depth
    (opacity) bytes that fade at the shoreline. Returns (chunk bytes or b'',
    wet chunk count). Empty tiles return b'' so MHDR.mh2o can be zero.
    """
    water = cfg.get('water', {})
    if not water.get('enabled', True) or not fields.zone.water_enabled:
        return b'', 0
    river_type = int(water.get('liquid_type', 1))
    ocean_type = int(water.get('ocean_liquid_type', 2))
    lvf = int(water.get('liquid_vertex_format', 0))

    header_entries: List[Tuple[int, int, int]] = []
    tail = bytearray()
    header_size = 256 * 12
    wet_chunks = 0

    for cy in range(16):
        for cx in range(16):
            cw = fields.chunk_water(cx, cy)
            if cw is None:
                header_entries.append((0, 0, 0))
                continue
            wet_chunks += 1
            bits = cw['bits']
            wet_cells = [(qx, qy) for qy in range(8) for qx in range(8) if bits & (1 << (qy * 8 + qx))]
            min_x = min(q[0] for q in wet_cells); max_x = max(q[0] for q in wet_cells)
            min_y = min(q[1] for q in wet_cells); max_y = max(q[1] for q in wet_cells)
            x_off, y_off = min_x, min_y
            width, height = max_x - min_x + 1, max_y - min_y + 1
            first = wet_cells[0]
            wtype = fields.water_type[16 * cy + 2 * first[1] + 1 + MARGIN][16 * cx + 2 * first[0] + 1 + MARGIN]
            liquid_type = ocean_type if wtype == 'ocean_edge' else river_type

            # MH2O_Attributes: fishable where water exists, no fatigue.
            attr_offset = header_size + len(tail)
            tail.extend(u64(bits) + u64(0))

            info_offset = header_size + len(tail)
            info_pos_in_tail = len(tail)
            tail.extend(b'\0' * 24)

            rect_mask = 0; bit = 0; all_set = True
            for qy in range(y_off, y_off + height):
                for qx in range(x_off, x_off + width):
                    if bits & (1 << (qy * 8 + qx)):
                        rect_mask |= 1 << bit
                    else:
                        all_set = False
                    bit += 1
            info_mask_offset = 0
            if not all_set:
                info_mask_offset = header_size + len(tail)
                tail.extend(pack_bitmap_u64(rect_mask))

            heightmap_offset = header_size + len(tail)
            hs = cw['heights']; ds = cw['depths']
            verts = [(hs[vy][vx], ds[vy][vx]) for vy in range(y_off, y_off + height + 1) for vx in range(x_off, x_off + width + 1)]
            min_h = min(v[0] for v in verts); max_h = max(v[0] for v in verts)
            if lvf in (0, 1):
                for h, _d in verts:
                    tail.extend(f32(h))
            if lvf == 1:
                for vy in range(height + 1):
                    for vx in range(width + 1):
                        tail.extend(u16(int((vx / 4.0) * 255)))
                        tail.extend(u16(int((vy / 4.0) * 255)))
            if lvf in (0, 2):
                for _h, d in verts:
                    tail.extend(u8(d))

            info = b''.join([
                u16(liquid_type),
                u16(lvf),
                f32(min_h),
                f32(max_h),
                u8(x_off), u8(y_off), u8(width), u8(height),
                u32(info_mask_offset),
                u32(heightmap_offset),
            ])
            assert len(info) == 24
            tail[info_pos_in_tail:info_pos_in_tail + 24] = info
            header_entries.append((info_offset, 1, attr_offset))

    if wet_chunks == 0:
        return b'', 0
    headers = b''.join(u32(off) + u32(count) + u32(attr) for off, count, attr in header_entries)
    assert len(headers) == header_size
    return chunk('MH2O', headers + bytes(tail)), wet_chunks


# -----------------------------------------------------------------------------
# ADT/WDT/WDL containers
# -----------------------------------------------------------------------------

def build_adt(fields: TileFields, cfg: dict) -> Tuple[bytes, int]:
    # MTEX contains all candidate terrain textures used by MCLY layers. Paths are
    # validated as ASCII in texture_catalog(); encode strictly so a regression
    # fails loudly instead of writing '?' garbage the MPQ can't resolve.
    tex_blob = b''.join(t['path'].encode('ascii') + b'\0' for t in texture_catalog(cfg))

    mver = chunk('MVER', u32(18))
    mcin_placeholder = chunk('MCIN', b'\0' * (256 * 16))
    # Do NOT pad MTEX: Noggit reads NUL-terminated texture strings until the exact
    # MTEX chunk size. Extra NUL padding is read as empty texture filenames.
    mtex = chunk('MTEX', tex_blob)
    mmdx = chunk('MMDX', b'')
    mmid = chunk('MMID', b'')
    mwmo = chunk('MWMO', b'')
    mwid = chunk('MWID', b'')
    mddf = chunk('MDDF', b'')
    modf = chunk('MODF', b'')
    mh2o, wet_chunks = build_mh2o(fields, cfg)

    mhdr = chunk('MHDR', b'\0' * 64)
    pre = mver + mhdr
    mhdr_data_start = len(mver) + 8  # file offset 0x14 = start of MHDR payload

    # MHDR offsets are relative to the MHDR payload start and point at chunk headers.
    pos = len(pre)
    offsets = {}
    metadata_chunks = [('MCIN', mcin_placeholder), ('MTEX', mtex), ('MMDX', mmdx), ('MMID', mmid), ('MWMO', mwmo), ('MWID', mwid), ('MDDF', mddf), ('MODF', modf)]
    if mh2o:
        metadata_chunks.append(('MH2O', mh2o))
    for name, c in metadata_chunks:
        offsets[name] = pos - mhdr_data_start
        pos += len(c)

    mcnks = []
    mcin_entries = []
    mcnk_pos = pos
    for cy in range(16):
        for cx in range(16):
            c = build_mcnk(fields, cx, cy, cfg)
            # MCIN: absolute offset of the MCNK fourcc, size including the 8-byte chunk header.
            mcin_entries.append((mcnk_pos, len(c), 0, 0))
            mcnks.append(c)
            mcnk_pos += len(c)

    mcin_data = b''.join(u32(off) + u32(size) + u32(flags) + u32(async_id) for off, size, flags, async_id in mcin_entries)
    mcin = chunk('MCIN', mcin_data)

    mhdr_payload = b''.join([
        u32(0),                # flags: no MFBO
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
        u32(0),                # MTXF omitted
        u32(0), u32(0), u32(0), u32(0)
    ])
    assert len(mhdr_payload) == 64
    mhdr = chunk('MHDR', mhdr_payload)
    return mver + mhdr + mcin + mtex + mmdx + mmid + mwmo + mwid + mddf + modf + mh2o + b''.join(mcnks), wet_chunks


def build_wdt(map_name: str, present_tiles: set[Tuple[int, int]], cfg: Optional[dict] = None) -> bytes:
    mver = chunk('MVER', u32(18))
    # Noggit treats MPHD flag 0x04 as the big-alpha WDT flag and forces
    # FLAG_SHADING (0x02) on when loading/saving a WDT; generated textured
    # maps therefore use 0x06 in auto mode.
    mphd_cfg = (cfg or {}).get('wdt', {}).get('mphd_flags', 'auto')
    if isinstance(mphd_cfg, str) and mphd_cfg.lower() == 'auto':
        mphd_flags = 0x02
        if (cfg or {}).get('texture_painting', {}).get('enabled', True):
            mphd_flags |= 0x04
    else:
        mphd_flags = int(mphd_cfg or 0)
        if not (cfg or {}).get('wdt', {}).get('force_exact_mphd_flags', False):
            mphd_flags |= 0x02
    mphd = chunk('MPHD', b''.join([u32(mphd_flags)] + [u32(0)] * 7))
    main_entries = []
    for y in range(64):
        for x in range(64):
            flags = 1 if (x, y) in present_tiles else 0
            main_entries.append(u32(flags) + u32(0))
    main = chunk('MAIN', b''.join(main_entries))
    # Blizzard terrain-only WDTs carry an empty MWMO after MAIN (no MODF).
    return mver + mphd + main + chunk('MWMO', b'')


def build_mare(fields: TileFields) -> bytes:
    outer, inner = fields.mare_heights()
    return chunk('MARE', b''.join(i16(h) for h in outer + inner))


def build_wdl(mares: Dict[Tuple[int, int], bytes]) -> bytes:
    """WotLK WDL: MVER + empty MWMO/MWID/MODF + MAOF + one MARE per tile.

    MAHO (hole masks) is omitted: this generator writes no terrain holes and
    the chunk is optional in 3.3.5a.
    """
    pre = chunk('MVER', u32(18)) + chunk('MWMO', b'') + chunk('MWID', b'') + chunk('MODF', b'')
    maof_payload = bytearray(4096 * 4)
    area_chunks = bytearray()
    base_after_maof = len(pre) + 8 + len(maof_payload)
    for y in range(64):
        for x in range(64):
            mare = mares.get((x, y))
            if not mare:
                continue
            struct.pack_into('<I', maof_payload, (y * 64 + x) * 4, base_after_maof + len(area_chunks))
            area_chunks.extend(mare)
    return pre + chunk('MAOF', bytes(maof_payload)) + bytes(area_chunks)


# -----------------------------------------------------------------------------
# Minimap generation: BLP2 RAW1 + TGA previews + md5translate.trs
# -----------------------------------------------------------------------------

def median_cut_palette(pixels: List[Tuple[int, int, int]], size: int = 256) -> List[Tuple[int, int, int]]:
    """Adaptive RGB palette (median cut) so each minimap tile keeps smooth gradients."""
    hist: Dict[Tuple[int, int, int], int] = {}
    for p in pixels:
        hist[p] = hist.get(p, 0) + 1
    colors = list(hist.items())
    if len(colors) <= size:
        return [c for c, _ in colors]
    boxes = [colors]
    while len(boxes) < size:
        # split the box with the largest colour range along its widest channel
        best_i = -1; best_range = -1; best_ch = 0
        for i, box in enumerate(boxes):
            if len(box) < 2:
                continue
            for ch in range(3):
                lo = min(c[ch] for c, _ in box); hi = max(c[ch] for c, _ in box)
                if hi - lo > best_range:
                    best_range = hi - lo; best_i = i; best_ch = ch
        if best_i < 0:
            break
        box = boxes.pop(best_i)
        box.sort(key=lambda cw: cw[0][best_ch])
        total = sum(w for _, w in box)
        acc = 0; cut = 0
        for k, (_, w) in enumerate(box):
            acc += w
            if acc >= total / 2:
                cut = max(1, min(len(box) - 1, k + 1))
                break
        boxes.append(box[:cut]); boxes.append(box[cut:])
    palette = []
    for box in boxes:
        w = sum(cw[1] for cw in box) or 1
        palette.append(tuple(int(round(sum(c[ch] * cw for c, cw in box) / w)) for ch in range(3)))
    return palette


def quantize_pixels(pixels: List[Tuple[int, int, int]]) -> Tuple[bytes, List[Tuple[int, int, int, int]]]:
    """Map RGB pixels to a per-tile 256-colour palette; returns (indices, BGRA palette)."""
    palette = median_cut_palette(pixels, 256)
    cache: Dict[Tuple[int, int, int], int] = {}
    indices = bytearray(len(pixels))
    for i, p in enumerate(pixels):
        idx = cache.get(p)
        if idx is None:
            best = 0; bd = 1 << 30
            pr, pg, pb = p
            for k, (r, g, b) in enumerate(palette):
                d = (r - pr) * (r - pr) + (g - pg) * (g - pg) + (b - pb) * (b - pb)
                if d < bd:
                    bd = d; best = k
            cache[p] = idx = best
        indices[i] = idx
    bgra = [(b, g, r, 255) for r, g, b in palette]
    while len(bgra) < 256:
        bgra.append((0, 0, 0, 255))
    return bytes(indices), bgra


def write_blp_raw1(path: Path, indices: bytes, width: int, height: int, palette: List[Tuple[int, int, int, int]]):
    """Write a BLP2 palettized (colorEncoding 1) texture, no alpha, no mipmaps."""
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
    header.extend(u32(1))      # version, always 1
    header.extend(u8(1))       # colorEncoding: palettized
    header.extend(u8(0))       # alphaSize
    header.extend(u8(0))       # preferredFormat / alphaType
    header.extend(u8(0))       # hasMips
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


_MINIMAP_SHAPING = {
    'road': (0.10, 0.70, 1.00), 'rock': (0.12, 0.80, 0.95), 'shore': (0.12, 0.75, 0.85),
    'sand': (0.12, 0.75, 0.90), 'forest': (0.20, 0.85, 0.70), 'snow': (0.15, 0.80, 1.00), 'plague': (0.15, 0.80, 0.80),
}


def build_minimap_tile(fields: TileFields, cfg: dict) -> Tuple[bytes, List[Tuple[int, int, int]], List[Tuple[int, int, int, int]]]:
    """Composite role colours by texture weight, hill-shade by the terrain normal, tint water by depth.

    Returns (palette indices, RGB pixels, per-tile BGRA palette).
    """
    theme = detect_texture_theme(cfg)
    base_col = THEME_BASE_COLORS.get(theme, ROLE_COLORS['base'])
    weights = {role: fields.weights(role) for role in ROLE_ORDER if role != 'base'}
    # light from the north-west, slightly above: (col, up, row) in noggit's frame
    lx, ly, lz = -0.45, 0.80, -0.40
    inv = 1.0 / math.sqrt(lx * lx + ly * ly + lz * lz)
    lx *= inv; ly *= inv; lz *= inv
    rgb: List[Tuple[int, int, int]] = []
    scale = 256.0 / MINIMAP_SIZE  # grid samples per pixel
    for py in range(MINIMAP_SIZE):
        gy = (py + 0.5) * scale
        for px in range(MINIMAP_SIZE):
            gx = (px + 0.5) * scale
            r, g, b = base_col
            gi = int(gx) + MARGIN; gj = int(gy) + MARGIN
            for role in ('forest', 'sand', 'shore', 'plague', 'snow', 'rock', 'road'):
                w = weights[role][gj][gi]
                e0, e1, gain = _MINIMAP_SHAPING[role]
                a = smoothstep(e0, e1, w) * gain
                if a > 0.001:
                    cr, cg, cb = ROLE_COLORS[role]
                    r += (cr - r) * a; g += (cg - g) * a; b += (cb - b) * a
            # hill shading from the height field
            h = fields.height_f(gx, gy)
            dhdx = (fields.height_f(gx + 0.5, gy) - fields.height_f(gx - 0.5, gy)) / UNIT * 2.0
            dhdz = (fields.height_f(gx, gy + 0.5) - fields.height_f(gx, gy - 0.5)) / UNIT * 2.0
            nx, ny, nz = -dhdx, 1.0, -dhdz
            ninv = 1.0 / math.sqrt(nx * nx + ny * ny + nz * nz)
            ndl = (nx * lx + ny * ly + nz * lz) * ninv
            shade = 0.52 + 0.62 * max(0.0, ndl)
            r *= shade; g *= shade; b *= shade
            surf = fields.surface[gj][gi]
            if surf is not None and surf > h:
                depth = surf - h
                a = min(0.9, 0.42 + depth / 9.0)
                r += (WATER_COLOR[0] - r) * a; g += (WATER_COLOR[1] - g) * a; b += (WATER_COLOR[2] - b) * a
            ri = int(min(255, max(0, r))); gi_ = int(min(255, max(0, g))); bi = int(min(255, max(0, b)))
            rgb.append((ri, gi_, bi))
    indices, palette = quantize_pixels(rgb)
    return indices, rgb, palette


def minimap_output_name(map_name: str, tile_x: int, tile_y: int) -> str:
    # Deterministic physical filename; md5translate.trs maps <Map>\mapX_Y.blp to it.
    return f'{map_name}{tile_x:02d}{tile_y:02d}.blp'


def write_minimap_tile(out: Path, map_name: str, x: int, y: int, fields: TileFields, cfg: dict) -> str:
    mini_cfg = cfg.get('minimap', {})
    indices, rgb_pixels, palette = build_minimap_tile(fields, cfg)
    physical_name = minimap_output_name(map_name, x, y)
    write_blp_raw1(out / 'Textures' / 'Minimap' / physical_name, indices, MINIMAP_SIZE, MINIMAP_SIZE, palette)
    if mini_cfg.get('write_world_minimaps_copy', False):
        # World\Minimaps\<map>\ is the Cataclysm+ location; 3.3.5a only uses the
        # trs. Kept as an opt-in for tools that read it.
        write_blp_raw1(out / 'world' / 'Minimaps' / map_name / f'map{x}_{y:02d}.blp', indices, MINIMAP_SIZE, MINIMAP_SIZE, palette)
    if mini_cfg.get('write_tga_previews', True):
        write_tga(out / 'minimap_previews' / map_name / f'map{x}_{y}.tga', rgb_pixels, MINIMAP_SIZE, MINIMAP_SIZE)
    # Blizzard keys: <Directory>\map<x>_<yy>.blp with y zero-padded to 2 digits;
    # exactly one tab before the physical name (a trailing tab would be read as
    # part of the filename by strict parsers).
    return f'{map_name}\\map{x}_{y:02d}.blp\t{physical_name}'


def write_md5translate(out: Path, fragments: List[str], cfg: Optional[dict] = None):
    """Write the minimap translation fragment.

    The 3.3.5a client resolves ALL minimaps through a single global
    Textures\\Minimap\\md5translate.trs; a patch MPQ replaces it wholesale.
    Shipping a fragment-only file under the real name would therefore wipe the
    minimaps of every stock map. By default only the *_FRAGMENT.trs is written
    (merge it into your extracted original); set
    minimap.write_full_md5translate=true for a disposable-test full file.
    """
    if not fragments:
        return
    text = '\r\n'.join(fragments) + '\r\n'
    write_file(out / 'Textures' / 'Minimap' / 'md5translate_AIGEN_FRAGMENT.trs', text.encode('ascii', errors='replace'))
    if (cfg or {}).get('minimap', {}).get('write_full_md5translate', False):
        write_file(out / 'Textures' / 'Minimap' / 'md5translate.trs', text.encode('ascii', errors='replace'))


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
    write_custom = mode in ('both', 'custom')
    write_override = mode in ('both', 'azeroth_override')
    mini_cfg = cfg.get('minimap', {})

    load_learned_rules(cfg)
    detect_texture_theme(cfg)
    texture_catalog(cfg)
    zone = ZoneModel(cfg)
    cfg['_zone_model'] = zone

    trs_custom: List[str] = [f'dir: {map_name}']
    trs_override: List[str] = ['dir: Azeroth']
    mares: Dict[Tuple[int, int], bytes] = {}
    wet_total = 0
    custom_base = out / 'world' / 'maps' / map_name
    override_base = out / 'world' / 'maps' / 'azeroth'

    for y in tiles_y:
        for x in tiles_x:
            fields = TileFields(x, y, zone)
            adt, wet = build_adt(fields, cfg)
            wet_total += wet
            if write_custom:
                write_file(custom_base / f'{map_name}_{x}_{y}.adt', adt)
                mares[(x, y)] = build_mare(fields)
                if mini_cfg.get('enabled', True):
                    trs_custom.append(write_minimap_tile(out, map_name, x, y, fields, cfg))
            if write_override:
                write_file(override_base / f'azeroth_{x}_{y}.adt', adt)
                if mini_cfg.get('enabled', True):
                    trs_override.append(write_minimap_tile(out, 'Azeroth', x, y, fields, cfg))
            print(f'  tile {x},{y}: {len(adt)} bytes, wet MCNKs={wet}')

    if write_custom:
        write_file(custom_base / f'{map_name}.wdt', build_wdt(map_name, present, cfg))
        if cfg.get('wdl', {}).get('enabled', True):
            write_file(custom_base / f'{map_name}.wdl', build_wdl(mares))
    # No replacement Azeroth.wdl is written for the override mode: that would
    # replace the full continent's distant terrain.

    fragments: List[str] = []
    if write_custom and len(trs_custom) > 1:
        fragments.extend(trs_custom)
    if write_override and len(trs_override) > 1:
        fragments.extend(trs_override)
    if mini_cfg.get('write_md5translate', True):
        write_md5translate(out, fragments, cfg)

    water_lines = []
    for f in zone.water_features:
        lvl = f"{f['_level0']:.1f}" if f['_level0'] == f['_level1'] else f"{f['_level0']:.1f}->{f['_level1']:.1f}"
        snapped = f" (level snapped to {f['_snapped_to']})" if f.get('_snapped_to') else ''
        water_lines.append(f"  {f.get('id', f['_type'])}: {f['_type']} level {lvl}{snapped}")
    (out / 'GENERATION_REPORT.txt').write_text(
        f"AI ADT One-Click v0.19\n"
        f"Generated {len(present)} ADT tile(s) in mode={mode}\n"
        f"Tiles: {sorted(present)}\n"
        f"Texture theme: {detect_texture_theme(cfg)}\n"
        f"Texture layers: {len(texture_catalog(cfg))} MTEX entries, auto alphamaps={cfg.get('texture_painting', {}).get('enabled', True)}\n"
        f"Ground-effect ids learned for {len(cfg.get('_texture_effects') or {})} texture(s)\n"
        f"Water enabled: {cfg.get('water', {}).get('enabled', True)}\n"
        f"Wet MCNK chunks: {wet_total}\n" + ('\n'.join(water_lines) + '\n' if water_lines else '') +
        f"WDL enabled for custom map: {cfg.get('wdl', {}).get('enabled', True)}\n"
        f"Minimap enabled: {mini_cfg.get('enabled', True)}\n\n"
        f"Custom map: run patch_dbc.py against your extracted DBFilesClient to add Map.dbc/AreaTable.dbc rows for {map_name}.\n"
        "Minimap note: merge md5translate_AIGEN_FRAGMENT.trs into your extracted original Textures\\Minimap\\md5translate.trs.\n",
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
    import sys as _sys
    cfg = load_config(_sys.argv[1] if len(_sys.argv) > 1 else 'config.json')
    generate(cfg)
    print('AI ADT One-Click v0.19 finished.')
    print(f"Config: {cfg.get('_config_path', 'config.json')}")
    print(f"Output: {Path(cfg.get('output_dir', 'output_loose')).resolve()}")
    print('Generated loose patch folder: ADT/WDT + MCLY/MCAL alphamaps + MH2O water + custom WDL + minimap BLP/TRS files.')


if __name__ == '__main__':
    main()
