# v0.7 Deep-Dive Fixes — superseded by v0.8

**Note:** the MCNK position conclusion in this historical v0.7 note was later corrected in `V0_8_DEEP_DIVE_FIXES.md`. v0.8 writes raw MCNK position as `[Z/base-height, X, Y]` and pads the MCNK header to the 136-byte layout.

# v0.7 Deep-Dive Fixes

This pass was aimed at issues that could plausibly make v0.6 look structurally valid but fail or look wildly wrong in a real client/Noggit.

## Fixed: MCNK position vertical component

v0.6 wrote MCNK `position` as `[horizontal_x, horizontal_y, base_height]`.

Common ADT readers treat the second float, `position[1]`, as the vertical/base-height component that MCVT values are relative to. v0.7 now writes:

```text
position[0] = horizontal X
position[1] = base terrain height
position[2] = horizontal Y/Z
```

The learner and reverse parser were also updated to read base height from `position[1]` instead of `position[2]`.

## Changed: alpha maps default back to RLE

v0.6 used raw 4096-byte MCAL alpha maps by default. That was easier to inspect, but it depends more heavily on WDT alpha-mode flags. v0.7 defaults to RLE-compressed 4096-byte alpha maps and sets the MCLY compressed-alpha flag.

## Added: custom WDT MPHD big-alpha flag

v0.7 sets MPHD flag `0x04` automatically for generated custom WDTs when texture painting is enabled. This is safer for 4096-style alpha data and future raw-alpha experiments.

## Improved: validation

`validate_structure.py` now checks more than chunk presence:

- WDT MPHD flags;
- MCNK count per ADT;
- MCVT and MCNR sizes;
- MCLY layer count and alpha flags;
- MCAL offsets;
- RLE alpha maps decompress to exactly 4096 bytes;
- MH2O liquid instance offsets;
- vertical base-height range for each ADT.

## Improved: normals

v0.7 no longer writes every MCNR normal as perfectly upward. It writes a cheap terrain-derived normal per MCNK, which should make generated slopes shade more plausibly without slowing 4x4 generation to a crawl.

## Fixed: learner double-run

`learn_blizzlike_rules.py` accidentally had two `if __name__ == '__main__'` calls. v0.7 removes the duplicate.

## Fixed: reverse sample input helper

The old `reverse_make_sample_input.bat` assumed the map was named `aigen`. v0.7 reads the current map name from `config.json` / `zone_spec.json`, so it works with prompts like “For map X.”

## Still not live-client-proven

The package is still structurally validated only. The remaining highest-risk areas are real 3.3.5a client behavior, texture path existence, Map.dbc setup, MPQ packing details, and server-side extractor workflow.
