# v0.9 deep-dive fixes

This pass focused on problems that could let the bundled validator pass while Noggit/WoW still rejects or misreads the terrain.

## 1. Reverted v0.8 MCNK header size from 136 to the classic 128-byte WotLK layout

v0.8 treated the MCNK header as 136 bytes and inserted two kinds of extra padding. That was risky/wrong for 3.3.5a root ADTs. The classic documented MCNK header is 128 bytes:

- `0x00` flags
- `0x14` ofsMCVT
- `0x68` float[3] position
- `0x7C` unused

v0.9 now writes exactly 128 bytes before the nested MCNK subchunks. Therefore first nested subchunk is at `MCNK chunk start + 8 + 128 = +136`.

## 2. Restored the real 16-byte ReallyLowQualityTextureingMap field

v0.8 effectively compressed the low-quality texture map area into 8 bytes and then added unrelated padding later. v0.9 writes the expected layout:

- `0x3C` holes/pad word
- `0x40..0x4F` 16-byte low-quality textureing map placeholder
- `0x50` predTex
- `0x54` noEffectDoodad
- `0x58` ofsMCSE

This keeps all later fields at their documented offsets.

## 3. Kept vertical base height in zpos at 0x68

The position order remains `zpos, xpos, ypos`, matching the classic MCNK structure. MCVT values are still written as relative deltas from `zpos`.

## 4. Switched default MCAL output back to raw 4096-byte alpha maps

v0.7/v0.8 defaulted to RLE-compressed MCAL. The validator could decompress those maps, but RLE is still easier to get subtly wrong. v0.9 defaults to raw 4096-byte alpha maps with:

- MCLY flag `0x100` = use alpha map
- no `0x200` compressed flag
- WDT MPHD flag `0x04` enabled for large/newer alpha maps

RLE is still supported with `"alpha_mode": "rle"`, but raw is now the safer default.

## 5. Validator upgraded to enforce the corrected MCNK layout

The validator now fails if:

- MCNK header is shorter than 128 bytes
- ofsMCVT starts before `8 + 128`
- position/base-height checks read from the wrong offset
- raw alpha maps are not exactly 2048 or 4096 bytes
- compressed maps do not expand to exactly 4096 bytes

## 6. Source-code audit note

I could not clone the complete Noggit repository from the execution container because DNS/network access is blocked there. However, the public repository/release pages and format references were checked from the web, and v0.9 aligns the binary writer with the classic getMaNGOS MCNK layout plus wow_adt/wowdev-style reversed chunk magic and MCLY/MH2O semantics.

The next truly decisive test is still: open the generated ADTs in Noggit/Noggit Red and then in a 3.3.5a client.
