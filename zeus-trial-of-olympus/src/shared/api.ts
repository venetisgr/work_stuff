/** Types shared by the Devvit server and the game client. */

export interface LeaderboardEntry {
  username: string;
  score: number;
}

export interface InitResponse {
  username: string | null;
  /** UTC day key, e.g. "2026-10-08" — the Daily Trial id. */
  dayKey: string;
  best: number;
  dailyBest: number;
  /** Highest level the player may start (levels unlock by clearing the previous one). */
  unlocked: number;
  /** Best score per level number. */
  levelBest: Record<string, number>;
  leaderboard: LeaderboardEntry[];
}

export interface ScoreRequest {
  score: number;
  kills: number;
  maxCombo: number;
  daily: boolean;
  level: number;
  /** True if the full timer was survived. */
  cleared: boolean;
}

export interface ScoreResponse {
  best: number;
  dailyBest: number;
  levelBest: number;
  unlocked: number;
  newBest: boolean;
  /** 1-based rank on today's board, when available. */
  rank: number | null;
  leaderboard: LeaderboardEntry[];
}

/** Hard cap used by the server to reject absurd (tampered) scores. Levels scale scores, so it grows with the level. */
export const maxPlausibleScore = (level: number): number => 2_000_000 * Math.max(1, level);
export const MAX_LEVEL = 9999;
