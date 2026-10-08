// Procedural low-poly models for units, buildings and resources.

import * as THREE from 'three';
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js';
import { BUILDINGS, BId, CivId, UnitDef } from './data';

const matCache = new Map<string, THREE.Material>();
export function mat(col: number, opts: { emissive?: number; opacity?: number; rough?: number } = {}): THREE.MeshStandardMaterial {
  const key = `${col}|${opts.emissive ?? ''}|${opts.opacity ?? 1}`;
  let m = matCache.get(key) as THREE.MeshStandardMaterial | undefined;
  if (!m) {
    m = new THREE.MeshStandardMaterial({
      color: col, roughness: opts.rough ?? 0.85, metalness: 0.02, flatShading: true,
      emissive: opts.emissive ?? 0x000000, emissiveIntensity: opts.emissive ? 1.2 : 0,
      transparent: (opts.opacity ?? 1) < 1, opacity: opts.opacity ?? 1,
    });
    matCache.set(key, m);
  }
  return m;
}

const G = {
  box: new THREE.BoxGeometry(1, 1, 1),
  cyl: new THREE.CylinderGeometry(1, 1, 1, 10),
  cyl3: new THREE.CylinderGeometry(1, 1, 1, 3),
  cyl4: new THREE.CylinderGeometry(1, 1, 1, 4),
  cone: new THREE.ConeGeometry(1, 1, 8),
  cone4: new THREE.ConeGeometry(1, 1, 4),
  sph: new THREE.SphereGeometry(1, 10, 8),
  ring: new THREE.RingGeometry(0.82, 1, 28),
  disc: new THREE.CircleGeometry(1, 24),
  plane: new THREE.PlaneGeometry(1, 1),
};
export const geo = G;
const taperGeo = new THREE.CylinderGeometry(0.75, 1, 1, 10);

type Opt = { rx?: number; ry?: number; rz?: number };
function add(parent: THREE.Object3D, g: THREE.BufferGeometry, m: THREE.Material, sx: number, sy: number, sz: number, x: number, y: number, z: number, o: Opt = {}) {
  const mesh = new THREE.Mesh(g, m);
  mesh.scale.set(sx, sy, sz); mesh.position.set(x, y, z);
  if (o.rx) mesh.rotation.x = o.rx;
  if (o.ry) mesh.rotation.y = o.ry;
  if (o.rz) mesh.rotation.z = o.rz;
  mesh.castShadow = true; mesh.receiveShadow = true;
  parent.add(mesh);
  return mesh;
}
const box = (p: THREE.Object3D, col: number, w: number, h: number, d: number, x: number, y: number, z: number, o?: Opt) => add(p, G.box, mat(col), w, h, d, x, y, z, o);
const cyl = (p: THREE.Object3D, col: number, rt: number, h: number, x: number, y: number, z: number, o?: Opt) => add(p, G.cyl, mat(col), rt, h, rt, x, y, z, o);
const cone = (p: THREE.Object3D, col: number, r: number, h: number, x: number, y: number, z: number, o?: Opt) => add(p, G.cone, mat(col), r, h, r, x, y, z, o);
const sph = (p: THREE.Object3D, col: number, r: number, x: number, y: number, z: number, glow = 0) => add(p, G.sph, mat(col, glow ? { emissive: glow } : {}), r, r, r, x, y, z);

export const TEAM_COLORS = [0x3d8bff, 0xe23b3b];
const SKIN = 0xe3b48a;

// ------------------------------------------------------------------ units

export function makeUnit(d: UnitDef, team: number): THREE.Group {
  const g = new THREE.Group();
  const { h, w, col, flags } = d.look;
  const has = (f: string) => flags.includes(f);
  const teamCol = TEAM_COLORS[team];
  const body = new THREE.Group();
  g.add(body);
  g.userData.body = body;

  const quad = has('quad');
  let headY = h * 0.82, headZ = 0;
  if (quad) {
    const len = has('wolf') || has('croc') ? w * 2.2 : w * 1.9;
    box(body, col, w * 0.95, h * 0.5, len, 0, h * 0.55, 0);
    for (const sx of [-1, 1]) for (const sz of [-1, 1]) cyl(body, col, w * 0.14, h * 0.4, sx * w * 0.32, h * 0.2, sz * len * 0.34);
    headY = h * 0.85; headZ = len * 0.55;
    if (d.cls === 'cav' || has('crest') && d.cls !== 'myth') {
      // rider
      cyl(body, teamCol, w * 0.28, h * 0.55, 0, h * 1.05, -len * 0.05);
      sph(body, SKIN, w * 0.2, 0, h * 1.5, -len * 0.05);
      headY = h * 0.78; headZ = len * 0.5;
    }
    if (has('croc')) { box(body, col, w * 0.7, h * 0.35, len * 0.9, 0, h * 0.45, -len * 0.95); box(body, 0x8aa070, w * 0.8, h * 0.3, len * 0.55, 0, h * 0.7, len * 0.7); }
    else if (has('wolf')) { box(body, 0x333a46, w * 0.5, h * 0.3, len * 0.6, 0, h * 0.6, len * 0.7); cone(body, col, w * 0.2, h * 0.4, w * 0.22, h * 1.0, len * 0.35); cone(body, col, w * 0.2, h * 0.4, -w * 0.22, h * 1.0, len * 0.35); }
    else if (has('lion')) { sph(body, 0xc7893a, w * 0.55, 0, h * 0.85, len * 0.5); sph(body, 0xe8c070, w * 0.34, 0, h * 0.8, len * 0.78); }
    else sph(body, col, w * 0.32, 0, headY, headZ);
  } else {
    const bodyH = h * 0.58;
    add(body, taperGeo, mat(has('skull') ? 0xcfcdb8 : col), w, bodyH, w * 0.8, 0, h * 0.2 + bodyH / 2, 0);
    // team sash
    add(body, G.cyl, mat(teamCol), w * 0.8, h * 0.1, w * 0.68, 0, h * 0.5, 0);
    // legs
    for (const sx of [-1, 1]) cyl(body, has('skull') ? 0xcfcdb8 : 0x57463a, w * 0.16, h * 0.28, sx * w * 0.28, h * 0.14, 0);
    if (has('tail')) cone(body, 0x3f7a4b, w * 0.5, h * 0.7, 0, h * 0.2, -w * 0.5, { rx: -1.15 });
    // head
    if (has('jackal')) {
      sph(body, 0x2a2638, w * 0.5, 0, headY, 0);
      box(body, 0x2a2638, w * 0.35, w * 0.35, w * 0.9, 0, headY - w * 0.08, w * 0.55);
      cone(body, 0x2a2638, w * 0.14, w * 0.7, w * 0.25, headY + w * 0.6, -w * 0.05); cone(body, 0x2a2638, w * 0.14, w * 0.7, -w * 0.25, headY + w * 0.6, -w * 0.05);
    } else sph(body, has('skull') ? 0xe8e6d4 : has('snakes') ? 0x7ecb8a : d.cls === 'myth' ? col : SKIN, w * 0.5, 0, headY, 0);
  }
  const hz = quad ? headZ : 0, hy = headY;
  const hw = w * 0.5;

  if (has('eye')) { sph(body, 0xffffff, hw * 0.55, 0, hy + 0.02, hw * 0.75); sph(body, 0x222222, hw * 0.22, 0, hy + 0.02, hw * 1.2); }
  if (has('horns')) { for (const s of [-1, 1]) cone(body, 0xefe6cf, hw * 0.28, hw * 1.5, s * hw * 0.95, hy + hw * 0.55, hz, { rz: -s * 0.9 }); }
  if (has('horn')) { for (const s of [-1, 1]) cone(body, 0xefe6cf, hw * 0.2, hw * 0.9, s * hw * 0.75, hy + hw * 0.7, hz, { rz: -s * 0.55 }); }
  if (has('tusk')) { for (const s of [-1, 1]) cone(body, 0xf5f0dc, hw * 0.12, hw * 0.7, s * hw * 0.38, hy - hw * 0.5, hz + hw * 0.7, { rx: 2.3 }); }
  if (has('snakes')) { for (let i = 0; i < 6; i++) { const a = (i / 6) * Math.PI * 2; cyl(body, 0x2f9a4f, hw * 0.12, hw * 1.2, Math.cos(a) * hw * 0.85, hy + hw * 0.4, Math.sin(a) * hw * 0.85, { rz: Math.cos(a) * 0.6, rx: Math.sin(a) * 0.6 }); } }
  if (has('crest')) box(body, teamCol, hw * 0.25, hw * 0.8, hw * 1.3, 0, hy + hw * 0.95, hz);
  if (has('crown')) cyl(body, 0xf0c040, hw * 0.85, hw * 0.35, 0, hy + hw * 0.7, hz);

  const arm = new THREE.Group();
  arm.position.set(w * 0.78, h * 0.55, 0);
  body.add(arm);
  g.userData.arm = arm;
  const big = has('giant') || d.cls === 'myth' ? 1.3 : 1;
  if (has('spear')) { cyl(arm, 0x7a5a3a, 0.035 * big, h * 1.5, 0, h * 0.2, w * 0.4, { rx: 0.15 }); cone(arm, 0xcfd6dc, 0.07 * big, 0.28 * big, 0, h * 0.95, w * 0.5, { rx: 0.15 }); }
  if (has('axe')) { cyl(arm, 0x7a5a3a, 0.04 * big, h * 0.75, 0, 0.1, 0.1); box(arm, 0xb8c2cc, 0.05 * big, 0.26 * big, 0.3 * big, 0, h * 0.4, 0.12); }
  if (has('club')) { cyl(arm, 0x6a4a2a, 0.12 * big, h * 0.7, 0, h * 0.1, 0.2, { rx: 0.5 }); cyl(arm, 0x5a3a1a, 0.2 * big, 0.5 * big, 0, h * 0.45, 0.45, { rx: 0.5 }); }
  if (has('tool')) { cyl(arm, 0x7a5a3a, 0.03, 0.55, 0, 0.1, 0.1, { rx: 0.4 }); box(arm, 0x9aa2aa, 0.05, 0.05, 0.22, 0, 0.33, 0.28); }
  if (has('bow')) { const b = add(arm, new THREE.TorusGeometry(0.3 * big, 0.025, 4, 12, Math.PI), mat(0x6a4a2a), 1, 1, 1, 0, 0.2, 0.15, { ry: Math.PI / 2 }); b.rotation.z = Math.PI / 2; }
  if (has('shield')) { const s = add(body, G.cyl, mat(teamCol), 0.3, 0.06, 0.3, -w * 0.85, h * 0.5, 0.05, { rz: Math.PI / 2 }); s.scale.set(0.3, 0.06, 0.3); }
  if (has('wings')) {
    const wc = has('flame') ? 0xff9a2a : 0xf4f6ff;
    for (const s of [-1, 1]) add(body, G.box, mat(wc, has('flame') ? { emissive: 0xff5a00 } : {}), 0.08, h * 0.9, w * 1.3, s * w * 0.9, h * 0.75, -w * 0.3, { rz: s * 0.7 });
  }
  if (has('flame')) { sph(body, 0xffd24a, w * 0.35, 0, h * 0.7, 0, 0xff7a00); cone(body, 0xff6a00, w * 0.25, w * 0.9, 0, h * 1.25, -w * 0.2, { rx: -0.4 }); }
  if (has('fly')) { body.position.y = 0.7; }

  // team ring at feet
  const ring = new THREE.Mesh(G.ring, new THREE.MeshBasicMaterial({ color: teamCol, transparent: true, opacity: 0.85, side: THREE.DoubleSide }));
  ring.rotation.x = -Math.PI / 2; ring.position.y = 0.03; ring.scale.setScalar(d.r * 1.25 + 0.1);
  g.add(ring);
  g.userData.fly = has('fly');
  return g;
}

// ------------------------------------------------------------------ buildings

interface Style { wall: number; roof: number; trim: number; accent: number }
const STYLES: Record<CivId, Style> = {
  greek: { wall: 0xece6d6, roof: 0xb85a30, trim: 0xd8d0bc, accent: 0x4a78c2 },
  egypt: { wall: 0xdcbf80, roof: 0xc29a50, trim: 0xf0d58a, accent: 0x3aa0a0 },
  norse: { wall: 0xa0723f, roof: 0x6a4a30, trim: 0xc09a60, accent: 0xaa3a30 },
};

// Triangular prism whose ridge runs along X (or Z): apex at y0+hgt, base at y0.
const prismGeo = (() => { const g = new THREE.CylinderGeometry(1, 1, 1, 3); g.rotateY(Math.PI / 2); g.rotateZ(Math.PI / 2); return g; })();
function gable(p: THREE.Object3D, col: number, len: number, wid: number, hgt: number, x: number, y0: number, z: number, alongZ = false) {
  const m = add(p, prismGeo, mat(col), len, hgt / 1.5, wid / 1.732, x, y0 + hgt / 3, z);
  if (alongZ) m.rotation.y = Math.PI / 2;
  // pale ridge cap so the roof pitch reads from above
  const ridge = add(p, G.box, mat(0xe8dcc0), alongZ ? 0.12 : len + 0.05, 0.1, alongZ ? len + 0.05 : 0.12, x, y0 + hgt, z);
  ridge.castShadow = false;
  return m;
}
function pyramid(p: THREE.Object3D, col: number, r: number, h: number, x: number, y: number, z: number) {
  const m = add(p, G.cone4, mat(col), r, h, r, x, y, z);
  m.rotation.y = Math.PI / 4;
  return m;
}

export function makeBuilding(id: BId, civ: CivId, team: number): THREE.Group {
  const d = BUILDINGS[id], st = STYLES[civ], g = new THREE.Group();
  const S = d.size;
  const inner = new THREE.Group();
  g.add(inner);
  g.userData.inner = inner;
  const flag = (x: number, y: number, z: number) => {
    cyl(g, 0x5a4a3a, 0.03, 1.1, x, y + 0.55, z);
    const f = add(g, G.plane, new THREE.MeshBasicMaterial({ color: TEAM_COLORS[team], side: THREE.DoubleSide }), 0.55, 0.32, 1, x + 0.28, y + 0.95, z);
    f.castShadow = false;
    g.userData.flag = f;
  };
  switch (id) {
    case 'tc': {
      const h = 1.5;
      box(inner, st.trim, S * 0.95, 0.25, S * 0.95, 0, 0.12, 0);
      box(inner, st.wall, S * 0.78, h, S * 0.78, 0, 0.25 + h / 2, 0);
      if (civ === 'greek') {
        for (let i = 0; i < 4; i++) for (const sd of [-1, 1]) cyl(inner, 0xffffff, 0.09, h + 0.1, -S * 0.36 + i * (S * 0.24), 0.25 + h / 2, sd * S * 0.46);
        pyramid(inner, st.roof, S * 0.62, 0.9, 0, 0.25 + h + 0.45, 0);
        sph(inner, 0xf0c040, 0.17, 0, 0.25 + h + 1.0, 0);
      } else if (civ === 'egypt') {
        for (let i = 0; i < 3; i++) box(inner, i % 2 ? st.trim : st.roof, S * (0.78 - i * 0.2), 0.32, S * (0.78 - i * 0.2), 0, 0.25 + h + 0.16 + i * 0.32, 0);
        for (const sx of [-1, 1]) for (const sz of [-1, 1]) pyramid(inner, 0xf0d070, 0.12, 1.2, sx * S * 0.45, 0.7, sz * S * 0.45);
      } else {
        gable(inner, st.roof, S * 0.95, S * 0.95, 1.0, 0, 0.25 + h, 0);
        for (const sx of [-1, 1]) cone(inner, 0xd8c080, 0.1, 0.7, sx * S * 0.5, 0.25 + h + 0.95, 0, { rz: -sx * 0.5 });
        box(inner, st.accent, 0.8, 0.8, 0.06, 0, 0.9, S * 0.4);
      }
      flag(S * 0.38, h + 0.25, S * 0.38);
      break;
    }
    case 'house': {
      box(inner, st.wall, 1.4, 0.8, 1.4, 0, 0.4, 0);
      if (civ === 'egypt') box(inner, st.roof, 1.55, 0.14, 1.55, 0, 0.85, 0);
      else if (civ === 'greek') pyramid(inner, st.roof, 1.1, 0.7, 0, 1.15, 0);
      else gable(inner, st.roof, 1.65, 1.6, 0.75, 0, 0.8, 0);
      box(inner, 0x3a2a1c, 0.35, 0.5, 0.05, 0, 0.3, 0.72);
      break;
    }
    case 'storehouse': {
      box(inner, st.wall, 1.6, 0.7, 1.4, 0, 0.35, -0.1);
      box(inner, st.roof, 1.9, 0.12, 1.9, 0, 0.82, 0, { rx: 0.15 });
      for (let i = 0; i < 3; i++) sph(inner, 0xc9b27a, 0.2, -0.5 + i * 0.5, 0.2, 0.75);
      break;
    }
    case 'farm': {
      box(inner, 0x6b4a2a, 1.9, 0.08, 1.9, 0, 0.04, 0);
      for (let i = 0; i < 4; i++) box(inner, 0xd8c04a, 1.7, 0.2, 0.2, 0, 0.18, -0.7 + i * 0.47);
      g.userData.crops = inner.children.slice(1);
      break;
    }
    case 'barracks': {
      const h = 1.0;
      box(inner, st.wall, S * 0.88, h, S * 0.7, 0, h / 2, 0);
      if (civ === 'egypt') box(inner, st.roof, S * 0.98, 0.16, S * 0.8, 0, h + 0.08, 0);
      else gable(inner, st.roof, S * 0.98, S * 0.85, 0.8, 0, h, 0);
      for (let i = 0; i < 3; i++) box(inner, TEAM_COLORS[team], 0.4, 0.55, 0.05, -0.8 + i * 0.8, 0.6, S * 0.36);
      for (let i = 0; i < 2; i++) cyl(inner, 0x7a5a3a, 0.03, 1.2, 1.2 + i * 0.2, 0.6, 1.2);
      flag(S * 0.4, h, -S * 0.3);
      break;
    }
    case 'temple': {
      box(inner, st.trim, S * 0.95, 0.2, S * 0.95, 0, 0.1, 0);
      if (civ === 'greek') {
        box(inner, st.wall, S * 0.6, 1.0, S * 0.6, 0, 0.7, 0);
        for (let i = 0; i < 5; i++) for (const sd of [-1, 1]) cyl(inner, 0xffffff, 0.09, 1.3, -S * 0.36 + i * S * 0.18, 0.85, sd * S * 0.4);
        box(inner, st.trim, S * 0.95, 0.15, S * 0.95, 0, 1.58, 0);
        gable(inner, st.roof, S * 0.98, S * 0.98, 0.7, 0, 1.65, 0);
      } else if (civ === 'egypt') {
        box(inner, st.wall, S * 0.7, 0.9, S * 0.7, 0, 0.65, 0);
        pyramid(inner, 0xf0d070, 0.45, 3.0, 0, 2.5, 0);
        sph(inner, 0xffe28a, 0.16, 0, 4.1, 0, 0xffb000);
      } else {
        box(inner, st.wall, S * 0.82, 1.0, S * 0.6, 0, 0.7, 0);
        gable(inner, st.roof, S * 0.95, S * 0.8, 0.9, 0, 1.2, 0);
        for (const s of [-1, 1]) cone(inner, 0xe8dcc0, 0.12, 0.9, s * S * 0.46, 2.1, 0, { rz: -s * 0.5 });
        sph(inner, 0x9ad0ff, 0.2, 0, 2.5, 0, 0x3a90ff);
      }
      flag(S * 0.4, 0.2, S * 0.4);
      break;
    }
    case 'tower': {
      cyl(inner, st.wall, 0.62, 2.0, 0, 1.0, 0);
      cyl(inner, st.trim, 0.78, 0.3, 0, 2.1, 0);
      if (civ === 'egypt') box(inner, st.roof, 1.5, 0.15, 1.5, 0, 2.35, 0); else cone(inner, st.roof, 0.85, 0.8, 0, 2.7, 0);
      flag(0.55, 2.0, 0.55);
      break;
    }
    default: break;
  }
  // team-colored ground plate
  const plate = new THREE.Mesh(G.box, new THREE.MeshBasicMaterial({ color: TEAM_COLORS[team] }));
  plate.scale.set(S * 0.98, 0.03, S * 0.98); plate.position.y = 0.015;
  g.add(plate);
  return g;
}

// ------------------------------------------------------------------ resources

export function makeGold(): THREE.Group {
  const g = new THREE.Group();
  box(g, 0x6c6a66, 1.6, 0.8, 1.5, 0, 0.4, 0, { ry: 0.3 });
  box(g, 0x5a5854, 1.0, 1.0, 1.0, 0.3, 0.5, -0.3, { ry: 0.8 });
  for (let i = 0; i < 6; i++) {
    add(g, G.cone4, mat(0xf2c230, { emissive: 0x6a4a00 }), 0.18, 0.4, 0.18, -0.7 + (i % 3) * 0.7, 0.9 + (i % 2) * 0.1, -0.5 + Math.floor(i / 3) * 1.0, { rx: (i - 3) * 0.1, rz: (i % 3 - 1) * 0.2 });
  }
  return g;
}
export function makeBerries(): THREE.Group {
  const g = new THREE.Group();
  sph(g, 0x3f7a35, 0.38, 0, 0.3, 0);
  for (let i = 0; i < 6; i++) sph(g, 0xd23a4a, 0.08, Math.cos(i * 1.1) * 0.3, 0.35 + (i % 3) * 0.08, Math.sin(i * 1.1) * 0.3);
  return g;
}

export const treeTrunkGeo = new THREE.CylinderGeometry(0.07, 0.11, 0.7, 6);
export const treeLeafGeo = (() => {
  const a = new THREE.ConeGeometry(0.46, 0.85, 7); a.translate(0, -0.2, 0);
  const b = new THREE.ConeGeometry(0.34, 0.8, 7); b.translate(0, 0.38, 0);
  return mergeGeometries([a, b])!;
})();
