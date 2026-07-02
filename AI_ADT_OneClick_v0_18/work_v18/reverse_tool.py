#!/usr/bin/env python3
"""
AI ADT One-Click v0.18 reverse mode.

Reads existing WotLK-style ADTs, analyzes height/object/water/texture structure,
and can produce an improved copy using region modes:
  - preserve: no height changes
  - polish: conservative smoothing / seam cleanup
  - regenerate: procedural redo blended into borders
  - auto: chooses regenerate/polish/preserve from simple metrics

This script intentionally does not add/remove/replace M2/WMO objects. It preserves
MDDF/MODF chunks and reports object counts/risks instead.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ADT_SIZE = 533.3333333333334
CHUNK_SIZE = ADT_SIZE / 16.0
UNIT = CHUNK_SIZE / 8.0
MINIMAP_SIZE = 256
WORLD_SIZE = ADT_SIZE * 64.0
WORLD_HALF = WORLD_SIZE / 2.0

# -----------------------------------------------------------------------------
# Binary helpers
# -----------------------------------------------------------------------------

def u32(x): return struct.pack('<I', int(x) & 0xFFFFFFFF)
def i16(x): return struct.pack('<h', max(-32768, min(32767, int(round(x)))))
def u8(x): return struct.pack('<B', int(x) & 0xFF)
def f32(x): return struct.pack('<f', float(x))
def r_u32(b: bytes | bytearray, o: int) -> int: return struct.unpack_from('<I', b, o)[0]
def r_i16(b: bytes | bytearray, o: int) -> int: return struct.unpack_from('<h', b, o)[0]
def r_f32(b: bytes | bytearray, o: int) -> float: return struct.unpack_from('<f', b, o)[0]
def w_f32(b: bytearray, o: int, x: float): struct.pack_into('<f', b, o, float(x))

def magic(name: str) -> bytes:
    return name[::-1].encode('ascii')

def display(raw: bytes) -> str:
    return raw[::-1].decode('ascii', errors='replace')

def chunk(name: str, data: bytes) -> bytes:
    return magic(name) + u32(len(data)) + data


def scan_chunks(b: bytes, start: int = 0, end: Optional[int] = None) -> List[Tuple[int, str, int]]:
    if end is None:
        end = len(b)
    pos = start
    out = []
    while pos + 8 <= end:
        raw = b[pos:pos+4]
        try:
            name = display(raw)
        except UnicodeDecodeError:
            break
        if not all(32 <= c <= 126 for c in raw):
            break
        size = r_u32(b, pos + 4)
        if size < 0 or pos + 8 + size > end:
            break
        out.append((pos, name, size))
        pos += 8 + size
    return out


def strings_from_zero_blob(blob: bytes) -> List[str]:
    out = []
    for s in blob.split(b'\0'):
        if s:
            out.append(s.decode('ascii', errors='replace'))
    return out

# -----------------------------------------------------------------------------
# BLP/TGA output helpers, duplicated here so reverse mode is standalone.
# -----------------------------------------------------------------------------

def write_file(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def lerp(a: int, b: int, t: float) -> int:
    return int(round(a + (b - a) * max(0.0, min(1.0, t))))


def rgb_lerp(c1: Tuple[int, int, int], c2: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    return tuple(lerp(c1[i], c2[i], t) for i in range(3))  # type: ignore


def make_minimap_palette() -> List[Tuple[int, int, int, int]]:
    pal: List[Tuple[int, int, int, int]] = []
    stops = [
        (0.00, (45, 66, 39)),
        (0.35, (74, 101, 49)),
        (0.62, (108, 120, 68)),
        (0.82, (128, 112, 85)),
        (1.00, (164, 158, 142)),
    ]
    for i in range(128):
        t = i / 127.0
        for si in range(len(stops) - 1):
            if stops[si][0] <= t <= stops[si + 1][0]:
                local = (t - stops[si][0]) / (stops[si + 1][0] - stops[si][0])
                r, g, b = rgb_lerp(stops[si][1], stops[si + 1][1], local)
                pal.append((b, g, r, 255))
                break
    for i in range(32):
        t = i / 31.0
        r, g, b = rgb_lerp((34, 67, 95), (76, 120, 145), t)
        pal.append((b, g, r, 255))
    for i in range(32):
        t = i / 31.0
        r, g, b = rgb_lerp((80, 72, 52), (135, 122, 82), t)
        pal.append((b, g, r, 255))
    while len(pal) < 256:
        pal.append((0, 0, 0, 255))
    return pal


def write_blp_raw1(path: Path, indices: bytes, width: int, height: int, palette: List[Tuple[int, int, int, int]]):
    header_size = 4 + 4 + 1 + 1 + 1 + 1 + 4 + 4 + 16 * 4 + 16 * 4
    palette_size = 256 * 4
    data_offset = header_size + palette_size
    offsets = [0] * 16
    lengths = [0] * 16
    offsets[0] = data_offset
    lengths[0] = len(indices)
    header = bytearray()
    header.extend(b'BLP2')
    header.extend(u32(1))
    header.extend(u8(1))
    header.extend(u8(0))
    header.extend(u8(0))
    header.extend(u8(0))
    header.extend(u32(width))
    header.extend(u32(height))
    for o in offsets: header.extend(u32(o))
    for l in lengths: header.extend(u32(l))
    for b, g, r, a in palette:
        header.extend(bytes([b, g, r, a]))
    write_file(path, bytes(header) + indices)


def write_tga(path: Path, pixels_rgb: List[Tuple[int, int, int]], width: int, height: int):
    header = bytearray(18)
    header[2] = 2
    header[12:14] = struct.pack('<H', width)
    header[14:16] = struct.pack('<H', height)
    header[16] = 24
    header[17] = 0x20
    data = bytearray()
    for r, g, b in pixels_rgb:
        data.extend(bytes([b, g, r]))
    write_file(path, bytes(header) + bytes(data))

# -----------------------------------------------------------------------------
# ADT scene structures
# -----------------------------------------------------------------------------

@dataclass
class McnkRef:
    index: int
    cx: int
    cy: int
    start: int
    size: int
    data_start: int
    ofs_mcvt: int
    mcvt_start: int
    base_pos_offset: int
    base_z: float
    abs_heights: List[float]

@dataclass
class AdtTile:
    path: Path
    map_name: str
    x: int
    y: int
    data: bytearray
    chunks: List[Tuple[int, str, int]]
    mcnks: List[McnkRef] = field(default_factory=list)
    grid: List[List[float]] = field(default_factory=list)
    original_grid: List[List[float]] = field(default_factory=list)
    textures: List[str] = field(default_factory=list)
    doodad_defs: int = 0
    wmo_defs: int = 0
    wet_chunks: int = 0
    parse_warnings: List[str] = field(default_factory=list)
    mode: str = 'auto'

    @property
    def object_refs(self) -> int:
        return self.doodad_defs + self.wmo_defs

    def metrics(self) -> Dict[str, float | int | str]:
        vals = [v for row in self.grid for v in row]
        hmin, hmax = min(vals), max(vals)
        slopes = []
        rough = []
        for yy in range(1, 128):
            for xx in range(1, 128):
                dx = abs(self.grid[yy][xx+1] - self.grid[yy][xx-1]) / (2 * UNIT)
                dy = abs(self.grid[yy+1][xx] - self.grid[yy-1][xx]) / (2 * UNIT)
                slopes.append(math.sqrt(dx*dx + dy*dy))
                avg = (self.grid[yy-1][xx] + self.grid[yy+1][xx] + self.grid[yy][xx-1] + self.grid[yy][xx+1]) * 0.25
                rough.append(abs(self.grid[yy][xx] - avg))
        avg_slope = sum(slopes) / max(1, len(slopes))
        avg_rough = sum(rough) / max(1, len(rough))
        return {
            'tile': f'{self.x},{self.y}',
            'mode': self.mode,
            'height_min': round(hmin, 3),
            'height_max': round(hmax, 3),
            'height_range': round(hmax - hmin, 3),
            'avg_slope': round(avg_slope, 4),
            'avg_roughness': round(avg_rough, 4),
            'textures': len(self.textures),
            'objects': self.object_refs,
            'm2': self.doodad_defs,
            'wmo': self.wmo_defs,
            'wet_chunks': self.wet_chunks,
        }


def parse_tile_name(path: Path) -> Optional[Tuple[str, int, int]]:
    m = re.match(r'(.+)_([0-9]{1,2})_([0-9]{1,2})\.adt$', path.name, re.IGNORECASE)
    if not m:
        return None
    return (m.group(1), int(m.group(2)), int(m.group(3)))


def read_mcvtheights(data: bytes | bytearray, mcvtheader: int, base_z: float) -> List[float]:
    if data[mcvtheader:mcvtheader+4] != magic('MCVT'):
        raise ValueError('missing MCVT')
    size = r_u32(data, mcvtheader + 4)
    if size < 145 * 4:
        raise ValueError(f'MCVT too small: {size}')
    pos = mcvtheader + 8
    return [base_z + r_f32(data, pos + i * 4) for i in range(145)]


def outer_grid_from_mcnks(mcnks: List[McnkRef]) -> List[List[float]]:
    grid = [[0.0 for _ in range(129)] for _ in range(129)]
    filled = [[False for _ in range(129)] for _ in range(129)]
    for m in mcnks:
        idx = 0
        for row in range(17):
            if row % 2 == 0:
                out_row = row // 2
                for col in range(9):
                    gx = m.cx * 8 + col
                    gy = m.cy * 8 + out_row
                    h = m.abs_heights[idx]
                    if not filled[gy][gx]:
                        grid[gy][gx] = h
                        filled[gy][gx] = True
                    else:
                        # overlapping borders: average defensively
                        grid[gy][gx] = (grid[gy][gx] + h) * 0.5
                    idx += 1
            else:
                idx += 8
    # Fill missing points by neighbor average if a malformed ADT lacked a chunk.
    for _ in range(3):
        for yy in range(129):
            for xx in range(129):
                if filled[yy][xx]:
                    continue
                vals = []
                for dy, dx in [(-1,0),(1,0),(0,-1),(0,1)]:
                    y2, x2 = yy+dy, xx+dx
                    if 0 <= y2 < 129 and 0 <= x2 < 129 and filled[y2][x2]:
                        vals.append(grid[y2][x2])
                if vals:
                    grid[yy][xx] = sum(vals) / len(vals)
                    filled[yy][xx] = True
    return grid


def parse_adt(path: Path) -> AdtTile:
    info = parse_tile_name(path)
    if not info:
        raise ValueError(f'Not an ADT tile name: {path.name}')
    map_name, tx, ty = info
    data = bytearray(path.read_bytes())
    top = scan_chunks(data)
    tile = AdtTile(path=path, map_name=map_name, x=tx, y=ty, data=data, chunks=top)

    by_name = {name: (pos, size) for pos, name, size in top}
    if 'MTEX' in by_name:
        pos, size = by_name['MTEX']
        tile.textures = strings_from_zero_blob(data[pos+8:pos+8+size])
    if 'MDDF' in by_name:
        pos, size = by_name['MDDF']
        tile.doodad_defs = size // 36
    if 'MODF' in by_name:
        pos, size = by_name['MODF']
        tile.wmo_defs = size // 64
    if 'MH2O' in by_name:
        pos, size = by_name['MH2O']
        if size >= 256 * 12:
            wet = 0
            for i in range(256):
                cnt = r_u32(data, pos + 8 + i * 12 + 4)
                if cnt:
                    wet += 1
            tile.wet_chunks = wet

    if 'MCIN' not in by_name:
        raise ValueError(f'{path} has no MCIN chunk; not supported by v0.3 reverse parser')
    mcin_pos, mcin_size = by_name['MCIN']
    if mcin_size < 256 * 16:
        raise ValueError(f'{path} has malformed MCIN')
    for i in range(256):
        off = r_u32(data, mcin_pos + 8 + i * 16)
        size = r_u32(data, mcin_pos + 8 + i * 16 + 4)
        if off == 0 or off + 8 > len(data) or data[off:off+4] != magic('MCNK'):
            tile.parse_warnings.append(f'MCNK {i} offset invalid or missing')
            continue
        ds = off + 8
        if ds + 128 > len(data):
            tile.parse_warnings.append(f'MCNK {i} header truncated')
            continue
        cx = r_u32(data, ds + 4)
        cy = r_u32(data, ds + 8)
        # Some tools store chunk index fields oddly; fall back to the MCIN index.
        if not (0 <= cx <= 15 and 0 <= cy <= 15):
            cx = i % 16
            cy = i // 16
        ofs_mcvt = r_u32(data, ds + 20)
        candidates = [off + ofs_mcvt, ds + ofs_mcvt]
        mcvt_start = -1
        for c in candidates:
            if 0 <= c <= len(data)-8 and data[c:c+4] == magic('MCVT'):
                mcvt_start = c
                break
        if mcvt_start < 0:
            # Fallback: scan nested chunks after the 128-byte MCNK header.
            nested = scan_chunks(data, ds + 128, off + 8 + size)
            found = [p for p, n, s in nested if n == 'MCVT']
            if found:
                mcvt_start = found[0]
        if mcvt_start < 0:
            tile.parse_warnings.append(f'MCNK {i} missing MCVT')
            continue
        base_z = r_f32(data, ds + 112)  # Noggit header.ypos is the vertical base height
        try:
            heights = read_mcvtheights(data, mcvt_start, base_z)
        except Exception as e:
            tile.parse_warnings.append(f'MCNK {i} MCVT parse error: {e}')
            continue
        tile.mcnks.append(McnkRef(
            index=i, cx=int(cx), cy=int(cy), start=off, size=size, data_start=ds,
            ofs_mcvt=ofs_mcvt, mcvt_start=mcvt_start, base_pos_offset=ds + 112,
            base_z=base_z, abs_heights=heights
        ))
    if len(tile.mcnks) == 0:
        raise ValueError(f'{path} had zero parseable MCNK/MCVT chunks')
    tile.grid = outer_grid_from_mcnks(tile.mcnks)
    tile.original_grid = [row[:] for row in tile.grid]
    return tile


def find_input_adts(cfg: dict) -> List[Path]:
    root = Path(cfg.get('input_dir', 'input_existing')) / 'world' / 'maps' / cfg.get('map_name', 'aigen')
    if not root.exists():
        raise FileNotFoundError(f'Input map folder does not exist: {root}\nPut your ADTs there or run reverse_make_sample_input.bat after one_click_generate.bat.')
    files = sorted(root.glob(f"{cfg.get('map_name','aigen')}_*_*.adt"))
    if cfg.get('tiles', 'auto') != 'auto':
        wanted = set(tuple(t) for t in cfg['tiles'])
        files = [p for p in files if parse_tile_name(p) and (parse_tile_name(p)[1], parse_tile_name(p)[2]) in wanted]
    if not files:
        raise FileNotFoundError(f'No ADTs found in {root}')
    return files

# -----------------------------------------------------------------------------
# Analysis/improvement algorithms
# -----------------------------------------------------------------------------

def choose_mode(tile: AdtTile, cfg: dict) -> str:
    key = f'{tile.x},{tile.y}'
    explicit = cfg.get('region_modes', {}).get(key)
    if explicit and explicit != 'auto':
        return explicit
    default = cfg.get('default_mode', 'auto')
    if default != 'auto':
        return default
    m = tile.metrics()
    ed = cfg.get('empty_detection', {})
    # Heavily decorated tiles are finished work: auto mode must be able to
    # answer 'preserve', not only polish/regenerate.
    if tile.object_refs >= int(ed.get('preserve_if_object_refs_gte', 8)):
        return 'preserve'
    if tile.object_refs <= int(ed.get('regenerate_if_object_refs_lte', 0)) and float(m['height_range']) <= float(ed.get('regenerate_if_height_range_lte', 14.0)):
        return 'regenerate'
    if tile.object_refs >= int(ed.get('polish_if_object_refs_gte', 1)):
        return 'polish'
    return 'polish'


def smooth_grid(grid: List[List[float]], strength: float, passes: int, freeze_border: int = 0) -> List[List[float]]:
    out = [row[:] for row in grid]
    for _ in range(passes):
        old = [row[:] for row in out]
        for yy in range(freeze_border, 129 - freeze_border):
            for xx in range(freeze_border, 129 - freeze_border):
                if yy == 0 or yy == 128 or xx == 0 or xx == 128:
                    continue
                avg = (old[yy-1][xx] + old[yy+1][xx] + old[yy][xx-1] + old[yy][xx+1]) * 0.25
                out[yy][xx] = old[yy][xx] * (1.0 - strength) + avg * strength
    return out


def tile_center_world(tx: int, ty: int) -> Tuple[float, float]:
    bx = WORLD_HALF - tx * ADT_SIZE
    by = WORLD_HALF - ty * ADT_SIZE
    return bx - ADT_SIZE / 2.0, by - ADT_SIZE / 2.0


def local_world(tx: int, ty: int, gx: int, gy: int) -> Tuple[float, float]:
    bx = WORLD_HALF - tx * ADT_SIZE
    by = WORLD_HALF - ty * ADT_SIZE
    return bx - gx * UNIT, by - gy * UNIT


def regen_height(tx: int, ty: int, gx: int, gy: int, cfg: dict) -> float:
    pc = cfg.get('procedural_regen', {})
    base = float(pc.get('base_height', 42.0))
    hill = float(pc.get('hill_height', 70.0))
    wave = float(pc.get('wave_height', 28.0))
    valley = float(pc.get('valley_depth', 20.0))
    seed = int(pc.get('seed', 1337))
    wx, wy = local_world(tx, ty, gx, gy)
    # Local center around the selected ADT plus deterministic seed phase.
    cx, cy = tile_center_world(tx, ty)
    lx, ly = wx - cx, wy - cy
    r = math.sqrt(lx*lx + ly*ly)
    phase = (seed % 997) / 997.0 * 6.28318
    radial = math.exp(-(r*r)/(2*290.0*290.0)) * hill
    waves = (math.sin((wx + seed) / 123.0) * 0.55 + math.cos((wy - seed) / 161.0) * 0.45 + math.sin((wx + wy) / 217.0 + phase) * 0.32) * wave
    # A soft diagonal valley through the tile.
    d = abs((ly * 0.62) - (lx * 0.22)) / math.sqrt(0.62*0.62 + 0.22*0.22)
    carved = -valley * math.exp(-(d*d)/(2*55.0*55.0))
    return base + radial + waves + carved


def regenerate_grid(tile: AdtTile, cfg: dict) -> List[List[float]]:
    target = [[regen_height(tile.x, tile.y, xx, yy, cfg) for xx in range(129)] for yy in range(129)]
    old = tile.grid
    strength = float(cfg.get('improve', {}).get('regenerate_strength', 0.88))
    edge = int(cfg.get('improve', {}).get('edge_blend_vertices', 18))
    out = [[0.0 for _ in range(129)] for _ in range(129)]
    for yy in range(129):
        for xx in range(129):
            # Fade regeneration strength toward all edges so groups can be blended later.
            dist = min(xx, yy, 128-xx, 128-yy)
            edge_t = min(1.0, dist / max(1, edge))
            s = strength * edge_t
            out[yy][xx] = old[yy][xx] * (1.0 - s) + target[yy][xx] * s
    return out


def apply_region_modes(tiles: Dict[Tuple[int, int], AdtTile], cfg: dict):
    imp = cfg.get('improve', {})
    polish_passes = int(imp.get('polish_passes', 2))
    polish_strength = float(imp.get('polish_strength', 0.22))
    for tile in tiles.values():
        tile.mode = choose_mode(tile, cfg)
        if tile.mode == 'preserve':
            tile.grid = [row[:] for row in tile.original_grid]
        elif tile.mode == 'polish':
            # Freeze a small border before the global seam pass.
            tile.grid = smooth_grid(tile.original_grid, polish_strength, polish_passes, freeze_border=2)
        elif tile.mode == 'regenerate':
            if tile.object_refs:
                tile.parse_warnings.append('REGION WARNING: regenerate mode has existing objects; heights changed but M2/WMO were preserved and may need manual review.')
            if tile.wet_chunks:
                tile.parse_warnings.append('REGION WARNING: regenerate mode has MH2O water; liquid heightmaps are not rewritten, so moved terrain can leave water floating or buried.')
            tile.grid = regenerate_grid(tile, cfg)
        elif tile.mode == 'auto':
            tile.grid = smooth_grid(tile.original_grid, polish_strength, polish_passes, freeze_border=2)
        else:
            tile.parse_warnings.append(f'Unknown mode {tile.mode}; preserving tile')
            tile.grid = [row[:] for row in tile.original_grid]


def fix_seams(tiles: Dict[Tuple[int, int], AdtTile], cfg: dict):
    if not cfg.get('improve', {}).get('fix_loaded_tile_seams', True):
        return
    # 'preserve' tiles must keep their exact bytes: when one side of a seam is
    # preserved, snap the other side onto it; average only when both changed.
    # East/west pairs: tile x+1 west edge should match tile x east edge.
    for (tx, ty), t in list(tiles.items()):
        east = tiles.get((tx + 1, ty))
        if east:
            t_locked = t.mode == 'preserve'
            e_locked = east.mode == 'preserve'
            for yy in range(129):
                if t_locked and e_locked:
                    break
                if t_locked:
                    east.grid[yy][0] = t.grid[yy][128]
                elif e_locked:
                    t.grid[yy][128] = east.grid[yy][0]
                else:
                    avg = (t.grid[yy][128] + east.grid[yy][0]) * 0.5
                    t.grid[yy][128] = avg
                    east.grid[yy][0] = avg
        south = tiles.get((tx, ty + 1))
        if south:
            t_locked = t.mode == 'preserve'
            s_locked = south.mode == 'preserve'
            for xx in range(129):
                if t_locked and s_locked:
                    break
                if t_locked:
                    south.grid[0][xx] = t.grid[128][xx]
                elif s_locked:
                    t.grid[128][xx] = south.grid[0][xx]
                else:
                    avg = (t.grid[128][xx] + south.grid[0][xx]) * 0.5
                    t.grid[128][xx] = avg
                    south.grid[0][xx] = avg


def bilinear_grid(grid: List[List[float]], x: float, y: float) -> float:
    x = max(0.0, min(128.0, x))
    y = max(0.0, min(128.0, y))
    x0, y0 = int(math.floor(x)), int(math.floor(y))
    x1, y1 = min(128, x0 + 1), min(128, y0 + 1)
    tx, ty = x - x0, y - y0
    a = grid[y0][x0] * (1-tx) + grid[y0][x1] * tx
    b = grid[y1][x0] * (1-tx) + grid[y1][x1] * tx
    return a * (1-ty) + b * ty


def pack_normal_bytes(nx: float, ny: float, nz: float) -> bytes:
    inv = 1.0 / max(1e-6, math.sqrt(nx*nx + ny*ny + nz*nz))
    # Noggit saves MCNR bytes as normal.x, normal.z, normal.y.
    vals = (nx * inv, nz * inv, ny * inv)
    return b''.join(struct.pack('<b', max(-127, min(127, int(round(v * 127.0))))) for v in vals)


def recalc_mcnr_for_tile(tile: AdtTile, m: McnkRef):
    ofs_mcnr = r_u32(tile.data, m.data_start + 24)
    mcnr_start = m.start + ofs_mcnr
    if not (0 <= mcnr_start <= len(tile.data) - 8 and tile.data[mcnr_start:mcnr_start+4] == magic('MCNR')):
        return
    if r_u32(tile.data, mcnr_start + 4) != 145 * 3:
        return
    payload = mcnr_start + 8
    out = bytearray()
    step = 0.5
    for row in range(17):
        if row % 2 == 0:
            yy = m.cy * 8 + (row // 2)
            for col in range(9):
                xx = m.cx * 8 + col
                dhdx = (bilinear_grid(tile.grid, xx + step, yy) - bilinear_grid(tile.grid, xx - step, yy)) / (2.0 * step * UNIT)
                dhdy = (bilinear_grid(tile.grid, xx, yy + step) - bilinear_grid(tile.grid, xx, yy - step)) / (2.0 * step * UNIT)
                out.extend(pack_normal_bytes(-dhdx, 1.0, -dhdy))
        else:
            yy = m.cy * 8 + (row // 2) + 0.5
            for col in range(8):
                xx = m.cx * 8 + col + 0.5
                dhdx = (bilinear_grid(tile.grid, xx + step, yy) - bilinear_grid(tile.grid, xx - step, yy)) / (2.0 * step * UNIT)
                dhdy = (bilinear_grid(tile.grid, xx, yy + step) - bilinear_grid(tile.grid, xx, yy - step)) / (2.0 * step * UNIT)
                out.extend(pack_normal_bytes(-dhdx, 1.0, -dhdy))
    if len(out) == 145 * 3:
        tile.data[payload:payload + 145 * 3] = out


def write_grid_back(tile: AdtTile):
    if tile.mode == 'preserve':
        # Preserve means byte-identical terrain: no MCVT/ypos rewrite, no MCNR
        # recalculation. fix_seams never edits preserve tiles either.
        return
    for m in tile.mcnks:
        values: List[float] = []
        idx = 0
        for row in range(17):
            if row % 2 == 0:
                yy = m.cy * 8 + (row // 2)
                for col in range(9):
                    xx = m.cx * 8 + col
                    values.append(tile.grid[yy][xx])
                    idx += 1
            else:
                # The 8x8 inner vertices are independent height samples, not
                # derivable from the outer ring. Keep the original inner detail
                # and shift it by how much the surrounding outer grid moved.
                yy = m.cy * 8 + (row // 2) + 0.5
                for col in range(8):
                    xx = m.cx * 8 + col + 0.5
                    delta = bilinear_grid(tile.grid, xx, yy) - bilinear_grid(tile.original_grid, xx, yy)
                    values.append(m.abs_heights[idx] + delta)
                    idx += 1
        if len(values) != 145:
            raise AssertionError('bad MCVT sample count')
        new_base = values[0]
        # Noggit MapChunkHeader stores zpos,xpos,ypos at 0x68/0x6C/0x70.
        # Only ypos is the vertical base height. Earlier reverse builds wrote
        # the new base into zpos, corrupting the chunk's raw X/Z anchor.
        w_f32(tile.data, m.data_start + 112, new_base)
        start = m.mcvt_start + 8
        for i, h in enumerate(values):
            w_f32(tile.data, start + i * 4, h - new_base)
        recalc_mcnr_for_tile(tile, m)

# -----------------------------------------------------------------------------
# WDL/minimap generation from parsed/improved tile grids
# -----------------------------------------------------------------------------

def build_wdt(present_tiles: set[Tuple[int, int]], big_alpha: bool = False) -> bytes:
    mver = chunk('MVER', u32(18))
    # Fallback WDT for reverse mode when the input folder had no WDT. Noggit
    # always enables FLAG_SHADING (0x02) on WDT load/save. The big-alpha flag
    # (0x04) must only be set when the ADTs actually use big/compressed alpha;
    # forcing it for old 2048-byte alphamaps would make Noggit/client parse MCAL
    # with the wrong format.
    flags = 0x02 | (0x04 if big_alpha else 0)
    mphd = chunk('MPHD', b''.join([u32(flags)] + [u32(0)] * 7))
    main_entries = []
    for y in range(64):
        for x in range(64):
            tile_flags = 1 if (x, y) in present_tiles else 0
            main_entries.append(u32(tile_flags) + u32(0))
    # Blizzard terrain-only WDTs carry an empty MWMO after MAIN (no MODF).
    return mver + mphd + chunk('MAIN', b''.join(main_entries)) + chunk('MWMO', b'')


def tile_uses_big_alpha(tile: AdtTile) -> bool:
    """Infer whether any chunk in an ADT needs WDT MPHD big-alpha flag 0x04.

    Noggit chooses alphamap format from the WDT bigAlpha flag, but also treats a
    per-layer compressed flag as big alpha. For reverse mode, copied source WDTs
    must preserve/correct this instead of blindly forcing 0x04.
    """
    data = tile.data
    for m in tile.mcnks:
        ds = m.data_start
        n_layers = r_u32(data, ds + 12)
        if n_layers <= 1 or n_layers > 4:
            continue
        ofs_mcly = r_u32(data, ds + 28)
        ofs_mcal = r_u32(data, ds + 36)
        # Match parse_adt's tolerance for tools that store subchunk offsets
        # relative to the header start instead of the MCNK fourcc; otherwise
        # big-alpha ADTs from such tools silently read as 'not big alpha'.
        mcly = next((c for c in (m.start + ofs_mcly, ds + ofs_mcly)
                     if 0 <= c <= len(data)-8 and data[c:c+4] == magic('MCLY')), -1)
        mcal = next((c for c in (m.start + ofs_mcal, ds + ofs_mcal)
                     if 0 <= c <= len(data)-8 and data[c:c+4] == magic('MCAL')), -1)
        if mcly < 0 or mcal < 0:
            continue
        mcal_size = r_u32(data, mcal + 4)
        alpha_offsets = []
        flags_by_layer = []
        for li in range(1, n_layers):
            entry = mcly + 8 + li * 16
            if entry + 16 > len(data):
                continue
            flags_by_layer.append(r_u32(data, entry + 4))
            alpha_offsets.append(r_u32(data, entry + 8))
        for idx, alpha_off in enumerate(alpha_offsets):
            next_off = alpha_offsets[idx + 1] if idx + 1 < len(alpha_offsets) else mcal_size
            layer_size = next_off - alpha_off
            if (flags_by_layer[idx] & 0x200) or layer_size >= 4096:
                return True
    return False


def normalize_wdt_bytes(wdt_bytes: bytes, present_tiles: set[Tuple[int, int]], big_alpha: bool) -> bytes:
    """Patch a copied WDT the same way Noggit would: shading flag on, present
    ADT tiles marked in MAIN, and big-alpha flag on only when inferred needed.
    """
    b = bytearray(wdt_bytes)
    chunks = scan_chunks(b)
    by_name = {name: (pos, size) for pos, name, size in chunks}
    if 'MPHD' not in by_name or 'MAIN' not in by_name:
        return build_wdt(present_tiles, big_alpha)
    mphd_pos, mphd_size = by_name['MPHD']
    if mphd_size >= 4:
        # Mirror Noggit's required shading flag. The big-alpha bit is map-global
        # and reverse mode may only be processing a subset of the map's tiles,
        # so never CLEAR an existing 0x04 based on subset inference (that would
        # make the client parse every other tile's 4096-byte MCAL as 4-bit);
        # only OR it in when the processed ADTs demonstrably need it.
        flags = r_u32(b, mphd_pos + 8) | 0x02
        if big_alpha:
            flags |= 0x04
        struct.pack_into('<I', b, mphd_pos + 8, flags)
    main_pos, main_size = by_name['MAIN']
    if main_size >= 64 * 64 * 8:
        for x, y in present_tiles:
            off = main_pos + 8 + (y * 64 + x) * 8
            flags = r_u32(b, off) | 1
            struct.pack_into('<I', b, off, flags)
    return bytes(b)


def build_mare_from_tile(tile: AdtTile) -> bytes:
    outer = []
    inner = []
    for oy in range(17):
        for ox in range(17):
            outer.append(i16(tile.grid[oy*8][ox*8]))
    for oy in range(16):
        for ox in range(16):
            inner.append(i16(bilinear_grid(tile.grid, ox*8 + 4, oy*8 + 4)))
    return chunk('MARE', b''.join(outer + inner))


def build_wdl_from_tiles(tiles: Dict[Tuple[int, int], AdtTile]) -> bytes:
    # Blizzard 3.3.5a WDLs carry empty MWMO/MWID/MODF between MVER and MAOF.
    pre = chunk('MVER', u32(18)) + chunk('MWMO', b'') + chunk('MWID', b'') + chunk('MODF', b'')
    maof_payload = bytearray(4096 * 4)
    area_chunks = bytearray()
    base_after_maof = len(pre) + 8 + len(maof_payload)
    for y in range(64):
        for x in range(64):
            t = tiles.get((x, y))
            if not t:
                continue
            abs_off = base_after_maof + len(area_chunks)
            struct.pack_into('<I', maof_payload, (y * 64 + x) * 4, abs_off)
            area_chunks.extend(build_mare_from_tile(t))
            # MAHO is optional hole data. Reverse mode currently preserves/edits
            # no terrain holes, so omit MAHO rather than writing all-ones masks.
    return pre + chunk('MAOF', bytes(maof_payload)) + bytes(area_chunks)


def splice_wdl(source: bytes, tiles: Dict[Tuple[int, int], AdtTile]) -> bytes:
    """Update a whole-map WDL in place of rebuilding it from a tile subset.

    The WDL covers all 64x64 tiles of the map; reverse mode often processes
    only a group of ADTs. Rebuilding from that subset would zero the MAOF
    entries of every other tile and delete their distant-horizon terrain.
    Instead, keep every chunk before MAOF verbatim, keep unprocessed tiles'
    MARE/MAHO blocks, and swap in freshly built MARE data (plus the original
    MAHO, holes are unedited) for the processed tiles.
    """
    chunks = scan_chunks(source)
    maof = next(((p, s) for p, n, s in chunks if n == 'MAOF'), None)
    if maof is None or maof[1] < 64 * 64 * 4:
        return build_wdl_from_tiles(tiles)
    maof_pos, _maof_size = maof

    def tile_block(off: int) -> bytes:
        if not (0 <= off <= len(source) - 8) or source[off:off+4] != magic('MARE'):
            return b''
        end = off + 8 + r_u32(source, off + 4)
        if source[end:end+4] == magic('MAHO'):
            end += 8 + r_u32(source, end + 4)
        return bytes(source[off:end])

    blocks: Dict[Tuple[int, int], bytes] = {}
    for y in range(64):
        for x in range(64):
            off = r_u32(source, maof_pos + 8 + (y * 64 + x) * 4)
            if off:
                blk = tile_block(off)
                if blk:
                    blocks[(x, y)] = blk
    for (x, y), t in tiles.items():
        old = blocks.get((x, y), b'')
        maho = b''
        if old:
            mare_end = 8 + r_u32(old, 4)
            if old[mare_end:mare_end+4] == magic('MAHO'):
                maho = old[mare_end:]
        blocks[(x, y)] = build_mare_from_tile(t) + maho

    head = bytes(source[:maof_pos])
    maof_payload = bytearray(4096 * 4)
    body = bytearray()
    base = len(head) + 8 + len(maof_payload)
    for y in range(64):
        for x in range(64):
            blk = blocks.get((x, y))
            if not blk:
                continue
            struct.pack_into('<I', maof_payload, (y * 64 + x) * 4, base + len(body))
            body.extend(blk)
    return head + chunk('MAOF', bytes(maof_payload)) + bytes(body)


def minimap_color(tile: AdtTile, px: int, py: int, palette: List[Tuple[int, int, int, int]], mn: float, mx: float, low_q: float) -> Tuple[int, Tuple[int, int, int]]:
    gx = (px + 0.5) / MINIMAP_SIZE * 128.0
    gy = (py + 0.5) / MINIMAP_SIZE * 128.0
    h = bilinear_grid(tile.grid, gx, gy)
    # approximate shade from local gradient
    h1 = bilinear_grid(tile.grid, gx + 1, gy)
    h2 = bilinear_grid(tile.grid, gx - 1, gy)
    h3 = bilinear_grid(tile.grid, gx, gy + 1)
    h4 = bilinear_grid(tile.grid, gx, gy - 1)
    shade = ((h2 - h1) + (h4 - h3)) * 0.015
    # Approx water display by wet chunks only; detailed MH2O bitmaps are not rewritten yet.
    is_wet = bool(tile.wet_chunks) and h <= low_q + 2.0
    if is_wet:
        idx = 140
        b, g, r, _ = palette[idx]
        return idx, (r, g, b)
    t = (h - mn) / max(1.0, mx - mn) + shade
    idx = max(0, min(127, int(t * 127)))
    b, g, r, _ = palette[idx]
    return idx, (r, g, b)


def generate_minimaps(out: Path, map_name: str, tiles: Dict[Tuple[int, int], AdtTile], cfg: dict) -> List[str]:
    pal = make_minimap_palette()
    lines = [f'dir: {map_name}']
    for (tx, ty), tile in sorted(tiles.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        indices = bytearray()
        rgb = []
        vals = [v for row in tile.grid for v in row]
        mn, mx = min(vals), max(vals)
        sorted_vals = sorted(vals)
        low_q = sorted_vals[max(0, int(len(sorted_vals) * 0.18) - 1)]
        for py in range(MINIMAP_SIZE):
            for px in range(MINIMAP_SIZE):
                idx, col = minimap_color(tile, px, py, pal, mn, mx, low_q)
                indices.append(idx)
                rgb.append(col)
        physical = f'{map_name}{tx:02d}{ty:02d}.blp'
        if cfg.get('outputs', {}).get('write_minimap_blp', True):
            write_blp_raw1(out / 'Textures' / 'Minimap' / physical, bytes(indices), MINIMAP_SIZE, MINIMAP_SIZE, pal)
        if cfg.get('outputs', {}).get('write_tga_previews', True):
            write_tga(out / 'minimap_previews' / map_name / f'map{tx}_{ty}.tga', rgb, MINIMAP_SIZE, MINIMAP_SIZE)
        # Exactly one tab between virtual and physical name; no trailing tab.
        lines.append(f'{map_name}\\map{tx}_{ty}.blp\t{physical}')
    if cfg.get('outputs', {}).get('write_md5translate_fragment', True):
        text = '\r\n'.join(lines) + '\r\n'
        write_file(out / 'Textures' / 'Minimap' / f'md5translate_{map_name.upper()}_FRAGMENT.trs', text.encode('ascii', errors='replace'))
        # md5translate.trs is one global file for ALL maps; a fragment-only
        # replacement wipes every stock map's minimap. Full-file write is a
        # disposable-test opt-in; merge the fragment for real patches.
        if cfg.get('outputs', {}).get('write_full_md5translate', False):
            write_file(out / 'Textures' / 'Minimap' / 'md5translate.trs', text.encode('ascii', errors='replace'))
    return lines

# -----------------------------------------------------------------------------
# Reports and orchestration
# -----------------------------------------------------------------------------

def report_lines(tiles: Dict[Tuple[int, int], AdtTile]) -> List[str]:
    lines = []
    lines.append('AI ADT One-Click v0.17 reverse analysis')
    lines.append('')
    for key in sorted(tiles, key=lambda k: (k[1], k[0])):
        t = tiles[key]
        m = t.metrics()
        lines.append(f"Tile {m['tile']} mode={m['mode']} h_range={m['height_range']} avg_slope={m['avg_slope']} rough={m['avg_roughness']} textures={m['textures']} objects={m['objects']} m2={m['m2']} wmo={m['wmo']} wet_chunks={m['wet_chunks']}")
        for w in t.parse_warnings:
            lines.append(f'  WARNING: {w}')
    lines.append('')
    lines.append('Mode meanings: preserve=no height edits; polish=conservative smoothing; regenerate=procedural redo blended into edges; M2/WMO are preserved but not edited.')
    return lines


def write_html_report(path: Path, tiles: Dict[Tuple[int, int], AdtTile], title: str):
    rows = []
    for key in sorted(tiles, key=lambda k: (k[1], k[0])):
        t = tiles[key]
        m = t.metrics()
        warn = '<br>'.join(t.parse_warnings)
        rows.append(
            '<tr>' + ''.join(f'<td>{m[k]}</td>' for k in ['tile','mode','height_min','height_max','height_range','avg_slope','avg_roughness','textures','objects','m2','wmo','wet_chunks']) + f'<td>{warn}</td></tr>'
        )
    html = f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><title>{title}</title>
<style>body{{font-family:Arial,sans-serif;margin:24px}}table{{border-collapse:collapse}}td,th{{border:1px solid #aaa;padding:4px 8px}}th{{background:#eee}}code{{background:#eee;padding:2px 4px}}</style>
</head><body><h1>{title}</h1>
<p>v0.11 reverse mode parses ADT height/texture/water/object metadata, applies region-mode terrain edits, preserves M2/WMO object chunks, then regenerates WDL and minimap outputs.</p>
<table><tr>{''.join(f'<th>{h}</th>' for h in ['tile','mode','height_min','height_max','height_range','avg_slope','avg_roughness','textures','objects','m2','wmo','wet_chunks','warnings'])}</tr>{''.join(rows)}</table>
<p>For half-complete maps, run this on ADT groups with a one-tile context margin when possible. Mark finished areas as <code>preserve</code> or <code>polish</code>, empty areas as <code>regenerate</code>.</p>
</body></html>"""
    write_file(path, html.encode('utf-8'))


def load_config(config_path: str | Path = 'reverse_config.json') -> dict:
    p = Path(config_path)
    if not p.exists():
        raise FileNotFoundError(f'Reverse config is missing: {p}')
    cfg = json.loads(p.read_text(encoding='utf-8'))
    cfg['_config_path'] = str(p)
    return cfg


def load_tiles(cfg: dict) -> Dict[Tuple[int, int], AdtTile]:
    tiles = {}
    for p in find_input_adts(cfg):
        t = parse_adt(p)
        tiles[(t.x, t.y)] = t
    for t in tiles.values():
        t.mode = choose_mode(t, cfg)
    return tiles


def analyze(config_path: str | Path = 'reverse_config.json'):
    cfg = load_config(config_path)
    tiles = load_tiles(cfg)
    out = Path(cfg.get('output_dir', 'output_reverse')) / 'analysis'
    out.mkdir(parents=True, exist_ok=True)
    txt = '\n'.join(report_lines(tiles)) + '\n'
    write_file(out / 'ANALYSIS_REPORT.txt', txt.encode('utf-8'))
    if cfg.get('outputs', {}).get('write_html_report', True):
        write_html_report(out / 'analysis_report.html', tiles, 'AI ADT v0.18 Reverse Analysis')
    print(txt)
    print(f'Analysis written to: {out.resolve()}')


def copy_passthrough_files(cfg: dict, out_root: Path, tiles: Dict[Tuple[int, int], AdtTile]):
    in_map = Path(cfg.get('input_dir', 'input_existing')) / 'world' / 'maps' / cfg.get('map_name', 'aigen')
    out_map = out_root / 'world' / 'maps' / cfg.get('map_name', 'aigen')
    out_map.mkdir(parents=True, exist_ok=True)
    map_name = cfg.get('map_name', 'aigen')
    # The WDT/WDL cover the whole map, not just the processed subset: mark
    # every ADT that exists in the input folder, or a cfg 'tiles' filter would
    # emit a WDT that hides all unprocessed tiles when packed as a patch.
    present = set(tiles.keys())
    for p in in_map.glob(f'{map_name}_*_*.adt'):
        info = parse_tile_name(p)
        if info:
            present.add((info[1], info[2]))
    inferred_big_alpha = any(tile_uses_big_alpha(t) for t in tiles.values())
    wdt_source: Optional[Path] = None
    # Copy non-ADT files first; WDL can be replaced after, WDT is normalized below.
    for p in in_map.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() == '.adt':
            continue
        if p.suffix.lower() == '.wdl' and cfg.get('outputs', {}).get('write_wdl', True):
            continue
        if p.name.lower() == f'{map_name.lower()}.wdt':
            wdt_source = p
            continue
        shutil.copy2(p, out_map / p.name)
    # Normalize or create the WDT. This mirrors Noggit's behavior: it turns on
    # shading and marks MAIN entries for ADTs that exist on disk. Unlike v0.17,
    # it does not blindly force big-alpha for every reverse input.
    if wdt_source and wdt_source.exists():
        write_file(out_map / f'{map_name}.wdt', normalize_wdt_bytes(wdt_source.read_bytes(), present, inferred_big_alpha))
    else:
        write_file(out_map / f'{map_name}.wdt', build_wdt(present, inferred_big_alpha))


def improve(config_path: str | Path = 'reverse_config.json'):
    cfg = load_config(config_path)
    tiles = load_tiles(cfg)
    apply_region_modes(tiles, cfg)
    fix_seams(tiles, cfg)
    for t in tiles.values():
        write_grid_back(t)

    out_root = Path(cfg.get('output_dir', 'output_reverse'))
    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    copy_passthrough_files(cfg, out_root, tiles)
    map_name = cfg.get('map_name', 'aigen')
    out_map = out_root / 'world' / 'maps' / map_name
    if cfg.get('outputs', {}).get('write_improved_adts', True):
        for (tx, ty), t in tiles.items():
            write_file(out_map / f'{map_name}_{tx}_{ty}.adt', bytes(t.data))
    if cfg.get('outputs', {}).get('write_wdl', True):
        # Splice into the source WDL when one exists so unprocessed tiles keep
        # their distant-horizon data; only build from scratch without a source.
        wdl_source = Path(cfg.get('input_dir', 'input_existing')) / 'world' / 'maps' / map_name / f'{map_name}.wdl'
        if wdl_source.exists():
            write_file(out_map / f'{map_name}.wdl', splice_wdl(wdl_source.read_bytes(), tiles))
        else:
            write_file(out_map / f'{map_name}.wdl', build_wdl_from_tiles(tiles))
    generate_minimaps(out_root, map_name, tiles, cfg)
    report = '\n'.join(report_lines(tiles)) + '\n'
    write_file(out_root / 'REVERSE_IMPROVE_REPORT.txt', report.encode('utf-8'))
    if cfg.get('outputs', {}).get('write_html_report', True):
        write_html_report(out_root / 'reverse_improve_report.html', tiles, 'AI ADT v0.18 Reverse Improve Report')
    print(report)
    print(f'Improved loose patch written to: {out_root.resolve()}')
    print('Pack output_reverse into an MPQ patch to test. Object chunks were preserved, not edited.')


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ('analyze', 'improve'):
        print('Usage: python reverse_tool.py analyze|improve [reverse_config.json]')
        sys.exit(2)
    cfg_path = sys.argv[2] if len(sys.argv) > 2 else 'reverse_config.json'
    if sys.argv[1] == 'analyze':
        analyze(cfg_path)
    else:
        improve(cfg_path)

if __name__ == '__main__':
    main()
