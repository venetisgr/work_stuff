import * as THREE from 'three';

/**
 * Unified keyboard / mouse / touch input. The game reads `move`, `aim` and the
 * edge-triggered `consume*` flags; it never touches DOM events directly.
 */
export class Input {
  /** Desired movement, length ≤ 1, in screen axes: x right, y up. */
  readonly move = new THREE.Vector2();
  /** Mouse position in NDC; null until the mouse has been used. */
  mouseNdc: THREE.Vector2 | null = null;
  attackHeld = false;
  isTouch = false;

  private keys = new Set<string>();
  private dodgeQueued = false;
  private ultQueued = false;
  private anyQueued = false;
  private joyId: number | null = null;
  private joyOrigin = new THREE.Vector2();
  private joyVec = new THREE.Vector2();
  private cleanup: Array<() => void> = [];

  constructor(private canvas: HTMLElement, private touchRoot: HTMLElement) {
    const on = <K extends keyof WindowEventMap>(t: Window, k: K, f: (e: WindowEventMap[K]) => void, o?: AddEventListenerOptions) => {
      t.addEventListener(k, f, o);
      this.cleanup.push(() => t.removeEventListener(k, f, o));
    };
    on(window, 'keydown', (e) => {
      if (e.repeat) return;
      const k = e.code;
      this.keys.add(k);
      this.anyQueued = true;
      if (k === 'ShiftLeft' || k === 'ShiftRight') this.dodgeQueued = true;
      if (k === 'KeyE' || k === 'KeyQ') this.ultQueued = true;
      if (k === 'Space' || k.startsWith('Arrow')) e.preventDefault();
      if (k === 'Space') this.attackHeld = true;
    });
    on(window, 'keyup', (e) => {
      this.keys.delete(e.code);
      if (e.code === 'Space') this.attackHeld = false;
    });
    on(window, 'blur', () => {
      this.keys.clear();
      this.attackHeld = false;
    });

    on(window, 'mousemove', (e) => {
      this.setMouse(e.clientX, e.clientY);
    });
    on(window, 'mousedown', (e) => {
      if ((e.target as HTMLElement).closest('.ui-panel, .ui-btn')) return;
      this.setMouse(e.clientX, e.clientY);
      this.anyQueued = true;
      if (e.button === 0) this.attackHeld = true;
      if (e.button === 2) this.ultQueued = true;
    });
    on(window, 'mouseup', (e) => {
      if (e.button === 0) this.attackHeld = false;
    });
    on(window, 'contextmenu', (e) => e.preventDefault());

    this.bindTouch();
  }

  private setMouse(x: number, y: number): void {
    const r = this.canvas.getBoundingClientRect();
    if (!this.mouseNdc) this.mouseNdc = new THREE.Vector2();
    this.mouseNdc.set(((x - r.left) / r.width) * 2 - 1, -((y - r.top) / r.height) * 2 + 1);
  }

  private bindTouch(): void {
    const zone = this.touchRoot.querySelector<HTMLElement>('#joy-zone')!;
    const knob = this.touchRoot.querySelector<HTMLElement>('#joy-knob')!;
    const base = this.touchRoot.querySelector<HTMLElement>('#joy-base')!;
    const R = 56;
    const add = (el: HTMLElement | Window, k: string, f: (e: PointerEvent) => void) => {
      el.addEventListener(k, f as EventListener, { passive: false });
      this.cleanup.push(() => el.removeEventListener(k, f as EventListener));
    };
    add(zone, 'pointerdown', (e) => {
      if (e.pointerType === 'mouse') return;
      this.isTouch = true;
      this.touchRoot.classList.add('on');
      this.mouseNdc = null;
      e.preventDefault();
      this.joyId = e.pointerId;
      this.joyOrigin.set(e.clientX, e.clientY);
      this.joyVec.set(0, 0);
      base.style.left = `${e.clientX}px`;
      base.style.top = `${e.clientY}px`;
      base.style.opacity = '1';
      knob.style.transform = 'translate(-50%,-50%)';
      this.anyQueued = true;
    });
    add(window, 'pointermove', (e) => {
      if (e.pointerId !== this.joyId) return;
      const dx = e.clientX - this.joyOrigin.x;
      const dy = e.clientY - this.joyOrigin.y;
      const len = Math.hypot(dx, dy);
      const k = len > R ? R / len : 1;
      this.joyVec.set((dx * k) / R, (-dy * k) / R);
      knob.style.transform = `translate(calc(-50% + ${dx * k}px), calc(-50% + ${dy * k}px))`;
    });
    const end = (e: PointerEvent) => {
      if (e.pointerId !== this.joyId) return;
      this.joyId = null;
      this.joyVec.set(0, 0);
      base.style.opacity = '0.35';
    };
    add(window, 'pointerup', end);
    add(window, 'pointercancel', end);

    const btn = (id: string, down: () => void, up?: () => void) => {
      const el = this.touchRoot.querySelector<HTMLElement>(id)!;
      add(el, 'pointerdown', (e) => {
        e.preventDefault();
        this.isTouch = true;
        this.touchRoot.classList.add('on');
        this.mouseNdc = null;
        this.anyQueued = true;
        el.classList.add('down');
        down();
      });
      const release = () => {
        el.classList.remove('down');
        up?.();
      };
      add(el, 'pointerup', release);
      add(el, 'pointercancel', release);
      add(el, 'pointerleave', release);
    };
    btn('#btn-attack', () => (this.attackHeld = true), () => (this.attackHeld = false));
    btn('#btn-dodge', () => (this.dodgeQueued = true));
    btn('#btn-ult', () => (this.ultQueued = true));
  }

  /** Call once per frame before reading `move`. */
  poll(): void {
    const k = this.keys;
    let x = 0;
    let y = 0;
    if (k.has('KeyA') || k.has('ArrowLeft')) x -= 1;
    if (k.has('KeyD') || k.has('ArrowRight')) x += 1;
    if (k.has('KeyW') || k.has('ArrowUp')) y += 1;
    if (k.has('KeyS') || k.has('ArrowDown')) y -= 1;
    this.move.set(x, y);
    if (this.move.lengthSq() > 1) this.move.normalize();
    if (this.joyId !== null) {
      const m = this.joyVec.length();
      if (m > 0.12) this.move.copy(this.joyVec).multiplyScalar(Math.min(1, m * 1.15));
    }
  }

  consumeDodge(): boolean {
    const v = this.dodgeQueued;
    this.dodgeQueued = false;
    return v;
  }
  consumeUlt(): boolean {
    const v = this.ultQueued;
    this.ultQueued = false;
    return v;
  }
  /** True if any input happened since last call (used for menu "press any key"). */
  consumeAny(): boolean {
    const v = this.anyQueued;
    this.anyQueued = false;
    return v;
  }
  clearQueued(): void {
    this.dodgeQueued = this.ultQueued = this.anyQueued = false;
  }

  dispose(): void {
    this.cleanup.forEach((f) => f());
    this.cleanup = [];
  }
}
