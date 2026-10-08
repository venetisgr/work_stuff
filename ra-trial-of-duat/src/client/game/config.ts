/** Tunables for the whole game. Everything is in world units / seconds. */
export const CFG = {
  RUN_TIME: 75,
  PHASES: { warmup: 15, escalation: 35, chaos: 65 }, // wrath = 65..75
  ARENA_RADIUS: 15,
  PLAYER_RADIUS: 0.5,
  PLAYER_SPEED: 8.2,
  PLAYER_HEIGHT: 2.7,
  /** Procedural deity models face +Z, which is what atan2(x, z) assumes. */
  MODEL_YAW_OFFSET: 0,
  /** Default HP; each deity overrides it. */
  MAX_HP: 6,
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
  COMPLETION_BONUS: 2500,

  MAX_ENEMIES: 56,
} as const;

export type EnemyKind = 'mummy' | 'scarab' | 'colossus';

export interface EnemyDef {
  hp: number;
  speed: number;
  radius: number;
  scale: number;
  score: number;
  charge: number;
  damage: number;
}

export const ENEMY_DEFS: Record<EnemyKind, EnemyDef> = {
  mummy: { hp: 1, speed: 3.1, radius: 0.55, scale: 0.9, score: 100, charge: 5, damage: 1 },
  scarab: { hp: 1, speed: 5.6, radius: 0.42, scale: 0.78, score: 150, charge: 4, damage: 1 },
  colossus: { hp: 3, speed: 1.9, radius: 1.0, scale: 1.45, score: 400, charge: 14, damage: 2 },
};

/** The playable gods. Each one tweaks the same core loop. */
export type DeityId = 'ra' | 'horus' | 'anubis';

export interface DeityDef {
  id: DeityId;
  name: string;
  title: string;
  blurb: string;
  /** Name of the Divine Charge ultimate. */
  ultName: string;
  maxHp: number;
  speed: number;
  attackCooldown: number;
  boltRadius: number;
  attackRange: number;
  /** Multiplier on Divine Charge gained from kills and Ankh sparks. */
  chargeGain: number;
  /** Free chain-beam arcs added on top of the combo-tier chains. */
  extraChains: number;
  /** Beam colour per combo tier (index = tier + 1; index 0 is the uncharged shot). */
  beam: readonly number[];
  /** CSS accent colour for the HUD (hex string). */
  accent: string;
  /** Model colours. */
  skin: number;
  cloth: number;
  trim: number;
  /** Colour of the hand/staff glow. */
  glow: number;
  /** [r, g, b] 0..255 used for screen flashes. */
  flash: string;
}

export const DEITIES: Record<DeityId, DeityDef> = {
  ra: {
    id: 'ra',
    name: 'RA',
    title: 'Sun God',
    blurb: 'Balanced. Every beam scorches an extra enemy.',
    ultName: 'EYE OF RA',
    maxHp: 5,
    speed: 8.2,
    attackCooldown: 0.3,
    boltRadius: 1.5,
    attackRange: 22,
    chargeGain: 1,
    extraChains: 1,
    beam: [0xffe9a8, 0xffe9a8, 0xffd36a, 0xffb23a, 0xff8a1a, 0xffffff],
    accent: '#ffb23a',
    skin: 0xb9693a,
    cloth: 0xf4ecd8,
    trim: 0xe8b84a,
    glow: 0xffc24a,
    flash: '255,210,120',
  },
  horus: {
    id: 'horus',
    name: 'HORUS',
    title: 'Falcon of the Sky',
    blurb: 'Fast and far-sighted. Quick strikes, long reach, fragile.',
    ultName: 'WINGS OF HORUS',
    maxHp: 4,
    speed: 9.4,
    attackCooldown: 0.23,
    boltRadius: 1.25,
    attackRange: 27,
    chargeGain: 1,
    extraChains: 0,
    beam: [0xd8f0ff, 0xd8f0ff, 0x9fd8ff, 0x6ab8ff, 0xffe08a, 0xffffff],
    accent: '#5fb4ff',
    skin: 0xa85a32,
    cloth: 0xf1ede2,
    trim: 0x2f6fd0,
    glow: 0x8fd0ff,
    flash: '170,215,255',
  },
  anubis: {
    id: 'anubis',
    name: 'ANUBIS',
    title: 'Judge of the Dead',
    blurb: 'Tough and wide-reaching. Souls fill his charge faster.',
    ultName: 'WEIGHING OF THE HEART',
    maxHp: 6,
    speed: 7.6,
    attackCooldown: 0.36,
    boltRadius: 1.95,
    attackRange: 20,
    chargeGain: 1.4,
    extraChains: 0,
    beam: [0xd6ffe6, 0xd6ffe6, 0x9affc4, 0x6aeaa0, 0xb48aff, 0xffffff],
    accent: '#5cf0a8',
    skin: 0x23242c,
    cloth: 0xe9e2d0,
    trim: 0xe8b84a,
    glow: 0x6affb0,
    flash: '150,255,200',
  },
};
export const DEITY_ORDER: DeityId[] = ['ra', 'horus', 'anubis'];

export type Phase = 'warmup' | 'escalation' | 'chaos' | 'wrath';
export function phaseAt(t: number): Phase {
  if (t < CFG.PHASES.warmup) return 'warmup';
  if (t < CFG.PHASES.escalation) return 'escalation';
  if (t < CFG.PHASES.chaos) return 'chaos';
  return 'wrath';
}

/** 0..1 darkness of Apep's eclipse for the atmosphere. */
export function stormAt(t: number): number {
  const x = t / CFG.RUN_TIME;
  return Math.min(1, Math.max(0, x * x * 0.85 + (t > CFG.PHASES.chaos ? 0.15 : 0)));
}
