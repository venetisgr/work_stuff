import * as THREE from 'three';
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js';
import { CFG, ENEMY_DEFS, phaseAt, type EnemyKind } from './config';
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
  slot: number;
  /** Used by combat to group simultaneous kills into a multi-kill. */
  lastHitBy: number;
}

const MAX_PER_KIND = CFG.MAX_ENEMIES;

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

function placed(geo: THREE.BufferGeometry, x: number, y: number, z: number, rx = 0, rz = 0): THREE.BufferGeometry {
  geo.rotateX(rx);
  geo.rotateZ(rz);
  geo.translate(x, y, z);
  return geo;
}

function toNonIndexed(g: THREE.BufferGeometry): THREE.BufferGeometry {
  const out = g.index ? g.toNonIndexed() : g;
  if (!out.attributes.uv) out.setAttribute('uv', new THREE.BufferAttribute(new Float32Array(out.attributes.position.count * 2), 2));
  return out;
}

/** Apep's brood: a bandaged mummy, a darting scarab and a hulking stone colossus. All local +Z = forward. */
function buildGeometry(kind: EnemyKind): THREE.BufferGeometry {
  const parts: THREE.BufferGeometry[] = [];
  const add = (g: THREE.BufferGeometry, hex: number) => parts.push(toNonIndexed(colorize(g, hex)));
  if (kind === 'mummy') {
    add(placed(new THREE.CylinderGeometry(0.3, 0.38, 1.3, 9), 0, 0.75, 0), 0xcdbf9a); // wrapped body
    add(placed(new THREE.TorusGeometry(0.36, 0.045, 5, 12), 0, 0.55, 0, Math.PI / 2), 0x8a7a58); // bandage bands
    add(placed(new THREE.TorusGeometry(0.33, 0.045, 5, 12), 0, 0.95, 0, Math.PI / 2), 0x8a7a58);
    add(placed(new THREE.SphereGeometry(0.27, 10, 8), 0, 1.65, 0), 0xe0d3ae); // head
    add(placed(new THREE.SphereGeometry(0.06, 6, 5), -0.1, 1.68, 0.23), 0x66ffb0); // glowing eyes
    add(placed(new THREE.SphereGeometry(0.06, 6, 5), 0.1, 1.68, 0.23), 0x66ffb0);
    add(placed(new THREE.CylinderGeometry(0.09, 0.08, 0.85, 6), -0.45, 1.2, 0.35, -1.3, 0), 0xcdbf9a); // arms reaching out
    add(placed(new THREE.CylinderGeometry(0.09, 0.08, 0.85, 6), 0.45, 1.2, 0.35, -1.3, 0), 0xcdbf9a);
    add(placed(new THREE.BoxGeometry(0.5, 0.22, 0.04), 0, 0.28, 0.36), 0xb09a6a); // trailing bandage
  } else if (kind === 'scarab') {
    add(placed(new THREE.SphereGeometry(0.5, 12, 8), 0, 0.55, 0), 0x1d6f8a); // shell
    add(placed(new THREE.SphereGeometry(0.5, 12, 8), 0, 0.55, 0), 0x1d6f8a);
    add(placed(new THREE.BoxGeometry(0.03, 0.04, 0.95), 0, 1.04, -0.05), 0x0c3a4a); // wing-case seam
    add(placed(new THREE.SphereGeometry(0.26, 8, 6), 0, 0.5, 0.52), 0x0f4a5e); // head
    add(placed(new THREE.ConeGeometry(0.05, 0.4, 5), -0.1, 0.62, 0.78, Math.PI / 2, 0), 0xe8b84a); // horns
    add(placed(new THREE.ConeGeometry(0.05, 0.4, 5), 0.1, 0.62, 0.78, Math.PI / 2, 0), 0xe8b84a);
    add(placed(new THREE.SphereGeometry(0.05, 5, 4), -0.12, 0.58, 0.72), 0xffe36a);
    add(placed(new THREE.SphereGeometry(0.05, 5, 4), 0.12, 0.58, 0.72), 0xffe36a);
    for (const z of [-0.3, 0, 0.3]) {
      add(placed(new THREE.CylinderGeometry(0.03, 0.03, 0.55, 4), -0.55, 0.22, z, 0, 0.9), 0x0c3a4a); // legs
      add(placed(new THREE.CylinderGeometry(0.03, 0.03, 0.55, 4), 0.55, 0.22, z, 0, -0.9), 0x0c3a4a);
    }
  } else {
    add(placed(new THREE.BoxGeometry(1.1, 1.5, 0.75), 0, 1.0, 0), 0x8f7a52); // sandstone torso
    add(placed(new THREE.BoxGeometry(1.2, 0.25, 0.8), 0, 1.78, 0), 0xe8b84a); // gold collar
    add(placed(new THREE.BoxGeometry(0.6, 0.62, 0.6), 0, 2.2, 0.02), 0xa68a58); // head
    add(placed(new THREE.BoxGeometry(0.9, 0.2, 0.7), 0, 2.58, 0), 0x2a4f9a); // striped nemes crown
    add(placed(new THREE.BoxGeometry(0.9, 0.2, 0.7), 0, 2.4, -0.08), 0xe8b84a);
    add(placed(new THREE.SphereGeometry(0.07, 6, 5), -0.15, 2.25, 0.32), 0xff5a2a);
    add(placed(new THREE.SphereGeometry(0.07, 6, 5), 0.15, 2.25, 0.32), 0xff5a2a);
    add(placed(new THREE.BoxGeometry(0.4, 1.3, 0.45), -0.85, 1.05, 0.1), 0x7a6644); // thick arms
    add(placed(new THREE.BoxGeometry(0.4, 1.3, 0.45), 0.85, 1.05, 0.1), 0x7a6644);
    add(placed(new THREE.BoxGeometry(0.5, 0.45, 0.5), -0.85, 0.3, 0.15), 0x6a5838); // fists
    add(placed(new THREE.BoxGeometry(0.5, 0.45, 0.5), 0.85, 0.3, 0.15), 0x6a5838);
    add(placed(new THREE.BoxGeometry(1.0, 0.5, 0.65), 0, 0.25, 0), 0x6a5838); // kilt block
  }
  const merged = mergeGeometries(parts, false)!;
  merged.computeVertexNormals();
  parts.forEach((p) => p.dispose());
  return merged;
}

/** Pooled enemies rendered with one InstancedMesh per kind (3 draw calls) + one blob-shadow mesh. */
export class EnemyManager {
  readonly active: Enemy[] = [];
  onKill: (e: Enemy) => void = () => {};

  private pool: Enemy[] = [];
  private meshes = {} as Record<EnemyKind, THREE.InstancedMesh>;
  private shadows: THREE.InstancedMesh;
  private rng: Rng;
  private spawnTimer = 1.2;
  private counts: Record<EnemyKind, number> = { mummy: 0, scarab: 0, colossus: 0 };
  private m4 = new THREE.Matrix4();
  private q = new THREE.Quaternion();
  private v = new THREE.Vector3();
  private s = new THREE.Vector3();
  private col = new THREE.Color();
  private up = new THREE.Vector3(0, 1, 0);
  spawning = true;

  constructor(private scene: THREE.Scene, private fx: Effects, private player: Player, seed: number) {
    this.rng = new Rng(seed);
    for (const kind of ['mummy', 'scarab', 'colossus'] as EnemyKind[]) {
      const mat = new THREE.MeshLambertMaterial({ vertexColors: true, emissive: 0x2a2010, side: THREE.DoubleSide });
      const mesh = new THREE.InstancedMesh(buildGeometry(kind), mat, MAX_PER_KIND);
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
    this.shadows = new THREE.InstancedMesh(
      sg,
      new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.3, depthWrite: false }),
      MAX_PER_KIND * 3,
    );
    this.shadows.frustumCulled = false;
    this.shadows.count = 0;
    scene.add(this.shadows);
    for (let i = 0; i < CFG.MAX_ENEMIES + 8; i++) this.pool.push(this.blank());
  }

  private blank(): Enemy {
    return { kind: 'mummy', state: 'alive', x: 0, z: 0, vx: 0, vz: 0, hp: 1, radius: 0.5, scale: 1, t: 0, hitFlash: 0, phase: 0, yaw: 0, attackCd: 0, slot: 0, lastHitBy: -1 };
  }

  reset(seed: number): void {
    this.rng = new Rng(seed);
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

  private targetsFor(t: number): { interval: number; batch: number; maxAlive: number; weights: [number, number, number]; colossi: number } {
    const lerp = (a: number, b: number, k: number) => a + (b - a) * Math.min(1, Math.max(0, k));
    switch (phaseAt(t)) {
      case 'warmup':
        return { interval: lerp(1.6, 1.15, t / 15), batch: 1, maxAlive: 5 + Math.floor(t / 4), weights: [1, 0, 0], colossi: 0 };
      case 'escalation': {
        const k = (t - 15) / 20;
        return { interval: lerp(1.1, 0.7, k), batch: k > 0.5 ? 2 : 1, maxAlive: Math.floor(lerp(9, 18, k)), weights: [0.75, 0.25, 0], colossi: 0 };
      }
      case 'chaos': {
        const k = (t - 35) / 30;
        return { interval: lerp(0.65, 0.42, k), batch: 2, maxAlive: Math.floor(lerp(20, 32, k)), weights: [0.5, 0.3, 0.2], colossi: 4 };
      }
      default:
        return { interval: 0.3, batch: 3, maxAlive: 46, weights: [0.45, 0.35, 0.2], colossi: 6 };
    }
  }

  private spawnOne(kind: EnemyKind, angle: number, rise = true): void {
    const e = this.pool.pop();
    if (!e) return;
    const def = ENEMY_DEFS[kind];
    const R = CFG.ARENA_RADIUS - 1.3;
    e.kind = kind;
    e.state = rise ? 'rising' : 'alive';
    e.x = Math.cos(angle) * R;
    e.z = Math.sin(angle) * R;
    e.vx = e.vz = 0;
    e.hp = def.hp;
    e.radius = def.radius * def.scale;
    e.scale = def.scale;
    e.t = 0;
    e.hitFlash = 0;
    e.phase = this.rng.next() * 6.28;
    e.attackCd = 0.4;
    e.lastHitBy = -1;
    e.yaw = angle + Math.PI;
    this.active.push(e);
    this.fx.burst(e.x, 0.2, e.z, 5, 0xe8c46a, 2.5, 0.5, 0.5);
  }

  update(dt: number, runTime: number, running: boolean): void {
    const p = this.player;
    if (running && this.spawning) {
      const tg = this.targetsFor(runTime);
      this.spawnTimer -= dt;
      if (this.spawnTimer <= 0) {
        this.spawnTimer = tg.interval;
        for (let b = 0; b < tg.batch; b++) {
          // RNG draws happen unconditionally so every player gets the same sequence.
          let angle = this.rng.next() * Math.PI * 2;
          const roll = this.rng.next();
          const w = tg.weights;
          let kind: EnemyKind = roll < w[0] ? 'mummy' : roll < w[0] + w[1] ? 'scarab' : 'colossus';
          if (Math.hypot(Math.cos(angle) * 13.7 - p.pos.x, Math.sin(angle) * 13.7 - p.pos.z) < 7) angle += Math.PI;
          let colossi = 0;
          for (const e of this.active) if (e.kind === 'colossus' && e.state !== 'dying') colossi++;
          if (kind === 'colossus' && colossi >= tg.colossi) kind = 'mummy';
          if (this.aliveCount < tg.maxAlive) this.spawnOne(kind, angle);
        }
      }
    }

    const speedMul = 1 + (runTime / CFG.RUN_TIME) * 0.35 + (phaseAt(runTime) === 'wrath' ? 0.1 : 0);
    const R = CFG.ARENA_RADIUS - 0.5;
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
      // chase
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
        if (e.kind === 'scarab') {
          const w = Math.sin(runTime * 5 + e.phase) * 0.6;
          sx += -dz * w;
          sz += dx * w;
        } else if (e.kind === 'mummy') {
          const w = Math.sin(runTime * 2.2 + e.phase) * 0.25;
          sx += -dz * w;
          sz += dx * w;
        }
        // colossi lurch: short pauses between strides
        if (e.kind === 'colossus') sp *= 0.55 + 0.45 * Math.max(0, Math.sin(runTime * 2.6 + e.phase));
        e.x += (sx * sp + e.vx) * dt;
        e.z += (sz * sp + e.vz) * dt;
      } else {
        e.x += e.vx * dt;
        e.z += e.vz * dt;
      }
      const kd = Math.exp(-7 * dt);
      e.vx *= kd;
      e.vz *= kd;
      e.yaw = Math.atan2(dx, dz);

      // separation (cheap O(n²), n ≤ ~56)
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

      // contact with the god
      if (e.state === 'alive' && running && p.alive && p.state !== 'dodge') {
        const cx = p.pos.x - e.x;
        const cz = p.pos.z - e.z;
        const cd = Math.hypot(cx, cz);
        const reach = e.radius + CFG.PLAYER_RADIUS + 0.15;
        if (cd < reach) {
          if (e.attackCd <= 0 && p.takeHit(e.x, e.z, def.damage)) {
            e.attackCd = 1.2;
            e.vx -= (cx / (cd || 1)) * 6;
            e.vz -= (cz / (cd || 1)) * 6;
            this.fx.burst(p.pos.x, 1.2, p.pos.z, 12, 0xff6a5a, 5, 0.6, 0.5);
            this.onPlayerHit?.(e);
          } else if (p.invuln > 0 || e.attackCd > 0) {
            // soft push-out so enemies never stack inside the player
            const push = (reach - cd) * 0.5;
            e.x -= (cx / (cd || 1)) * push;
            e.z -= (cz / (cd || 1)) * push;
          }
        }
      }
    }
    this.flush();
  }

  onPlayerHit?: (e: Enemy) => void;

  /** Applies damage. Returns true if the enemy died. */
  hit(e: Enemy, dmg: number, dirX: number, dirZ: number, force = 7): boolean {
    if (e.state !== 'alive') return false;
    e.hp -= dmg;
    e.hitFlash = 1;
    e.vx += dirX * force;
    e.vz += dirZ * force;
    if (e.hp > 0) {
      this.fx.burst(e.x, 1.2, e.z, 6, 0xfff0c8, 4, 0.45, 0.35);
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
    const col = e.kind === 'colossus' ? 0xffb050 : e.kind === 'scarab' ? 0x58d0e8 : 0xe8d9a8;
    this.fx.burst(e.x, 1.1 * e.scale, e.z, e.kind === 'colossus' ? 26 : 14, col, 6, 0.8, 0.8);
    this.fx.burst(e.x, 1.1 * e.scale, e.z, 6, 0xffffff, 5, 0.5, 0.5);
    this.onKill(e);
  }

  /** Instantly dissolves everything still alive (used for the finale). Returns count. */
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
    this.counts.mummy = this.counts.scarab = this.counts.colossus = 0;
    let sh = 0;
    for (const e of this.active) {
      const mesh = this.meshes[e.kind];
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
      } else {
        y = Math.abs(Math.sin(e.t * 6 + e.phase)) * 0.06;
      }
      this.q.setFromAxisAngle(this.up, e.yaw);
      this.m4.compose(this.v.set(e.x, y, e.z), this.q, this.s.set(sc, sc * (e.state === 'dying' ? 0.7 : 1), sc));
      mesh.setMatrixAt(i, this.m4);
      const f = e.hitFlash;
      mesh.setColorAt(i, this.col.setRGB(1 + f * 3, 1 + f * 3, 1 + f * 4));
      // blob shadow
      this.m4.compose(this.v.set(e.x, 0.04, e.z), this.q.identity(), this.s.setScalar(e.scale * (e.state === 'dying' ? 0.3 : 1)));
      this.shadows.setMatrixAt(sh++, this.m4);
    }
    for (const k of ['mummy', 'scarab', 'colossus'] as EnemyKind[]) {
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
