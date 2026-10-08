import { Game } from './game/Game';

const host = document.getElementById('stage')!;
const game = new Game(host);
game.init().catch((err) => {
  console.error(err);
  const hint = document.querySelector('#loading .hint');
  if (hint) hint.textContent = 'Could not start WebGL. Please try another browser.';
});

if (import.meta.hot) import.meta.hot.dispose(() => game.dispose());
