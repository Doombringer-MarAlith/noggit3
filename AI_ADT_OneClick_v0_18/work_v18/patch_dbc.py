#!/usr/bin/env python3
"""
patch_dbc.py - add the generated map to Map.dbc (and optionally AreaTable.dbc)

A 3.3.5a client cannot enter a map that has no Map.dbc row whose Directory
matches the world/maps/<dir> folder, and a server (TrinityCore & co.) needs
the same row in its dbc folder. This tool appends those rows to the client's
extracted DBCs so the generated patch is playable without hand-editing.

Usage:
    python patch_dbc.py --dbc-dir C:\\WoWExtract\\DBFilesClient --out output_loose\\DBFilesClient
        [--map-id 900] [--map-name x] [--display-name "Greenreed Valley"]
        [--area-id 5100] [--area-name "Greenreed Valley"] [--expansion 2]
        [--loading-screen 0] [--instance-type 0]

Defaults come from zone_spec.json / config.json when present. The tool never
overwrites the source DBCs: patched copies are written to --out (put that
DBFilesClient folder into the patch MPQ next to world/ and Textures/).

WDBC layout (all 3.3.5a DBCs): 'WDBC', recordCount, fieldCount, recordSize,
stringBlockSize, then records (fieldCount x uint32/float/int32 each, string
columns are offsets into the string block), then the string block (first byte
is NUL, so offset 0 == empty string).

Map.dbc (build 12340), 66 columns:
    0  ID
    1  Directory (string)
    2  InstanceType (0 = normal/continent)
    3  Flags
    4  PVP
    5-21  MapName_lang (16 locale strings + flags)
    22 AreaTableID
    23-39 MapDescription0_lang
    40-56 MapDescription1_lang
    57 LoadingScreenID
    58 MinimapIconScale (float)
    59 CorpseMapID
    60 CorpseX (float)
    61 CorpseY (float)
    62 TimeOfDayOverride
    63 ExpansionID
    64 RaidOffset
    65 MaxPlayers

AreaTable.dbc (build 12340), 36 columns:
    0  ID
    1  ContinentID (Map.dbc ID)
    2  ParentAreaID
    3  AreaBit (unique exploration bit)
    4  Flags
    5  SoundProviderPref
    6  SoundProviderPrefUnderwater
    7  AmbienceID
    8  ZoneMusic
    9  IntroSound
    10 ExplorationLevel
    11-27 AreaName_lang
    28 FactionGroupMask
    29-32 LiquidTypeID[4]
    33 MinElevation (float)
    34 AmbientMultiplier (float)
    35 LightID
"""
from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LOCALE_ENUS = 0  # index of enUS in the 16-locale string block


class Dbc:
    def __init__(self, data: bytes):
        magic, self.record_count, self.field_count, self.record_size, self.string_size = struct.unpack_from('<4sIIII', data, 0)
        if magic != b'WDBC':
            raise ValueError('not a WDBC file')
        if self.record_size != self.field_count * 4:
            raise ValueError(f'unsupported record size {self.record_size} for {self.field_count} fields')
        start = 20
        self.records: List[List[int]] = []
        for i in range(self.record_count):
            off = start + i * self.record_size
            self.records.append(list(struct.unpack_from(f'<{self.field_count}I', data, off)))
        sb_start = start + self.record_count * self.record_size
        self.strings = bytearray(data[sb_start:sb_start + self.string_size])
        if not self.strings:
            self.strings = bytearray(b'\0')

    def string(self, off: int) -> str:
        end = self.strings.find(b'\0', off)
        return self.strings[off:end].decode('utf-8', 'replace')

    def add_string(self, s: str) -> int:
        if not s:
            return 0
        off = len(self.strings)
        self.strings.extend(s.encode('utf-8') + b'\0')
        return off

    def max_id(self, col: int = 0) -> int:
        return max((r[col] for r in self.records), default=0)

    def has_id(self, value: int, col: int = 0) -> bool:
        return any(r[col] == value for r in self.records)

    def to_bytes(self) -> bytes:
        out = bytearray(struct.pack('<4sIIII', b'WDBC', len(self.records), self.field_count, self.record_size, len(self.strings)))
        for r in self.records:
            out.extend(struct.pack(f'<{self.field_count}I', *[v & 0xFFFFFFFF for v in r]))
        out.extend(self.strings)
        return bytes(out)


def f32bits(x: float) -> int:
    return struct.unpack('<I', struct.pack('<f', float(x)))[0]


def localized(dbc: Dbc, text: str) -> List[int]:
    """16 locale string offsets (enUS filled, others empty) + the locale flags mask."""
    vals = [0] * 17
    vals[LOCALE_ENUS] = dbc.add_string(text)
    vals[16] = 0xFF01FE  # flags mask Blizzard uses on 3.3.5 rows with an enUS string
    return vals


def add_map_row(dbc: Dbc, map_id: int, directory: str, display_name: str, area_id: int,
                loading_screen: int, expansion: int, instance_type: int, corpse_map: int) -> None:
    if dbc.field_count != 66:
        raise SystemExit(f'Map.dbc has {dbc.field_count} columns; expected 66 (3.3.5a build 12340)')
    if dbc.has_id(map_id):
        raise SystemExit(f'Map.dbc already contains ID {map_id}; pick another --map-id')
    for r in dbc.records:
        if dbc.string(r[1]).lower() == directory.lower():
            raise SystemExit(f'Map.dbc already contains Directory {directory!r} (ID {r[0]})')
    row = [0] * 66
    row[0] = map_id
    row[1] = dbc.add_string(directory)
    row[2] = instance_type
    row[3] = 0
    row[4] = 0
    row[5:22] = localized(dbc, display_name)
    row[22] = area_id
    row[23:40] = [0] * 17
    row[40:57] = [0] * 17
    row[57] = loading_screen
    row[58] = f32bits(1.0)
    row[59] = corpse_map if corpse_map >= 0 else 0xFFFFFFFF  # -1 = no corpse map (graveyard map)
    row[60] = f32bits(0.0)
    row[61] = f32bits(0.0)
    row[62] = 0xFFFFFFFF  # -1 = no time-of-day override
    row[63] = expansion
    row[64] = 0
    row[65] = 0
    dbc.records.append(row)


def add_area_row(dbc: Dbc, area_id: int, map_id: int, name: str, explore_level: int = 0) -> None:
    if dbc.field_count != 36:
        raise SystemExit(f'AreaTable.dbc has {dbc.field_count} columns; expected 36 (3.3.5a build 12340)')
    if dbc.has_id(area_id):
        raise SystemExit(f'AreaTable.dbc already contains ID {area_id}; pick another --area-id')
    used_bits = {r[3] for r in dbc.records}
    area_bit = 0
    while area_bit in used_bits:
        area_bit += 1
    row = [0] * 36
    row[0] = area_id
    row[1] = map_id
    row[2] = 0
    row[3] = area_bit
    row[4] = 0
    row[10] = explore_level
    row[11:28] = localized(dbc, name)
    row[28] = 0
    row[33] = f32bits(-500.0)   # MinElevation, as on Blizzard zones
    row[34] = f32bits(0.0)      # Ambient_multiplier, 0 on Blizzard zones
    row[35] = 0
    dbc.records.append(row)


def read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return {}


def main() -> None:
    ap = argparse.ArgumentParser(description='Append the generated map to Map.dbc / AreaTable.dbc')
    ap.add_argument('--dbc-dir', required=True, help='folder with the extracted client Map.dbc / AreaTable.dbc')
    ap.add_argument('--out', default=None, help='output folder (default: <output_dir>/DBFilesClient)')
    ap.add_argument('--map-id', type=int, default=None)
    ap.add_argument('--map-name', default=None, help='Map.dbc Directory (world/maps/<name>)')
    ap.add_argument('--display-name', default=None)
    ap.add_argument('--area-id', type=int, default=None, help='AreaTable id to create (0 = skip AreaTable)')
    ap.add_argument('--area-name', default=None)
    ap.add_argument('--expansion', type=int, default=0, help='0 classic (like Azeroth), 1 TBC, 2 WotLK')
    ap.add_argument('--loading-screen', type=int, default=4, help='LoadingScreens.dbc id; 4 = Eastern Kingdoms, 3 = Kalimdor, 216 = Northrend')
    ap.add_argument('--instance-type', type=int, default=0)
    ap.add_argument('--corpse-map', type=int, default=-1)
    args = ap.parse_args()

    cfg = read_json(Path('config.json'))
    spec = read_json(Path('zone_spec.json'))
    map_name = args.map_name or cfg.get('map_name') or spec.get('map_name') or 'aigen'
    display = args.display_name or spec.get('zone_name') or map_name
    area_name = args.area_name or display
    out_dir = Path(args.out) if args.out else Path(cfg.get('output_dir', 'output_loose')) / 'DBFilesClient'
    out_dir.mkdir(parents=True, exist_ok=True)

    src = Path(args.dbc_dir)
    map_path = src / 'Map.dbc'
    if not map_path.exists():
        raise SystemExit(f'Map.dbc not found in {src}')
    mapdbc = Dbc(map_path.read_bytes())
    map_id = args.map_id if args.map_id is not None else int(cfg.get('dbc', {}).get('map_id', 0)) or (max(900, mapdbc.max_id() + 1))

    area_path = src / 'AreaTable.dbc'
    area_id = args.area_id
    areadbc: Optional[Dbc] = None
    if area_path.exists() and area_id != 0:
        areadbc = Dbc(area_path.read_bytes())
        if area_id is None:
            area_id = int(cfg.get('dbc', {}).get('area_id', 0)) or (areadbc.max_id() + 1)
    else:
        area_id = 0

    add_map_row(mapdbc, map_id, map_name, display, area_id or 0, args.loading_screen, args.expansion, args.instance_type, args.corpse_map)
    (out_dir / 'Map.dbc').write_bytes(mapdbc.to_bytes())
    print(f'Map.dbc: added ID {map_id} Directory={map_name!r} name={display!r} -> {out_dir / "Map.dbc"}')

    if areadbc is not None and area_id:
        add_area_row(areadbc, area_id, map_id, area_name)
        (out_dir / 'AreaTable.dbc').write_bytes(areadbc.to_bytes())
        print(f'AreaTable.dbc: added ID {area_id} ({area_name!r}) on map {map_id} -> {out_dir / "AreaTable.dbc"}')
        print(f'Set "area_id": {area_id} in config.json (and regenerate) so every MCNK reports this zone.')
    print('Copy the patched DBFilesClient folder into the patch MPQ (client) and into the server dbc folder.')


if __name__ == '__main__':
    main()
