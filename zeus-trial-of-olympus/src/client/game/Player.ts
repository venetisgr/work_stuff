import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { AnimationController, ProceduralAnimator, type Animator } from './AnimationController';
import { CFG, world } from './config';
import type { Effects } from './Effects';

export type PlayerState = 'idle' | 'move' | 'attack' | 'dodge' | 'hit' | 'ult' | 'victory' | 'dead';

export interface PlayerEvents {
  /** The lightning cast animation has reached its release point. */
  onCast(dir: THREE.Vector3): void;
}

/** Zeus: the loaded GLB (or a stand-in), movement, dodge, cast timing, HP and Divine Charge. */
export class Player {
  readonly root = new THREE.Group();
  readonly pos = new THREE.Vector3();
  readonly vel = new THREE.Vector3();
  readonly facing = new THREE.Vector3(0, 0, 1);
  state: PlayerState = 'idle';
  hp: number = CFG.MAX_HP;
  charge = 0;
  invuln = 0;
  dodgeLeft = 0;
  dodgeCooldown = 0;
  ultLeft = 0;

  anim: Animator | null = null;
  usingFallback = false;
  clipMapping: Record<string, string> = {};

  private model = new THREE.Group();
  private yaw = 0;
  private targetYaw = 0;
  private dodgeDir = new THREE.Vector3();
  private attackCd = 0;
  private castTimer = -1;
  private castDir = new THREE.Vector3();
  private lockTimer = 0;
  private hand: THREE.Object3D | null = null;
  private glow: THREE.Sprite;
  private glowPulse = 0;
  private events: PlayerEvents;
  private tmp = new THREE.Vector3();
  private shadow: THREE.Mesh;

  constructor(private scene: THREE.Scene, private fx: Effects, events: PlayerEvents) {
    this.events = events;
    scene.add(this.root);
    this.root.add(this.model);

    const sh = new THREE.Mesh(
      new THREE.CircleGeometry(0.9, 20),
      new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.28, depthWrite: false }),
    );
    sh.rotation.x = -Math.PI / 2;
    sh.position.y = 0.03;
    this.shadow = sh;
    this.root.add(sh);

    const c = document.createElement('canvas');
    c.width = c.height = 64;
    const g = c.getContext('2d')!;
    const grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    grad.addColorStop(0, 'rgba(255,255,255,1)');
    grad.addColorStop(0.3, 'rgba(160,210,255,0.7)');
    grad.addColorStop(1, 'rgba(100,160,255,0)');
    g.fillStyle = grad;
    g.fillRect(0, 0, 64, 64);
    this.glow = new THREE.Sprite(
      new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c), blending: THREE.AdditiveBlending, depthWrite: false, transparent: true, fog: false }),
    );
    this.glow.scale.setScalar(0.01);
    this.root.add(this.glow);
  }

  async load(url: string, onProgress?: (f: number) => void): Promise<void> {
    try {
      const gltf = await new GLTFLoader().loadAsync(url, (e) => {
        if (e.total) onProgress?.(e.loaded / e.total);
      });
      this.setupModel(gltf.scene, gltf.animations);
    } catch (err) {
      console.warn('[zeus] could not load zeus.glb, using stand-in model', err);
      this.setupFallback();
    }
    onProgress?.(1);
  }

  private setupModel(scene: THREE.Group, clips: THREE.AnimationClip[]): void {
    // Normalise scale / origin so the feet sit at y=0 and Zeus is PLAYER_HEIGHT tall.
    scene.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(scene, true);
    const size = box.getSize(new THREE.Vector3());
    const s = CFG.PLAYER_HEIGHT / (size.y || 1);
    const holder = new THREE.Group();
    holder.add(scene);
    scene.position.set(-(box.min.x + box.max.x) / 2, -box.min.y, -(box.min.z + box.max.z) / 2);
    holder.scale.setScalar(s);
    this.model.add(holder);

    scene.traverse((o) => {
      const m = o as THREE.Mesh;
      if (m.isMesh || (o as THREE.SkinnedMesh).isSkinnedMesh) {
        m.castShadow = true;
        m.frustumCulled = false; // animated bounds are unreliable for skinned meshes
        const mat = m.material as THREE.MeshStandardMaterial;
        if (mat) {
          // No environment map in the scene: keep metalness low so the model isn't black.
          mat.metalness = Math.min(mat.metalness, 0.2);
          mat.roughness = Math.max(mat.roughness, 0.7);
          if (mat.map) mat.map.anisotropy = 4;
        }
      }
      if (/righthand$/i.test(o.name)) this.hand = o;
    });

    this.anim = new AnimationController(scene, clips);
    this.clipMapping = (this.anim as AnimationController).describe();
    console.info('[zeus] clips', clips.map((c) => c.name));
    console.info('[zeus] role mapping', this.clipMapping);
    (window as unknown as { __zeus?: unknown }).__zeus = { mapping: this.clipMapping, clips: clips.map((c) => c.name) };
  }

  private setupFallback(): void {
    this.usingFallback = true;
    const body = new THREE.Group();
    const robe = new THREE.MeshStandardMaterial({ color: 0xf3efe6, roughness: 0.7 });
    const gold = new THREE.MeshStandardMaterial({ color: 0xe8b84a, metalness: 0.6, roughness: 0.4 });
    const skin = new THREE.MeshStandardMaterial({ color: 0xd9a77f, roughness: 0.8 });
    const torso = new THREE.Mesh(new THREE.CylinderGeometry(0.4, 0.6, 1.4, 12), robe);
    torso.position.y = 0.9;
    const head = new THREE.Mesh(new THREE.SphereGeometry(0.3, 14, 12), skin);
    head.position.y = 1.85;
    const beard = new THREE.Mesh(new THREE.ConeGeometry(0.3, 0.6, 10), new THREE.MeshStandardMaterial({ color: 0xf5f5f5 }));
    beard.position.set(0, 1.6, 0.12);
    beard.rotation.x = Math.PI;
    const sash = new THREE.Mesh(new THREE.TorusGeometry(0.5, 0.07, 6, 16), gold);
    sash.position.y = 1.2;
    sash.rotation.x = Math.PI / 2;
    body.add(torso, head, beard, sash);
    body.traverse((o) => ((o as THREE.Mesh).castShadow = true));
    this.model.add(body);
    this.anim = new ProceduralAnimator(body);
  }

  get alive(): boolean {
    return this.state !== 'dead';
  }

  get castingUlt(): boolean {
    return this.ultLeft > 0;
  }

  reset(): void {
    this.pos.set(0, 0, 0);
    this.vel.set(0, 0, 0);
    this.facing.set(0, 0, 1);
    this.yaw = this.targetYaw = 0;
    this.hp = CFG.MAX_HP;
    this.charge = 0;
    this.invuln = this.dodgeLeft = this.dodgeCooldown = this.ultLeft = this.attackCd = this.lockTimer = 0;
    this.castTimer = -1;
    this.state = 'idle';
    this.root.position.set(0, 0, 0);
    this.root.rotation.set(0, 0, 0);
    this.anim?.playOnce('idle', { duration: 0.01, fade: 0.05 });
  }

  /** Direction used to aim at the world point/direction; also snaps facing. */
  face(dir: THREE.Vector3, snap = false): void {
    if (dir.lengthSq() < 1e-6) return;
    this.facing.copy(dir).setY(0).normalize();
    this.targetYaw = Math.atan2(this.facing.x, this.facing.z);
    if (snap) this.yaw = this.targetYaw;
  }

  canAttack(): boolean {
    return this.attackCd <= 0 && (this.state === 'idle' || this.state === 'move' || this.state === 'attack' || this.state === 'hit') && this.ultLeft <= 0;
  }

  tryAttack(dir: THREE.Vector3): boolean {
    if (!this.canAttack()) return false;
    this.face(dir);
    this.castDir.copy(this.facing);
    const dur = this.anim?.playOnce('attack', { duration: 0.42, fade: 0.06 }) ?? 0.4;
    void dur;
    this.state = 'attack';
    this.attackCd = CFG.ATTACK_COOLDOWN;
    this.castTimer = CFG.CAST_DELAY;
    this.lockTimer = 0.34;
    this.glowPulse = 1;
    return true;
  }

  tryDodge(dir: THREE.Vector3): boolean {
    if (this.dodgeCooldown > 0 || this.ultLeft > 0 || this.state === 'dodge' || this.state === 'dead' || this.state === 'victory') return false;
    this.dodgeDir.copy(dir).setY(0);
    if (this.dodgeDir.lengthSq() < 0.01) this.dodgeDir.copy(this.facing);
    this.dodgeDir.normalize();
    this.dodgeLeft = CFG.DODGE_TIME;
    this.dodgeCooldown = CFG.DODGE_COOLDOWN;
    this.state = 'dodge';
    this.castTimer = -1;
    // Back-flip travels backwards, so Zeus faces away from the dash direction.
    this.targetYaw = Math.atan2(-this.dodgeDir.x, -this.dodgeDir.z);
    this.yaw = this.targetYaw;
    this.anim?.playOnce('dodge', { duration: CFG.DODGE_TIME + 0.05, fade: 0.05 });
    return true;
  }

  startUltimate(): boolean {
    if (this.charge < 100 || this.ultLeft > 0 || this.state === 'dead' || this.state === 'victory') return false;
    this.charge = 0;
    this.ultLeft = CFG.ULT_DURATION;
    this.state = 'ult';
    this.castTimer = -1;
    this.invuln = Math.max(this.invuln, CFG.ULT_DURATION + 0.3);
    this.anim?.playOnce('ultimate', { duration: CFG.ULT_DURATION, fade: 0.1 });
    this.glowPulse = 2;
    return true;
  }

  /** Returns true if damage was applied. */
  takeHit(fromX: number, fromZ: number, damage: number): boolean {
    if (this.invuln > 0 || this.state === 'dead' || this.state === 'victory') return false;
    this.hp = Math.max(0, this.hp - damage);
    this.invuln = CFG.INVULN_AFTER_HIT;
    this.tmp.set(this.pos.x - fromX, 0, this.pos.z - fromZ);
    if (this.tmp.lengthSq() < 1e-4) this.tmp.set(0, 0, 1);
    this.tmp.normalize();
    this.vel.addScaledVector(this.tmp, 9);
    if (this.hp <= 0) {
      this.state = 'dead';
      this.anim?.playOnce('defeat', { hold: true, fade: 0.08 });
    } else if (this.state !== 'dodge' && this.ultLeft <= 0) {
      this.state = 'hit';
      this.lockTimer = 0.38;
      this.castTimer = -1;
      this.anim?.playOnce('hit', { duration: 0.4, fade: 0.05 });
    }
    return true;
  }

  celebrate(): void {
    if (this.state === 'dead') return;
    this.state = 'victory';
    this.vel.set(0, 0, 0);
    this.castTimer = -1;
    this.anim?.playOnce('victory', { hold: true, fade: 0.2 });
    this.facing.set(0, 0, 1);
    this.targetYaw = 0;
  }

  /** World position of the casting hand (or chest height above Zeus as a fallback). */
  handWorld(out: THREE.Vector3): THREE.Vector3 {
    if (this.hand) return this.hand.getWorldPosition(out);
    return out.copy(this.pos).setY(1.6);
  }

  update(dt: number, move: THREE.Vector3): void {
    this.attackCd -= dt;
    this.dodgeCooldown -= dt;
    this.invuln -= dt;
    this.lockTimer -= dt;

    const dead = this.state === 'dead';
    const done = this.state === 'victory' || dead;

    if (this.ultLeft > 0) {
      this.ultLeft -= dt;
      if (this.ultLeft <= 0) {
        this.ultLeft = 0;
        this.state = 'idle';
      }
    }

    // --- movement ---
    if (this.state === 'dodge') {
      this.dodgeLeft -= dt;
      const k = Math.max(0, this.dodgeLeft / CFG.DODGE_TIME);
      this.vel.copy(this.dodgeDir).multiplyScalar(CFG.DODGE_SPEED * (0.35 + 0.65 * k));
      if (Math.random() < 0.9) {
        this.fx.spawn(this.pos.x, 1.0, this.pos.z, 0, 0.6, 0, 0xbfe0ff, 0.35, 0.7, 0);
      }
      if (this.dodgeLeft <= 0) this.state = 'idle';
    } else if (!done) {
      const slow = this.ultLeft > 0 ? 0.2 : this.state === 'attack' ? 0.6 : 1;
      const want = this.tmp.copy(move).multiplyScalar(CFG.PLAYER_SPEED * slow);
      // snappy accel/decel
      const rate = want.lengthSq() > 0 ? 70 : 55;
      const dv = Math.min(1, rate * dt);
      this.vel.x += (want.x - this.vel.x) * dv;
      this.vel.z += (want.z - this.vel.z) * dv;
    } else {
      this.vel.multiplyScalar(Math.max(0, 1 - 10 * dt));
    }
    this.pos.x += this.vel.x * dt;
    this.pos.z += this.vel.z * dt;
    const maxR = world.radius - 0.7;
    const r = Math.hypot(this.pos.x, this.pos.z);
    if (r > maxR) {
      this.pos.x *= maxR / r;
      this.pos.z *= maxR / r;
      // kill outward velocity
      const nx = this.pos.x / maxR;
      const nz = this.pos.z / maxR;
      const out = this.vel.x * nx + this.vel.z * nz;
      if (out > 0) {
        this.vel.x -= nx * out;
        this.vel.z -= nz * out;
      }
    }

    // solid obstacles: slide around them
    for (const o of world.obstacles) {
      const ox = this.pos.x - o.x;
      const oz = this.pos.z - o.z;
      const min = o.r + CFG.PLAYER_RADIUS;
      const d2 = ox * ox + oz * oz;
      if (d2 < min * min) {
        const d = Math.sqrt(d2) || 0.001;
        this.pos.x = o.x + (ox / d) * min;
        this.pos.z = o.z + (oz / d) * min;
        const inward = this.vel.x * (ox / d) + this.vel.z * (oz / d);
        if (inward < 0) {
          this.vel.x -= (ox / d) * inward;
          this.vel.z -= (oz / d) * inward;
        }
      }
    }

    // --- state / facing ---
    const speed01 = Math.min(1, Math.hypot(this.vel.x, this.vel.z) / CFG.PLAYER_SPEED);
    if (this.state === 'attack' && this.lockTimer <= 0) this.state = 'idle';
    if (this.state === 'hit' && this.lockTimer <= 0) this.state = 'idle';
    if (this.state === 'idle' || this.state === 'move') {
      this.state = speed01 > 0.08 ? 'move' : 'idle';
      if (move.lengthSq() > 0.01 && this.lockTimer <= 0) {
        this.facing.copy(move).normalize();
        this.targetYaw = Math.atan2(this.facing.x, this.facing.z);
      }
    }
    // shortest-angle smoothing
    let d = this.targetYaw - this.yaw;
    d = Math.atan2(Math.sin(d), Math.cos(d));
    this.yaw += d * Math.min(1, (this.state === 'attack' ? 30 : 16) * dt);
    this.root.rotation.y = this.yaw + CFG.MODEL_YAW_OFFSET;
    this.root.position.copy(this.pos);

    // --- cast release ---
    if (this.castTimer >= 0) {
      this.castTimer -= dt;
      if (this.castTimer < 0) this.events.onCast(this.castDir);
    }

    // invulnerability blink
    const blink = this.invuln > 0 && this.state !== 'ult' && this.state !== 'dead' && this.state !== 'dodge' ? Math.floor(this.invuln * 18) % 2 === 0 : true;
    this.model.visible = blink;
    this.shadow.visible = true;

    // hand glow: pulses on cast, steady when ready for the ultimate
    this.glowPulse = Math.max(0, this.glowPulse - dt * 3.5);
    const ready = this.charge >= 100;
    const gs = 0.2 + this.glowPulse * 1.8 + (ready ? 0.9 + Math.sin(performance.now() * 0.012) * 0.25 : 0) + (this.ultLeft > 0 ? 2.2 : 0);
    this.handWorld(this.tmp);
    this.glow.position.copy(this.tmp).sub(this.root.position);
    // rotate back into the root's local space
    this.glow.position.applyAxisAngle(THREE.Object3D.DEFAULT_UP, -this.root.rotation.y);
    this.glow.scale.setScalar(Math.max(0.01, gs * (ready || this.glowPulse > 0 || this.ultLeft > 0 ? 1 : 0.15)));

    this.anim?.setLocomotion(speed01, ready);
    this.anim?.update(dt);
  }

  dispose(): void {
    (this.anim as AnimationController | null)?.dispose?.();
    this.scene.remove(this.root);
    this.root.traverse((o) => {
      const m = o as THREE.Mesh;
      m.geometry?.dispose();
      const mats = Array.isArray(m.material) ? m.material : m.material ? [m.material] : [];
      mats.forEach((mat) => {
        (mat as THREE.MeshStandardMaterial).map?.dispose();
        mat.dispose();
      });
    });
  }
}
