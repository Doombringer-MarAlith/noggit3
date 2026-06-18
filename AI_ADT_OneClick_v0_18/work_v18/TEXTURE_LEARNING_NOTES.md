# Texture learning notes

The goal is not to train a giant model on Blizzard data. The first useful step is rule distillation:

- ADTs already contain texture choices and per-chunk layer counts.
- MTEX reveals which texture paths are used.
- MCLY reveals which textures are layered together.
- MCVT terrain heights let us estimate slope.
- From this, we infer roles like road, rock, shore, forest, snow, sand, plague.

The output `learned_blizzlike_rules.json` is intentionally simple and editable. You can hand-correct roles if the classifier guesses wrong.

Future improvements:

- parse MCAL alpha maps from source ADTs and learn actual mask shapes;
- cluster texture palettes by biome/subzone;
- learn road-width distributions from dirt masks;
- learn shoreline transition widths from water + mud/sand masks;
- generate per-biome alpha painting profiles;
- add screenshot critic loop.


## v0.11 alphamap default

v0.11 defaults to raw 4096-byte MCAL alphamaps for stability. RLE compression is still available by setting `texture_painting.alpha_mode` to `rle`, but raw maps are easier to validate and closer to a conservative first client test.


## v0.11 Noggit alignment

Noggit keeps alpha maps internally as 64x64 big alpha maps for editing. When saving big-alpha ADTs, it writes one raw 4096-byte alpha map per non-base layer, clears the compressed-alpha flag, and stores `sizeAlpha` as the MCAL chunk size plus the 8-byte MCAL chunk header. v0.11 follows that safer raw-alpha path by default.
