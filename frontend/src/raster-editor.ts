export type Point = { x: number; y: number };
export type SelectionMode = "replace" | "add" | "subtract" | "intersect";
export type PaintOptions = { color: string; opacity: number; size: number; erase: boolean };
export type RasterAction =
  | { type: "shape"; shape: "rectangle" | "ellipse" | "lasso"; points: Point[]; mode: SelectionMode }
  | { type: "wand"; point: Point; tolerance: number; contiguous: boolean; mode: SelectionMode }
  | { type: "selection"; operation: "all" | "none" | "invert" }
  | { type: "fill"; color: string; opacity: number }
  | { type: "delete" }
  | { type: "undo" | "redo" | "reset" };
type Snapshot = { pixels: Uint8ClampedArray; selection: Uint8Array | null };
const clamp = (n: number, low: number, high: number) => Math.max(low, Math.min(high, n));

export function parseColor(color: string): [number, number, number] {
  if (!/^#[\da-f]{6}$/i.test(color)) throw new Error("请输入六位颜色，例如 #FF6600");
  return [1, 3, 5].map((i) => Number.parseInt(color.slice(i, i + 2), 16)) as [number, number, number];
}

/** Pixel operations run at source resolution. A null selection means unrestricted;
 * an empty mask remains restrictive, even after subtracting the entire selection. */
export class RasterEditor {
  readonly original: Uint8ClampedArray;
  pixels: Uint8ClampedArray;
  selection: Uint8Array | null = null;
  private undoStack: Snapshot[] = [];
  private redoStack: Snapshot[] = [];
  private stroke: { before: Snapshot; coverage: Uint8Array; last: Point; options: PaintOptions; rgb: number[] } | null = null;

  constructor(readonly width: number, readonly height: number, pixels: Uint8ClampedArray) {
    if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1 || width * height > 16_000_000 || pixels.length !== width * height * 4) throw new Error("基础编辑支持最高 1600 万像素的图片");
    this.original = pixels.slice();
    this.pixels = this.original;
  }
  get canUndo() { return this.undoStack.length > 0; }
  get canRedo() { return this.redoStack.length > 0; }
  get drawing() { return this.stroke !== null; }
  get changed() { return this.pixels !== this.original; }
  get selectedCount() { return this.selection?.reduce((sum, value) => sum + Number(value > 0), 0) ?? null; }
  private snapshot(): Snapshot { return { pixels: this.pixels, selection: this.selection }; }
  private restore(state: Snapshot) { this.pixels = state.pixels; this.selection = state.selection; }
  private remember(before: Snapshot) {
    this.undoStack.push(before); this.redoStack = [];
    // Keep bounded history, counting shared immutable buffers only once.
    while (this.undoStack.length > 1) {
      const buffers = new Set<ArrayBufferLike>();
      for (const state of this.undoStack) {
        if (state.pixels !== this.original && state.pixels !== this.pixels) buffers.add(state.pixels.buffer);
        if (state.selection) buffers.add(state.selection.buffer);
      }
      const bytes = [...buffers].reduce((sum, buffer) => sum + buffer.byteLength, 0);
      if (bytes <= 96_000_000 && this.undoStack.length <= 30) break;
      this.undoStack.shift();
    }
  }
  private bounded(point: Point) {
    if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) throw new Error("选区坐标无效");
    return { x: clamp(point.x, 0, this.width - 1), y: clamp(point.y, 0, this.height - 1) };
  }
  sample(point: Point, original: boolean) {
    const p = this.bounded(point), i = (Math.floor(p.y) * this.width + Math.floor(p.x)) * 4;
    const pixels = original ? this.original : this.pixels;
    if (!pixels[i + 3]) throw new Error("这里是透明区域，请在有颜色的位置取色");
    return "#" + [...pixels.subarray(i, i + 3)].map((c) => c.toString(16).padStart(2, "0")).join("").toUpperCase();
  }
  private combine(mask: Uint8Array, mode: SelectionMode) {
    if (mode === "replace" || (!this.selection && mode === "add")) { this.selection = mask; return; }
    const previous = this.selection;
    for (let i = 0; i < mask.length; i++) {
      const old = previous?.[i] ?? 0;
      mask[i] = mode === "add" ? Math.max(old, mask[i]) : mode === "subtract" ? Math.max(0, old - mask[i]) : Math.min(old, mask[i]);
    }
    this.selection = mask;
  }
  private shape(action: Extract<RasterAction, { type: "shape" }>) {
    const points = action.points.map((p) => this.bounded(p));
    if (points.length < (action.shape === "lasso" ? 3 : 2)) return;
    const mask = new Uint8Array(this.width * this.height);
    let left = this.width - 1, right = 0, top = this.height - 1, bottom = 0;
    for (const p of points) { left = Math.min(left, p.x); right = Math.max(right, p.x); top = Math.min(top, p.y); bottom = Math.max(bottom, p.y); }
    left = Math.floor(left); right = Math.floor(right); top = Math.floor(top); bottom = Math.floor(bottom);
    const cx = (left + right + 1) / 2, cy = (top + bottom + 1) / 2;
    const rx = (right - left + 1) / 2, ry = (bottom - top + 1) / 2;
    for (let y = top; y <= bottom; y++) {
      if (action.shape === "lasso") {
        const crossings: number[] = [];
        for (let j = 0; j < points.length; j++) {
          const a = points[j], b = points[(j + 1) % points.length];
          if ((a.y > y + .5) !== (b.y > y + .5)) crossings.push(a.x + (y + .5 - a.y) * (b.x - a.x) / (b.y - a.y));
        }
        crossings.sort((a, b) => a - b);
        for (let j = 0; j + 1 < crossings.length; j += 2) {
          for (let x = Math.max(left, Math.ceil(crossings[j] - .5)); x <= Math.min(right, Math.floor(crossings[j + 1] - .5)); x++) mask[y * this.width + x] = 255;
        }
      } else for (let x = left; x <= right; x++) {
        if (action.shape === "rectangle" || ((x + .5 - cx) / rx) ** 2 + ((y + .5 - cy) / ry) ** 2 <= 1) mask[y * this.width + x] = 255;
      }
    }
    this.combine(mask, action.mode);
  }
  private wand(action: Extract<RasterAction, { type: "wand" }>) {
    const p = this.bounded(action.point), start = Math.floor(p.y) * this.width + Math.floor(p.x);
    const mask = new Uint8Array(this.width * this.height);
    const tolerance = clamp(action.tolerance, 0, 100) * 2.55;
    const matches = (i: number) => {
      if (this.pixels[start * 4 + 3] === 0) return this.pixels[i * 4 + 3] === 0;
      if (this.pixels[i * 4 + 3] === 0) return false;
      for (let c = 0; c < 4; c++) if (Math.abs(this.pixels[i * 4 + c] - this.pixels[start * 4 + c]) > tolerance) return false;
      return true;
    };
    if (!action.contiguous) {
      for (let i = 0; i < mask.length; i++) if (matches(i)) mask[i] = 255;
    } else {
      const seen = new Uint8Array(mask.length), queue = new Int32Array(mask.length);
      let read = 0, write = 0;
      const enqueue = (i: number) => { if (!seen[i]) { seen[i] = 1; if (matches(i)) queue[write++] = i; } };
      enqueue(start);
      while (read < write) {
        const i = queue[read++]; mask[i] = 255;
        if (i % this.width) enqueue(i - 1);
        if (i % this.width < this.width - 1) enqueue(i + 1);
        if (i >= this.width) enqueue(i - this.width);
        if (i < mask.length - this.width) enqueue(i + this.width);
      }
    }
    this.combine(mask, action.mode);
  }
  private blend(i: number, amount: number, rgb: number[], erase: boolean, before: Uint8ClampedArray) {
    const at = i * 4, oldAlpha = before[at + 3] / 255;
    const alpha = erase ? oldAlpha * (1 - amount) : amount + oldAlpha * (1 - amount);
    for (let c = 0; c < 3; c++) this.pixels[at + c] = alpha === 0 ? 0 : erase ? before[at + c] : (rgb[c] * amount + before[at + c] * oldAlpha * (1 - amount)) / alpha;
    this.pixels[at + 3] = alpha * 255;
  }
  apply(action: RasterAction) {
    if (this.stroke) throw new Error("请先完成当前笔画");
    if (action.type === "undo" || action.type === "redo") {
      const from = action.type === "undo" ? this.undoStack : this.redoStack;
      const to = action.type === "undo" ? this.redoStack : this.undoStack;
      const next = from.pop();
      if (next) { to.push(this.snapshot()); this.restore(next); }
      return;
    }
    const before = this.snapshot();
    if (action.type === "shape") this.shape(action);
    else if (action.type === "wand") this.wand(action);
    else if (action.type === "selection") {
      if (action.operation === "none") this.selection = null;
      else {
        const mask = new Uint8Array(this.width * this.height);
        for (let i = 0; i < mask.length; i++) mask[i] = action.operation === "all" ? 255 : 255 - (this.selection?.[i] ?? 0);
        this.selection = mask;
      }
    } else if (action.type === "reset") { this.pixels = this.original; this.selection = null; }
    else if (action.type === "fill" || action.type === "delete") {
      if (action.type === "delete" && !this.selection) throw new Error("请先框选要清除的区域，或使用橡皮擦");
      const rgb = action.type === "fill" ? parseColor(action.color) : [0, 0, 0];
      const opacity = action.type === "fill" ? clamp(action.opacity, 0, 100) / 100 : 1;
      if (this.selectedCount === 0 || !opacity) return;
      this.pixels = this.pixels.slice();
      for (let i = 0; i < this.width * this.height; i++) {
        const amount = opacity * (this.selection?.[i] ?? 255) / 255;
        if (amount) this.blend(i, amount, rgb, action.type === "delete", before.pixels);
      }
    }
    if (before.pixels !== this.pixels || before.selection !== this.selection) this.remember(before);
  }
  beginStroke(point: Point, options: PaintOptions) {
    if (this.stroke) throw new Error("当前笔画尚未完成");
    const p = this.bounded(point), rgb = parseColor(options.color);
    const before = this.snapshot();
    this.pixels = this.pixels.slice();
    this.stroke = { before, coverage: new Uint8Array(this.width * this.height), last: p, rgb, options: { ...options, size: clamp(options.size, 1, 1000), opacity: clamp(options.opacity, 0, 100) } };
    this.extendStroke([p]);
  }
  extendStroke(points: Point[]) {
    const stroke = this.stroke;
    if (!stroke) return;
    const radius = stroke.options.size / 2;
    for (const value of points) {
      const p = this.bounded(value), a = stroke.last;
      const dx = p.x - a.x, dy = p.y - a.y, length = dx * dx + dy * dy;
      for (let y = Math.max(0, Math.floor(Math.min(a.y, p.y) - radius)); y <= Math.min(this.height - 1, Math.ceil(Math.max(a.y, p.y) + radius)); y++) {
        for (let x = Math.max(0, Math.floor(Math.min(a.x, p.x) - radius)); x <= Math.min(this.width - 1, Math.ceil(Math.max(a.x, p.x) + radius)); x++) {
          const t = length ? clamp(((x - a.x) * dx + (y - a.y) * dy) / length, 0, 1) : 0;
          const distance = Math.hypot(x - a.x - t * dx, y - a.y - t * dy);
          const i = y * this.width + x;
          const strength = Math.round(clamp(radius + .5 - distance, 0, 1) * stroke.options.opacity / 100 * (this.selection?.[i] ?? 255));
          if (strength > stroke.coverage[i]) {
            stroke.coverage[i] = strength;
            this.blend(i, strength / 255, stroke.rgb, stroke.options.erase, stroke.before.pixels);
          }
        }
      }
      stroke.last = p;
    }
  }
  endStroke(cancel = false) {
    const stroke = this.stroke;
    if (!stroke) return;
    this.stroke = null;
    if (cancel || !stroke.coverage.some(Boolean)) this.restore(stroke.before);
    else this.remember(stroke.before);
  }
}
