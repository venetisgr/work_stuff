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

/** Live arena size — set from the current level so every system clamps to the same edge. */
export const world = { radius: 15 };

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

export interface Level {
  id: number;
  name: string;
  blurb: string;
  radius: number;
  time: number;
  /** Spawn rate / density multiplier. */
  spawn: number;
  speed: number;
  scoreMul: number;
  /** Chance a kill drops a wine cup (health) — only while Zeus is hurt. 0 = none on easy trials. */
  wine: number;
  heavyCap: number;
  braziers: number;
  difficulty: 1 | 2 | 3 | 4 | 5;
  weights: Record<Phase, Weights>;
  theme: LevelTheme;
}

export const LEVELS: Level[] = [
  {
    id: 1,
    name: 'Foothills of Olympus',
    blurb: 'Satyrs and harpies test the new god.',
    radius: 15,
    time: 60,
    spawn: 0.85,
    speed: 0.92,
    scoreMul: 1,
    wine: 0,
    heavyCap: 0,
    braziers: 4,
    difficulty: 1,
    weights: { warmup: [1, 0, 0, 0, 0], escalation: [0.8, 0.2, 0, 0, 0], chaos: [0.6, 0.4, 0, 0, 0], wrath: [0.55, 0.45, 0, 0, 0] },
    theme: { skyDay: [0x2f78c8, 0xbfe0ff], skyStorm: [0x0d1226, 0x3a4468], skyWrath: [0x1a0f2e, 0x55406e], floor: '#ece6d8', floorRing: '#c99a3a', floorAccent: '#2e5f9e', mountains: 0x7d93b8 },
  },
  {
    id: 2,
    name: 'Temple of Athena',
    blurb: 'Bone warriors rise from the colonnades.',
    radius: 18,
    time: 70,
    spawn: 1,
    speed: 1,
    scoreMul: 1.25,
    wine: 0,
    heavyCap: 0,
    braziers: 4,
    difficulty: 2,
    weights: { warmup: [0.8, 0.2, 0, 0, 0], escalation: [0.5, 0.2, 0.3, 0, 0], chaos: [0.35, 0.3, 0.35, 0, 0], wrath: [0.3, 0.35, 0.35, 0, 0] },
    theme: { skyDay: [0xd9822b, 0xffe2a8], skyStorm: [0x2a1230, 0x7a4a58], skyWrath: [0x30102a, 0x8a4058], floor: '#efe3c8', floorRing: '#b8862a', floorAccent: '#7a3a2a', mountains: 0xb08a78 },
  },
  {
    id: 3,
    name: 'The Labyrinth',
    blurb: 'The Minotaur charges. Wine cups restore health.',
    radius: 21,
    time: 75,
    spawn: 1.15,
    speed: 1.05,
    scoreMul: 1.5,
    wine: 0.06,
    heavyCap: 3,
    braziers: 6,
    difficulty: 3,
    weights: { warmup: [0.7, 0.15, 0.15, 0, 0], escalation: [0.4, 0.2, 0.3, 0.1, 0], chaos: [0.28, 0.25, 0.3, 0.17, 0], wrath: [0.25, 0.3, 0.28, 0.17, 0] },
    theme: { skyDay: [0x4a5a6a, 0xb8c0c8], skyStorm: [0x10141c, 0x384250], skyWrath: [0x1c1018, 0x50384a], floor: '#d8cdb4', floorRing: '#8a7a5a', floorAccent: '#5a4a3a', mountains: 0x6a7480 },
  },
  {
    id: 4,
    name: "Poseidon's Wrath",
    blurb: 'A Cyclops hurls the sea at you.',
    radius: 24,
    time: 80,
    spawn: 1.3,
    speed: 1.1,
    scoreMul: 1.75,
    wine: 0.08,
    heavyCap: 4,
    braziers: 6,
    difficulty: 4,
    weights: { warmup: [0.55, 0.2, 0.25, 0, 0], escalation: [0.32, 0.25, 0.28, 0.1, 0.05], chaos: [0.22, 0.25, 0.26, 0.15, 0.12], wrath: [0.2, 0.28, 0.25, 0.15, 0.12] },
    theme: { skyDay: [0x0f6a8a, 0x9fe0e8], skyStorm: [0x05202e, 0x1f5a6a], skyWrath: [0x0a1a30, 0x2a5a78], floor: '#e2e8e2', floorRing: '#4aa0a8', floorAccent: '#1f6a8a', mountains: 0x4a8aa0 },
  },
  {
    id: 5,
    name: 'Gates of Hades',
    blurb: 'Everything the underworld has. No mercy.',
    radius: 27,
    time: 90,
    spawn: 1.5,
    speed: 1.18,
    scoreMul: 2,
    wine: 0.1,
    heavyCap: 6,
    braziers: 8,
    difficulty: 5,
    weights: { warmup: [0.4, 0.2, 0.3, 0.1, 0], escalation: [0.25, 0.25, 0.25, 0.15, 0.1], chaos: [0.18, 0.25, 0.22, 0.18, 0.17], wrath: [0.15, 0.27, 0.2, 0.2, 0.18] },
    theme: { skyDay: [0x2a0a14, 0x8a2a2a], skyStorm: [0x0a0208, 0x3a1020], skyWrath: [0x200408, 0x6a1a20], floor: '#bdb2a6', floorRing: '#b02a20', floorAccent: '#6a1a1a', mountains: 0x4a2028 },
  },
];

const MYTH_PLACES = ['Elysian Fields', 'Mount Ida', 'Tartarus', 'Isles of the Blessed', 'Delphi', 'Thebes', 'Troy', 'Ithaca', 'Sparta', 'Crete', 'Marathon', 'Arcadia', 'Mycenae', 'Corinth'];

/** Hand-made levels 1–5, then an endless ramp: bigger arenas, denser and faster hordes, heavier rosters. */
export function levelById(id: number): Level {
  const n = Math.max(1, Math.floor(id));
  if (n <= LEVELS.length) return LEVELS[n - 1];
  const k = n - LEVELS.length;
  const base = LEVELS[(n - 1) % LEVELS.length];
  const heavy = Math.min(0.5, 0.36 + k * 0.01);
  const w = (a: number, b: number, c: number, d: number, e: number): Weights => [a, b, c, d, e];
  return {
    id: n,
    name: `${MYTH_PLACES[(k - 1) % MYTH_PLACES.length]}`,
    blurb: 'An endless trial. Only the strongest survive.',
    radius: Math.min(44, Math.round(27 + k * 2.2)),
    time: Math.min(120, 90 + k * 3),
    spawn: Math.min(3.2, 1.5 + k * 0.1),
    speed: Math.min(1.6, 1.18 + k * 0.02),
    scoreMul: 2 + k * 0.25,
    wine: 0.1,
    heavyCap: Math.min(14, 6 + k),
    braziers: Math.min(12, 8 + Math.floor(k / 2)),
    difficulty: 5,
    weights: {
      warmup: w(0.35, 0.2, 0.3, 0.1, 0.05),
      escalation: w(0.22, 0.24, 0.24, 0.15, 0.15),
      chaos: w(0.14, 0.24, 0.2, heavy / 2 + 0.05, heavy / 2 + 0.05 + 0.03),
      wrath: w(0.12, 0.26, 0.18, heavy / 2 + 0.05, heavy / 2 + 0.07),
    },
    theme: base.theme,
  };
}
