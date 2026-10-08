import { Rng, hashString } from './rng';
/** Tunables for the whole game. Everything is in world units / seconds. */
export const CFG = {
  PLAYER_RADIUS: 0.5,
  PLAYER_SPEED: 8.2,
  PLAYER_HEIGHT: 2.7,
  /** Mixamo characters face +Z, which is what atan2(x, z) assumes. */
  MODEL_YAW_OFFSET: 0,
  MAX_HP: 5,
  INVULN_AFTER_HIT: 1.1,

  ATTACK_COOLDOWN: 0.3,
  ATTACK_COST: 3, // % of charge
  ATTACK_RANGE: 22,
  BOLT_RADIUS: 1.5,
  CAST_DELAY: 0.14, // seconds from pressing attack to bolt landing
  AIM_ASSIST_DEG: 24,

  DODGE_TIME: 0.42,
  DODGE_SPEED: 19,
  DODGE_COOLDOWN: 0.9,

  ULT_DURATION: 2.4,
  ULT_RADIUS: 11,

  SPARK_CHANCE: 0.4,
  SPARK_CHARGE: 9,
  SPARK_SCORE: 50,
  SPARK_MAGNET: 3.2,

  /** Kills needed (without being hit) to reach each combo multiplier. */
  COMBO_STEPS: [
    { kills: 0, mult: 1 },
    { kills: 3, mult: 2 },
    { kills: 7, mult: 3 },
    { kills: 12, mult: 5 },
    { kills: 18, mult: 8 },
  ],
  SURVIVE_POINTS_PER_SEC: 10,
  COMPLETION_BONUS: 2500, // × level number

  MAX_ENEMIES: 64,
} as const;

/** Live arena state — set from the current level so every system clamps to the same edge and sees the same obstacles. */
export const world: { radius: number; obstacles: Obstacle[] } = { radius: 15, obstacles: [] };

export type EnemyKind = 'satyr' | 'harpy' | 'spartoi' | 'minotaur' | 'cyclops';
export const ENEMY_KINDS: EnemyKind[] = ['satyr', 'harpy', 'spartoi', 'minotaur', 'cyclops'];

export interface EnemyDef {
  name: string;
  hp: number;
  speed: number;
  radius: number;
  scale: number;
  score: number;
  charge: number;
  damage: number;
  /** Hover height in world units (harpies fly). */
  hover: number;
  /** Big creatures: more sparks, better wine odds, count toward the heavy cap. */
  heavy: boolean;
}

export const ENEMY_DEFS: Record<EnemyKind, EnemyDef> = {
  satyr: { name: 'Satyr', hp: 1, speed: 3.1, radius: 0.55, scale: 1, score: 100, charge: 5, damage: 1, hover: 0, heavy: false },
  harpy: { name: 'Harpy', hp: 1, speed: 5.4, radius: 0.45, scale: 0.85, score: 150, charge: 4, damage: 1, hover: 1.1, heavy: false },
  spartoi: { name: 'Spartoi', hp: 2, speed: 3.0, radius: 0.55, scale: 1, score: 200, charge: 7, damage: 1, hover: 0, heavy: false },
  minotaur: { name: 'Minotaur', hp: 4, speed: 2.3, radius: 0.95, scale: 1.45, score: 450, charge: 14, damage: 2, hover: 0, heavy: true },
  cyclops: { name: 'Cyclops', hp: 8, speed: 1.5, radius: 1.35, scale: 1.8, score: 1000, charge: 26, damage: 2, hover: 0, heavy: true },
};

export type Phase = 'warmup' | 'escalation' | 'chaos' | 'wrath';
/** Phase boundaries as a fraction of the run length (≈ 15 / 35 / 65 s of a 75 s run). */
export const PHASE_AT = { escalation: 0.2, chaos: 0.467, wrath: 0.867 };

export function phaseAt(t: number, total: number): Phase {
  const f = t / total;
  if (f < PHASE_AT.escalation) return 'warmup';
  if (f < PHASE_AT.chaos) return 'escalation';
  if (f < PHASE_AT.wrath) return 'chaos';
  return 'wrath';
}

/** 0..1 storm darkness for the atmosphere. */
export function stormAt(t: number, total: number): number {
  const x = t / total;
  return Math.min(1, Math.max(0, x * x * 0.85 + (x > PHASE_AT.wrath ? 0.15 : 0)));
}

/** 0..1 through the final "Wrath of Olympus" stretch. */
export function wrathAt(t: number, total: number): number {
  return Math.min(1, Math.max(0, (t / total - PHASE_AT.chaos) / (1 - PHASE_AT.chaos)));
}

export interface LevelTheme {
  skyDay: [number, number];
  skyStorm: [number, number];
  skyWrath: [number, number];
  floor: string;
  floorRing: string;
  floorAccent: string;
  mountains: number;
}

export type Weights = [number, number, number, number, number]; // satyr, harpy, spartoi, minotaur, cyclops

export type ObstacleStyle = 'none' | 'pillar' | 'statue' | 'rock' | 'spike' | 'block';

export interface Obstacle {
  x: number;
  z: number;
  r: number;
  style: ObstacleStyle;
}

/** A distinct environment: palette plus the kind of solid obstacles scattered across it. */
export interface MapDef {
  name: string;
  theme: LevelTheme;
  obstacle: ObstacleStyle;
  /** Obstacles per 5 units of radius (0 = open arena). */
  density: number;
  obstacleColor: number;
}

export interface Level {
  id: number;
  name: string;
  blurb: string;
  map: MapDef;
  radius: number;
  time: number;
  /** Spawn rate / density multiplier. */
  spawn: number;
  speed: number;
  scoreMul: number;
  /** Chance a kill drops a wine cup (health) — only while Zeus is hurt. 0 = none on early trials. */
  wine: number;
  heavyCap: number;
  braziers: number;
  difficulty: 1 | 2 | 3 | 4 | 5;
  /** Every 10th level: a heavier "Champion Trial". */
  boss: boolean;
  weights: Record<Phase, Weights>;
}

export const MAX_LEVEL = 100;

const T = (skyDay: [number, number], skyStorm: [number, number], skyWrath: [number, number], floor: string, floorRing: string, floorAccent: string, mountains: number): LevelTheme => ({
  skyDay, skyStorm, skyWrath, floor, floorRing, floorAccent, mountains,
});

/** Twelve maps. Levels 1–5 use the first five; after that they cycle (with a numeral: "Troy II"). */
export const MAPS: MapDef[] = [
  { name: 'Foothills of Olympus', obstacle: 'none', density: 0, obstacleColor: 0xe8e2d2, theme: T([0x2f78c8, 0xbfe0ff], [0x0d1226, 0x3a4468], [0x1a0f2e, 0x55406e], '#ece6d8', '#c99a3a', '#2e5f9e', 0x7d93b8) },
  { name: 'Temple of Athena', obstacle: 'pillar', density: 0.9, obstacleColor: 0xf0e6d0, theme: T([0xd9822b, 0xffe2a8], [0x2a1230, 0x7a4a58], [0x30102a, 0x8a4058], '#efe3c8', '#b8862a', '#7a3a2a', 0xb08a78) },
  { name: 'The Labyrinth', obstacle: 'block', density: 1.2, obstacleColor: 0x9a8e78, theme: T([0x4a5a6a, 0xb8c0c8], [0x10141c, 0x384250], [0x1c1018, 0x50384a], '#d8cdb4', '#8a7a5a', '#5a4a3a', 0x6a7480) },
  { name: "Poseidon's Wrath", obstacle: 'spike', density: 1.0, obstacleColor: 0x3aa8a0, theme: T([0x0f6a8a, 0x9fe0e8], [0x05202e, 0x1f5a6a], [0x0a1a30, 0x2a5a78], '#e2e8e2', '#4aa0a8', '#1f6a8a', 0x4a8aa0) },
  { name: 'Gates of Hades', obstacle: 'spike', density: 1.2, obstacleColor: 0x1c1418, theme: T([0x2a0a14, 0x8a2a2a], [0x0a0208, 0x3a1020], [0x200408, 0x6a1a20], '#bdb2a6', '#b02a20', '#6a1a1a', 0x4a2028) },
  { name: 'Elysian Fields', obstacle: 'statue', density: 0.8, obstacleColor: 0xdcdcc8, theme: T([0x3a8ac8, 0xd8f0c0], [0x0e2018, 0x3a5a48], [0x1a2a1a, 0x4a6a3a], '#e8ecd0', '#c9a43a', '#3a7a3a', 0x5a8a5a) },
  { name: 'Mount Ida', obstacle: 'rock', density: 1.1, obstacleColor: 0xb8c4d4, theme: T([0x6a9ad8, 0xe8f2ff], [0x1a2230, 0x5a6a80], [0x201a38, 0x6a608a], '#f2f6fa', '#8aa8c8', '#4a78a8', 0xb8c8dc) },
  { name: 'Tartarus', obstacle: 'spike', density: 1.4, obstacleColor: 0x4a2a6a, theme: T([0x1a0a2a, 0x5a2a6a], [0x07030e, 0x2a1238], [0x180420, 0x5a1a60], '#a89cb0', '#7a3aa8', '#4a1a6a', 0x3a2050) },
  { name: 'Delphi', obstacle: 'pillar', density: 0.9, obstacleColor: 0xe8d8c8, theme: T([0x7a4ab8, 0xffc8a0], [0x1a1030, 0x6a3a60], [0x2a0a30, 0x8a3a58], '#f0e0d0', '#c98a3a', '#8a3a6a', 0x9a6a88) },
  { name: 'Isles of the Blessed', obstacle: 'rock', density: 0.8, obstacleColor: 0xd8c8a8, theme: T([0x20a8c8, 0xffe0d0], [0x0a2a3a, 0x3a7a88], [0x102a40, 0x4a8a9a], '#f6f0e0', '#e0a050', '#20a0a8', 0x58b0a8) },
  { name: 'Troy', obstacle: 'block', density: 1.0, obstacleColor: 0xc8a878, theme: T([0xc87a3a, 0xf8d8a0], [0x2a1a10, 0x7a5030], [0x3a1408, 0x9a4a28], '#e0c8a0', '#a8782a', '#8a4a20', 0xa88050) },
  { name: 'Mycenae', obstacle: 'pillar', density: 1.1, obstacleColor: 0xd8c898, theme: T([0x3a5a9a, 0xd0c8b0], [0x10141c, 0x4a4a58], [0x201418, 0x6a4448], '#d8cca8', '#b88a30', '#7a2a2a', 0x8a7a68) },
];

/** Hand-tuned values for the first five levels; everything after eases toward the caps. */
const EARLY = [
  { radius: 15, time: 60, spawn: 0.85, speed: 0.92, score: 1, wine: 0, heavy: 0, brazier: 4 },
  { radius: 18, time: 70, spawn: 1, speed: 1, score: 1.25, wine: 0, heavy: 0, brazier: 4 },
  { radius: 21, time: 75, spawn: 1.15, speed: 1.05, score: 1.5, wine: 0.06, heavy: 3, brazier: 6 },
  { radius: 24, time: 80, spawn: 1.3, speed: 1.1, score: 1.75, wine: 0.08, heavy: 4, brazier: 6 },
  { radius: 27, time: 90, spawn: 1.5, speed: 1.18, score: 2, wine: 0.1, heavy: 6, brazier: 8 },
];

const W = (a: number, b: number, c: number, d: number, e: number): Weights => [a, b, c, d, e];
const EARLY_WEIGHTS: Record<Phase, Weights>[] = [
  { warmup: W(1, 0, 0, 0, 0), escalation: W(0.8, 0.2, 0, 0, 0), chaos: W(0.6, 0.4, 0, 0, 0), wrath: W(0.55, 0.45, 0, 0, 0) },
  { warmup: W(0.8, 0.2, 0, 0, 0), escalation: W(0.5, 0.2, 0.3, 0, 0), chaos: W(0.35, 0.3, 0.35, 0, 0), wrath: W(0.3, 0.35, 0.35, 0, 0) },
  { warmup: W(0.7, 0.15, 0.15, 0, 0), escalation: W(0.4, 0.2, 0.3, 0.1, 0), chaos: W(0.28, 0.25, 0.3, 0.17, 0), wrath: W(0.25, 0.3, 0.28, 0.17, 0) },
  { warmup: W(0.55, 0.2, 0.25, 0, 0), escalation: W(0.32, 0.25, 0.28, 0.1, 0.05), chaos: W(0.22, 0.25, 0.26, 0.15, 0.12), wrath: W(0.2, 0.28, 0.25, 0.15, 0.12) },
  { warmup: W(0.4, 0.2, 0.3, 0.1, 0), escalation: W(0.25, 0.25, 0.25, 0.15, 0.1), chaos: W(0.18, 0.25, 0.22, 0.18, 0.17), wrath: W(0.15, 0.27, 0.2, 0.2, 0.18) },
];
/** Where the rosters end up by level 100: mostly heavies. */
const LATE_WEIGHTS: Record<Phase, Weights> = {
  warmup: W(0.2, 0.2, 0.25, 0.2, 0.15),
  escalation: W(0.12, 0.22, 0.2, 0.24, 0.22),
  chaos: W(0.08, 0.2, 0.16, 0.26, 0.3),
  wrath: W(0.06, 0.2, 0.14, 0.26, 0.34),
};
const BOSS_WEIGHTS: Weights = W(0.04, 0.1, 0.14, 0.32, 0.4);
const PHASES_LIST: Phase[] = ['warmup', 'escalation', 'chaos', 'wrath'];

const ROMAN = ['', ' II', ' III', ' IV', ' V', ' VI', ' VII', ' VIII', ' IX', ' X'];
const lerp = (a: number, b: number, k: number) => a + (b - a) * Math.min(1, Math.max(0, k));
const cache = new Map<number, Level>();

/** Levels 1–100. The first five are hand-tuned; the rest ramp smoothly toward the caps at level 100. */
export function levelById(id: number): Level {
  const n = Math.min(MAX_LEVEL, Math.max(1, Math.floor(id) || 1));
  const hit = cache.get(n);
  if (hit) return hit;
  const e = n <= 5 ? EARLY[n - 1] : null;
  const u = Math.max(0, n - 5) / (MAX_LEVEL - 5);
  const boss = n % 10 === 0;
  const map = MAPS[(n - 1) % MAPS.length];
  const cycle = Math.floor((n - 1) / MAPS.length);
  const weights = {} as Record<Phase, Weights>;
  for (const ph of PHASES_LIST) {
    const base = n <= 5 ? EARLY_WEIGHTS[n - 1][ph] : (EARLY_WEIGHTS[4][ph].map((v, i) => lerp(v, LATE_WEIGHTS[ph][i], u)) as Weights);
    const mix = boss && ph !== 'warmup' ? (base.map((v, i) => lerp(v, BOSS_WEIGHTS[i], 0.55)) as Weights) : base;
    const sum = mix.reduce((a, b) => a + b, 0);
    weights[ph] = mix.map((v) => v / sum) as Weights;
  }
  const L: Level = {
    id: n,
    name: `${map.name}${ROMAN[Math.min(cycle, ROMAN.length - 1)]}`,
    blurb: boss ? 'A Champion Trial: the strongest creatures guard this arena.' : 'Survive the trial.',
    map,
    radius: e ? e.radius : Math.min(44, Math.round(27 + (n - 5) * 0.18)),
    time: e ? e.time : Math.min(120, Math.round(90 + (n - 5) * 0.3)),
    spawn: (e ? e.spawn : 1.5 + (n - 5) * 0.018) * (boss ? 1.15 : 1),
    speed: e ? e.speed : 1.18 + (n - 5) * 0.0044,
    scoreMul: (e ? e.score : 2 + (n - 5) * 0.1) * (boss ? 1.5 : 1),
    wine: e ? e.wine : 0.1,
    heavyCap: (e ? e.heavy : 6 + Math.floor((n - 5) / 12)) + (boss ? 4 : 0),
    braziers: e ? e.brazier : Math.min(12, 8 + Math.floor((n - 5) / 20)),
    difficulty: Math.min(5, Math.max(1, Math.ceil(n / 20))) as 1 | 2 | 3 | 4 | 5,
    boss,
    weights,
  };
  cache.set(n, L);
  return L;
}

/** Deterministic solid obstacles for a level: same layout every time you play it. */
export function buildObstacles(level: Level): Obstacle[] {
  const m = level.map;
  if (m.obstacle === 'none' || m.density <= 0) return [];
  const rng = new Rng(hashString(`obstacles:${level.id}`));
  const count = Math.min(16, Math.round((m.density * level.radius) / 5) + Math.floor(level.id / 25));
  const out: Obstacle[] = [];
  let tries = 0;
  while (out.length < count && tries++ < 400) {
    const a = rng.next() * Math.PI * 2;
    const r = level.radius * (0.3 + rng.next() * 0.55);
    const size = m.obstacle === 'block' ? 1.1 : m.obstacle === 'rock' ? 0.9 + rng.next() * 0.5 : 0.85;
    const x = Math.cos(a) * r;
    const z = Math.sin(a) * r;
    if (Math.hypot(x, z) < 6) continue; // keep the centre clear: Zeus starts there
    if (out.some((o) => Math.hypot(o.x - x, o.z - z) < o.r + size + 2.4)) continue;
    out.push({ x, z, r: size, style: m.obstacle });
  }
  return out;
}

export const LEVEL_COUNT = MAX_LEVEL;
