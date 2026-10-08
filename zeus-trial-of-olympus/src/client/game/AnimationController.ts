import * as THREE from 'three';

/** Gameplay roles we need an animation for. */
export type Role = 'idle' | 'ready' | 'walk' | 'attack' | 'ultimate' | 'dodge' | 'hit' | 'victory' | 'defeat';

/**
 * Role → clip-name patterns, tried in order against the clip name with the
 * "mixamo_auto_" prefix stripped. This is derived from the clips that actually
 * ship in zeus.glb (see tools/inspect-glb.mjs), with generic fallbacks so a
 * re-exported model with different names still gets a sensible mapping.
 */
const RULES: Record<Role, RegExp[]> = {
  idle: [/^standing_idle$/, /^standing_idle/, /idle/, /stand/],
  ready: [/^spell_simple_idle$/, /spell.*idle/, /idle_0?2/],
  walk: [/^walk$/, /^walk_formal$/, /walk(?!.*stealth)/, /run|jog|sprint/, /walk/],
  attack: [/^spell_cast$/, /spell|cast|magic|zap|lightning/, /attack(?!.*ground)/, /punch|strike|slash/],
  ultimate: [/spell_cast_epic/, /epic|ground_pound|summon|special|power|roar/, /^spell_cast$/, /spell|cast/],
  dodge: [/backflip/, /evade|dodge|roll|dash/, /flip|jump/],
  hit: [/block/, /hit|hurt|react|flinch|damage|impact/, /look_away/],
  victory: [/victory_fist_pump/, /victory|win|cheer|celebrat|triumph/, /yes|dance/],
  defeat: [/death_a|death_b/, /death|die|dead|fall/],
};
const ROLE_ORDER: Role[] = ['idle', 'walk', 'attack', 'ultimate', 'dodge', 'hit', 'victory', 'defeat', 'ready'];

export interface PlayOpts {
  /** Seconds the clip should take (the clip is sped up/down to fit). */
  duration?: number;
  fade?: number;
  /** Stay on the last frame instead of returning to locomotion. */
  hold?: boolean;
  /** Start offset in seconds into the clip. */
  startAt?: number;
}

export interface Animator {
  hasRole(role: Role): boolean;
  setLocomotion(speed01: number, ready: boolean): void;
  playOnce(role: Role, opts?: PlayOpts): number;
  update(dt: number): void;
  readonly busy: boolean;
}

export class AnimationController implements Animator {
  readonly mixer: THREE.AnimationMixer;
  readonly mapping = new Map<Role, THREE.AnimationClip>();
  private actions = new Map<Role, THREE.AnimationAction>();
  private current: THREE.AnimationAction | null = null;
  private currentRole: Role | null = null;
  private oneShotLeft = 0;
  private holding = false;
  private speed01 = 0;
  private ready = false;

  constructor(root: THREE.Object3D, clips: THREE.AnimationClip[]) {
    this.mixer = new THREE.AnimationMixer(root);
    const prepared = clips.map((c) => AnimationController.makeInPlace(c));
    this.resolve(prepared);
  }

  /** Mixamo exports often drift the hips forward; lock hip X/Z so the game owns movement. */
  private static makeInPlace(src: THREE.AnimationClip): THREE.AnimationClip {
    const clip = src.clone();
    for (const track of clip.tracks) {
      if (/hips\.position$/i.test(track.name) && track instanceof THREE.VectorKeyframeTrack) {
        const v = track.values;
        const x0 = v[0];
        const z0 = v[2];
        for (let i = 0; i < v.length; i += 3) {
          v[i] = x0;
          v[i + 2] = z0;
        }
      }
    }
    return clip;
  }

  private static key(name: string): string {
    return name.toLowerCase().replace(/^mixamo_auto_/, '').replace(/^mixamo\.com$/, 'idle');
  }

  private resolve(clips: THREE.AnimationClip[]): void {
    const used = new Set<THREE.AnimationClip>();
    const pass = (allowReuse: boolean) => {
      for (const role of ROLE_ORDER) {
        if (this.mapping.has(role)) continue;
        for (const re of RULES[role]) {
          const hit = clips.find((c) => re.test(AnimationController.key(c.name)) && (allowReuse || !used.has(c)));
          if (hit) {
            this.mapping.set(role, hit);
            used.add(hit);
            break;
          }
        }
      }
    };
    pass(false);
    pass(true);
    // Last resorts so every locomotion role has *something* to play.
    if (!this.mapping.has('idle') && clips[0]) this.mapping.set('idle', clips[0]);
    if (!this.mapping.has('walk') && this.mapping.has('idle')) this.mapping.set('walk', this.mapping.get('idle')!);
    if (!this.mapping.has('ready') && this.mapping.has('idle')) this.mapping.set('ready', this.mapping.get('idle')!);
    if (!this.mapping.has('attack') && this.mapping.has('ultimate')) this.mapping.set('attack', this.mapping.get('ultimate')!);
  }

  describe(): Record<string, string> {
    const out: Record<string, string> = {};
    for (const [r, c] of this.mapping) out[r] = c.name;
    return out;
  }

  hasRole(role: Role): boolean {
    return this.mapping.has(role);
  }

  get busy(): boolean {
    return this.oneShotLeft > 0 || this.holding;
  }

  private action(role: Role): THREE.AnimationAction | null {
    let a = this.actions.get(role);
    if (!a) {
      const clip = this.mapping.get(role);
      if (!clip) return null;
      a = this.mixer.clipAction(clip);
      this.actions.set(role, a);
    }
    return a;
  }

  private fadeTo(role: Role, action: THREE.AnimationAction, fade: number): void {
    if (this.current === action) return;
    action.enabled = true;
    action.reset();
    action.setEffectiveWeight(1);
    action.fadeIn(fade);
    action.play();
    if (this.current) this.current.fadeOut(fade);
    this.current = action;
    this.currentRole = role;
  }

  /** Called every frame with 0..1 movement amount; ignored while a one-shot plays. */
  setLocomotion(speed01: number, ready: boolean): void {
    this.speed01 = speed01;
    this.ready = ready;
  }

  /** Plays a role once. Returns the real duration in seconds (0 if no clip). */
  playOnce(role: Role, opts: PlayOpts = {}): number {
    const a = this.action(role);
    const clip = this.mapping.get(role);
    if (!a || !clip) return 0;
    const fade = opts.fade ?? 0.1;
    const start = opts.startAt ?? 0;
    const avail = Math.max(0.05, clip.duration - start);
    const dur = opts.duration ?? avail;
    a.setLoop(THREE.LoopOnce, 1);
    a.clampWhenFinished = true;
    a.timeScale = avail / dur;
    if (this.current !== a) {
      this.fadeTo(role, a, fade);
    } else {
      a.reset().play();
    }
    a.time = start;
    this.oneShotLeft = dur;
    this.holding = !!opts.hold;
    return dur;
  }

  update(dt: number): void {
    if (this.oneShotLeft > 0) {
      this.oneShotLeft -= dt;
      if (this.oneShotLeft <= 0) this.oneShotLeft = 0;
    }
    if (!this.holding && this.oneShotLeft <= 0) {
      const moving = this.speed01 > 0.08;
      const role: Role = moving ? 'walk' : this.ready ? 'ready' : 'idle';
      const a = this.action(role);
      if (a) {
        a.setLoop(THREE.LoopRepeat, Infinity);
        a.clampWhenFinished = false;
        a.timeScale = moving ? 0.9 + this.speed01 * 0.75 : 1;
        if (this.currentRole !== role) this.fadeTo(role, a, 0.16);
      }
    }
    this.mixer.update(dt);
  }

  dispose(): void {
    this.mixer.stopAllAction();
    this.mixer.uncacheRoot(this.mixer.getRoot());
  }
}

/**
 * Used only if zeus.glb cannot be loaded, so the game remains playable.
 * Same interface; "animation" is simple procedural squash/lean on the stand-in body.
 */
export class ProceduralAnimator implements Animator {
  private t = 0;
  private speed = 0;
  private shot: { role: Role; left: number; total: number } | null = null;
  constructor(private body: THREE.Object3D) {}
  hasRole(): boolean {
    return true;
  }
  get busy(): boolean {
    return !!this.shot;
  }
  setLocomotion(speed01: number): void {
    this.speed = speed01;
  }
  playOnce(role: Role, opts: PlayOpts = {}): number {
    const total = opts.duration ?? (role === 'ultimate' ? 2.4 : role === 'victory' ? 2 : 0.4);
    this.shot = { role, left: total, total };
    return total;
  }
  update(dt: number): void {
    this.t += dt;
    const b = this.body;
    let bob = Math.sin(this.t * (this.speed > 0.1 ? 14 : 2.5)) * (this.speed > 0.1 ? 0.07 : 0.025);
    let lean = this.speed * 0.2;
    let scaleY = 1;
    if (this.shot) {
      this.shot.left -= dt;
      const k = 1 - Math.max(0, this.shot.left) / this.shot.total;
      if (this.shot.role === 'attack' || this.shot.role === 'ultimate' || this.shot.role === 'victory') {
        bob += Math.sin(k * Math.PI) * 0.3;
        lean = -0.2;
      } else if (this.shot.role === 'dodge') {
        b.rotation.x = -k * Math.PI * 2;
      } else if (this.shot.role === 'hit') {
        lean = -0.5 * (1 - k);
      } else if (this.shot.role === 'defeat') {
        lean = -k * 1.4;
        scaleY = 1 - k * 0.1;
      }
      if (this.shot.left <= 0 && this.shot.role !== 'defeat') {
        this.shot = null;
        b.rotation.x = 0;
      }
    }
    if (!this.shot || this.shot.role !== 'dodge') b.rotation.x = lean;
    b.position.y = bob;
    b.scale.y = scaleY;
  }
}
