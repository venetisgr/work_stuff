// DOM HUD: top bar, selection panel, command card, minimap, menus.

import {
  AGE_NAMES, AGE_UP, BUILDINGS, BUILD_ORDER, BId, CIVS, CivId, Cost, MAJORS, MAP_N, UNITS, UnitDef, civUnits,
} from './data';
import type { Game } from './game';
import { Ent } from './sim';

export const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;

const UNIT_ICON: Record<string, string> = {
  vil: '🧑‍🌾', inf: '🛡️', rng: '🏹', cav: '🐎', minotaur: '🐂', centaur: '🏹', cyclops: '👁️', medusa: '🐍', anubite: '🐺', sphinx: '🦁',
  phoenix: '🔥', croc: '🐊', troll: '👹', valkyrie: '🗡️', fenrir: '🐺', frost_giant: '🧊', skeleton: '💀', einherjar: '⚔️',
};
const BLD_ICON: Record<string, string> = { tc: '🏛️', house: '🏠', storehouse: '📦', farm: '🌾', barracks: '⚔️', temple: '🏺', tower: '🗼' };
export const unitIcon = (d: UnitDef) => UNIT_ICON[d.slot ?? d.id] ?? '❔';
const RES_ICON: Record<string, string> = { food: '🍖', wood: '🪵', gold: '🪙', favor: '✨' };
export const costStr = (c: Cost) => (['food', 'wood', 'gold', 'favor'] as const).filter((k) => c[k]).map((k) => `${RES_ICON[k]}${c[k]}`).join(' ');

export class UI {
  private sig = '';
  private miniCtx = $<HTMLCanvasElement>('mini').getContext('2d')!;
  private miniT = 0;
  private logCount = 0;

  constructor(private game: Game) {}

  invalidate() { this.sig = ''; this.lastSel = ''; this.lastCmd = ''; }

  // ------------------------------------------------------------------ messages
  log(text: string, bad = false) {
    const d = document.createElement('div');
    d.textContent = text;
    if (bad) d.className = 'bad';
    const log = $('log');
    log.appendChild(d);
    this.logCount++;
    while (log.children.length > 6) log.firstChild?.remove();
    setTimeout(() => d.remove(), 9000);
  }
  hint(text: string) {
    const h = $('hint');
    h.style.display = text ? 'block' : 'none';
    h.textContent = text;
  }

  // ------------------------------------------------------------------ top bar
  update(dt: number) {
    const g = this.game, sim = g.sim, p = sim.players[0];
    const set = (id: string, v: string) => { const b = $(id).querySelector('b'); if (b && b.textContent !== v) b.textContent = v; };
    set('r-food', String(Math.floor(p.food)));
    set('r-wood', String(Math.floor(p.wood)));
    set('r-gold', String(Math.floor(p.gold)));
    set('r-favor', String(Math.floor(p.favor)));
    set('r-pop', `${p.pop}/${p.popCap}`);
    $('r-pop').classList.toggle('warn', p.pop + p.queuedPop >= p.popCap);
    $('r-age').textContent = AGE_NAMES[p.age] + (p.ageing ? ' → …' : '');
    // god power
    const pw = MAJORS[p.god].power, btn = $('power');
    btn.querySelector('.pname')!.textContent = `⚡ ${pw.name} (Q)`;
    (btn.querySelector('.pcost') as HTMLElement).textContent = p.age < 2 ? 'Unlocks in the Classical Age' : p.powerCd > 0 ? `Recharging ${Math.ceil(p.powerCd)}s` : `${pw.cost} favor`;
    (btn.querySelector('.pcd') as HTMLElement).style.width = `${(p.powerCd / pw.cd) * 100}%`;
    btn.classList.toggle('ready', p.age >= 2 && p.powerCd <= 0 && p.favor >= pw.cost);
    btn.title = pw.desc;

    this.renderPanels(dt);
    this.updateChoice();
    this.miniT -= dt;
    if (this.miniT <= 0) { this.miniT = 0.2; this.drawMinimap(); }
  }

  // ------------------------------------------------------------------ selection + command card
  private lastSel = '';
  private lastCmd = '';
  private panelT = 0;
  private renderPanels(dt: number) {
    const g = this.game, sim = g.sim;
    const ents = g.selection.map((id) => sim.get(id)).filter((e): e is Ent => !!e);
    const bld = ents.length === 1 && ents[0].kind === 'bld' && ents[0].owner === 0 ? ents[0] : null;
    this.panelT -= dt;
    if (this.panelT <= 0 || !this.sig) {
      this.panelT = 0.1;
      // Only touch the DOM when the generated markup changed, so in-flight clicks are never lost.
      const { sel, cmd } = this.buildPanels(ents, bld);
      if (sel !== this.lastSel) { $('sel').innerHTML = sel; this.lastSel = sel; }
      if (cmd !== this.lastCmd) { $('cmd').innerHTML = cmd; this.lastCmd = cmd; }
      this.sig = 'x';
    }
    this.updateDynamic(ents);
  }

  private btn(act: string, icon: string, label: string, cost: string, enabled: boolean, title = '', cls = '') {
    return `<button class="cbtn ${enabled ? '' : 'no'} ${cls}" data-act="${act}" title="${title.replace(/"/g, '&quot;')}" ${enabled ? '' : 'data-off="1"'}><span class="ic">${icon}</span><span>${label}</span><span class="cs">${cost}</span></button>`;
  }

  private buildPanels(ents: Ent[], bld: Ent | null): { sel: string; cmd: string } {
    const g = this.game, sim = g.sim, p = sim.players[0], civ = CIVS[p.civ];
    let html = '', cards = '';
    if (!ents.length) {
      html = `<div class="sel-title">${civ.name} — ${MAJORS[p.god].name}</div><div class="sel-sub">${MAJORS[p.god].title}</div>
        <div class="sel-sub">${MAJORS[p.god].blurb}</div><div class="sel-sub">${civ.favor}</div>
        <div class="sel-sub" style="margin-top:8px;opacity:.65">Destroy the enemy ${CIVS[sim.players[1].civ].names.tc} to win. Left-drag to select, right-click to command. F1 for help.</div>`;
    } else if (ents.length === 1) {
      const e = ents[0];
      const own = e.owner === 0;
      if (e.kind === 'unit') {
        const d = UNITS[e.def];
        html = `<div class="sel-title">${unitIcon(d)} ${d.name}</div><div class="sel-sub">${own ? '' : 'Enemy · '}${d.civ ? CIVS[d.civ].name : ''} ${d.cls === 'myth' ? '· Mythic unit' : ''}</div>
          <div class="bar"><i id="hpbar"></i></div><div class="sel-sub" id="hptxt"></div>
          <div class="stats"><span>⚔️ ${(d.atk * p.mods.atk).toFixed(0)}</span><span>🎯 ${d.range > 2 ? d.range.toFixed(0) : 'melee'}</span><span>👟 ${d.speed.toFixed(1)}</span><span>⏱ ${d.cd}s</span></div>
          <div class="sel-sub" style="margin-top:4px">${d.desc}</div><div class="sel-sub" id="carry"></div>`;
      } else if (e.kind === 'bld') {
        const d = BUILDINGS[e.def as BId];
        const name = CIVS[sim.players[e.owner].civ].names[e.def as BId];
        html = `<div class="sel-title">${BLD_ICON[e.def]} ${name}</div><div class="sel-sub">${own ? '' : 'Enemy · '}${d.blurb}</div>
          <div class="bar"><i id="hpbar"></i></div><div class="sel-sub" id="hptxt"></div>
          ${e.done ? '' : `<div class="sel-sub">Under construction — right-click it with villagers</div><div class="bar prog"><i id="progbar"></i></div>`}
          ${e.rtype === 'farm' ? `<div class="sel-sub" id="farmtxt"></div>` : ''}
          ${own && e.done && e.def === 'temple' ? `<div class="sel-sub">${civ.favor}</div>` : ''}
          <div class="queue" id="queue"></div>`;
      } else {
        html = `<div class="sel-title">${e.def === 'tree' ? '🌲 Forest' : e.def === 'gold' ? '🪙 Gold Mine' : '🫐 Berry Bush'}</div>
          <div class="sel-sub" id="restxt"></div>`;
      }
    } else {
      const by = new Map<string, number>();
      ents.forEach((e) => by.set(e.def, (by.get(e.def) ?? 0) + 1));
      html = `<div class="sel-title">${ents.length} units selected</div><div class="sel-sub">${[...by].map(([k, n]) => `${n}× ${UNITS[k]?.name ?? k}`).join(', ')}</div><div class="groupicons">` +
        ents.slice(0, 60).map((e) => `<div class="gi" data-pick="${e.id}" title="${UNITS[e.def].name}">${unitIcon(UNITS[e.def])}<i data-hp="${e.id}"></i></div>`).join('') + '</div>';
    }
    // command card
    const units = ents.filter((e) => e.kind === 'unit' && e.owner === 0);
    if (units.length && units.some((u) => UNITS[u.def].cls === 'vil')) {
      const place = g.mode === 'place';
      for (const id of BUILD_ORDER) {
        const d = BUILDINGS[id];
        const ok = p.age >= d.age && sim.canAfford(p, d.cost);
        const reason = p.age < d.age ? ` (needs ${AGE_NAMES[d.age]})` : '';
        cards += this.btn('build:' + id, BLD_ICON[id], civ.names[id], costStr(d.cost), ok, d.blurb + reason, place && g.placeDef === id ? 'wide' : '');
      }
    }
    if (units.some((u) => UNITS[u.def].cls !== 'vil')) {
      cards += this.btn('stop', '✋', 'Stop (S)', '', true, 'Halt and hold position');
      cards += this.btn('amove', '⚔️', 'Attack-move (A)', '', true, 'Move, attacking anything on the way');
    }
    if (bld && bld.done) {
      if (bld.def === 'tc') {
        const d = UNITS[`${p.civ}_vil`];
        cards += this.btn('train:' + d.id, unitIcon(d), d.name, costStr(d.cost), sim.canAfford(p, d.cost) && !!(p.pop + p.queuedPop < p.popCap), `Train a villager (${d.time}s)`);
        const next = p.age + 1;
        if (next <= 4) {
          const c = AGE_UP[next];
          const cost = { food: c.food, gold: c.gold, favor: c.favor };
          const needTemple = next >= 3 && !g.hasTemple();
          const busy = p.ageing || !!p.pendingChoice;
          cards += this.btn('age', '🌅', AGE_NAMES[next], costStr(cost), sim.canAfford(p, cost) && !needTemple && !busy, needTemple ? 'Requires a completed Temple' : `Advance to the ${AGE_NAMES[next]} and choose a minor god (${c.time}s)`, 'wide');
        }
      } else if (bld.def === 'barracks') {
        for (const d of civUnits(p.civ, 'barracks')) {
          const ok = p.age >= d.age && sim.canAfford(p, d.cost) && p.pop + p.queuedPop + d.pop <= p.popCap;
          cards += this.btn('train:' + d.id, unitIcon(d), d.name, costStr(d.cost), ok, `${d.desc} HP ${d.hp}, ATK ${d.atk}${p.age < d.age ? ` — needs ${AGE_NAMES[d.age]}` : ''}`);
        }
      } else if (bld.def === 'temple') {
        for (const id of p.unlocked) {
          const d = UNITS[id];
          const ok = sim.canAfford(p, d.cost) && p.pop + p.queuedPop + d.pop <= p.popCap;
          cards += this.btn('train:' + d.id, unitIcon(d), d.name, costStr(d.cost), ok, `${d.desc} HP ${d.hp}, ATK ${d.atk}`);
        }
        if (!p.unlocked.length) cards += `<div class="sel-sub" style="grid-column: span 4">Reach the Heroic Age and choose a minor god to unlock mythic units.</div>`;
      }
    }
    return { sel: html, cmd: cards };
  }

  private updateDynamic(ents: Ent[]) {
    const g = this.game, sim = g.sim;
    if (ents.length === 1) {
      const e = ents[0];
      const hp = document.getElementById('hpbar'), tx = document.getElementById('hptxt');
      if (hp) hp.style.width = `${Math.max(0, (e.hp / e.maxHp) * 100)}%`;
      if (tx) tx.textContent = `${Math.ceil(e.hp)} / ${Math.ceil(e.maxHp)} HP`;
      const pb = document.getElementById('progbar');
      if (pb) pb.style.width = `${e.progress * 100}%`;
      const ft = document.getElementById('farmtxt');
      if (ft) ft.textContent = `Food remaining: ${Math.ceil(e.amount)}`;
      const rt = document.getElementById('restxt');
      if (rt) rt.textContent = `${Math.ceil(e.amount)} ${e.rtype === 'wood' ? 'wood' : e.rtype === 'gold' ? 'gold' : 'food'} remaining`;
      const cr = document.getElementById('carry');
      if (cr) cr.textContent = e.carryAmt > 0 ? `Carrying ${e.carryAmt} ${e.carryType}` : (e.order ? `Order: ${e.order.t}` : 'Idle');
      const q = document.getElementById('queue');
      if (q && e.owner === 0) {
        const html = e.queue.map((it, i) => {
          const d = it.kind === 'age' ? null : UNITS[it.unit!];
          return `<div class="qi" data-cancel="${i}" title="Click to cancel">${d ? unitIcon(d) : '🌅'}${i === 0 ? `<i style="width:${(1 - it.t / it.total) * 100}%"></i>` : ''}</div>`;
        }).join('');
        if (html !== q.innerHTML) q.innerHTML = html;
      }
    } else {
      document.querySelectorAll<HTMLElement>('[data-hp]').forEach((el) => {
        const e = sim.get(Number(el.dataset.hp));
        el.style.width = e ? `${(e.hp / e.maxHp) * 100}%` : '0';
      });
    }
  }

  // ------------------------------------------------------------------ minor god choice
  private choiceAge = 0;
  private updateChoice() {
    const p = this.game.sim.players[0], box = $('choice');
    if (!p.pendingChoice) { if (this.choiceAge) { box.classList.add('hidden'); this.choiceAge = 0; } return; }
    if (this.choiceAge === p.pendingChoice) return;
    this.choiceAge = p.pendingChoice;
    const opts = CIVS[p.civ].minors.filter((m) => m.age === p.pendingChoice);
    box.innerHTML = `<h2>You have entered the ${AGE_NAMES[p.age]}</h2><div>Choose a minor god to worship</div><div class="cards">` +
      opts.map((m) => {
        const u = m.unlock ? UNITS[m.unlock] : null;
        return `<div class="card" data-minor="${m.id}"><h3>${m.name}</h3><p>${m.blurb}</p>${u ? `<p class="tag">Mythic unit: ${u.name}</p><p>${u.desc}</p>` : ''}</div>`;
      }).join('') + '</div>';
    box.classList.remove('hidden');
  }

  // ------------------------------------------------------------------ minimap
  private drawMinimap() {
    const c = this.miniCtx, W = 190, k = W / MAP_N, sim = this.game.sim;
    c.fillStyle = '#35602f'; c.fillRect(0, 0, W, W);
    for (const e of sim.ents.values()) {
      if (e.kind !== 'res') continue;
      c.fillStyle = e.def === 'tree' ? '#1d4a22' : e.def === 'gold' ? '#f2c230' : '#c24a5a';
      c.fillRect(e.tx * k, e.ty * k, Math.max(1.5, e.size * k), Math.max(1.5, e.size * k));
    }
    for (const e of sim.ents.values()) {
      if (e.kind === 'bld') { c.fillStyle = e.owner === 0 ? '#4da0ff' : '#ff4a4a'; c.fillRect(e.tx * k, e.ty * k, e.size * k, e.size * k); }
    }
    for (const e of sim.ents.values()) {
      if (e.kind !== 'unit') continue;
      c.fillStyle = e.owner === 0 ? '#9fd0ff' : '#ff9a8a';
      c.fillRect(e.x * k - 1, e.y * k - 1, 2.5, 2.5);
    }
    const q = this.game.view.viewQuad();
    c.strokeStyle = '#fff'; c.lineWidth = 1; c.beginPath();
    q.forEach((pt, i) => { const x = Math.max(0, Math.min(W, pt.x * k)), y = Math.max(0, Math.min(W, pt.y * k)); i ? c.lineTo(x, y) : c.moveTo(x, y); });
    c.closePath(); c.stroke();
  }

  // ------------------------------------------------------------------ menu / end / help
  showHelp(on: boolean) {
    const h = $('help');
    if (!on) { h.classList.add('hidden'); return; }
    h.innerHTML = `<div class="box"><h2>How to play</h2><table>
      <tr><td>Goal</td><td>Destroy the enemy Town Center (Hill Fort) before they destroy yours.</td></tr>
      <tr><td>Economy</td><td>Villagers gather 🍖 food (berries, farms), 🪵 wood and 🪙 gold and return it to a Town Center or Storehouse. Build Houses for population.</td></tr>
      <tr><td>Favor ✨</td><td>Greeks: villagers pray at a Temple (right-click it). Egyptians: Temples generate Favor. Norse: every kill earns Favor. Spend it on Ages, mythic units and your god power.</td></tr>
      <tr><td>Ages</td><td>Advance at the Town Center (Heroic & Mythic need a Temple). Each age you choose a minor god; Heroic and Mythic ones unlock mythic units trained at the Temple.</td></tr>
      <tr><td>God power (Q)</td><td>From the Classical Age. Press Q, then click the battlefield.</td></tr>
      <tr><td>Combat</td><td>Infantry beats cavalry, cavalry beats archers, archers beat infantry. Mythic units and giants wreck buildings.</td></tr>
      <tr><td colspan="2">&nbsp;</td></tr>
      <tr><td>Left click / drag</td><td>Select · box-select · double-click selects all of a type on screen · Shift adds</td></tr>
      <tr><td>Right click</td><td>Move / gather / build / attack / set rally point (with a building selected)</td></tr>
      <tr><td>Arrows · screen edges</td><td>Scroll the camera · mouse wheel zooms · Space centers on selection</td></tr>
      <tr><td>A · S · Q</td><td>Attack-move · Stop · God power</td></tr>
      <tr><td>Ctrl+1…9 / 1…9</td><td>Set / recall control groups</td></tr>
      <tr><td>. (period)</td><td>Select next idle villager</td></tr>
      <tr><td>H</td><td>Jump to your Town Center</td></tr>
      <tr><td>P · + / − · M · Esc</td><td>Pause · game speed · mute · cancel</td></tr></table>
      <div class="row"><button id="help-close" class="big" style="margin-top:6px">Got it</button></div></div>`;
    h.classList.remove('hidden');
    $('help-close').onclick = () => { h.classList.add('hidden'); this.game.paused = false; };
  }

  showEnd(win: boolean) {
    const sim = this.game.sim, a = sim.players[0], b = sim.players[1];
    const e = $('end');
    e.className = win ? 'win' : 'lose';
    const mm = Math.floor(sim.time / 60), ss = String(Math.floor(sim.time % 60)).padStart(2, '0');
    e.innerHTML = `<h1>${win ? 'VICTORY' : 'DEFEAT'}</h1>
      <div class="sub">${win ? `${MAJORS[a.god].name} is pleased. The enemy ${CIVS[b.civ].names.tc} lies in ruins.` : `${MAJORS[b.god].name}'s forces have crushed your ${CIVS[a.civ].names.tc}.`}</div>
      <table><tr><th></th><th>You</th><th>Enemy</th></tr>
      <tr><td>Time</td><td colspan="2">${mm}:${ss}</td></tr>
      <tr><td>Age reached</td><td>${AGE_NAMES[a.age]}</td><td>${AGE_NAMES[b.age]}</td></tr>
      <tr><td>Kills</td><td>${a.kills}</td><td>${b.kills}</td></tr>
      <tr><td>Resources gathered</td><td>${Math.round(a.gathered)}</td><td>${Math.round(b.gathered)}</td></tr></table>
      <div class="row"><button id="end-menu" class="big">New game</button><button id="end-watch" class="big" style="background:linear-gradient(#3a2e1c,#241c10)">Keep watching</button></div>`;
    $('end-menu').onclick = () => { e.classList.add('hidden'); this.game.toMenu(); };
    $('end-watch').onclick = () => e.classList.add('hidden');
  }

  showMenu(onStart: (cfg: { civ: CivId; god: string; enemy: CivId | 'random'; diff: 'easy' | 'normal' | 'hard' }) => void) {
    const m = $('menu');
    let civ: CivId = 'greek', god = 'zeus', enemy: CivId | 'random' = 'random', diff: 'easy' | 'normal' | 'hard' = 'normal';
    const render = () => {
      const c = CIVS[civ];
      m.innerHTML = `<h1>MYTHIC CONQUEST</h1><div class="sub">Gods · Heroes · Monsters · Empires</div>
        <div class="step">1 · Choose your civilization</div>
        <div class="menu-cards">${(Object.keys(CIVS) as CivId[]).map((id) => {
          const x = CIVS[id];
          return `<div class="card ${id === civ ? 'on' : ''}" data-civ="${id}" style="border-top:4px solid ${x.accent}"><h3 style="color:${x.accent}">${x.name}</h3><p>${x.tagline}</p><p class="tag">${x.favor}</p></div>`;
        }).join('')}</div>
        <div class="step">2 · Choose your major god</div>
        <div class="menu-cards">${c.majors.map((id) => {
          const g = MAJORS[id];
          return `<div class="card ${id === god ? 'on' : ''}" data-god="${id}"><h3>${g.name}</h3><p class="tag">${g.title}</p><p>${g.blurb}</p><p><b>⚡ ${g.power.name}</b><br>${g.power.desc}</p></div>`;
        }).join('')}</div>
        <div class="step">3 · Difficulty &nbsp;·&nbsp; Enemy</div>
        <div class="row seg">${(['easy', 'normal', 'hard'] as const).map((d) => `<button class="${d === diff ? 'on' : ''}" data-diff="${d}">${d[0].toUpperCase() + d.slice(1)}</button>`).join('')}
          <span style="width:18px"></span>
          ${(['random', 'greek', 'egypt', 'norse'] as const).map((d) => `<button class="${d === enemy ? 'on' : ''}" data-enemy="${d}">${d === 'random' ? 'Random' : CIVS[d].name}</button>`).join('')}</div>
        <button class="big" id="start">BEGIN THE WAR</button>`;
      m.querySelectorAll<HTMLElement>('[data-civ]').forEach((el) => el.onclick = () => { civ = el.dataset.civ as CivId; god = CIVS[civ].majors[0]; render(); });
      m.querySelectorAll<HTMLElement>('[data-god]').forEach((el) => el.onclick = () => { god = el.dataset.god!; render(); });
      m.querySelectorAll<HTMLElement>('[data-diff]').forEach((el) => el.onclick = () => { diff = el.dataset.diff as typeof diff; render(); });
      m.querySelectorAll<HTMLElement>('[data-enemy]').forEach((el) => el.onclick = () => { enemy = el.dataset.enemy as typeof enemy; render(); });
      $('start').onclick = () => onStart({ civ, god, enemy, diff });
    };
    render();
    m.classList.remove('hidden');
  }
}
