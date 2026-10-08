# Thor: Trial of Asgard

A 75-second arcade arena survival game for Reddit, built with **Three.js** on **Devvit Web**.
You are Thor on a rune-carved stone platform above the clouds of Asgard. Draugr rise from the rim, you smash
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

Install the app on a subreddit, then use the subreddit menu **Create Thor: Trial of Asgard post**
(it also creates a post on install).

## Controls

| | Desktop | Touch |
|---|---|---|
| Move | WASD / arrows | left-thumb floating joystick |
| Lightning | Space / left click (hold to keep firing) — aims at the mouse, with aim assist | SMASH button — auto-targets the nearest shade |
| Dodge (i-frames) | Shift | DODGE |
| Ultimate (full Odinforce) | E / Q / right click | ULT |

## The run

| Time | Phase | |
|---|---|---|
| 0–15 s | Warm-up | few draugr, learn the loop |
| 15–35 s | Escalation | runners appear, faster spawns |
| 35–65 s | Chaos | brutes, 2-spawn batches, darker storm |
| 65–75 s | Wrath of Asgard | spawn peak, strikes across the arena, stormiest sky |

Ends with a slow-mo lightning barrage that clears the arena and a victory pose. Falling to 0 HP ends the run early.

* **Score**: kills × combo multiplier, Rune Shards, survival time, flat multi-kill bonuses, completion bonus.
* **Combo** (resets when hit): x1 → x2 (3 kills) → x3 (7) → x5 (12) → x8 (18). Higher tiers thicken the bolts,
  change their colour and add chain lightning (+1 arc per tier).
* **Odinforce**: smiting costs 3 %, kills and sparks refill it. At 100 % the ultimate calls a 2.4 s storm
  around Thor (and heals one pip).
* **Enemies**: Draugr (normal undead), Wolf of Fenrir (fast, weak, zig-zags), Frost Giant (slow, big, 3 hits, hits for 2).

## Thor model & animations

`src/client/public/thor.glb` is the supplied Mixamo-rigged model (25 clips). Its textures were re-encoded
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
| dodge | `backflip` (Thor faces away from the dash so it travels backwards) |
| hit reaction | `sword_block` (no dedicated hit clip) |
| victory | `victory_fist_pump` |
| defeat | `death_b` |

All transitions cross-fade. Hip X/Z translation is locked per clip so Mixamo "root drift" doesn't fight
gameplay movement. The mapping is logged to the console and available as `window.__thor`.
If `thor.glb` fails to load, a simple stand-in model is used so the game still runs.

## Code map (`src/client/game`)

`Game` (loop, state machine, camera, resize, cleanup) · `Player` (GLB, movement, dodge, cast timing, HP) ·
`AnimationController` · `EnemyManager` (pooled, instanced: 3 draw calls for all enemies, director/spawner) ·
`CombatSystem` (attacks, chain lightning, combo, charge, ultimate, finale) · `Pickups` (Rune Shards) ·
`Arena` (platform, columns, braziers, clouds, backdrop, storm atmosphere) · `Effects` (pooled bolts, particles,
rings, shake, flash) · `UI` (HUD, menu, results) · `Input` · `Audio` · `DailyChallenge` · `config` (all tuning).

Server (`src/server/index.ts`): `GET /api/init`, `POST /api/score`, plus the post-creation menu/trigger.
Redis keys: `thor:best:<user>` (personal best), `thor:lb:<YYYY-MM-DD>` (daily sorted set, 14-day TTL).

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
