import { requestExpandedMode } from '@devvit/web/client';
import type { InitResponse } from '../shared/api';

const $ = (id: string) => document.getElementById(id) as HTMLElement;

$('day').textContent = new Date().toISOString().slice(0, 10);
try {
  const best = Number(localStorage.getItem('zeus.best') ?? 0);
  if (best) $('best').textContent = best.toLocaleString('en-US');
} catch {
  /* storage can be blocked inside the webview */
}

// Best score from the server, best-effort.
fetch('/api/init')
  .then((r) => (r.ok ? (r.json() as Promise<InitResponse>) : null))
  .then((d) => {
    if (d) $('best').textContent = d.best.toLocaleString('en-US');
  })
  .catch(() => {});

$('play').addEventListener('click', (e) => {
  try {
    requestExpandedMode(e as MouseEvent, 'game');
  } catch {
    // Not inside Reddit (e.g. local dev): open the game page directly.
    window.location.href = './game.html';
  }
});
