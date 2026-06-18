# v0.15 core audit fixes

This pass focused on remaining correctness/validation issues after v0.14.

## WDL / MAHO fix

Earlier builds emitted `MAHO` after every `MARE` as 16 uint16 values of `0xFFFF` while the generated ADTs contain no terrain holes. Noggit currently reads `MARE` by `MAOF` offsets and leaves `MAHO` handling as a TODO in `map_horizon.cpp`, but independent WDL parsers describe `MAHO` as optional hole bitmasks. That makes an all-ones generated MAHO unsafe: consumers that do parse it may interpret it as all holes.

v0.15 omits `MAHO` entirely for generated no-holes WDL files. The validator now rejects generated `MAHO` payloads that are not all zero.

## Reverse-mode WDL fix

The first v0.15 patch fixed forward WDL generation, but reverse mode had its own WDL writer that still emitted all-ones MAHO chunks. The validator caught this during reverse smoke testing. Reverse WDL generation now also omits MAHO for no-holes output.
