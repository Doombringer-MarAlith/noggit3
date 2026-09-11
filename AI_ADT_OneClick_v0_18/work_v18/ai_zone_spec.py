#!/usr/bin/env python3
"""
AI ADT One-Click v0.11 - AI Zone Spec Writer

Turns a high-level zone prompt into a detailed zone_spec.json that generate_adt.py can consume.

Modes:
  1) Real AI planner: set AI_API_KEY and optionally AI_BASE_URL / AI_MODEL.
     Uses an OpenAI-compatible /v1/chat/completions endpoint through stdlib urllib.
  2) Offline fallback planner: no API key needed; creates a detailed deterministic spec.

This script writes specs only. It does not ship or require Blizzard assets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Tuple

DEFAULT_AI_CONFIG = {
    "mode": "auto",                  # auto | api | offline
    "provider": "auto",              # auto | anthropic | openai_compatible
    "anthropic_model": "claude-opus-5",
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4.1-mini",
    "temperature": 0.7,
    "max_tokens": 12000,
    "map_name": "aigen",
    "origin_tile": [32, 32],
    "size_adts": [4, 4],
    "client_version": "3.3.5a",
    "fallback_theme": "northern pine coast",
    "write_markdown_brief": True,
    "generator_defaults": {
        "mode": "custom",
        "area_id": 0,
        "output_dir": "output_loose"
    }
}

BIOME_LIBRARY = {

    # base_texture paths must exist in the 3.3.5a client (see tileset_listfile_335.txt)
    "jungle": {
        "name": "cozy jungle valley",
        "base_texture": "Tileset\\Stranglethorn\\StrangleThornGrass.blp",
        "palette": ["lush jungle grass", "muddy footpath", "wet gray rock", "dark jungle floor", "riverbank mud"],
        "assets": ["jungle trees", "vines", "wet rocks", "small huts", "ruined troll stones"],
        "colors": ["warm green", "deep shadow", "wet brown", "mossy gray"]
    },
    "forest": {
        "name": "temperate haunted forest",
        "base_texture": "Tileset\\Elwynn\\ElwynnGrassBase.blp",
        "palette": ["dark grass", "dirt road", "gray rock", "forest floor", "muddy shore"],
        "assets": ["pine trees", "dead trees", "human fences", "crates", "ruined walls"],
        "colors": ["deep green", "brown", "gray", "cold blue shadow"]
    },
    "northrend": {
        "name": "northern pine fjord",
        "base_texture": "Tileset\\Expansion02\\BoreanTundra\\BT_GroundA.blp",
        "palette": ["cold grass", "dirt road", "gray cliff rock", "pine needles", "snow patches"],
        "assets": ["pines", "gray rocks", "vrykul ruins", "log structures", "boats"],
        "colors": ["cold green", "blue gray", "dark brown", "snow white"]
    },
    "snow": {
        "name": "cold mountain snowfield",
        "base_texture": "Tileset\\IronForge\\IronForgeSnow01solid.blp",
        "palette": ["snow", "ice dirt", "dark rock", "pine needles", "frozen shore"],
        "assets": ["pines", "snow rocks", "dwarf camp", "dead trees"],
        "colors": ["white", "blue", "black rock", "cold gray"]
    },
    "desert": {
        "name": "arid canyon basin",
        "base_texture": "Tileset\\Tanaris\\TanarisSandBase01.blp",
        "palette": ["sand", "dry dirt", "orange rock", "dry grass", "oasis mud"],
        "assets": ["cacti", "bones", "goblin tents", "ruins", "oasis palms"],
        "colors": ["tan", "orange", "brown", "sunlit cream"]
    },
    "swamp": {
        "name": "wet lowland swamp",
        "base_texture": "Tileset\\Swamp of Sorrows\\SwampSorrowsdirt02.blp",
        "palette": ["swamp grass", "mud", "dark rock", "marsh moss", "shallow water"],
        "assets": ["mangroves", "dead trees", "bog huts", "roots", "reeds"],
        "colors": ["olive", "mud brown", "dark green", "fog gray"]
    },
    "plague": {
        "name": "plagued Lordaeron woodland",
        "base_texture": "Tileset\\PlagueLandsEast\\EastPlaguedBaseGround.blp",
        "palette": ["sickly grass", "plague dirt", "gray rock", "dead leaves", "toxic shore"],
        "assets": ["dead trees", "ruined human buildings", "graveyard props", "plague cauldrons"],
        "colors": ["yellow green", "gray", "brown", "purple rot"]
    }
}

SYSTEM_PROMPT = r"""
You are a senior WoW 3.3.5a custom-zone planner for Noggit/ADT tooling.
Return ONLY strict JSON. No markdown. No comments.

Create a detailed zone specification for an AI ADT generator. The spec must be practical, not fantasy fluff.
It must use coordinates in local ADT units where [0,0] is the north-west/top-left of the generated rectangle and [size_x,size_y] is south-east/bottom-right.
It must include enough details for terrain, roads, water, texture masks, subzones, object-intent, polish rules, validation rules, and reverse-region modes.
Do not mention copyrighted asset file dumps or distribute assets. Object placement must be intent-level only.

Required top-level JSON keys:
version, map_name, zone_name, client_version, origin_tile, size_adts, tiles_x, tiles_y,
creative_intent, terrain, water, roads, textures, subzones, landmarks, settlements,
encounter_spaces, object_intent, minimap, wdl, reverse_region_modes, polish_rules,
generator_config, validation_targets, production_notes.

For terrain.features, use items with:
  id, type, path OR center, height OR depth, width OR radius, purpose.
Allowed terrain feature types: ridge, hill, basin, valley, plateau, coast, cliff_band.
terrain may also carry base_elevation (yards), snowline (yards; snow textures above it) and
global_noise {large_wave_height, medium_noise_height, micro_noise_height}.
For water.features, use river/lake/ocean_edge with path/center, width/radius (ADT units,
half-width), level (surface height in yards), depth (channel/basin depth in yards) and, for
rivers, an optional level_end so the surface slopes downstream from level to level_end.
Lakes that touch a river are automatically snapped to the river surface height.
For roads, use path, width (ADT units, half-width), target_slope_max, surface, purpose;
the generator cuts and fills terrain to a smoothed profile along each road.
For terrain.settlement_flattening use center, radius, max_slope_degrees (pads are flattened
toward the natural height at their centre).
For reverse_region_modes, use tile keys like "32,32" with preserve/polish/regenerate.

Keep numeric values realistic for WotLK ADT terrain: river depths 4-9, lake depths 8-16,
ridge heights 40-120, water levels below the base elevation so channels read as valleys.
Avoid impossible vertical overhangs unless object_intent says WMO needed.
"""


def read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        path.write_text(json.dumps(default, indent=2), encoding="utf-8")
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        out = dict(default)
        deep_update_inplace(out, data)
        return out
    except Exception as e:
        raise SystemExit(f"Could not read {path}: {e}")


def deep_update_inplace(base: Dict[str, Any], user: Dict[str, Any]) -> None:
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_update_inplace(base[k], v)
        else:
            base[k] = v


def slugify(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:32] or "aigen"


def infer_biome(prompt: str, fallback: str) -> str:
    p = prompt.lower()
    if any(w in p for w in ["jungle", "tropical", "stranglethorn", "vale", "lush", "rainforest"]):
        return "jungle"
    if any(w in p for w in ["plague", "undead", "lordaeron", "scarlet", "haunted", "grave", "crypt"]):
        return "plague"
    if any(w in p for w in ["northrend", "fjord", "grizzly", "vrykul", "borean", "howling"]):
        return "northrend"
    if any(w in p for w in ["snow", "ice", "frozen", "winter", "dun morogh"]):
        return "snow"
    if any(w in p for w in ["desert", "tanaris", "uldum", "canyon", "dry"]):
        return "desert"
    if any(w in p for w in ["swamp", "marsh", "bog", "wetlands"]):
        return "swamp"
    if any(w in p for w in ["forest", "pine", "elwynn", "silverpine", "woods"]):
        return "forest"
    return "northrend" if "northern" in fallback.lower() else "forest"


def make_zone_name(prompt: str, biome: str) -> str:
    p = prompt.lower()
    if "coast" in p or "shore" in p or "fjord" in p:
        suffix = "Coast"
    elif "valley" in p:
        suffix = "Valley"
    elif "forest" in p or "woods" in p:
        suffix = "Woods"
    elif "island" in p:
        suffix = "Isle"
    elif "desert" in p:
        suffix = "Wastes"
    else:
        suffix = "Reach"
    adjective_bank = {
        "jungle": ["Mossvale", "Greenreed", "Warmgrove", "Riverbloom"],
        "plague": ["Graypine", "Rotmere", "Hollowgrave", "Ashenvale"],
        "northrend": ["Frostpine", "Stormglen", "Skaldfjord", "Graycliff"],
        "snow": ["Frostmere", "Whitefall", "Icebarrow", "Snowdrift"],
        "desert": ["Suncleft", "Amberdune", "Dustscar", "Redmesa"],
        "swamp": ["Mirefen", "Bogshade", "Blackreed", "Stillmuck"],
        "forest": ["Greenhollow", "Pinefall", "Mistwood", "Oldbrook"]
    }
    seed = int(hashlib.md5(prompt.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    return f"{rng.choice(adjective_bank.get(biome, adjective_bank['forest']))} {suffix}"


def line_path(points: List[Tuple[float, float]]) -> List[List[float]]:
    return [[round(x, 3), round(y, 3)] for x, y in points]




def default_texture_layers_for_biome(biome_key: str, base_texture: str) -> List[Dict[str, Any]]:
    """Editable built-in texture paths. Learned rules override these in generate_adt.py."""
    # All paths verified against the 3.3.5a client listfile (tileset_listfile_335.txt).
    common = {
        "base": base_texture,
        "road": "Tileset\\Elwynn\\ElwynnDirtBase.blp",
        "rock": "Tileset\\Elwynn\\ElwynnRockBase.blp",
        "forest": "Tileset\\Elwynn\\ElwynnGrassShadow.blp",
        "shore": "Tileset\\Elwynn\\ElwynnRiverMudBase.blp",
        "snow": "Tileset\\IronForge\\IronForgeSnow01solid.blp",
        "sand": "Tileset\\Ashenvale\\AshenvaleSand.blp",
        "plague": "Tileset\\PlagueLandsEast\\EastPlaguedCorrupt.blp",
    }
    if biome_key == "jungle":
        common["road"] = "Tileset\\Stranglethorn\\StrangleThornDirt03.blp"; common["rock"] = "Tileset\\Stranglethorn\\StrangleThornRock05.blp"; common["forest"] = "Tileset\\Stranglethorn\\StrangleThornplants01.blp"; common["shore"] = "Tileset\\Stranglethorn\\StrangleThornMossrootDirt01.blp"; common["sand"] = "Tileset\\Darkshore\\DarkshoreSand.blp"
    elif biome_key == "snow":
        common["road"] = "Tileset\\IronForge\\IronForgeRock06road2.blp"; common["rock"] = "Tileset\\IronForge\\IronForgeRock07mountain.blp"; common["forest"] = "Tileset\\IronForge\\IronForgePineNeedles.blp"; common["shore"] = "Tileset\\IronForge\\IronForgeSnow05ScrapeBrown.blp"; common["snow"] = "Tileset\\IronForge\\IronForgeSnow03ripple.blp"; common["sand"] = "Tileset\\IronForge\\IronForgeSnow04Rock.blp"
    elif biome_key == "desert":
        common["road"] = "Tileset\\Tanaris\\TanarisDirtFootprint.blp"; common["rock"] = "Tileset\\Tanaris\\TanarisRockBase01.blp"; common["forest"] = "Tileset\\Tanaris\\TanarisCrackedGround.blp"; common["shore"] = "Tileset\\Tanaris\\TanarisSandStones.blp"; common["sand"] = "Tileset\\Tanaris\\TanarisSandBase02.blp"
    elif biome_key == "swamp":
        common["road"] = "Tileset\\Swamp of Sorrows\\SwampSorrowsStoneRoad07.blp"; common["rock"] = "Tileset\\Swamp of Sorrows\\SwampSorrowsRock02.blp"; common["forest"] = "Tileset\\Swamp of Sorrows\\SwampSorrowsRoot01.blp"; common["shore"] = "Tileset\\Swamp of Sorrows\\SwampSorrowsMuckdark01.blp"; common["sand"] = "Tileset\\Duskwallow Marsh\\DuskwallowSand.blp"
    elif biome_key == "plague":
        common["road"] = "Tileset\\PlagueLandsEast\\EastPlaguedRoadBase.blp"; common["forest"] = "Tileset\\PlagueLandsEast\\EastPlaguedDeadGrass.blp"; common["rock"] = "Tileset\\PlagueLandsEast\\EastPlaguedBaseRock.blp"; common["shore"] = "Tileset\\PlagueLandsEast\\EastPlaguedMudGrass.blp"
    elif biome_key == "northrend":
        common["road"] = "Tileset\\Expansion02\\BoreanTundra\\BT_RoadA.blp"; common["rock"] = "Tileset\\Expansion02\\BoreanTundra\\BT_RockA.blp"; common["forest"] = "Tileset\\Expansion02\\GrizzlyHills\\GH_PineNeedlesA.blp"; common["shore"] = "Tileset\\Expansion02\\HowlingFjord\\HFjords_ShoreA.blp"; common["snow"] = "Tileset\\Expansion02\\BoreanTundra\\BT_ColdarraSnowA.blp"; common["sand"] = "Tileset\\Expansion02\\BoreanTundra\\BT_SandA.blp"
    return [
        {"role":"base", "label":"primary ground", "path":common["base"], "priority":0.05},
        {"role":"forest", "label":"forest floor / darker grass", "path":common["forest"], "priority":0.35},
        {"role":"shore", "label":"mud or shore transition", "path":common["shore"], "priority":0.55},
        {"role":"rock", "label":"slope rock", "path":common["rock"], "priority":0.70},
        {"role":"road", "label":"road dirt", "path":common["road"], "priority":0.95},
        {"role":"snow", "label":"optional high elevation snow", "path":common["snow"], "priority":0.45},
        {"role":"sand", "label":"optional beach sand", "path":common["sand"], "priority":0.50},
        {"role":"plague", "label":"optional haunted/plague accent", "path":common["plague"], "priority":0.52},
    ]


def infer_size_from_prompt(prompt: str, default_size: List[int]) -> List[int]:
    p = prompt.lower()
    m = re.search(r"(\d+)\s*[x×]\s*(\d+)\s*adt", p)
    if not m:
        m = re.search(r"(\d+)\s+by\s+(\d+)\s*adt", p)
    if m:
        sx = max(1, min(16, int(m.group(1))))
        sy = max(1, min(16, int(m.group(2))))
        return [sx, sy]
    return [int(default_size[0]), int(default_size[1])]

def infer_map_name_from_prompt(prompt: str, configured: str) -> str:
    # Accept phrases such as "for map X", "map: jungle_test", or "directory name aigen".
    m = re.search(r"(?:for\s+map|map\s*[:=]|directory\s+name)\s+([A-Za-z0-9_\-]+)", prompt, re.I)
    if m:
        return slugify(m.group(1))
    return slugify(configured)

def build_offline_spec(prompt: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    biome_key = infer_biome(prompt, cfg.get("fallback_theme", ""))
    biome = BIOME_LIBRARY[biome_key]
    sx, sy = infer_size_from_prompt(prompt, cfg.get("size_adts", [4, 4]))
    ox, oy = [int(v) for v in cfg.get("origin_tile", [32, 32])]
    tiles_x = list(range(ox, ox + sx))
    tiles_y = list(range(oy, oy + sy))
    zone_name = make_zone_name(prompt, biome_key)
    map_name = infer_map_name_from_prompt(prompt, cfg.get("map_name") or zone_name)

    coastal = any(w in prompt.lower() for w in ["coast", "shore", "fjord", "island", "sea", "ocean"])
    haunted = any(w in prompt.lower() for w in ["haunted", "undead", "plague", "crypt", "grave", "ruin"])
    faction = "Alliance" if any(w in prompt.lower() for w in ["alliance", "human", "dwarf", "gnome"]) else "neutral frontier"

    base = 42.0 if biome_key not in ["snow", "northrend"] else 55.0
    water_level = 34.0 if coastal else 38.0
    if biome_key == "desert": water_level = 28.0
    if biome_key == "swamp": water_level = 36.0

    # Macro landmarks and traversal skeleton.
    main_road = line_path([
        (0.25, sy - 0.55),
        (1.2, sy - 1.15),
        (sx * 0.42, sy * 0.58),
        (sx * 0.63, sy * 0.42),
        (sx - 0.45, 0.75),
    ])
    # The river enters and leaves the generated block instead of starting on a
    # hillside; the generator smooths the polyline into a meander.
    river = line_path([
        (sx * 0.16, -0.08),
        (sx * 0.24, sy * 0.22),
        (sx * 0.40, sy * 0.40),
        (sx * 0.52, sy * 0.58),
        (sx * 0.66, sy * 0.74),
        (sx * 0.84, sy + 0.08),
    ])
    ridge_north = line_path([(0.0, 0.65), (sx * 0.3, 0.35), (sx * 0.7, 0.55), (sx, 0.25)])
    ridge_east = line_path([(sx - 0.35, 0.5), (sx - 0.55, sy * 0.45), (sx - 0.25, sy - 0.2)])
    coast_path = line_path([(0.0, 0.0), (0.12, sy * 0.25), (0.05, sy * 0.55), (0.18, sy), (0.0, sy)])

    settlements = [
        {
            "id": "entry_hamlet",
            "name": "Southwatch Camp" if faction == "Alliance" else "Wayfarer's Camp",
            "type": "small safe hub",
            "center": [round(sx * 0.25, 2), round(sy * 0.78, 2)],
            "radius": 0.34,
            "faction": faction,
            "terrain_requirements": {"flatten_radius": 0.28, "max_slope_degrees": 4},
            "visual_grammar": ["central campfire", "2-4 tents or houses", "crates near road", "lamps at entrance", "partial fence facing wilderness"]
        },
        {
            "id": "mid_village",
            "name": "Old Ferry" if coastal else "Bridgefield",
            "type": "quest village / crossing hub",
            "center": [round(sx * 0.44, 2), round(sy * 0.62, 2)],
            "radius": 0.42,
            "faction": "contested",
            "terrain_requirements": {"flatten_radius": 0.34, "max_slope_degrees": 5},
            "visual_grammar": ["main road bends around hub", "bridge or dock focus", "market clutter", "visible inn/tower silhouette"]
        }
    ]
    if haunted:
        settlements.append({
            "id": "ruined_chapel",
            "name": "Chapel of Last Bells",
            "type": "hostile ruined landmark",
            "center": [round(sx * 0.74, 2), round(sy * 0.24, 2)],
            "radius": 0.46,
            "faction": "undead hostile",
            "terrain_requirements": {"flatten_radius": 0.31, "max_slope_degrees": 7},
            "visual_grammar": ["broken walls", "grave rows", "dead trees", "blackened road spur", "crypt entrance must face approach path"]
        })

    subzones = [
        {"id": "southern_entry", "name": "Southern Approach", "bounds": [[0, sy*0.62], [sx*0.48, sy]], "role": "safe introduction and first vista", "density": "low", "palette_bias": [biome["palette"][0], biome["palette"][1]]},
        {"id": "central_crossing", "name": "The Crossing", "bounds": [[sx*0.32, sy*0.38], [sx*0.7, sy*0.72]], "role": "main navigation knot", "density": "medium", "palette_bias": [biome["palette"][1], biome["palette"][4]]},
        {"id": "northern_highlands", "name": "Northern Highridge", "bounds": [[0, 0], [sx, sy*0.32]], "role": "ridge, cliffs, hostile reveal", "density": "medium-high", "palette_bias": [biome["palette"][2], biome["palette"][3]]},
        {"id": "eastern_wilds", "name": "Eastern Wilds", "bounds": [[sx*0.62, sy*0.25], [sx, sy]], "role": "wilderness loop and optional objectives", "density": "medium", "palette_bias": [biome["palette"][0], biome["palette"][3]]}
    ]
    if coastal:
        subzones.append({"id": "western_coast", "name": "Windbreak Shore", "bounds": [[0, 0], [sx*0.22, sy]], "role": "ocean edge, docks, shoreline movement", "density": "low-medium", "palette_bias": [biome["palette"][4], biome["palette"][2]]})

    reverse_modes = {}
    for y in tiles_y:
        for x in tiles_x:
            lx = x - ox
            ly = y - oy
            mode = "polish"
            # By default, empty/redesign targets occupy north/east, while entry corridor is preserved-ish.
            if lx >= sx * 0.60 or ly <= sy * 0.30:
                mode = "regenerate"
            if lx <= sx * 0.35 and ly >= sy * 0.62:
                mode = "preserve"
            reverse_modes[f"{x},{y}"] = mode

    spec = {
        "version": "0.4",
        "map_name": map_name,
        "zone_name": zone_name,
        "client_version": cfg.get("client_version", "3.3.5a"),
        "origin_tile": [ox, oy],
        "size_adts": [sx, sy],
        "tiles_x": tiles_x,
        "tiles_y": tiles_y,
        "source_prompt": prompt,
        "creative_intent": {
            "one_sentence": f"A {biome['name']} zone with readable roads, strong landmarks, and restrained WotLK-era composition.",
            "primary_biome": biome["name"],
            "mood_words": biome["colors"] + (["haunted", "ruined", "uneasy"] if haunted else ["frontier", "windy", "remote"]),
            "blizzlike_rules": [
                "strong macro silhouette before small details",
                "limited terrain texture vocabulary per subzone",
                "roads must clearly connect every major gameplay pocket",
                "empty travel space is allowed; random clutter is not",
                "landmarks should be visible before the player reaches them"
            ],
            "scale_guidance": "Third-person readable: wide roads, chunky silhouettes, large negative space between quest pockets."
        },
        "terrain": {
            "base_elevation": base,
            "snowline": base - 45.0 if biome_key == "snow" else base + (70.0 if biome_key == "northrend" else 120.0),
            "global_noise": {"large_wave_height": 22, "medium_noise_height": 6.5, "micro_noise_height": 1.1, "micro_noise_allowed": True},
            "features": [
                {"id": "north_macro_ridge", "type": "ridge", "path": ridge_north, "height": 75, "width": 0.75, "purpose": "northern zone wall and distant silhouette"},
                {"id": "eastern_boundary_ridge", "type": "ridge", "path": ridge_east, "height": 65, "width": 0.62, "purpose": "contain the play space without invisible walls"},
                {"id": "central_river_valley", "type": "valley", "path": river, "depth": 25, "width": 0.48, "purpose": "natural travel guide and water basin"},
                {"id": "entry_plateau", "type": "plateau", "center": [round(sx*0.25,2), round(sy*0.78,2)], "height": 8, "radius": 0.42, "purpose": "flat safe start hub"},
                {"id": "mid_crossing_flat", "type": "plateau", "center": [round(sx*0.44,2), round(sy*0.62,2)], "height": 6, "radius": 0.45, "purpose": "flat crossing village and bridge approach"},
                {"id": "wilds_basin", "type": "basin", "center": [round(sx*0.82,2), round(sy*0.74,2)], "depth": 18, "radius": 0.72, "purpose": "redo target with more interesting lowland loop"}
            ] + ([{"id": "western_coast_cut", "type": "coast", "path": coast_path, "depth": 48, "width": 0.42, "purpose": "coastal water edge and cliffs"}] if coastal else []),
            "settlement_flattening": [
                {"settlement_id": s["id"], "center": s["center"], "radius": s["terrain_requirements"]["flatten_radius"], "max_slope_degrees": s["terrain_requirements"]["max_slope_degrees"]}
                for s in settlements
            ]
        },
        "water": {
            "enabled": True,
            "level": water_level,
            "liquid_type": 1,
            "features": [
                # The river surface slopes downstream (level -> level_end) and carves a
                # 6-yard channel; the lake snaps to the river surface where they meet.
                {"id": "main_river", "type": "river", "path": river, "width": 0.09 if sx <= 4 else 0.11, "level": water_level + 4.0, "level_end": water_level - 4.0, "depth": 6.0, "purpose": "zone-spanning visual guide"},
                {"id": "crossing_lake", "type": "lake", "center": [round(sx*0.58,2), round(sy*0.64,2)], "radius": 0.2, "level": water_level, "depth": 11.0, "purpose": "landmark beside the crossing village"}
            ] + ([{"id": "ocean_west", "type": "ocean_edge", "path": coast_path, "width": 0.24, "level": water_level - 6.0, "depth": 26.0, "purpose": "coastal boundary"}] if coastal else []),
            "shore_rules": ["mud/sand mask 0.05-0.12 ADT from liquid", "rocky banks where slope exceeds 20 degrees", "roads require bridge/ford markers where crossing river"]
        },
        "roads": [
            {"id": "main_story_road", "path": main_road, "width": 0.065, "target_slope_max": 11, "surface": "dirt road", "purpose": "connect entry hub, crossing, northern landmark"},
            {"id": "ruin_spur", "path": line_path([(sx*0.55, sy*0.54), (sx*0.66, sy*0.42), (sx*0.74, sy*0.24)]), "width": 0.045, "target_slope_max": 14, "surface": "dark dirt / broken stone", "purpose": "optional hostile branch"},
            {"id": "wilds_loop", "path": line_path([(sx*0.56, sy*0.58), (sx*0.76, sy*0.7), (sx*0.86, sy*0.83), (sx*0.68, sy*0.9), (sx*0.49, sy*0.72)]), "width": 0.045, "target_slope_max": 13, "surface": "trampled earth", "purpose": "make regenerated empty area playable"}
        ],
        "textures": {
            "base_texture": biome["base_texture"],
            "palette": biome["palette"],
            "texture_layers": default_texture_layers_for_biome(biome_key, biome["base_texture"]),
            "layer_budget_per_chunk": 4,
            "alpha_painting": {
                "enabled": True,
                "max_layers_per_chunk": 4,
                "roles": ["base", "forest", "shore", "rock", "road", "snow", "sand", "plague"],
                "style": "large connected masks, road continuity, slope rock, shoreline mud, no speckle"
            },
            "mask_rules": [
                {"texture": biome["palette"][0], "where": "default low/medium slope ground"},
                {"texture": biome["palette"][1], "where": "distance_to_road < road_width"},
                {"texture": biome["palette"][2], "where": "slope > 28 degrees or ridge/cliff feature"},
                {"texture": biome["palette"][3], "where": "forest or sheltered inland pockets"},
                {"texture": biome["palette"][4], "where": "within shoreline band or wet basin"}
            ],
            "anti_private_server_smell": ["no speckled alphamaps", "no more than one accent texture per chunk", "road texture must be continuous", "slope rock should form connected shapes"]
        },
        "subzones": subzones,
        "landmarks": [
            {"id": "entry_vista", "name": "First Ridge View", "position": [round(sx*0.22,2), round(sy*0.72,2)], "type": "vista", "visibility_goal": "player sees central crossing and northern ridge within 20 seconds"},
            {"id": "crossing_bridge", "name": "The Old Bridge", "position": [round(sx*0.54,2), round(sy*0.55,2)], "type": "bridge/dock landmark", "visibility_goal": "readable from both road approaches"},
            {"id": "northern_silhouette", "name": "Highridge Ruins", "position": [round(sx*0.74,2), round(sy*0.24,2)], "type": "distant hostile landmark", "visibility_goal": "visible above treeline from central road"}
        ],
        "settlements": settlements,
        "encounter_spaces": [
            {"id": "entry_wolves", "center": [round(sx*0.36,2), round(sy*0.74,2)], "radius": 0.28, "density": "low", "terrain_need": "gentle slopes and sparse trees"},
            {"id": "river_murlocs_or_bandits", "center": [round(sx*0.60,2), round(sy*0.63,2)], "radius": 0.35, "density": "medium", "terrain_need": "shoreline and clear pull pockets"},
            {"id": "wilds_hostile_loop", "center": [round(sx*0.82,2), round(sy*0.78,2)], "radius": 0.5, "density": "medium-high", "terrain_need": "regenerated empty ADTs should receive terrain shape and road loop"}
        ],
        "object_intent": {
            "status": "intent_only_not_applied_by_v0_5",
            "safe_strategy": ["reuse existing assets when reverse-improving", "use whitelisted biome sets only", "never choose from all M2/WMO paths blindly", "quarantine crashy assets"],
            "desired_asset_families": biome["assets"],
            "placement_grammars": {
                "safe_hub": ["flat pad", "central fire/well", "2-5 structures", "crate clusters", "road lamps", "partial fence"],
                "hostile_ruin": ["broken walls", "dead trees", "grave rows", "debris radius", "clear entrance silhouette"],
                "wilderness": ["tree clusters not uniform", "rock clusters on slope changes", "clear roads", "empty quiet spaces"]
            }
        },
        "minimap": {"enabled": True, "style": "height/water derived preview", "labels_to_add_later": [s["name"] for s in settlements]},
        "wdl": {"enabled": True, "custom_only": True, "purpose": "low-resolution distant terrain from generated height field"},
        "reverse_region_modes": reverse_modes,
        "polish_rules": {
            "preserve": ["do not alter terrain except optional seam touch in future", "do not remove objects", "regenerate WDL/minimap only"],
            "polish": ["smooth high-frequency brush noise", "preserve macro shape", "fix seams", "repaint roads/shorelines in future texture pass"],
            "regenerate": ["replace empty terrain with planned macro forms", "blend border 0.10 ADT against neighbors", "ensure road/water continuity"]
        },
        "generator_config": {
            "mode": cfg.get("generator_defaults", {}).get("mode", "both"),
            "area_id": cfg.get("generator_defaults", {}).get("area_id", 0),
            "output_dir": cfg.get("generator_defaults", {}).get("output_dir", "output_loose"),
            "base_texture": biome["base_texture"],
            "height_scale": 18.0,
            "hill_height": 35.0,
            "valley_depth": 20.0,
            "water": {"enabled": True, "level": water_level, "liquid_type": 1, "liquid_vertex_format": 0, "river_width": 27.0, "shore_tolerance": 11.0},
            "wdl": {"enabled": True, "custom_only": True},
            "minimap": {"enabled": True, "tile_size": 256, "write_tga_previews": True, "write_world_minimaps_copy": False, "write_md5translate": True, "write_full_md5translate": False},
            "texture_painting": {"enabled": True, "learned_rules_path": "learned_blizzlike_rules.json", "max_layers_per_chunk": 4, "alpha_mode": "raw_4096", "breakup": 0.22},
            "dbc": {"map_id": 0, "area_id": 0},
            "seed": 1337
        },
        "validation_targets": {
            "road_slope_max_degrees": 14,
            "settlement_slope_max_degrees": 5,
            "texture_layers_per_chunk_max": 4,
            "adt_seam_delta_max": 0.25,
            "water_should_not_flood_non_basin_land": True,
            "minimap_tiles_expected": sx * sy,
            "mcnk_per_adt_expected": 256
        },
        "production_notes": [
            "v0.11 consumes terrain/water/road/texture spec and paints real MCLY/MCAL alphamaps automatically.",
            "M2/WMO placement is intentionally intent-only until an asset trust database exists.",
            "For half-complete handmade maps, feed 3x3 or 4x4 ADT groups and mark center empty tiles regenerate while finished border tiles preserve/polish.",
            "Server-side DBC/SQL remains outside this package by design."
        ]
    }
    return spec


def force_spec_defaults(spec: Dict[str, Any], cfg: Dict[str, Any], prompt: str) -> Dict[str, Any]:
    # Fill missing top-level values without destroying AI output.
    sx, sy = [int(v) for v in spec.get("size_adts") or cfg.get("size_adts", [6, 6])]
    ox, oy = [int(v) for v in spec.get("origin_tile") or cfg.get("origin_tile", [32, 32])]
    spec.setdefault("version", "0.5")
    spec.setdefault("client_version", cfg.get("client_version", "3.3.5a"))
    spec.setdefault("source_prompt", prompt)
    spec["size_adts"] = [sx, sy]
    spec["origin_tile"] = [ox, oy]
    spec["tiles_x"] = list(range(ox, ox + sx)) if not spec.get("tiles_x") else [int(x) for x in spec["tiles_x"]]
    spec["tiles_y"] = list(range(oy, oy + sy)) if not spec.get("tiles_y") else [int(y) for y in spec["tiles_y"]]
    spec.setdefault("map_name", slugify(cfg.get("map_name", "aigen")))
    spec.setdefault("zone_name", spec["map_name"].replace("_", " ").title())
    spec.setdefault("generator_config", {})
    defaults = cfg.get("generator_defaults", {})
    for k, v in defaults.items():
        spec["generator_config"].setdefault(k, v)
    spec.setdefault("water", {"enabled": True, "level": 38.0, "features": []})
    spec.setdefault("terrain", {"base_elevation": 42.0, "features": []})
    spec.setdefault("roads", [])
    spec.setdefault("textures", {"base_texture": BIOME_LIBRARY["forest"]["base_texture"], "palette": BIOME_LIBRARY["forest"]["palette"]})
    spec.setdefault("reverse_region_modes", {})
    for y in spec["tiles_y"]:
        for x in spec["tiles_x"]:
            spec["reverse_region_modes"].setdefault(f"{x},{y}", "polish")
    return spec


def extract_json_object(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in AI response")
    return json.loads(text[start:end+1])


def call_anthropic(prompt: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Plan the zone with Claude through the official anthropic SDK (pip install anthropic).

    Credentials resolve from ANTHROPIC_API_KEY (or an `ant auth login` profile).
    Thinking is adaptive by default on current models, so it is not configured
    here; the request streams because the spec JSON is long.
    """
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError("provider=anthropic needs the SDK: pip install anthropic") from e
    model = os.environ.get("ANTHROPIC_MODEL") or cfg.get("anthropic_model", DEFAULT_AI_CONFIG["anthropic_model"])
    client = anthropic.Anthropic()
    try:
        with client.messages.stream(
            model=model,
            max_tokens=int(cfg.get("max_tokens", 16000)),
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            response = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise RuntimeError("Anthropic API key rejected (set ANTHROPIC_API_KEY)") from e
    except anthropic.RateLimitError as e:
        raise RuntimeError("Anthropic API rate limited; retry later") from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise RuntimeError("Could not reach the Anthropic API") from e
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to write this zone spec")
    text = "".join(block.text for block in response.content if block.type == "text")
    return extract_json_object(text)


def call_openai_compatible(prompt: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    api_key = os.environ.get("AI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("No AI_API_KEY or OPENAI_API_KEY set")
    base_url = os.environ.get("AI_BASE_URL") or cfg.get("base_url", DEFAULT_AI_CONFIG["base_url"])
    model = os.environ.get("AI_MODEL") or cfg.get("model", DEFAULT_AI_CONFIG["model"])
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": float(cfg.get("temperature", 0.7)),
        "max_tokens": int(cfg.get("max_tokens", 12000)),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        "response_format": {"type": "json_object"}
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"AI HTTP error {e.code}: {detail}")
    data = json.loads(raw)
    content = data["choices"][0]["message"]["content"]
    return extract_json_object(content)


def write_generator_config_from_spec(spec: Dict[str, Any], path: Path) -> None:
    gen = dict(spec.get("generator_config", {}))
    gen["map_name"] = spec.get("map_name", gen.get("map_name", "aigen"))
    gen["tiles_x"] = spec.get("tiles_x", gen.get("tiles_x", [32, 33]))
    gen["tiles_y"] = spec.get("tiles_y", gen.get("tiles_y", [32, 33]))
    gen["zone_spec_path"] = str(Path("zone_spec.json"))
    gen["base_texture"] = spec.get("textures", {}).get("base_texture", gen.get("base_texture", BIOME_LIBRARY["forest"]["base_texture"]))
    water = dict(gen.get("water", {}))
    water.setdefault("enabled", spec.get("water", {}).get("enabled", True))
    water.setdefault("level", spec.get("water", {}).get("level", 38.0))
    water.setdefault("liquid_type", spec.get("water", {}).get("liquid_type", 1))
    water.setdefault("liquid_vertex_format", 0)
    water.setdefault("river_width", 27.0)
    water.setdefault("shore_tolerance", 11.0)
    gen["water"] = water
    gen.setdefault("mode", "custom")
    gen.setdefault("area_id", 0)
    gen.setdefault("output_dir", "output_loose")
    gen.setdefault("wdl", {"enabled": True, "custom_only": True})
    gen.setdefault("minimap", {"enabled": True, "tile_size": 256, "write_tga_previews": True, "write_world_minimaps_copy": False, "write_md5translate": True, "write_full_md5translate": False})
    gen.setdefault("texture_painting", {"enabled": True, "learned_rules_path": "learned_blizzlike_rules.json", "max_layers_per_chunk": 4, "alpha_mode": "raw_4096", "breakup": 0.22})
    gen.setdefault("dbc", {"map_id": 0, "area_id": 0})
    gen.setdefault("seed", 1337)
    if spec.get("textures", {}).get("texture_layers"):
        gen["texture_layers"] = spec["textures"]["texture_layers"]
    path.write_text(json.dumps(gen, indent=2), encoding="utf-8")


def write_reverse_config_from_spec(spec: Dict[str, Any], path: Path) -> None:
    rev = {
        "input_dir": "input_existing",
        "map_name": spec.get("map_name", "aigen"),
        "output_dir": "output_reverse",
        "tiles": "auto",
        "region_modes": spec.get("reverse_region_modes", {}),
        "improvement": {
            "polish_strength": 0.28,
            "regenerate_strength": 1.0,
            "border_blend_adt": 0.10,
            "preserve_objects": True,
            "edit_m2_wmo": False
        },
        "zone_spec_path": "zone_spec.json"
    }
    path.write_text(json.dumps(rev, indent=2), encoding="utf-8")


def write_brief(spec: Dict[str, Any], path: Path) -> None:
    lines = []
    lines.append(f"# {spec.get('zone_name', 'AI Zone')} ({spec.get('map_name', 'aigen')})")
    lines.append("")
    ci = spec.get("creative_intent", {})
    lines.append(str(ci.get("one_sentence", "AI-generated zone spec.")))
    lines.append("")
    lines.append(f"Size: {spec.get('size_adts')} ADTs, origin tile {spec.get('origin_tile')}")
    lines.append("")
    lines.append("## Subzones")
    for z in spec.get("subzones", []):
        lines.append(f"- **{z.get('name', z.get('id'))}**: {z.get('role','')}; density={z.get('density','')}")
    lines.append("")
    lines.append("## Roads")
    for r in spec.get("roads", []):
        lines.append(f"- **{r.get('id')}**: {r.get('purpose','')} path={r.get('path')}")
    lines.append("")
    lines.append("## Terrain Features")
    for f in spec.get("terrain", {}).get("features", []):
        lines.append(f"- **{f.get('id')}** ({f.get('type')}): {f.get('purpose','')}")
    lines.append("")
    lines.append("## Object Intent")
    oi = spec.get("object_intent", {})
    for k, v in oi.items():
        if isinstance(v, list):
            lines.append(f"- {k}: {', '.join(map(str, v))}")
        elif isinstance(v, str):
            lines.append(f"- {k}: {v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", help="Zone prompt text")
    ap.add_argument("--prompt-file", default="zone_prompt.txt")
    ap.add_argument("--config", default="ai_config.json")
    ap.add_argument("--out", default="zone_spec.json")
    ap.add_argument("--force-offline", action="store_true")
    args = ap.parse_args()

    cfg = read_json(Path(args.config), DEFAULT_AI_CONFIG)
    if args.prompt:
        prompt = args.prompt
    else:
        p = Path(args.prompt_file)
        if not p.exists():
            p.write_text(
                "Create a 6x6 ADT haunted northern pine coast for WoW 3.3.5a. "
                "It should feel like Silverpine, Grizzly Hills, and Howling Fjord. "
                "Some areas should be complete/preserved, but the north-east empty ADTs should be regenerated with a hostile ruin, river valley, roads, and a small safe camp.\n",
                encoding="utf-8"
            )
        prompt = p.read_text(encoding="utf-8").strip()

    mode = str(cfg.get("mode", "auto")).lower()
    provider = str(cfg.get("provider", "auto")).lower()
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    has_openai = bool(os.environ.get("AI_API_KEY") or os.environ.get("OPENAI_API_KEY"))
    if provider == "auto":
        provider = "anthropic" if has_anthropic else "openai_compatible"
    use_api = (mode == "api") or (mode == "auto" and (has_anthropic or has_openai) and not args.force_offline)
    planner = "offline"
    if use_api:
        try:
            if provider == "anthropic":
                raw_spec = call_anthropic(prompt, cfg)
                planner = "anthropic"
            else:
                raw_spec = call_openai_compatible(prompt, cfg)
                planner = "openai_compatible"
        except Exception as e:
            if mode == "api":
                raise SystemExit(f"AI planner failed: {e}")
            print(f"AI planner failed, falling back to offline planner: {e}")
            raw_spec = build_offline_spec(prompt, cfg)
    else:
        raw_spec = build_offline_spec(prompt, cfg)

    spec = force_spec_defaults(raw_spec, cfg, prompt)
    spec["planner_used"] = planner
    spec["generated_at_unix"] = int(time.time())

    out = Path(args.out)
    out.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    write_generator_config_from_spec(spec, Path("config.json"))
    write_reverse_config_from_spec(spec, Path("reverse_config.json"))
    if cfg.get("write_markdown_brief", True):
        write_brief(spec, Path("ZONE_SPEC_BRIEF.md"))

    print(f"Wrote {out.resolve()}")
    print(f"Planner used: {planner}")
    print(f"Map: {spec.get('map_name')} | Zone: {spec.get('zone_name')} | Tiles: {len(spec.get('tiles_x',[]))*len(spec.get('tiles_y',[]))}")
    print("Also wrote config.json, reverse_config.json, and ZONE_SPEC_BRIEF.md")


if __name__ == "__main__":
    main()
