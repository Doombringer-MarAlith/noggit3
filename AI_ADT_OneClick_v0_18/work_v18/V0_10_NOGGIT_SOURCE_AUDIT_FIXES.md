# v0.10 Noggit-source audit fixes

This pass read the relevant public Noggit source files and patched the ADT writer to match Noggit's actual loader/saver behavior.

## Critical fixes

1. **MCNK vertical base height moved to `ypos`**

   Noggit's `MapChunkHeader` stores `zpos`, `xpos`, `ypos`; `MapChunk` uses `header.ypos` as the vertical terrain base and adds MCVT deltas to it. v0.9 incorrectly put base height into `zpos`. v0.10 writes:

   - `zpos = ZEROPOINT - chunk_zbase`
   - `xpos = ZEROPOINT - chunk_xbase`
   - `ypos = generated base height`

2. **MCNR is 435 bytes, plus 13 external unknown bytes**

   v0.9 placed the 13 bytes inside MCNR, making the MCNR chunk size 448. Noggit writes MCNR size 435, then appends 13 unknown bytes before MCLY. v0.10 follows Noggit.

3. **`holes` is uint32**

   v0.9 split the 32-bit holes field into `uint16 holes + uint16 unknown`. Noggit's `MapChunkHeader` defines `uint32_t holes`. v0.10 writes a proper 32-bit holes value.

4. **Empty chunks now match Noggit save behavior**

   v0.10 emits empty `MCRF`, `MCAL`, and `MCSE` chunks and sets MCNK offsets to them, matching Noggit's saver even when there are no doodad refs, alpha payload, or sound emitters.

5. **`sizeAlpha` now includes the MCAL header**

   Noggit writes `header.sizeAlpha = 8 + MCAL_payload_size`. v0.10 now does the same.

6. **MCLY effectID defaults to `0xFFFF`**

   Noggit's `ENTRY_MCLY` default effectID is `0xFFFF`. v0.10 writes that instead of `0`.

7. **Normal byte order follows Noggit save/load**

   Noggit saves MCNR bytes as normal.x, normal.z, normal.y and reconstructs normal.x, normal.y, normal.z on load. v0.10 writes that byte order.

## Validation updates

The validator now checks the Noggit-aligned invariants above, including `ypos` base-height range, MCNR payload size 435, external 13-byte MCNR padding, MCRF/MCAL/MCSE presence, full uint32 holes, and `sizeAlpha == 8 + MCAL payload`.
