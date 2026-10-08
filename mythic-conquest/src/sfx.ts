// Tiny synthesized sound effects (no assets).
let ctx: AudioContext | null = null;
export let muted = false;
export const toggleMute = () => (muted = !muted);

function ac(): AudioContext | null {
  if (muted) return null;
  try { ctx ??= new AudioContext(); if (ctx.state === 'suspended') void ctx.resume(); return ctx; } catch { return null; }
}
function tone(freq: number, dur: number, type: OscillatorType = 'sine', vol = 0.08, slide = 0, delay = 0) {
  const c = ac(); if (!c) return;
  const t = c.currentTime + delay, o = c.createOscillator(), g = c.createGain();
  o.type = type; o.frequency.setValueAtTime(freq, t);
  if (slide) o.frequency.exponentialRampToValueAtTime(Math.max(20, freq + slide), t + dur);
  g.gain.setValueAtTime(vol, t); g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
  o.connect(g).connect(c.destination); o.start(t); o.stop(t + dur + 0.02);
}
function noise(dur: number, vol = 0.1, freq = 800) {
  const c = ac(); if (!c) return;
  const n = Math.floor(c.sampleRate * dur), buf = c.createBuffer(1, n, c.sampleRate), d = buf.getChannelData(0);
  for (let i = 0; i < n; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / n);
  const s = c.createBufferSource(), f = c.createBiquadFilter(), g = c.createGain();
  s.buffer = buf; f.type = 'lowpass'; f.frequency.value = freq; g.gain.value = vol;
  s.connect(f).connect(g).connect(c.destination); s.start();
}
let lastHit = 0;
export const sfx = {
  click: () => tone(660, 0.05, 'square', 0.03),
  order: () => tone(440, 0.07, 'triangle', 0.05, 160),
  error: () => tone(160, 0.18, 'sawtooth', 0.05, -60),
  hit: () => { const t = performance.now(); if (t - lastHit > 70) { lastHit = t; noise(0.06, 0.05, 1500); } },
  built: () => { tone(523, 0.12, 'triangle', 0.06); tone(784, 0.18, 'triangle', 0.06, 0, 0.1); },
  bolt: () => noise(0.35, 0.18, 2500),
  power: () => { noise(0.8, 0.16, 500); tone(110, 0.8, 'sawtooth', 0.07, 60); },
  age: () => { [392, 523, 659, 784].forEach((f, i) => tone(f, 0.45, 'triangle', 0.07, 0, i * 0.14)); },
  win: () => { [523, 659, 784, 1046].forEach((f, i) => tone(f, 0.5, 'triangle', 0.08, 0, i * 0.16)); },
  lose: () => { [392, 330, 262, 196].forEach((f, i) => tone(f, 0.55, 'sawtooth', 0.05, 0, i * 0.2)); },
  collapse: () => noise(0.7, 0.2, 400),
};
