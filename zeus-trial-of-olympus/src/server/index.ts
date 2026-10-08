import { createServer, getServerPort, context, redis, reddit } from '@devvit/web/server';
import type { IncomingMessage, ServerResponse } from 'node:http';
import {
  DAILY_TIERS,
  MAX_LEVEL,
  maxPlausibleScore,
  type InitResponse,
  type LeaderboardEntry,
  type ScoreRequest,
  type ScoreResponse,
} from '../shared/api';

const TOP_N = 10;
const dayKeyNow = () => new Date().toISOString().slice(0, 10);
const boardKey = (day: string, tier: number) => `zeus:lb:${day}:${tier}`;
const bestKey = (user: string) => `zeus:best:${user}`;
const clampLevel = (n: unknown) => Math.min(MAX_LEVEL, Math.max(1, Math.floor(Number(n)) || 1));

async function readBody(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const c of req) {
    size += (c as Buffer).length;
    if (size > 8192) throw new Error('body too large');
    chunks.push(c as Buffer);
  }
  const text = Buffer.concat(chunks).toString('utf8');
  return text ? JSON.parse(text) : {};
}

function send(res: ServerResponse, status: number, body: unknown): void {
  // Devvit's proxy rejects responses without an explicit Content-Length.
  const payload = JSON.stringify(body);
  res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) });
  res.end(payload);
}

async function topBoard(day: string, tier: number): Promise<LeaderboardEntry[]> {
  const rows = await redis.zRange(boardKey(day, tier), 0, TOP_N - 1, { by: 'rank', reverse: true });
  return rows.map((r) => ({ username: r.member, score: r.score }));
}

async function getUser(): Promise<string | null> {
  try {
    return (await reddit.getCurrentUsername()) ?? null;
  } catch {
    return null;
  }
}

async function handleInit(res: ServerResponse): Promise<void> {
  const day = dayKeyNow();
  const username = await getUser();
  let best = 0;
  const dailyBest: number[] = [0, 0, 0];
  let unlocked = 1;
  const levelBest: Record<string, number> = {};
  if (username) {
    const h = await redis.hGetAll(bestKey(username));
    best = Number(h.all ?? 0);
    unlocked = clampLevel(h.unlocked ?? 1);
    for (const [k, v] of Object.entries(h)) if (k.startsWith('L')) levelBest[k.slice(1)] = Number(v) || 0;
    for (let t = 1; t <= DAILY_TIERS; t++) dailyBest[t - 1] = (await redis.zScore(boardKey(day, t), username)) ?? 0;
  }
  const leaderboards: LeaderboardEntry[][] = [];
  for (let t = 1; t <= DAILY_TIERS; t++) leaderboards.push(await topBoard(day, t));
  const body: InitResponse = { username, dayKey: day, best, dailyBest, unlocked, levelBest, leaderboards };
  send(res, 200, body);
}

async function handleScore(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const data = (await readBody(req)) as Partial<ScoreRequest>;
  const score = Math.floor(Number(data.score));
  const level = clampLevel(data.level);
  const tier = Math.min(DAILY_TIERS, Math.max(0, Math.floor(Number(data.tier)) || 0));
  if (!Number.isFinite(score) || score < 0 || score > maxPlausibleScore(level)) {
    return send(res, 400, { error: 'invalid score' });
  }
  const day = dayKeyNow();
  const username = await getUser();
  if (!username) {
    // Logged-out players can play, they just don't get stored.
    const empty: ScoreResponse = { best: 0, dailyBest: 0, levelBest: 0, unlocked: 1, newBest: false, rank: null, leaderboard: tier ? await topBoard(day, tier) : [] };
    return send(res, 200, empty);
  }
  const h = await redis.hGetAll(bestKey(username));
  const prevBest = Number(h.all ?? 0);
  const prevLevelBest = Number(h[`L${level}`] ?? 0);
  let unlocked = clampLevel(h.unlocked ?? 1);
  const best = Math.max(prevBest, score);
  const update: Record<string, string> = {};
  if (score > prevBest) update.all = String(score);
  if (score > prevLevelBest) update[`L${level}`] = String(score);
  // Clearing a level (surviving the full timer) unlocks the next one.
  if (data.cleared && level >= unlocked && level < MAX_LEVEL) {
    unlocked = level + 1;
    update.unlocked = String(unlocked);
  }
  if (Object.keys(update).length) await redis.hSet(bestKey(username), update);

  let dailyBest = tier ? ((await redis.zScore(boardKey(day, tier), username)) ?? 0) : 0;
  if (tier && score > dailyBest) {
    dailyBest = score;
    await redis.zAdd(boardKey(day, tier), { member: username, score });
    await redis.expire(boardKey(day, tier), 60 * 60 * 24 * 14);
  }
  let rank: number | null = null;
  if (tier) {
    const r = await redis.zRank(boardKey(day, tier), username);
    if (r !== undefined) {
      const total = await redis.zCard(boardKey(day, tier));
      rank = total - r;
    }
  }
  const out: ScoreResponse = {
    best,
    dailyBest,
    levelBest: Math.max(prevLevelBest, score),
    unlocked,
    newBest: score > prevLevelBest,
    rank,
    leaderboard: tier ? await topBoard(day, tier) : [],
  };
  send(res, 200, out);
}

async function createPost() {
  return reddit.submitCustomPost({
    subredditName: context.subredditName,
    title: 'Zeus: Trial of Olympus — survive the storm, beat the daily score ⚡',
  });
}

const server = createServer(async (req, res) => {
  try {
    const url = req.url ?? '';
    if (req.method === 'GET' && url.startsWith('/api/init')) return await handleInit(res);
    if (req.method === 'POST' && url.startsWith('/api/score')) return await handleScore(req, res);
    if (req.method === 'POST' && url.startsWith('/internal/menu/post-create')) {
      const post = await createPost();
      return send(res, 200, { navigateTo: post });
    }
    if (req.method === 'POST' && url.startsWith('/internal/on-app-install')) {
      // A failed post must never fail the install itself.
      try {
        await createPost();
      } catch (err) {
        console.error('could not create the install post', err);
      }
      return send(res, 200, {});
    }
    send(res, 404, { error: 'not found' });
  } catch (err) {
    console.error('zeus server error', err);
    send(res, 500, { error: 'server error' });
  }
});

server.on('error', (err) => console.error(`server error: ${err.stack}`));
server.listen(getServerPort());
