import * as THREE from 'three';
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js';
import { CFG } from './config';
import type { Effects } from './Effects';

export type PickupKind = 'spark' | 'wine';

interface Item {
  active: boolean;
  kind: PickupKind;
  x: number;
  y: number;
  z: number;
  vx: number;
  vz: number;
  vy: number;
  t: number;
  phase: number;
  trail: number;
}

const MAX_SPARKS = 48;
const MAX_WINE = 6;
const LIFE = { spark: 10, wine: 16 } as const;

/** A gold drinking cup (kylix) with a glowing red wine surface. */
function kylixGeometry(): THREE.BufferGeometry {
  const col = (g: THREE.BufferGeometry, hex: number) => {
    const c = new THREE.Color(hex);
    const arr = new Float32Array(g.attributes.position.count * 3);
    for (let i = 0; i < arr.length; i += 3) {
      arr[i] = c.r;
      arr[i + 1] = c.g;
      arr[i + 2] = c.b;
    }
    g.setAttribute('color', new THREE.BufferAttribute(arr, 3));
    const ni = g.index ? g.toNonIndexed() : g;
    if (!ni.attributes.uv) ni.setAttribute('uv', new THREE.BufferAttribute(new Float32Array(ni.attributes.position.count * 2), 2));
    return ni;
  };
  const base = new THREE.CylinderGeometry(0.22, 0.26, 0.06, 14);
  base.translate(0, 0.03, 0);
  const stem = new THREE.CylinderGeometry(0.05, 0.07, 0.3, 8);
  stem.translate(0, 0.21, 0);
  const bowl = new THREE.CylinderGeometry(0.42, 0.14, 0.26, 16, 1, true);
  bowl.translate(0, 0.49, 0);
  const rim = new THREE.TorusGeometry(0.42, 0.03, 6, 18);
  rim.rotateX(Math.PI / 2);
  rim.translate(0, 0.62, 0);
  const wine = new THREE.CircleGeometry(0.39, 16);
  wine.rotateX(-Math.PI / 2);
  wine.translate(0, 0.57, 0);
  const handleL = new THREE.TorusGeometry(0.13, 0.025, 5, 10, Math.PI);
  handleL.rotateZ(Math.PI / 2);
  handleL.translate(-0.46, 0.52, 0);
  const handleR = handleL.clone();
  handleR.rotateY(Math.PI);
  const merged = mergeGeometries([col(base, 0xe8b030), col(stem, 0xe8b030), col(bowl, 0xf0c040), col(rim, 0xffe080), col(wine, 0xff2a3a), col(handleL, 0xe8b030), col(handleR, 0xe8b030)], false)!;
  merged.computeVertexNormals();
  return merged;
}

/** Divine Sparks (Divine Charge) and wine cups (health): pooled, magnetised toward Zeus. */
export class Pickups {
  private items: Item[] = [];
  private sparkMesh: THREE.InstancedMesh;
  private wineMesh: THREE.InstancedMesh;
  private m4 = new THREE.Matrix4();
  private q = new THREE.Quaternion();
  private e = new THREE.Euler();
  private v = new THREE.Vector3();
  private s = new THREE.Vector3();
  private col = new THREE.Color();

  constructor(private scene: THREE.Scene, private fx: Effects) {
    for (let i = 0; i < MAX_SPARKS + MAX_WINE; i++) {
      this.items.push({ active: false, kind: i < MAX_SPARKS ? 'spark' : 'wine', x: 0, y: 0, z: 0, vx: 0, vz: 0, vy: 0, t: 0, phase: 0, trail: 0 });
    }
    this.sparkMesh = new THREE.InstancedMesh(new THREE.OctahedronGeometry(0.17, 0), new THREE.MeshBasicMaterial({ color: 0xffffff, fog: false }), MAX_SPARKS);
    this.wineMesh = new THREE.InstancedMesh(kylixGeometry(), new THREE.MeshBasicMaterial({ vertexColors: true, fog: false, side: THREE.DoubleSide }), MAX_WINE);
    for (const m of [this.sparkMesh, this.wineMesh]) {
      m.frustumCulled = false;
      m.count = 0;
      m.setColorAt(0, this.col.set(0xffffff));
      m.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      scene.add(m);
    }
  }

  get wineCount(): number {
    let n = 0;
    for (const i of this.items) if (i.active && i.kind === 'wine') n++;
    return n;
  }

  private spawn(kind: PickupKind, x: number, z: number): void {
    const range = kind === 'spark' ? [0, MAX_SPARKS] : [MAX_SPARKS, MAX_SPARKS + MAX_WINE];
    let s: Item | undefined;
    for (let i = range[0]; i < range[1]; i++) if (!this.items[i].active) { s = this.items[i]; break; }
    s ??= this.items[range[0]];
    s.active = true;
    s.x = x;
    s.z = z;
    s.y = 0.8;
    const a = Math.random() * Math.PI * 2;
    s.vx = Math.cos(a) * 2.5;
    s.vz = Math.sin(a) * 2.5;
    s.vy = 4.5;
    s.t = 0;
    s.trail = 0;
    s.phase = Math.random() * 6;
  }

  drop(x: number, z: number): void {
    this.spawn('spark', x, z);
  }

  dropWine(x: number, z: number): void {
    this.spawn('wine', x, z);
    this.fx.ring(x, z, 2.4, 0.6, 0xff5060);
  }

  reset(): void {
    this.items.forEach((s) => (s.active = false));
    this.sparkMesh.count = 0;
    this.wineMesh.count = 0;
  }

  update(dt: number, px: number, pz: number, canCollect: boolean, onCollect: (kind: PickupKind, x: number, z: number) => boolean): void {
    let si = 0;
    let wi = 0;
    for (const s of this.items) {
      if (!s.active) continue;
      s.t += dt;
      const life = LIFE[s.kind];
      if (s.t > life) {
        s.active = false;
        continue;
      }
      const wine = s.kind === 'wine';
      s.vy -= 14 * dt;
      s.y += s.vy * dt;
      const floor = wine ? 0.1 : 0.7;
      if (s.y < floor) {
        s.y = floor;
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
      const magnet = wine ? CFG.SPARK_MAGNET * 0.8 : CFG.SPARK_MAGNET;
      if (canCollect && d < magnet) {
        const pull = (1 - d / magnet) * 26 + 6;
        s.x += (dx / (d || 1)) * pull * dt;
        s.z += (dz / (d || 1)) * pull * dt;
      }
      if (canCollect && d < (wine ? 1.1 : 0.95)) {
        // The callback may refuse (e.g. already at full health): the cup stays on the floor.
        if (onCollect(s.kind, s.x, s.z)) {
          s.active = false;
          continue;
        }
      }
      const blink = s.t > life - 2 ? (Math.sin(s.t * 20) > 0 ? 1 : 0.25) : 1;
      if (wine) {
        s.trail -= dt;
        if (s.trail <= 0) {
          s.trail = 0.09;
          this.fx.spawn(s.x + (Math.random() - 0.5) * 0.5, s.y + 0.7, s.z + (Math.random() - 0.5) * 0.5, 0, 1.3, 0, Math.random() < 0.5 ? 0xff4a5a : 0xffd070, 0.8, 0.45, -0.5);
        }
        const bob = Math.sin(s.t * 4 + s.phase) * 0.1;
        this.q.setFromEuler(this.e.set(0, s.t * 2 + s.phase, 0));
        this.m4.compose(this.v.set(s.x, s.y + 0.35 + bob, s.z), this.q, this.s.setScalar(1.7 * blink));
        this.wineMesh.setMatrixAt(wi, this.m4);
        this.wineMesh.setColorAt(wi, this.col.setRGB(1, 1, 1));
        wi++;
      } else {
        const bob = Math.sin(s.t * 5 + s.phase) * 0.12;
        this.q.setFromEuler(this.e.set(0, s.t * 3 + s.phase, 0));
        this.m4.compose(this.v.set(s.x, s.y + bob, s.z), this.q, this.s.set(blink, blink * 1.4, blink));
        this.sparkMesh.setMatrixAt(si, this.m4);
        const k = 0.5 + 0.5 * Math.sin(s.t * 8 + s.phase);
        this.sparkMesh.setColorAt(si, this.col.setRGB(1 - k * 0.55, 0.78 + k * 0.18, 0.25 + k * 0.75));
        si++;
      }
    }
    this.sparkMesh.count = si;
    this.wineMesh.count = wi;
    for (const m of [this.sparkMesh, this.wineMesh]) {
      m.instanceMatrix.needsUpdate = true;
      if (m.instanceColor) m.instanceColor.needsUpdate = true;
    }
  }

  dispose(): void {
    for (const m of [this.sparkMesh, this.wineMesh]) {
      this.scene.remove(m);
      m.geometry.dispose();
      (m.material as THREE.Material).dispose();
      m.dispose();
    }
  }
}
