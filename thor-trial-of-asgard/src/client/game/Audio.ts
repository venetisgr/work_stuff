export type Sfx = 'zap' | 'impact' | 'kill' | 'spark' | 'ultimate' | 'thunder' | 'hit' | 'dodge' | 'combo' | 'victory';

/**
 * Procedural WebAudio sound with a drop-in slot for real assets:
 *   audio.loadSample('thunder', '/audio/thunder.mp3')   // overrides the synth voice
 *   audio.loadMusic('/audio/battle.mp3')                // replaces the synth drums
 * Nothing here is required — if audio is blocked or missing, the game runs silently.
 */
export class GameAudio {
  muted = false;
  private ctx: AudioContext | null = null;
  private master: GainNode | null = null;
  private samples = new Map<Sfx, AudioBuffer>();
  private noise: AudioBuffer | null = null;
  private musicBuf: AudioBuffer | null = null;
  private musicSrc: AudioBufferSourceNode | null = null;
  private drumTimer = 0;
  private intensity = 0;
  private musicOn = false;

  constructor() {
    try {
      this.muted = localStorage.getItem('thor.muted') === '1';
    } catch {
      /* ignore */
    }
  }

  /** Must be called from a user gesture (first tap/key). Safe to call repeatedly. */
  unlock(): void {
    if (!this.ctx) {
      try {
        const AC = window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
        this.ctx = new AC();
        this.master = this.ctx.createGain();
        this.master.gain.value = this.muted ? 0 : 0.7;
        this.master.connect(this.ctx.destination);
        const len = this.ctx.sampleRate * 2;
        this.noise = this.ctx.createBuffer(1, len, this.ctx.sampleRate);
        const d = this.noise.getChannelData(0);
        for (let i = 0; i < len; i++) d[i] = Math.random() * 2 - 1;
      } catch {
        this.ctx = null;
      }
    }
    void this.ctx?.resume();
  }

  setMuted(m: boolean): void {
    this.muted = m;
    try {
      localStorage.setItem('thor.muted', m ? '1' : '0');
    } catch {
      /* ignore */
    }
    if (this.master && this.ctx) this.master.gain.setTargetAtTime(m ? 0 : 0.7, this.ctx.currentTime, 0.03);
  }

  async loadSample(name: Sfx, url: string): Promise<void> {
    try {
      this.unlockContextOnly();
      const buf = await (await fetch(url)).arrayBuffer();
      this.samples.set(name, await this.ctx!.decodeAudioData(buf));
    } catch {
      /* keep synth fallback */
    }
  }

  async loadMusic(url: string): Promise<void> {
    try {
      this.unlockContextOnly();
      this.musicBuf = await this.ctx!.decodeAudioData(await (await fetch(url)).arrayBuffer());
    } catch {
      /* keep synth music */
    }
  }

  private unlockContextOnly(): void {
    if (!this.ctx) this.unlock();
  }

  startMusic(): void {
    this.musicOn = true;
    this.intensity = 0;
    if (this.ctx && this.master && this.musicBuf && !this.musicSrc) {
      this.musicSrc = this.ctx.createBufferSource();
      this.musicSrc.buffer = this.musicBuf;
      this.musicSrc.loop = true;
      const g = this.ctx.createGain();
      g.gain.value = 0.5;
      this.musicSrc.connect(g).connect(this.master);
      this.musicSrc.start();
    }
  }

  stopMusic(): void {
    this.musicOn = false;
    try {
      this.musicSrc?.stop();
    } catch {
      /* ignore */
    }
    this.musicSrc = null;
  }

  /** 0..1 — drives the synth drum tempo. */
  setIntensity(v: number): void {
    this.intensity = v;
  }

  update(dt: number): void {
    if (!this.musicOn || this.musicBuf || !this.ctx || this.muted) return;
    this.drumTimer -= dt;
    if (this.drumTimer <= 0) {
      this.drumTimer = 0.62 - this.intensity * 0.3;
      this.tone(58 + this.intensity * 12, 0.22, 'sine', 0.5, 0.08, 38);
      if (this.intensity > 0.45) this.noiseBurst(0.07, 0.18, 1800, 'highpass');
    }
  }

  play(name: Sfx, power = 1): void {
    const ctx = this.ctx;
    if (!ctx || !this.master || this.muted) return;
    const sample = this.samples.get(name);
    if (sample) {
      const src = ctx.createBufferSource();
      src.buffer = sample;
      const g = ctx.createGain();
      g.gain.value = Math.min(1, 0.5 + power * 0.3);
      src.connect(g).connect(this.master);
      src.start();
      return;
    }
    switch (name) {
      case 'zap':
        this.noiseBurst(0.18, 0.35, 3000, 'bandpass');
        this.tone(900, 0.12, 'sawtooth', 0.12, 0, 120);
        break;
      case 'impact':
        this.noiseBurst(0.3, 0.45 * power, 900, 'lowpass');
        this.tone(95, 0.25, 'sine', 0.5, 0, 40);
        break;
      case 'kill':
        this.tone(420 + Math.random() * 120, 0.14, 'triangle', 0.18, 0, 90);
        break;
      case 'spark':
        this.tone(880, 0.1, 'sine', 0.2, 0, 1320);
        this.tone(1320, 0.14, 'sine', 0.14, 0.06);
        break;
      case 'combo':
        this.tone(520 * power, 0.16, 'square', 0.1, 0, 780 * power);
        break;
      case 'hit':
        this.noiseBurst(0.25, 0.5, 500, 'lowpass');
        this.tone(140, 0.3, 'sawtooth', 0.35, 0, 50);
        break;
      case 'dodge':
        this.noiseBurst(0.2, 0.22, 1400, 'bandpass');
        break;
      case 'thunder':
        this.noiseBurst(1.6, 0.55 * power, 260, 'lowpass', 0.06);
        break;
      case 'ultimate':
        this.noiseBurst(2.2, 0.6, 400, 'lowpass');
        this.tone(55, 2, 'sawtooth', 0.4, 0, 220);
        this.tone(330, 1.6, 'triangle', 0.15, 0, 990);
        break;
      case 'victory':
        [523, 659, 784, 1047].forEach((f, i) => this.tone(f, 0.5, 'triangle', 0.22, i * 0.12));
        break;
    }
  }

  private tone(freq: number, dur: number, type: OscillatorType, vol: number, delay = 0, endFreq?: number): void {
    const ctx = this.ctx!;
    const t = ctx.currentTime + delay;
    const osc = ctx.createOscillator();
    const g = ctx.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, t);
    if (endFreq) osc.frequency.exponentialRampToValueAtTime(endFreq, t + dur);
    g.gain.setValueAtTime(vol, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + dur);
    osc.connect(g).connect(this.master!);
    osc.start(t);
    osc.stop(t + dur + 0.05);
  }

  private noiseBurst(dur: number, vol: number, freq: number, type: BiquadFilterType, delay = 0): void {
    const ctx = this.ctx!;
    const t = ctx.currentTime + delay;
    const src = ctx.createBufferSource();
    src.buffer = this.noise;
    const f = ctx.createBiquadFilter();
    f.type = type;
    f.frequency.value = freq;
    const g = ctx.createGain();
    g.gain.setValueAtTime(vol, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + dur);
    src.connect(f).connect(g).connect(this.master!);
    src.start(t, Math.random());
    src.stop(t + dur + 0.05);
  }
}
