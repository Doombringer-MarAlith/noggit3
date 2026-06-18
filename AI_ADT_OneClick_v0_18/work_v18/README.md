# AI ADT OneClick v0.18


## v0.18 core audit notes

- MH2O generation now follows Noggit's liquid save shape more closely: 256 headers, optional render attributes, MH2O_Information, height maps, and depth bytes for flat water layers. Empty-water ADTs now omit MH2O instead of writing an empty MH2O chunk and pointing MHDR at it.
- MCNK low-quality texture maps are no longer all zero. They are derived from dominant generated layer per 8x8 mini-cell and packed into the 16-byte two-bit map in the same bit order Noggit uses.
- The validator now rejects zero-sized MH2O chunks, checks MH2O attributes/info/height/depth offsets, and enforces MCIN size as MCNK payload size plus the 8-byte MCNK chunk header.


## v0.18 Noggit-source audit notes

This version was patched after reading the relevant Noggit source files directly:

- `MapHeaders.h` defines the 3.3.5a `MapChunkHeader` as a 128-byte struct with `zpos`, `xpos`, `ypos` at offsets `0x68`, `0x6C`, `0x70`.
- `MapChunk.cpp` loads `header.ypos` as the vertical terrain base and adds MCVT deltas to it. Therefore generated base height now goes into `ypos`, not `zpos`.
- Noggit writes MCNR as a 435-byte chunk and then writes 13 unknown bytes after the MCNR chunk. v0.10 follows that exact layout.
- `holes` is a full `uint32_t`, not a split `uint16 holes + uint16 unknown`. v0.10 writes it as a full 32-bit value.
- Noggit saves `MCRF`, `MCAL`, and `MCSE` chunks even when their payloads are empty. v0.10 now emits those empty chunks and points MCNK offsets at them.
- `header.sizeAlpha` now includes the 8-byte MCAL chunk header, matching Noggit's save code.
- MCLY `effectID` defaults to `0xFFFF`, matching Noggit's `ENTRY_MCLY`.

This is still structurally validated, not live-client proven, but it is now aligned against Noggit's own loader/saver behavior rather than only third-party format notes.

## v0.18 source-audit fixes

v0.18 fixes remaining core issues found after v0.18:

- Forward ADT generation now writes per-vertex MCNR terrain normals instead of repeating one normal per MCNK. The byte layout still follows Noggit: `normal.x, normal.z, normal.y`, with the 13 unknown bytes after MCNR.
- Reverse mode now recalculates MCNR normals after height edits, so polished/regenerated ADTs do not keep stale lighting normals.
- Reverse fallback WDT generation now uses MPHD flags `0x06` instead of `0`, matching the safe generated-map default of Noggit shading + big-alpha.
- The local learner now uses the correct 128-byte MCNK header guard.

The included validator smoke-test was run on a 1x1 generated map and reverse-improved output. Full 4x4 generation remains the default prompt workflow, but it may take longer than this execution environment allows.


## Critical v0.6/v0.7/v0.10/v0.18 fixes

v0.5 likely would not load correctly because it wrote human-readable chunk names directly. WoW ADT/WDT/WDL chunk magic is reversed on disk:

```text
MVER -> REVM
MHDR -> RDHM
MCNK -> KNCM
MCVT -> TVCM
MCLY -> YLCM
MCAL -> LACM
MH2O -> O2HM
```

v0.6 fixed reversed chunk magic. v0.7/v0.9/v0.10/v0.18 progressively fixed alphamap and MCNK layout assumptions. v0.10 started Noggit-source alignment; v0.18 adds second-pass MH2O/LQ-map validation on top of: 128-byte MCNK header, `ypos` as vertical base height, 435-byte MCNR plus 13 external unknown bytes, full uint32 holes field, empty MCRF/MCAL/MCSE chunks, and Noggit-style `sizeAlpha`.

## Fast path: prompt to map

Edit:

```text
zone_prompt.txt
```

Run:

```text
RUN_PROMPT_TO_READY_MAP.bat
```

This runs:

```text
ai_zone_spec.py
  -> zone_spec.json
  -> config.json
  -> reverse_config.json

generate_adt.py
  -> WDT + 16 ADTs + MH2O + WDL + minimap BLPs

validate_structure.py
  -> structural checks
```

## Better path: learn Azeroth first, then generate

Extract your own 3.3.5a client ADTs locally, then run:

```text
RUN_LEARN_AZEROTH_THEN_PROMPT.bat C:\WoWExtract\world\maps\azeroth
```

This writes:

```text
learned_blizzlike_rules.json
```

v0.18 learns:

```text
texture paths actually used in your local ADTs
theme-specific texture packs, including jungle/stranglethorn if present
role labels: base, road, rock, shore, forest, snow, sand, plague
average layer counts
slope statistics by role
```

Then the generator consumes the learned rules. For a jungle prompt, it first tries `recommended_texture_layers_by_theme.jungle`; if that exists, those local texture paths override the guessed fallback paths.

## Real AI mode

Set one of these before running:

```bat
set AI_API_KEY=your_key_here
set AI_MODEL=gpt-4.1-mini
```

or:

```bat
set OPENAI_API_KEY=your_key_here
```

Optional OpenAI-compatible endpoint:

```bat
set AI_BASE_URL=https://your-compatible-endpoint/v1
```

Without an API key, `ai_zone_spec.py` uses a deterministic offline planner. That fallback is not neural AI, but it still produces a detailed `zone_spec.json` so the rest of the pipeline can run.

## What the generator now writes

### New map files

```text
world/maps/<map>/<map>.wdt
world/maps/<map>/<map>.wdl
world/maps/<map>/<map>_<x>_<y>.adt
```

### ADT contents

```text
MVER
MHDR
MCIN
MTEX
MMDX / MMID empty
MWMO / MWID empty
MDDF / MODF empty
MH2O water
256 MCNK chunks per ADT
  MCVT heights
  MCNR terrain-derived normals
  MCLY texture layers
  MCAL RLE-compressed 64x64 alphamaps
```

### Minimap

```text
Textures/Minimap/*.blp
Textures/Minimap/md5translate.trs
Textures/Minimap/md5translate_AIGEN_FRAGMENT.trs
minimap_previews/<map>/*.tga
```

For a real client patch, merge the generated `md5translate_AIGEN_FRAGMENT.trs` into your extracted original `md5translate.trs`. Do not permanently replace the full original with this tiny generated file unless you are only doing a disposable test.

## Packing

Put `MPQEditor.exe` next to the scripts, then run:

```text
pack_mpq.bat
```

It creates:

```text
patch-AI.MPQ
```

Put that in your 3.3.5a `Data` folder.

## Quality/performance settings

In `config.json`:

```json
"texture_painting": {
  "alpha_mode": "raw_4096",
  "alpha_sample_step": 32,
  "layer_choice_stride": 4
}
```

These defaults make a 4x4 block generate quickly. For better-looking alphamaps, try:

```json
"alpha_sample_step": 16,
"layer_choice_stride": 2
```

For slow but smoother painting:

```json
"alpha_sample_step": 8,
"layer_choice_stride": 1
```

## Important limitations

This is still **not** a guaranteed retail-quality map maker. It is a direct binary generator that has been structurally validated here, but I cannot run a 3.3.5a client in this environment.

Still not included:

```text
M2/WMO placement
object collision or doodad references
real vertex-color lighting
server DBC/SQL
server map/vmap/mmap extraction
quests/creatures/gameobjects
```

M2/WMO is deliberately left out until there is an asset trust database. Randomly choosing from thousands of models is exactly how you get crashy Noggit/private-server chaos.

## Most likely remaining failure points

If it does not load, these are the first things to check:

```text
1. Map.dbc Directory field must match generated folder, e.g. x.
2. MPQ must include world/maps/x/x.wdt and all x_32_32..x_35_35 ADTs.
3. Texture BLP paths must exist in your client. Running the Azeroth learner first reduces this risk.
4. MH2O liquid type may need adjustment if your client/server setup expects a specific LiquidType.dbc row.
5. Minimap TRS should be merged into the original md5translate.trs for serious testing.
```

## Files to care about

```text
zone_prompt.txt                 Your prompt
ai_config.json                  AI planner settings
zone_spec.json                  The AI-written zone specification
learned_blizzlike_rules.json    Learned local style/texture rules, if generated
config.json                     Generator config derived from the spec
output_loose/                   Loose patch root
GENERATION_REPORT.txt           Summary after generation
```

## v0.18 note

v0.18 fixes another Noggit-source-aligned coordinate bug: the generator now writes raw MCNK `zpos/xpos` directly from the zero-point-relative chunk base, and the reverse improver writes vertical base height back to `ypos` instead of corrupting `zpos`. The validator now checks these raw coordinates from ADT filenames.


## v0.18 source-audit fixes

See `V0_13_FOURTH_NOGGIT_SOURCE_AUDIT_FIXES.md`. This pass removes MTEX NUL padding and writes Noggit-compatible WDT auto flags (`0x06` for textured generated maps).


## v0.18 config path note

You can now run `python generate_adt.py my_config.json`. Explicit config fields override zone_spec defaults, so you can keep a full prompt/spec and still test a smaller tile subset. Reverse mode also accepts `python reverse_tool.py analyze my_reverse_config.json` and `python reverse_tool.py improve my_reverse_config.json`.
