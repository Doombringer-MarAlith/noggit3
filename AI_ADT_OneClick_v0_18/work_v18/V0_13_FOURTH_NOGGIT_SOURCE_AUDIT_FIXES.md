# v0.13 fourth Noggit source audit fixes

This pass re-checked the Noggit reader/writer assumptions used in v0.12.

## Fixes

1. **MTEX is no longer padded.** Noggit reads texture filenames from the MTEX payload until the exact chunk end. Extra NUL bytes can become empty texture names. v0.13 writes only the concatenated NUL-terminated texture paths.

2. **WDT MPHD auto flags now include FLAG_SHADING (0x02).** Noggit sets this flag when loading a WDT if it is missing. Auto-mode now writes `0x06` for textured generated maps: `0x02` shading + `0x04` big-alpha.

3. **Validator now rejects MTEX empty names and WDTs missing the shading flag.**

No speculative binary-layout changes were made in this pass.
