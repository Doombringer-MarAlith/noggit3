#!/usr/bin/env python3
"""
AI ADT One-Click v0.14 - local ADT style learner

This does NOT train a neural net. It distills a folder of existing ADTs into
machine-consumable rules for generate_adt.py / ai_zone_spec.py:
  - which terrain texture paths are actually used locally
  - which paths appear to be base/road/rock/shore/snow/forest/etc.
  - typical texture layer counts per MCNK
  - rough slope/height/style heuristics

Point it at your own extracted client/custom map folders. It ships no assets.
"""
from __future__ import annotations
import argparse, json, math, re, struct
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

ADT_SIZE = 533.3333333333334
CHUNK_SIZE = ADT_SIZE / 16.0
UNIT = CHUNK_SIZE / 8.0

def u32(b,o): return struct.unpack_from('<I', b, o)[0]
def f32(b,o): return struct.unpack_from('<f', b, o)[0]
def magic(name: str) -> bytes: return name[::-1].encode('ascii')
def display(raw: bytes) -> str: return raw[::-1].decode('ascii','replace')

def scan_chunks(b: bytes, start=0, end=None):
    if end is None: end=len(b)
    pos=start; out=[]
    while pos+8 <= end:
        raw=b[pos:pos+4]; name=display(raw)
        size=u32(b,pos+4)
        if size < 0 or pos+8+size > end: break
        out.append((pos,name,size)); pos += 8+size
    return out

def strings_from_zero_blob(blob: bytes) -> List[str]:
    return [p.decode('ascii','replace') for p in blob.split(b'\0') if p]

def classify_role(path: str) -> str:
    p=path.lower().replace('/','\\')
    name=p.split('\\')[-1]
    if any(w in p for w in ['road','path','trail']) or 'dirt' in name or 'cobble' in name:
        return 'road'
    if any(w in p for w in ['rock','cliff','stone','mountain']):
        return 'rock'
    if any(w in p for w in ['snow','ice','frozen']):
        return 'snow'
    if any(w in p for w in ['sand','beach','desert','tanaris','uldum']):
        return 'sand'
    if any(w in p for w in ['mud','shore','swamp','marsh','bog','moss']):
        return 'shore'
    if any(w in p for w in ['plague','dead','scarlet','epl','wpl']):
        return 'plague'
    if any(w in p for w in ['forest','pine','leaf','grass','elwynn','ashenvale','grizzly','silverpine']):
        return 'forest' if any(w in p for w in ['forest','pine','leaf','ashenvale','silverpine']) else 'base'
    return 'base'


def classify_theme(path: str) -> str:
    p = path.lower().replace('/', '\\')
    if any(w in p for w in ['stranglethorn', 'jungle', 'zulgurub']): return 'jungle'
    if any(w in p for w in ['swamp', 'marsh', 'bog', 'wetlands', 'sorrow']): return 'swamp'
    if any(w in p for w in ['snow', 'ice', 'winterspring', 'dunmorogh']): return 'snow'
    if any(w in p for w in ['tanaris', 'silithus', 'desert', 'uldum', 'sand']): return 'desert'
    if any(w in p for w in ['plague', 'easternplaguelands', 'westernplaguelands', 'undead']): return 'plague'
    if any(w in p for w in ['northrend', 'borean', 'grizzly', 'howlingfjord', 'dragonblight', 'zuldrak', 'sholazar']): return 'northrend'
    if any(w in p for w in ['forest', 'elwynn', 'ashenvale', 'silverpine', 'feralas', 'hinterlands']): return 'forest'
    return 'generic'

def read_mtex(b: bytes, top) -> List[str]:
    for pos,n,size in top:
        if n=='MTEX': return strings_from_zero_blob(b[pos+8:pos+8+size])
    return []

def read_mcvt_heights(b: bytes, mcnk_off: int, ds: int, mcvt_off: int) -> Optional[List[float]]:
    base_z=f32(b, ds+112)  # Noggit header.ypos is the vertical base height
    for cand in (mcnk_off+mcvt_off, ds+mcvt_off):
        if 0 <= cand <= len(b)-8 and b[cand:cand+4] == magic('MCVT'):
            size=u32(b,cand+4)
            if size >= 145*4:
                return [base_z + f32(b, cand+8+i*4) for i in range(145)]
    return None

def outer_grid_from_mcvtheights(vals: List[float]) -> List[List[float]]:
    grid=[[0.0]*9 for _ in range(9)]
    idx=0
    for row in range(17):
        if row%2==0:
            gy=row//2
            for col in range(9):
                grid[gy][col]=vals[idx]; idx+=1
        else:
            idx+=8
    return grid

def avg_slope_from_outer(grid: List[List[float]]) -> float:
    slopes=[]
    for y in range(1,8):
        for x in range(1,8):
            dx=(grid[y][x-1]-grid[y][x+1])/(2*UNIT)
            dy=(grid[y-1][x]-grid[y+1][x])/(2*UNIT)
            slopes.append(math.degrees(math.atan(math.sqrt(dx*dx+dy*dy))))
    return sum(slopes)/len(slopes) if slopes else 0.0

def parse_layers_for_mcnk(b: bytes, mcnk_off: int, size: int, textures: List[str]) -> List[Tuple[int,int,int,int,str,str]]:
    ds=mcnk_off+8
    if ds+128 > len(b): return []
    n_layers=u32(b, ds+12)
    ofs_mcly=u32(b, ds+28)
    cands=[mcnk_off+ofs_mcly, ds+ofs_mcly]
    mcly=-1
    for c in cands:
        if 0 <= c <= len(b)-8 and b[c:c+4]==magic('MCLY'): mcly=c; break
    if mcly<0: return []
    sz=u32(b, mcly+4)
    n=min(n_layers, sz//16, 4)
    out=[]
    for i in range(n):
        o=mcly+8+i*16
        tid=u32(b,o); flags=u32(b,o+4); off=u32(b,o+8); eff=u32(b,o+12)
        path=textures[tid] if 0 <= tid < len(textures) else f'<bad_texture_{tid}>'
        out.append((tid, flags, off, eff, path, classify_role(path)))
    return out

def analyze_adt(path: Path) -> Dict[str, Any]:
    b=path.read_bytes(); top=scan_chunks(b); textures=read_mtex(b, top)
    by={n:(p,s) for p,n,s in top}
    tile={"path":str(path),"textures":textures,"chunks":0,"layer_counts":Counter(),"role_counts":Counter(),"theme_counts":Counter(),"texture_counts":Counter(),"role_slope":defaultdict(list),"warnings":[],
          "texture_effects":defaultdict(Counter),"mcnk_flags":Counter(),"holes_chunks":0,"base_heights":[],
          "liquid_ids":Counter(),"liquid_lvf":Counter(),"liquid_depths":[],"wet_chunks":0}
    if 'MCIN' not in by:
        tile['warnings'].append('missing MCIN')
        return tile
    mcin,size=by['MCIN']
    for i in range(256):
        off=u32(b,mcin+8+i*16); sz=u32(b,mcin+8+i*16+4)
        if off <= 0 or off+8 > len(b) or b[off:off+4] != magic('MCNK'): continue
        ds=off+8
        if ds+128 > len(b): continue
        vals=read_mcvt_heights(b, off, ds, u32(b, ds+20))
        slope=0.0
        if vals:
            slope=avg_slope_from_outer(outer_grid_from_mcvtheights(vals))
        layers=parse_layers_for_mcnk(b, off, sz, textures)
        tile['chunks'] += 1
        tile['layer_counts'][len(layers)] += 1
        tile['mcnk_flags'][u32(b, ds)] += 1
        if u32(b, ds+60) & 0xFFFF:
            tile['holes_chunks'] += 1
        tile['base_heights'].append(f32(b, ds+112))
        for li,(_tid,flags,_ao,eff,path2,role) in enumerate(layers):
            tile['texture_counts'][path2] += 1
            tile['role_counts'][role] += 1
            tile['theme_counts'][classify_theme(path2)] += 1
            tile['role_slope'][role].append(slope)
            # GroundEffectTexture ids per texture: this is what makes grass/pebble
            # ground doodads appear in the client, so learn them per path.
            tile['texture_effects'][path2][eff] += 1
    analyze_mh2o(b, by, tile)
    return tile


def analyze_mh2o(b: bytes, by: Dict[str, Tuple[int,int]], tile: Dict[str, Any]) -> None:
    """Liquid ids / vertex formats / depth bytes from MH2O, per Noggit's layout."""
    if 'MH2O' not in by:
        return
    pos, size = by['MH2O']
    if size < 256*12:
        return
    base = pos + 8
    for i in range(256):
        ofs_info = u32(b, base + i*12); n_layers = u32(b, base + i*12 + 4)
        if not n_layers or not ofs_info:
            continue
        tile['wet_chunks'] += 1
        for l in range(min(n_layers, 4)):
            ip = base + ofs_info + l*24
            if ip + 24 > base + size: break
            liquid_id = struct.unpack_from('<H', b, ip)[0]
            lvf = struct.unpack_from('<H', b, ip+2)[0]
            w = b[ip+14]; h = b[ip+15]
            ofs_hm = u32(b, ip+20)
            tile['liquid_ids'][liquid_id] += 1
            tile['liquid_lvf'][lvf] += 1
            if ofs_hm and lvf in (0, 2) and 1 <= w <= 8 and 1 <= h <= 8:
                nverts = (w+1)*(h+1)
                dpos = base + ofs_hm + (nverts*4 if lvf == 0 else 0)
                if dpos + nverts <= base + size:
                    tile['liquid_depths'].extend(b[dpos:dpos+nverts:max(1, nverts//8)])

def percentile(vals: List[float], p: float) -> float:
    if not vals: return 0.0
    vals=sorted(vals); idx=max(0,min(len(vals)-1,int(round((len(vals)-1)*p))))
    return vals[idx]

def build_rules(results: List[Dict[str, Any]], source_root: str) -> Dict[str, Any]:
    texture_counts=Counter(); role_counts=Counter(); theme_counts=Counter(); layer_counts=Counter(); role_slope=defaultdict(list)
    texture_effects=defaultdict(Counter); mcnk_flags=Counter(); holes_chunks=0; base_heights=[]
    liquid_ids=Counter(); liquid_lvf=Counter(); liquid_depths=[]; wet_chunks=0
    warnings=[]; chunks=0
    for r in results:
        texture_counts.update(r['texture_counts']); role_counts.update(r['role_counts']); theme_counts.update(r.get('theme_counts', Counter())); layer_counts.update(r['layer_counts']); chunks += r['chunks']; warnings += r['warnings'][:3]
        for role, vals in r['role_slope'].items(): role_slope[role].extend(vals)
        for path2, effs in r.get('texture_effects', {}).items(): texture_effects[path2].update(effs)
        mcnk_flags.update(r.get('mcnk_flags', Counter())); holes_chunks += r.get('holes_chunks', 0)
        base_heights.extend(r.get('base_heights', [])); liquid_ids.update(r.get('liquid_ids', Counter()))
        liquid_lvf.update(r.get('liquid_lvf', Counter())); liquid_depths.extend(r.get('liquid_depths', [])); wet_chunks += r.get('wet_chunks', 0)
    # Most common GroundEffectTexture id per texture path; 0xFFFF/0xFFFFFFFF mean 'none'.
    effect_ids={}
    for path2, effs in texture_effects.items():
        eff, _cnt = effs.most_common(1)[0]
        effect_ids[path2] = int(eff)
    by_role=defaultdict(list)
    by_theme_role=defaultdict(lambda: defaultdict(list))
    for path,count in texture_counts.most_common():
        role=classify_role(path); theme=classify_theme(path)
        by_role[role].append((path,count))
        by_theme_role[theme][role].append((path,count))
    recommended=[]
    role_priority={'base':0.05,'forest':0.35,'shore':0.55,'sand':0.55,'snow':0.50,'plague':0.52,'rock':0.70,'road':0.95}
    for role in ['base','forest','road','rock','shore','sand','snow','plague']:
        if by_role.get(role):
            path,count=by_role[role][0]
            recommended.append({'role':role,'path':path,'label':path.split('\\')[-1] or role,'source_count':count,'priority':role_priority.get(role,0.4)})
    # Ensure minimum set if source lacks obvious roads/rocks.
    if not any(x['role']=='base' for x in recommended) and texture_counts:
        p,c=texture_counts.most_common(1)[0]
        recommended.insert(0, {'role':'base','path':p,'label':p.split('\\')[-1],'source_count':c,'priority':0.05})
    recommended_by_theme={}
    for theme, role_map in sorted(by_theme_role.items()):
        arr=[]
        for role in ['base','forest','road','rock','shore','sand','snow','plague']:
            if role_map.get(role):
                path,count=role_map[role][0]
                arr.append({'role':role,'path':path,'label':path.split('\\')[-1] or role,'source_count':count,'priority':role_priority.get(role,0.4),'theme':theme})
        if not any(x['role']=='base' for x in arr):
            # choose most common theme texture as fallback base.
            all_theme=[]
            for vals in role_map.values(): all_theme.extend(vals)
            if all_theme:
                path,count=sorted(all_theme, key=lambda pc: pc[1], reverse=True)[0]
                arr.insert(0, {'role':'base','path':path,'label':path.split('\\')[-1] or 'base','source_count':count,'priority':0.05,'theme':theme})
        if arr: recommended_by_theme[theme]=arr[:12]
    avg_layers=sum(k*v for k,v in layer_counts.items())/max(1,sum(layer_counts.values()))
    rules={
        'version':'0.7',
        'source_root':source_root,
        'source_adts':len(results),
        'source_chunks':chunks,
        'summary':{
            'avg_layers_per_chunk':round(avg_layers,3),
            'layer_count_distribution':{str(k):v for k,v in sorted(layer_counts.items())},
            'top_roles':role_counts.most_common(20),
            'top_themes':theme_counts.most_common(20),
            'top_textures':texture_counts.most_common(50),
            'warnings':warnings[:20]
        },
        'recommended_texture_layers':recommended[:12],
        'recommended_texture_layers_by_theme':recommended_by_theme,
        'texture_effect_ids':effect_ids,
        'terrain_stats':{
            'base_height_p05':round(percentile(base_heights,0.05),2),'base_height_p50':round(percentile(base_heights,0.50),2),
            'base_height_p95':round(percentile(base_heights,0.95),2),'holes_chunk_fraction':round(holes_chunks/max(1,chunks),4),
            'mcnk_flags_top':[(hex(f),c) for f,c in mcnk_flags.most_common(8)]
        },
        'liquid_stats':{
            'wet_chunk_fraction':round(wet_chunks/max(1,chunks),4),
            'liquid_ids':liquid_ids.most_common(10),'vertex_formats':liquid_lvf.most_common(4),
            'depth_byte_p25':percentile(liquid_depths,0.25),'depth_byte_p50':percentile(liquid_depths,0.50),'depth_byte_p75':percentile(liquid_depths,0.75)
        },
        'texture_role_thresholds':{
            role:{'slope_p25':round(percentile(vals,0.25),2),'slope_p50':round(percentile(vals,0.50),2),'slope_p75':round(percentile(vals,0.75),2),'samples':len(vals)}
            for role,vals in sorted(role_slope.items())
        },
        'generator_hints':{
            'max_layers_per_chunk':4,
            'prefer_chunk_layer_count':min(4, max(2, round(avg_layers))),
            'rock_slope_threshold_degrees': max(20, round(percentile(role_slope.get('rock',[]),0.25) or 28, 1)),
            'roads_should_be_continuous': True,
            'avoid_speckled_alphamaps': True,
            'prefer_large_connected_texture_masks': True,
            'use_only_learned_texture_paths_when_possible': True
        },
        'notes':[
            'This is a local statistical rule distillation, not redistributed Blizzard data.',
            'The generator consumes recommended_texture_layers directly if this file is present.',
            'Object placement is intentionally not learned here; M2/WMO trust filtering should be separate.'
        ]
    }
    return rules

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('input', nargs='?', default='learn_input', help='folder containing ADTs, recursively')
    ap.add_argument('-o','--output', default='learned_blizzlike_rules.json')
    ap.add_argument('--limit', type=int, default=0, help='max ADTs to analyze, 0=all')
    args=ap.parse_args()
    root=Path(args.input)
    files=sorted(root.rglob('*.adt'))
    if args.limit and len(files)>args.limit: files=files[:args.limit]
    if not files:
        raise SystemExit(f'No ADT files found under {root}. Put extracted/custom ADTs there or pass another folder.')
    results=[]
    for i,p in enumerate(files,1):
        try:
            results.append(analyze_adt(p))
        except Exception as e:
            print(f'WARN {p}: {e}')
    rules=build_rules(results, str(root))
    Path(args.output).write_text(json.dumps(rules, indent=2), encoding='utf-8')
    print(f'Wrote {args.output}: {rules["source_adts"]} ADTs, {rules["source_chunks"]} chunks')
    print('Recommended texture layers:')
    for item in rules['recommended_texture_layers']:
        print(f"  {item['role']:7s} {item['path']}  count={item.get('source_count')}")
    if rules.get('recommended_texture_layers_by_theme'):
        print('Themes learned: ' + ', '.join(sorted(rules['recommended_texture_layers_by_theme'].keys())))

if __name__=='__main__': main()
