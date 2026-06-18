# v0.18 config / validation audit fixes

This pass found a non-binary but still critical workflow correctness bug:

## 1. `generate_adt.py some_config.json` was ignored

Earlier builds accepted no command-line config path. Any command such as:

```bat
python generate_adt.py config_smoke.json
```

still loaded `config.json`. This means several smoke-test and custom-config runs could silently generate the default map instead of the requested test configuration.

v0.18 now supports:

```bat
python generate_adt.py config.json
python generate_adt.py config_smoke.json
python generate_adt.py my_custom_config.json
```

and prints the config path it actually used.

## 2. `zone_spec.json` overrode explicit config fields

Earlier builds always let `zone_spec.json` replace `map_name`, `tiles_x`, `tiles_y`, and `base_texture`. That was bad for testing and bad for users who want to keep a prompt/spec but temporarily generate a smaller ADT subset.

v0.18 now treats zone_spec values as defaults. Explicit top-level values in the selected config file win.

## 3. Reverse CLI config path was ignored too

`python reverse_tool.py analyze reverse_config_alt.json` and `python reverse_tool.py improve reverse_config_alt.json` now work. Older builds always used `reverse_config.json`.

## 4. Validator now checks adjacent ADT seams

The validator now extracts each ADT's 129x129 outer height grid and compares neighboring ADT borders. This catches cracks that per-file MCIN/MCNK/MCVT checks cannot catch.

