# v0.11 second Noggit-source audit fixes

This pass re-read the earlier Noggit source files and added more files around liquids and alphamaps:

- `src/noggit/liquid_tile.cpp`
- `src/noggit/liquid_chunk.cpp`
- `src/noggit/liquid_layer.cpp`
- `src/noggit/alphamap.cpp`

## Fixes

1. **Empty MH2O chunk removed**

   v0.10 could write an 8-byte zero-payload `MH2O` chunk and still point `MHDR.mh2o` at it. Noggit only saves MH2O when liquid data exists. v0.11 returns no chunk when no wet MCNK exists, so `MHDR.mh2o` becomes `0`.

2. **MH2O now includes Noggit-style payloads**

   v0.10 wrote `MH2O_Information` plus an info bitmap, but omitted render attributes and explicit height/depth payloads. Noggit writes per-chunk `MH2O_Attributes` for normal water, then `MH2O_Information`, then height/depth data. v0.11 writes that simpler but complete one-layer form.

3. **Full-water chunks use no info mask**

   Noggit sets `ofsInfoMask = 0` when a liquid layer fully covers the 8x8 liquid grid. v0.11 does the same and writes a mask only for partial liquid chunks.

4. **Low-quality texture map generated**

   v0.10 left MCNK `low_quality_texture_map[16]` zeroed. Noggit packs 64 two-bit dominant texture indices into this field. v0.11 derives it from generated texture masks, so far/LOD texturing is not always layer zero.

5. **Validator tightened**

   v0.11 validates MH2O header/info/attribute/height/depth offsets, rejects empty MH2O chunks, and enforces MCIN size equals MCNK chunk size including the 8-byte MCNK header.

## Still not proven

This is still structurally validated only. Noggit/client testing remains required before calling it game-proven.
