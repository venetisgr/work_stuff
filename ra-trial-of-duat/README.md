# Trial of the Duat

A 75-second arcade arena survival game for Reddit, built with **Three.js** on **Devvit Web** — a sibling of
[Zeus: Trial of Olympus](../zeus-trial-of-olympus/) with Egyptian gods. Pick **Ra**, **Horus** or **Anubis**, hold the
sandstone arena against the brood of Apep, build a combo, charge your ultimate and chase a score.

## Run it

```bash
npm install
npm run dev:client        # game only, in a normal browser (server calls fall back to localStorage)
npm run build             # dist/client + dist/server
npx devvit login          # once
npm run devvit:playtest   # playtest on a dev subreddit
npm run devvit:upload     # upload a version
```

Install the app on a subreddit, then use the subreddit menu **Create Trial of the Duat post**
(it also creates a post on install).

## The gods

| God | Style | HP | Speed | Strengths |
|---|---|---|---|---|
| **Ra** — Sun God | balanced, gold beams | 5 | 8.2 | every beam chains to one extra enemy · ultimate **Eye of Ra** |
| **Horus** — Falcon of the Sky | fast, blue beams | 4 | 9.4 | quickest attack rate, longest reach · ultimate **Wings of Horus** |
| **Anubis** — Judge of the Dead | tough, green soul-fire | 6 | 7.6 | widest beam, +40 % Divine Power from kills and Ankhs · ultimate **Weighing of the Heart** |

All stats live in `DEITIES` in `src/client/game/config.ts`. Your pick is remembered in `localStorage`.

## Controls

| | Desktop | Touch |
|---|---|---|
| Move | WASD / arrows | left-thumb floating joystick |
| Divine beam | Space / left click (hold to keep firing) — aims at the mouse, with aim assist | BEAM button — auto-targets the nearest enemy |
| Dodge (i-frames) | Shift | DODGE |
| Ultimate (full Divine Power) | E / Q / right click | ULT |

## The run

| Time | Phase | |
|---|---|---|
| 0–15 s | Warm-up | a few mummies, learn the loop |
| 15–35 s | Apep's Brood Gathers | scarabs appear, faster spawns |
| 35–65 s | The Sands Rise | colossi, 2-spawn batches, the sky turns to dusk |
| 65–75 s | The Eternal Eclipse | spawn peak, stray strikes across the arena, the sun goes black |

Surviving to the end triggers a slow-mo barrage that clears the arena and a victory pose. Falling to 0 HP ends the run early.

* **Score**: kills × combo multiplier, Ankh sparks, survival time, flat multi-kill bonuses, completion bonus.
* **Combo** (resets when hit): x1 → x2 (3 kills) → x3 (7) → x5 (12) → x8 (18). Higher tiers thicken the beams,
  change their colour and add chain arcs (+1 per tier).
* **Divine Power**: firing costs 3 %, kills and Ankhs refill it. At 100 % the ultimate calls a 2.4 s storm of
  divine strikes around your god (and heals one pip).
* **Enemies**: Mummy (normal), Scarab (fast, weak, zig-zags), Stone Colossus (slow, big, 3 hits, hits for 2).

## No external assets

Everything is generated in code, so there are no model or texture files to ship:

* **Gods** — `DeityModel.ts` builds each god from primitives (falcon/jackal head, sun disc, pschent, wings, broad
  collar, kilt). `RigAnimator` poses the limbs procedurally for idle, walk, ready, cast, ultimate, dodge (back-flip),
  hit, victory and defeat.
* **Arena** — canvas-painted sandstone floor with a hieroglyph band and sun-disc/ankh emblem, columns and beams,
  braziers, dunes, pyramids, obelisks and a sun disc that eclipses as the run progresses.
* **Enemies** — merged vertex-coloured geometry, rendered as 3 instanced meshes.
* **Audio** — procedural WebAudio with a mute button; `audio.loadSample(...)` / `audio.loadMusic(...)` can swap in real assets.

## Code map (`src/client/game`)

`Game` (loop, state machine, camera, deity selection) · `Player` (movement, dodge, cast timing, HP) ·
`DeityModel` (procedural gods + animator) · `EnemyManager` (pooled, instanced, director/spawner) ·
`CombatSystem` (attacks, chain beams, combo, charge, ultimate, finale) · `Pickups` (Ankh sparks) ·
`Arena` · `Effects` (pooled bolts, particles, rings, shake, flash) · `UI` · `Input` · `Audio` ·
`DailyChallenge` · `config` (all tuning, including the gods).

Server (`src/server/index.ts`): `GET /api/init`, `POST /api/score`, plus the post-creation menu/trigger.
Redis keys: `duat:best:<user>` (personal best), `duat:lb:<YYYY-MM-DD>` (daily sorted set, 14-day TTL).

## Daily Trial

`DailyChallenge` derives a seed from the UTC date; the spawn director uses a seeded PRNG so everyone gets the same
spawn angles and enemy mix that day, whichever god they pick. Daily scores go to the daily leaderboard; free play
only updates your personal best. Every network call is best-effort with a timeout and a `localStorage` fallback.
