import * as THREE from 'three';
import { Arena } from './Arena';
import { GameAudio } from './Audio';
import { CFG, levelById, phaseAt, stormAt, world, wrathAt, type Level, type Phase } from './config';
import { CombatSystem } from './CombatSystem';
import { DailyChallenge } from './DailyChallenge';
import { Effects } from './Effects';
import { EnemyManager } from './Enemies';
import { Input } from './Input';
import { Pickups } from './Pickups';
import { Player } from './Player';
import { UI } from './UI';

type State = 'loading' | 'menu' | 'playing' | 'ending' | 'results';

const BANNERS: Partial<Record<Phase, string>> = {
  escalation: 'THE SHADES GATHER',
  chaos: 'CHAOS',
  wrath: 'WRATH OF OLYMPUS',
};

export class Game {
  private renderer: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera = new THREE.PerspectiveCamera(42, 1, 0.5, 600);
  private ui = new UI();
  private audio = new GameAudio();
  private daily = new DailyChallenge();
  private input: Input;
  private fx: Effects;
  private arena: Arena;
  private player: Player;
  private enemies: EnemyManager;
  private pickups: Pickups;
  private combat: CombatSystem;
  private reticle: THREE.Mesh;
  private level: Level = levelById(1);
  private selectedLevel = 1;

  private state: State = 'loading';
  private rafId = 0;
  private last = 0;
  private runTime = 0;
  private endTimer = 0;
  private victory = false;
  /** 0 = free play, 1..3 = Daily Trial Easy / Normal / Hard. */
  private tier = 0;
  private lastPhase: Phase = 'warmup';
  private timeScale = 1;
  private camPos = new THREE.Vector3();
  private camFocus = new THREE.Vector3();
  private zoom = 1;
  private heroK = 1;
  private resultsShown = false;
  private survivePts = 0;

  private ray = new THREE.Raycaster();
  private ground = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
  private tmpV = new THREE.Vector3();
  private move = new THREE.Vector3();
  private aimDir = new THREE.Vector3(0, 0, 1);
  private isMobile = matchMedia('(pointer: coarse)').matches;
  private onResize = () => this.resize();
  private onVis = () => {
    this.last = performance.now();
  };

  constructor(host: HTMLElement) {
    const renderer = new THREE.WebGLRenderer({ antialias: !this.isMobile, powerPreference: 'high-performance', alpha: false });
    this.renderer = renderer;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, this.isMobile ? 1.5 : 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFShadowMap;
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.05;
    host.appendChild(renderer.domElement);

    this.fx = new Effects(this.scene, this.ui.flash, 800);
    this.arena = new Arena(this.scene, this.fx, this.isMobile ? 1024 : 2048, this.isMobile ? 1024 : 2048, this.level);
    this.pickups = new Pickups(this.scene, this.fx);
    this.player = new Player(this.scene, this.fx, { onCast: (d) => this.combat.cast(d) });
    this.enemies = new EnemyManager(this.scene, this.fx, this.player, this.daily.tiers[0].seed, this.level);
    this.combat = new CombatSystem(this.player, this.enemies, this.pickups, this.fx, this.arena, this.audio, {
      popup: (x, y, z, text, kind) => this.popup(x, y, z, text, kind),
      comboTier: (m) => this.ui.banner(m >= 8 ? 'x8 — GODLIKE' : `COMBO x${m}`),
      ultReady: () => this.ui.banner('ULTIMATE READY'),
      playerHit: () => {},
      wine: () => this.ui.banner('+1 HEALTH'),
    }, this.level);
    this.input = new Input(renderer.domElement, this.ui.touchRoot);

    // aim reticle on the floor (desktop)
    const rg = new THREE.RingGeometry(0.45, 0.55, 28);
    rg.rotateX(-Math.PI / 2);
    this.reticle = new THREE.Mesh(rg, new THREE.MeshBasicMaterial({ color: 0xbfe0ff, transparent: true, opacity: 0.7, depthWrite: false, fog: false }));
    this.reticle.visible = false;
    this.reticle.position.y = 0.06;
    this.scene.add(this.reticle);

    this.ui.setMuteIcon(this.audio.muted);
    this.ui.onMute(() => {
      this.audio.unlock();
      this.audio.setMuted(!this.audio.muted);
      this.ui.setMuteIcon(this.audio.muted);
    });
    window.addEventListener('resize', this.onResize);
    window.addEventListener('orientationchange', this.onResize);
    document.addEventListener('visibilitychange', this.onVis);
    this.resize();
    this.camPos.set(0, 16, 13);
    this.camera.position.copy(this.camPos);
    this.camera.lookAt(0, 0, 0);
  }

  async init(): Promise<void> {
    const profileP = this.daily.load();
    await this.player.load(`${import.meta.env.BASE_URL}zeus.glb`, (f) => this.ui.setLoading(f));
    this.player.reset();
    // First render compiles shaders / uploads textures before the menu is shown.
    this.renderer.compile(this.scene, this.camera);
    this.renderFrame();
    await Promise.race([profileP, new Promise((r) => setTimeout(r, 1500))]);
    this.ui.hideLoading();
    this.toMenu();
    this.last = performance.now();
    this.rafId = requestAnimationFrame(this.loop);
    const w = window as unknown as { __game?: Game };
    w.__game = this;
  }

  private toMenu(): void {
    this.state = 'menu';
    this.ui.setTouchVisible(false);
    this.ui.hideResults();
    this.resetWorld(0, this.level.id);
    this.ui.showMenu(this.daily.profile, this.daily.dayKey, this.daily.tiers, this.selectedLevel, (lvl, tier) => this.start(tier, lvl));
    this.ui.setTouch(this.input.isTouch || document.body.classList.contains('is-touch'));
  }

  private resetWorld(tier: number, levelId: number): void {
    if (levelId !== this.level.id) {
      this.level = levelById(levelId);
      this.arena.buildWorld(this.level);
    }
    this.runTime = 0;
    this.endTimer = 0;
    this.victory = false;
    this.timeScale = 1;
    this.zoom = 1;
    this.resultsShown = false;
    this.survivePts = 0;
    this.lastPhase = 'warmup';
    this.player.reset();
    const seed = tier ? this.daily.tiers[tier - 1].seed : (Math.random() * 0xffffffff) >>> 0;
    this.enemies.reset(seed, this.level);
    this.pickups.reset();
    this.combat.reset(this.level);
    this.fx.reset();
    this.arena.setStorm(0, 0);
  }

  /** Starts a run immediately — no reload, no re-init. */
  start(tier: number, levelId?: number): void {
    const lvl = tier ? this.daily.tiers[tier - 1].level : Math.min(levelId ?? this.selectedLevel, this.daily.profile.unlocked);
    if (!tier) this.selectedLevel = lvl;
    this.audio.unlock();
    this.audio.startMusic();
    this.tier = tier;
    this.resetWorld(tier, lvl);
    this.input.clearQueued();
    this.ui.setLevelLabel(`LEVEL ${this.level.id} · ${this.level.name}`.toUpperCase());
    this.ui.hideMenu();
    this.ui.hideResults();
    this.ui.showHud();
    this.ui.banner(tier ? `DAILY TRIAL · ${this.daily.tiers[tier - 1].label}` : this.level.boss ? 'CHAMPION TRIAL' : this.level.name.toUpperCase());
    this.ui.setTouchVisible(true);
    this.state = 'playing';
  }

  private popup(x: number, y: number, z: number, text: string, kind: 'score' | 'spark' | 'multi' | 'wine'): void {
    this.tmpV.set(x, y, z).project(this.camera);
    const r = this.renderer.domElement.getBoundingClientRect();
    this.ui.popup(((this.tmpV.x + 1) / 2) * r.width, ((1 - this.tmpV.y) / 2) * r.height, text, kind);
  }

  private resize(): void {
    const w = window.innerWidth;
    const h = window.innerHeight;
    this.renderer.setSize(w, h, false);
    this.renderer.domElement.style.width = '100%';
    this.renderer.domElement.style.height = '100%';
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    const px = this.renderer.getPixelRatio();
    this.fx.setPointScale((h * px) / (2 * Math.tan((this.camera.fov * Math.PI) / 360)));
  }

  private loop = (now: number): void => {
    this.rafId = requestAnimationFrame(this.loop);
    const rawDt = Math.min(0.05, Math.max(0, (now - this.last) / 1000));
    this.last = now;
    this.step(rawDt);
    this.renderFrame();
  };

  private renderFrame(): void {
    this.renderer.render(this.scene, this.camera);
  }

  /** Advances the simulation. Public so tests/tools can step deterministically. */
  step(rawDt: number): void {
    const dt = rawDt * this.timeScale;
    this.input.poll();
    const p = this.player;

    if (this.state === 'playing') {
      this.runTime += dt;
      this.handleInput(dt);
      // surviving earns points
      this.survivePts += CFG.SURVIVE_POINTS_PER_SEC * dt;
      if (this.survivePts >= 1) {
        const whole = Math.floor(this.survivePts);
        this.combat.stats.score += whole;
        this.survivePts -= whole;
      }
      const ph = phaseAt(this.runTime, this.level.time);
      if (ph !== this.lastPhase) {
        this.lastPhase = ph;
        const b = BANNERS[ph];
        if (b) this.ui.banner(b);
        if (ph === 'wrath') {
          this.fx.addShake(0.7);
          this.audio.play('thunder', 1.4);
        }
      }
      if (!p.alive) this.beginEnding(false);
      else if (this.runTime >= this.level.time) this.beginEnding(true);
    } else if (this.state === 'ending') {
      this.endTimer += rawDt;
      p.update(dt, this.move.set(0, 0, 0));
      // ease slow-mo back to normal after the first beat
      this.timeScale = this.endTimer < 0.9 ? 0.35 : Math.min(1, this.timeScale + rawDt * 2);
      this.zoom = Math.max(0.78, this.zoom - rawDt * 0.12);
      if (this.endTimer > (this.victory ? 2.9 : 2.0) && !this.resultsShown) this.showResults();
    }

    if (this.state === 'playing' || this.state === 'ending') {
      if (this.state === 'playing') p.update(dt, this.move);
      this.enemies.update(dt, this.runTime, this.state === 'playing');
      this.combat.update(dt, this.state === 'playing');
    } else {
      // menu / results: Zeus idles, arena keeps animating
      p.update(rawDt, this.move.set(0, 0, 0));
      this.enemies.update(rawDt, this.runTime, false);
    }

    const storm = this.state === 'menu' ? 0.15 : this.state === 'results' ? 0.5 : stormAt(this.runTime, this.level.time);
    const wrath = this.state === 'playing' || this.state === 'ending' ? wrathAt(this.runTime, this.level.time) : 0;
    this.arena.setStorm(storm, wrath);
    this.renderer.toneMappingExposure = 1.05 - storm * 0.3;
    this.arena.update(rawDt, performance.now() / 1000, storm, p.pos);
    // wrath: random strikes around the arena
    if (this.state === 'playing' && wrath > 0 && Math.random() < dt * (3 + wrath * 6) * (world.radius / 15)) {
      const a = Math.random() * Math.PI * 2;
      const r = 4 + Math.random() * (world.radius - 5);
      this.fx.strike(Math.cos(a) * r, Math.sin(a) * r, 0.35 + wrath * 0.3, 0xdfeaff);
      this.arena.flash(0.2);
      this.fx.addShake(0.04);
    }
    this.fx.update(rawDt);
    this.audio.setIntensity(storm);
    this.audio.update(rawDt);
    this.updateCamera(rawDt);

    if (this.state === 'playing' || this.state === 'ending') {
      this.ui.update(this.combat.stats.score, this.level.time - this.runTime, this.combat.multiplier, this.combat.tier, p.charge, p.hp);
    }
  }

  private handleInput(dt: number): void {
    const p = this.player;
    const inp = this.input;
    // screen axes → world: camera looks toward -Z
    this.move.set(inp.move.x, 0, -inp.move.y);

    // aim: mouse on desktop, nearest enemy on touch, otherwise facing
    this.updateAim();

    if (inp.consumeDodge() && p.tryDodge(this.move.lengthSq() > 0.01 ? this.move : p.facing)) {
      this.audio.play('dodge');
      this.fx.ring(p.pos.x, p.pos.z, 2.2, 0.35, 0xbfe0ff);
    }
    if (inp.consumeUlt()) this.combat.startUltimate();
    if (inp.attackHeld && p.canAttack()) {
      if (p.tryAttack(this.aimDir)) {
        // cost + feedback handled when the bolt releases
      }
    }
    void dt;
  }

  private updateAim(): void {
    const p = this.player;
    this.reticle.visible = false;
    if (this.input.mouseNdc && !this.input.isTouch) {
      this.ray.setFromCamera(this.input.mouseNdc, this.camera);
      if (this.ray.ray.intersectPlane(this.ground, this.tmpV)) {
        const dx = this.tmpV.x - p.pos.x;
        const dz = this.tmpV.z - p.pos.z;
        if (dx * dx + dz * dz > 0.25) this.aimDir.set(dx, 0, dz).normalize();
        this.reticle.visible = this.state === 'playing';
        this.reticle.position.set(this.tmpV.x, 0.06, this.tmpV.z);
        return;
      }
    }
    // touch / keyboard-only: nearest living enemy, else current facing
    let best = Infinity;
    let found = false;
    for (const e of this.enemies.active) {
      if (e.state !== 'alive') continue;
      const d = (e.x - p.pos.x) ** 2 + (e.z - p.pos.z) ** 2;
      if (d < best) {
        best = d;
        this.aimDir.set(e.x - p.pos.x, 0, e.z - p.pos.z);
        found = true;
      }
    }
    if (found) this.aimDir.normalize();
    else this.aimDir.copy(p.facing);
  }

  private beginEnding(victory: boolean): void {
    this.state = 'ending';
    this.victory = victory;
    this.endTimer = 0;
    this.enemies.spawning = false;
    this.timeScale = 0.35;
    if (victory) {
      this.combat.stats.score += CFG.COMPLETION_BONUS * this.level.id;
      this.ui.banner(`LEVEL ${this.level.id} CLEARED`);
      this.combat.finale();
      this.player.celebrate();
    } else {
      this.ui.banner('ZEUS HAS FALLEN');
      this.fx.addShake(0.8);
      this.fx.screenFlash(0.5, '255,60,40');
    }
    this.audio.stopMusic();
  }

  private async showResults(): Promise<void> {
    this.resultsShown = true;
    this.state = 'results';
    this.ui.setTouchVisible(false);
    const s = this.combat.stats;
    const score = Math.floor(s.score);
    const prevBest = this.daily.profile.levelBest[this.level.id] ?? 0;
    // Local record is saved synchronously inside submit(); the server call is best-effort.
    const submit = this.daily.submit(score, s.kills, s.maxCombo, this.tier, this.level.id, this.victory);
    const show = (rank: number | null, newBest: boolean) =>
      this.ui.showResults(
        {
          score,
          best: this.daily.profile.levelBest[this.level.id] ?? score,
          kills: s.kills,
          maxCombo: s.maxCombo,
          newBest: newBest && score > 0,
          victory: this.victory,
          tier: this.tier,
          tierLabel: this.tier ? this.daily.tiers[this.tier - 1].label : '',
          dayKey: this.daily.dayKey,
          rank,
          leaderboard: this.tier ? this.daily.profile.leaderboards[this.tier - 1] : [],
          username: this.daily.profile.username,
          level: this.level.id,
          levelName: this.level.name,
          cleared: this.victory,
        },
        () => this.start(this.tier, this.level.id),
        () => this.toMenu(),
        () => this.start(0, this.level.id + 1),
      );
    show(null, score > prevBest);
    const res = await submit;
    if (this.state === 'results') show(res.rank, res.newBest);
  }

  private updateCamera(dt: number): void {
    const p = this.player.pos;
    const aspect = this.camera.aspect;
    // portrait screens get a wider lens and a pull-back so the arena stays readable
    const fov = aspect < 1 ? 58 : 42;
    if (Math.abs(this.camera.fov - fov) > 0.01) {
      this.camera.fov = fov;
      this.camera.updateProjectionMatrix();
    }
    const hero = this.state === 'menu' || this.state === 'results' ? 0.62 : 1;
    this.heroK += (hero - this.heroK) * (1 - Math.exp(-4 * dt));
    const tanH = Math.tan((fov * Math.PI) / 360) * aspect;
    const dist = Math.min(40, Math.max(15, 8.5 / tanH)) * this.zoom * this.heroK * (1 + Math.min(0.35, 0.04 * (this.level.id - 1)));
    const elev = this.state === 'menu' ? 0.6 : 0.78;
    const target = this.tmpV.set(p.x * 0.8, 0, p.z * 0.8);
    this.camFocus.lerp(target, 1 - Math.exp(-6 * dt));
    const desired = new THREE.Vector3(this.camFocus.x, Math.sin(elev) * dist, this.camFocus.z + Math.cos(elev) * dist);
    this.camPos.lerp(desired, 1 - Math.exp(-8 * dt));
    const s = this.fx.shake * this.fx.shake;
    this.camera.position.set(
      this.camPos.x + (Math.random() - 0.5) * s * 1.1,
      this.camPos.y + (Math.random() - 0.5) * s * 0.8,
      this.camPos.z + (Math.random() - 0.5) * s * 1.1,
    );
    this.camera.lookAt(this.camFocus.x, 0.8, this.camFocus.z - 1.5);
  }

  obstacleList() {
    return world.obstacles;
  }

  /** Debug helper: number of solid obstacles in the current arena. */
  obstacleCount(): number {
    return world.obstacles.length;
  }

  /** Test/debug helper: jump the run clock forward. */
  debugSkip(seconds: number): void {
    this.runTime += seconds;
  }

  get debugState() {
    return { level: this.level.id, radius: world.radius, state: this.state, runTime: this.runTime, stats: this.combat.stats, hp: this.player.hp, charge: this.player.charge, enemies: this.enemies.active.length, mapping: this.player.clipMapping, fallback: this.player.usingFallback };
  }

  dispose(): void {
    cancelAnimationFrame(this.rafId);
    window.removeEventListener('resize', this.onResize);
    window.removeEventListener('orientationchange', this.onResize);
    document.removeEventListener('visibilitychange', this.onVis);
    this.input.dispose();
    this.ui.dispose();
    this.audio.stopMusic();
    this.enemies.dispose();
    this.pickups.dispose();
    this.player.dispose();
    this.arena.dispose();
    this.fx.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}
