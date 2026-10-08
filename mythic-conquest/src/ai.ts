// Computer opponent: economy, build order, age-ups, god powers and attack waves.

import { BUILDINGS, BId, CIVS, MAJORS, POP_MAX, UNITS, civUnits } from './data';
import { Ent, Sim } from './sim';

export type Difficulty = 'easy' | 'normal' | 'hard';
const CFG = {
  easy: { vils: 18, wave: 12, gather: 0.85, think: 1.2, waveGrow: 3 },
  normal: { vils: 26, wave: 11, gather: 1.0, think: 0.8, waveGrow: 4 },
  hard: { vils: 34, wave: 9, gather: 1.2, think: 0.5, waveGrow: 5 },
};

export class AI {
  private t = 0;
  private waveSize: number;
  private attacking = false;
  private waveStart = 0;
  private lastWaveEnd = 0;
  private cfg;

  constructor(private sim: Sim, private me: number, diff: Difficulty) {
    this.cfg = CFG[diff];
    this.waveSize = this.cfg.wave;
    sim.players[me].mods.gather *= this.cfg.gather;
  }

  private mine(kind: 'unit' | 'bld', pred?: (e: Ent) => boolean): Ent[] {
    const out: Ent[] = [];
    for (const e of this.sim.ents.values()) if (!e.dead && e.owner === this.me && e.kind === kind && (!pred || pred(e))) out.push(e);
    return out;
  }

  update(dt: number) {
    this.t -= dt;
    if (this.t > 0) return;
    this.t = this.cfg.think;
    const s = this.sim, p = s.players[this.me];
    if (!p.alive) return;
    const units = this.mine('unit');
    const blds = this.mine('bld');
    const tc = blds.find((b) => b.def === 'tc' && b.done);
    if (!tc) return;
    const vils = units.filter((u) => UNITS[u.def].cls === 'vil');
    const army = units.filter((u) => UNITS[u.def].cls !== 'vil');
    const count = (d: string, doneOnly = false) => blds.filter((b) => b.def === d && (!doneOnly || b.done)).length;

    // pick minor god
    if (p.pendingChoice) {
      const opts = CIVS[p.civ].minors.filter((m) => m.age === p.pendingChoice);
      s.chooseMinor(this.me, opts[Math.floor(s.rng() * opts.length)].id);
    }

    this.economy(vils, blds, tc, count);
    this.production(tc, blds, vils, army, count);
    this.military(army, tc);
    this.powers(army);
  }

  // ------------------------------------------------------------ economy
  private economy(vils: Ent[], blds: Ent[], tc: Ent, count: (d: string, d2?: boolean) => number) {
    const s = this.sim, p = s.players[this.me];
    // distribution of gatherers
    const tally = { food: 0, wood: 0, gold: 0 };
    for (const v of vils) if (v.order?.t === 'gather' && v.carryType) tally[v.carryType]++;
    const n = Math.max(1, vils.length);
    // desired share per resource, damped by how much of it we already hold
    const base = { food: 0.4, wood: 0.36, gold: p.age >= 2 ? 0.26 : 0.16 };
    const stock = { food: p.food, wood: p.wood, gold: p.gold };
    const want = { food: 0, wood: 0, gold: 0 };
    let tot = 0;
    for (const k of ['food', 'wood', 'gold'] as const) { want[k] = base[k] / (1 + stock[k] / 450); tot += want[k]; }
    for (const k of ['food', 'wood', 'gold'] as const) want[k] = Math.max(0.04, want[k] / tot);
    // rebalance: pull one gatherer off a resource we are swimming in
    for (const k of ['gold', 'wood', 'food'] as const) {
      if (stock[k] > 700 && tally[k] / n > want[k] * 1.5 + 0.05) {
        const v = vils.find((x) => x.order?.t === 'gather' && x.carryType === k && x.carryAmt === 0);
        if (v) { s.cmdMove(this.me, [v.id], v.x, v.y); tally[k]--; break; }
      }
    }

    const builders = vils.filter((v) => v.order?.t === 'build');
    const idle = vils.filter((v) => !v.order);

    // houses
    const housing = blds.filter((b) => b.def === 'house' && !b.done).length;
    if (p.popCap < POP_MAX && p.pop + p.queuedPop >= p.popCap - 4 && housing === 0 && p.wood >= BUILDINGS.house.cost.wood!) {
      this.build('house', tc, [idle[0] ?? vils.find((v) => v.order?.t === 'gather' && v.carryType === 'wood') ?? vils[0]]);
    }
    // farms when berries run low
    const farms = count('farm');
    const berriesLeft = [...s.ents.values()].some((r) => r.def === 'berries' && !r.dead && Math.hypot(r.x - tc.x, r.y - tc.y) < 16);
    const farmsWanted = berriesLeft ? (vils.length > 14 ? 2 : 0) : Math.min(8, 2 + Math.floor(vils.length / 5));
    if (farms < farmsWanted && p.wood >= 60 && blds.filter((b) => b.def === 'farm' && !b.done).length < 2) {
      const v = idle[0] ?? vils.find((x) => x.order?.t === 'gather' && x.carryType === 'wood');
      if (v) this.build('farm', tc, [v], 3, 7);
    }
    // storehouse next to far-away wood / gold so gatherers do not walk across the map
    if (p.wood >= BUILDINGS.storehouse.cost.wood! && vils.length >= 10 && !blds.some((b) => b.def === 'storehouse' && !b.done)) {
      const drops = blds.filter((b) => (b.def === 'storehouse' || b.def === 'tc') && b.done);
      for (const v of vils) {
        if (v.order?.t !== 'gather' || (v.carryType !== 'wood' && v.carryType !== 'gold')) continue;
        const r = s.get(v.order.target);
        if (!r || r.kind !== 'res') continue;
        const nearest = Math.min(...drops.map((d) => Math.hypot(d.x - r.x, d.y - r.y)));
        if (nearest > 11) { this.buildNear('storehouse', r.x, r.y, [v]); break; }
      }
    }

    // idle villagers -> most under-staffed resource
    for (const v of idle) {
      const order = (['food', 'wood', 'gold'] as ('food' | 'wood' | 'gold')[]).sort((a, b) => tally[a] / n / want[a] - tally[b] / n / want[b]);
      let target: Ent | null = null;
      for (const r of order) {
        target = r === 'food' ? s.nearestFood(v, 40) : s.nearestResource(v, r, 40);
        if (target) { tally[r]++; break; }
      }
      if (target) s.cmdTarget(this.me, [v.id], target.id);
    }
    // unfinished buildings without builders
    for (const b of blds) {
      if (b.done || builders.some((x) => x.order?.target === b.id)) continue;
      const v = idle.find((x) => !x.order) ?? vils.find((x) => x.order?.t === 'gather' && x.carryType === 'wood');
      if (v) s.cmdTarget(this.me, [v.id], b.id);
    }
    // farms need farmers: keep 3 per farm up to what we have
    for (const b of blds) {
      if (b.rtype !== 'farm' || !b.done) continue;
      const farmers = vils.filter((v) => v.order?.t === 'gather' && v.order.target === b.id).length;
      if (farmers < 2) {
        const v = vils.find((x) => x.order?.t === 'gather' && x.carryType === 'wood' && tally.wood > 3) ?? idle.find((x) => !x.order);
        if (v) { s.cmdTarget(this.me, [v.id], b.id); tally.wood--; tally.food++; }
      }
    }
  }

  private build(def: BId, near: Ent, vils: (Ent | undefined)[], minR = 4, maxR = 9) {
    const vs = vils.filter((v): v is Ent => !!v);
    if (!vs.length) return false;
    const s = this.sim;
    for (let tries = 0; tries < 40; tries++) {
      const a = s.rng() * Math.PI * 2, d = minR + s.rng() * (maxR - minR);
      const tx = Math.floor(near.x + Math.cos(a) * d - BUILDINGS[def].size / 2), ty = Math.floor(near.y + Math.sin(a) * d - BUILDINGS[def].size / 2);
      if (s.canPlace(def, tx, ty) && this.leavesRoom(tx, ty, BUILDINGS[def].size)) {
        return !!s.cmdBuild(this.me, vs.map((v) => v.id), def, tx, ty);
      }
    }
    return false;
  }
  private buildNear(def: BId, x: number, y: number, vils: Ent[]) {
    const s = this.sim;
    for (let r = 2; r < 7; r++) for (let k = 0; k < 8; k++) {
      const a = (k / 8) * Math.PI * 2;
      const tx = Math.floor(x + Math.cos(a) * r), ty = Math.floor(y + Math.sin(a) * r);
      if (s.canPlace(def, tx, ty)) { s.cmdBuild(this.me, vils.map((v) => v.id), def, tx, ty); return; }
    }
  }
  /** don't wall in the base: keep a free ring around the footprint */
  private leavesRoom(tx: number, ty: number, size: number) {
    const s = this.sim;
    for (let y = ty - 1; y <= ty + size; y++) for (let x = tx - 1; x <= tx + size; x++) {
      const edge = x === tx - 1 || x === tx + size || y === ty - 1 || y === ty + size;
      if (edge && (x < 1 || y < 1 || x >= s.N - 1 || y >= s.N - 1 || s.blocked[y * s.N + x] === 1)) return false;
    }
    return true;
  }

  // ------------------------------------------------------------ production
  private production(tc: Ent, blds: Ent[], vils: Ent[], army: Ent[], count: (d: string, d2?: boolean) => number) {
    const s = this.sim, p = s.players[this.me];
    const vilId = `${p.civ}_vil`;
    // villagers
    if (vils.length + tc.queue.filter((q) => q.kind === 'unit').length < this.cfg.vils && tc.queue.length < 2) s.train(this.me, tc.id, vilId);

    // buildings
    const vcount = vils.length;
    const spare = vils.find((v) => v.order?.t === 'gather' && v.carryType === 'wood') ?? vils[0];
    if (count('barracks') < (p.age >= 3 ? 3 : p.age >= 2 ? 2 : 1) && vcount >= (count('barracks') === 0 ? 9 : 16)) {
      if (p.wood >= BUILDINGS.barracks.cost.wood! && blds.filter((b) => b.def === 'barracks' && !b.done).length === 0) this.build('barracks', tc, [spare], 5, 10);
    }
    if (p.age >= 2 && count('temple') < (p.age >= 4 ? 2 : 1) && p.wood >= 140 && p.gold >= 80 && blds.filter((b) => b.def === 'temple' && !b.done).length === 0) {
      this.build('temple', tc, [spare], 5, 10);
    }
    if (p.age >= 2 && count('tower') < Math.min(4, p.age + 1) && vcount > 14 && p.wood >= 90 && p.gold >= 160 && blds.filter((b) => b.def === 'tower' && !b.done).length === 0) {
      this.build('tower', tc, [spare], 7, 11);
    }

    // greek: put villagers to prayer
    const temple = blds.find((b) => b.def === 'temple' && b.done);
    if (temple && p.civ === 'greek') {
      const prayers = vils.filter((v) => v.order?.t === 'pray');
      if (p.favor > 180) { for (const v of prayers) s.cmdMove(this.me, [v.id], v.x, v.y); }
      else if (prayers.length < 4) {
        const v = vils.find((x) => x.order?.t === 'gather' && x.carryType === 'wood' && x.carryAmt < 4);
        if (v) s.cmdTarget(this.me, [v.id], temple.id);
      }
    }

    // age up
    const wantAge = (p.age === 1 && vcount >= 14) || (p.age === 2 && vcount >= 20 && army.length >= 6) || (p.age === 3 && vcount >= 22 && army.length >= 12);
    if (wantAge) s.ageUp(this.me);

    // army
    const barracks = blds.filter((b) => b.def === 'barracks' && b.done);
    // economy first: never starve villager production for the army
    const econOk = vils.length >= Math.min(8, this.cfg.vils) && (p.food >= 120 || vils.length >= this.cfg.vils);
    for (const b of econOk ? barracks : []) {
      if (b.queue.length >= 2) continue;
      const opts = civUnits(p.civ, 'barracks').filter((u) => u.age <= p.age);
      const roll = s.rng();
      const picks = [`${p.civ}_inf`, `${p.civ}_rng`, `${p.civ}_inf`, ...(p.age >= 2 ? [`${p.civ}_cav`] : [])];
      const choice = picks[Math.floor(roll * picks.length)];
      if (opts.some((u) => u.id === choice)) s.train(this.me, b.id, choice);
      // keep some gold in reserve for age-up when close
    }
    const tmp = blds.find((b) => b.def === 'temple' && b.done && b.queue.length < 2);
    if (tmp && p.unlocked.length && econOk) {
      const id = p.unlocked[Math.floor(s.rng() * p.unlocked.length)];
      s.train(this.me, tmp.id, id);
    }
    // rally
    for (const b of blds) if (b.done && b.def !== 'tc' && !b.rally) s.setRally(this.me, b.id, tc.x + (b.x > tc.x ? 6 : -6), tc.y + (b.y > tc.y ? 6 : -6), -1);
    if (!tc.rally) {
      const r = s.nearestResource(tc, 'wood', 30);
      if (r) s.setRally(this.me, tc.id, r.x, r.y, r.id);
    }
  }

  // ------------------------------------------------------------ military
  private military(army: Ent[], tc: Ent) {
    const s = this.sim;
    if (!army.length) { this.attacking = false; return; }
    // defend: enemies near base
    let threat: Ent | null = null;
    for (const e of s.ents.values()) {
      if (e.kind === 'unit' && !e.dead && e.owner !== this.me && Math.hypot(e.x - tc.x, e.y - tc.y) < 16) { threat = e; break; }
    }
    if (threat && !this.attacking) {
      const idle = army.filter((u) => !u.order || (u.order.t === 'move'));
      if (idle.length) s.cmdMove(this.me, idle.map((u) => u.id), threat.x, threat.y, true);
      return;
    }
    if (!this.attacking) {
      const stale = s.time - this.lastWaveEnd > 300 && army.length >= 6;
      if (army.length >= Math.min(this.waveSize, 22) || stale) {
        this.attacking = true;
        this.waveStart = s.time;
        s.msg('The enemy marches to war!', 1 - this.me);
      }
      return;
    }
    // attacking: head for the nearest enemy building (prefer TC)
    const enemyBlds = [...s.ents.values()].filter((b) => b.kind === 'bld' && b.owner !== this.me && !b.dead);
    if (!enemyBlds.length) return;
    const ref = army[0];
    const target = enemyBlds.find((b) => b.def === 'tc') ?? enemyBlds[0];
    enemyBlds.sort((a, b) => Math.hypot(a.x - ref.x, a.y - ref.y) - Math.hypot(b.x - ref.x, b.y - ref.y));
    const goal = Math.hypot(target.x - ref.x, target.y - ref.y) < 40 ? target : enemyBlds[0];
    const idle = army.filter((u) => !u.order);
    if (idle.length) s.cmdMove(this.me, idle.map((u) => u.id), goal.x, goal.y, true);
    // wave over: army is small now
    if (army.length <= Math.max(2, this.waveSize / 4) || s.time - this.waveStart > 240) {
      this.attacking = false;
      this.lastWaveEnd = s.time;
      this.waveSize += this.cfg.waveGrow;
      s.cmdMove(this.me, army.map((u) => u.id), tc.x + (tc.x < 40 ? 7 : -7), tc.y + (tc.y < 40 ? 7 : -7));
    }
  }

  // ------------------------------------------------------------ god powers
  private powers(army: Ent[]) {
    const s = this.sim, p = s.players[this.me], pw = MAJORS[p.god].power;
    if (p.age < 2 || p.powerCd > 0 || p.favor < pw.cost + 5) return;
    // best cluster of enemy units (or buildings for the earthquake)
    let best: { x: number; y: number; score: number } | null = null;
    const foes = [...s.ents.values()].filter((e) => !e.dead && e.owner !== this.me && e.kind === 'unit' && UNITS[e.def].cls !== 'vil');
    for (const f of foes) {
      let score = 0;
      for (const g of foes) if (Math.hypot(g.x - f.x, g.y - f.y) < pw.r * 0.8) score++;
      if (!best || score > best.score) best = { x: f.x, y: f.y, score };
    }
    switch (pw.kind) {
      case 'zone':
      case 'hammer':
        if (pw.id === 'quake') {
          const b = [...s.ents.values()].find((e) => e.kind === 'bld' && e.owner !== this.me && e.def === 'tc' && !e.dead && army.some((u) => Math.hypot(u.x - e.x, u.y - e.y) < 12));
          if (b) s.castPower(this.me, b.x, b.y);
        } else if (best && best.score >= 3) s.castPower(this.me, best.x, best.y);
        break;
      case 'convert':
        if (best && best.score >= 3) s.castPower(this.me, best.x, best.y);
        break;
      case 'summon': {
        const fighting = army.find((u) => u.order?.t === 'attack' || u.order?.t === 'amove');
        if (fighting && foes.some((f) => Math.hypot(f.x - fighting.x, f.y - fighting.y) < 12)) s.castPower(this.me, fighting.x, fighting.y);
        break;
      }
      case 'heal': {
        const hurt = army.filter((u) => u.hp < u.maxHp * 0.6);
        if (hurt.length >= 4) s.castPower(this.me, hurt[0].x, hurt[0].y);
        break;
      }
    }
  }
}
