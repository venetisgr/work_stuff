import type { InitResponse, LeaderboardEntry, ScoreResponse } from '../../shared/api';
import { hashString } from './rng';

export interface Profile {
  username: string | null;
  best: number;
  dailyBest: number;
  /** Highest level that may be started. */
  unlocked: number;
  levelBest: Record<number, number>;
  leaderboard: LeaderboardEntry[];
  online: boolean;
}

const LS_BEST = 'zeus.best';
const LS_UNLOCKED = 'zeus.unlocked';
const lsGet = (k: string): string | null => {
  try {
    return localStorage.getItem(k);
  } catch {
    return null;
  }
};
const lsSet = (k: string, v: string) => {
  try {
    localStorage.setItem(k, v);
  } catch {
    /* storage can be blocked in embedded webviews */
  }
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 4000);
  try {
    const res = await fetch(path, { ...init, signal: ctrl.signal });
    if (!res.ok) throw new Error(String(res.status));
    return (await res.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Daily Trial + progress: one UTC-date seed shared by everybody, plus persistence.
 * Every network call is best-effort — the game never waits on or fails because of it.
 */
export class DailyChallenge {
  readonly dayKey = new Date().toISOString().slice(0, 10);
  readonly seed = hashString(`zeus-trial-of-olympus:${this.dayKey}`);
  /** The Daily Trial always runs on one of the first three arenas so it is open to everyone. */
  readonly level = 1 + (this.seed % 3);
  profile: Profile = {
    username: null,
    best: Number(lsGet(LS_BEST) ?? 0) || 0,
    dailyBest: Number(lsGet(`zeus.daily.${this.dayKey}`) ?? 0) || 0,
    unlocked: Math.max(1, Number(lsGet(LS_UNLOCKED) ?? 1) || 1),
    levelBest: {},
    leaderboard: [],
    online: false,
  };

  constructor() {
    for (let l = 1; l <= this.profile.unlocked + 1; l++) {
      const v = Number(lsGet(`zeus.level.${l}`) ?? 0);
      if (v) this.profile.levelBest[l] = v;
    }
  }

  async load(): Promise<Profile> {
    try {
      const r = await api<InitResponse>('/api/init');
      const p = this.profile;
      p.username = r.username;
      p.best = Math.max(r.best, p.best);
      p.dailyBest = Math.max(r.dailyBest, p.dailyBest);
      p.unlocked = Math.max(r.unlocked, p.unlocked);
      for (const [k, v] of Object.entries(r.levelBest)) p.levelBest[Number(k)] = Math.max(v, p.levelBest[Number(k)] ?? 0);
      p.leaderboard = r.leaderboard;
      p.online = true;
      this.saveLocal();
    } catch {
      /* offline / local dev: keep local profile */
    }
    return this.profile;
  }

  private saveLocal(): void {
    const p = this.profile;
    lsSet(LS_BEST, String(p.best));
    lsSet(LS_UNLOCKED, String(p.unlocked));
    for (const [l, v] of Object.entries(p.levelBest)) lsSet(`zeus.level.${l}`, String(v));
  }

  /** Records a finished run locally right away, then tries the server. */
  async submit(score: number, kills: number, maxCombo: number, daily: boolean, level: number, cleared: boolean): Promise<{ newBest: boolean; rank: number | null }> {
    const p = this.profile;
    const newBest = score > (p.levelBest[level] ?? 0);
    p.best = Math.max(p.best, score);
    p.levelBest[level] = Math.max(p.levelBest[level] ?? 0, score);
    if (cleared && level >= p.unlocked) p.unlocked = level + 1;
    if (daily) {
      p.dailyBest = Math.max(p.dailyBest, score);
      lsSet(`zeus.daily.${this.dayKey}`, String(p.dailyBest));
    }
    this.saveLocal();
    let rank: number | null = null;
    try {
      const r = await api<ScoreResponse>('/api/score', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ score, kills, maxCombo, daily, level, cleared }),
      });
      p.best = Math.max(p.best, r.best);
      p.dailyBest = Math.max(p.dailyBest, r.dailyBest);
      p.unlocked = Math.max(p.unlocked, r.unlocked);
      p.levelBest[level] = Math.max(p.levelBest[level] ?? 0, r.levelBest);
      p.leaderboard = r.leaderboard;
      p.online = true;
      rank = r.rank;
      this.saveLocal();
    } catch {
      /* ignore — the local record is already saved */
    }
    return { newBest, rank };
  }
}
