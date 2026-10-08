// Game controller: main loop, input, selection, command modes.

import { AI, Difficulty } from './ai';
import { BUILDINGS, BId, CIVS, CivId, MAJORS, MAP_N, UNITS } from './data';
import { sfx, toggleMute, muted } from './sfx';
import { Ent, Sim } from './sim';
import { $, UI } from './ui';
import { View } from './view';

export type Mode = 'normal' | 'place' | 'amove' | 'power';
export interface StartCfg { civ: CivId; god: string; enemy: CivId | 'random'; diff: Difficulty }

export class Game {
  sim!: Sim;
  view: View;
  ui: UI;
  ais: AI[] = [];
  selection: number[] = [];
  groups: number[][] = Array.from({ length: 10 }, () => []);
  mode: Mode = 'normal';
  placeDef: BId | null = null;
  speed = 1;
  paused = true;
  running = false;
  hovered = -1;
  private acc = 0;
  private last = performance.now();
  private keys = new Set<string>();
  private mouse = { x: 0, y: 0, inside: false };
  private drag: { x: number; y: number; on: boolean } | null = null;
  private lastClick = { t: 0, id: -1 };
  private lastGroupKey = { k: '', t: 0 };
  private idleIdx = 0;
  private ended = false;
  private pickT = 0;
  autoPlayer = false;

  constructor(private canvas: HTMLCanvasElement) {
    this.view = new View(canvas);
    this.ui = new UI(this);
    this.bindInput();
    requestAnimationFrame(() => this.frame());
  }

  hasTemple() { return this.sim ? this.sim.hasBuilding(0, 'temple') : false; }

  // ------------------------------------------------------------------ lifecycle
  start(cfg: StartCfg, seed = Math.floor(Math.random() * 1e6), auto = false) {
    const civs = Object.keys(CIVS) as CivId[];
    const enemy: CivId = cfg.enemy === 'random' ? civs[Math.floor(Math.random() * 3)] : cfg.enemy;
    const eg = CIVS[enemy].majors[Math.floor(Math.random() * 3)];
    this.autoPlayer = auto;
    this.sim = new Sim({ civs: [cfg.civ, enemy], gods: [cfg.god, eg], seed, ai: [auto, true] });
    this.ais = [new AI(this.sim, 1, cfg.diff)];
    if (auto) this.ais.push(new AI(this.sim, 0, 'normal'));
    this.view.init(this.sim);
    this.selection = []; this.mode = 'normal'; this.placeDef = null; this.ended = false;
    const tc = [...this.sim.ents.values()].find((e) => e.owner === 0 && e.def === 'tc')!;
    this.view.camDist = 30;
    this.view.setCamera(tc.x + 3, tc.y + 1);
    $('menu').classList.add('hidden'); $('end').classList.add('hidden'); $('hud').classList.remove('hidden');
    $('choice').classList.add('hidden'); $('log').innerHTML = '';
    this.paused = false; this.running = true; this.speed = 1; $('b-speed').textContent = '1×';
    this.ui.log(`${MAJORS[cfg.god].name} watches over you. Destroy the enemy ${CIVS[enemy].names.tc}!`);
    this.ui.log(`Your enemy: ${CIVS[enemy].name} under ${MAJORS[eg].name}.`, true);
    this.ui.showHelp(false);
    (window as unknown as Record<string, unknown>).__game = this;
  }

  toMenu() {
    this.running = false; this.paused = true;
    $('hud').classList.add('hidden');
    this.ui.showMenu((cfg) => this.start({ ...cfg, diff: cfg.diff as Difficulty }));
  }

  // ------------------------------------------------------------------ main loop
  private frame() {
    const now = performance.now();
    const dt = Math.min(0.1, (now - this.last) / 1000);
    this.last = now;
    if (this.running) {
      if (!this.paused) {
        this.acc += dt * this.speed;
        let n = 0;
        while (this.acc >= 0.05 && n++ < 8) {
          this.sim.tick(0.05);
          for (const a of this.ais) a.update(0.05);
          this.acc -= 0.05;
        }
        if (this.acc > 0.5) this.acc = 0;
      }
      this.scrollCamera(dt);
      this.handleEvents();
      this.selection = this.selection.filter((id) => this.sim.get(id));
      this.pickT -= dt;
      this.view.sync(this.paused ? 0 : dt, new Set(this.selection), this.hovered);
      this.updateGhost();
      this.ui.update(dt);
      if (this.sim.winner >= 0 && !this.ended) {
        this.ended = true;
        const win = this.sim.winner === 0;
        win ? sfx.win() : sfx.lose();
        setTimeout(() => this.ui.showEnd(win), 1800);
      }
    }
    this.view.render();
    requestAnimationFrame(() => this.frame());
  }

  private handleEvents() {
    for (const ev of this.sim.events) {
      switch (ev.t) {
        case 'msg': if (ev.owner < 0 || ev.owner === 0) this.ui.log(ev.text, ev.owner === 1); break;
        case 'age':
          if (ev.owner === 0) { sfx.age(); this.ui.log(`You have advanced to the ${['', 'Archaic', 'Classical', 'Heroic', 'Mythic'][ev.age]} Age!`); } else this.ui.log(`The enemy has reached the ${['', 'Archaic', 'Classical', 'Heroic', 'Mythic'][ev.age]} Age.`, true);
          break;
        case 'built': { const e = this.sim.get(ev.id); if (e && e.owner === 0) { sfx.built(); } break; }
        case 'strike': sfx.bolt(); break;
        case 'power': sfx.power(); break;
        case 'hit': sfx.hit(); break;
        case 'die': if (ev.kind === 'bld') sfx.collapse(); break;
        default: break;
      }
    }
  }

  // ------------------------------------------------------------------ camera
  private scrollCamera(dt: number) {
    const v = this.view, sp = (v.camDist * 0.9 + 6) * dt;
    let dx = 0, dy = 0;
    const k = this.keys;
    if (k.has('arrowleft')) dx -= 1;
    if (k.has('arrowright')) dx += 1;
    if (k.has('arrowup')) dy -= 1;
    if (k.has('arrowdown')) dy += 1;
    const m = this.mouse, W = window.innerWidth, H = window.innerHeight, e = 5;
    if (m.inside && !this.drag?.on) {
      if (m.x <= e) dx -= 1; if (m.x >= W - e) dx += 1;
      if (m.y <= e) dy -= 1; if (m.y >= H - e && m.y < H) dy += 1;
    }
    if (dx || dy) v.setCamera(v.camX + dx * sp, v.camZ + dy * sp);
  }

  // ------------------------------------------------------------------ selection helpers
  private ndc(cx: number, cy: number): [number, number] { return [(cx / window.innerWidth) * 2 - 1, -(cy / window.innerHeight) * 2 + 1]; }
  private select(ids: number[]) { this.selection = ids; if (this.mode === 'place') this.cancelMode(); }
  private myUnits(ids: number[] = this.selection): Ent[] { return ids.map((i) => this.sim.get(i)).filter((e): e is Ent => !!e && e.kind === 'unit' && e.owner === 0); }
  private selectedVillagers() { return this.myUnits().filter((u) => UNITS[u.def].cls === 'vil'); }

  private boxSelect(x0: number, y0: number, x1: number, y1: number, add: boolean) {
    const lx = Math.min(x0, x1), hx = Math.max(x0, x1), ly = Math.min(y0, y1), hy = Math.max(y0, y1);
    const found: Ent[] = [];
    for (const e of this.sim.ents.values()) {
      if (e.dead || e.kind !== 'unit' || e.owner !== 0) continue;
      const s = this.view.worldToScreen(e.x, e.y, 0.6);
      if (!s.behind && s.x >= lx && s.x <= hx && s.y >= ly && s.y <= hy) found.push(e);
    }
    // prefer military over villagers in a mixed box
    const mil = found.filter((e) => UNITS[e.def].cls !== 'vil');
    const ids = (mil.length ? mil : found).map((e) => e.id);
    this.select(add ? [...new Set([...this.selection, ...ids])] : ids);
    if (ids.length) sfx.click();
  }

  private clickSelect(cx: number, cy: number, shift: boolean) {
    const [nx, ny] = this.ndc(cx, cy);
    const e = this.view.pick(nx, ny);
    if (!e) { if (!shift) this.select([]); return; }
    const now = performance.now();
    if (e.kind === 'unit' && e.owner === 0 && this.lastClick.id === e.id && now - this.lastClick.t < 350) {
      // double click: all same type on screen
      const ids: number[] = [];
      for (const u of this.sim.ents.values()) {
        if (u.dead || u.kind !== 'unit' || u.owner !== 0 || u.def !== e.def) continue;
        const s = this.view.worldToScreen(u.x, u.y);
        if (!s.behind && s.x >= 0 && s.x <= window.innerWidth && s.y >= 0 && s.y <= window.innerHeight) ids.push(u.id);
      }
      this.select(ids);
      return;
    }
    this.lastClick = { t: now, id: e.id };
    if (shift && e.kind === 'unit' && e.owner === 0 && this.myUnits().length === this.selection.length) {
      this.select(this.selection.includes(e.id) ? this.selection.filter((i) => i !== e.id) : [...this.selection, e.id]);
    } else this.select([e.id]);
    sfx.click();
  }

  // ------------------------------------------------------------------ commands
  private rightClick(cx: number, cy: number, mini?: { x: number; y: number }) {
    const sim = this.sim;
    if (this.mode !== 'normal') { this.cancelMode(); return; }
    const sel = this.selection.map((i) => sim.get(i)).filter((e): e is Ent => !!e);
    if (!sel.length) return;
    const units = this.myUnits();
    const [nx, ny] = this.ndc(cx, cy);
    const g = mini ?? this.view.groundAt(nx, ny);
    const target = mini ? null : this.view.pick(nx, ny);
    if (units.length) {
      if (target) {
        const hostile = target.owner >= 0 && target.owner !== 0;
        sim.cmdTarget(0, units.map((u) => u.id), target.id);
        this.view.commandMarker(target.x, target.y, hostile ? 0xff5a4a : 0x66ff88);
      } else if (g) {
        sim.cmdMove(0, units.map((u) => u.id), g.x, g.y);
        this.view.commandMarker(g.x, g.y);
      }
      sfx.order();
    } else if (sel.length === 1 && sel[0].kind === 'bld' && sel[0].owner === 0 && g) {
      sim.setRally(0, sel[0].id, g.x, g.y, target && (target.kind === 'res' || target.owner === 0) ? target.id : -1);
      this.view.commandMarker(g.x, g.y, 0xffd24a);
      this.ui.log('Rally point set.');
      sfx.order();
    }
  }

  private leftClickMode(cx: number, cy: number, shift: boolean): boolean {
    if (this.mode === 'normal') return false;
    const [nx, ny] = this.ndc(cx, cy);
    const g = this.view.groundAt(nx, ny);
    if (!g) return true;
    if (this.mode === 'place' && this.placeDef) {
      const s = BUILDINGS[this.placeDef].size;
      const tx = Math.round(g.x - s / 2), ty = Math.round(g.y - s / 2);
      const vs = this.selectedVillagers();
      const b = this.sim.cmdBuild(0, vs.map((v) => v.id), this.placeDef, tx, ty);
      if (!b) { sfx.error(); this.ui.log(this.sim.canPlace(this.placeDef, tx, ty) ? 'Not enough resources.' : 'Cannot build there.', true); return true; }
      sfx.order();
      if (!shift || !this.sim.canAfford(this.sim.players[0], BUILDINGS[this.placeDef].cost)) this.cancelMode();
    } else if (this.mode === 'amove') {
      this.sim.cmdMove(0, this.myUnits().map((u) => u.id), g.x, g.y, true);
      this.view.commandMarker(g.x, g.y, 0xff5a4a);
      sfx.order();
      this.cancelMode();
    } else if (this.mode === 'power') {
      if (this.sim.castPower(0, g.x, g.y)) this.cancelMode(); else { sfx.error(); this.ui.log('The god power is not ready.', true); this.cancelMode(); }
    }
    return true;
  }

  setMode(m: Mode, def: BId | null = null) {
    this.mode = m; this.placeDef = def;
    this.canvas.style.cursor = m === 'normal' ? 'default' : 'crosshair';
    this.ui.hint(m === 'place' ? `Placing ${CIVS[this.sim.players[0].civ].names[def!]} — click to build (Shift: keep placing), right-click / Esc to cancel`
      : m === 'amove' ? 'Attack-move: click a destination, Esc to cancel'
      : m === 'power' ? `${MAJORS[this.sim.players[0].god].power.name}: click a target location, Esc to cancel` : '');
    if (m !== 'place') this.view.setGhost(null, 0, 0, true);
  }
  cancelMode() { this.setMode('normal'); }

  private startPower() {
    const p = this.sim.players[0], pw = MAJORS[p.god].power;
    if (p.age < 2) { this.ui.log('God powers unlock in the Classical Age.', true); sfx.error(); return; }
    if (p.powerCd > 0) { this.ui.log(`${pw.name} is recharging.`, true); sfx.error(); return; }
    if (p.favor < pw.cost) { this.ui.log(`Not enough favor (${pw.cost} needed).`, true); sfx.error(); return; }
    this.setMode('power');
  }

  private updateGhost() {
    if (this.mode !== 'place' || !this.placeDef) return;
    const [nx, ny] = this.ndc(this.mouse.x, this.mouse.y);
    const g = this.view.groundAt(nx, ny);
    if (!g) return;
    const s = BUILDINGS[this.placeDef].size;
    const tx = Math.round(g.x - s / 2), ty = Math.round(g.y - s / 2);
    this.view.setGhost(this.placeDef, tx, ty, this.sim.canPlace(this.placeDef, tx, ty));
  }

  // ------------------------------------------------------------------ UI actions (command card clicks)
  private onCommand(act: string) {
    const sim = this.sim, p = sim.players[0];
    const [kind, arg] = act.split(':');
    const b = this.selection.length === 1 ? sim.get(this.selection[0]) : undefined;
    switch (kind) {
      case 'build': {
        const d = BUILDINGS[arg as BId];
        if (p.age < d.age) { this.ui.log(`Requires the ${['', 'Archaic', 'Classical', 'Heroic', 'Mythic'][d.age]} Age.`, true); sfx.error(); return; }
        if (!sim.canAfford(p, d.cost)) { this.ui.log('Not enough resources.', true); sfx.error(); return; }
        this.setMode('place', arg as BId); sfx.click();
        break;
      }
      case 'train':
        if (b && sim.train(0, b.id, arg)) sfx.click(); else { sfx.error(); this.ui.log(p.pop + p.queuedPop >= p.popCap ? 'Build more houses!' : 'Cannot train that now.', true); }
        break;
      case 'age':
        if (sim.ageUp(0)) { sfx.click(); this.ui.log('Your people begin the journey to a new Age…'); } else { sfx.error(); this.ui.log(this.hasTemple() || p.age < 2 ? 'Cannot advance now.' : 'Heroic and Mythic Ages require a Temple.', true); }
        break;
      case 'stop': for (const u of this.myUnits()) sim.cmdMove(0, [u.id], u.x, u.y); sfx.click(); break;
      case 'amove': this.setMode('amove'); sfx.click(); break;
      default: break;
    }
  }

  // ------------------------------------------------------------------ input binding
  private bindInput() {
    const cv = this.canvas;
    cv.addEventListener('contextmenu', (e) => e.preventDefault());
    window.addEventListener('contextmenu', (e) => e.preventDefault());
    cv.addEventListener('mousedown', (e) => {
      if (!this.running) return;
      this.mouse.x = e.clientX; this.mouse.y = e.clientY;
      if (e.button === 0) {
        if (this.leftClickMode(e.clientX, e.clientY, e.shiftKey)) return;
        this.drag = { x: e.clientX, y: e.clientY, on: false };
      } else if (e.button === 2) this.rightClick(e.clientX, e.clientY);
    });
    window.addEventListener('mousemove', (e) => {
      this.mouse.x = e.clientX; this.mouse.y = e.clientY; this.mouse.inside = true;
      if (!this.running) return;
      if (this.drag) {
        if (!this.drag.on && Math.hypot(e.clientX - this.drag.x, e.clientY - this.drag.y) > 6) this.drag.on = true;
        const box = $('selbox');
        if (this.drag.on) {
          box.style.display = 'block';
          box.style.left = Math.min(this.drag.x, e.clientX) + 'px'; box.style.top = Math.min(this.drag.y, e.clientY) + 'px';
          box.style.width = Math.abs(e.clientX - this.drag.x) + 'px'; box.style.height = Math.abs(e.clientY - this.drag.y) + 'px';
        }
      }
      if (this.pickT <= 0 && (e.target as HTMLElement) === cv) {
        this.pickT = 0.06;
        const [nx, ny] = this.ndc(e.clientX, e.clientY);
        const h = this.view.pick(nx, ny);
        this.hovered = h ? h.id : -1;
      }
    });
    document.addEventListener('mouseleave', () => (this.mouse.inside = false));
    window.addEventListener('mouseup', (e) => {
      if (e.button !== 0 || !this.drag) return;
      const d = this.drag; this.drag = null;
      $('selbox').style.display = 'none';
      if (d.on) this.boxSelect(d.x, d.y, e.clientX, e.clientY, e.shiftKey);
      else this.clickSelect(e.clientX, e.clientY, e.shiftKey);
    });
    cv.addEventListener('wheel', (e) => {
      e.preventDefault();
      this.view.camDist = Math.max(11, Math.min(42, this.view.camDist * (1 + Math.sign(e.deltaY) * 0.1)));
    }, { passive: false });

    // minimap
    const mini = $<HTMLCanvasElement>('mini');
    const miniPos = (e: MouseEvent) => {
      const r = mini.getBoundingClientRect();
      return { x: ((e.clientX - r.left) / r.width) * MAP_N, y: ((e.clientY - r.top) / r.height) * MAP_N };
    };
    let miniDown = false;
    mini.addEventListener('mousedown', (e) => {
      e.preventDefault();
      const p = miniPos(e);
      if (e.button === 0) {
        if (this.mode === 'power' || this.mode === 'amove') {
          if (this.mode === 'power') { this.sim.castPower(0, p.x, p.y); } else { this.sim.cmdMove(0, this.myUnits().map((u) => u.id), p.x, p.y, true); }
          this.cancelMode();
        } else { miniDown = true; this.view.setCamera(p.x, p.y + this.view.camDist * 0.15); }
      } else if (e.button === 2) this.rightClick(0, 0, p);
    });
    mini.addEventListener('mousemove', (e) => { if (miniDown) { const p = miniPos(e); this.view.setCamera(p.x, p.y + this.view.camDist * 0.15); } });
    window.addEventListener('mouseup', () => (miniDown = false));

    // delegated clicks for panels
    document.addEventListener('click', (e) => {
      const t = e.target as HTMLElement;
      const btn = t.closest('[data-act]') as HTMLElement | null;
      if (btn && this.running) { this.onCommand(btn.dataset.act!); return; }
      const pick = t.closest('[data-pick]') as HTMLElement | null;
      if (pick) { this.select([Number(pick.dataset.pick)]); return; }
      const cancel = t.closest('[data-cancel]') as HTMLElement | null;
      if (cancel && this.selection.length === 1) { this.sim.cancelQueue(0, this.selection[0], Number(cancel.dataset.cancel)); this.ui.invalidate(); return; }
      const minor = t.closest('[data-minor]') as HTMLElement | null;
      if (minor) { if (this.sim.chooseMinor(0, minor.dataset.minor!)) { sfx.click(); } return; }
      if (t.closest('#power')) { this.startPower(); return; }
      if (t.id === 'b-speed') this.setSpeed(this.speed >= 3 ? 1 : this.speed + 1);
      if (t.id === 'b-pause') this.togglePause();
      if (t.id === 'b-mute') { toggleMute(); t.textContent = muted ? '🔇' : '🔊'; }
      if (t.id === 'b-help') { this.ui.showHelp(true); this.paused = true; }
    });

    window.addEventListener('keydown', (e) => {
      if (!this.running) return;
      const k = e.key.toLowerCase();
      this.keys.add(k);
      if (k === 'escape') { this.cancelMode(); if (!this.mode || this.mode === 'normal') this.select([]); $('help').classList.add('hidden'); this.paused = false; }
      else if (k === 'q') this.startPower();
      else if (k === 'a' && !e.ctrlKey && this.myUnits().some((u) => UNITS[u.def].cls !== 'vil')) this.setMode('amove');
      else if (k === 's' && !e.ctrlKey && this.myUnits().length) { this.onCommand('stop'); }
      else if (k === 'h') { const tc = [...this.sim.ents.values()].find((x) => x.owner === 0 && x.def === 'tc'); if (tc) { this.select([tc.id]); this.view.setCamera(tc.x, tc.y + 1); } }
      else if (k === ' ') { e.preventDefault(); const s = this.selection.map((i) => this.sim.get(i)).filter((x): x is Ent => !!x); if (s.length) this.view.setCamera(s[0].x, s[0].y + this.view.camDist * 0.1); }
      else if (k === 'p') this.togglePause();
      else if (k === '+' || k === '=') this.setSpeed(Math.min(3, this.speed + 1));
      else if (k === '-') this.setSpeed(Math.max(1, this.speed - 1));
      else if (k === 'm') { toggleMute(); $('b-mute').textContent = muted ? '🔇' : '🔊'; }
      else if (k === 'f1') { e.preventDefault(); this.ui.showHelp(true); this.paused = true; }
      else if (k === '.') this.nextIdleVillager();
      else if (/^[0-9]$/.test(k)) this.controlGroup(Number(k), e.ctrlKey || e.metaKey, e);
    });
    window.addEventListener('keyup', (e) => this.keys.delete(e.key.toLowerCase()));
    window.addEventListener('blur', () => this.keys.clear());
  }

  private controlGroup(n: number, set: boolean, e: KeyboardEvent) {
    if (set) { e.preventDefault(); this.groups[n] = this.selection.slice(); this.ui.log(`Group ${n} set.`); return; }
    const ids = this.groups[n].filter((i) => this.sim.get(i));
    if (!ids.length) return;
    this.select(ids);
    const now = performance.now();
    if (this.lastGroupKey.k === String(n) && now - this.lastGroupKey.t < 400) { const u = this.sim.get(ids[0])!; this.view.setCamera(u.x, u.y + this.view.camDist * 0.1); }
    this.lastGroupKey = { k: String(n), t: now };
  }

  private nextIdleVillager() {
    const idle = [...this.sim.ents.values()].filter((e) => !e.dead && e.kind === 'unit' && e.owner === 0 && UNITS[e.def].cls === 'vil' && !e.order);
    if (!idle.length) { this.ui.log('No idle villagers.'); return; }
    const v = idle[this.idleIdx++ % idle.length];
    this.select([v.id]);
    this.view.setCamera(v.x, v.y + this.view.camDist * 0.1);
  }

  setSpeed(s: number) { this.speed = s; $('b-speed').textContent = `${s}×`; }
  togglePause() { this.paused = !this.paused; $('b-pause').textContent = this.paused ? '▶' : '⏸'; this.ui.hint(this.paused ? 'Paused — press P to resume' : ''); }
}
