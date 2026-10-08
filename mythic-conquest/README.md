# Mythic Conquest

A browser real-time-strategy game in the spirit of *Age of Mythology*. Pick a civilization — **Greeks, Egyptians or Norse** —
choose a major god, build an economy, climb through four Ages, call on minor gods, raise mythic monsters and smash the enemy
Town Center with the help of a divine power. Built with **Three.js + TypeScript + Vite**, no assets (all models and sounds are procedural).

## Run it

```bash
npm install
npm run dev        # http://localhost:5173
npm run build      # static build in dist/
npm run typecheck
npm run soak       # headless AI-vs-AI simulation test (3 matches)
```

Dev shortcut: `?civ=norse&god=thor&enemy=greek&diff=hard&seed=7` skips the menu; add `&auto=1` to let the AI play your side too (spectate).

## How to play

**Goal:** destroy the enemy Town Center (Hill Fort for the Norse) before they destroy yours.

* **Economy** — villagers gather 🍖 food (berry bushes, then farms), 🪵 wood and 🪙 gold and drop it at a Town Center or Storehouse.
  Build Houses for population (cap 120).
* **Favor ✨** — each civilization earns it differently:
  | Civ | Favor source |
  |---|---|
  | Greeks | villagers **pray at Temples** (right-click a Temple with villagers) |
  | Egyptians | **Temples generate** Favor on their own |
  | Norse | every **kill** earns Favor |
* **Ages** — Archaic → Classical → Heroic → Mythic, advanced at the Town Center (Heroic and Mythic need a Temple).
  Each new Age you pick one of two **minor gods**: Classical gods give stat bonuses, Heroic and Mythic gods unlock a **mythic unit** trained at the Temple.
* **God power** (from the Classical Age, key **Q**) — one signature power per major god, costs Favor, has a cooldown.
* **Combat** — infantry > cavalry > archers > infantry. Mythic giants (Cyclops, Frost Giant, Great Crocodile) wreck buildings; towers shoot.

### The gods

| Civ | Major god | God power | Minor gods (Classical / Heroic / Mythic) → mythic units |
|---|---|---|---|
| Greek | **Zeus** | Lightning Storm — bolts rain over an area | Athena, Hermes / Ares → Minotaur, Artemis → Centaur / Hephaestus → Cyclops, Hecate → Medusa |
| | **Poseidon** | Earthquake — wrecks buildings, slows units | |
| | **Hades** | Underworld Passage — summons 6 skeletons | |
| Egyptian | **Ra** | Solar Wrath — sunfire burns an area | Bast, Thoth / Anubis → Anubite, Sekhmet → Sphinx / Horus → Phoenix, Sobek → Great Crocodile |
| | **Isis** | Blessing of Isis — mass heal | |
| | **Set** | Sandstorm — damages and slows enemies | |
| Norse | **Odin** | Valhalla Calls — summons 5 Einherjar | Freyr, Heimdall / Njord → Troll, Skadi → Valkyrie / Tyr → Fenrir Wolf, Hel → Frost Giant |
| | **Thor** | Hammer of Thor — huge damage + stun | |
| | **Loki** | Mischief — turns 3 enemy units to your side | |

### Controls

| | |
|---|---|
| Left click / drag | select / box-select (double-click: all of that type on screen, Shift adds) |
| Right click | move · gather · build · attack · pray · set rally point (with a building selected) |
| Arrow keys / screen edges / minimap | scroll the camera · mouse wheel zooms · `Space` centers on selection |
| `A` · `S` · `Q` | attack-move · stop · god power |
| `Ctrl+1…9` / `1…9` | set / recall control groups (double-tap to center) |
| `.` · `H` | next idle villager · jump to Town Center |
| `P` · `+`/`-` · `M` · `F1` · `Esc` | pause · speed (1–3×) · mute · help · cancel |

## Code map (`src/`)

| File | |
|---|---|
| `data.ts` | all static data: civs, gods, powers, units, buildings, age costs |
| `sim.ts` | headless simulation — map generation, A* pathfinding, gathering, building, production, combat, god powers, win check |
| `ai.ts` | computer opponent (easy / normal / hard): economy, build order, ages, minor gods, attack waves, god powers |
| `models.ts` | procedural low-poly units, mythic creatures and civ-styled buildings |
| `view.ts` | Three.js scene: terrain, entity views, effects, camera, picking, placement ghost |
| `ui.ts` / `game.ts` / `sfx.ts` | HUD & menus · input, selection and main loop · synthesized sound |

`sim.ts` and `ai.ts` have no DOM or Three.js dependency, so `scripts/soak.ts` runs whole AI-vs-AI games in Node (about a second each).

## Not (yet) in the game

Fog of war, heroes, naval maps, walls, multiplayer, save/load. The simulation is deliberately kept separate from rendering so these can be added.
