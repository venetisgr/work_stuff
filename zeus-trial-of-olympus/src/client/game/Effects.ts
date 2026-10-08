import * as THREE from 'three';

const SEGS = 14;
const MAX_PARTICLES = 700;

interface Bolt {
  mesh: THREE.Mesh;
  pos: THREE.BufferAttribute;
  life: number;
  max: number;
  width: number;
  active: boolean;
}

interface Ring {
  mesh: THREE.Mesh;
  life: number;
  max: number;
  radius: number;
  active: boolean;
}

const PARTICLE_VERT = /* glsl */ `
attribute float aSize;
attribute float aAlpha;
attribute vec3 aColor;
varying float vAlpha;
varying vec3 vColor;
uniform float uScale;
void main() {
  vAlpha = aAlpha;
  vColor = aColor;
  vec4 mv = modelViewMatrix * vec4(position, 1.0);
  gl_PointSize = aSize * uScale / -mv.z;
  gl_Position = projectionMatrix * mv;
}`;
const PARTICLE_FRAG = /* glsl */ `
varying float vAlpha;
varying vec3 vColor;
void main() {
  vec2 d = gl_PointCoord - 0.5;
  float r = length(d) * 2.0;
  float a = smoothstep(1.0, 0.0, r) * vAlpha;
  if (a < 0.01) discard;
  gl_FragColor = vec4(vColor * (1.0 + (1.0 - r) * 0.5), a);
}`;

/**
 * All transient visuals: pooled lightning bolts, one pooled particle system,
 * shockwave rings, screen flash and camera shake. Nothing allocates per frame.
 */
export class Effects {
  readonly group = new THREE.Group();
  shake = 0;
  private bolts: Bolt[] = [];
  private rings: Ring[] = [];

  private pPos = new Float32Array(MAX_PARTICLES * 3);
  private pVel = new Float32Array(MAX_PARTICLES * 3);
  private pCol = new Float32Array(MAX_PARTICLES * 3);
  private pSize = new Float32Array(MAX_PARTICLES);
  private pAlpha = new Float32Array(MAX_PARTICLES);
  private pLife = new Float32Array(MAX_PARTICLES);
  private pMax = new Float32Array(MAX_PARTICLES);
  private pGrav = new Float32Array(MAX_PARTICLES);
  private pBase = new Float32Array(MAX_PARTICLES);
  private pCursor = 0;
  private points: THREE.Points;
  private pGeo: THREE.BufferGeometry;
  private pMat: THREE.ShaderMaterial;

  private flashEl: HTMLElement;
  private flash = 0;
  private flashColor = '255,255,255';
  private tmpA = new THREE.Vector3();
  private tmpB = new THREE.Vector3();
  private tmpC = new THREE.Vector3();

  constructor(private scene: THREE.Scene, flashEl: HTMLElement, pointScale: number) {
    this.flashEl = flashEl;
    scene.add(this.group);

    const boltMat = new THREE.MeshBasicMaterial({
      color: 0xcfe9ff,
      transparent: true,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      side: THREE.DoubleSide,
      fog: false,
    });
    for (let i = 0; i < 18; i++) {
      // 2 crossed ribbons × SEGS quads
      const geo = new THREE.BufferGeometry();
      const pos = new THREE.BufferAttribute(new Float32Array(SEGS * 2 * 4 * 3), 3);
      pos.setUsage(THREE.DynamicDrawUsage);
      geo.setAttribute('position', pos);
      const idx: number[] = [];
      for (let q = 0; q < SEGS * 2; q++) {
        const o = q * 4;
        idx.push(o, o + 1, o + 2, o + 2, o + 1, o + 3);
      }
      geo.setIndex(idx);
      const mesh = new THREE.Mesh(geo, boltMat.clone());
      mesh.frustumCulled = false;
      mesh.visible = false;
      mesh.renderOrder = 10;
      this.group.add(mesh);
      this.bolts.push({ mesh, pos, life: 0, max: 0.3, width: 0.3, active: false });
    }

    const ringGeo = new THREE.RingGeometry(0.85, 1, 40);
    ringGeo.rotateX(-Math.PI / 2);
    for (let i = 0; i < 14; i++) {
      const mesh = new THREE.Mesh(
        ringGeo,
        new THREE.MeshBasicMaterial({ color: 0xaad4ff, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, fog: false }),
      );
      mesh.visible = false;
      mesh.renderOrder = 9;
      this.group.add(mesh);
      this.rings.push({ mesh, life: 0, max: 0.5, radius: 3, active: false });
    }

    this.pGeo = new THREE.BufferGeometry();
    this.pGeo.setAttribute('position', new THREE.BufferAttribute(this.pPos, 3).setUsage(THREE.DynamicDrawUsage));
    this.pGeo.setAttribute('aColor', new THREE.BufferAttribute(this.pCol, 3).setUsage(THREE.DynamicDrawUsage));
    this.pGeo.setAttribute('aSize', new THREE.BufferAttribute(this.pSize, 1).setUsage(THREE.DynamicDrawUsage));
    this.pGeo.setAttribute('aAlpha', new THREE.BufferAttribute(this.pAlpha, 1).setUsage(THREE.DynamicDrawUsage));
    this.pMat = new THREE.ShaderMaterial({
      vertexShader: PARTICLE_VERT,
      fragmentShader: PARTICLE_FRAG,
      uniforms: { uScale: { value: pointScale } },
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    this.points = new THREE.Points(this.pGeo, this.pMat);
    this.points.frustumCulled = false;
    this.points.renderOrder = 8;
    this.group.add(this.points);
    this.pLife.fill(0);
  }

  setPointScale(s: number): void {
    this.pMat.uniforms.uScale.value = s;
  }

  /** Jagged bolt from `from` to `to`. `power` 0..1 thickens it and adds a brighter core. */
  bolt(from: THREE.Vector3, to: THREE.Vector3, power = 0.3, life = 0.28, color = 0xcfe9ff): void {
    const b = this.bolts.find((x) => !x.active) ?? this.bolts.reduce((a, c) => (c.life < a.life ? c : a));
    b.active = true;
    b.life = b.max = life;
    b.width = 0.12 + power * 0.34;
    (b.mesh.material as THREE.MeshBasicMaterial).color.setHex(color);
    b.mesh.visible = true;
    this.buildBolt(b, from, to, 0.35 + power * 0.5);
  }

  private buildBolt(b: Bolt, from: THREE.Vector3, to: THREE.Vector3, jitter: number): void {
    const dir = this.tmpA.copy(to).sub(from);
    const len = dir.length();
    dir.divideScalar(len || 1);
    // two perpendicular axes for the cross ribbons + jitter
    const up = Math.abs(dir.y) > 0.95 ? this.tmpB.set(1, 0, 0) : this.tmpB.set(0, 1, 0);
    const p1 = this.tmpC.copy(dir).cross(up).normalize();
    const p2 = up.copy(dir).cross(p1).normalize();
    const arr = b.pos.array as Float32Array;
    let px = from.x,
      py = from.y,
      pz = from.z;
    const w = b.width;
    for (let i = 0; i < SEGS; i++) {
      const t = (i + 1) / SEGS;
      const last = i === SEGS - 1;
      const taper = Math.sin(Math.PI * Math.min(1, t * 0.9 + 0.1)) * 0.5 + 0.5;
      const j = last ? 0 : jitter * (1 - t * 0.4);
      const nx = from.x + dir.x * len * t + (p1.x * (Math.random() - 0.5) + p2.x * (Math.random() - 0.5)) * j * 2;
      const ny = from.y + dir.y * len * t + (p1.y * (Math.random() - 0.5) + p2.y * (Math.random() - 0.5)) * j * 2;
      const nz = from.z + dir.z * len * t + (p1.z * (Math.random() - 0.5) + p2.z * (Math.random() - 0.5)) * j * 2;
      for (let r = 0; r < 2; r++) {
        const ax = r === 0 ? p1 : p2;
        const q = (i * 2 + r) * 12;
        const hw = w * taper * 0.5;
        arr[q] = px - ax.x * hw;
        arr[q + 1] = py - ax.y * hw;
        arr[q + 2] = pz - ax.z * hw;
        arr[q + 3] = px + ax.x * hw;
        arr[q + 4] = py + ax.y * hw;
        arr[q + 5] = pz + ax.z * hw;
        arr[q + 6] = nx - ax.x * hw;
        arr[q + 7] = ny - ax.y * hw;
        arr[q + 8] = nz - ax.z * hw;
        arr[q + 9] = nx + ax.x * hw;
        arr[q + 10] = ny + ax.y * hw;
        arr[q + 11] = nz + ax.z * hw;
      }
      px = nx;
      py = ny;
      pz = nz;
    }
    b.pos.needsUpdate = true;
  }

  /** Sky-to-ground strike. */
  strike(x: number, z: number, power = 0.3, color = 0xcfe9ff): void {
    this.tmpB.set(x + (Math.random() - 0.5) * 5, 34, z + (Math.random() - 0.5) * 5);
    this.tmpC.set(x, 0.1, z);
    this.bolt(this.tmpB, this.tmpC, power, 0.26 + power * 0.18, color);
    this.impact(x, z, power, color);
  }

  impact(x: number, z: number, power = 0.3, color = 0xcfe9ff): void {
    this.ring(x, z, 1.6 + power * 3.5, 0.35 + power * 0.2, color);
    const n = 8 + Math.floor(power * 16);
    for (let i = 0; i < n; i++) {
      const a = Math.random() * Math.PI * 2;
      const sp = 2 + Math.random() * 6 * (0.6 + power);
      this.spawn(x, 0.3, z, Math.cos(a) * sp, 2 + Math.random() * 5, Math.sin(a) * sp, color, 0.35 + Math.random() * 0.35, 0.5 + Math.random() * 0.5, 14);
    }
  }

  ring(x: number, z: number, radius: number, life = 0.5, color = 0xaad4ff): void {
    const r = this.rings.find((q) => !q.active) ?? this.rings[0];
    r.active = true;
    r.life = r.max = life;
    r.radius = radius;
    r.mesh.position.set(x, 0.12, z);
    (r.mesh.material as THREE.MeshBasicMaterial).color.setHex(color);
    r.mesh.visible = true;
  }

  /** Enemy dissolve / generic burst. */
  burst(x: number, y: number, z: number, count: number, color: number, speed = 4, size = 0.5, life = 0.7): void {
    for (let i = 0; i < count; i++) {
      const a = Math.random() * Math.PI * 2;
      const e = Math.random() * 2 - 1;
      const s = speed * (0.4 + Math.random() * 0.8);
      this.spawn(x, y, z, Math.cos(a) * s, e * s + 1.5, Math.sin(a) * s, color, life * (0.6 + Math.random() * 0.6), size * (0.6 + Math.random() * 0.8), 7);
    }
  }

  spawn(x: number, y: number, z: number, vx: number, vy: number, vz: number, color: number, life: number, size: number, gravity: number): void {
    const i = this.pCursor;
    this.pCursor = (this.pCursor + 1) % MAX_PARTICLES;
    const o = i * 3;
    this.pPos[o] = x;
    this.pPos[o + 1] = y;
    this.pPos[o + 2] = z;
    this.pVel[o] = vx;
    this.pVel[o + 1] = vy;
    this.pVel[o + 2] = vz;
    this.pCol[o] = ((color >> 16) & 255) / 255;
    this.pCol[o + 1] = ((color >> 8) & 255) / 255;
    this.pCol[o + 2] = (color & 255) / 255;
    this.pLife[i] = this.pMax[i] = life;
    this.pBase[i] = size;
    this.pGrav[i] = gravity;
  }

  screenFlash(amount: number, color = '255,255,255'): void {
    if (amount > this.flash) {
      this.flash = amount;
      this.flashColor = color;
    }
  }

  addShake(v: number): void {
    this.shake = Math.min(1.4, this.shake + v);
  }

  update(dt: number): void {
    for (const b of this.bolts) {
      if (!b.active) continue;
      b.life -= dt;
      if (b.life <= 0) {
        b.active = false;
        b.mesh.visible = false;
        continue;
      }
      const k = b.life / b.max;
      (b.mesh.material as THREE.MeshBasicMaterial).opacity = Math.min(1, k * 1.6) * (Math.random() > 0.15 ? 1 : 0.4);
    }
    for (const r of this.rings) {
      if (!r.active) continue;
      r.life -= dt;
      if (r.life <= 0) {
        r.active = false;
        r.mesh.visible = false;
        continue;
      }
      const k = 1 - r.life / r.max;
      const s = r.radius * (1 - Math.pow(1 - k, 3));
      r.mesh.scale.set(s, 1, s);
      (r.mesh.material as THREE.MeshBasicMaterial).opacity = (1 - k) * 0.9;
    }
    for (let i = 0; i < MAX_PARTICLES; i++) {
      if (this.pLife[i] <= 0) {
        this.pAlpha[i] = 0;
        this.pSize[i] = 0;
        continue;
      }
      this.pLife[i] -= dt;
      const o = i * 3;
      this.pVel[o + 1] -= this.pGrav[i] * dt;
      this.pPos[o] += this.pVel[o] * dt;
      this.pPos[o + 1] += this.pVel[o + 1] * dt;
      this.pPos[o + 2] += this.pVel[o + 2] * dt;
      if (this.pPos[o + 1] < 0.05 && this.pVel[o + 1] < 0) {
        this.pPos[o + 1] = 0.05;
        this.pVel[o + 1] *= -0.3;
      }
      const k = Math.max(0, this.pLife[i] / this.pMax[i]);
      this.pAlpha[i] = k;
      this.pSize[i] = this.pBase[i] * (0.4 + k * 0.6) * 0.55;
    }
    (this.pGeo.attributes.position as THREE.BufferAttribute).needsUpdate = true;
    (this.pGeo.attributes.aColor as THREE.BufferAttribute).needsUpdate = true;
    (this.pGeo.attributes.aSize as THREE.BufferAttribute).needsUpdate = true;
    (this.pGeo.attributes.aAlpha as THREE.BufferAttribute).needsUpdate = true;

    this.shake = Math.max(0, this.shake - dt * 2.6);
    if (this.flash > 0.002) {
      this.flash = Math.max(0, this.flash - dt * 3.2);
      this.flashEl.style.background = `rgba(${this.flashColor},${this.flash.toFixed(3)})`;
    } else if (this.flash !== 0) {
      this.flash = 0;
      this.flashEl.style.background = 'transparent';
    }
  }

  /** Clears everything for an instant restart. */
  reset(): void {
    this.pLife.fill(0);
    for (const b of this.bolts) {
      b.active = false;
      b.mesh.visible = false;
    }
    for (const r of this.rings) {
      r.active = false;
      r.mesh.visible = false;
    }
    this.shake = 0;
    this.flash = 0;
    this.flashEl.style.background = 'transparent';
  }

  dispose(): void {
    this.scene.remove(this.group);
    this.group.traverse((o) => {
      const m = o as THREE.Mesh;
      m.geometry?.dispose();
      const mat = m.material as THREE.Material | undefined;
      mat?.dispose();
    });
  }
}
