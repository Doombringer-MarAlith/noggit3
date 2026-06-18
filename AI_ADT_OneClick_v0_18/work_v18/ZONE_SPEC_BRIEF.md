# Greenreed Valley (x)

A cozy jungle valley zone with readable roads, strong landmarks, and restrained WotLK-era composition.

Size: [4, 4] ADTs, origin tile [32, 32]

## Subzones
- **Southern Approach**: safe introduction and first vista; density=low
- **The Crossing**: main navigation knot; density=medium
- **Northern Highridge**: ridge, cliffs, hostile reveal; density=medium-high
- **Eastern Wilds**: wilderness loop and optional objectives; density=medium

## Roads
- **main_story_road**: connect entry hub, crossing, northern landmark path=[[0.25, 3.45], [1.2, 2.85], [1.68, 2.32], [2.52, 1.68], [3.55, 0.75]]
- **ruin_spur**: optional hostile branch path=[[2.2, 2.16], [2.64, 1.68], [2.96, 0.96]]
- **wilds_loop**: make regenerated empty area playable path=[[2.24, 2.32], [3.04, 2.8], [3.44, 3.32], [2.72, 3.6], [1.96, 2.88]]

## Terrain Features
- **north_macro_ridge** (ridge): northern zone wall and distant silhouette
- **eastern_boundary_ridge** (ridge): contain the play space without invisible walls
- **central_river_valley** (valley): natural travel guide and water basin
- **entry_plateau** (plateau): flat safe start hub
- **mid_crossing_flat** (plateau): flat crossing village and bridge approach
- **wilds_basin** (basin): redo target with more interesting lowland loop

## Object Intent
- status: intent_only_not_applied_by_v0_5
- safe_strategy: reuse existing assets when reverse-improving, use whitelisted biome sets only, never choose from all M2/WMO paths blindly, quarantine crashy assets
- desired_asset_families: jungle trees, vines, wet rocks, small huts, ruined troll stones
