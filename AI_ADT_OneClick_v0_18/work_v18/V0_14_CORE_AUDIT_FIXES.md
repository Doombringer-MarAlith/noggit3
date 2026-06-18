# v0.14 core audit fixes

This pass found three remaining core issues after v0.13:

1. Forward MCNR normals were structurally valid but too weak: one repeated normal
   per MCNK. v0.14 writes per-vertex normals in Noggit's disk byte order
   `normal.x, normal.z, normal.y`, leaving the 13 unknown bytes after MCNR.

2. Reverse mode updated MCVT heights but left old MCNR normals untouched. Any
   polished/regenerated reverse tile could have stale lighting. v0.14 recalculates
   MCNR normals for every rewritten MCNK after height changes.

3. Reverse fallback WDT generation still used MPHD flags `0`. If the input folder
   lacked a WDT, the generated fallback WDT was missing Noggit's shading flag and
   big-alpha flag. v0.14 writes `0x06` for fallback reverse WDTs.

Minor: the local learner now uses the correct 128-byte MCNK header guard instead
of an overly conservative 136-byte guard.
