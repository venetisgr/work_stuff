import { Game } from './game/Game';

/** Shows startup failures on screen — easier to report from a phone or a Reddit webview than a console. */
function showError(msg: string): void {
  const loading = document.getElementById('loading');
  const hint = loading?.querySelector('.hint');
  loading?.classList.remove('hidden');
  if (hint) hint.textContent = `Error: ${msg}`.slice(0, 240);
}
window.addEventListener('error', (e) => showError(e.message));
window.addEventListener('unhandledrejection', (e) => showError(String((e.reason as Error)?.message ?? e.reason)));

const host = document.getElementById('stage')!;
const game = new Game(host);
game.init().catch((err: unknown) => {
  console.error(err);
  showError(err instanceof Error ? err.message : 'Could not start the game (WebGL unavailable?)');
});

if (import.meta.hot) import.meta.hot.dispose(() => game.dispose());
