# v0.16 reverse/WDT validation audit fixes

Source-backed issue found after re-reading Noggit WDT behavior:

- Noggit `MapIndex` enables `FLAG_SHADING` (0x02) when loading a WDT if it is missing.
- Noggit also marks `MAIN` tile flags present when matching ADT files exist on disk.
- The WDT big-alpha flag (0x04) controls alphamap format and must not be blindly forced for old 2048-byte alpha ADTs.

v0.15 reverse fallback always wrote 0x06 and copied existing WDTs unchanged. That was wrong in both directions:

1. Old-alpha reverse inputs could be broken by forcing 0x04 in a fallback WDT.
2. Existing copied WDTs could keep missing shading or missing MAIN flags.

v0.16 fixes this by:

- Inferring big-alpha need from ADT MCLY/MCAL layer sizes and compressed flags.
- Normalizing copied WDTs with shading flag 0x02 and MAIN entries for output ADTs.
- Setting big-alpha flag 0x04 only when inferred necessary.
- Updating the validator to require WDT MAIN coverage for present ADTs and require 0x04 only when ADTs actually contain big/compressed alpha data.
