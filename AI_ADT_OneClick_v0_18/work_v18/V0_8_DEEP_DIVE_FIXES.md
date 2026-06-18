# v0.8 Deep-Dive Fixes

This pass was aimed at two more mistakes that could make v0.7 pass its own validator while still failing or rendering incorrectly in a 3.3.5a client/Noggit.

## 1. MCNK header padding / subchunk offsets

v0.7 used a 128-byte MCNK payload header and placed the first nested subchunk at `MCNK_start + 8 + 128`. Current ADT references describe the WotLK-era MCNK offset fields as relative to the MCNK chunk start including the 8-byte chunk header, and the header structure includes the trailing padding used by the 136-byte layout.

v0.8 now uses:

```text
MCNK_HEADER_SIZE = 136
ofsMCVT = 8 + MCNK_HEADER_SIZE = 144
```

The validator now fails if MCVT appears before the padded header boundary or if MCNK subchunk offsets are not increasing.

## 2. Raw MCNK position order

v0.7 stored base height in raw `position[1]`. That was based on renderer-space wording, but the raw file layout used by current references is better treated as:

```text
position[0] = Z / vertical base height
position[1] = X / north-south
position[2] = Y / west-east
```

v0.8 writes base height to raw `position[0]` and updates the learner, reverse parser, reverse writer, and validator to use that same slot.

## 3. Reverse-mode write fix

Reverse mode previously wrote regenerated base heights into the wrong raw position field. v0.8 writes regenerated chunk base heights back into raw `position[0]`, then writes MCVT values relative to that base.

## 4. Regenerated outputs

The default prompt output and reverse-mode sample were regenerated and validated after these changes. Validation now checks WDT, WDL, 16 ADTs, 4096 MCNK chunks, MCVT, MCNR, MCLY, MCAL RLE decompression, MH2O, BLP minimap headers, padded MCNK offsets, and raw base-height ranges.
