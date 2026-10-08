import * as THREE from 'three';
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js';
import { CFG } from './config';

/** A small golden ankh: loop + crossbar + stem, merged into one geometry. */
function ankhGeometry(): THREE.BufferGeometry {
  const loop = new THREE.TorusGeometry(0.11, 0.035, 6, 14);
  loop.translate(0, 0.2, 0);
  const stem = new THREE.BoxGeometry(0.07, 0.4, 0.07);
  stem.translate(0, -0.05, 0);
  const bar = new THREE.BoxGeometry(0.3, 0.07, 0.07);
  bar.translate(0, 0.02, 0);
  const g = mergeGeometries([loop.toNonIndexed(), stem.toNonIndexed(), bar.toNonIndexed()])!;
  [loop, stem, bar].forEach((x) => x.dispose());
  return g;
}

interface Spark {
  active: boolean;
  x: number;
  y: number;
  z: number;
  vx: number;
  vz: number;
  vy: number;
  t: number;
  phase: number;
}

const MAX = 48;
const LIFE = 10;

/** Ankh sparks: pooled, one InstancedMesh, magnetised toward the player. */
export class Pickups {
  private sparks: Spark[] = [];
  private mesh: THREE.InstancedMesh;
  private m4 = new THREE.Matrix4();
  private q = new THREE.Quaternion();
  private e = new THREE.Euler();
  private v = new THREE.Vector3();
  private s = new THREE.Vector3();
  private col = new THREE.Color();

  constructor(private scene: THREE.Scene) {
    for (let i = 0; i < MAX; i++) this.sparks.push({ active: false, x: 0, y: 0, z: 0, vx: 0, vz: 0, vy: 0, t: 0, phase: 0 });
    this.mesh = new THREE.InstancedMesh(
      ankhGeometry(),
      new THREE.MeshBasicMaterial({ color: 0xffffff, fog: false }),
      MAX,
    );
    this.mesh.frustumCulled = false;
    this.mesh.count = 0;
    this.mesh.setColorAt(0, this.col.set(0xffffff));
    this.mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    scene.add(this.mesh);
  }

  drop(x: number, z: number): void {
    const s = this.sparks.find((p) => !p.active) ?? this.sparks[0];
    s.active = true;
    s.x = x;
    s.z = z;
    s.y = 0.8;
    const a = Math.random() * Math.PI * 2;
    s.vx = Math.cos(a) * 2.5;
    s.vz = Math.sin(a) * 2.5;
    s.vy = 4.5;
    s.t = 0;
    s.phase = Math.random() * 6;
  }

  reset(): void {
    this.sparks.forEach((s) => (s.active = false));
    this.mesh.count = 0;
  }

  /** Returns the number collected this frame. */
  update(dt: number, px: number, pz: number, canCollect: boolean, onCollect: (x: number, z: number) => void): number {
    let n = 0;
    let idx = 0;
    for (const s of this.sparks) {
      if (!s.active) continue;
      s.t += dt;
      if (s.t > LIFE) {
        s.active = false;
        continue;
      }
      s.vy -= 14 * dt;
      s.y += s.vy * dt;
      if (s.y < 0.7) {
        s.y = 0.7;
        s.vy = 0;
      }
      s.x += s.vx * dt;
      s.z += s.vz * dt;
      const damp = Math.exp(-4 * dt);
      s.vx *= damp;
      s.vz *= damp;
      const dx = px - s.x;
      const dz = pz - s.z;
      const d = Math.hypot(dx, dz);
      if (canCollect && d < CFG.SPARK_MAGNET) {
        const pull = (1 - d / CFG.SPARK_MAGNET) * 26 + 6;
        s.x += (dx / (d || 1)) * pull * dt;
        s.z += (dz / (d || 1)) * pull * dt;
      }
      if (canCollect && d < 0.95) {
        s.active = false;
        n++;
        onCollect(s.x, s.z);
        continue;
      }
      const fade = s.t > LIFE - 2 ? (Math.sin(s.t * 20) > 0 ? 1 : 0.2) : 1;
      const bob = Math.sin(s.t * 5 + s.phase) * 0.12;
      this.q.setFromEuler(this.e.set(0, s.t * 3 + s.phase, 0));
      this.m4.compose(this.v.set(s.x, s.y + bob, s.z), this.q, this.s.set(fade * 1.2, fade * 1.2, fade * 1.2));
      this.mesh.setMatrixAt(idx, this.m4);
      // gold ↔ pale-sun pulse, over-bright so it reads as glowing
      const k = 0.5 + 0.5 * Math.sin(s.t * 8 + s.phase);
      this.mesh.setColorAt(idx, this.col.setRGB(1.2, 0.8 + k * 0.35, 0.25 + k * 0.5));
      idx++;
    }
    this.mesh.count = idx;
    this.mesh.instanceMatrix.needsUpdate = true;
    if (this.mesh.instanceColor) this.mesh.instanceColor.needsUpdate = true;
    return n;
  }

  dispose(): void {
    this.scene.remove(this.mesh);
    this.mesh.geometry.dispose();
    (this.mesh.material as THREE.Material).dispose();
    this.mesh.dispose();
  }
}
