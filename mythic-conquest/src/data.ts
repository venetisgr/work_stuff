// All static game data: civilizations, gods, units, buildings, god powers.

export const MAP_N = 80;

export type Res = 'food' | 'wood' | 'gold';
export type CivId = 'greek' | 'egypt' | 'norse';
export type Cost = { food?: number; wood?: number; gold?: number; favor?: number };
export type UClass = 'vil' | 'inf' | 'rng' | 'cav' | 'myth';
export type Slot = 'vil' | 'inf' | 'rng' | 'cav';

export interface Mods { hp: number; atk: number; speed: number; gather: number; favor: number; build: number }
export const baseMods = (): Mods => ({ hp: 1, atk: 1, speed: 1, gather: 1, favor: 1, build: 1 });

export const AGE_NAMES = ['', 'Archaic Age', 'Classical Age', 'Heroic Age', 'Mythic Age'];
export const POP_MAX = 120;
export const CARRY = 12;
export const GATHER_RATE: Record<string, number> = { wood: 0.95, gold: 0.85, berries: 1.05, farm: 0.75 };

// ---------------------------------------------------------------- units

export interface Look { h: number; w: number; col: number; flags: string[] }
export interface UnitDef {
  id: string; name: string; cls: UClass; hp: number; atk: number; range: number; speed: number; cd: number;
  cost: Cost; pop: number; time: number; age: number; at: 'tc' | 'barracks' | 'temple' | null;
  r: number; bldMult: number; sight: number; look: Look; desc: string;
  regen?: number; slow?: number; civ?: CivId; slot?: Slot;
}

const BASE: Record<Slot, Omit<UnitDef, 'id' | 'name' | 'look' | 'desc'>> = {
  vil: { cls: 'vil', hp: 50, atk: 3, range: 1, speed: 2.4, cd: 1.2, cost: { food: 50 }, pop: 1, time: 12, age: 1, at: 'tc', r: 0.3, bldMult: 0.1, sight: 7 },
  inf: { cls: 'inf', hp: 100, atk: 9, range: 1, speed: 2.7, cd: 1.0, cost: { food: 50, wood: 25 }, pop: 1, time: 16, age: 1, at: 'barracks', r: 0.34, bldMult: 0.5, sight: 8 },
  rng: { cls: 'rng', hp: 62, atk: 8, range: 7, speed: 2.7, cd: 1.25, cost: { wood: 45, gold: 30 }, pop: 1, time: 17, age: 1, at: 'barracks', r: 0.3, bldMult: 0.25, sight: 9 },
  cav: { cls: 'cav', hp: 135, atk: 11, range: 1, speed: 4.4, cd: 1.0, cost: { food: 75, gold: 40 }, pop: 2, time: 22, age: 2, at: 'barracks', r: 0.42, bldMult: 0.4, sight: 9 },
};

export const UNITS: Record<string, UnitDef> = {};

function addSlotUnits(civ: CivId, names: Record<Slot, string>, col: number, tweak: Partial<Record<Slot, Partial<UnitDef>>>, flags: Record<Slot, string[]>) {
  const looks: Record<Slot, [number, number]> = { vil: [0.85, 0.28], inf: [1.0, 0.34], rng: [0.95, 0.3], cav: [1.15, 0.5] };
  (Object.keys(BASE) as Slot[]).forEach((slot) => {
    const id = `${civ}_${slot}`;
    UNITS[id] = {
      ...BASE[slot], ...(tweak[slot] ?? {}), id, name: names[slot], civ, slot,
      look: { h: looks[slot][0], w: looks[slot][1], col, flags: flags[slot] },
      desc: '',
    };
  });
}

addSlotUnits('greek', { vil: 'Citizen', inf: 'Hoplite', rng: 'Toxotes', cav: 'Hippeus' }, 0xe8e0c8,
  { inf: { hp: 110, desc: 'Phalanx infantry. Strong vs cavalry.' } as Partial<UnitDef> },
  { vil: ['tool'], inf: ['spear', 'shield', 'crest'], rng: ['bow'], cav: ['quad', 'spear'] });
addSlotUnits('egypt', { vil: 'Laborer', inf: 'Spearman', rng: 'Slinger', cav: 'Chariot Rider' }, 0xd9b45a,
  { rng: { range: 8, cost: { wood: 35, gold: 25 } } },
  { vil: ['tool'], inf: ['spear', 'crown'], rng: ['bow'], cav: ['quad', 'bow'] });
addSlotUnits('norse', { vil: 'Gatherer', inf: 'Ulfsark', rng: 'Throwing Axeman', cav: 'Raiding Rider' }, 0x8a6a4a,
  { inf: { atk: 10, hp: 105 }, cav: { hp: 120 } },
  { vil: ['tool'], inf: ['axe', 'shield', 'horn'], rng: ['axe'], cav: ['quad', 'axe'] });

function myth(id: string, name: string, civ: CivId, age: number, p: Partial<UnitDef> & { hp: number; atk: number; range: number; speed: number; cd: number; cost: Cost }, look: Look, desc: string) {
  UNITS[id] = {
    cls: 'myth', pop: 3, time: 30, age, at: 'temple', r: 0.6, bldMult: 1, sight: 9, ...p, id, name, civ, look, desc,
  };
}

myth('minotaur', 'Minotaur', 'greek', 3, { hp: 330, atk: 26, range: 1.2, speed: 3.3, cd: 1.1, cost: { food: 120, gold: 70, favor: 12 }, r: 0.62 },
  { h: 1.6, w: 0.62, col: 0x6b3f2a, flags: ['horns', 'tusk', 'axe'] }, 'Fast brute with a mighty axe.');
myth('centaur', 'Centaur', 'greek', 3, { hp: 170, atk: 16, range: 7.5, speed: 4.4, cd: 1.0, cost: { food: 90, gold: 80, favor: 10 }, r: 0.5 },
  { h: 1.35, w: 0.5, col: 0xa56b3a, flags: ['quad', 'bow', 'crest'] }, 'Swift archer on four legs.');
myth('cyclops', 'Cyclops', 'greek', 4, { hp: 720, atk: 40, range: 1.7, speed: 2.7, cd: 1.7, cost: { food: 220, gold: 150, favor: 25 }, pop: 5, bldMult: 2.5, r: 0.85, time: 40 },
  { h: 2.6, w: 0.95, col: 0x7e8a6a, flags: ['eye', 'club', 'giant'] }, 'One-eyed giant. Crushes buildings.');
myth('medusa', 'Medusa', 'greek', 4, { hp: 340, atk: 30, range: 8, speed: 3.2, cd: 1.5, cost: { food: 150, gold: 140, favor: 22 }, pop: 4, slow: 0.5, r: 0.5, time: 36 },
  { h: 1.5, w: 0.5, col: 0x4f8a5b, flags: ['snakes', 'tail', 'bow'] }, 'Her gaze slows enemies.');

myth('anubite', 'Anubite', 'egypt', 3, { hp: 210, atk: 18, range: 1.1, speed: 4.2, cd: 0.8, cost: { food: 80, gold: 60, favor: 8 }, r: 0.45 },
  { h: 1.3, w: 0.4, col: 0x242030, flags: ['jackal', 'spear'] }, 'Jackal-headed warrior. Fast attacks.');
myth('sphinx', 'Sphinx', 'egypt', 3, { hp: 390, atk: 21, range: 1.3, speed: 3.0, cd: 1.3, cost: { food: 130, gold: 80, favor: 14 }, r: 0.7 },
  { h: 1.2, w: 0.7, col: 0xd0aa5a, flags: ['quad', 'lion', 'crown'] }, 'Tough guardian lion.');
myth('phoenix', 'Phoenix', 'egypt', 4, { hp: 410, atk: 29, range: 7, speed: 5.4, cd: 1.2, cost: { food: 150, gold: 150, favor: 22 }, pop: 4, r: 0.5, time: 36 },
  { h: 1.6, w: 0.5, col: 0xff7a1a, flags: ['wings', 'flame', 'fly'] }, 'Fire bird. Fast ranged flyer.');
myth('croc', 'Great Crocodile', 'egypt', 4, { hp: 680, atk: 38, range: 1.5, speed: 3.4, cd: 1.5, cost: { food: 210, gold: 130, favor: 24 }, pop: 5, bldMult: 1.8, r: 0.85, time: 40 },
  { h: 1.1, w: 0.85, col: 0x4a6b3a, flags: ['quad', 'croc', 'giant'] }, "Sobek's war-beast.");

myth('troll', 'Troll', 'norse', 3, { hp: 370, atk: 19, range: 1.2, speed: 3.0, cd: 1.2, cost: { food: 120, gold: 50, favor: 12 }, regen: 2, r: 0.62 },
  { h: 1.8, w: 0.7, col: 0x5d7a55, flags: ['tusk', 'club'] }, 'Regenerates health.');
myth('valkyrie', 'Valkyrie', 'norse', 3, { hp: 190, atk: 17, range: 6, speed: 4.6, cd: 1.0, cost: { food: 90, gold: 80, favor: 10 }, r: 0.4 },
  { h: 1.3, w: 0.36, col: 0xdde6f0, flags: ['wings', 'spear', 'crest', 'fly'] }, 'Winged spear-maiden.');
myth('fenrir', 'Fenrir Wolf', 'norse', 4, { hp: 570, atk: 41, range: 1.4, speed: 5.2, cd: 1.1, cost: { food: 180, gold: 120, favor: 22 }, pop: 4, r: 0.7, time: 36 },
  { h: 1.2, w: 0.7, col: 0x555a66, flags: ['quad', 'wolf', 'giant'] }, 'Huge, fast wolf.');
myth('frost_giant', 'Frost Giant', 'norse', 4, { hp: 920, atk: 46, range: 1.9, speed: 2.6, cd: 1.8, cost: { food: 240, gold: 160, favor: 28 }, pop: 5, bldMult: 3, r: 0.9, time: 42 },
  { h: 2.8, w: 1.0, col: 0x9ec7e6, flags: ['club', 'giant', 'horn'] }, 'Colossus of ice. Shatters buildings.');

// Summoned by god powers (not trainable)
UNITS['skeleton'] = {
  ...BASE.inf, id: 'skeleton', name: 'Skeleton', cls: 'inf', hp: 80, atk: 10, speed: 3.0, cost: {}, pop: 0, at: null, bldMult: 0.5,
  look: { h: 1.0, w: 0.32, col: 0xd8d6c4, flags: ['spear', 'skull'] }, desc: 'Summoned by Hades.',
};
UNITS['einherjar'] = {
  ...BASE.inf, id: 'einherjar', name: 'Einherjar', cls: 'inf', hp: 160, atk: 15, speed: 3.0, cost: {}, pop: 0, at: null, bldMult: 0.6,
  look: { h: 1.15, w: 0.4, col: 0xe8d28a, flags: ['axe', 'shield', 'horn'] }, desc: 'Fallen heroes called by Odin.',
};

// ---------------------------------------------------------------- buildings

export type BId = 'tc' | 'house' | 'storehouse' | 'farm' | 'barracks' | 'temple' | 'tower';
export interface BldDef {
  id: BId; size: number; hp: number; cost: Cost; time: number; age: number; pop?: number; drop?: boolean; farm?: boolean;
  atk?: number; range?: number; cd?: number; blurb: string;
}
export const BUILDINGS: Record<BId, BldDef> = {
  tc: { id: 'tc', size: 3, hp: 2000, cost: {}, time: 60, age: 1, pop: 15, drop: true, blurb: 'Trains villagers, advances your Age.' },
  house: { id: 'house', size: 2, hp: 450, cost: { wood: 40 }, time: 18, age: 1, pop: 8, blurb: '+8 population.' },
  storehouse: { id: 'storehouse', size: 2, hp: 600, cost: { wood: 60 }, time: 22, age: 1, drop: true, blurb: 'Drop-off for wood, gold and food.' },
  farm: { id: 'farm', size: 2, hp: 250, cost: { wood: 60 }, time: 14, age: 1, farm: true, blurb: 'Endless food (600 per farm).' },
  barracks: { id: 'barracks', size: 3, hp: 1300, cost: { wood: 120 }, time: 38, age: 1, blurb: 'Trains infantry, archers, cavalry.' },
  temple: { id: 'temple', size: 3, hp: 1100, cost: { wood: 140, gold: 80 }, time: 45, age: 2, blurb: 'Trains mythic units. Source of Favor.' },
  tower: { id: 'tower', size: 2, hp: 900, cost: { wood: 90, gold: 60 }, time: 32, age: 2, atk: 13, range: 9, cd: 1.5, blurb: 'Shoots nearby enemies.' },
};
export const BUILD_ORDER: BId[] = ['house', 'storehouse', 'farm', 'barracks', 'temple', 'tower'];
export const FARM_FOOD = 600;

export interface AgeUp { food: number; gold: number; favor: number; time: number }
export const AGE_UP: Record<number, AgeUp> = {
  2: { food: 350, gold: 100, favor: 0, time: 40 },
  3: { food: 650, gold: 320, favor: 20, time: 55 },
  4: { food: 950, gold: 650, favor: 50, time: 70 },
};

// ---------------------------------------------------------------- gods & civs

export interface PowerDef {
  id: string; name: string; desc: string; cost: number; cd: number; r: number;
  kind: 'zone' | 'heal' | 'summon' | 'convert' | 'hammer';
  life?: number; dps?: number; bldDps?: number; slow?: number; strikeEvery?: number; strikeDmg?: number; strikeR?: number;
  amount?: number; unit?: string; n?: number; stun?: number; dmg?: number; bldDmg?: number; color: number;
}
export interface MajorGod { id: string; name: string; civ: CivId; title: string; blurb: string; mods: Partial<Mods>; power: PowerDef }
export interface MinorGod { id: string; name: string; age: 2 | 3 | 4; blurb: string; mods?: Partial<Mods>; unlock?: string }

export interface Civ {
  id: CivId; name: string; tagline: string; favor: string; accent: string; color: number;
  names: Record<BId, string>; majors: string[]; minors: MinorGod[]; start: Cost;
}

export const MAJORS: Record<string, MajorGod> = {
  zeus: { id: 'zeus', name: 'Zeus', civ: 'greek', title: 'King of the Gods', blurb: '+10% attack, +10% favor. Calls the Lightning Storm.',
    mods: { atk: 1.1, favor: 1.1 },
    power: { id: 'lightning', name: 'Lightning Storm', desc: 'Hurls lightning bolts across an area for 6 seconds.', cost: 45, cd: 90, r: 7, kind: 'zone', life: 6, strikeEvery: 0.28, strikeDmg: 115, strikeR: 2.2, color: 0xbfe0ff } },
  poseidon: { id: 'poseidon', name: 'Poseidon', civ: 'greek', title: 'Lord of the Sea', blurb: '+10% unit health, +15% production speed. Shakes the earth.',
    mods: { hp: 1.1, build: 1.15 },
    power: { id: 'quake', name: 'Earthquake', desc: 'Cracks buildings and slows units in an area for 10 seconds.', cost: 50, cd: 100, r: 8, kind: 'zone', life: 10, dps: 5, bldDps: 38, slow: 0.6, color: 0xb08a5a } },
  hades: { id: 'hades', name: 'Hades', civ: 'greek', title: 'Lord of the Underworld', blurb: '+10% attack, +5% health. Raises the dead.',
    mods: { atk: 1.1, hp: 1.05 },
    power: { id: 'passage', name: 'Underworld Passage', desc: 'Summons 6 Skeleton warriors for 70 seconds.', cost: 40, cd: 80, r: 3, kind: 'summon', unit: 'skeleton', n: 6, life: 70, color: 0x8ab87a } },
  ra: { id: 'ra', name: 'Ra', civ: 'egypt', title: 'The Sun God', blurb: '+15% gather rate, +10% favor. Burns his enemies.',
    mods: { gather: 1.15, favor: 1.1 },
    power: { id: 'solar', name: 'Solar Wrath', desc: 'A column of sunfire scorches everything in the area for 8 seconds.', cost: 45, cd: 90, r: 6, kind: 'zone', life: 8, dps: 30, bldDps: 14, color: 0xffc83a } },
  isis: { id: 'isis', name: 'Isis', civ: 'egypt', title: 'Mother of Magic', blurb: '+10% unit health, +5% gather rate. Heals the faithful.',
    mods: { hp: 1.1, gather: 1.05 },
    power: { id: 'blessing', name: 'Blessing of Isis', desc: 'Instantly heals your units in the area.', cost: 35, cd: 60, r: 9, kind: 'heal', amount: 260, color: 0x7affc8 } },
  set: { id: 'set', name: 'Set', civ: 'egypt', title: 'Lord of Storms', blurb: '+10% attack, +5% speed. Summons the sandstorm.',
    mods: { atk: 1.1, speed: 1.05 },
    power: { id: 'sandstorm', name: 'Sandstorm', desc: 'Scours and slows enemy units in a wide area for 12 seconds.', cost: 40, cd: 90, r: 9, kind: 'zone', life: 12, dps: 11, slow: 0.5, color: 0xd9b46a } },
  odin: { id: 'odin', name: 'Odin', civ: 'norse', title: 'The Allfather', blurb: '+25% favor, +5% health. Calls the fallen heroes.',
    mods: { favor: 1.25, hp: 1.05 },
    power: { id: 'valhalla', name: 'Valhalla Calls', desc: 'Summons 5 Einherjar for 70 seconds.', cost: 45, cd: 80, r: 3, kind: 'summon', unit: 'einherjar', n: 5, life: 70, color: 0xffe08a } },
  thor: { id: 'thor', name: 'Thor', civ: 'norse', title: 'God of Thunder', blurb: '+10% attack, +10% health. Wields Mjolnir.',
    mods: { atk: 1.1, hp: 1.1 },
    power: { id: 'hammer', name: "Hammer of Thor", desc: 'A thunderous blow: heavy damage and a 5 second stun.', cost: 50, cd: 90, r: 4.5, kind: 'hammer', dmg: 230, bldDmg: 420, stun: 5, color: 0x9ad0ff } },
  loki: { id: 'loki', name: 'Loki', civ: 'norse', title: 'The Trickster', blurb: '+10% speed, +15% production speed. Turns foes against each other.',
    mods: { speed: 1.1, build: 1.15 },
    power: { id: 'mischief', name: 'Mischief', desc: 'Up to 3 enemy units in the area switch to your side permanently.', cost: 60, cd: 100, r: 6, kind: 'convert', n: 3, color: 0x6aff9a } },
};

export const CIVS: Record<CivId, Civ> = {
  greek: {
    id: 'greek', name: 'Greeks', tagline: 'Heroes and monsters of Olympus.', accent: '#6fb4ff', color: 0x6fb4ff,
    favor: 'Villagers pray at Temples to earn Favor.',
    names: { tc: 'Town Center', house: 'House', storehouse: 'Storehouse', farm: 'Farm', barracks: 'Barracks', temple: 'Temple', tower: 'Watch Tower' },
    majors: ['zeus', 'poseidon', 'hades'], start: { food: 200, wood: 200, gold: 100, favor: 0 },
    minors: [
      { id: 'athena', name: 'Athena', age: 2, blurb: '+10% unit health.', mods: { hp: 1.1 } },
      { id: 'hermes', name: 'Hermes', age: 2, blurb: '+10% speed, +5% gathering.', mods: { speed: 1.1, gather: 1.05 } },
      { id: 'ares', name: 'Ares', age: 3, blurb: 'Unlocks Minotaur. +5% attack.', mods: { atk: 1.05 }, unlock: 'minotaur' },
      { id: 'artemis', name: 'Artemis', age: 3, blurb: 'Unlocks Centaur. +5% speed.', mods: { speed: 1.05 }, unlock: 'centaur' },
      { id: 'hephaestus', name: 'Hephaestus', age: 4, blurb: 'Unlocks Cyclops. +5% health.', mods: { hp: 1.05 }, unlock: 'cyclops' },
      { id: 'hecate', name: 'Hecate', age: 4, blurb: 'Unlocks Medusa. +10% favor.', mods: { favor: 1.1 }, unlock: 'medusa' },
    ],
  },
  egypt: {
    id: 'egypt', name: 'Egyptians', tagline: 'Gold, sand and the gods of the Nile.', accent: '#f0c050', color: 0xf0c050,
    favor: 'Temples generate Favor on their own.',
    names: { tc: 'Town Center', house: 'Mud House', storehouse: 'Granary', farm: 'Farm', barracks: 'Barracks', temple: 'Obelisk Temple', tower: 'Migdol' },
    majors: ['ra', 'isis', 'set'], start: { food: 200, wood: 200, gold: 100, favor: 0 },
    minors: [
      { id: 'bast', name: 'Bast', age: 2, blurb: '+10% attack.', mods: { atk: 1.1 } },
      { id: 'thoth', name: 'Thoth', age: 2, blurb: '+10% gathering, +10% favor.', mods: { gather: 1.1, favor: 1.1 } },
      { id: 'anubis', name: 'Anubis', age: 3, blurb: 'Unlocks Anubite. +5% attack.', mods: { atk: 1.05 }, unlock: 'anubite' },
      { id: 'sekhmet', name: 'Sekhmet', age: 3, blurb: 'Unlocks Sphinx. +5% health.', mods: { hp: 1.05 }, unlock: 'sphinx' },
      { id: 'horus', name: 'Horus', age: 4, blurb: 'Unlocks Phoenix. +5% speed.', mods: { speed: 1.05 }, unlock: 'phoenix' },
      { id: 'sobek', name: 'Sobek', age: 4, blurb: 'Unlocks Great Crocodile. +5% health.', mods: { hp: 1.05 }, unlock: 'croc' },
    ],
  },
  norse: {
    id: 'norse', name: 'Norse', tagline: 'Raiders of the frozen north.', accent: '#e8605a', color: 0xe8605a,
    favor: 'Fighting earns Favor: every kill grants Favor.',
    names: { tc: 'Hill Fort', house: 'Longhouse', storehouse: 'Storehouse', farm: 'Farm', barracks: 'Great Hall', temple: 'Shrine of Valhalla', tower: 'Watch Tower' },
    majors: ['odin', 'thor', 'loki'], start: { food: 200, wood: 200, gold: 100, favor: 8 },
    minors: [
      { id: 'freyr', name: 'Freyr', age: 2, blurb: '+10% health, +5% gathering.', mods: { hp: 1.1, gather: 1.05 } },
      { id: 'heimdall', name: 'Heimdall', age: 2, blurb: '+5% attack, +5% speed.', mods: { atk: 1.05, speed: 1.05 } },
      { id: 'njord', name: 'Njord', age: 3, blurb: 'Unlocks Troll. +5% health.', mods: { hp: 1.05 }, unlock: 'troll' },
      { id: 'skadi', name: 'Skadi', age: 3, blurb: 'Unlocks Valkyrie. +5% speed.', mods: { speed: 1.05 }, unlock: 'valkyrie' },
      { id: 'tyr', name: 'Tyr', age: 4, blurb: 'Unlocks Fenrir Wolf. +5% attack.', mods: { atk: 1.05 }, unlock: 'fenrir' },
      { id: 'hel', name: 'Hel', age: 4, blurb: 'Unlocks Frost Giant. +5% health.', mods: { hp: 1.05 }, unlock: 'frost_giant' },
    ],
  },
};

/** Damage multiplier by unit class (rock-paper-scissors). */
export function classMult(a: UClass, d: UClass): number {
  if (a === 'inf' && d === 'cav') return 1.6;
  if (a === 'cav' && d === 'rng') return 1.6;
  if (a === 'rng' && d === 'inf') return 1.5;
  if (a === 'rng' && d === 'myth') return 0.7;
  if (a === 'myth' && d === 'vil') return 1.2;
  return 1;
}

/** Unit ids a civ can ever train from a given building. */
export function civUnits(civ: CivId, at: 'tc' | 'barracks' | 'temple'): UnitDef[] {
  return Object.values(UNITS).filter((u) => u.civ === civ && u.at === at);
}
