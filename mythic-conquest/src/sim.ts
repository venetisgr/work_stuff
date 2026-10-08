// Headless RTS simulation: map, pathfinding, economy, combat, production, god powers.
// No DOM / three.js here so it can be soak-tested in Node.

import {
  AGE_UP, BUILDINGS, BId, CARRY, CIVS, CivId, Cost, FARM_FOOD, GATHER_RATE, MAJORS, MAP_N, Mods, POP_MAX,
  PowerDef, Res, UNITS, UnitDef, baseMods, classMult,
} from './data';

export type EKind = 'unit' | 'bld' | 'res';
export interface QItem { kind: 'unit' | 'age'; unit?: string; t: number; total: number; cost: Cost; pop: number }
export interface Order { t: 'move' | 'amove' | 'attack' | 'gather' | 'build' | 'pray'; x: number; y: number; target: number; auto?: boolean }
export interface Pt { x: number; y: number }

export interface Ent {
  id: number; kind: EKind; def: string; owner: number; x: number; y: number; hp: number; maxHp: number; face: number; dead?: boolean;
  // units
  order: Order | null; path: Pt[]; pathFor: number; pathFails: number; repathT: number; stuck: number; state: string;
  carryType: Res | null; carryAmt: number; gatherAcc: number; atkT: number; scanT: number; target: number;
  slowT: number; slowF: number; stunT: number; life: number; home: Pt; resume: { target: number; type: Res } | null;
  // buildings & resources (footprint)
  tx: number; ty: number; size: number; done: boolean; progress: number; queue: QItem[]; rally: (Pt & { target: number }) | null;
  amount: number; rtype: string; cdT: number; prayers: number; prayersPrev: number;
}

export interface Player {
  id: number; civ: CivId; god: string; food: number; wood: number; gold: number; favor: number; age: number; mods: Mods;
  minors: string[]; unlocked: string[]; pop: number; popCap: number; queuedPop: number; pendingChoice: number; ageing: boolean;
  powerCd: number; alive: boolean; kills: number; losses: number; gathered: number; ai: boolean;
}

export interface Zone { x: number; y: number; r: number; life: number; owner: number; power: PowerDef; strikeT: number }

export type SimEvent =
  | { t: 'shot'; x: number; y: number; tx: number; ty: number; owner: number; myth: boolean }
  | { t: 'hit'; x: number; y: number }
  | { t: 'die'; id: number; x: number; y: number; kind: EKind; def: string; owner: number; size: number }
  | { t: 'strike'; x: number; y: number; r: number }
  | { t: 'power'; id: string; x: number; y: number; r: number; owner: number; color: number }
  | { t: 'msg'; text: string; owner: number }
  | { t: 'age'; owner: number; age: number }
  | { t: 'built'; id: number }
  | { t: 'win'; owner: number };

export interface SimOpts { civs: [CivId, CivId]; gods: [string, string]; seed: number; ai: [boolean, boolean] }

export function mulberry32(a: number) {
  return () => {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const CELL = 4;

export class Sim {
  readonly N = MAP_N;
  ents = new Map<number, Ent>();
  nextId = 1;
  blocked = new Uint8Array(MAP_N * MAP_N);
  players: Player[] = [];
  zones: Zone[] = [];
  events: SimEvent[] = [];
  time = 0;
  winner = -1;
  rng: () => number;
  private hash = new Map<number, Ent[]>();
  // A* scratch
  private g = new Float32Array(MAP_N * MAP_N);
  private par = new Int32Array(MAP_N * MAP_N);
  private seen = new Uint32Array(MAP_N * MAP_N);
  private stamp = 0;

  constructor(public opts: SimOpts) {
    this.rng = mulberry32(opts.seed);
    for (let i = 0; i < 2; i++) {
      const civ = CIVS[opts.civs[i]];
      const p: Player = {
        id: i, civ: civ.id, god: opts.gods[i], food: civ.start.food ?? 0, wood: civ.start.wood ?? 0, gold: civ.start.gold ?? 0,
        favor: civ.start.favor ?? 0, age: 1, mods: baseMods(), minors: [], unlocked: [], pop: 0, popCap: 0, queuedPop: 0,
        pendingChoice: 0, ageing: false, powerCd: 0, alive: true, kills: 0, losses: 0, gathered: 0, ai: opts.ai[i],
      };
      this.players.push(p);
      this.recomputeMods(p);
    }
    this.generateMap();
  }

  // ------------------------------------------------------------------ helpers
  get(id: number): Ent | undefined { const e = this.ents.get(id); return e && !e.dead ? e : undefined; }
  tile(x: number, y: number) { return Math.floor(y) * this.N + Math.floor(x); }
  ent(kind: EKind, def: string, owner: number, x: number, y: number): Ent {
    const e: Ent = {
      id: this.nextId++, kind, def, owner, x, y, hp: 1, maxHp: 1, face: 0,
      order: null, path: [], pathFor: -1, pathFails: 0, repathT: 0, stuck: 0, state: '', carryType: null, carryAmt: 0, gatherAcc: 0,
      atkT: 0, scanT: Math.random() * 0.4, target: -1, slowT: 0, slowF: 1, stunT: 0, life: -1, home: { x, y }, resume: null,
      tx: 0, ty: 0, size: 0, done: true, progress: 1, queue: [], rally: null, amount: 0, rtype: '', cdT: 0, prayers: 0, prayersPrev: 0,
    };
    this.ents.set(e.id, e);
    return e;
  }
  emit(e: SimEvent) { if (this.events.length < 4000) this.events.push(e); }
  msg(text: string, owner: number) { this.emit({ t: 'msg', text, owner }); }
  udef(e: Ent): UnitDef { return UNITS[e.def]; }

  private setBlock(e: Ent, v: number) {
    for (let y = e.ty; y < e.ty + e.size; y++) for (let x = e.tx; x < e.tx + e.size; x++) this.blocked[y * this.N + x] = v;
  }
  isBlockedAt(x: number, y: number) {
    return x < 0.4 || y < 0.4 || x > this.N - 0.4 || y > this.N - 0.4 || this.blocked[this.tile(x, y)] === 1;
  }

  recomputeMods(p: Player) {
    const m = baseMods();
    const mul = (o?: Partial<Mods>) => { if (o) for (const k of Object.keys(o) as (keyof Mods)[]) m[k] *= o[k]!; };
    mul(MAJORS[p.god].mods);
    for (const id of p.minors) mul(CIVS[p.civ].minors.find((g) => g.id === id)?.mods);
    const oldHp = p.mods.hp;
    p.mods = m;
    if (m.hp !== oldHp) {
      const f = m.hp / oldHp;
      for (const e of this.ents.values()) if (e.kind === 'unit' && e.owner === p.id && !e.dead) { e.hp *= f; e.maxHp *= f; }
    }
  }

  // ------------------------------------------------------------------ map
  private addBuilding(def: string, owner: number, tx: number, ty: number, done: boolean): Ent {
    const d = BUILDINGS[def as BId];
    const e = this.ent('bld', def, owner, tx + d.size / 2, ty + d.size / 2);
    e.tx = tx; e.ty = ty; e.size = d.size; e.maxHp = d.hp; e.done = done; e.progress = done ? 1 : 0;
    e.hp = done ? d.hp : d.hp * 0.1;
    if (d.farm) { e.amount = FARM_FOOD; e.rtype = 'farm'; }
    this.setBlock(e, 1);
    return e;
  }
  private addResource(type: 'tree' | 'gold' | 'berries', tx: number, ty: number): Ent | null {
    const size = type === 'gold' ? 2 : 1;
    for (let y = ty; y < ty + size; y++) for (let x = tx; x < tx + size; x++) {
      if (x < 1 || y < 1 || x >= this.N - 1 || y >= this.N - 1 || this.blocked[y * this.N + x]) return null;
    }
    const e = this.ent('res', type, -1, tx + size / 2, ty + size / 2);
    e.tx = tx; e.ty = ty; e.size = size;
    e.amount = type === 'tree' ? 160 : type === 'gold' ? 900 : 260;
    e.maxHp = e.amount; e.hp = e.amount;
    e.rtype = type === 'tree' ? 'wood' : type === 'gold' ? 'gold' : 'food';
    this.setBlock(e, 1);
    return e;
  }

  private generateMap() {
    const N = this.N, r = this.rng;
    const bases = [{ x: 13, y: 66 }, { x: 66, y: 13 }];
    const center = { x: N / 2, y: N / 2 };
    const nearBase = (x: number, y: number, d: number) => bases.some((b) => Math.hypot(b.x - x, b.y - y) < d);
    // forests (neutral)
    for (let c = 0; c < 20; c++) {
      let cx = 0, cy = 0, ok = false;
      for (let tries = 0; tries < 30 && !ok; tries++) {
        cx = 4 + r() * (N - 8); cy = 4 + r() * (N - 8);
        ok = !nearBase(cx, cy, 17);
      }
      const n = 10 + Math.floor(r() * 16);
      for (let i = 0; i < n; i++) {
        const a = r() * Math.PI * 2, d = Math.sqrt(r()) * 4.2;
        const x = Math.floor(cx + Math.cos(a) * d), y = Math.floor(cy + Math.sin(a) * d);
        if (!nearBase(x, y, 14) || Math.hypot(x - center.x, y - center.y) < 4) continue;
        if (Math.hypot(x - center.x, y - center.y) < 4) continue;
        this.addResource('tree', x, y);
      }
    }
    // contested gold in the middle & scattered gold
    for (const [dx, dy] of [[-5, 3], [5, -3], [0, 9], [0, -9]]) this.addResource('gold', Math.floor(center.x + dx), Math.floor(center.y + dy));
    // starting bases
    bases.forEach((b, i) => {
      const dir = i === 0 ? 1 : -1;      // toward the center
      const tc = this.addBuilding('tc', i, b.x - 1, b.y - 1, true);
      // gold
      this.addResource('gold', b.x + dir * 9, b.y - dir * 3);
      this.addResource('gold', b.x + dir * 11, b.y - dir * 1);
      // berries (arc)
      for (let k = 0; k < 6; k++) this.addResource('berries', b.x - dir * 6 + (k % 3) * 1, b.y + dir * (-1 + Math.floor(k / 3) * 1) - dir * 4);
      // forest near the base
      for (let k = 0; k < 46; k++) {
        const a = r() * Math.PI * 2, d = 3 + Math.sqrt(r()) * 3.6;
        const x = Math.floor(b.x - dir * 4 + Math.cos(a) * d - dir * 6), y = Math.floor(b.y + dir * 8 + Math.sin(a) * d * 0.8);
        if (Math.hypot(x - b.x, y - b.y) > 7) this.addResource('tree', x, y);
      }
      for (let k = 0; k < 26; k++) {
        const a = r() * Math.PI * 2, d = 2 + Math.sqrt(r()) * 3;
        const x = Math.floor(b.x + dir * 8 + Math.cos(a) * d + dir * 3), y = Math.floor(b.y + dir * 8 + Math.sin(a) * d);
        if (Math.hypot(x - b.x, y - b.y) > 7) this.addResource('tree', x, y);
      }
      // villagers
      for (let k = 0; k < 5; k++) this.spawnUnit(`${this.players[i].civ}_vil`, i, tc.x + (k - 2) * 0.7, tc.y + 2.6);
    });
  }

  // ------------------------------------------------------------------ spawning
  spawnUnit(defId: string, owner: number, x: number, y: number): Ent {
    const d = UNITS[defId];
    const p = this.players[owner];
    const e = this.ent('unit', defId, owner, x, y);
    e.maxHp = d.hp * p.mods.hp; e.hp = e.maxHp;
    e.home = { x, y };
    this.unstick(e);
    return e;
  }

  private unstick(e: Ent) {
    if (!this.isBlockedAt(e.x, e.y)) return;
    const f = this.freeTileNear(e.x, e.y, 8);
    if (f) { e.x = f.x; e.y = f.y; }
  }

  /** nearest free tile center to (x,y) by expanding rings */
  freeTileNear(x: number, y: number, maxR = 6): Pt | null {
    const cx = Math.floor(x), cy = Math.floor(y);
    for (let r = 0; r <= maxR; r++) {
      let best: Pt | null = null, bd = 1e9;
      for (let dy = -r; dy <= r; dy++) for (let dx = -r; dx <= r; dx++) {
        if (Math.max(Math.abs(dx), Math.abs(dy)) !== r) continue;
        const tx = cx + dx, ty = cy + dy;
        if (tx < 1 || ty < 1 || tx >= this.N - 1 || ty >= this.N - 1 || this.blocked[ty * this.N + tx]) continue;
        const d = Math.hypot(tx + 0.5 - x, ty + 0.5 - y);
        if (d < bd) { bd = d; best = { x: tx + 0.5, y: ty + 0.5 }; }
      }
      if (best) return best;
    }
    return null;
  }

  // ------------------------------------------------------------------ geometry
  distTo(u: Ent, t: Ent): number {
    if (t.kind === 'unit') return Math.max(0, Math.hypot(u.x - t.x, u.y - t.y) - this.udef(u).r - this.udef(t).r);
    const dx = Math.max(t.tx - u.x, 0, u.x - (t.tx + t.size)), dy = Math.max(t.ty - u.y, 0, u.y - (t.ty + t.size));
    return Math.hypot(dx, dy);
  }
  centerDist(a: Pt, b: Pt) { return Math.hypot(a.x - b.x, a.y - b.y); }

  los(x0: number, y0: number, x1: number, y1: number): boolean {
    const dx = x1 - x0, dy = y1 - y0, d = Math.hypot(dx, dy);
    if (d < 0.01) return true;
    const steps = Math.ceil(d / 0.35), nx = -dy / d * 0.27, ny = dx / d * 0.27;
    for (let i = 1; i <= steps; i++) {
      const t = i / steps, x = x0 + dx * t, y = y0 + dy * t;
      if (this.isBlockedAt(x, y) || this.isBlockedAt(x + nx, y + ny) || this.isBlockedAt(x - nx, y - ny)) return false;
    }
    return true;
  }

  // ------------------------------------------------------------------ pathfinding (A*)
  findPath(sx: number, sy: number, gx: number, gy: number): Pt[] | null {
    const N = this.N;
    const s = this.tile(sx, sy), g = this.tile(gx, gy);
    if (this.blocked[g]) return null;
    if (s === g) return [{ x: gx, y: gy }];
    this.stamp++;
    const stamp = this.stamp, G = this.g, P = this.par, S = this.seen;
    const hI: number[] = [], hF: number[] = [];
    const push = (i: number, f: number) => {
      let k = hI.length; hI.push(i); hF.push(f);
      while (k > 0) {
        const p = (k - 1) >> 1;
        if (hF[p] <= hF[k]) break;
        [hI[p], hI[k]] = [hI[k], hI[p]]; [hF[p], hF[k]] = [hF[k], hF[p]]; k = p;
      }
    };
    const pop = () => {
      const top = hI[0], li = hI.pop()!, lf = hF.pop()!;
      if (hI.length) {
        hI[0] = li; hF[0] = lf;
        let k = 0;
        for (;;) {
          const l = 2 * k + 1, r = l + 1; let m = k;
          if (l < hI.length && hF[l] < hF[m]) m = l;
          if (r < hI.length && hF[r] < hF[m]) m = r;
          if (m === k) break;
          [hI[m], hI[k]] = [hI[k], hI[m]]; [hF[m], hF[k]] = [hF[k], hF[m]]; k = m;
        }
      }
      return top;
    };
    const gx0 = g % N, gy0 = (g / N) | 0;
    const h = (i: number) => {
      const dx = Math.abs((i % N) - gx0), dy = Math.abs(((i / N) | 0) - gy0);
      return Math.max(dx, dy) + 0.414 * Math.min(dx, dy);
    };
    G[s] = 0; P[s] = -1; S[s] = stamp; push(s, h(s));
    const closed = new Set<number>();
    let found = false, exp = 0;
    const DX = [1, -1, 0, 0, 1, 1, -1, -1], DY = [0, 0, 1, -1, 1, -1, 1, -1];
    while (hI.length && exp < 7000) {
      const cur = pop();
      if (closed.has(cur)) continue;
      closed.add(cur); exp++;
      if (cur === g) { found = true; break; }
      const cx = cur % N, cy = (cur / N) | 0;
      for (let k = 0; k < 8; k++) {
        const nx = cx + DX[k], ny = cy + DY[k];
        if (nx < 0 || ny < 0 || nx >= N || ny >= N) continue;
        const ni = ny * N + nx;
        if (this.blocked[ni] || closed.has(ni)) continue;
        if (k >= 4 && (this.blocked[cy * N + nx] || this.blocked[ny * N + cx])) continue;
        const ng = G[cur] + (k >= 4 ? 1.414 : 1);
        if (S[ni] !== stamp || ng < G[ni]) { S[ni] = stamp; G[ni] = ng; P[ni] = cur; push(ni, ng + h(ni)); }
      }
    }
    if (!found) return null;
    const raw: Pt[] = [];
    for (let i = g; i !== -1 && i !== s; i = P[i]) raw.push({ x: (i % N) + 0.5, y: ((i / N) | 0) + 0.5 });
    raw.reverse();
    if (raw.length) raw[raw.length - 1] = { x: gx, y: gy };
    // string-pull
    const out: Pt[] = [];
    let cx = sx, cy = sy, i = 0;
    while (i < raw.length) {
      let j = raw.length - 1;
      while (j > i && !this.los(cx, cy, raw[j].x, raw[j].y)) j--;
      out.push(raw[j]); cx = raw[j].x; cy = raw[j].y; i = j + 1;
    }
    return out;
  }

  private pathTo(u: Ent, x: number, y: number): boolean {
    let gx = x, gy = y;
    if (this.isBlockedAt(gx, gy)) { const f = this.freeTileNear(gx, gy, 5); if (!f) { u.path = []; return false; } gx = f.x; gy = f.y; }
    if (this.los(u.x, u.y, gx, gy)) { u.path = [{ x: gx, y: gy }]; return true; }
    const p = this.findPath(u.x, u.y, gx, gy);
    u.path = p ?? [];
    return !!p;
  }

  private pathToEntity(u: Ent, t: Ent): boolean {
    const cands: Pt[] = [];
    for (let y = t.ty - 1; y <= t.ty + t.size; y++) for (let x = t.tx - 1; x <= t.tx + t.size; x++) {
      const edge = x === t.tx - 1 || x === t.tx + t.size || y === t.ty - 1 || y === t.ty + t.size;
      if (!edge || x < 1 || y < 1 || x >= this.N - 1 || y >= this.N - 1 || this.blocked[y * this.N + x]) continue;
      cands.push({ x: x + 0.5, y: y + 0.5 });
    }
    cands.sort((a, b) => Math.hypot(a.x - u.x, a.y - u.y) - Math.hypot(b.x - u.x, b.y - u.y));
    for (let i = 0; i < Math.min(3, cands.length); i++) {
      if (this.pathTo(u, cands[i].x, cands[i].y)) return true;
    }
    u.path = [];
    return false;
  }

  private speedOf(u: Ent) {
    const d = this.udef(u);
    return d.speed * this.players[u.owner].mods.speed * (u.slowT > 0 ? u.slowF : 1);
  }

  private tryMove(u: Ent, nx: number, ny: number): boolean {
    if (!this.isBlockedAt(nx, ny)) { u.x = nx; u.y = ny; return true; }
    if (!this.isBlockedAt(nx, u.y)) { u.x = nx; return true; }
    if (!this.isBlockedAt(u.x, ny)) { u.y = ny; return true; }
    return false;
  }

  private followPath(u: Ent, dt: number): 'moving' | 'done' | 'blocked' {
    while (u.path.length) {
      const w = u.path[0], dx = w.x - u.x, dy = w.y - u.y, d = Math.hypot(dx, dy);
      if (d < 0.12) { u.path.shift(); continue; }
      const step = Math.min(d, this.speedOf(u) * dt);
      const ok = this.tryMove(u, u.x + (dx / d) * step, u.y + (dy / d) * step);
      u.face = Math.atan2(dx, dy);
      if (!ok) { u.stuck += dt; if (u.stuck > 0.5) { u.stuck = 0; u.path = []; return 'blocked'; } } else u.stuck = 0;
      return 'moving';
    }
    return 'done';
  }

  private approach(u: Ent, t: Ent, reach: number, dt: number): 'in' | 'moving' | 'fail' {
    if (this.distTo(u, t) <= reach) { u.path = []; return 'in'; }
    u.repathT -= dt;
    const mobile = t.kind === 'unit';
    if (mobile) {
      if (this.los(u.x, u.y, t.x, t.y)) u.path = [{ x: t.x, y: t.y }];
      else if (u.path.length === 0 || u.repathT <= 0) { u.repathT = 0.6; this.pathTo(u, t.x, t.y); }
    } else if (u.pathFor !== t.id || u.path.length === 0) {
      if (u.pathFails >= 3) return 'fail';
      u.pathFor = t.id;
      if (!this.pathToEntity(u, t)) { u.pathFails++; return 'moving'; }
    }
    const r = this.followPath(u, dt);
    if ((r === 'blocked' || r === 'done') && !mobile) { u.pathFails++; u.pathFor = -1; if (u.pathFails >= 3) return 'fail'; }
    return 'moving';
  }

  private moveTo(u: Ent, x: number, y: number, dt: number): boolean {
    // returns true when arrived
    if (u.path.length === 0) {
      if (Math.hypot(u.x - x, u.y - y) < 0.5) return true;
      if (u.pathFails >= 3) return true;
      if (!this.pathTo(u, x, y)) { u.pathFails++; return false; }
    }
    const r = this.followPath(u, dt);
    if (r === 'blocked') u.pathFails++;
    return r === 'done' && Math.hypot(u.x - x, u.y - y) < 1.5;
  }

  // ------------------------------------------------------------------ orders (public API)
  private setOrder(u: Ent, o: Order | null) {
    u.order = o; u.path = []; u.pathFor = -1; u.pathFails = 0; u.target = -1; u.repathT = 0;
    if (!o || o.t !== 'build') u.resume = null;
    u.state = o?.t === 'gather' ? 'toRes' : '';
  }
  private owned(owner: number, ids: number[]): Ent[] {
    const out: Ent[] = [];
    for (const id of ids) { const e = this.get(id); if (e && e.kind === 'unit' && e.owner === owner) out.push(e); }
    return out;
  }

  cmdMove(owner: number, ids: number[], x: number, y: number, attackMove = false) {
    const us = this.owned(owner, ids);
    const n = us.length;
    us.forEach((u, i) => {
      // loose formation
      const cols = Math.ceil(Math.sqrt(n)), dx = ((i % cols) - (cols - 1) / 2) * 0.9, dy = (Math.floor(i / cols) - (cols - 1) / 2) * 0.9;
      const px = Math.max(1, Math.min(this.N - 1, x + (n > 1 ? dx : 0))), py = Math.max(1, Math.min(this.N - 1, y + (n > 1 ? dy : 0)));
      this.setOrder(u, { t: attackMove ? 'amove' : 'move', x: px, y: py, target: -1 });
    });
  }

  /** Right-click on an entity: gather / attack / build / pray / follow depending on both sides. */
  cmdTarget(owner: number, ids: number[], targetId: number) {
    const t = this.get(targetId);
    if (!t) return;
    for (const u of this.owned(owner, ids)) {
      const d = this.udef(u);
      if (t.owner >= 0 && t.owner !== owner) {
        if (d.cls !== 'vil' || t.kind === 'unit') this.setOrder(u, { t: 'attack', x: t.x, y: t.y, target: t.id });
        else this.setOrder(u, { t: 'attack', x: t.x, y: t.y, target: t.id });
      } else if (t.kind === 'res' && d.cls === 'vil') {
        u.carryType = u.carryType && u.carryType !== (t.rtype as Res) ? null : u.carryType;
        if (u.carryType !== t.rtype) u.carryAmt = 0;
        this.setOrder(u, { t: 'gather', x: t.x, y: t.y, target: t.id });
        u.carryType = t.rtype as Res;
      } else if (t.kind === 'bld' && t.owner === owner) {
        if (!t.done && d.cls === 'vil') this.setOrder(u, { t: 'build', x: t.x, y: t.y, target: t.id });
        else if (t.done && t.rtype === 'farm' && d.cls === 'vil') {
          this.setOrder(u, { t: 'gather', x: t.x, y: t.y, target: t.id }); u.carryType = 'food';
        } else if (t.done && t.def === 'temple' && d.cls === 'vil' && this.players[owner].civ === 'greek') {
          this.setOrder(u, { t: 'pray', x: t.x, y: t.y, target: t.id });
        } else if (t.hp < t.maxHp && d.cls === 'vil') this.setOrder(u, { t: 'build', x: t.x, y: t.y, target: t.id });
        else this.setOrder(u, { t: 'move', x: t.x, y: t.y + t.size / 2 + 1, target: -1 });
      } else this.setOrder(u, { t: 'move', x: t.x, y: t.y, target: -1 });
    }
  }

  canPlace(def: BId, tx: number, ty: number): boolean {
    const d = BUILDINGS[def];
    if (tx < 1 || ty < 1 || tx + d.size > this.N - 1 || ty + d.size > this.N - 1) return false;
    for (let y = ty; y < ty + d.size; y++) for (let x = tx; x < tx + d.size; x++) if (this.blocked[y * this.N + x]) return false;
    return true;
  }

  canAfford(p: Player, c: Cost) {
    return p.food >= (c.food ?? 0) && p.wood >= (c.wood ?? 0) && p.gold >= (c.gold ?? 0) && p.favor >= (c.favor ?? 0);
  }
  pay(p: Player, c: Cost, sign = -1) {
    p.food += sign * (c.food ?? 0); p.wood += sign * (c.wood ?? 0); p.gold += sign * (c.gold ?? 0); p.favor += sign * (c.favor ?? 0);
  }

  cmdBuild(owner: number, ids: number[], def: BId, tx: number, ty: number): Ent | null {
    const p = this.players[owner], d = BUILDINGS[def];
    const builders = this.owned(owner, ids).filter((u) => this.udef(u).cls === 'vil');
    if (!builders.length || p.age < d.age || !this.canAfford(p, d.cost) || !this.canPlace(def, tx, ty)) return null;
    this.pay(p, d.cost);
    const b = this.addBuilding(def, owner, tx, ty, false);
    for (const u of builders) {
      // remember what the villager was doing so it goes back to work afterwards
      const prev = u.order?.t === 'gather' && u.carryType ? { target: u.order.target, type: u.carryType } : u.resume;
      this.setOrder(u, { t: 'build', x: b.x, y: b.y, target: b.id });
      u.resume = prev;
      this.unstick(u);
    }
    // units standing in the footprint get nudged out
    for (const e of this.ents.values()) if (e.kind === 'unit') this.unstick(e);
    return b;
  }

  train(owner: number, bldId: number, unitId: string): boolean {
    const b = this.get(bldId), p = this.players[owner], d = UNITS[unitId];
    if (!b || b.owner !== owner || !b.done || !d || d.at !== b.def) return false;
    if (d.civ !== p.civ || p.age < d.age || b.queue.length >= 5) return false;
    if (d.at === 'temple' && !p.unlocked.includes(d.id)) return false;
    if (!this.canAfford(p, d.cost) || p.pop + p.queuedPop + d.pop > Math.min(POP_MAX, p.popCap)) return false;
    this.pay(p, d.cost);
    p.queuedPop += d.pop;
    b.queue.push({ kind: 'unit', unit: d.id, t: d.time, total: d.time, cost: d.cost, pop: d.pop });
    return true;
  }

  ageUp(owner: number): boolean {
    const p = this.players[owner];
    const tc = [...this.ents.values()].find((e) => e.owner === owner && e.def === 'tc' && e.done && !e.dead);
    const next = p.age + 1;
    if (!tc || p.ageing || p.pendingChoice || next > 4) return false;
    const c = AGE_UP[next];
    if (next >= 3 && !this.hasBuilding(owner, 'temple')) return false;
    const cost: Cost = { food: c.food, gold: c.gold, favor: c.favor };
    if (!this.canAfford(p, cost)) return false;
    this.pay(p, cost);
    p.ageing = true;
    tc.queue.push({ kind: 'age', t: c.time, total: c.time, cost, pop: 0 });
    return true;
  }
  hasBuilding(owner: number, def: string) {
    for (const e of this.ents.values()) if (e.owner === owner && e.def === def && e.done && !e.dead) return true;
    return false;
  }

  cancelQueue(owner: number, bldId: number, idx: number) {
    const b = this.get(bldId);
    if (!b || b.owner !== owner || !b.queue[idx]) return;
    const q = b.queue.splice(idx, 1)[0], p = this.players[owner];
    this.pay(p, q.cost, +1);
    p.queuedPop -= q.pop;
    if (q.kind === 'age') p.ageing = false;
  }

  chooseMinor(owner: number, id: string): boolean {
    const p = this.players[owner];
    const g = CIVS[p.civ].minors.find((m) => m.id === id);
    if (!g || !p.pendingChoice || g.age !== p.pendingChoice) return false;
    p.minors.push(id);
    if (g.unlock) p.unlocked.push(g.unlock);
    p.pendingChoice = 0;
    this.recomputeMods(p);
    this.msg(`${g.name} answers your call. ${g.blurb}`, owner);
    return true;
  }

  setRally(owner: number, bldId: number, x: number, y: number, target: number) {
    const b = this.get(bldId);
    if (b && b.owner === owner) b.rally = { x, y, target };
  }

  castPower(owner: number, x: number, y: number): boolean {
    const p = this.players[owner], god = MAJORS[p.god], pw = god.power;
    if (p.age < 2 || p.powerCd > 0 || p.favor < pw.cost) return false;
    p.favor -= pw.cost; p.powerCd = pw.cd;
    x = Math.max(1, Math.min(this.N - 1, x)); y = Math.max(1, Math.min(this.N - 1, y));
    this.emit({ t: 'power', id: pw.id, x, y, r: pw.r, owner, color: pw.color });
    this.msg(`${god.name} unleashes ${pw.name}!`, -1);
    const enemies = (cb: (e: Ent) => void, r = pw.r) => this.unitsNear(x, y, r, (e) => { if (e.owner !== owner) cb(e); });
    switch (pw.kind) {
      case 'zone': this.zones.push({ x, y, r: pw.r, life: pw.life!, owner, power: pw, strikeT: 0 }); break;
      case 'heal':
        this.unitsNear(x, y, pw.r, (e) => { if (e.owner === owner) e.hp = Math.min(e.maxHp, e.hp + pw.amount!); });
        break;
      case 'summon':
        for (let i = 0; i < pw.n!; i++) {
          const a = (i / pw.n!) * Math.PI * 2;
          const u = this.spawnUnit(pw.unit!, owner, x + Math.cos(a) * 1.6, y + Math.sin(a) * 1.6);
          u.life = pw.life!;
        }
        break;
      case 'hammer':
        enemies((e) => { this.damage(null, e, pw.dmg!, owner); e.stunT = pw.stun!; });
        for (const b of [...this.ents.values()]) {
          if (b.kind === 'bld' && b.owner !== owner && !b.dead && Math.hypot(b.x - x, b.y - y) < pw.r + b.size / 2) this.damage(null, b, pw.bldDmg!, owner);
        }
        this.emit({ t: 'strike', x, y, r: pw.r });
        break;
      case 'convert': {
        const cands: Ent[] = [];
        enemies((e) => { if (this.udef(e).cls !== 'vil') cands.push(e); });
        cands.sort((a, b) => Math.hypot(a.x - x, a.y - y) - Math.hypot(b.x - x, b.y - y));
        for (const e of cands.slice(0, pw.n!)) {
          const od = this.players[e.owner], nd = this.players[owner];
          e.owner = owner; e.order = null; e.path = []; e.maxHp *= nd.mods.hp / od.mods.hp; e.hp = Math.min(e.hp, e.maxHp);
          this.emit({ t: 'hit', x: e.x, y: e.y });
        }
        break;
      }
    }
    return true;
  }

  // ------------------------------------------------------------------ spatial queries
  private rebuildHash() {
    this.hash.clear();
    for (const e of this.ents.values()) {
      if (e.kind !== 'unit' || e.dead) continue;
      const k = Math.floor(e.x / CELL) * 1000 + Math.floor(e.y / CELL);
      const a = this.hash.get(k);
      if (a) a.push(e); else this.hash.set(k, [e]);
    }
  }
  unitsNear(x: number, y: number, r: number, cb: (e: Ent) => void) {
    const x0 = Math.floor((x - r) / CELL), x1 = Math.floor((x + r) / CELL), y0 = Math.floor((y - r) / CELL), y1 = Math.floor((y + r) / CELL);
    for (let cx = x0; cx <= x1; cx++) for (let cy = y0; cy <= y1; cy++) {
      const a = this.hash.get(cx * 1000 + cy);
      if (!a) continue;
      for (const e of a) if (!e.dead && Math.hypot(e.x - x, e.y - y) <= r) cb(e);
    }
  }

  findEnemy(u: Ent, radius: number, buildings: boolean): Ent | null {
    let best: Ent | null = null, bs = 1e9;
    this.unitsNear(u.x, u.y, radius, (e) => {
      if (e.owner === u.owner) return;
      const s = Math.hypot(e.x - u.x, e.y - u.y) + (this.udef(e).cls === 'vil' ? 3 : 0);
      if (s < bs) { bs = s; best = e; }
    });
    if (!best && buildings) {
      for (const b of this.ents.values()) {
        if (b.kind !== 'bld' || b.owner === u.owner || b.dead) continue;
        const s = this.distTo(u, b);
        if (s <= radius && s + 6 < bs) { bs = s + 6; best = b; }
      }
    }
    return best;
  }

  nearestResource(u: Ent, rtype: string, maxD = 18, exclude = -1): Ent | null {
    let best: Ent | null = null, bd = maxD;
    for (const r of this.ents.values()) {
      if (r.dead || r.id === exclude || r.rtype !== rtype || r.amount <= 0) continue;
      if (r.kind === 'res' || (r.kind === 'bld' && r.owner === u.owner && r.done && rtype === 'farm')) {
        const d = Math.hypot(r.x - u.x, r.y - u.y);
        if (d < bd) { bd = d; best = r; }
      }
    }
    return best;
  }
  /** Same resource kind: wood/gold/food (farm or berries) */
  nearestFood(u: Ent, maxD = 22, exclude = -1): Ent | null {
    return this.nearestResource(u, 'food', maxD, exclude) ?? this.nearestResource(u, 'farm', maxD, exclude);
  }
  nearestDrop(u: Ent): Ent | null {
    let best: Ent | null = null, bd = 1e9;
    for (const b of this.ents.values()) {
      if (b.kind !== 'bld' || b.owner !== u.owner || !b.done || b.dead || !BUILDINGS[b.def as BId].drop) continue;
      const d = Math.hypot(b.x - u.x, b.y - u.y);
      if (d < bd) { bd = d; best = b; }
    }
    return best;
  }

  // ------------------------------------------------------------------ combat
  damage(src: Ent | null, t: Ent, amount: number, srcOwner = src ? src.owner : -1) {
    if (t.dead) return;
    t.hp -= amount;
    this.emit({ t: 'hit', x: t.x, y: t.y });
    if (t.kind === 'unit' && t.hp > 0 && !t.order && src && this.udef(t).cls !== 'vil') {
      this.setOrder(t, { t: 'attack', x: src.x, y: src.y, target: src.id, auto: true });
    }
    if (t.hp <= 0) this.kill(t, srcOwner);
  }

  kill(t: Ent, killerOwner: number) {
    if (t.dead) return;
    t.dead = true;
    if (t.kind !== 'res') this.emit({ t: 'die', id: t.id, x: t.x, y: t.y, kind: t.kind, def: t.def, owner: t.owner, size: t.size });
    else this.emit({ t: 'die', id: t.id, x: t.x, y: t.y, kind: 'res', def: t.def, owner: -1, size: t.size });
    if (t.kind !== 'unit') this.setBlock(t, 0);
    if (t.owner >= 0 && t.kind !== 'res') {
      this.players[t.owner].losses++;
      if (killerOwner >= 0 && killerOwner !== t.owner) {
        const k = this.players[killerOwner];
        k.kills++;
        if (k.civ === 'norse') k.favor += (t.kind === 'unit' ? Math.max(1, UNITS[t.def].pop) * 1.2 : 4) * k.mods.favor;
      }
    }
    if (t.kind === 'bld') {
      // refund nothing; clear queue pop reservations
      const p = this.players[t.owner];
      for (const q of t.queue) { p.queuedPop -= q.pop; if (q.kind === 'age') p.ageing = false; }
    }
  }

  private strike(u: Ent, t: Ent) {
    const d = this.udef(u), p = this.players[u.owner];
    const tcls = t.kind === 'unit' ? this.udef(t).cls : null;
    let dmg = d.atk * p.mods.atk * (tcls ? classMult(d.cls, tcls) : d.bldMult);
    if (d.cls === 'vil' && t.kind === 'bld') dmg = 0.3;
    u.face = Math.atan2(t.x - u.x, t.y - u.y);
    if (d.range > 2.2) this.emit({ t: 'shot', x: u.x, y: u.y, tx: t.x, ty: t.y, owner: u.owner, myth: d.cls === 'myth' });
    if (d.slow && t.kind === 'unit') { t.slowT = 3; t.slowF = d.slow; }
    this.damage(u, t, dmg);
  }

  // ------------------------------------------------------------------ main tick
  tick(dt: number) {
    this.time += dt;
    this.rebuildHash();
    for (const p of this.players) {
      p.powerCd = Math.max(0, p.powerCd - dt);
      p.favor += 0.1 * p.mods.favor * dt;
    }
    this.updateZones(dt);
    for (const e of this.ents.values()) {
      if (e.dead) continue;
      if (e.kind === 'unit') this.updateUnit(e, dt);
      else if (e.kind === 'bld') this.updateBuilding(e, dt);
    }
    this.separate();
    for (const e of [...this.ents.values()]) if (e.dead) this.ents.delete(e.id);
    this.recount(dt);
    this.checkWin();
  }

  private recount(dt: number) {
    for (const p of this.players) { p.pop = 0; p.popCap = 0; }
    for (const e of this.ents.values()) {
      if (e.dead) continue;
      if (e.kind === 'unit') this.players[e.owner].pop += UNITS[e.def].pop;
      else if (e.kind === 'bld' && e.done && e.owner >= 0) {
        const d = BUILDINGS[e.def as BId];
        if (d.pop) this.players[e.owner].popCap += d.pop;
        e.prayersPrev = e.prayers; e.prayers = 0;
        if (e.def === 'temple' && this.players[e.owner].civ === 'egypt') this.players[e.owner].favor += 0.45 * this.players[e.owner].mods.favor * dt;
      }
    }
    for (const p of this.players) {
      p.popCap = Math.min(POP_MAX, p.popCap);
      // mercy rule: a player with no units and no food can always afford one more villager
      if (p.alive && p.pop === 0 && p.queuedPop === 0 && p.food < 50) p.food = 50;
    }
  }

  private checkWin() {
    if (this.winner >= 0) return;
    for (const p of this.players) {
      if (!p.alive) continue;
      let tc = false;
      for (const e of this.ents.values()) if (e.owner === p.id && e.def === 'tc' && !e.dead) { tc = true; break; }
      if (!tc) {
        p.alive = false;
        this.winner = 1 - p.id;
        this.emit({ t: 'win', owner: this.winner });
      }
    }
  }

  private updateZones(dt: number) {
    for (const z of this.zones) {
      z.life -= dt;
      const pw = z.power;
      if (pw.dps || pw.slow) {
        this.unitsNear(z.x, z.y, z.r, (e) => {
          if (e.owner === z.owner) return;
          if (pw.dps) this.damage(null, e, pw.dps * dt, z.owner);
          if (pw.slow) { e.slowT = Math.max(e.slowT, 0.3); e.slowF = pw.slow; }
        });
      }
      if (pw.bldDps) {
        for (const b of this.ents.values()) {
          if (b.kind === 'bld' && b.owner !== z.owner && !b.dead && Math.hypot(b.x - z.x, b.y - z.y) < z.r + b.size / 2) this.damage(null, b, pw.bldDps * dt, z.owner);
        }
      }
      if (pw.strikeEvery) {
        z.strikeT -= dt;
        while (z.strikeT <= 0) {
          z.strikeT += pw.strikeEvery;
          const a = this.rng() * Math.PI * 2, d = Math.sqrt(this.rng()) * z.r;
          const sx = z.x + Math.cos(a) * d, sy = z.y + Math.sin(a) * d;
          this.emit({ t: 'strike', x: sx, y: sy, r: pw.strikeR! });
          this.unitsNear(sx, sy, pw.strikeR!, (e) => { if (e.owner !== z.owner) this.damage(null, e, pw.strikeDmg!, z.owner); });
          for (const b of this.ents.values()) {
            if (b.kind === 'bld' && b.owner !== z.owner && !b.dead && Math.hypot(b.x - sx, b.y - sy) < pw.strikeR! + b.size / 2) this.damage(null, b, pw.strikeDmg! * 0.6, z.owner);
          }
        }
      }
    }
    this.zones = this.zones.filter((z) => z.life > 0);
  }

  private separate() {
    for (const a of this.ents.values()) {
      if (a.kind !== 'unit' || a.dead) continue;
      const ra = this.udef(a).r;
      this.unitsNear(a.x, a.y, 1.6, (b) => {
        if (b.id <= a.id) return;
        const rb = this.udef(b).r, min = (ra + rb) * 0.95;
        let dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy);
        if (d >= min) return;
        if (d < 0.001) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d = Math.hypot(dx, dy); }
        const push = (min - d) * 0.5, px = (dx / d) * push, py = (dy / d) * push;
        // moving units shove idle ones more
        const wa = a.order ? 0.35 : 1, wb = b.order ? 0.35 : 1, s = wa + wb;
        const nax = a.x - px * 2 * wa / s, nay = a.y - py * 2 * wa / s, nbx = b.x + px * 2 * wb / s, nby = b.y + py * 2 * wb / s;
        if (!this.isBlockedAt(nax, nay)) { a.x = nax; a.y = nay; }
        if (!this.isBlockedAt(nbx, nby)) { b.x = nbx; b.y = nby; }
      });
    }
  }

  // ------------------------------------------------------------------ unit AI
  private updateUnit(u: Ent, dt: number) {
    const d = this.udef(u), p = this.players[u.owner];
    if (u.life >= 0) { u.life -= dt; if (u.life <= 0) { this.kill(u, -1); return; } }
    if (d.regen) u.hp = Math.min(u.maxHp, u.hp + d.regen * dt);
    if (u.slowT > 0) u.slowT -= dt;
    if (u.stunT > 0) { u.stunT -= dt; return; }
    u.atkT = Math.max(0, u.atkT - dt);
    if (this.isBlockedAt(u.x, u.y)) this.unstick(u);
    const o = u.order;

    if (!o) {
      u.scanT -= dt;
      if (u.scanT <= 0 && d.cls !== 'vil') {
        u.scanT = 0.5;
        const e = this.findEnemy(u, Math.max(d.range + 2, d.sight - 1), false);
        if (e) this.setOrder(u, { t: 'attack', x: e.x, y: e.y, target: e.id, auto: true });
      }
      return;
    }

    switch (o.t) {
      case 'move':
        if (this.moveTo(u, o.x, o.y, dt)) this.setOrder(u, null);
        break;

      case 'amove': {
        let tgt = this.get(u.target);
        u.scanT -= dt;
        if (!tgt && u.scanT <= 0) {
          u.scanT = 0.4;
          const e = d.cls === 'vil' ? null : this.findEnemy(u, d.sight, true);
          if (e) { tgt = e; u.target = e.id; u.pathFor = -1; u.pathFails = 0; }
        }
        if (tgt) {
          const reach = d.range;
          const a = this.approach(u, tgt, reach, dt);
          if (a === 'in') { if (u.atkT <= 0) { u.atkT = d.cd; this.strike(u, tgt); } }
          else if (a === 'fail') { u.target = -1; u.pathFails = 0; }
        } else if (this.moveTo(u, o.x, o.y, dt)) this.setOrder(u, null);
        break;
      }

      case 'attack': {
        const t = this.get(o.target);
        if (!t || t.owner === u.owner) { this.setOrder(u, null); break; }
        if (o.auto && Math.hypot(t.x - u.x, t.y - u.y) > d.sight + 5) { this.setOrder(u, null); break; }
        const a = this.approach(u, t, d.range, dt);
        if (a === 'in') {
          if (u.atkT <= 0) { u.atkT = d.cd; this.strike(u, t); }
          else u.face = Math.atan2(t.x - u.x, t.y - u.y);
        } else if (a === 'fail') this.setOrder(u, null);
        break;
      }

      case 'build': {
        const b = this.get(o.target);
        if (!b) { this.setOrder(u, null); break; }
        if (b.done && b.hp >= b.maxHp) {
          // finished: farmers start working, others look for work nearby
          if (b.rtype === 'farm') { this.setOrder(u, { t: 'gather', x: b.x, y: b.y, target: b.id }); u.carryType = 'food'; }
          else this.resumeWork(u);
          break;
        }
        const a = this.approach(u, b, 1.0, dt);
        if (a === 'in') {
          u.state = 'work';
          const def = BUILDINGS[b.def as BId];
          if (!b.done) {
            b.progress += (dt / def.time) * 1;
            b.hp = Math.min(b.maxHp, b.hp + (b.maxHp * 0.9 * dt) / def.time);
            if (b.progress >= 1) {
              b.done = true; b.progress = 1; b.hp = b.maxHp;
              this.emit({ t: 'built', id: b.id });
            }
          } else b.hp = Math.min(b.maxHp, b.hp + b.maxHp * 0.04 * dt);
          u.face = Math.atan2(b.x - u.x, b.y - u.y);
        } else if (a === 'fail') this.setOrder(u, null);
        break;
      }

      case 'pray': {
        const t = this.get(o.target);
        if (!t || !t.done) { this.setOrder(u, null); break; }
        const a = this.approach(u, t, 1.2, dt);
        if (a === 'in') {
          t.prayers++;
          const rate = 0.5 * Math.min(1, 5 / Math.max(1, t.prayersPrev));
          p.favor += rate * p.mods.favor * dt;
          u.state = 'pray';
        } else if (a === 'fail') this.setOrder(u, null);
        break;
      }

      case 'gather': this.updateGather(u, o, dt, p); break;
    }
  }

  private updateGather(u: Ent, o: Order, dt: number, p: Player) {
    let res = this.get(o.target);
    if (u.carryAmt >= CARRY || (!res && u.carryAmt > 0)) u.state = 'toDrop';
    if (u.state === 'toDrop') {
      const dz = this.nearestDrop(u);
      if (!dz) { this.setOrder(u, null); return; }
      const a = this.approach(u, dz, 1.0, dt);
      if (a === 'in') {
        if (u.carryType) { (p as unknown as Record<string, number>)[u.carryType] += u.carryAmt; p.gathered += u.carryAmt; }
        u.carryAmt = 0; u.state = 'toRes'; u.pathFor = -1; u.pathFails = 0;
        if (!res) {
          const n = this.findSame(u);
          if (n) o.target = n.id; else this.setOrder(u, null);
        }
      } else if (a === 'fail') this.setOrder(u, null);
      return;
    }
    if (!res || res.amount <= 0) {
      const n = this.findSame(u);
      if (n) { o.target = n.id; u.pathFor = -1; u.pathFails = 0; } else if (u.carryAmt > 0) u.state = 'toDrop'; else this.setOrder(u, null);
      return;
    }
    if (res.kind === 'bld' && (res.owner !== u.owner || !res.done)) { this.setOrder(u, null); return; }
    const a = this.approach(u, res, 1.0, dt);
    if (a === 'in') {
      u.state = 'gather';
      u.face = Math.atan2(res.x - u.x, res.y - u.y);
      const key = res.def === 'tree' ? 'wood' : res.def === 'gold' ? 'gold' : res.def === 'berries' ? 'berries' : 'farm';
      u.gatherAcc += GATHER_RATE[key] * p.mods.gather * dt;
      while (u.gatherAcc >= 1 && u.carryAmt < CARRY && res.amount > 0) {
        u.gatherAcc -= 1; u.carryAmt++; res.amount--;
        u.carryType = res.rtype === 'farm' ? 'food' : (res.rtype as Res);
      }
      if (res.amount <= 0) this.depleted(res);
    } else if (a === 'fail') {
      const n = this.findSame(u, res.id);
      if (n) { o.target = n.id; u.pathFor = -1; u.pathFails = 0; } else this.setOrder(u, null);
    }
  }

  /** After building: go back to the previous job (or the nearest resource of that kind). */
  private resumeWork(u: Ent) {
    const r = u.resume;
    u.resume = null;
    if (r) {
      u.carryType = r.type;
      const t = this.get(r.target) ?? this.findSame(u);
      if (t) { this.setOrder(u, { t: 'gather', x: t.x, y: t.y, target: t.id }); u.carryType = r.type; return; }
    }
    this.setOrder(u, null);
  }

  private findSame(u: Ent, exclude = -1): Ent | null {
    const t = u.carryType;
    if (!t) return null;
    if (t === 'food') return this.nearestFood(u, 22, exclude);
    return this.nearestResource(u, t, 22, exclude);
  }

  private depleted(r: Ent) {
    this.kill(r, -1);
  }

  // ------------------------------------------------------------------ buildings
  private updateBuilding(b: Ent, dt: number) {
    if (!b.done || b.owner < 0) return;
    const def = BUILDINGS[b.def as BId], p = this.players[b.owner];
    if (def.atk) {
      b.cdT = Math.max(0, b.cdT - dt);
      b.scanT -= dt;
      if (b.cdT <= 0 && b.scanT <= 0) {
        b.scanT = 0.3;
        let best: Ent | null = null, bd = def.range!;
        this.unitsNear(b.x, b.y, def.range! + 1, (e) => {
          if (e.owner === b.owner) return;
          const dd = Math.hypot(e.x - b.x, e.y - b.y) - b.size / 2;
          if (dd < bd) { bd = dd; best = e; }
        });
        if (best) {
          b.cdT = def.cd!;
          const t = best as Ent;
          this.emit({ t: 'shot', x: b.x, y: b.y, tx: t.x, ty: t.y, owner: b.owner, myth: false });
          this.damage(null, t, def.atk * p.mods.atk, b.owner);
        }
      }
    }
    const q = b.queue[0];
    if (q) {
      q.t -= dt * p.mods.build;
      if (q.t <= 0) {
        b.queue.shift();
        if (q.kind === 'age') {
          p.ageing = false; p.age++; p.pendingChoice = p.age;
          this.emit({ t: 'age', owner: b.owner, age: p.age });
        } else {
          p.queuedPop -= q.pop;
          this.spawnTrained(b, q.unit!);
        }
      }
    }
  }

  private spawnTrained(b: Ent, unitId: string) {
    const rally = b.rally;
    const to = rally ?? { x: this.N / 2, y: this.N / 2, target: -1 };
    // find free ring tile closest to the rally point
    let best: Pt | null = null, bd = 1e9;
    for (let y = b.ty - 1; y <= b.ty + b.size; y++) for (let x = b.tx - 1; x <= b.tx + b.size; x++) {
      const edge = x === b.tx - 1 || x === b.tx + b.size || y === b.ty - 1 || y === b.ty + b.size;
      if (!edge || x < 1 || y < 1 || x >= this.N - 1 || y >= this.N - 1 || this.blocked[y * this.N + x]) continue;
      const d = Math.hypot(x + 0.5 - to.x, y + 0.5 - to.y) + this.rng() * 0.6;
      if (d < bd) { bd = d; best = { x: x + 0.5, y: y + 0.5 }; }
    }
    const pos = best ?? { x: b.x, y: b.y + b.size / 2 + 1 };
    const u = this.spawnUnit(unitId, b.owner, pos.x, pos.y);
    if (rally) {
      if (rally.target >= 0 && this.get(rally.target)) this.cmdTarget(b.owner, [u.id], rally.target);
      else this.cmdMove(b.owner, [u.id], rally.x, rally.y);
    }
    return u;
  }
}
