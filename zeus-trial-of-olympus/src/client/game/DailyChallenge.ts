import type { InitResponse, LeaderboardEntry, ScoreResponse } from '../../shared/api';
import { hashString } from './rng';

export interface Profile {
  username: string | null;
  best: number;
  dailyBest: number;
  leaderboard: LeaderboardEntry[];
  online: boolean;
}

const LS_BEST = 'zeus.best';
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
 * Daily Trial: one UTC-date seed shared by everybody, plus persistence.
 * Every network call is best-effort — the game never waits on or fails because of it.
 */
export class DailyChallenge {
  readonly dayKey = new Date().toISOString().slice(0, 10);
  readonly seed = hashString(`zeus-trial-of-olympus:${this.dayKey}`);
  profile: Profile = {
    username: null,
    best: Number(lsGet(LS_BEST) ?? 0) || 0,
    dailyBest: Number(lsGet(`zeus.daily.${this.dayKey}`) ?? 0) || 0,
    leaderboard: [],
    online: false,
  };

  async load(): Promise<Profile> {
    try {
      const r = await api<InitResponse>('/api/init');
      this.profile = {
        username: r.username,
        best: Math.max(r.best, this.profile.best),
        dailyBest: Math.max(r.dailyBest, this.profile.dailyBest),
        leaderboard: r.leaderboard,
        online: true,
      };
    } catch {
      /* offline / local dev: keep local profile */
    }
    return this.profile;
  }

  /** Records a finished run locally right away, then tries the server. */
  async submit(score: number, kills: number, maxCombo: number, daily: boolean): Promise<{ newBest: boolean; rank: number | null }> {
    const p = this.profile;
    const newBest = score > p.best;
    p.best = Math.max(p.best, score);
    lsSet(LS_BEST, String(p.best));
    if (daily) {
      p.dailyBest = Math.max(p.dailyBest, score);
      lsSet(`zeus.daily.${this.dayKey}`, String(p.dailyBest));
    }
    let rank: number | null = null;
    try {
      const r = await api<ScoreResponse>('/api/score', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ score, kills, maxCombo, daily }),
      });
      p.best = Math.max(p.best, r.best);
      p.dailyBest = Math.max(p.dailyBest, r.dailyBest);
      p.leaderboard = r.leaderboard;
      p.online = true;
      rank = r.rank;
    } catch {
      /* ignore — the local record is already saved */
    }
    return { newBest, rank };
  }
}
