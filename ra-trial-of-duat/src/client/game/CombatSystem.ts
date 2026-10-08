import * as THREE from 'three';
import { CFG, ENEMY_DEFS } from './config';
import type { Arena } from './Arena';
import type { GameAudio } from './Audio';
import type { Effects } from './Effects';
import type { Enemy, EnemyManager } from './Enemies';
import type { Pickups } from './Pickups';
import type { Player } from './Player';

export interface RunStats {
  score: number;
  kills: number;
  streak: number;
  maxCombo: number;
  multiKillBest: number;
  sparks: number;
}

export interface CombatEvents {
  popup(x: number, y: number, z: number, text: string, kind: 'score' | 'spark' | 'multi'): void;
  comboTier(mult: number): void;
  ultReady(): void;
  playerHit(): void;
}

/** Scoring, combo, Divine Charge, divine-beam attack resolution, ultimate and Ankh sparks. */
export class CombatSystem {
  stats: RunStats = { score: 0, kills: 0, streak: 0, maxCombo: 1, multiKillBest: 0, sparks: 0 };
  ultActive = false;
  /** True while the end-of-run barrage is clearing the arena (flat score, no combo bonuses). */
  private finaleMode = false;

  private ultT = 0;
  private ultStrikeTimer = 0;
  private ultSlam1 = false;
  private ultSlam2 = false;
  private multiCount = 0;
  private multiWindow = 0;
  private wasReady = false;
  private time = 0;
  private v1 = new THREE.Vector3();
  private v2 = new THREE.Vector3();

  constructor(
    private player: Player,
    private enemies: EnemyManager,
    private pickups: Pickups,
    private fx: Effects,
    private arena: Arena,
    private audio: GameAudio,
    private events: CombatEvents,
  ) {
    enemies.onKill = (e) => this.handleKill(e);
    enemies.onPlayerHit = () => this.handlePlayerHit();
  }

  reset(): void {
    this.stats = { score: 0, kills: 0, streak: 0, maxCombo: 1, multiKillBest: 0, sparks: 0 };
    this.ultActive = false;
    this.ultT = 0;
    this.multiCount = 0;
    this.multiWindow = 0;
    this.wasReady = false;
  }

  get multiplier(): number {
    const steps = CFG.COMBO_STEPS;
    let m = 1;
    for (const s of steps) if (this.stats.streak >= s.kills) m = s.mult;
    return m;
  }

  /** 0..4 → drives bolt thickness, colour and shake. */
  get tier(): number {
    const m = this.multiplier;
    return m >= 8 ? 4 : m >= 5 ? 3 : m >= 3 ? 2 : m >= 2 ? 1 : 0;
  }

  private addScore(base: number): number {
    const pts = Math.round(base * this.multiplier);
    this.stats.score += pts;
    return pts;
  }

  private handlePlayerHit(): void {
    this.stats.streak = 0;
    this.multiCount = 0;
    this.audio.play('hit');
    this.fx.addShake(0.5);
    this.fx.screenFlash(0.35, '255,70,50');
    this.events.playerHit();
  }

  private handleKill(e: Enemy): void {
    const def = ENEMY_DEFS[e.kind];
    if (this.finaleMode) {
      this.stats.kills++;
      this.stats.score += def.score;
      return;
    }
    const prevMult = this.multiplier;
    this.stats.kills++;
    this.stats.streak++;
    const mult = this.multiplier;
    this.stats.maxCombo = Math.max(this.stats.maxCombo, mult);
    const pts = this.addScore(def.score);
    this.events.popup(e.x, 1.8 * e.scale, e.z, `+${pts}`, 'score');
    if (mult > prevMult) {
      this.audio.play('combo', 1 + this.tier * 0.15);
      this.events.comboTier(mult);
      this.fx.screenFlash(0.18, '255,230,150');
    }
    this.audio.play('kill');
    if (!this.ultActive) this.gainCharge(def.charge * this.player.deity.chargeGain);
    // multi-kill: 3+ kills inside a short window
    this.multiCount++;
    this.multiWindow = 0.55;
    if (this.multiCount >= 3) {
      // flat, capped bonus: rewards a burst of kills without snowballing
      const bonus = 50 * Math.min(this.multiCount, 8);
      this.stats.score += bonus;
      this.stats.multiKillBest = Math.max(this.stats.multiKillBest, this.multiCount);
      this.events.popup(e.x, 2.8, e.z, `x${this.multiCount} MULTI +${bonus}`, 'multi');
    }
    const drops = e.kind === 'colossus' ? 3 : Math.random() < CFG.SPARK_CHANCE ? 1 : 0;
    for (let i = 0; i < drops; i++) this.pickups.drop(e.x, e.z);
  }

  private gainCharge(amount: number): void {
    const p = this.player;
    p.charge = Math.min(100, p.charge + amount);
    if (p.charge >= 100 && !this.wasReady) {
      this.wasReady = true;
      this.events.ultReady();
      this.audio.play('combo', 1.6);
    }
  }

  /** Called by Player when the cast animation releases the beam. */
  cast(dir: THREE.Vector3): void {
    const p = this.player;
    const tier = this.tier;
    const charged = p.charge >= CFG.ATTACK_COST;
    if (charged) {
      p.charge -= CFG.ATTACK_COST;
      if (p.charge < 100) this.wasReady = false;
    }
    const origin = p.pos;
    // aim assist: best enemy inside a cone around `dir`
    const cos = Math.cos((CFG.AIM_ASSIST_DEG * Math.PI) / 180);
    let best: Enemy | null = null;
    let bestScore = -Infinity;
    for (const e of this.enemies.active) {
      if (e.state !== 'alive') continue;
      const dx = e.x - origin.x;
      const dz = e.z - origin.z;
      const d = Math.hypot(dx, dz);
      if (d > this.player.deity.attackRange || d < 0.01) continue;
      const dot = (dx * dir.x + dz * dir.z) / d;
      if (dot < cos) continue;
      const sc = dot * 6 - d * 0.35;
      if (sc > bestScore) {
        bestScore = sc;
        best = e;
      }
    }
    let tx: number;
    let tz: number;
    if (best) {
      tx = best.x;
      tz = best.z;
    } else {
      const reach = 7;
      tx = origin.x + dir.x * reach;
      tz = origin.z + dir.z * reach;
      const r = Math.hypot(tx, tz);
      const maxR = CFG.ARENA_RADIUS - 0.5;
      if (r > maxR) {
        tx *= maxR / r;
        tz *= maxR / r;
      }
    }
    const radius = charged ? this.player.deity.boltRadius + tier * 0.12 : 0.9;
    const col = this.player.deity.beam[tier + 1];
    const power = 0.25 + tier * 0.17;
    // hand → target crackle + sky strike
    p.handWorld(this.v1);
    this.v2.set(tx, 1.0, tz);
    this.fx.bolt(this.v1, this.v2, power * 0.6, 0.18, col);
    this.fx.strike(tx, tz, power, col);
    this.arena.strikeLight.position.set(tx, 4, tz);
    this.arena.strikeLight.intensity = 90 + tier * 30;
    this.arena.flash(0.12 + tier * 0.05);
    this.fx.addShake(0.08 + tier * 0.07);
    this.fx.screenFlash(0.05 + tier * 0.03);
    this.audio.play('zap');
    this.audio.play('impact', 0.5 + tier * 0.15);

    const hitList = this.damageArea(tx, tz, radius, 1, 8);
    // chain beams, scaling with combo tier
    const chains = charged ? [0, 1, 2, 3, 4][tier] + this.player.deity.extraChains : 0;
    let fromX = tx;
    let fromZ = tz;
    const chained = new Set<Enemy>(hitList);
    for (let c = 0; c < chains; c++) {
      let next: Enemy | null = null;
      let nd = 6.5 * 6.5;
      for (const e of this.enemies.active) {
        if (e.state !== 'alive' || chained.has(e)) continue;
        const d2 = (e.x - fromX) ** 2 + (e.z - fromZ) ** 2;
        if (d2 < nd) {
          nd = d2;
          next = e;
        }
      }
      if (!next) break;
      chained.add(next);
      this.v1.set(fromX, 1.2, fromZ);
      this.v2.set(next.x, 1.2, next.z);
      this.fx.bolt(this.v1, this.v2, power * 0.5, 0.22, col);
      this.fx.impact(next.x, next.z, 0.2, col);
      this.enemies.hit(next, 1, (next.x - fromX) * 0.1, (next.z - fromZ) * 0.1, 5);
      fromX = next.x;
      fromZ = next.z;
    }
    this.audio.play('thunder', 0.3);
  }

  private damageArea(x: number, z: number, radius: number, dmg: number, force: number): Enemy[] {
    const out: Enemy[] = [];
    for (let i = this.enemies.active.length - 1; i >= 0; i--) {
      const e = this.enemies.active[i];
      if (e.state !== 'alive') continue;
      const dx = e.x - x;
      const dz = e.z - z;
      const d = Math.hypot(dx, dz);
      if (d < radius + e.radius) {
        out.push(e);
        this.enemies.hit(e, dmg, dx / (d || 1), dz / (d || 1), force);
      }
    }
    return out;
  }

  startUltimate(): boolean {
    if (!this.player.startUltimate()) return false;
    this.ultActive = true;
    this.ultT = 0;
    this.ultStrikeTimer = 0.5;
    this.ultSlam1 = this.ultSlam2 = false;
    this.wasReady = false;
    this.audio.play('ultimate');
    this.fx.addShake(0.6);
    this.fx.screenFlash(0.5, this.player.deity.flash);
    this.fx.ring(this.player.pos.x, this.player.pos.z, 5, 0.8, this.player.deity.beam[2]);
    // heal a pip as reward for landing the ultimate
    this.player.hp = Math.min(this.player.deity.maxHp, this.player.hp + 1);
    return true;
  }

  /** Random strike inside the storm radius, preferring enemies. */
  private ultStrike(): void {
    const p = this.player.pos;
    let tx: number;
    let tz: number;
    const pool = this.enemies.active.filter((e) => e.state === 'alive' && Math.hypot(e.x - p.x, e.z - p.z) < CFG.ULT_RADIUS + 2);
    if (pool.length && Math.random() < 0.8) {
      const e = pool[(Math.random() * pool.length) | 0];
      tx = e.x;
      tz = e.z;
    } else {
      const a = Math.random() * Math.PI * 2;
      const r = 3 + Math.random() * (CFG.ULT_RADIUS - 3);
      tx = p.x + Math.cos(a) * r;
      tz = p.z + Math.sin(a) * r;
    }
    const b = this.player.deity.beam;
    const col = Math.random() < 0.3 ? b[4] : b[2];
    this.fx.strike(tx, tz, 0.55, col);
    this.fx.addShake(0.1);
    this.damageArea(tx, tz, 2.1, 3, 10);
  }

  private slam(radius: number, big: boolean): void {
    const p = this.player.pos;
    const b = this.player.deity.beam;
    this.fx.ring(p.x, p.z, radius, 0.7, b[4]);
    this.fx.ring(p.x, p.z, radius * 0.6, 0.55, b[1]);
    this.fx.addShake(big ? 1 : 0.7);
    this.fx.screenFlash(big ? 0.8 : 0.45, this.player.deity.flash);
    this.arena.flash(big ? 1 : 0.6);
    this.arena.strikeLight.position.set(p.x, 5, p.z);
    this.arena.strikeLight.intensity = 260;
    this.audio.play('thunder', 1.4);
    this.audio.play('impact', 1.4);
    for (let i = 0; i < 10; i++) {
      const a = (i / 10) * Math.PI * 2 + Math.random() * 0.3;
      const r = radius * (0.5 + Math.random() * 0.5);
      this.fx.strike(p.x + Math.cos(a) * r, p.z + Math.sin(a) * r, 0.7, b[2]);
    }
    this.damageArea(p.x, p.z, radius, 99, 14);
  }

  /** Final divine barrage when the timer ends. Returns kills. */
  finale(): void {
    const b = this.player.deity.beam;
    for (let i = 0; i < 12; i++) {
      const a = Math.random() * Math.PI * 2;
      const r = Math.random() * 13;
      this.fx.strike(Math.cos(a) * r, Math.sin(a) * r, 0.8, i % 3 === 0 ? b[4] : b[2]);
    }
    this.fx.addShake(1);
    this.fx.screenFlash(0.9, this.player.deity.flash);
    this.arena.flash(1);
    this.audio.play('thunder', 1.6);
    this.audio.play('victory');
    this.finaleMode = true;
    this.enemies.killAll();
    this.finaleMode = false;
  }

  update(dt: number, running: boolean): void {
    this.time += dt;
    if (this.multiWindow > 0) {
      this.multiWindow -= dt;
      if (this.multiWindow <= 0) this.multiCount = 0;
    }
    if (this.ultActive) {
      this.ultT += dt;
      if (this.ultT > 0.55 && !this.ultSlam1) {
        this.ultSlam1 = true;
        this.slam(6, false);
      }
      if (this.ultT > 0.55 && this.ultT < CFG.ULT_DURATION - 0.4) {
        this.ultStrikeTimer -= dt;
        while (this.ultStrikeTimer <= 0) {
          this.ultStrikeTimer += 0.08;
          this.ultStrike();
        }
      }
      if (this.ultT > 1.6 && !this.ultSlam2) {
        this.ultSlam2 = true;
        this.slam(CFG.ULT_RADIUS, true);
      }
      if (this.ultT >= CFG.ULT_DURATION) this.ultActive = false;
    }
    const collected = this.pickups.update(dt, this.player.pos.x, this.player.pos.z, running && this.player.alive, (x, z) => {
      this.stats.sparks++;
      this.stats.score += CFG.SPARK_SCORE;
      this.events.popup(x, 1.6, z, `+${CFG.SPARK_SCORE}`, 'spark');
      this.fx.burst(x, 1, z, 8, 0xffe08a, 3, 0.4, 0.4);
      this.audio.play('spark');
      if (!this.ultActive) this.gainCharge(CFG.SPARK_CHARGE * this.player.deity.chargeGain);
    });
    void collected;
  }
}
