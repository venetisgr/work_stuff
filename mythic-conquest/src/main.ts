import { Game } from './game';
import { CIVS, CivId } from './data';
import { Difficulty } from './ai';

const game = new Game(document.getElementById('c') as HTMLCanvasElement);
const q = new URLSearchParams(location.search);
// Dev shortcuts: ?civ=greek&god=zeus&enemy=norse&diff=hard&seed=3  (&auto=1 lets the AI play both sides)
if (q.has('civ')) {
  const civ = (q.get('civ') as CivId) in CIVS ? (q.get('civ') as CivId) : 'greek';
  game.start(
    { civ, god: q.get('god') ?? CIVS[civ].majors[0], enemy: (q.get('enemy') as CivId) || 'random', diff: (q.get('diff') as Difficulty) || 'normal' },
    q.has('seed') ? Number(q.get('seed')) : undefined, q.has('auto'),
  );
} else game.toMenu();
