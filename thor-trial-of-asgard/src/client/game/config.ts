/** Tunables for the whole game. Everything is in world units / seconds. */
export const CFG = {
  RUN_TIME: 75,
  PHASES: { warmup: 15, escalation: 35, chaos: 65 }, // wrath = 65..75
  ARENA_RADIUS: 15,
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
  COMPLETION_BONUS: 2500,

  MAX_ENEMIES: 56,
} as const;

export type EnemyKind = 'shade' | 'runner' | 'brute';

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
  shade: { hp: 1, speed: 3.1, radius: 0.55, scale: 0.9, score: 100, charge: 5, damage: 1 },
  runner: { hp: 1, speed: 5.6, radius: 0.42, scale: 0.78, score: 150, charge: 4, damage: 1 },
  brute: { hp: 3, speed: 1.9, radius: 1.0, scale: 1.45, score: 400, charge: 14, damage: 2 },
};

export type Phase = 'warmup' | 'escalation' | 'chaos' | 'wrath';
export function phaseAt(t: number): Phase {
  if (t < CFG.PHASES.warmup) return 'warmup';
  if (t < CFG.PHASES.escalation) return 'escalation';
  if (t < CFG.PHASES.chaos) return 'chaos';
  return 'wrath';
}

/** 0..1 storm darkness for the atmosphere. */
export function stormAt(t: number): number {
  const x = t / CFG.RUN_TIME;
  return Math.min(1, Math.max(0, x * x * 0.85 + (t > CFG.PHASES.chaos ? 0.15 : 0)));
}
