import * as THREE from 'three';
import type { DeityDef, DeityId } from './config';

/** Gameplay roles we need an animation for. */
export type Role = 'idle' | 'ready' | 'walk' | 'attack' | 'ultimate' | 'dodge' | 'hit' | 'victory' | 'defeat';

export interface PlayOpts {
  /** Seconds the pose should take. */
  duration?: number;
  fade?: number;
  /** Stay on the last pose instead of returning to locomotion. */
  hold?: boolean;
}

export interface Animator {
  setLocomotion(speed01: number, ready: boolean): void;
  playOnce(role: Role, opts?: PlayOpts): number;
  update(dt: number): void;
  readonly busy: boolean;
}

/** Everything the animator drives. Local space: the pivot sits at the hips, 1.1 above the floor. */
interface Rig {
  pivot: THREE.Group;
  torso: THREE.Group;
  head: THREE.Group;
  armR: THREE.Group;
  armL: THREE.Group;
  legR: THREE.Group;
  legL: THREE.Group;
  wings: THREE.Object3D[];
  /** Glowing hand marker (child of the right arm) used as the beam origin. */
  hand: THREE.Object3D;
}

export interface DeityModel {
  /** Feet at y=0, facing +Z. */
  root: THREE.Group;
  hand: THREE.Object3D;
  animator: RigAnimator;
  dispose(): void;
}

const HIP_Y = 1.1;

function std(color: number, extra: Partial<THREE.MeshStandardMaterialParameters> = {}): THREE.MeshStandardMaterial {
  return new THREE.MeshStandardMaterial({ color, roughness: 0.65, metalness: 0.05, ...extra });
}

/** Builds a deity from primitives: a human-shaped body topped with an animal head and a signature crown. */
export function buildDeity(def: DeityDef): DeityModel {
  const root = new THREE.Group();
  const pivot = new THREE.Group();
  pivot.position.y = HIP_Y;
  root.add(pivot);

  const mats: THREE.Material[] = [];
  const mat = (color: number, extra: Partial<THREE.MeshStandardMaterialParameters> = {}) => {
    const m = std(color, extra);
    mats.push(m);
    return m;
  };
  const skin = mat(def.skin, { roughness: 0.8 });
  const cloth = mat(def.cloth, { roughness: 0.9, side: THREE.DoubleSide });
  const gold = mat(def.trim, { metalness: 0.8, roughness: 0.3, emissive: 0x3a2400, emissiveIntensity: 0.6 });
  const dark = mat(0x15110e, { roughness: 0.5 });
  const eyeMat = new THREE.MeshBasicMaterial({ color: 0xfff0b0 });
  mats.push(eyeMat);

  const mesh = (geo: THREE.BufferGeometry, m: THREE.Material, x = 0, y = 0, z = 0): THREE.Mesh => {
    const o = new THREE.Mesh(geo, m);
    o.position.set(x, y, z);
    o.castShadow = true;
    return o;
  };

  // ---- legs (pivot at the hip) ----
  const makeLeg = (side: number): THREE.Group => {
    const g = new THREE.Group();
    g.position.set(side * 0.2, 0, 0);
    g.add(mesh(new THREE.CylinderGeometry(0.13, 0.1, 1.0, 8), skin, 0, -0.5, 0));
    g.add(mesh(new THREE.BoxGeometry(0.2, 0.1, 0.34), gold, 0, -1.04, 0.07)); // sandal
    return g;
  };
  const legR = makeLeg(1);
  const legL = makeLeg(-1);
  pivot.add(legR, legL);

  // ---- torso ----
  const torso = new THREE.Group();
  pivot.add(torso);
  torso.add(mesh(new THREE.CylinderGeometry(0.4, 0.3, 0.85, 10), skin, 0, 0.42, 0)); // chest
  torso.add(mesh(new THREE.CylinderGeometry(0.34, 0.5, 0.78, 12, 1, true), cloth, 0, -0.2, 0)); // shendyt kilt
  const belt = mesh(new THREE.CylinderGeometry(0.34, 0.34, 0.1, 12), gold, 0, 0.1, 0);
  torso.add(belt);
  const collar = mesh(new THREE.TorusGeometry(0.34, 0.1, 6, 16), gold, 0, 0.82, 0.02); // usekh broad collar
  collar.rotation.x = Math.PI / 2;
  collar.scale.set(1.15, 1, 0.8);
  torso.add(collar);

  // ---- arms (pivot at the shoulder) ----
  const makeArm = (side: number): THREE.Group => {
    const g = new THREE.Group();
    g.position.set(side * 0.5, 0.78, 0);
    g.add(mesh(new THREE.CylinderGeometry(0.085, 0.07, 0.8, 8), skin, 0, -0.4, 0));
    g.add(mesh(new THREE.CylinderGeometry(0.1, 0.1, 0.1, 8), gold, 0, -0.72, 0)); // bracer
    g.add(mesh(new THREE.SphereGeometry(0.09, 8, 6), skin, 0, -0.86, 0)); // hand
    return g;
  };
  const armR = makeArm(1);
  const armL = makeArm(-1);
  torso.add(armR, armL);
  const hand = new THREE.Object3D();
  hand.position.set(0, -1.0, 0.04);
  armR.add(hand);

  // ---- head & crown, per deity ----
  const head = new THREE.Group();
  head.position.y = 1.02;
  torso.add(head);
  torso.add(mesh(new THREE.CylinderGeometry(0.1, 0.12, 0.2, 8), skin, 0, 0.9, 0)); // neck
  const wings: THREE.Object3D[] = [];
  const id: DeityId = def.id;

  const lineEye = (x: number): void => {
    head.add(mesh(new THREE.SphereGeometry(0.045, 8, 6), eyeMat, x, 0.06, 0.2));
    head.add(mesh(new THREE.BoxGeometry(0.05, 0.03, 0.2), dark, x * 1.1, 0.0, 0.2)); // kohl line
  };

  if (id === 'anubis') {
    head.add(mesh(new THREE.SphereGeometry(0.25, 12, 10), skin, 0, 0, 0));
    const snout = mesh(new THREE.BoxGeometry(0.17, 0.14, 0.46), skin, 0, -0.06, 0.3);
    snout.rotation.x = 0.12;
    head.add(snout);
    head.add(mesh(new THREE.SphereGeometry(0.06, 8, 6), dark, 0, -0.01, 0.53)); // nose
    for (const s of [-1, 1]) {
      const ear = mesh(new THREE.ConeGeometry(0.09, 0.5, 4), skin, s * 0.14, 0.38, -0.02);
      ear.rotation.z = -s * 0.12;
      head.add(ear);
      const earIn = mesh(new THREE.ConeGeometry(0.045, 0.34, 4), gold, s * 0.14, 0.34, 0.03);
      earIn.rotation.z = -s * 0.12;
      head.add(earIn);
      lineEye(s * 0.1);
    }
    // nemes-style striped cloth
    const cloak = mesh(new THREE.CylinderGeometry(0.27, 0.3, 0.22, 10, 1, true), mat(def.trim, { side: THREE.DoubleSide, metalness: 0.6, roughness: 0.4 }), 0, -0.2, -0.04);
    head.add(cloak);
  } else {
    // falcon head (Ra / Horus)
    head.add(mesh(new THREE.SphereGeometry(0.26, 12, 10), skin, 0, 0, 0));
    const beak = mesh(new THREE.ConeGeometry(0.1, 0.34, 6), mat(0xe8b84a, { metalness: 0.4 }), 0, -0.05, 0.3);
    beak.rotation.x = Math.PI / 2 + 0.25;
    head.add(beak);
    for (const s of [-1, 1]) lineEye(s * 0.12);
    // wadjet eye tear-line
    for (const s of [-1, 1]) {
      const tear = mesh(new THREE.BoxGeometry(0.03, 0.2, 0.03), dark, s * 0.13, -0.16, 0.18);
      tear.rotation.z = s * 0.25;
      head.add(tear);
    }
    const cloak = mesh(new THREE.CylinderGeometry(0.29, 0.34, 0.26, 10, 1, true), mat(def.trim, { side: THREE.DoubleSide, metalness: 0.6, roughness: 0.4 }), 0, -0.14, -0.06);
    head.add(cloak);

    if (id === 'ra') {
      // sun disc with uraeus
      const discMat = mat(0xff8a1a, { emissive: 0xff5a00, emissiveIntensity: 1.1, roughness: 0.4 });
      const disc = mesh(new THREE.CylinderGeometry(0.36, 0.36, 0.08, 20), discMat, 0, 0.62, 0);
      disc.rotation.x = Math.PI / 2;
      const ring = mesh(new THREE.TorusGeometry(0.36, 0.045, 8, 24), gold, 0, 0.62, 0.02);
      head.add(disc, ring);
      const cobra = mesh(new THREE.ConeGeometry(0.06, 0.22, 6), mat(0xffd36a, { emissive: 0x553300 }), 0, 0.36, 0.2);
      head.add(cobra);
    } else {
      // pschent: white crown inside a red crown
      const red = mesh(new THREE.CylinderGeometry(0.2, 0.25, 0.28, 10), mat(0xc0392b), 0, 0.3, 0);
      const white = mesh(new THREE.CylinderGeometry(0.08, 0.17, 0.62, 10), mat(0xf4f0e6), 0, 0.68, 0);
      const knob = mesh(new THREE.SphereGeometry(0.075, 8, 6), mat(0xf4f0e6), 0, 1.0, 0);
      head.add(red, white, knob);
    }
  }

  if (id === 'horus') {
    // folded wings on the back, flapped by the animator
    for (const s of [-1, 1]) {
      const wing = new THREE.Group();
      wing.position.set(s * 0.22, 0.65, -0.3);
      for (let i = 0; i < 4; i++) {
        const f = mesh(new THREE.BoxGeometry(0.1, 0.9 - i * 0.12, 0.04), mat(i % 2 ? def.trim : 0xf1ede2, { roughness: 0.5 }), s * (0.08 + i * 0.09), 0.1 - i * 0.04, 0);
        f.rotation.z = -s * (0.15 + i * 0.18);
        f.position.y = 0.28 - i * 0.07;
        wing.add(f);
      }
      wing.rotation.y = s * 0.35;
      torso.add(wing);
      wings.push(wing);
    }
  }

  root.traverse((o) => ((o as THREE.Mesh).isMesh ? ((o as THREE.Mesh).castShadow = true) : undefined));

  const rig: Rig = { pivot, torso, head, armR, armL, legR, legL, wings, hand };
  const animator = new RigAnimator(rig);
  return {
    root,
    hand,
    animator,
    dispose() {
      root.traverse((o) => (o as THREE.Mesh).geometry?.dispose());
      mats.forEach((m) => m.dispose());
    },
  };
}

interface Pose {
  armRx: number;
  armRz: number;
  armLx: number;
  armLz: number;
  legRx: number;
  legLx: number;
  torsoX: number;
  torsoY: number;
  headX: number;
  y: number;
  /** Whole-body pitch about the hips (flip / fall). */
  pitch: number;
}

const newPose = (): Pose => ({ armRx: 0, armRz: 0, armLx: 0, armLz: 0, legRx: 0, legLx: 0, torsoX: 0, torsoY: 0, headX: 0, y: 0, pitch: 0 });
const KEYS = Object.keys(newPose()) as Array<keyof Pose>;
const smooth = (x: number) => x * x * (3 - 2 * x);

/**
 * Pose-driven procedural animation. Every frame a target pose is computed from the
 * current role and the rig eases towards it; flips and falls write the pose directly.
 */
export class RigAnimator implements Animator {
  private cur = newPose();
  private tgt = newPose();
  private t = 0;
  private speed = 0;
  private ready = false;
  private shot: { role: Role; t: number; dur: number; hold: boolean } | null = null;
  private direct = false;

  constructor(private rig: Rig) {}

  get busy(): boolean {
    return !!this.shot;
  }

  setLocomotion(speed01: number, ready: boolean): void {
    this.speed = speed01;
    this.ready = ready;
  }

  playOnce(role: Role, opts: PlayOpts = {}): number {
    const dur = opts.duration ?? (role === 'ultimate' ? 2.4 : role === 'victory' ? 2 : role === 'dodge' ? 0.45 : 0.4);
    this.shot = { role, t: 0, dur: Math.max(0.05, dur), hold: !!opts.hold };
    return dur;
  }

  update(dt: number): void {
    this.t += dt;
    const p = this.tgt;
    Object.assign(p, newPose());
    this.direct = false;
    let rate = 16;

    if (this.shot) {
      this.shot.t += dt;
      if (this.shot.t >= this.shot.dur && !this.shot.hold) this.shot = null;
    }

    if (this.shot) {
      const s = this.shot;
      const k = Math.min(1, s.t / s.dur);
      switch (s.role) {
        case 'attack': {
          // wind up overhead, then thrust forward
          const wind = smooth(Math.min(1, k / 0.32));
          const thrust = smooth(Math.min(1, Math.max(0, (k - 0.32) / 0.2)));
          p.armRx = -0.2 - 2.4 * wind + 1.2 * thrust;
          p.armRz = 0.25 * (1 - thrust);
          p.armLx = -0.9 * thrust - 0.4 * wind;
          p.torsoX = -0.15 * wind + 0.4 * thrust;
          p.headX = 0.2 * thrust;
          p.legRx = -0.35;
          p.legLx = 0.4;
          rate = 30;
          break;
        }
        case 'ultimate': {
          const up = smooth(Math.min(1, k / 0.2));
          p.armRx = -Math.PI + 0.25;
          p.armLx = -Math.PI + 0.25;
          p.armRz = 0.5 * up + Math.sin(this.t * 22) * 0.04;
          p.armLz = -0.5 * up - Math.sin(this.t * 22) * 0.04;
          p.torsoX = -0.18 * up;
          p.headX = -0.35 * up;
          p.y = 0.45 * up + Math.sin(this.t * 6) * 0.05;
          p.legRx = 0.15;
          p.legLx = -0.15;
          rate = 12;
          break;
        }
        case 'dodge': {
          this.direct = true;
          p.pitch = -Math.PI * 2 * smooth(k);
          p.y = Math.sin(k * Math.PI) * 0.55;
          p.armRx = -2.4;
          p.armLx = -2.4;
          p.legRx = -0.9;
          p.legLx = -0.9;
          break;
        }
        case 'hit': {
          const f = 1 - k;
          p.torsoX = 0.5 * f;
          p.headX = 0.5 * f;
          p.armRx = -0.5 * f;
          p.armLx = -0.5 * f;
          p.armRz = 0.9 * f;
          p.armLz = -0.9 * f;
          rate = 34;
          break;
        }
        case 'victory': {
          const up = smooth(Math.min(1, k / 0.25));
          p.armRx = -Math.PI + 0.3;
          p.armLx = -Math.PI + 0.3;
          p.armRz = 0.45 * up;
          p.armLz = -0.45 * up;
          p.headX = -0.25 * up;
          p.torsoX = -0.1 * up;
          p.y = Math.abs(Math.sin(this.t * 5)) * 0.14 * up;
          rate = 12;
          break;
        }
        case 'defeat': {
          this.direct = true;
          const f = smooth(Math.min(1, k * 1.4));
          p.pitch = -(Math.PI / 2) * f;
          p.y = -0.78 * f;
          p.armRz = 1.2 * f;
          p.armLz = -1.2 * f;
          p.armRx = -0.2 * f;
          p.armLx = -0.2 * f;
          break;
        }
        default:
          this.locomotion(p);
      }
    } else {
      this.locomotion(p);
    }

    const a = this.direct ? 1 : 1 - Math.exp(-rate * dt);
    const c = this.cur;
    for (const key of KEYS) c[key] += (p[key] - c[key]) * a;
    this.apply(c);
  }

  private locomotion(p: Pose): void {
    const moving = this.speed > 0.08;
    if (moving) {
      const ph = this.t * (6 + this.speed * 6);
      const sw = Math.sin(ph);
      p.legRx = sw * 0.75 * this.speed;
      p.legLx = -sw * 0.75 * this.speed;
      p.armRx = -sw * 0.6 * this.speed;
      p.armLx = sw * 0.6 * this.speed;
      p.torsoX = 0.14 * this.speed;
      p.y = -Math.abs(Math.cos(ph)) * 0.06 * this.speed;
      p.torsoY = sw * 0.12 * this.speed;
    } else if (this.ready) {
      // charged: arms open, ready to call down the ultimate
      p.armRx = -0.5 + Math.sin(this.t * 4) * 0.08;
      p.armLx = -0.5 + Math.cos(this.t * 4) * 0.08;
      p.armRz = 0.7;
      p.armLz = -0.7;
      p.y = Math.sin(this.t * 3) * 0.04;
    } else {
      p.armRz = 0.08 + Math.sin(this.t * 2) * 0.02;
      p.armLz = -0.08 - Math.sin(this.t * 2) * 0.02;
      p.y = Math.sin(this.t * 2) * 0.015;
      p.headX = Math.sin(this.t * 1.3) * 0.04;
    }
  }

  private apply(c: Pose): void {
    const r = this.rig;
    r.armR.rotation.set(c.armRx, 0, -c.armRz);
    r.armL.rotation.set(c.armLx, 0, -c.armLz);
    r.legR.rotation.x = c.legRx;
    r.legL.rotation.x = c.legLx;
    r.torso.rotation.set(c.torsoX, c.torsoY, 0);
    r.head.rotation.x = c.headX;
    r.pivot.rotation.x = c.pitch;
    r.pivot.position.y = HIP_Y + c.y;
    const flap = Math.sin(this.t * (this.speed > 0.08 ? 9 : 2.4)) * (this.speed > 0.08 ? 0.18 : 0.06);
    r.wings.forEach((w, i) => (w.rotation.z = (i === 0 ? -1 : 1) * flap));
  }
}
