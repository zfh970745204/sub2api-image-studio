export type SelectionAction =
  | { type: "wand"; x: number; y: number; tolerance: number; contiguous: boolean; mode: "remove" | "restore" }
  | { type: "tolerance"; tolerance: number }
  | { type: "undo" | "redo" | "reset" | "clear" | "auto" };

/** All masks are full-resolution output alpha, never display-resolution pixels. */
export class BackgroundSelection {
  readonly initial: Uint8ClampedArray;
  alpha: Uint8ClampedArray;
  private undoStack: Uint8ClampedArray[] = [];
  private redoStack: Uint8ClampedArray[] = [];
  private lastWand: { action: Extract<SelectionAction, { type: "wand" }>; before: Uint8ClampedArray } | null = null;
  private historyLimit: number;

  constructor(readonly width: number, readonly height: number, readonly source: Uint8ClampedArray, readonly result: Uint8ClampedArray, hasSelection: boolean) {
    const size = width * height;
    if (size < 1 || size > 16_000_000 || source.length !== size * 4 || result.length !== source.length) throw new Error("修边原图与结果尺寸不一致，无法安全恢复像素");
    this.alpha = new Uint8ClampedArray(size);
    for (let i = 0; i < size; i++) this.alpha[i] = Math.min(source[i * 4 + 3], result[i * 4 + 3]);
    this.historyLimit = Math.max(2, Math.min(30, Math.floor(48_000_000 / size)));
    if (!hasSelection) this.selectBorder();
    this.initial = this.alpha.slice();
  }

  get canUndo() { return this.undoStack.length > 0; }
  get canRedo() { return this.redoStack.length > 0; }
  get canRetune() { return this.lastWand !== null; }

  apply(action: SelectionAction) {
    if (action.type === "tolerance") {
      if (this.lastWand) {
        this.alpha = this.lastWand.before.slice();
        this.lastWand.action = { ...this.lastWand.action, tolerance: action.tolerance };
        this.wand(this.lastWand.action);
      }
      return;
    }
    this.lastWand = null;
    if (action.type === "undo" || action.type === "redo") {
      const from = action.type === "undo" ? this.undoStack : this.redoStack;
      const to = action.type === "undo" ? this.redoStack : this.undoStack;
      const snapshot = from.pop();
      if (snapshot) { to.push(this.alpha); this.alpha = snapshot; }
      return;
    }
    const before = this.alpha.slice();
    this.undoStack.push(before);
    if (this.undoStack.length > this.historyLimit) this.undoStack.shift();
    this.redoStack = [];
    if (action.type === "wand") {
      this.lastWand = { action, before };
      this.wand(action);
    } else if (action.type === "reset") this.alpha = this.initial.slice();
    else if (action.type === "clear") {
      for (let i = 0; i < this.alpha.length; i++) this.alpha[i] = this.source[i * 4 + 3];
    } else if (action.type === "auto") this.selectBorder();
  }

  private distance(index: number, color: number[]) {
    const offset = index * 4;
    return Math.max(Math.abs(this.source[offset] - color[0]), Math.abs(this.source[offset + 1] - color[1]), Math.abs(this.source[offset + 2] - color[2]));
  }

  private region(seeds: number[], color: number[], tolerance: number, contiguous: boolean, visit: (i: number) => void) {
    const threshold = Math.max(0, Math.min(100, tolerance)) / 100 * 255;
    const matches = (i: number) => this.source[i * 4 + 3] > 0 && this.distance(i, color) <= threshold;
    if (!contiguous) {
      for (let i = 0; i < this.alpha.length; i++) if (matches(i)) visit(i);
      return;
    }
    const seen = new Uint8Array(this.alpha.length);
    const queue = new Int32Array(this.alpha.length);
    let start = 0, end = 0;
    const enqueue = (i: number) => {
      if (seen[i]) return;
      seen[i] = 1;
      if (matches(i)) queue[end++] = i;
    };
    seeds.forEach(enqueue);
    while (start < end) {
      const i = queue[start++];
      visit(i);
      if (i % this.width > 0) enqueue(i - 1);
      if (i % this.width < this.width - 1) enqueue(i + 1);
      if (i >= this.width) enqueue(i - this.width);
      if (i < this.alpha.length - this.width) enqueue(i + this.width);
    }
  }

  private wand(action: Extract<SelectionAction, { type: "wand" }>) {
    const x = Math.max(0, Math.min(this.width - 1, Math.floor(action.x)));
    const y = Math.max(0, Math.min(this.height - 1, Math.floor(action.y)));
    const index = y * this.width + x;
    if (!this.source[index * 4 + 3]) return;
    const color = Array.from(this.source.subarray(index * 4, index * 4 + 3));
    this.region([index], color, action.tolerance, action.contiguous, (i) => {
      this.alpha[i] = action.mode === "remove" ? 0 : this.source[i * 4 + 3];
    });
  }

  private selectBorder() {
    const border: number[] = [];
    for (let x = 0; x < this.width; x++) { border.push(x); if (this.height > 1) border.push((this.height - 1) * this.width + x); }
    for (let y = 1; y < this.height - 1; y++) { border.push(y * this.width); if (this.width > 1) border.push(y * this.width + this.width - 1); }
    const buckets = new Map<string, { count: number; sum: number[] }>();
    let opaque = 0;
    for (const i of border) {
      if (this.source[i * 4 + 3] < 200) continue;
      opaque++;
      const rgb = Array.from(this.source.subarray(i * 4, i * 4 + 3));
      const key = rgb.map((c) => Math.floor(c / 24)).join(":");
      const bucket = buckets.get(key) || { count: 0, sum: [0, 0, 0] };
      bucket.count++;
      rgb.forEach((c, n) => { bucket.sum[n] += c; });
      buckets.set(key, bucket);
    }
    const dominant = [...buckets.values()].sort((a, b) => b.count - a.count)[0];
    if (!dominant || dominant.count < opaque * .45) return;
    const color = dominant.sum.map((c) => c / dominant.count);
    this.region(border, color, 8, true, (i) => { this.alpha[i] = 0; });
  }

  render(edge = 0, pixels = new Uint8ClampedArray(this.source.length)) {
    if (pixels.length !== this.source.length || pixels.buffer === this.source.buffer || pixels.buffer === this.result.buffer) throw new Error("预览缓冲区必须与原图等大且独立");
    let alpha = this.alpha;
    // Erode alpha only; callers can turn this down without losing their selection.
    for (let pass = 0; pass < Math.max(0, Math.min(3, Math.floor(edge))); pass++) {
      const next = alpha.slice();
      for (let i = 0; i < alpha.length; i++) {
        let value = alpha[i];
        if (i % this.width > 0) value = Math.min(value, alpha[i - 1]);
        if (i % this.width < this.width - 1) value = Math.min(value, alpha[i + 1]);
        if (i >= this.width) value = Math.min(value, alpha[i - this.width]);
        if (i < alpha.length - this.width) value = Math.min(value, alpha[i + this.width]);
        next[i] = value;
      }
      alpha = next;
    }
    let removed = 0, visible = 0;
    for (let i = 0; i < alpha.length; i++) {
      const at = i * 4;
      // Preserve the model's edge decontamination until that pixel is restored.
      const rgb = this.alpha[i] === this.result[at + 3] ? this.result : this.source;
      pixels[at] = rgb[at]; pixels[at + 1] = rgb[at + 1]; pixels[at + 2] = rgb[at + 2];
      pixels[at + 3] = alpha[i];
      if (alpha[i] < this.source[at + 3]) removed++;
      if (alpha[i]) visible++;
    }
    return { pixels, removed, visible };
  }
}
