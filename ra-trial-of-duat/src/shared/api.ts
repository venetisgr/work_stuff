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
  leaderboard: LeaderboardEntry[];
}

export interface ScoreRequest {
  score: number;
  kills: number;
  maxCombo: number;
  daily: boolean;
}

export interface ScoreResponse {
  best: number;
  dailyBest: number;
  newBest: boolean;
  /** 1-based rank on today's board, when available. */
  rank: number | null;
  leaderboard: LeaderboardEntry[];
}

/** Hard cap used by the server to reject absurd (tampered) scores. */
export const MAX_PLAUSIBLE_SCORE = 2_000_000;
