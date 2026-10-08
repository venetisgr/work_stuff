import * as THREE from 'three';
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js';
import { CFG, ENEMY_DEFS, ENEMY_KINDS, phaseAt, world, type EnemyKind, type Level } from './config';
import type { Effects } from './Effects';
import type { Player } from './Player';
import { Rng } from './rng';

export type EnemyState = 'rising' | 'alive' | 'dying';

export interface Enemy {
  kind: EnemyKind;
  state: EnemyState;
  x: number;
  z: number;
  vx: number;
  vz: number;
  hp: number;
  radius: number;
  scale: number;
  t: number;
  hitFlash: number;
  phase: number;
  yaw: number;
  attackCd: number;
  /** Minotaur charge: seconds until next charge, telegraph and dash timers. */
  aiT: number;
  windup: number;
  dash: number;
  dashX: number;
  dashZ: number;
}

const MAX_PER_KIND = CFG.MAX_ENEMIES;

/* ---------- procedural creature geometry (vertex-coloured, merged) ---------- */

function colorize(geo: THREE.BufferGeometry, hex: number): THREE.BufferGeometry {
  const c = new THREE.Color(hex);
  const n = geo.attributes.position.count;
  const arr = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) {
    arr[i * 3] = c.r;
    arr[i * 3 + 1] = c.g;
    arr[i * 3 + 2] = c.b;
  }
  geo.setAttribute('color', new THREE.BufferAttribute(arr, 3));
  return geo;
}

type Part = { g: THREE.BufferGeometry; x?: number; y?: number; z?: number; rx?: number; ry?: number; rz?: number; sx?: number; sy?: number; sz?: number };

class Builder {
  private parts: THREE.BufferGeometry[] = [];
  add(hex: number, p: Part): this {
    const g = p.g;
    if (p.sx !== undefined || p.sy !== undefined || p.sz !== undefined) g.scale(p.sx ?? 1, p.sy ?? 1, p.sz ?? 1);
    if (p.rx) g.rotateX(p.rx);
    if (p.ry) g.rotateY(p.ry);
    if (p.rz) g.rotateZ(p.rz);
    g.translate(p.x ?? 0, p.y ?? 0, p.z ?? 0);
    const ni = g.index ? g.toNonIndexed() : g;
    if (!ni.attributes.uv) ni.setAttribute('uv', new THREE.BufferAttribute(new Float32Array(ni.attributes.position.count * 2), 2));
    this.parts.push(colorize(ni, hex));
    return this;
  }
  build(): THREE.BufferGeometry {
    const m = mergeGeometries(this.parts, false)!;
    m.computeVertexNormals();
    this.parts.forEach((p) => p.dispose());
    return m;
  }
}

const cyl = (rt: number, rb: number, h: number, seg = 8) => new THREE.CylinderGeometry(rt, rb, h, seg);
const sph = (r: number, w = 10, h = 8) => new THREE.SphereGeometry(r, w, h);
const cone = (r: number, h: number, seg = 6) => new THREE.ConeGeometry(r, h, seg);
const box = (x: number, y: number, z: number) => new THREE.BoxGeometry(x, y, z);

/** Goat-legged satyr with curled horns. */
function satyr(): THREE.BufferGeometry {
  return new Builder()
    .add(0x3a2414, { g: cyl(0.1, 0.07, 0.8), x: -0.17, y: 0.4 })
    .add(0x3a2414, { g: cyl(0.1, 0.07, 0.8), x: 0.17, y: 0.4 })
    .add(0x1a1008, { g: cone(0.09, 0.14), x: -0.17, y: 0.05, rx: Math.PI })
    .add(0x1a1008, { g: cone(0.09, 0.14), x: 0.17, y: 0.05, rx: Math.PI })
    .add(0x6a4424, { g: cyl(0.3, 0.34, 0.5), y: 0.95 })
    .add(0xc58a58, { g: cyl(0.3, 0.26, 0.7), y: 1.4 })
    .add(0xc58a58, { g: cyl(0.07, 0.07, 0.7), x: -0.42, y: 1.35, rz: 0.35 })
    .add(0xc58a58, { g: cyl(0.07, 0.07, 0.7), x: 0.42, y: 1.35, rz: -0.35 })
    .add(0xd9a070, { g: sph(0.25), y: 1.95 })
    .add(0xd9a070, { g: cone(0.07, 0.22, 4), x: -0.27, y: 2.0, rz: 1.3 })
    .add(0xd9a070, { g: cone(0.07, 0.22, 4), x: 0.27, y: 2.0, rz: -1.3 })
    .add(0xeee2c0, { g: cone(0.07, 0.5), x: -0.16, y: 2.28, rz: 0.45 })
    .add(0xeee2c0, { g: cone(0.07, 0.5), x: 0.16, y: 2.28, rz: -0.45 })
    .add(0x3a2414, { g: cone(0.1, 0.3, 5), y: 1.7, z: 0.17, rx: Math.PI })
    .add(0xffe060, { g: sph(0.05, 5, 4), x: -0.09, y: 1.98, z: 0.22 })
    .add(0xffe060, { g: sph(0.05, 5, 4), x: 0.09, y: 1.98, z: 0.22 })
    .build();
}

/** Winged harpy; hovers (see ENEMY_DEFS.hover). */
function harpy(): THREE.BufferGeometry {
  return new Builder()
    .add(0x6a3a7a, { g: sph(0.32), y: 0.6, sy: 1.5 })
    .add(0xd8b8a8, { g: sph(0.2), y: 1.3 })
    .add(0xf0b020, { g: cone(0.06, 0.25, 4), y: 1.27, z: 0.24, rx: Math.PI / 2 })
    .add(0xff3030, { g: sph(0.04, 5, 4), x: -0.08, y: 1.34, z: 0.16 })
    .add(0xff3030, { g: sph(0.04, 5, 4), x: 0.08, y: 1.34, z: 0.16 })
    .add(0x3a2a6a, { g: box(1.15, 0.05, 0.6), x: -0.85, y: 0.95, rz: 0.45 })
    .add(0x3a2a6a, { g: box(1.15, 0.05, 0.6), x: 0.85, y: 0.95, rz: -0.45 })
    .add(0xa070d0, { g: box(0.6, 0.04, 0.45), x: -1.55, y: 1.2, rz: 0.5 })
    .add(0xa070d0, { g: box(0.6, 0.04, 0.45), x: 1.55, y: 1.2, rz: -0.5 })
    .add(0x4a2a5a, { g: box(0.25, 0.04, 0.7), y: 0.25, z: -0.45, rx: 0.5 })
    .add(0xf0b020, { g: cone(0.05, 0.3, 4), x: -0.12, y: 0.05, rx: Math.PI })
    .add(0xf0b020, { g: cone(0.05, 0.3, 4), x: 0.12, y: 0.05, rx: Math.PI })
    .build();
}

/** Skeleton hoplite with crested helmet, round shield and spear. */
function spartoi(): THREE.BufferGeometry {
  return new Builder()
    .add(0xe8e0c8, { g: cyl(0.06, 0.05, 0.85), x: -0.15, y: 0.42 })
    .add(0xe8e0c8, { g: cyl(0.06, 0.05, 0.85), x: 0.15, y: 0.42 })
    .add(0xd8d0b8, { g: cyl(0.22, 0.18, 0.2), y: 0.95 })
    .add(0xe8e0c8, { g: cyl(0.3, 0.2, 0.7), y: 1.4 })
    .add(0x555040, { g: box(0.5, 0.05, 0.12), y: 1.35, z: 0.18 })
    .add(0x555040, { g: box(0.45, 0.05, 0.12), y: 1.5, z: 0.18 })
    .add(0xe8e0c8, { g: sph(0.22), y: 1.95 })
    .add(0xb8862a, { g: sph(0.25, 10, 6), y: 2.0, sy: 0.8 })
    .add(0xb02a20, { g: box(0.08, 0.3, 0.5), y: 2.3 })
    .add(0xff8030, { g: sph(0.05, 5, 4), x: -0.08, y: 1.92, z: 0.19 })
    .add(0xff8030, { g: sph(0.05, 5, 4), x: 0.08, y: 1.92, z: 0.19 })
    .add(0xb8862a, { g: cyl(0.48, 0.48, 0.07, 16), x: -0.52, y: 1.35, z: 0.1, rz: Math.PI / 2 })
    .add(0xb02a20, { g: cyl(0.2, 0.2, 0.09, 12), x: -0.55, y: 1.35, z: 0.1, rz: Math.PI / 2 })
    .add(0x6a4a2a, { g: cyl(0.035, 0.035, 2.3), x: 0.5, y: 1.3, z: 0.15, rx: 0.15 })
    .add(0xc0c8d0, { g: cone(0.08, 0.35, 4), x: 0.5, y: 2.55, z: 0.28, rx: 0.15 })
    .build();
}

/** Bull-headed Minotaur with a great axe. */
function minotaur(): THREE.BufferGeometry {
  return new Builder()
    .add(0x2a1a10, { g: cyl(0.2, 0.15, 0.9), x: -0.28, y: 0.45 })
    .add(0x2a1a10, { g: cyl(0.2, 0.15, 0.9), x: 0.28, y: 0.45 })
    .add(0x1a1008, { g: box(0.3, 0.15, 0.45), x: -0.28, y: 0.07, z: 0.08 })
    .add(0x1a1008, { g: box(0.3, 0.15, 0.45), x: 0.28, y: 0.07, z: 0.08 })
    .add(0x5a3420, { g: cyl(0.55, 0.38, 0.9), y: 1.2 })
    .add(0x6a4028, { g: sph(0.62, 10, 8), y: 1.65, sy: 0.7 })
    .add(0x6a4028, { g: cyl(0.14, 0.16, 0.8), x: -0.78, y: 1.4, rz: 0.3 })
    .add(0x6a4028, { g: cyl(0.14, 0.16, 0.8), x: 0.78, y: 1.4, rz: -0.3 })
    .add(0x5a3420, { g: sph(0.36), y: 2.15, z: 0.08 })
    .add(0x8a6a50, { g: box(0.3, 0.26, 0.3), y: 2.05, z: 0.4 })
    .add(0xe8d8a0, { g: cone(0.1, 0.9, 6), x: -0.5, y: 2.3, rz: 1.1 })
    .add(0xe8d8a0, { g: cone(0.1, 0.9, 6), x: 0.5, y: 2.3, rz: -1.1 })
    .add(0xe0b030, { g: new THREE.TorusGeometry(0.09, 0.025, 5, 10), y: 1.98, z: 0.55 })
    .add(0xff3020, { g: sph(0.06, 5, 4), x: -0.15, y: 2.22, z: 0.3 })
    .add(0xff3020, { g: sph(0.06, 5, 4), x: 0.15, y: 2.22, z: 0.3 })
    .add(0x4a3020, { g: cyl(0.05, 0.05, 1.7), x: 0.95, y: 1.4, z: 0.1, rx: 0.1 })
    .add(0xc0c8d0, { g: box(0.7, 0.45, 0.06), x: 1.2, y: 2.05, z: 0.12 })
    .build();
}

/** One-eyed Cyclops with a spiked club. */
function cyclops(): THREE.BufferGeometry {
  return new Builder()
    .add(0x6a5a40, { g: cyl(0.24, 0.2, 0.9), x: -0.3, y: 0.45 })
    .add(0x6a5a40, { g: cyl(0.24, 0.2, 0.9), x: 0.3, y: 0.45 })
    .add(0x9a8a6a, { g: sph(0.7, 10, 8), y: 1.3, sy: 0.9 })
    .add(0x8a6a3a, { g: cyl(0.7, 0.72, 0.28), y: 0.95 })
    .add(0x9a8a6a, { g: cyl(0.55, 0.65, 0.8), y: 1.7 })
    .add(0x9a8a6a, { g: cyl(0.17, 0.2, 1.0), x: -0.85, y: 1.5, rz: 0.25 })
    .add(0x9a8a6a, { g: cyl(0.17, 0.2, 1.0), x: 0.85, y: 1.5, rz: -0.25 })
    .add(0xa8967a, { g: sph(0.46), y: 2.35 })
    .add(0xf4f0e0, { g: sph(0.19, 8, 6), y: 2.4, z: 0.38 })
    .add(0xd02010, { g: sph(0.09, 6, 5), y: 2.4, z: 0.54 })
    .add(0x3a2a1a, { g: box(0.34, 0.07, 0.1), y: 2.2, z: 0.4 })
    .add(0x4a3020, { g: cyl(0.1, 0.2, 1.6), x: 1.15, y: 1.6, rz: -0.1 })
    .add(0x5a3a22, { g: cyl(0.3, 0.12, 0.9), x: 1.2, y: 2.6, rz: -0.1 })
    .add(0xc0c8d0, { g: cone(0.06, 0.28, 4), x: 1.5, y: 2.7, rz: -1.4 })
    .add(0xc0c8d0, { g: cone(0.06, 0.28, 4), x: 0.9, y: 2.75, rz: 1.4 })
    .add(0xc0c8d0, { g: cone(0.06, 0.28, 4), x: 1.2, y: 3.1 })
    .build();
}

const BUILDERS: Record<EnemyKind, () => THREE.BufferGeometry> = { satyr, harpy, spartoi, minotaur, cyclops };
const DEATH_COLOR: Record<EnemyKind, number> = { satyr: 0xd9a070, harpy: 0xb89cff, spartoi: 0xe8e0c8, minotaur: 0xff8a50, cyclops: 0xffd070 };

/** Pooled mythological creatures, drawn with one InstancedMesh per kind + one blob-shadow mesh. */
export class EnemyManager {
  readonly active: Enemy[] = [];
  onKill: (e: Enemy) => void = () => {};
  onPlayerHit?: (e: Enemy) => void;
  spawning = true;

  private pool: Enemy[] = [];
  private meshes = {} as Record<EnemyKind, THREE.InstancedMesh>;
  private shadows: THREE.InstancedMesh;
  private rng: Rng;
  private level: Level;
  private spawnTimer = 1.2;
  private counts = {} as Record<EnemyKind, number>;
  private m4 = new THREE.Matrix4();
  private q = new THREE.Quaternion();
  private v = new THREE.Vector3();
  private s = new THREE.Vector3();
  private col = new THREE.Color();
  private up = new THREE.Vector3(0, 1, 0);

  constructor(private scene: THREE.Scene, private fx: Effects, private player: Player, seed: number, level: Level) {
    this.rng = new Rng(seed);
    this.level = level;
    for (const kind of ENEMY_KINDS) {
      this.counts[kind] = 0;
      const mat = new THREE.MeshLambertMaterial({ vertexColors: true, emissive: 0x1a1030, side: THREE.DoubleSide });
      const mesh = new THREE.InstancedMesh(BUILDERS[kind](), mat, MAX_PER_KIND);
      mesh.frustumCulled = false;
      mesh.count = 0;
      mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      mesh.setColorAt(0, this.col.set(0xffffff));
      mesh.instanceColor!.setUsage(THREE.DynamicDrawUsage);
      this.meshes[kind] = mesh;
      scene.add(mesh);
    }
    const sg = new THREE.CircleGeometry(0.8, 14);
    sg.rotateX(-Math.PI / 2);
    this.shadows = new THREE.InstancedMesh(sg, new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.3, depthWrite: false }), CFG.MAX_ENEMIES + 8);
    this.shadows.frustumCulled = false;
    this.shadows.count = 0;
    scene.add(this.shadows);
    for (let i = 0; i < CFG.MAX_ENEMIES + 8; i++) this.pool.push(this.blank());
  }

  private blank(): Enemy {
    return { kind: 'satyr', state: 'alive', x: 0, z: 0, vx: 0, vz: 0, hp: 1, radius: 0.5, scale: 1, t: 0, hitFlash: 0, phase: 0, yaw: 0, attackCd: 0, aiT: 0, windup: 0, dash: 0, dashX: 0, dashZ: 0 };
  }

  reset(seed: number, level: Level): void {
    this.rng = new Rng(seed);
    this.level = level;
    while (this.active.length) this.pool.push(this.active.pop()!);
    this.spawnTimer = 1.2;
    this.spawning = true;
    this.flush();
  }

  get aliveCount(): number {
    let n = 0;
    for (const e of this.active) if (e.state !== 'dying') n++;
    return n;
  }

  private heavyCount(): number {
    let n = 0;
    for (const e of this.active) if (ENEMY_DEFS[e.kind].heavy && e.state !== 'dying') n++;
    return n;
  }

  private director(t: number): { interval: number; batch: number; maxAlive: number } {
    const L = this.level;
    const f = t / L.time;
    const lerp = (a: number, b: number, k: number) => a + (b - a) * Math.min(1, Math.max(0, k));
    // Baseline curve (tuned on the 75 s run), then scaled by the level.
    let interval: number;
    let batch: number;
    let max: number;
    switch (phaseAt(t, L.time)) {
      case 'warmup':
        interval = lerp(1.6, 1.15, f / 0.2);
        batch = 1;
        max = 5 + f * 20;
        break;
      case 'escalation': {
        const k = (f - 0.2) / 0.267;
        interval = lerp(1.1, 0.7, k);
        batch = k > 0.5 ? 2 : 1;
        max = lerp(9, 18, k);
        break;
      }
      case 'chaos': {
        const k = (f - 0.467) / 0.4;
        interval = lerp(0.65, 0.42, k);
        batch = 2;
        max = lerp(20, 32, k);
        break;
      }
      default:
        interval = 0.3;
        batch = 3;
        max = 46;
    }
    return {
      interval: interval / L.spawn,
      batch: batch + (L.spawn > 1.25 && batch > 1 ? 1 : 0),
      maxAlive: Math.min(CFG.MAX_ENEMIES - 4, Math.round(max * L.spawn)),
    };
  }

  private pick(roll: number, w: readonly number[]): EnemyKind {
    let acc = 0;
    for (let i = 0; i < w.length; i++) {
      acc += w[i];
      if (roll < acc) return ENEMY_KINDS[i];
    }
    return 'satyr';
  }

  private spawnOne(kind: EnemyKind, angle: number): void {
    const e = this.pool.pop();
    if (!e) return;
    const def = ENEMY_DEFS[kind];
    const R = world.radius - 1.3;
    let x = Math.cos(angle) * R;
    let z = Math.sin(angle) * R;
    // On big maps the rim can be far away: pull the spawn in so threats arrive in a few seconds.
    const p = this.player.pos;
    const dx = x - p.x;
    const dz = z - p.z;
    const d = Math.hypot(dx, dz);
    const MAXD = 24;
    if (d > MAXD) {
      x = p.x + (dx / d) * MAXD;
      z = p.z + (dz / d) * MAXD;
      const r = Math.hypot(x, z);
      if (r > R) {
        x *= R / r;
        z *= R / r;
      }
    }
    e.kind = kind;
    e.state = 'rising';
    e.x = x;
    e.z = z;
    e.vx = e.vz = 0;
    e.hp = def.hp;
    e.radius = def.radius * def.scale;
    e.scale = def.scale;
    e.t = 0;
    e.hitFlash = 0;
    e.phase = this.rng.next() * 6.28;
    e.attackCd = 0.4;
    e.aiT = 2 + this.rng.next() * 2;
    e.windup = e.dash = 0;
    e.yaw = angle + Math.PI;
    this.active.push(e);
    this.fx.burst(e.x, 0.2, e.z, def.heavy ? 12 : 5, 0x9a7ad8, 2.5, 0.5, 0.5);
  }

  update(dt: number, runTime: number, running: boolean): void {
    const p = this.player;
    const L = this.level;
    if (running && this.spawning) {
      const tg = this.director(runTime);
      this.spawnTimer -= dt;
      if (this.spawnTimer <= 0) {
        this.spawnTimer = tg.interval;
        const w = L.weights[phaseAt(runTime, L.time)];
        for (let b = 0; b < tg.batch; b++) {
          // RNG draws happen unconditionally so every player gets the same sequence.
          let angle = this.rng.next() * Math.PI * 2;
          const roll = this.rng.next();
          let kind = this.pick(roll, w);
          if (Math.hypot(Math.cos(angle) * (world.radius - 1.3) - p.pos.x, Math.sin(angle) * (world.radius - 1.3) - p.pos.z) < 7) angle += Math.PI;
          if (ENEMY_DEFS[kind].heavy && this.heavyCount() >= L.heavyCap) kind = 'spartoi';
          if (this.aliveCount < tg.maxAlive) this.spawnOne(kind, angle);
        }
      }
    }

    const speedMul = L.speed * (1 + (runTime / L.time) * 0.3 + (phaseAt(runTime, L.time) === 'wrath' ? 0.08 : 0));
    const R = world.radius - 0.5;
    for (let i = this.active.length - 1; i >= 0; i--) {
      const e = this.active[i];
      e.t += dt;
      e.attackCd -= dt;
      e.hitFlash = Math.max(0, e.hitFlash - dt * 6);
      if (e.state === 'rising') {
        if (e.t >= 0.5) {
          e.state = 'alive';
          e.t = 0;
        }
      } else if (e.state === 'dying') {
        if (e.t >= 0.4) {
          this.active.splice(i, 1);
          this.pool.push(e);
        }
        e.x += e.vx * dt;
        e.z += e.vz * dt;
        e.vx *= 0.92;
        e.vz *= 0.92;
        continue;
      }
      const def = ENEMY_DEFS[e.kind];
      let dx = p.pos.x - e.x;
      let dz = p.pos.z - e.z;
      const dist = Math.hypot(dx, dz) || 1;
      dx /= dist;
      dz /= dist;
      if (e.state === 'alive' && running && p.alive) {
        let sp = def.speed * speedMul;
        let sx = dx;
        let sz = dz;
        if (e.kind === 'harpy') {
          const w = Math.sin(runTime * 5 + e.phase) * 0.7;
          sx += -dz * w;
          sz += dx * w;
        } else if (e.kind === 'satyr') {
          const w = Math.sin(runTime * 2.2 + e.phase) * 0.25;
          sx += -dz * w;
          sz += dx * w;
        } else if (e.kind === 'cyclops') {
          sp *= 0.55 + 0.45 * Math.max(0, Math.sin(runTime * 2.4 + e.phase));
        } else if (e.kind === 'minotaur') {
          // wind up (telegraph), then a straight-line charge
          if (e.dash > 0) {
            e.dash -= dt;
            sx = e.dashX;
            sz = e.dashZ;
            sp = def.speed * 4.2 * speedMul;
          } else if (e.windup > 0) {
            e.windup -= dt;
            sp = 0;
            if (Math.random() < dt * 40) this.fx.spawn(e.x, 0.3, e.z, (Math.random() - 0.5) * 2, 2, (Math.random() - 0.5) * 2, 0xff5030, 0.4, 0.6, 3);
            if (e.windup <= 0) {
              e.dash = 0.75;
              e.dashX = dx;
              e.dashZ = dz;
            }
          } else {
            e.aiT -= dt;
            if (e.aiT <= 0 && dist < 14 && dist > 4) {
              e.windup = 0.6;
              e.aiT = 4 + Math.random() * 2;
            }
          }
        }
        e.x += (sx * sp + e.vx) * dt;
        e.z += (sz * sp + e.vz) * dt;
      } else {
        e.x += e.vx * dt;
        e.z += e.vz * dt;
      }
      const kd = Math.exp(-7 * dt);
      e.vx *= kd;
      e.vz *= kd;
      e.yaw = e.dash > 0 ? Math.atan2(e.dashX, e.dashZ) : Math.atan2(dx, dz);

      // separation (cheap O(n²), n ≤ ~60)
      for (let j = i - 1; j >= 0; j--) {
        const o = this.active[j];
        if (o.state === 'dying') continue;
        const ox = e.x - o.x;
        const oz = e.z - o.z;
        const min = (e.radius + o.radius) * 0.85;
        const d2 = ox * ox + oz * oz;
        if (d2 < min * min && d2 > 1e-5) {
          const d = Math.sqrt(d2);
          const push = ((min - d) / d) * 0.5;
          e.x += ox * push;
          e.z += oz * push;
          o.x -= ox * push;
          o.z -= oz * push;
        }
      }
      const er = Math.hypot(e.x, e.z);
      if (er > R) {
        e.x *= R / er;
        e.z *= R / er;
      }

      // contact with Zeus
      if (e.state === 'alive' && running && p.alive && p.state !== 'dodge') {
        const cx = p.pos.x - e.x;
        const cz = p.pos.z - e.z;
        const cd = Math.hypot(cx, cz);
        const reach = e.radius + CFG.PLAYER_RADIUS + 0.15;
        if (cd < reach) {
          if (e.attackCd <= 0 && p.takeHit(e.x, e.z, def.damage)) {
            e.attackCd = 1.2;
            e.dash = 0;
            e.vx -= (cx / (cd || 1)) * 6;
            e.vz -= (cz / (cd || 1)) * 6;
            this.fx.burst(p.pos.x, 1.2, p.pos.z, 12, 0xff6a5a, 5, 0.6, 0.5);
            this.onPlayerHit?.(e);
          } else if (p.invuln > 0 || e.attackCd > 0) {
            // soft push-out so creatures never stack inside Zeus
            const push = (reach - cd) * 0.5;
            e.x -= (cx / (cd || 1)) * push;
            e.z -= (cz / (cd || 1)) * push;
          }
        }
      }
    }
    this.flush();
  }

  /** Applies damage. Returns true if the creature died. */
  hit(e: Enemy, dmg: number, dirX: number, dirZ: number, force = 7): boolean {
    if (e.state !== 'alive') return false;
    e.hp -= dmg;
    e.hitFlash = 1;
    const resist = ENEMY_DEFS[e.kind].heavy ? 0.4 : 1;
    e.vx += dirX * force * resist;
    e.vz += dirZ * force * resist;
    if (e.hp > 0) {
      this.fx.burst(e.x, 1.2, e.z, 6, 0xcfe9ff, 4, 0.45, 0.35);
      return false;
    }
    this.kill(e, dirX, dirZ);
    return true;
  }

  private kill(e: Enemy, dirX: number, dirZ: number): void {
    e.state = 'dying';
    e.t = 0;
    e.vx = dirX * 5;
    e.vz = dirZ * 5;
    const heavy = ENEMY_DEFS[e.kind].heavy;
    this.fx.burst(e.x, 1.1 * e.scale, e.z, heavy ? 28 : 14, DEATH_COLOR[e.kind], 6, 0.8, 0.8);
    this.fx.burst(e.x, 1.1 * e.scale, e.z, 6, 0xffffff, 5, 0.5, 0.5);
    this.onKill(e);
  }

  /** Instantly dissolves everything still alive (used for the finale). */
  killAll(): Enemy[] {
    const out: Enemy[] = [];
    for (const e of this.active) {
      if (e.state === 'dying') continue;
      e.hp = 0;
      this.kill(e, 0, 0);
      out.push(e);
    }
    return out;
  }

  private flush(): void {
    for (const k of ENEMY_KINDS) this.counts[k] = 0;
    let sh = 0;
    for (const e of this.active) {
      const mesh = this.meshes[e.kind];
      const def = ENEMY_DEFS[e.kind];
      const i = this.counts[e.kind]++;
      let sc = e.scale;
      let y = 0;
      if (e.state === 'rising') {
        const k = Math.min(1, e.t / 0.5);
        y = -1.8 * e.scale * (1 - k);
        sc *= 0.5 + 0.5 * k;
      } else if (e.state === 'dying') {
        const k = e.t / 0.4;
        sc *= k < 0.2 ? 1 + k : Math.max(0.001, 1.2 * (1 - (k - 0.2) / 0.8));
        y = k * 1.2;
      } else if (def.hover > 0) {
        y = def.hover + Math.sin(e.t * 9 + e.phase) * 0.25;
      } else {
        y = Math.abs(Math.sin(e.t * 6 + e.phase)) * 0.06;
      }
      // minotaur telegraph: lean back and shake
      const wob = e.windup > 0 ? Math.sin(e.t * 60) * 0.06 : 0;
      this.q.setFromAxisAngle(this.up, e.yaw + wob);
      this.m4.compose(this.v.set(e.x, y, e.z), this.q, this.s.set(sc, sc * (e.state === 'dying' ? 0.7 : 1), sc));
      mesh.setMatrixAt(i, this.m4);
      const f = e.hitFlash + (e.windup > 0 ? 0.5 + Math.sin(e.t * 40) * 0.3 : 0);
      mesh.setColorAt(i, this.col.setRGB(1 + f * 3, 1 + f * (e.windup > 0 ? 0.3 : 3), 1 + f * (e.windup > 0 ? 0.3 : 4)));
      this.m4.compose(this.v.set(e.x, 0.04, e.z), this.q.identity(), this.s.setScalar(e.scale * (e.state === 'dying' ? 0.3 : 1)));
      this.shadows.setMatrixAt(sh++, this.m4);
    }
    for (const k of ENEMY_KINDS) {
      const m = this.meshes[k];
      m.count = this.counts[k];
      m.instanceMatrix.needsUpdate = true;
      if (m.instanceColor) m.instanceColor.needsUpdate = true;
    }
    this.shadows.count = sh;
    this.shadows.instanceMatrix.needsUpdate = true;
  }

  dispose(): void {
    for (const m of Object.values(this.meshes)) {
      this.scene.remove(m);
      m.geometry.dispose();
      (m.material as THREE.Material).dispose();
      m.dispose();
    }
    this.scene.remove(this.shadows);
    this.shadows.geometry.dispose();
    (this.shadows.material as THREE.Material).dispose();
  }
}
