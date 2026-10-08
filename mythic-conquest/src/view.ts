// Three.js scene: terrain, entity views, effects, camera, picking.

import * as THREE from 'three';
import { BUILDINGS, BId, CIVS, MAP_N, UNITS } from './data';
import { Ent, Sim, SimEvent, Zone } from './sim';
import { TEAM_COLORS, geo, makeBerries, makeBuilding, makeGold, makeUnit, mat, treeLeafGeo, treeTrunkGeo } from './models';

interface EntView {
  ent: Ent; obj: THREE.Object3D; ring?: THREE.Mesh; bar?: THREE.Group; barFg?: THREE.Mesh;
  px: number; py: number; phase: number; swing: number; prevAtk: number; treeIdx: number; maxScale: number;
}

interface Particle { mesh: THREE.Mesh; vx: number; vy: number; vz: number; life: number; max: number; g: number; grow: number }

const TREE_CAP = 1600;

export class View {
  renderer: THREE.WebGLRenderer;
  scene = new THREE.Scene();
  camera: THREE.PerspectiveCamera;
  sim!: Sim;
  views = new Map<number, EntView>();
  camX = 14; camZ = 62; camDist = 24; pitch = 1.0;
  private trunks: THREE.InstancedMesh; private leaves: THREE.InstancedMesh;
  private freeTrees: number[] = [];
  private particles: Particle[] = [];
  private zoneViews = new Map<Zone, THREE.Group>();
  private temp: { obj: THREE.Object3D; life: number; max: number; update: (t: number) => void }[] = [];
  private ghost = new THREE.Group();
  private ghostKey = '';
  private marker: THREE.Mesh;
  private ray = new THREE.Raycaster();
  private v3 = new THREE.Vector3();
  private time = 0;
  private sun: THREE.DirectionalLight;
  shake = 0;

  constructor(canvas: HTMLCanvasElement) {
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.scene.background = new THREE.Color(0x9cc6e8);
    this.scene.fog = new THREE.Fog(0x9cc6e8, 55, 120);
    this.camera = new THREE.PerspectiveCamera(42, 1, 0.5, 220);

    this.scene.add(new THREE.HemisphereLight(0xdfeeff, 0x4a5a3a, 0.9));
    this.sun = new THREE.DirectionalLight(0xfff0d0, 2.1);
    this.sun.position.set(-20, 40, 15);
    this.sun.castShadow = true;
    this.sun.shadow.mapSize.set(2048, 2048);
    const sc = this.sun.shadow.camera;
    sc.left = -34; sc.right = 34; sc.top = 34; sc.bottom = -34; sc.near = 1; sc.far = 120;
    this.sun.shadow.bias = -0.0006;
    this.scene.add(this.sun, this.sun.target);

    this.buildTerrain();

    this.trunks = new THREE.InstancedMesh(treeTrunkGeo, mat(0x6b4a2c), TREE_CAP);
    this.leaves = new THREE.InstancedMesh(treeLeafGeo, mat(0x3b8c3f), TREE_CAP);
    for (const m of [this.trunks, this.leaves]) { m.castShadow = true; m.receiveShadow = true; m.count = TREE_CAP; m.frustumCulled = false; this.scene.add(m); }
    const zero = new THREE.Matrix4().makeScale(0, 0, 0);
    for (let i = TREE_CAP - 1; i >= 0; i--) { this.trunks.setMatrixAt(i, zero); this.leaves.setMatrixAt(i, zero); this.freeTrees.push(i); }

    this.marker = new THREE.Mesh(geo.ring, new THREE.MeshBasicMaterial({ color: 0x66ff88, transparent: true, side: THREE.DoubleSide, depthWrite: false }));
    this.marker.rotation.x = -Math.PI / 2; this.marker.visible = false; this.marker.position.y = 0.06;
    this.scene.add(this.marker);
    this.scene.add(this.ghost);

    const pg = new THREE.BoxGeometry(1, 1, 1);
    for (let i = 0; i < 500; i++) {
      const m = new THREE.Mesh(pg, new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, depthWrite: false }));
      m.visible = false; this.scene.add(m);
      this.particles.push({ mesh: m, vx: 0, vy: 0, vz: 0, life: 0, max: 1, g: 0, grow: 0 });
    }
    this.resize();
    window.addEventListener('resize', () => this.resize());
  }

  resize() {
    const w = window.innerWidth, h = window.innerHeight;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  private buildTerrain() {
    const N = MAP_N, c = document.createElement('canvas');
    c.width = c.height = 1024;
    const g = c.getContext('2d')!;
    g.fillStyle = '#5d9a46'; g.fillRect(0, 0, 1024, 1024);
    let seed = 7; const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
    for (let i = 0; i < 1800; i++) {
      const x = rnd() * 1024, y = rnd() * 1024, r = 10 + rnd() * 50;
      g.fillStyle = `rgba(${40 + rnd() * 50},${110 + rnd() * 50},${30 + rnd() * 40},0.18)`;
      g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2); g.fill();
    }
    for (let i = 0; i < 9000; i++) {
      g.fillStyle = `rgba(${rnd() > 0.5 ? '255,255,200' : '20,60,20'},0.10)`;
      g.fillRect(rnd() * 1024, rnd() * 1024, 2, 3 + rnd() * 4);
    }
    // dirt around start bases
    for (const b of [{ x: 13, y: 66 }, { x: 66, y: 13 }]) {
      const px = (b.x / N) * 1024, py = (b.y / N) * 1024;
      const gr = g.createRadialGradient(px, py, 10, px, py, 120);
      gr.addColorStop(0, 'rgba(150,120,80,0.75)'); gr.addColorStop(1, 'rgba(150,120,80,0)');
      g.fillStyle = gr; g.fillRect(px - 130, py - 130, 260, 260);
    }
    const tex = new THREE.CanvasTexture(c);
    tex.colorSpace = THREE.SRGBColorSpace; tex.anisotropy = 4;
    const ground = new THREE.Mesh(new THREE.PlaneGeometry(N, N), new THREE.MeshStandardMaterial({ map: tex, roughness: 1 }));
    ground.rotation.x = -Math.PI / 2; ground.position.set(N / 2, 0, N / 2); ground.receiveShadow = true;
    this.scene.add(ground);
    const sea = new THREE.Mesh(new THREE.PlaneGeometry(600, 600), new THREE.MeshStandardMaterial({ color: 0x2f6f9a, roughness: 0.4 }));
    sea.rotation.x = -Math.PI / 2; sea.position.set(N / 2, -0.35, N / 2);
    this.scene.add(sea);
    const rim = new THREE.Mesh(new THREE.BoxGeometry(N + 2, 0.7, N + 2), new THREE.MeshStandardMaterial({ color: 0x8a7a5a, roughness: 1 }));
    rim.position.set(N / 2, -0.37, N / 2);
    this.scene.add(rim);
  }

  init(sim: Sim) {
    this.sim = sim;
    for (const v of this.views.values()) this.scene.remove(v.obj);
    this.views.clear();
  }

  // ------------------------------------------------------------------ camera
  setCamera(x: number, z: number) { this.camX = Math.max(5, Math.min(MAP_N - 5, x)); this.camZ = Math.max(8, Math.min(MAP_N - 2, z)); }
  private updateCamera(dt: number) {
    const d = this.camDist, p = this.pitch;
    const sx = this.shake > 0 ? (Math.random() - 0.5) * this.shake : 0, sz = this.shake > 0 ? (Math.random() - 0.5) * this.shake : 0;
    this.shake = Math.max(0, this.shake - dt * 2);
    this.camera.position.set(this.camX + sx, Math.sin(p) * d, this.camZ + Math.cos(p) * d + sz);
    this.camera.lookAt(this.camX + sx, 0, this.camZ + sz);
    this.sun.position.set(this.camX - 18, 38, this.camZ + 14);
    this.sun.target.position.set(this.camX, 0, this.camZ);
  }
  groundAt(ndcX: number, ndcY: number): { x: number; y: number } | null {
    this.ray.setFromCamera(new THREE.Vector2(ndcX, ndcY), this.camera);
    const o = this.ray.ray.origin, dir = this.ray.ray.direction;
    if (dir.y > -0.0001) return null;
    const t = -o.y / dir.y;
    return { x: o.x + dir.x * t, y: o.z + dir.z * t };
  }
  worldToScreen(x: number, y: number, h = 0.5): { x: number; y: number; behind: boolean } {
    this.v3.set(x, h, y).project(this.camera);
    return { x: (this.v3.x * 0.5 + 0.5) * window.innerWidth, y: (-this.v3.y * 0.5 + 0.5) * window.innerHeight, behind: this.v3.z > 1 };
  }
  /** Visible ground quad corners for the minimap */
  viewQuad(): { x: number; y: number }[] {
    return [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([a, b]) => this.groundAt(a, b) ?? { x: this.camX, y: this.camZ });
  }

  /** Pick a unit/building with the mouse, falling back to resource tiles. */
  pick(ndcX: number, ndcY: number): Ent | null {
    this.ray.setFromCamera(new THREE.Vector2(ndcX, ndcY), this.camera);
    const objs: THREE.Object3D[] = [];
    for (const v of this.views.values()) if (v.ent.kind !== 'res' || v.ent.def !== 'tree') objs.push(v.obj);
    const hits = this.ray.intersectObjects(objs, true);
    for (const h of hits) {
      let o: THREE.Object3D | null = h.object;
      while (o && o.userData.id === undefined) o = o.parent;
      if (o) { const e = this.sim.get(o.userData.id); if (e) return e; }
    }
    // resources: screen-space test against the visible mass (foliage / ore), then the ground tile
    const px = (ndcX * 0.5 + 0.5) * window.innerWidth, py = (-ndcY * 0.5 + 0.5) * window.innerHeight;
    let best: Ent | null = null, bd = 1e9;
    for (const v of this.views.values()) {
      const e = v.ent;
      if (e.kind !== 'res' || e.dead) continue;
      const top = e.def === 'tree' ? 1.0 : 0.5;
      const s = this.worldToScreen(e.x, e.y, top);
      if (s.behind) continue;
      const scale = 1100 / this.camDist;           // px per world unit, roughly
      const reach = (e.def === 'tree' ? 0.55 : e.def === 'gold' ? 1.0 : 0.45) * scale * 0.45 + 8;
      const d = Math.hypot(s.x - px, s.y - py);
      if (d < reach && d < bd) { bd = d; best = e; }
    }
    if (best) return best;
    const g = this.groundAt(ndcX, ndcY);
    if (g) {
      for (const v of this.views.values()) {
        const e = v.ent;
        if (e.kind === 'res' && !e.dead && g.x >= e.tx - 0.2 && g.x <= e.tx + e.size + 0.2 && g.y >= e.ty - 0.2 && g.y <= e.ty + e.size + 0.2) return e;
      }
    }
    return null;
  }

  // ------------------------------------------------------------------ sync
  private create(e: Ent): EntView {
    const v: EntView = { ent: e, obj: new THREE.Group(), px: e.x, py: e.y, phase: Math.random() * 6, swing: 0, prevAtk: 0, treeIdx: -1, maxScale: 1 };
    if (e.kind === 'unit') {
      v.obj = makeUnit(UNITS[e.def], e.owner);
    } else if (e.kind === 'bld') {
      v.obj = makeBuilding(e.def as BId, this.sim.players[e.owner].civ, e.owner);
    } else if (e.def === 'tree') {
      v.obj = new THREE.Group();
      v.treeIdx = this.freeTrees.pop() ?? -1;
      const s = 0.85 + (e.id * 37 % 100) / 250;
      v.maxScale = s;
      this.placeTree(v, s);
    } else if (e.def === 'gold') {
      v.obj = makeGold();
      v.obj.scale.setScalar(1.05);
    } else v.obj = makeBerries();
    v.obj.userData.id = e.id;
    v.obj.position.set(e.x, 0, e.y);
    if (e.kind === 'res' && e.def !== 'tree') v.obj.rotation.y = (e.id * 1.7) % 6;
    if (e.def !== 'tree') this.scene.add(v.obj);
    // selection ring
    if (e.kind !== 'res' || e.def === 'gold') {
      const r = new THREE.Mesh(geo.ring, new THREE.MeshBasicMaterial({ color: 0x5dff7a, transparent: true, opacity: 0.95, side: THREE.DoubleSide, depthWrite: false }));
      r.rotation.x = -Math.PI / 2; r.position.y = 0.08; r.visible = false;
      const rad = e.kind === 'unit' ? UNITS[e.def].r * 1.7 + 0.15 : (e.size * 0.62);
      r.scale.setScalar(rad);
      v.ring = r; v.obj.add(r);
    }
    return v;
  }

  private placeTree(v: EntView, s: number) {
    if (v.treeIdx < 0) return;
    const e = v.ent, m = new THREE.Matrix4(), ry = (e.id * 2.3) % 6.28;
    const jx = ((e.id * 53 % 100) / 100 - 0.5) * 0.4, jz = ((e.id * 29 % 100) / 100 - 0.5) * 0.4;
    m.compose(this.v3.set(e.x + jx, 0.35 * s, e.y + jz).clone(), new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), ry), new THREE.Vector3(s, s, s));
    this.trunks.setMatrixAt(v.treeIdx, m);
    const m2 = new THREE.Matrix4().compose(new THREE.Vector3(e.x + jx, 0.35 * s + 0.95 * s, e.y + jz), new THREE.Quaternion(), new THREE.Vector3(s, s, s));
    this.leaves.setMatrixAt(v.treeIdx, m2);
    this.trunks.instanceMatrix.needsUpdate = true; this.leaves.instanceMatrix.needsUpdate = true;
  }
  private removeTree(v: EntView) {
    if (v.treeIdx < 0) return;
    const z = new THREE.Matrix4().makeScale(0, 0, 0);
    this.trunks.setMatrixAt(v.treeIdx, z); this.leaves.setMatrixAt(v.treeIdx, z);
    this.trunks.instanceMatrix.needsUpdate = true; this.leaves.instanceMatrix.needsUpdate = true;
    this.freeTrees.push(v.treeIdx);
  }

  private makeBar(): { g: THREE.Group; fg: THREE.Mesh } {
    const g = new THREE.Group();
    const bg = new THREE.Mesh(geo.plane, new THREE.MeshBasicMaterial({ color: 0x111111, depthTest: false, transparent: true, opacity: 0.8 }));
    bg.scale.set(1.04, 0.14, 1); bg.renderOrder = 10;
    const fg = new THREE.Mesh(geo.plane, new THREE.MeshBasicMaterial({ color: 0x4cff6a, depthTest: false }));
    fg.renderOrder = 11; fg.position.z = 0.001;
    g.add(bg, fg);
    return { g, fg };
  }

  sync(dt: number, selected: Set<number>, hovered: number) {
    this.time += dt;
    const sim = this.sim;
    // consume events for effects
    for (const ev of sim.events) this.onEvent(ev);
    sim.events.length = 0;

    for (const e of sim.ents.values()) {
      if (e.dead) continue;
      let v = this.views.get(e.id);
      if (!v) { v = this.create(e); this.views.set(e.id, v); }
    }
    for (const [id, v] of this.views) {
      const e = sim.ents.get(id);
      if (!e || e.dead) {
        this.removeTree(v); this.scene.remove(v.obj); this.views.delete(id);
        continue;
      }
      this.updateView(v, e, dt, selected.has(id), hovered === id);
    }
    this.updateZones(dt);
    this.updateFx(dt);
    this.updateCamera(dt);
  }

  private updateView(v: EntView, e: Ent, dt: number, sel: boolean, hov: boolean) {
    const o = v.obj;
    if (e.kind === 'unit') {
      const k = Math.min(1, dt * 14);
      const dx = e.x - v.px, dy = e.y - v.py;
      v.px += dx * k; v.py += dy * k;
      o.position.set(v.px, 0, v.py);
      const body = o.userData.body as THREE.Object3D, arm = o.userData.arm as THREE.Object3D;
      const moving = Math.hypot(dx, dy) > 0.004;
      if (moving) v.phase += dt * 11;
      let yaw = e.face;
      let da = yaw - o.rotation.y; da = Math.atan2(Math.sin(da), Math.cos(da));
      o.rotation.y += da * Math.min(1, dt * 12);
      const flyOff = o.userData.fly ? 0.7 + Math.sin(this.time * 4 + v.phase) * 0.08 : 0;
      body.position.y = (moving ? Math.abs(Math.sin(v.phase)) * 0.07 : 0) + flyOff;
      body.rotation.z = moving ? Math.sin(v.phase) * 0.06 : 0;
      // attack swing
      const def = UNITS[e.def];
      if (e.atkT > v.prevAtk + 0.05) v.swing = 1;
      v.prevAtk = e.atkT;
      v.swing = Math.max(0, v.swing - dt * 5);
      arm.rotation.x = -v.swing * 1.6;
      body.position.z = v.swing * 0.12;
      // working villagers bob
      if ((e.state === 'gather' || e.state === 'work') && e.order && def.cls === 'vil') arm.rotation.x = Math.sin(this.time * 9 + v.phase) * 0.9 - 0.3;
      if (e.stunT > 0) body.rotation.z = Math.sin(this.time * 30) * 0.15;
      body.scale.y = 1 + Math.sin(this.time * 2 + v.phase) * 0.012;
      o.scale.setScalar(e.slowT > 0 ? 0.97 : 1);
      // cargo marker: tint nothing, but show small sack when carrying
    } else if (e.kind === 'bld') {
      const inner = o.userData.inner as THREE.Object3D;
      if (!e.done) {
        const p = 0.12 + 0.88 * e.progress;
        inner.scale.set(1, p, 1);
        inner.position.y = 0;
      } else if (inner.scale.y !== 1) inner.scale.set(1, 1, 1);
      const flag = o.userData.flag as THREE.Object3D | undefined;
      if (flag) { flag.rotation.y = Math.sin(this.time * 3 + v.phase) * 0.35; flag.visible = e.done; }
      if (o.userData.crops) {
        const f = Math.max(0.15, e.amount / 600);
        for (const c of o.userData.crops as THREE.Object3D[]) c.scale.y = f;
      }
      if (e.done && e.hp < e.maxHp * 0.4) {
        // smoke from damaged buildings
        if (Math.random() < dt * 5) this.puff(e.x + (Math.random() - 0.5) * e.size * 0.6, 1.5, e.y + (Math.random() - 0.5) * e.size * 0.6, 0x3a3a3a, 0.35, 1.4);
      }
    } else if (e.def === 'gold') {
      const f = 0.6 + 0.45 * (e.amount / e.maxHp);
      o.scale.setScalar(f);
    } else if (e.def === 'berries') {
      o.scale.setScalar(0.5 + 0.6 * (e.amount / e.maxHp));
    } else if (e.def === 'tree' && e.amount < 160 && v.treeIdx >= 0) {
      // trees shrink as they are chopped
      const f = v.maxScale * (0.55 + 0.45 * (e.amount / 160));
      if (Math.abs(f - (v.maxScale)) > 0.01 && (Math.floor(e.amount) % 10 === 0)) { this.placeTree(v, f); }
    }
    if (v.ring) {
      const show = sel || hov;
      v.ring.visible = show;
      if (show) (v.ring.material as THREE.MeshBasicMaterial).color.set(e.owner === 0 ? (sel ? 0x5dff7a : 0xcfffd8) : e.owner < 0 ? 0xffe08a : (sel ? 0xff5a4a : 0xffb0a0));
    }
    // health bars
    const showBar = e.kind !== 'res' && ((e.hp < e.maxHp - 0.5) || sel || hov);
    if (showBar && !v.bar) { const b = this.makeBar(); v.bar = b.g; v.barFg = b.fg; this.scene.add(b.g); }
    if (v.bar) {
      v.bar.visible = showBar;
      if (showBar) {
        const w = e.kind === 'bld' ? e.size * 0.9 : 0.9 + (UNITS[e.def].r * 0.8);
        const top = e.kind === 'bld' ? 2.6 + e.size * 0.2 : UNITS[e.def].look.h * 1.35 + 0.35 + (UNITS[e.def].look.flags.includes('fly') ? 0.7 : 0);
        v.bar.position.set(e.kind === 'unit' ? v.px : e.x, top, e.kind === 'unit' ? v.py : e.y);
        v.bar.quaternion.copy(this.camera.quaternion);
        v.bar.scale.set(w, 1, 1);
        const f = Math.max(0, e.hp / e.maxHp);
        v.barFg!.scale.set(f * 1.0, 0.1, 1); v.barFg!.position.x = -(1 - f) * 0.5;
        (v.barFg!.material as THREE.MeshBasicMaterial).color.setHSL(f * 0.33, 0.9, 0.5);
      }
    }
  }

  // ------------------------------------------------------------------ effects
  private spawnP(x: number, y: number, z: number, vx: number, vy: number, vz: number, col: number, size: number, life: number, g = 6, grow = 0) {
    const p = this.particles.find((q) => q.life <= 0);
    if (!p) return;
    p.mesh.visible = true; p.mesh.position.set(x, y, z); p.mesh.scale.setScalar(size);
    (p.mesh.material as THREE.MeshBasicMaterial).color.set(col); (p.mesh.material as THREE.MeshBasicMaterial).opacity = 1;
    p.vx = vx; p.vy = vy; p.vz = vz; p.life = life; p.max = life; p.g = g; p.grow = grow;
  }
  private puff(x: number, y: number, z: number, col: number, size: number, life: number) {
    this.spawnP(x, y, z, (Math.random() - 0.5) * 0.4, 0.8, (Math.random() - 0.5) * 0.4, col, size, life, -0.2, size * 0.8);
  }
  private burst(x: number, y: number, z: number, col: number, n: number, speed: number, size = 0.1) {
    for (let i = 0; i < n; i++) {
      const a = Math.random() * 6.28, s = speed * (0.4 + Math.random());
      this.spawnP(x, y, z, Math.cos(a) * s, 1.5 + Math.random() * speed * 0.6, Math.sin(a) * s, col, size, 0.5 + Math.random() * 0.4);
    }
  }
  private updateFx(dt: number) {
    for (const p of this.particles) {
      if (p.life <= 0) continue;
      p.life -= dt;
      if (p.life <= 0) { p.mesh.visible = false; continue; }
      p.vy -= p.g * dt;
      p.mesh.position.x += p.vx * dt; p.mesh.position.y = Math.max(0.02, p.mesh.position.y + p.vy * dt); p.mesh.position.z += p.vz * dt;
      if (p.grow) p.mesh.scale.addScalar(p.grow * dt);
      (p.mesh.material as THREE.MeshBasicMaterial).opacity = Math.min(1, (p.life / p.max) * 1.5);
    }
    for (let i = this.temp.length - 1; i >= 0; i--) {
      const t = this.temp[i];
      t.life -= dt;
      if (t.life <= 0) { this.scene.remove(t.obj); this.temp.splice(i, 1); continue; }
      t.update(1 - t.life / t.max);
    }
    if (this.marker.visible) {
      const k = this.marker.userData.t as number - dt;
      this.marker.userData.t = k;
      if (k <= 0) this.marker.visible = false; else {
        this.marker.scale.setScalar(0.3 + (0.5 - k) * 1.6);
        (this.marker.material as THREE.MeshBasicMaterial).opacity = k * 2;
      }
    }
  }
  private addTemp(obj: THREE.Object3D, life: number, update: (t: number) => void) {
    this.scene.add(obj);
    this.temp.push({ obj, life, max: life, update });
  }

  commandMarker(x: number, y: number, color = 0x66ff88) {
    this.marker.position.set(x, 0.06, y);
    (this.marker.material as THREE.MeshBasicMaterial).color.set(color);
    this.marker.visible = true; this.marker.userData.t = 0.5;
  }

  private bolt(x: number, z: number, col = 0xcfe8ff, thick = 0.28) {
    const g = new THREE.Group();
    const mt = new THREE.MeshBasicMaterial({ color: col, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false });
    let px = x + (Math.random() - 0.5) * 3, py = 26, pz = z + (Math.random() - 0.5) * 3;
    const segs = 9;
    for (let i = 1; i <= segs; i++) {
      const f = i / segs;
      const nx = i === segs ? x : x + (px - x) * (1 - f) * 0.3 + (Math.random() - 0.5) * 1.6 * (1 - f), ny = 26 * (1 - f), nz = i === segs ? z : z + (Math.random() - 0.5) * 1.6 * (1 - f);
      const dx = nx - px, dy = ny - py, dz = nz - pz, len = Math.hypot(dx, dy, dz);
      const m = new THREE.Mesh(geo.box, mt);
      m.scale.set(thick, thick, len);
      m.position.set((px + nx) / 2, (py + ny) / 2, (pz + nz) / 2);
      m.lookAt(nx, ny, nz);
      g.add(m);
      px = nx; py = ny; pz = nz;
    }
    this.addTemp(g, 0.28, (t) => { mt.opacity = 1 - t; });
  }
  private ringWave(x: number, z: number, r: number, col: number, life = 0.8) {
    const m = new THREE.Mesh(geo.ring, new THREE.MeshBasicMaterial({ color: col, transparent: true, side: THREE.DoubleSide, depthWrite: false }));
    m.rotation.x = -Math.PI / 2; m.position.set(x, 0.1, z);
    this.addTemp(m, life, (t) => { m.scale.setScalar(0.3 + r * t); (m.material as THREE.MeshBasicMaterial).opacity = 1 - t; });
  }

  private onEvent(ev: SimEvent) {
    switch (ev.t) {
      case 'hit': this.burst(ev.x, 0.7, ev.y, 0xffe08a, 3, 1.6, 0.07); break;
      case 'shot': {
        const m = new THREE.Mesh(geo.box, new THREE.MeshBasicMaterial({ color: ev.myth ? 0xffd24a : 0xf2e9d0 }));
        const dx = ev.tx - ev.x, dz = ev.ty - ev.y, len = Math.hypot(dx, dz);
        m.scale.set(0.05, 0.05, ev.myth ? 0.7 : 0.4);
        m.lookAt(ev.tx, 0.8, ev.ty);
        const life = Math.max(0.08, len / 22);
        this.addTemp(m, life, (t) => { m.position.set(ev.x + dx * t, 0.9 + Math.sin(t * Math.PI) * 0.5, ev.y + dz * t); });
        break;
      }
      case 'die':
        if (ev.kind === 'unit') this.burst(ev.x, 0.6, ev.y, ev.owner === 0 ? 0x6aa0ff : 0xff6a5a, 8, 2.2, 0.1);
        else if (ev.kind === 'bld') {
          this.burst(ev.x, 1, ev.y, 0x9a8a6a, 40, 5, 0.22);
          for (let i = 0; i < 12; i++) this.puff(ev.x + (Math.random() - 0.5) * ev.size, 0.6, ev.y + (Math.random() - 0.5) * ev.size, 0x555555, 0.6, 1.8);
          this.shake = Math.max(this.shake, 0.5);
        } else if (ev.def === 'tree') this.burst(ev.x, 0.8, ev.y, 0x3f8a3a, 6, 1.5, 0.12);
        break;
      case 'strike':
        this.bolt(ev.x, ev.y);
        this.bolt(ev.x, ev.y, 0xffffff, 0.1);
        this.burst(ev.x, 0.4, ev.y, 0xcfe8ff, 10, 3, 0.1);
        this.ringWave(ev.x, ev.y, ev.r * 1.2, 0xbfe0ff, 0.4);
        this.shake = Math.max(this.shake, 0.25);
        break;
      case 'power': {
        this.ringWave(ev.x, ev.y, ev.r * 1.5, ev.color, 1.0);
        this.ringWave(ev.x, ev.y, ev.r * 1.0, 0xffffff, 0.6);
        this.burst(ev.x, 0.5, ev.y, ev.color, 40, 6, 0.16);
        const pillar = new THREE.Mesh(geo.cyl, new THREE.MeshBasicMaterial({ color: ev.color, transparent: true, depthWrite: false }));
        pillar.position.set(ev.x, 8, ev.y);
        this.addTemp(pillar, 0.9, (t) => { pillar.scale.set(1.2 * (1 - t) + 0.1, 16, 1.2 * (1 - t) + 0.1); (pillar.material as THREE.MeshBasicMaterial).opacity = 0.7 * (1 - t); });
        this.shake = Math.max(this.shake, 0.4);
        break;
      }
      case 'built': { const e = this.sim.get(ev.id); if (e) this.burst(e.x, 0.8, e.y, 0xffe9a0, 18, 2.5, 0.12); break; }
      case 'age': {
        const tc = [...this.sim.ents.values()].find((e) => e.owner === ev.owner && e.def === 'tc');
        if (tc) { this.ringWave(tc.x, tc.y, 9, CIVS[this.sim.players[ev.owner].civ].color, 1.4); this.burst(tc.x, 2, tc.y, 0xffe08a, 50, 5, 0.18); }
        break;
      }
      default: break;
    }
  }

  private updateZones(dt: number) {
    for (const z of this.sim.zones) {
      let g = this.zoneViews.get(z);
      if (!g) {
        g = new THREE.Group();
        const disc = new THREE.Mesh(geo.disc, new THREE.MeshBasicMaterial({ color: z.power.color, transparent: true, opacity: 0.2, depthWrite: false, side: THREE.DoubleSide }));
        disc.rotation.x = -Math.PI / 2; disc.position.y = 0.07; disc.scale.setScalar(z.r);
        const edge = new THREE.Mesh(geo.ring, new THREE.MeshBasicMaterial({ color: z.power.color, transparent: true, opacity: 0.8, depthWrite: false, side: THREE.DoubleSide }));
        edge.rotation.x = -Math.PI / 2; edge.position.y = 0.09; edge.scale.setScalar(z.r);
        g.add(disc, edge); g.position.set(z.x, 0, z.y);
        this.scene.add(g); this.zoneViews.set(z, g);
      }
      g.children[1].rotation.z += dt;
      if (Math.random() < dt * 40) {
        const a = Math.random() * 6.28, d = Math.sqrt(Math.random()) * z.r;
        const dust = z.power.id === 'sandstorm' || z.power.id === 'quake';
        this.spawnP(z.x + Math.cos(a) * d, 0.2, z.y + Math.sin(a) * d, (dust ? 3 : 0), dust ? 1.2 : 3 + Math.random() * 2, 0, z.power.color, 0.16, 0.8, dust ? 0.5 : 2);
      }
    }
    for (const [z, g] of this.zoneViews) if (!this.sim.zones.includes(z)) { this.scene.remove(g); this.zoneViews.delete(z); }
  }

  // ------------------------------------------------------------------ ghost placement
  setGhost(def: BId | null, tx: number, ty: number, valid: boolean) {
    if (!def) { this.ghost.visible = false; return; }
    const civ = this.sim.players[0].civ;
    const key = `${def}|${civ}`;
    if (key !== this.ghostKey) {
      this.ghost.clear();
      const m = makeBuilding(def, civ, 0);
      m.traverse((o) => {
        const me = o as THREE.Mesh;
        if (me.isMesh) { me.material = (me.material as THREE.Material).clone(); (me.material as THREE.Material).transparent = true; (me.material as THREE.Material).opacity = 0.55; me.castShadow = false; }
      });
      this.ghost.add(m);
      const f = new THREE.Mesh(geo.plane, new THREE.MeshBasicMaterial({ color: 0x44ff66, transparent: true, opacity: 0.35, depthWrite: false, side: THREE.DoubleSide }));
      f.rotation.x = -Math.PI / 2; f.position.y = 0.12; f.scale.setScalar(BUILDINGS[def].size);
      f.name = 'foot';
      this.ghost.add(f);
      this.ghostKey = key;
    }
    const s = BUILDINGS[def].size;
    this.ghost.visible = true;
    this.ghost.position.set(tx + s / 2, 0, ty + s / 2);
    (this.ghost.getObjectByName('foot') as THREE.Mesh).material = new THREE.MeshBasicMaterial({ color: valid ? 0x44ff66 : 0xff4444, transparent: true, opacity: 0.4, depthWrite: false, side: THREE.DoubleSide });
  }

  render() { this.renderer.render(this.scene, this.camera); }
}

export { TEAM_COLORS };
