import { createServer, getServerPort, context, redis, reddit } from '@devvit/web/server';
import type { IncomingMessage, ServerResponse } from 'node:http';
import {
  MAX_PLAUSIBLE_SCORE,
  type InitResponse,
  type LeaderboardEntry,
  type ScoreRequest,
  type ScoreResponse,
} from '../shared/api';

const TOP_N = 10;
const dayKeyNow = () => new Date().toISOString().slice(0, 10);
const boardKey = (day: string) => `zeus:lb:${day}`;
const bestKey = (user: string) => `zeus:best:${user}`;

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

async function topBoard(day: string): Promise<LeaderboardEntry[]> {
  const rows = await redis.zRange(boardKey(day), 0, TOP_N - 1, { by: 'rank', reverse: true });
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
  let dailyBest = 0;
  if (username) {
    best = Number((await redis.hGet(bestKey(username), 'all')) ?? 0);
    dailyBest = (await redis.zScore(boardKey(day), username)) ?? 0;
  }
  const body: InitResponse = { username, dayKey: day, best, dailyBest, leaderboard: await topBoard(day) };
  send(res, 200, body);
}

async function handleScore(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const data = (await readBody(req)) as Partial<ScoreRequest>;
  const score = Math.floor(Number(data.score));
  if (!Number.isFinite(score) || score < 0 || score > MAX_PLAUSIBLE_SCORE) {
    return send(res, 400, { error: 'invalid score' });
  }
  const day = dayKeyNow();
  const username = await getUser();
  if (!username) {
    // Logged-out players can play, they just don't get stored.
    const empty: ScoreResponse = { best: 0, dailyBest: 0, newBest: false, rank: null, leaderboard: await topBoard(day) };
    return send(res, 200, empty);
  }
  const prevBest = Number((await redis.hGet(bestKey(username), 'all')) ?? 0);
  const best = Math.max(prevBest, score);
  if (score > prevBest) await redis.hSet(bestKey(username), { all: String(score) });

  let dailyBest = (await redis.zScore(boardKey(day), username)) ?? 0;
  if (data.daily && score > dailyBest) {
    dailyBest = score;
    await redis.zAdd(boardKey(day), { member: username, score });
    await redis.expire(boardKey(day), 60 * 60 * 24 * 14);
  }
  let rank: number | null = null;
  if (data.daily) {
    const r = await redis.zRank(boardKey(day), username);
    if (r !== undefined) {
      const total = await redis.zCard(boardKey(day));
      rank = total - r;
    }
  }
  const out: ScoreResponse = {
    best,
    dailyBest,
    newBest: score > prevBest,
    rank,
    leaderboard: await topBoard(day),
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
