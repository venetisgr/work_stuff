import * as THREE from 'three';
import { CFG } from './config';
import type { Effects } from './Effects';

const GOLD = 0xe8b84a;

function canvasTex(size: number, draw: (ctx: CanvasRenderingContext2D, s: number) => void): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = c.height = size;
  draw(c.getContext('2d')!, size);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}

function sandGrain(ctx: CanvasRenderingContext2D, s: number, n: number): void {
  for (let i = 0; i < n; i++) {
    const v = 150 + Math.random() * 70;
    ctx.fillStyle = `rgba(${v},${v * 0.82},${v * 0.55},${0.04 + Math.random() * 0.08})`;
    const r = 1 + Math.random() * 5;
    ctx.beginPath();
    ctx.arc(Math.random() * s, Math.random() * s, r, 0, Math.PI * 2);
    ctx.fill();
  }
}

/** A tiny set of hieroglyph-like marks drawn with strokes (eye, ankh, wave, bird, sun). */
function glyph(ctx: CanvasRenderingContext2D, kind: number, w: number, h: number): void {
  ctx.beginPath();
  switch (kind % 5) {
    case 0: // eye of Horus
      ctx.moveTo(0, h * 0.5);
      ctx.quadraticCurveTo(w * 0.5, h * 0.05, w, h * 0.5);
      ctx.quadraticCurveTo(w * 0.5, h * 0.95, 0, h * 0.5);
      ctx.moveTo(w * 0.62, h * 0.5);
      ctx.arc(w * 0.5, h * 0.5, w * 0.12, 0, Math.PI * 2);
      ctx.moveTo(w * 0.7, h * 0.78);
      ctx.lineTo(w * 0.8, h);
      break;
    case 1: // ankh
      ctx.ellipse(w * 0.5, h * 0.25, w * 0.2, h * 0.22, 0, 0, Math.PI * 2);
      ctx.moveTo(w * 0.5, h * 0.47);
      ctx.lineTo(w * 0.5, h);
      ctx.moveTo(w * 0.2, h * 0.58);
      ctx.lineTo(w * 0.8, h * 0.58);
      break;
    case 2: // water
      for (let i = 0; i < 3; i++) {
        const y = h * (0.25 + i * 0.25);
        ctx.moveTo(0, y);
        ctx.lineTo(w * 0.25, y - h * 0.12);
        ctx.lineTo(w * 0.5, y);
        ctx.lineTo(w * 0.75, y - h * 0.12);
        ctx.lineTo(w, y);
      }
      break;
    case 3: // bird
      ctx.moveTo(w * 0.1, h * 0.85);
      ctx.lineTo(w * 0.4, h * 0.45);
      ctx.lineTo(w * 0.75, h * 0.45);
      ctx.lineTo(w * 0.9, h * 0.2);
      ctx.moveTo(w * 0.4, h * 0.45);
      ctx.lineTo(w * 0.4, h);
      break;
    default: // sun
      ctx.arc(w * 0.5, h * 0.5, w * 0.3, 0, Math.PI * 2);
      ctx.moveTo(w * 0.5, h * 0.5);
      ctx.arc(w * 0.5, h * 0.5, w * 0.05, 0, Math.PI * 2);
  }
  ctx.stroke();
}

function floorTexture(): THREE.CanvasTexture {
  return canvasTex(1024, (ctx, s) => {
    const c = s / 2;
    ctx.fillStyle = '#e6cf9a';
    ctx.fillRect(0, 0, s, s);
    sandGrain(ctx, s, 900);
    // sandstone block joints
    ctx.strokeStyle = 'rgba(120,86,40,0.18)';
    ctx.lineWidth = 2;
    for (let i = 1; i < 16; i++) {
      ctx.beginPath();
      ctx.moveTo(0, (i * s) / 16);
      ctx.lineTo(s, (i * s) / 16);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo((i * s) / 16, 0);
      ctx.lineTo((i * s) / 16, s);
      ctx.stroke();
    }
    const ring = (r: number, w: number, col: string) => {
      ctx.strokeStyle = col;
      ctx.lineWidth = w;
      ctx.beginPath();
      ctx.arc(c, c, r * c, 0, Math.PI * 2);
      ctx.stroke();
    };
    ring(0.985, 14, '#b8862a');
    ring(0.93, 4, '#e8c46a');
    ring(0.78, 4, '#e8c46a');
    ring(0.4, 3, '#c9993a');
    ring(0.18, 3, '#c9993a');
    // lapis band between r=0.80..0.92
    ctx.strokeStyle = 'rgba(31,100,150,0.35)';
    ctx.lineWidth = 0.12 * c;
    ctx.beginPath();
    ctx.arc(c, c, 0.86 * c, 0, Math.PI * 2);
    ctx.stroke();
    // hieroglyph band
    const N = 40;
    ctx.save();
    ctx.translate(c, c);
    ctx.strokeStyle = '#1f6a8a';
    ctx.lineWidth = 3;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    const gw = 0.075 * c;
    const gh = 0.085 * c;
    for (let i = 0; i < N; i++) {
      ctx.save();
      ctx.rotate((i / N) * Math.PI * 2);
      ctx.translate(-gw / 2, -0.9 * c);
      glyph(ctx, i, gw, gh);
      ctx.restore();
    }
    ctx.restore();
    // sunburst around the centre
    ctx.save();
    ctx.translate(c, c);
    for (let i = 0; i < 24; i++) {
      ctx.rotate((Math.PI * 2) / 24);
      ctx.fillStyle = i % 2 ? 'rgba(232,150,40,0.55)' : 'rgba(31,100,150,0.25)';
      ctx.beginPath();
      ctx.moveTo(0.19 * c, -0.015 * c);
      ctx.lineTo(0.39 * c, -0.05 * c);
      ctx.lineTo(0.39 * c, 0.05 * c);
      ctx.lineTo(0.19 * c, 0.015 * c);
      ctx.fill();
    }
    // sun disc + ankh emblem
    ctx.fillStyle = '#d98a1c';
    ctx.beginPath();
    ctx.arc(0, 0, 0.17 * c, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = '#7a4a08';
    ctx.lineWidth = 7;
    ctx.beginPath();
    ctx.ellipse(0, -0.045 * c, 0.05 * c, 0.065 * c, 0, 0, Math.PI * 2);
    ctx.moveTo(0, 0.02 * c);
    ctx.lineTo(0, 0.13 * c);
    ctx.moveTo(-0.06 * c, 0.045 * c);
    ctx.lineTo(0.06 * c, 0.045 * c);
    ctx.stroke();
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
  private sunDisc!: THREE.Mesh;
  private skyFlash = 0;
  private nextBolt = 3;
  private lastStorm = -1;

  private cTop = new THREE.Color();
  private cBot = new THREE.Color();
  private tmp = new THREE.Color();
  private skyDay = [new THREE.Color(0x3f8fd0), new THREE.Color(0xffe2a8)];
  private skyStorm = [new THREE.Color(0x1a1233), new THREE.Color(0xc8602a)];
  private skyWrath = [new THREE.Color(0x07040f), new THREE.Color(0x6a1a24)];

  constructor(private scene: THREE.Scene, private fx: Effects, shadowSize: number) {
    scene.add(this.group);
    scene.fog = new THREE.Fog(0xffe2a8, 45, 150);

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
    this.hemi = new THREE.HemisphereLight(0xfff0d0, 0x9a7a4a, 1.15);
    this.group.add(this.hemi);
    this.sun = new THREE.DirectionalLight(0xffe2a0, 2.1);
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
    this.strikeLight = new THREE.PointLight(0xffd080, 0, 24, 1.6);
    this.strikeLight.position.set(0, 5, 0);
    this.group.add(this.strikeLight);

    this.buildPlatform();
    this.buildColumns();
    this.buildBraziers();

    this.cloudMat = new THREE.SpriteMaterial({ map: cloudTexture(), transparent: true, depthWrite: false, opacity: 0.9, fog: false });
    this.buildClouds();
    this.mountains = new THREE.MeshLambertMaterial({ color: 0xd2a760, flatShading: true });
    this.buildBackdrop();
    this.setStorm(0, 0);
  }

  private buildPlatform(): void {
    const R = CFG.ARENA_RADIUS + 0.8;
    const floor = new THREE.MeshStandardMaterial({ map: floorTexture(), roughness: 0.45, metalness: 0.05 });
    const side = new THREE.MeshStandardMaterial({ color: 0xd8bc84, roughness: 0.75 });
    const slab = new THREE.Mesh(new THREE.CylinderGeometry(R, R, 1.4, 64), [side, floor, side]);
    slab.position.y = -0.7;
    slab.receiveShadow = true;
    this.group.add(slab);

    // gold rim
    const rim = new THREE.Mesh(new THREE.TorusGeometry(R, 0.14, 8, 64), new THREE.MeshStandardMaterial({ color: GOLD, metalness: 0.9, roughness: 0.3 }));
    rim.rotation.x = Math.PI / 2;
    rim.position.y = 0.02;
    this.group.add(rim);

    // floating sandstone mesa underneath
    const rockMat = new THREE.MeshLambertMaterial({ color: 0x8a6a3e, flatShading: true });
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

  private buildColumns(): void {
    const N = 14;
    const R = CFG.ARENA_RADIUS + 0.2;
    const marble = new THREE.MeshStandardMaterial({ color: 0xe2c994, roughness: 0.8 });
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
      // columns on the camera side are kept low so they never hide the player
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
    const bronze = new THREE.MeshStandardMaterial({ color: 0xc89a3a, metalness: 0.8, roughness: 0.35 });
    const flameMat = new THREE.MeshBasicMaterial({ color: 0xffa83a, transparent: true, opacity: 0.9, blending: THREE.AdditiveBlending, depthWrite: false });
    const haloMat = new THREE.SpriteMaterial({ map: cloudTexture(), color: 0xff9a30, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, opacity: 0.8 });
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
      const lower = i < 30; // dust haze banks drifting beneath and beyond the platform
      const r = lower ? rnd(14, 70) : rnd(70, 140);
      s.position.set(Math.cos(a) * r, lower ? rnd(-9, -3) : rnd(-4, 14), Math.sin(a) * r);
      const sz = lower ? rnd(16, 34) : rnd(30, 60);
      s.scale.set(sz, sz * 0.5, 1);
      this.clouds.add(s);
    }
    this.group.add(this.clouds);
  }

  private buildBackdrop(): void {
    // great dunes ringing the horizon
    const dunes: Array<[number, number, number, number]> = [
      [-60, -110, 60, 22],
      [-10, -140, 80, 26],
      [55, -120, 60, 20],
      [100, -60, 48, 18],
      [-105, -50, 50, 20],
      [-90, 40, 46, 16],
      [95, 50, 50, 18],
      [0, 120, 90, 20],
    ];
    for (const [x, z, r, h] of dunes) {
      const m = new THREE.Mesh(new THREE.ConeGeometry(r, h, 8, 1), this.mountains);
      m.position.set(x, h / 2 - 24, z);
      m.rotation.y = x;
      m.scale.set(1, 1, 0.7);
      this.group.add(m);
    }
    // pyramids of Giza on the far horizon
    const stone = new THREE.MeshStandardMaterial({ color: 0xe9cf95, roughness: 0.9, emissive: 0x6a4a18, emissiveIntensity: 0.25, flatShading: true });
    const cap = new THREE.MeshStandardMaterial({ color: 0xf2c04a, metalness: 0.8, roughness: 0.3, emissive: 0x7a4a08, emissiveIntensity: 0.6 });
    const pyr: Array<[number, number, number, number]> = [
      [-10, -150, 52, 52],
      [38, -135, 34, 34],
      [-52, -128, 24, 24],
      [85, -100, 18, 18],
    ];
    for (const [x, z, w, h] of pyr) {
      const p = new THREE.Mesh(new THREE.ConeGeometry(w * 0.72, h, 4, 1), stone);
      p.position.set(x, h / 2 - 22, z);
      p.rotation.y = Math.PI / 4;
      this.group.add(p);
      const tip = new THREE.Mesh(new THREE.ConeGeometry(w * 0.72 * 0.14, h * 0.14, 4), cap);
      tip.position.set(x, h - 22 - h * 0.07, z);
      tip.rotation.y = Math.PI / 4;
      this.group.add(tip);
    }
    // twin obelisks flanking the horizon
    for (const x of [-26, 14]) {
      const o = new THREE.Mesh(new THREE.CylinderGeometry(1.1, 2.0, 30, 4), stone);
      o.position.set(x, 15 - 22, -90);
      o.rotation.y = Math.PI / 4;
      const t = new THREE.Mesh(new THREE.ConeGeometry(1.1, 3, 4), cap);
      t.position.set(x, 31.5 - 22, -90);
      t.rotation.y = Math.PI / 4;
      this.group.add(o, t);
    }
    // the sun disc of Ra, low on the horizon (dims with the eclipse)
    this.sunDisc = new THREE.Mesh(
      new THREE.CircleGeometry(16, 40),
      new THREE.MeshBasicMaterial({ color: 0xffb040, fog: false, transparent: true, opacity: 0.95, depthWrite: false }),
    );
    this.sunDisc.position.set(-30, 36, -230);
    this.group.add(this.sunDisc);
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
    this.hemi.color.setHex(0xfff0d0).lerp(new THREE.Color(0x8a5a6a), storm);
    this.sun.intensity = 2.1 - storm * 1.6;
    this.cloudMat.color.setRGB(1 - storm * 0.45, 0.85 - storm * 0.5, 0.6 - storm * 0.3);
    const sm = this.sunDisc.material as THREE.MeshBasicMaterial;
    sm.color.setHex(0xffb040).lerp(new THREE.Color(0x14060a), Math.min(1, storm * 1.25));
    this.mountains.color.setHex(0xd2a760).lerp(new THREE.Color(0x4a2a30), storm);
  }

  /** Brief global brightening used when a divine strike lands anywhere. */
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
    // shadow camera follows the player
    this.sun.target.position.set(follow.x, 0, follow.z);
    this.sun.position.set(follow.x - 9, 18, follow.z + 10);

    // ambient shafts of Ra's light breaking through
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
        this.fx.bolt(from, to, 1.0, 0.45, 0xffd890);
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
