import type { LeaderboardEntry } from '../../shared/api';
import { CFG, MAX_LEVEL, levelById } from './config';
import type { Profile } from './DailyChallenge';

interface DailyTier {
  label: string;
  level: number;
}

export interface ResultData {
  score: number;
  best: number;
  kills: number;
  maxCombo: number;
  newBest: boolean;
  victory: boolean;
  tier: number;
  tierLabel: string;
  dayKey: string;
  rank: number | null;
  leaderboard: LeaderboardEntry[];
  username: string | null;
  level: number;
  levelName: string;
  cleared: boolean;
}

const $ = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;
const fmt = (n: number) => Math.floor(n).toLocaleString('en-US');

/** All DOM: HUD, menu, results, banners and floating score popups. */
export class UI {
  readonly flash = $('flash');
  readonly stage = $('stage');
  readonly touchRoot = $('touch');
  private hud = $('hud');
  private el = {
    score: $('score'),
    time: $('time'),
    timeBox: $('hud-time'),
    combo: $('combo'),
    comboBox: $('hud-combo'),
    fill: $('charge-fill'),
    charge: $('charge'),
    hint: $('charge-hint'),
    health: $('health'),
    healthFill: $('health-fill'),
    healthText: $('health-text'),
    banner: $('banner'),
    popups: $('popups'),
    mute: $('btn-mute'),
    vignette: $('vignette'),
  };
  private popPool: HTMLElement[] = [];
  private lastScore = -1;
  private lastCombo = -1;
  private lastHp = -1;
  private lastTime = -1;
  private lastCharge = -1;
  private lastReady = false;
  private cleanup: Array<() => void> = [];
  private selected = 1;

  constructor() {
    for (let i = 0; i < 16; i++) {
      const d = document.createElement('div');
      d.className = 'pop';
      d.style.display = 'none';
      this.el.popups.appendChild(d);
      this.popPool.push(d);
    }
    this.touchRoot.classList.add('avail');
    if (matchMedia('(pointer: coarse)').matches || 'ontouchstart' in window) document.body.classList.add('is-touch');
    else this.touchRoot.classList.remove('avail');
  }

  setLoading(f: number): void {
    $('load-fill').style.width = `${Math.round(f * 100)}%`;
  }

  hideLoading(): void {
    $('loading').classList.add('hidden');
  }

  setTouch(on: boolean): void {
    document.body.classList.toggle('is-touch', on);
    this.touchRoot.classList.toggle('avail', on);
  }

  bind(el: HTMLElement, ev: string, fn: (e: Event) => void): void {
    el.addEventListener(ev, fn);
    this.cleanup.push(() => el.removeEventListener(ev, fn));
  }

  onMute(cb: () => void): void {
    this.bind(this.el.mute, 'click', cb);
  }

  setMuteIcon(muted: boolean): void {
    this.el.mute.textContent = muted ? '🔇' : '🔊';
  }

  /** Level carousel (levels 1–100) plus the three Daily Trials. */
  showMenu(profile: Profile, dayKey: string, tiers: DailyTier[], selected: number, onPlay: (level: number, tier: number) => void): void {
    this.selected = Math.min(selected, profile.unlocked);
    $('daily-date').textContent = dayKey;
    $('daily-row').innerHTML = tiers
      .map((t, i) => {
        const best = profile.dailyBest[i];
        return `<button class="daily-btn d${i + 1} ui-btn" data-t="${i + 1}"><b>${t.label}</b><span>${levelById(t.level).name}</span><small>${best ? `BEST ${fmt(best)}` : `LEVEL ${t.level}`}</small></button>`;
      })
      .join('');
    $('daily-row').onclick = (e) => {
      const btn = (e.target as HTMLElement).closest<HTMLElement>('.daily-btn');
      if (btn) onPlay(0, Number(btn.dataset.t));
    };
    $('menu').classList.remove('hidden');
    $('results').classList.add('hidden');
    this.hud.classList.add('hidden');
    const strip = $('lv-strip');
    const free = $('play-free');
    const render = (center = true) => {
      const last = Math.min(MAX_LEVEL, profile.unlocked + 1); // one locked preview
      let html = '';
      for (let n = 1; n <= last; n++) {
        const L = levelById(n);
        const locked = n > profile.unlocked;
        const best = profile.levelBest[n];
        const stars = Array.from({ length: 5 }, (_, i) => `<span class="${i < L.difficulty ? '' : 'off'}">⚡</span>`).join('');
        html += `<button class="lv-card ${n === this.selected ? 'sel' : ''} ${locked ? 'locked' : ''} ${L.boss ? 'boss' : ''}" data-n="${n}">
          <div class="n">${locked ? '🔒 ' : L.boss ? '👑 ' : ''}LEVEL ${n}</div><div class="nm">${L.name}</div><div class="st">${stars}</div>
          <div class="sub">${locked ? `Clear level ${n - 1}` : best ? `BEST ${fmt(best)}` : `${L.radius * 2}m · ${L.time}s`}</div></button>`;
      }
      strip.innerHTML = html;
      const L = levelById(this.selected);
      free.innerHTML = `PLAY LEVEL ${this.selected}<small>${L.boss ? '👑 CHAMPION TRIAL · ' : ''}BEST ${fmt(profile.levelBest[this.selected] ?? 0)}</small>`;
      if (center) strip.querySelector('.sel')?.scrollIntoView({ inline: 'center', block: 'nearest' });
    };
    render();
    const step = (d: number) => {
      this.selected = Math.min(profile.unlocked, Math.max(1, this.selected + d));
      render();
    };
    strip.onclick = (e) => {
      const card = (e.target as HTMLElement).closest<HTMLElement>('.lv-card');
      if (!card || card.classList.contains('locked')) return;
      this.selected = Number(card.dataset.n);
      render(false);
    };
    $('lv-prev').onclick = () => step(-1);
    $('lv-next').onclick = () => step(1);
    $('lv-prev10').onclick = () => step(-10);
    $('lv-next10').onclick = () => step(10);
    free.onclick = () => onPlay(this.selected, 0);
  }

  setLevelLabel(text: string): void {
    $('hud-level').textContent = text;
  }

  setTouchVisible(on: boolean): void {
    this.touchRoot.classList.toggle('playing', on);
  }

  hideMenu(): void {
    $('menu').classList.add('hidden');
  }

  showHud(): void {
    this.hud.classList.remove('hidden');
    this.lastScore = this.lastCombo = this.lastHp = this.lastTime = this.lastCharge = -1;
    this.lastReady = false;
    document.body.classList.remove('ult-ready');
    this.el.banner.classList.remove('show');
    this.el.vignette.classList.remove('danger');
  }

  hideHud(): void {
    this.hud.classList.add('hidden');
  }

  update(score: number, timeLeft: number, mult: number, tier: number, charge: number, hp: number): void {
    const s = Math.floor(score);
    if (s !== this.lastScore) {
      this.lastScore = s;
      this.el.score.textContent = fmt(s);
    }
    const t = Math.max(0, Math.ceil(timeLeft));
    if (t !== this.lastTime) {
      this.lastTime = t;
      this.el.time.textContent = String(t);
      this.el.timeBox.classList.toggle('low', t <= 10);
    }
    if (mult !== this.lastCombo) {
      const up = mult > this.lastCombo && this.lastCombo > 0;
      this.lastCombo = mult;
      this.el.combo.textContent = `x${mult}`;
      this.el.comboBox.className = `t${tier + 1}`;
      this.el.comboBox.id = 'hud-combo';
      if (up) {
        this.el.comboBox.classList.add('pop');
        setTimeout(() => this.el.comboBox.classList.remove('pop'), 160);
      }
    }
    const c = Math.round(charge);
    if (c !== this.lastCharge) {
      this.lastCharge = c;
      this.el.fill.style.width = `${c}%`;
      const ready = c >= 100;
      if (ready !== this.lastReady) {
        this.lastReady = ready;
        this.el.charge.classList.toggle('full', ready);
        document.body.classList.toggle('ult-ready', ready);
        this.el.hint.textContent = document.body.classList.contains('is-touch') ? 'ULTIMATE READY' : 'ULTIMATE READY — E / RIGHT CLICK';
      }
    }
    if (hp !== this.lastHp) {
      const dropped = this.lastHp > hp && this.lastHp >= 0;
      this.lastHp = hp;
      const k = Math.max(0, hp) / CFG.MAX_HP;
      this.el.healthFill.style.width = `${k * 100}%`;
      this.el.healthText.textContent = `${hp} / ${CFG.MAX_HP}`;
      this.el.health.classList.toggle('low', hp <= 1);
      this.el.health.classList.toggle('mid', hp === 2 || hp === 3);
      if (dropped) {
        this.el.health.classList.add('hit');
        setTimeout(() => this.el.health.classList.remove('hit'), 300);
      }
      this.el.vignette.classList.toggle('danger', hp <= 1);
    }
  }

  banner(text: string): void {
    const b = this.el.banner;
    b.textContent = text;
    b.classList.remove('show');
    void b.offsetWidth;
    b.classList.add('show');
  }

  popup(px: number, py: number, text: string, kind: string): void {
    const d = this.popPool.find((p) => p.style.display === 'none') ?? this.popPool[0];
    d.textContent = text;
    d.className = `pop ${kind}`;
    d.style.left = `${px}px`;
    d.style.top = `${py}px`;
    d.style.transform = 'translate(-50%,-50%)';
    d.style.display = 'block';
    d.style.animation = 'none';
    void d.offsetWidth;
    d.style.animation = '';
    clearTimeout((d as unknown as { _t?: number })._t);
    (d as unknown as { _t?: number })._t = window.setTimeout(() => (d.style.display = 'none'), kind === 'multi' ? 1300 : 900);
  }

  showResults(r: ResultData, onAgain: () => void, onMenu: () => void, onNext: () => void): void {
    this.hideHud();
    $('res-title').textContent = r.victory ? (r.tier ? `DAILY TRIAL ${r.tierLabel} CLEARED` : r.level >= MAX_LEVEL ? 'ALL 100 TRIALS CONQUERED' : `LEVEL ${r.level} CLEARED`) : 'ZEUS HAS FALLEN';
    $('res-score').textContent = fmt(r.score);
    $('res-new').classList.toggle('hidden', !r.newBest);
    $('res-best').textContent = fmt(r.best);
    $('res-kills').textContent = String(r.kills);
    $('res-combo').textContent = `x${r.maxCombo}`;
    const board = $('res-board');
    if (r.leaderboard.length) {
      let h = `<h4>DAILY ${r.tierLabel} · ${r.dayKey}${r.rank ? ` · YOU #${r.rank}` : ''}</h4>`;
      r.leaderboard.slice(0, 5).forEach((e, i) => {
        h += `<div class="row ${e.username === r.username ? 'me' : ''}"><span>${i + 1}. ${e.username.replace(/[<>&]/g, '')}</span><span>${fmt(e.score)}</span></div>`;
      });
      board.innerHTML = h;
    } else {
      board.innerHTML = '';
    }
    $('results').classList.remove('hidden');
    const next = $('next-level');
    next.classList.toggle('hidden', !(r.cleared && !r.tier && r.level < MAX_LEVEL));
    next.onclick = () => onNext();
    const again = $('play-again');
    again.onclick = onAgain;
    $('to-menu').onclick = onMenu;
  }

  hideResults(): void {
    $('results').classList.add('hidden');
  }

  dispose(): void {
    this.cleanup.forEach((f) => f());
    this.cleanup = [];
  }
}
