# v0.12 Third Noggit Source Audit Fixes

This pass re-read the same Noggit source paths used for v0.10/v0.11 and compared
our writer/reverse improver against them again.

## Critical fixes

### 1. Forward generator MCNK xpos/zpos double-conversion fixed

`chunk_base_world()` already returns the zero-point-relative MCNK raw coordinate
value used by Noggit's header fields:

- `zpos = ZEROPOINT - chunk_zbase`
- `xpos = ZEROPOINT - chunk_xbase`
- `ypos = vertical base height`

v0.11 accidentally did `WORLD_HALF - base_x/base_y` again inside
`pack_mcnk_position()`. For tile 32 this could turn near-zero MCNK `xpos/zpos`
values into roughly 17066.666, which is clearly wrong for a chunk around the
world origin. v0.12 writes the zero-point-relative values directly.

### 2. Reverse improver no longer corrupts zpos

The reverse tool correctly read `ypos` from offset `0x70`, but when writing
improved height data back it wrote the new vertical base to offset `0x68`
(`zpos`). v0.12 writes it back to `0x70`, preserving raw `zpos/xpos` anchors.

### 3. MH2O payload writer now follows Noggit liquid_layer format rules

v0.11 always wrote `float[9*9] + depth[9*9]` for liquid payloads even if a user
changed `liquid_vertex_format`. v0.12 now writes:

- format 0: float heights + depth bytes
- format 1: float heights + `mh2o_uv` values
- format 2: depth bytes only

The default remains format 0.

### 4. Validator now checks MCNK raw xpos/zpos

The validator parses ADT filenames, derives the expected raw `xpos/zpos` for
all 256 MCNKs, and fails if generated or reverse-improved files drift from
Noggit's coordinate convention.

## Still not claimed

This is still not a substitute for opening the result in Noggit/3.3.5a. It is a
source-aligned structural pass that catches another class of binary mistakes.
