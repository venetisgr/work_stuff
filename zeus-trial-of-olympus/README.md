# Zeus: Trial of Olympus

A 75-second arcade arena survival game for Reddit, built with **Three.js** on **Devvit Web**.
You are Zeus on a marble platform above the clouds of Olympus. Shades rise from the rim, you smite
them with lightning, build a combo, charge your ultimate and chase a score — then press **PLAY AGAIN**.

## Run it

```bash
npm install
npm run dev:client        # game only, in a normal browser (server calls fall back to localStorage)
npm run build             # dist/client + dist/server
npx devvit login          # once
npm run devvit:playtest   # playtest on a dev subreddit
npm run devvit:upload     # upload a version
```

Install the app on a subreddit, then use the subreddit menu **Create Zeus: Trial of Olympus post**
(it also creates a post on install).

## Controls

| | Desktop | Touch |
|---|---|---|
| Move | WASD / arrows | left-thumb floating joystick |
| Lightning | Space / left click (hold to keep firing) — aims at the mouse, with aim assist | SMITE button — auto-targets the nearest shade |
| Dodge (i-frames) | Shift | DODGE |
| Ultimate (full Divine Charge) | E / Q / right click | ULT |

## Levels

Five hand-made trials, then an **endless ramp**: every level after 5 is generated (bigger arena up to a 44-unit radius,
longer timer up to 120 s, denser/faster hordes, more heavy creatures) so there is no last level. Clearing a level (surviving
its timer) unlocks the next; pick any unlocked level from the level strip on the menu.

| # | Trial | Arena radius | Time | New threats | Wine cups |
|---|---|---|---|---|---|
| 1 | Foothills of Olympus | 15 | 60 s | Satyrs, Harpies | – |
| 2 | Temple of Athena | 18 | 70 s | Spartoi (skeleton hoplites) | – |
| 3 | The Labyrinth | 21 | 75 s | Minotaur (charges) | 6 % |
| 4 | Poseidon's Wrath | 24 | 80 s | Cyclops | 8 % |
| 5 | Gates of Hades | 27 | 90 s | everything | 10 % |
| 6+ | generated (Elysian Fields, Mount Ida, …) | 29 → 44 | 93 → 120 s | heavier mixes | 10 % |

Higher levels also multiply score (×1 … ×2+). On levels 3+ creatures can drop a **wine cup** (kylix) that restores one health —
only while Zeus is hurt, one on the floor at a time, and heavy creatures drop it far more often.
Tuning lives in `LEVELS` / `levelById` in `game/config.ts`. The Daily Trial always runs on one of the first three arenas.

## The run

| Time | Phase | |
|---|---|---|
| 0–15 s | Warm-up | few shades, learn the loop |
| 15–35 s | Escalation | runners appear, faster spawns |
| 35–65 s | Chaos | brutes, 2-spawn batches, darker storm |
| 65–75 s | Wrath of Olympus | spawn peak, strikes across the arena, stormiest sky |

Ends with a slow-mo lightning barrage that clears the arena and a victory pose. Falling to 0 HP ends the run early.

* **Score**: kills × combo multiplier, Divine Sparks, survival time, flat multi-kill bonuses, completion bonus.
* **Combo** (resets when hit): x1 → x2 (3 kills) → x3 (7) → x5 (12) → x8 (18). Higher tiers thicken the bolts,
  change their colour and add chain lightning (+1 arc per tier).
* **Divine Charge**: smiting costs 3 %, kills and sparks refill it. At 100 % the ultimate calls a 2.4 s storm
  around Zeus (and heals one pip).
* **Creatures**: Satyr (basic), Harpy (flies, fast, zig-zags), Spartoi (2 hits, shield and spear), Minotaur (4 hits, telegraphed charge), Cyclops (8 hits, slow, hits hard).

## Zeus model & animations

`src/client/public/zeus.glb` is the supplied Mixamo-rigged model (25 clips). Its textures were re-encoded
as JPEG (14.6 MB → 3.4 MB; geometry, rig and animation untouched) with `tools/optimize_glb.py` so it loads
quickly in a Reddit webview. To inspect any GLB: `npm run inspect:glb -- path/to/model.glb`.

`AnimationController` maps clips to gameplay **roles** by name pattern (first matching rule wins, rules in
`AnimationController.ts`) rather than hard-coded indices. For the supplied file that resolves to:

| Role | Clip |
|---|---|
| idle | `standing_idle` |
| ready (ultimate charged) | `spell_simple_idle` |
| move | `walk` (time-scaled with speed — there is no run clip) |
| lightning cast | `spell_cast` (sped up to ~0.4 s) |
| ultimate | `spell_cast_epic` |
| dodge | `backflip` (Zeus faces away from the dash so it travels backwards) |
| hit reaction | `sword_block` (no dedicated hit clip) |
| victory | `victory_fist_pump` |
| defeat | `death_b` |

All transitions cross-fade. Hip X/Z translation is locked per clip so Mixamo "root drift" doesn't fight
gameplay movement. The mapping is logged to the console and available as `window.__zeus`.
If `zeus.glb` fails to load, a simple stand-in model is used so the game still runs.

## Code map (`src/client/game`)

`Game` (loop, state machine, camera, resize, cleanup) · `Player` (GLB, movement, dodge, cast timing, HP) ·
`AnimationController` · `EnemyManager` (pooled, instanced: 3 draw calls for all enemies, director/spawner) ·
`CombatSystem` (attacks, chain lightning, combo, charge, ultimate, finale) · `Pickups` (Divine Sparks) ·
`Arena` (platform, columns, braziers, clouds, backdrop, storm atmosphere) · `Effects` (pooled bolts, particles,
rings, shake, flash) · `UI` (HUD, menu, results) · `Input` · `Audio` · `DailyChallenge` · `config` (all tuning).

Posts show a branded splash card (`splash.html`, big PLAY button) that opens the game (`game.html`) in expanded mode.

Server (`src/server/index.ts`): `GET /api/init`, `POST /api/score`, plus the post-creation menu/trigger.
Redis keys: `zeus:best:<user>` (personal best), `zeus:lb:<YYYY-MM-DD>` (daily sorted set, 14-day TTL).

## Daily Trial

`DailyChallenge` derives a seed from the UTC date; the spawn director uses a seeded PRNG (separate from
cosmetic randomness) so everyone gets the same spawn angles and enemy mix that day. Daily scores go to the
daily leaderboard; free play scores only update personal best. Every network call is best-effort with a
timeout and a `localStorage` fallback — the game never depends on the backend.

## Audio

Fully procedural WebAudio (thunder, zaps, impacts, pickups, ultimate, drum pulse that speeds up with the storm),
with a mute button. To use real assets later: `audio.loadSample('thunder', url)` / `audio.loadMusic(url)` in
`Game.ts`; the synth voice is replaced automatically.

## Performance

~65 draw calls and ~55k triangles at peak, 1 shadow-casting light (1024² on mobile), pixel ratio capped at
1.5 on touch devices, no per-frame allocations in the hot paths, pooled bolts/particles/enemies.
Testing so far was in headless Chromium with a software GL; profile on real phones before shipping.
