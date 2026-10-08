// Devvit Web server: only creates the game post. The game itself runs entirely in the client.
import { createServer, getServerPort, context, reddit } from '@devvit/web/server';
import type { ServerResponse } from 'node:http';

function send(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
}

async function createPost() {
  return reddit.submitCustomPost({
    subredditName: context.subredditName,
    title: 'Mythic Conquest — command Greek, Egyptian & Norse gods in a real-time strategy war ⚡',
  });
}

const server = createServer(async (req, res) => {
  try {
    const url = req.url ?? '';
    if (req.method === 'POST' && url.startsWith('/internal/menu/post-create')) {
      const post = await createPost();
      return send(res, 200, { navigateTo: post });
    }
    if (req.method === 'POST' && url.startsWith('/internal/on-app-install')) {
      await createPost();
      return send(res, 200, { status: 'ok' });
    }
    send(res, 404, { error: 'not found' });
  } catch (err) {
    console.error('mythic-conquest server error', err);
    send(res, 500, { error: 'server error' });
  }
});

server.on('error', (err) => console.error(`server error: ${err.stack}`));
server.listen(getServerPort());
