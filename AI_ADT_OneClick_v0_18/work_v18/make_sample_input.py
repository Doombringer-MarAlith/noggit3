#!/usr/bin/env python3
"""Copy the current generated map into input_existing for reverse-mode testing.
Reads map_name from config.json/zone_spec.json instead of assuming aigen.
"""
import json, shutil
from pathlib import Path

def read_json(p):
    try: return json.loads(Path(p).read_text(encoding='utf-8'))
    except Exception: return {}

cfg=read_json('config.json'); spec=read_json('zone_spec.json')
map_name=str(cfg.get('map_name') or spec.get('map_name') or 'aigen')
src=Path('output_loose')/'world'/'maps'/map_name
dst=Path('input_existing')/'world'/'maps'/map_name
if not src.exists():
    raise SystemExit(f'No generated map found at {src}. Run RUN_PROMPT_TO_READY_MAP.bat first.')
if Path('input_existing').exists():
    shutil.rmtree('input_existing')
dst.mkdir(parents=True, exist_ok=True)
for ext in ('*.adt','*.wdt','*.wdl'):
    for p in src.glob(ext):
        shutil.copy2(p, dst/p.name)
print(f'Sample input copied to {dst}')
print('Now run reverse_analyze.bat or reverse_improve.bat.')
