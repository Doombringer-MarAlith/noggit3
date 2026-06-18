# v0.17 reverse WDT big-alpha normalization fixes

This pass found a concrete mismatch between the v0.16 claim and implementation.

## Issue

Reverse mode normalized copied WDTs by always enabling shading, and it set the
MPHD big-alpha flag when inferred necessary. However, when big-alpha was *not*
inferred, the code did not clear an existing MPHD 0x04 bit copied from the source
WDT.

That meant old-alpha reverse outputs could still carry the big-alpha WDT flag.
Noggit can often load alphamaps by per-layer size, but MapIndex still stores that
flag as `mBigAlpha`; future Noggit saves would tend to save those tiles as big
alpha even if the improved ADTs did not require it.

## Fix

- `reverse_tool.py` now clears MPHD 0x04 in copied WDTs unless the actual output
  ADTs require big/compressed alphamaps.
- `validate_structure.py` now fails when a WDT carries 0x04 but the output ADTs
  do not require big/compressed alphamaps.

Forward-generated terrain still uses big-alpha texture painting and therefore
keeps MPHD flags `0x06` in auto mode.
