#!/usr/bin/env python3
"""Structural validator for AI ADT One-Click v0.18 outputs.

This does not replace a real 3.3.5a client test. It catches the mistakes most
likely to make the client/Noggit reject files: reversed chunk magic, MCIN offsets,
MCNK subchunk offsets, MCLY/MCAL alpha flags/sizes, MH2O offsets, WDT tile table,
WDL MAOF offsets, and minimap BLP headers.
"""
import re
import struct
import sys
from pathlib import Path

MCNK_HEADER_SIZE = 128
ADT_SIZE = 533.3333333333334
CHUNK_SIZE = ADT_SIZE / 16.0
WORLD_HALF = ADT_SIZE * 64.0 / 2.0


def u32(b, o): return struct.unpack_from('<I', b, o)[0]
def f32(b, o): return struct.unpack_from('<f', b, o)[0]

def magic(name: str) -> bytes:
    return name[::-1].encode('ascii')

def display(raw: bytes) -> str:
    return raw[::-1].decode('ascii', errors='replace')

def scan_chunks(b):
    pos=0; out=[]
    while pos + 8 <= len(b):
        raw=b[pos:pos+4]; name=display(raw); size=u32(b,pos+4)
        if pos + 8 + size > len(b):
            raise AssertionError(f'Chunk {name} at {pos} overruns file')
        out.append((pos,name,size)); pos += 8 + size
    if pos != len(b):
        raise AssertionError(f'Trailing unparsed bytes: ended at {pos}, file len {len(b)}')
    return out

def decompress_rle_alpha(data: bytes) -> bytes:
    out=bytearray(); i=0
    while i < len(data) and len(out) < 4096:
        c=data[i]; i+=1; mode=c & 0x80; count=c & 0x7F
        if count == 0:
            raise AssertionError('RLE alpha control byte had zero count')
        if mode:
            assert i < len(data), 'RLE alpha EOF in fill mode'
            out.extend([data[i]] * count); i += 1
        else:
            assert i + count <= len(data), 'RLE alpha EOF in copy mode'
            out.extend(data[i:i+count]); i += count
    assert len(out) == 4096, f'RLE alpha decompressed to {len(out)} bytes, expected 4096'
    return bytes(out)

def expected_tile_xy(path: Path):
    m = re.search(r'_(\d+)_(\d+)\.adt$', path.name, re.I)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def extract_outer_grid(path: Path):
    """Extract the 129x129 outer/corner height grid from an ADT.

    This is intentionally independent from check_adt(): it lets the validator
    compare adjacent ADT borders and catch seam bugs that a per-file structure
    check would miss.
    """
    b = path.read_bytes()
    top = scan_chunks(b)
    mcin = next((p for p,n,s in top if n == 'MCIN'), None)
    assert mcin is not None, (path, 'missing MCIN for seam extraction')
    grid = [[None for _ in range(129)] for _ in range(129)]
    mcin_data = mcin + 8
    for i in range(256):
        off = u32(b, mcin_data + i*16)
        if not off:
            continue
        ds = off + 8
        cx = u32(b, ds + 4)
        cy = u32(b, ds + 8)
        assert 0 <= cx < 16 and 0 <= cy < 16, (path, i, 'bad chunk indices for seam extraction', cx, cy)
        base_h = f32(b, ds + 112)  # MapChunkHeader.ypos
        ofs_mcvt = u32(b, ds + 20)
        mcvt = off + ofs_mcvt
        assert b[mcvt:mcvt+4] == magic('MCVT'), (path, i, 'missing MCVT for seam extraction')
        pos = mcvt + 8
        idx = 0
        for row in range(17):
            if row % 2 == 0:
                gy = cy * 8 + row // 2
                for col in range(9):
                    gx = cx * 8 + col
                    h = base_h + f32(b, pos + idx*4)
                    if grid[gy][gx] is None:
                        grid[gy][gx] = h
                    else:
                        grid[gy][gx] = (grid[gy][gx] + h) * 0.5
                    idx += 1
            else:
                idx += 8
    for y in range(129):
        for x in range(129):
            assert grid[y][x] is not None, (path, 'missing outer-grid sample', x, y)
    return grid


def check_adt_seams(adt_by_map: dict[Path, list[Path]], tolerance: float = 0.03):
    for folder, paths in sorted(adt_by_map.items(), key=lambda kv: str(kv[0])):
        grids = {}
        for path in paths:
            xy = expected_tile_xy(path)
            if xy is not None:
                grids[xy] = (path, extract_outer_grid(path))
        for (x, y), (path, grid) in sorted(grids.items()):
            right = grids.get((x + 1, y))
            if right:
                rpath, rgrid = right
                diffs = [abs(grid[row][128] - rgrid[row][0]) for row in range(129)]
                md = max(diffs)
                assert md <= tolerance, (path, rpath, 'horizontal ADT seam mismatch', md)
            below = grids.get((x, y + 1))
            if below:
                bpath, bgrid = below
                diffs = [abs(grid[128][col] - bgrid[0][col]) for col in range(129)]
                md = max(diffs)
                assert md <= tolerance, (path, bpath, 'vertical ADT seam mismatch', md)
    if adt_by_map:
        print('OK ADT seams: adjacent ADT border heights match')

def check_adt(path: Path):
    tile_xy = expected_tile_xy(path)
    b=path.read_bytes(); top=scan_chunks(b); names=[n for _,n,_ in top[:16]]
    for need in ['MVER','MHDR','MCIN','MTEX']:
        assert need in names, (path, need, names)
    mtex_pos, _, mtex_size = next((p,n,s) for p,n,s in top if n=='MTEX')
    mtex_payload = b[mtex_pos+8:mtex_pos+8+mtex_size]
    assert mtex_size == 0 or mtex_payload.endswith(b'\0'), (path, 'MTEX must end with one NUL terminator')
    # Noggit scans MTEX until chunk end; padding NULs create empty texture names.
    assert b'\0\0' not in mtex_payload, (path, 'MTEX contains an empty texture name; likely NUL padding')
    mcin = next((p for p,n,s in top if n=='MCIN'), None); assert mcin is not None
    mcin_data = mcin+8; mcnk_count=0; alpha_maps=0; compressed_maps=0
    pos_y_samples=[]
    for i in range(256):
        off=u32(b,mcin_data+i*16); size=u32(b,mcin_data+i*16+4)
        if off:
            assert 0 <= off <= len(b)-8, (path, i, off, len(b))
            assert b[off:off+4] == magic('MCNK'), (path, i, off, b[off:off+4])
            assert u32(b, off+4) + 8 == size, (path, i, 'MCIN size must include MCNK header', size, u32(b,off+4))
            ds = off + 8
            assert ds + MCNK_HEADER_SIZE <= len(b), (path, i, 'MCNK header truncated')
            n_layers = u32(b, ds + 12)
            ofs_mcvt = u32(b, ds + 20); ofs_mcnr=u32(b,ds+24); ofs_mcly = u32(b, ds + 28)
            ofs_mcal = u32(b, ds + 36); size_alpha = u32(b, ds + 40)
            assert ofs_mcvt >= 8 + MCNK_HEADER_SIZE, (path, i, 'MCVT offset before 128-byte MCNK header', ofs_mcvt)
            assert ofs_mcnr > ofs_mcvt and ofs_mcly > ofs_mcnr, (path, i, 'subchunk offsets not increasing', ofs_mcvt, ofs_mcnr, ofs_mcly)
            holes = u32(b, ds + 60)
            assert holes == 0, (path, i, 'generated chunks should not contain holes yet', holes)
            lq_map = b[ds+64:ds+80]
            assert len(lq_map) == 16, (path, i, 'low quality texture map length')
            flags = u32(b, ds + 0)
            assert flags & (1 << 15), (path, i, 'missing do_not_fix_alpha_map flag')
            # Noggit MapHeaders.h stores zpos,xpos,ypos at 0x68/0x6C/0x70.
            # ypos is the vertical/base height used with MCVT deltas.
            if tile_xy is not None:
                tx, ty = tile_xy
                cx = i % 16; cy = i // 16
                expected_xpos = WORLD_HALF - (tx * ADT_SIZE + cx * CHUNK_SIZE)
                expected_zpos = WORLD_HALF - (ty * ADT_SIZE + cy * CHUNK_SIZE)
                got_zpos = f32(b, ds+104); got_xpos = f32(b, ds+108)
                assert abs(got_xpos - expected_xpos) < 0.02, (path, i, 'xpos mismatch', got_xpos, expected_xpos)
                assert abs(got_zpos - expected_zpos) < 0.02, (path, i, 'zpos mismatch', got_zpos, expected_zpos)
            pos_y_samples.append(f32(b, ds+112))
            cand = off + ofs_mcvt
            assert cand <= len(b)-8 and b[cand:cand+4] == magic('MCVT'), (path, i, 'MCVT', cand, b[cand:cand+4])
            assert u32(b, cand+4) == 145*4, (path, i, 'MCVT size', u32(b,cand+4))
            candn = off + ofs_mcnr
            assert candn <= len(b)-8 and b[candn:candn+4] == magic('MCNR'), (path, i, 'MCNR', candn, b[candn:candn+4])
            assert u32(b, candn+4) == 145*3, (path, i, 'MCNR size', u32(b,candn+4))
            after_mcnr_unknown = candn + 8 + 145*3
            assert ofs_mcly == after_mcnr_unknown + 13 - off, (path, i, 'expected 13 unknown bytes after MCNR', ofs_mcly, after_mcnr_unknown + 13 - off)
            assert 1 <= n_layers <= 4, (path, i, 'bad layer count', n_layers)
            cand = off + ofs_mcly
            assert cand <= len(b)-8 and b[cand:cand+4] == magic('MCLY'), (path, i, 'MCLY', cand, b[cand:cand+4])
            assert u32(b, cand+4) >= n_layers * 16, (path, i, 'MCLY size', u32(b,cand+4), n_layers)
            cand_mcrf = off + u32(b, ds + 32)
            assert cand_mcrf <= len(b)-8 and b[cand_mcrf:cand_mcrf+4] == magic('MCRF'), (path, i, 'MCRF', cand_mcrf, b[cand_mcrf:cand_mcrf+4])
            cand2 = off + ofs_mcal
            assert cand2 <= len(b)-8 and b[cand2:cand2+4] == magic('MCAL'), (path, i, 'MCAL', cand2, b[cand2:cand2+4])
            mcal_payload_size = u32(b, cand2+4)
            assert size_alpha == 8 + mcal_payload_size, (path, i, 'sizeAlpha should include MCAL header', size_alpha, mcal_payload_size)
            ofs_mcse = u32(b, ds + 88)
            cand_mcse = off + ofs_mcse
            assert cand_mcse <= len(b)-8 and b[cand_mcse:cand_mcse+4] == magic('MCSE'), (path, i, 'MCSE', cand_mcse, b[cand_mcse:cand_mcse+4])
            if n_layers > 1:
                layer_offsets=[]
                for li in range(1, n_layers):
                    lo = cand + 8 + li*16
                    flags = u32(b, lo+4); alpha_off = u32(b, lo+8)
                    assert flags & 0x100, (path, i, li, 'missing use-alpha flag')
                    assert alpha_off < max(1, mcal_payload_size), (path, i, li, alpha_off, mcal_payload_size)
                    layer_offsets.append((li, flags, alpha_off))
                for idx,(li,flags,alpha_off) in enumerate(layer_offsets):
                    next_off = layer_offsets[idx+1][2] if idx+1 < len(layer_offsets) else mcal_payload_size
                    assert next_off > alpha_off, (path, i, li, 'non-increasing alpha offsets', alpha_off, next_off)
                    alpha_blob=b[cand2+8+alpha_off:cand2+8+next_off]
                    alpha_maps += 1
                    if flags & 0x200:
                        compressed_maps += 1
                        decompress_rle_alpha(alpha_blob)
                    else:
                        assert len(alpha_blob) in (2048,4096), (path, i, li, 'raw alpha length', len(alpha_blob))
            mcnk_count+=1
    assert mcnk_count == 256, (path, 'expected 256 MCNKs', mcnk_count)
    mh2o = next((p for p,n,s in top if n=='MH2O'), None); wet=0
    if mh2o is not None:
        mh2o_size = u32(b, mh2o+4)
        assert mh2o_size >= 256*12, (path, 'MH2O chunk too small; omit chunk entirely if no water', mh2o_size)
        start=mh2o+8
        for i in range(256):
            off=u32(b,start+i*12); count=u32(b,start+i*12+4); attr=u32(b,start+i*12+8)
            if count:
                assert off >= 256*12 and off+24*count <= mh2o_size, (path, i, 'MH2O info offset', off, count, mh2o_size)
                if attr:
                    assert attr >= 256*12 and attr + 16 <= mh2o_size, (path, i, 'MH2O attributes offset', attr, mh2o_size)
                for layer in range(count):
                    ipos = mh2o + 8 + off + layer*24
                    liquid_id = struct.unpack_from('<H', b, ipos)[0]
                    lvf = struct.unpack_from('<H', b, ipos+2)[0]
                    width = b[ipos+14]; height = b[ipos+15]
                    mask_off = u32(b, ipos+16); hm_off = u32(b, ipos+20)
                    assert liquid_id >= 0 and lvf in (0,1,2), (path, i, 'bad liquid id/format', liquid_id, lvf)
                    assert 1 <= width <= 8 and 1 <= height <= 8, (path, i, 'bad water dimensions', width, height)
                    if mask_off:
                        assert mask_off >= 256*12 and mask_off + 8 <= mh2o_size, (path, i, 'MH2O mask offset', mask_off, mh2o_size)
                    if hm_off:
                        verts = (width + 1) * (height + 1)
                        # Match Noggit liquid_layer::save payload sizing.
                        if lvf == 0:
                            min_payload = verts * 4 + verts
                        elif lvf == 1:
                            min_payload = verts * 4 + verts * 4
                        else:  # lvf == 2
                            min_payload = verts
                        assert hm_off >= 256*12 and hm_off + min_payload <= mh2o_size, (path, i, 'MH2O height/depth offset', hm_off, min_payload, mh2o_size)
                wet+=1
    vertical_min=min(pos_y_samples); vertical_max=max(pos_y_samples)
    print(f'OK ADT {path}: {len(b)} bytes, MCNK={mcnk_count}, wet_MH2O_chunks={wet}, alpha_maps={alpha_maps} compressed={compressed_maps}, base_height_range={vertical_min:.1f}..{vertical_max:.1f}')


def adt_requires_big_alpha(path: Path) -> bool:
    b = path.read_bytes()
    try:
        top = scan_chunks(b)
        mcin = next((p for p,n,s in top if n=='MCIN'), None)
        if mcin is None:
            return False
        mcin_data = mcin + 8
        for i in range(256):
            off = u32(b, mcin_data + i*16)
            if not off or off + 8 > len(b) or b[off:off+4] != magic('MCNK'):
                continue
            ds = off + 8
            n_layers = u32(b, ds + 12)
            if n_layers <= 1 or n_layers > 4:
                continue
            ofs_mcly = u32(b, ds + 28); ofs_mcal = u32(b, ds + 36)
            mcly = off + ofs_mcly; mcal = off + ofs_mcal
            if not (0 <= mcly <= len(b)-8 and b[mcly:mcly+4] == magic('MCLY')):
                continue
            if not (0 <= mcal <= len(b)-8 and b[mcal:mcal+4] == magic('MCAL')):
                continue
            mcal_size = u32(b, mcal+4)
            alpha_offsets=[]; flags=[]
            for li in range(1, n_layers):
                entry = mcly + 8 + li*16
                if entry + 16 > len(b):
                    continue
                flags.append(u32(b, entry+4)); alpha_offsets.append(u32(b, entry+8))
            for idx, alpha_off in enumerate(alpha_offsets):
                next_off = alpha_offsets[idx+1] if idx+1 < len(alpha_offsets) else mcal_size
                if (flags[idx] & 0x200) or (next_off - alpha_off) >= 4096:
                    return True
    except Exception:
        return False
    return False

def check_wdt(path: Path, required_tiles: set[tuple[int,int]] | None = None, needs_big_alpha: bool = False):
    b=path.read_bytes(); top=scan_chunks(b); assert [n for _,n,_ in top[:3]] == ['MVER','MPHD','MAIN']
    mphd=next(p for p,n,s in top if n=='MPHD'); mphd_flags=u32(b,mphd+8)
    assert mphd_flags & 0x02, (path, 'MPHD missing Noggit FLAG_SHADING 0x02', mphd_flags)
    if needs_big_alpha:
        assert mphd_flags & 0x04, (path, 'ADTs use big/compressed alphamaps but WDT missing MPHD big-alpha flag 0x04', mphd_flags)
    else:
        assert not (mphd_flags & 0x04), (path, 'WDT has big-alpha flag 0x04 but no ADT appears to require big/compressed alphamaps', mphd_flags)
    if mphd_flags & 0x04:
        assert (mphd_flags & 0x06) == 0x06, (path, 'big-alpha WDT should also carry shading flag', mphd_flags)
    main = next(p for p,n,s in top if n=='MAIN'); present=[]
    for y in range(64):
        for x in range(64):
            flags=u32(b,main+8+(y*64+x)*8)
            if flags & 1: present.append((x,y))
    if required_tiles is not None:
        missing = sorted(required_tiles - set(present))
        assert not missing, (path, 'WDT MAIN missing present ADT tiles', missing)
    print(f'OK WDT {path}: mphd_flags=0x{mphd_flags:08X}, present={present}')

def check_wdl(path: Path):
    b=path.read_bytes(); top=scan_chunks(b); assert [n for _,n,_ in top[:2]] == ['MVER','MAOF'], [n for _,n,_ in top]
    maof=next(p for p,n,s in top if n=='MAOF'); present=[]
    for y in range(64):
        for x in range(64):
            off=u32(b, maof+8+(y*64+x)*4)
            if off:
                assert b[off:off+4] == magic('MARE'), (path, x, y, off, b[off:off+4])
                assert u32(b, off+4) == 545*2, (path, x, y, u32(b,off+4))
                after_mare = off + 8 + 545*2
                # MAHO is optional. If a generated WDL includes it, it must not
                # mark holes for our no-holes terrain. Older builds wrote
                # 0xFFFF here, which likely meant "all holes" to consumers that
                # do parse MAHO.
                if after_mare + 8 <= len(b) and b[after_mare:after_mare+4] == magic('MAHO'):
                    maho_size = u32(b, after_mare+4)
                    assert maho_size == 32, (path, x, y, 'MAHO size', maho_size)
                    maho_payload = b[after_mare+8:after_mare+8+maho_size]
                    assert maho_payload == b'\0' * 32, (path, x, y, 'generated no-holes MAHO must be all zero')
                present.append((x,y))
    print(f'OK WDL {path}: MARE tiles={present}')

def check_blp(path: Path):
    b=path.read_bytes(); assert b[:4] == b'BLP2', path
    typ=u32(b,4); comp=b[8]; width=u32(b,12); height=u32(b,16); off0=u32(b,20); len0=u32(b,84)
    assert typ == 1 and comp == 1 and width == 256 and height == 256, (path, typ, comp, width, height)
    assert off0 + len0 <= len(b), (path, off0, len0, len(b))
    print(f'OK BLP {path}: {width}x{height}, data={len0}')

root=Path(sys.argv[1]) if len(sys.argv) > 1 else Path('output_loose')
map_root = root/'world'/'maps'
adt_by_map: dict[Path, list[Path]] = {}
for adt in sorted(map_root.rglob('*.adt')):
    adt_by_map.setdefault(adt.parent, []).append(adt)
for p in sorted(map_root.rglob('*.wdt')):
    adts = adt_by_map.get(p.parent, [])
    required = set()
    for adt in adts:
        xy = expected_tile_xy(adt)
        if xy is not None:
            required.add(xy)
    needs_big = any(adt_requires_big_alpha(adt) for adt in adts)
    check_wdt(p, required, needs_big)
for p in sorted(map_root.rglob('*.wdl')): check_wdl(p)
for p in sorted(map_root.rglob('*.adt')): check_adt(p)
check_adt_seams(adt_by_map)
for p in sorted((root/'Textures'/'Minimap').glob('*.blp')): check_blp(p)
print(f'All structural checks passed for {root}.')
