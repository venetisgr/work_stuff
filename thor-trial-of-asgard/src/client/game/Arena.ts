import * as THREE from 'three';
import { CFG } from './config';
import type { Effects } from './Effects';

const GOLD = 0xb8c8d8; // cold steel

function canvasTex(size: number, draw: (ctx: CanvasRenderingContext2D, s: number) => void): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = c.height = size;
  draw(c.getContext('2d')!, size);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}

function stoneCracks(ctx: CanvasRenderingContext2D, s: number, n: number): void {
  ctx.lineCap = 'round';
  for (let i = 0; i < n; i++) {
    ctx.strokeStyle = `rgba(${30 + Math.random() * 30},${40 + Math.random() * 30},${55 + Math.random() * 30},${0.08 + Math.random() * 0.14})`;
    ctx.lineWidth = 0.5 + Math.random() * 2;
    ctx.beginPath();
    let x = Math.random() * s;
    let y = Math.random() * s;
    ctx.moveTo(x, y);
    for (let k = 0; k < 6; k++) {
      x += (Math.random() - 0.3) * s * 0.18;
      y += (Math.random() - 0.5) * s * 0.18;
      ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
}

function floorTexture(): THREE.CanvasTexture {
  return canvasTex(1024, (ctx, s) => {
    const c = s / 2;
    ctx.fillStyle = '#7d8794';
    ctx.fillRect(0, 0, s, s);
    stoneCracks(ctx, s, 140);
    // rings
    const ring = (r: number, w: number, col: string) => {
      ctx.strokeStyle = col;
      ctx.lineWidth = w;
      ctx.beginPath();
      ctx.arc(c, c, r * c, 0, Math.PI * 2);
      ctx.stroke();
    };
    ring(0.985, 14, '#6e7f93');
    ring(0.93, 4, '#8fc8f0');
    ring(0.78, 4, '#8fc8f0');
    ring(0.4, 3, '#7fb8e0');
    ring(0.18, 3, '#7fb8e0');
    // rune band between r=0.80..0.92 (Elder Futhark)
    const RUNES = 'ᚠᚢᚦᚨᚱᚲᚷᚹᚺᚾᛁᛃᛇᛈᛉᛊᛏᛒᛖᛗᛚᛜᛞᛟ';
    const N = 48;
    ctx.save();
    ctx.translate(c, c);
    ctx.fillStyle = '#9fd8ff';
    ctx.shadowColor = '#4aa8ff';
    ctx.shadowBlur = 8;
    ctx.font = `${Math.round(0.1 * c)}px serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    for (let i = 0; i < N; i++) {
      ctx.save();
      ctx.rotate((i / N) * Math.PI * 2);
      ctx.translate(0, -0.86 * c);
      ctx.fillText(RUNES[i % RUNES.length], 0, 0);
      ctx.restore();
    }
    ctx.restore();
    // sunburst
    ctx.save();
    ctx.translate(c, c);
    for (let i = 0; i < 24; i++) {
      ctx.rotate((Math.PI * 2) / 24);
      ctx.fillStyle = i % 2 ? 'rgba(120,190,255,0.5)' : 'rgba(40,70,120,0.3)';
      ctx.beginPath();
      ctx.moveTo(0.19 * c, -0.015 * c);
      ctx.lineTo(0.39 * c, -0.05 * c);
      ctx.lineTo(0.39 * c, 0.05 * c);
      ctx.lineTo(0.19 * c, 0.015 * c);
      ctx.fill();
    }
    // Mjölnir emblem
    ctx.fillStyle = '#4b5a6e';
    ctx.strokeStyle = '#d8e6f2';
    ctx.lineWidth = 3;
    ctx.fillRect(-0.09 * c, -0.13 * c, 0.18 * c, 0.1 * c);
    ctx.strokeRect(-0.09 * c, -0.13 * c, 0.18 * c, 0.1 * c);
    ctx.fillStyle = '#8a5a2e';
    ctx.fillRect(-0.015 * c, -0.03 * c, 0.03 * c, 0.16 * c);
    ctx.restore();
  });
}

function skyTexture(): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = 4;
  c.height = 256;
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

function cloudTexture(): THREE.CanvasTexture {
  return canvasTex(128, (ctx, s) => {
    const g = ctx.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
    g.addColorStop(0, 'rgba(255,255,255,0.95)');
    g.addColorStop(0.5, 'rgba(255,255,255,0.45)');
    g.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, s, s);
  });
}

const lerpC = (out: THREE.Color, a: THREE.Color, b: THREE.Color, t: number) => out.copy(a).lerp(b, t);

export class Arena {
  readonly group = new THREE.Group();
  readonly hemi: THREE.HemisphereLight;
  readonly sun: THREE.DirectionalLight;
  /** One shared point light used for strike flashes near the action. */
  readonly strikeLight: THREE.PointLight;

  private skyCanvas: HTMLCanvasElement;
  private skyTex: THREE.CanvasTexture;
  private clouds = new THREE.Group();
  private cloudMat: THREE.SpriteMaterial;
  private flames: THREE.Mesh[] = [];
  private halos: THREE.Sprite[] = [];
  private mountains: THREE.MeshLambertMaterial;
  private skyFlash = 0;
  private nextBolt = 3;
  private lastStorm = -1;

  private cTop = new THREE.Color();
  private cBot = new THREE.Color();
  private tmp = new THREE.Color();
  private skyDay = [new THREE.Color(0x0e2444), new THREE.Color(0x5a8fa8)];
  private skyStorm = [new THREE.Color(0x0d1226), new THREE.Color(0x3a4468)];
  private skyWrath = [new THREE.Color(0x2a0f24), new THREE.Color(0x8a3a3a)];

  constructor(private scene: THREE.Scene, private fx: Effects, shadowSize: number) {
    scene.add(this.group);
    scene.fog = new THREE.Fog(0x5a8fa8, 45, 150);

    // Sky dome
    this.skyTex = skyTexture();
    this.skyCanvas = this.skyTex.image as HTMLCanvasElement;
    const sky = new THREE.Mesh(
      new THREE.SphereGeometry(300, 24, 16),
      new THREE.MeshBasicMaterial({ map: this.skyTex, side: THREE.BackSide, fog: false, depthWrite: false }),
    );
    sky.renderOrder = -10;
    this.group.add(sky);

    // Lights
    this.hemi = new THREE.HemisphereLight(0xcfe6ff, 0x8a7a5a, 1.15);
    this.group.add(this.hemi);
    this.sun = new THREE.DirectionalLight(0xfff1d0, 2.1);
    this.sun.position.set(-9, 18, 10);
    this.sun.castShadow = true;
    this.sun.shadow.mapSize.set(shadowSize, shadowSize);
    const sc = this.sun.shadow.camera;
    sc.left = -9;
    sc.right = 9;
    sc.top = 9;
    sc.bottom = -9;
    sc.near = 1;
    sc.far = 50;
    this.sun.shadow.bias = -0.0008;
    this.group.add(this.sun, this.sun.target);
    this.strikeLight = new THREE.PointLight(0xaed4ff, 0, 24, 1.6);
    this.strikeLight.position.set(0, 5, 0);
    this.group.add(this.strikeLight);

    this.buildPlatform();
    this.buildStones();
    this.buildBraziers();

    this.cloudMat = new THREE.SpriteMaterial({ map: cloudTexture(), transparent: true, depthWrite: false, opacity: 0.9, fog: false });
    this.buildClouds();
    this.mountains = new THREE.MeshLambertMaterial({ color: 0x8fa6b8, flatShading: true });
    this.buildBackdrop();
    this.setStorm(0, 0);
  }

  private buildPlatform(): void {
    const R = CFG.ARENA_RADIUS + 0.8;
    const floor = new THREE.MeshStandardMaterial({ map: floorTexture(), roughness: 0.45, metalness: 0.05 });
    const side = new THREE.MeshStandardMaterial({ color: 0x6c7684, roughness: 0.8 });
    const slab = new THREE.Mesh(new THREE.CylinderGeometry(R, R, 1.4, 64), [side, floor, side]);
    slab.position.y = -0.7;
    slab.receiveShadow = true;
    this.group.add(slab);

    // gold rim
    const rim = new THREE.Mesh(new THREE.TorusGeometry(R, 0.14, 8, 64), new THREE.MeshStandardMaterial({ color: GOLD, metalness: 0.9, roughness: 0.3 }));
    rim.rotation.x = Math.PI / 2;
    rim.position.y = 0.02;
    this.group.add(rim);

    // floating rock underneath
    const rockMat = new THREE.MeshLambertMaterial({ color: 0x5a5a68, flatShading: true });
    const rock = new THREE.Mesh(new THREE.ConeGeometry(R * 0.95, 20, 9, 3), rockMat);
    rock.rotation.x = Math.PI;
    rock.position.y = -11.4;
    const pos = rock.geometry.attributes.position;
    for (let i = 0; i < pos.count; i++) {
      pos.setX(i, pos.getX(i) + (Math.sin(i * 12.9) * 0.5) * 1.2);
      pos.setZ(i, pos.getZ(i) + (Math.cos(i * 7.7) * 0.5) * 1.2);
    }
    rock.geometry.computeVertexNormals();
    this.group.add(rock);
  }

  private buildStones(): void {
    const N = 14;
    const R = CFG.ARENA_RADIUS + 0.2;
    const marble = new THREE.MeshStandardMaterial({ color: 0x8b95a1, roughness: 0.9 });
    const shaftGeo = new THREE.CylinderGeometry(0.55, 0.65, 1, 14, 1);
    const baseGeo = new THREE.BoxGeometry(1.7, 0.4, 1.7);
    const capGeo = new THREE.BoxGeometry(1.7, 0.4, 1.7);
    const shafts = new THREE.InstancedMesh(shaftGeo, marble, N);
    const bases = new THREE.InstancedMesh(baseGeo, marble, N);
    const caps = new THREE.InstancedMesh(capGeo, marble, N);
    shafts.castShadow = bases.castShadow = caps.castShadow = true;
    const m = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    const s = new THREE.Vector3();
    const p = new THREE.Vector3();
    let nCaps = 0;
    const rubble: THREE.Vector3[] = [];
    const hold: Array<{ a: number; h: number }> = [];
    for (let i = 0; i < N; i++) {
      const a = (i / N) * Math.PI * 2;
      // columns on the camera side are kept low so they never hide Thor
      const broken = i % 5 === 2 || i === 11 || Math.sin(a) > 0.25;
      const h = broken ? 1.4 + (i % 3) * 0.8 : 6.2;
      hold.push({ a, h });
      p.set(Math.cos(a) * R, 0.2, Math.sin(a) * R);
      q.setFromEuler(new THREE.Euler(0, -a, 0));
      m.compose(p, q, s.set(1, 1, 1));
      bases.setMatrixAt(i, m);
      p.set(Math.cos(a) * R, 0.4 + h / 2, Math.sin(a) * R);
      m.compose(p, q, s.set(1, h, 1));
      shafts.setMatrixAt(i, m);
      if (!broken) {
        p.set(Math.cos(a) * R, 0.4 + h + 0.2, Math.sin(a) * R);
        m.compose(p, q, s.set(1, 1, 1));
        caps.setMatrixAt(nCaps++, m);
      } else {
        rubble.push(new THREE.Vector3(Math.cos(a) * (R - 1.8), 0.3, Math.sin(a) * (R - 1.8)));
      }
    }
    caps.count = nCaps;
    this.group.add(shafts, bases, caps);

    // architrave beams between neighbouring intact columns
    const beams: THREE.Matrix4[] = [];
    for (let i = 0; i < N; i++) {
      const j = (i + 1) % N;
      if (hold[i].h < 6 || hold[j].h < 6) continue;
      const a1 = hold[i].a;
      const a2 = hold[j].a;
      const x1 = Math.cos(a1) * R,
        z1 = Math.sin(a1) * R,
        x2 = Math.cos(a2) * R,
        z2 = Math.sin(a2) * R;
      const len = Math.hypot(x2 - x1, z2 - z1);
      const mm = new THREE.Matrix4();
      mm.compose(
        new THREE.Vector3((x1 + x2) / 2, 0.4 + 6.2 + 0.6, (z1 + z2) / 2),
        new THREE.Quaternion().setFromEuler(new THREE.Euler(0, -Math.atan2(z2 - z1, x2 - x1), 0)),
        new THREE.Vector3(len, 0.7, 1.1),
      );
      beams.push(mm);
    }
    const beamMesh = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 1, 1), marble, Math.max(1, beams.length));
    beams.forEach((mm, i) => beamMesh.setMatrixAt(i, mm));
    beamMesh.count = beams.length;
    beamMesh.castShadow = true;
    this.group.add(beamMesh);

    // rubble
    const rubbleMesh = new THREE.InstancedMesh(new THREE.DodecahedronGeometry(0.5, 0), marble, rubble.length * 3);
    let k = 0;
    for (const r of rubble) {
      for (let n = 0; n < 3; n++) {
        const sc = 0.5 + Math.random() * 0.7;
        m.compose(
          new THREE.Vector3(r.x + (Math.random() - 0.5) * 1.8, 0.2 + sc * 0.25, r.z + (Math.random() - 0.5) * 1.8),
          new THREE.Quaternion().setFromEuler(new THREE.Euler(Math.random() * 3, Math.random() * 3, Math.random() * 3)),
          new THREE.Vector3(sc, sc * 0.7, sc),
        );
        rubbleMesh.setMatrixAt(k++, m);
      }
    }
    rubbleMesh.castShadow = true;
    this.group.add(rubbleMesh);
  }

  private buildBraziers(): void {
    const bronze = new THREE.MeshStandardMaterial({ color: 0x3a3a44, metalness: 0.7, roughness: 0.4 });
    const flameMat = new THREE.MeshBasicMaterial({ color: 0x7ad0ff, transparent: true, opacity: 0.9, blending: THREE.AdditiveBlending, depthWrite: false });
    const haloMat = new THREE.SpriteMaterial({ map: cloudTexture(), color: 0x5ac0ff, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, opacity: 0.8 });
    for (let i = 0; i < 4; i++) {
      const a = Math.PI / 4 + (i * Math.PI) / 2;
      const x = Math.cos(a) * 11.5;
      const z = Math.sin(a) * 11.5;
      const stand = new THREE.Mesh(new THREE.CylinderGeometry(0.18, 0.3, 1.5, 8), bronze);
      stand.position.set(x, 0.75, z);
      const bowl = new THREE.Mesh(new THREE.CylinderGeometry(0.75, 0.35, 0.45, 12), bronze);
      bowl.position.set(x, 1.6, z);
      stand.castShadow = bowl.castShadow = true;
      const flame = new THREE.Mesh(new THREE.ConeGeometry(0.5, 1.5, 8), flameMat);
      flame.position.set(x, 2.4, z);
      const halo = new THREE.Sprite(haloMat);
      halo.scale.set(4, 4, 1);
      halo.position.set(x, 2.3, z);
      this.group.add(stand, bowl, flame, halo);
      this.flames.push(flame);
      this.halos.push(halo);
    }
  }

  private buildClouds(): void {
    const rnd = (a: number, b: number) => a + Math.random() * (b - a);
    for (let i = 0; i < 46; i++) {
      const s = new THREE.Sprite(this.cloudMat);
      const a = Math.random() * Math.PI * 2;
      const lower = i < 30;
      const r = lower ? rnd(14, 70) : rnd(70, 140);
      s.position.set(Math.cos(a) * r, lower ? rnd(-9, -3) : rnd(-4, 14), Math.sin(a) * r);
      const sz = lower ? rnd(16, 34) : rnd(30, 60);
      s.scale.set(sz, sz * 0.5, 1);
      this.clouds.add(s);
    }
    this.group.add(this.clouds);
  }

  private buildBackdrop(): void {
    const peaks: Array<[number, number, number, number]> = [
      [-60, -110, 34, 70],
      [-10, -140, 70, 110],
      [55, -120, 30, 60],
      [100, -60, 28, 52],
      [-105, -50, 26, 48],
      [-90, 40, 22, 40],
      [95, 50, 24, 44],
    ];
    for (const [x, z, r, h] of peaks) {
      const m = new THREE.Mesh(new THREE.ConeGeometry(r, h, 7, 2), this.mountains);
      m.position.set(x, h / 2 - 22, z);
      m.rotation.y = x;
      this.group.add(m);
    }
    // Great hall (Valhalla) on the peak
    const marble = new THREE.MeshStandardMaterial({ color: 0x6b4a2e, roughness: 0.8, emissive: 0x3a2410, emissiveIntensity: 0.25 });
    const gold = new THREE.MeshStandardMaterial({ color: GOLD, metalness: 0.85, roughness: 0.3, emissive: 0x5a3a08, emissiveIntensity: 0.5 });
    const t = new THREE.Group();
    t.position.set(-10, 88 - 22, -140);
    t.add(new THREE.Mesh(new THREE.BoxGeometry(30, 2, 14), marble));
    for (let i = 0; i < 8; i++) {
      const c = new THREE.Mesh(new THREE.CylinderGeometry(0.9, 1, 9, 10), marble);
      c.position.set(-12.5 + i * 3.57, 5.5, 5.5);
      t.add(c);
    }
    const roof = new THREE.Mesh(new THREE.BoxGeometry(31, 1.2, 15), marble);
    roof.position.y = 10.6;
    const ped = new THREE.Mesh(new THREE.ConeGeometry(11.5, 4.5, 3), gold);
    ped.rotation.set(Math.PI / 2, 0, 0);
    ped.scale.set(1.35, 0.1, 1);
    ped.position.set(0, 13.4, 6);
    ped.rotation.set(0, 0, 0);
    ped.scale.set(1.4, 1, 0.12);
    t.add(roof, ped);
    t.scale.setScalar(1.3);
    this.group.add(t);
  }

  /** Atmosphere driven by run progress. storm 0..1; wrath 0..1 (final seconds). */
  setStorm(storm: number, wrath: number): void {
    const key = Math.round(storm * 200) + wrath * 7;
    if (key === this.lastStorm) return;
    this.lastStorm = key;
    const topDay = this.skyDay[0];
    lerpC(this.cTop, topDay, this.skyStorm[0], storm);
    lerpC(this.cBot, this.skyDay[1], this.skyStorm[1], storm);
    this.cTop.lerp(this.skyWrath[0], wrath * 0.7);
    this.cBot.lerp(this.skyWrath[1], wrath * 0.7);
    const ctx = this.skyCanvas.getContext('2d')!;
    const g = ctx.createLinearGradient(0, 0, 0, 256);
    g.addColorStop(0, `#${this.cTop.getHexString()}`);
    g.addColorStop(0.55, `#${this.tmp.copy(this.cTop).lerp(this.cBot, 0.7).getHexString()}`);
    g.addColorStop(1, `#${this.cBot.getHexString()}`);
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, 4, 256);
    this.skyTex.needsUpdate = true;
    // dome texture: v=1 is the top. The canvas is flipped by default, so top colour sits at canvas y=0.
    (this.scene.fog as THREE.Fog).color.copy(this.cBot);
    (this.scene.fog as THREE.Fog).near = 45 - storm * 15;
    this.hemi.intensity = 1.15 - storm * 0.8;
    this.hemi.color.setHex(0xcfe6ff).lerp(new THREE.Color(0x6a7aa8), storm);
    this.sun.intensity = 2.1 - storm * 1.6;
    this.cloudMat.color.setRGB(1 - storm * 0.72, 1 - storm * 0.7, 1 - storm * 0.6);
    this.mountains.color.setHex(0x8fa6b8).lerp(new THREE.Color(0x2a3548), storm);
  }

  /** Brief global brightening used when lightning strikes anywhere. */
  flash(amount: number): void {
    this.skyFlash = Math.max(this.skyFlash, amount);
  }

  update(dt: number, time: number, storm: number, follow: THREE.Vector3): void {
    this.clouds.rotation.y += dt * 0.012;
    for (let i = 0; i < this.flames.length; i++) {
      const f = this.flames[i];
      const k = 1 + Math.sin(time * 11 + i * 2.1) * 0.12 + Math.sin(time * 23 + i) * 0.08;
      f.scale.set(1 + Math.sin(time * 9 + i) * 0.08, k, 1);
      this.halos[i].material.opacity = 0.55 + Math.sin(time * 13 + i) * 0.12 + storm * 0.2;
    }
    // shadow camera follows Thor
    this.sun.target.position.set(follow.x, 0, follow.z);
    this.sun.position.set(follow.x - 9, 18, follow.z + 10);

    // ambient lightning
    this.nextBolt -= dt;
    if (this.nextBolt <= 0) {
      this.nextBolt = (1.1 + Math.random() * 3.5) * (1.2 - storm * 0.85);
      if (storm > 0.05 || time > 4) {
        const a = Math.random() * Math.PI * 2;
        const r = 55 + Math.random() * 60;
        const x = Math.cos(a) * r;
        const z = Math.sin(a) * r - 25;
        const from = new THREE.Vector3(x + (Math.random() - 0.5) * 20, 90, z);
        const to = new THREE.Vector3(x, 2, z);
        this.fx.bolt(from, to, 1.2, 0.35, 0xdbe8ff);
        this.skyFlash = Math.max(this.skyFlash, 0.5 + storm * 0.4);
      }
    }
    if (this.skyFlash > 0.001) {
      this.skyFlash = Math.max(0, this.skyFlash - dt * 3.5);
    }
    const f = this.skyFlash;
    this.hemi.intensity = 1.15 - storm * 0.8 + f * 1.6;
    this.strikeLight.intensity = Math.max(0, this.strikeLight.intensity - dt * 60);
  }

  dispose(): void {
    this.scene.remove(this.group);
    const seen = new Set<THREE.Material>();
    this.group.traverse((o) => {
      const m = o as THREE.Mesh;
      m.geometry?.dispose();
      const mats = Array.isArray(m.material) ? m.material : m.material ? [m.material] : [];
      for (const mat of mats) {
        if (seen.has(mat)) continue;
        seen.add(mat);
        const mm = mat as THREE.MeshStandardMaterial;
        mm.map?.dispose();
        mat.dispose();
      }
    });
  }
}
