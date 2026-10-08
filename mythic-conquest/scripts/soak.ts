// Headless AI-vs-AI soak test: node scripts/soak.ts [seconds] [seed]
import { Sim } from '../src/sim';
import { AI } from '../src/ai';
import { CIVS, MAJORS, CivId } from '../src/data';

const ids = Object.keys(CIVS) as CivId[];
const maxT = Number(process.argv[2] ?? 1500), seed0 = Number(process.argv[3] ?? 1);
let fails = 0;
for (let m = 0; m < 3; m++) {
  const c0 = ids[m % 3], c1 = ids[(m + 1) % 3];
  const g0 = CIVS[c0].majors[m % 3], g1 = CIVS[c1].majors[(m + 1) % 3];
  const sim = new Sim({ civs: [c0, c1], gods: [g0, g1], seed: seed0 + m, ai: [true, true] });
  const ais = [new AI(sim, 0, 'hard'), new AI(sim, 1, 'normal')];
  const t0 = Date.now();
  let step = 0;
  while (sim.time < maxT && sim.winner < 0) {
    sim.tick(0.1);
    ais.forEach((a) => a.update(0.1));
    sim.events.length = 0;
    if (++step % 3000 === 0) {
      const [a, b] = sim.players;
      console.log(`  t=${Math.round(sim.time)} P0 ${a.civ}/${MAJORS[a.god].name} age${a.age} pop${a.pop} f${Math.round(a.food)} w${Math.round(a.wood)} g${Math.round(a.gold)} fav${Math.round(a.favor)} | P1 ${b.civ}/${MAJORS[b.god].name} age${b.age} pop${b.pop} f${Math.round(b.food)} w${Math.round(b.wood)} g${Math.round(b.gold)} fav${Math.round(b.favor)}`);
    }
  }
  const [a, b] = sim.players;
  console.log(`match ${m}: ${c0}/${MAJORS[g0].name} vs ${c1}/${MAJORS[g1].name} -> winner ${sim.winner} at t=${Math.round(sim.time)}s (${Date.now() - t0}ms) kills ${a.kills}/${b.kills} age ${a.age}/${b.age} gathered ${Math.round(a.gathered)}/${Math.round(b.gathered)}`);
  if (a.gathered < 500 || b.gathered < 500) { console.log('  FAIL: economy did not work'); fails++; }
}
process.exit(fails ? 1 : 0);
