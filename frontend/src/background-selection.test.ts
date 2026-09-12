import { describe, expect, it } from "vitest";
import { BackgroundSelection } from "./background-selection";

function pixels(rows: number[][]) {
  return new Uint8ClampedArray(rows.flat().flatMap((value) => [value, value, value, 255]));
}
function remove(model: BackgroundSelection, x: number, y: number, tolerance = 0, contiguous = true) {
  model.apply({ type: "wand", x, y, tolerance, contiguous, mode: "remove" });
}

describe("editable background selection", () => {
  it("automatically selects the perimeter while preserving enclosed artwork", () => {
    const source = pixels([[255,255,255,255,255],[255,0,0,0,255],[255,0,255,0,255],[255,0,0,0,255],[255,255,255,255,255]]);
    const model = new BackgroundSelection(5, 5, source, source.slice(), false);
    expect(model.alpha[0]).toBe(0);
    expect(model.alpha[12]).toBe(255);
    remove(model, 2, 2);
    expect(model.alpha[12]).toBe(0);
    expect(model.alpha[11]).toBe(255);
  });

  it("distinguishes connected selection from global same-color selection without row wrapping", () => {
    const source = pixels([[255,0,255],[255,0,255],[0,255,0]]);
    const model = new BackgroundSelection(3, 3, source, source.slice(), true);
    remove(model, 2, 0);
    expect(Array.from(model.alpha)).toEqual([255,255,0,255,255,0,255,255,255]);
    model.apply({ type: "reset" });
    remove(model, 2, 0, 0, false);
    expect(Array.from(model.alpha)).toEqual([0,255,0,0,255,0,255,0,255]);
  });

  it("retunes the last click from its pre-click state, including when tolerance decreases", () => {
    const source = pixels([[255,245,215,0]]);
    const model = new BackgroundSelection(4, 1, source, source.slice(), true);
    remove(model, 0, 0, 20);
    expect(Array.from(model.alpha)).toEqual([0,0,0,255]);
    model.apply({ type: "tolerance", tolerance: 0 });
    expect(Array.from(model.alpha)).toEqual([0,255,255,255]);
    model.apply({ type: "undo" });
    expect(Array.from(model.alpha)).toEqual([255,255,255,255]);
    model.apply({ type: "redo" });
    expect(Array.from(model.alpha)).toEqual([0,255,255,255]);
  });

  it("restores deleted source pixels and retains untouched model edge colors", () => {
    const source = new Uint8ClampedArray([220,180,40,255, 240,230,210,255, 10,30,40,0]);
    const result = new Uint8ClampedArray([0,0,0,0, 210,190,100,128, 0,0,0,0]);
    const model = new BackgroundSelection(3, 1, source, result, true);
    model.apply({ type: "wand", x: 0, y: 0, tolerance: 0, contiguous: true, mode: "restore" });
    expect(Array.from(model.render().pixels)).toEqual([220,180,40,255, 210,190,100,128, 0,0,0,0]);
    model.apply({ type: "clear" });
    expect(Array.from(model.alpha)).toEqual([255,255,0]);
    model.apply({ type: "reset" });
    expect(Array.from(model.alpha)).toEqual([0,128,0]);
  });

  it("lets an over-selected foreground be recovered with undo and drops stale redo on a new click", () => {
    const source = pixels([[255,50,0]]);
    const model = new BackgroundSelection(3, 1, source, source.slice(), true);
    remove(model, 0, 0, 100);
    expect(model.render().visible).toBe(0);
    model.apply({ type: "undo" });
    expect(model.render().visible).toBe(3);
    remove(model, 2, 0);
    expect(model.canRedo).toBe(false);
    expect(model.canRetune).toBe(true);
    model.apply({ type: "clear" });
    expect(model.canRetune).toBe(false);
  });

  it("shrinks a thin rim reversibly without changing the saved selection or canvas dimensions", () => {
    const source = pixels(Array.from({ length: 5 }, () => [255,255,255,255,255]));
    const result = source.slice();
    for (let y = 0; y < 5; y++) for (let x = 0; x < 5; x++) if (x === 0 || y === 0 || x === 4 || y === 4) result[(y * 5 + x) * 4 + 3] = 0;
    const model = new BackgroundSelection(5, 5, source, result, true);
    expect(model.render(1).visible).toBe(1);
    expect(model.render(0).visible).toBe(9);
    expect(model.render().pixels).toHaveLength(source.length);
  });

  it("does not guess a perimeter color for a varied photo and never fills native transparent pixels", () => {
    const source = pixels([[0,55,110],[165,50,220],[35,95,155]]);
    source[3] = 0;
    const model = new BackgroundSelection(3, 3, source, source.slice(), false);
    expect(model.render().visible).toBe(8);
    model.apply({ type: "wand", x: 0, y: 0, tolerance: 100, contiguous: false, mode: "restore" });
    expect(model.alpha[0]).toBe(0);
  });

  it("safely shares identical source/result pixels and reuses a full-size render buffer across edits and undo", () => {
    const source = new Uint8ClampedArray([220,180,40,255, 10,30,40,128, 0,0,0,0]);
    const original = source.slice();
    const model = new BackgroundSelection(3, 1, source, source, true);
    const buffer = new Uint8ClampedArray(source.length);
    remove(model, 0, 0);
    const edited = model.render(0, buffer);
    expect(edited.pixels).toBe(buffer);
    expect(Array.from(buffer)).toEqual([220,180,40,0, 10,30,40,128, 0,0,0,0]);
    expect(source).toEqual(original);
    model.apply({ type: "undo" });
    expect(model.render(0, buffer).pixels).toBe(buffer);
    expect(buffer).toEqual(original);
    model.apply({ type: "redo" });
    model.apply({ type: "wand", x: 0, y: 0, tolerance: 0, contiguous: true, mode: "restore" });
    expect(model.render(0, buffer).pixels).toEqual(original);
    expect(source).toEqual(original);
    expect(() => model.render(0, source)).toThrow("缓冲区");
    expect(() => model.render(0, new Uint8ClampedArray(source.buffer))).toThrow("缓冲区");
    expect(() => model.render(0, new Uint8ClampedArray(4))).toThrow("缓冲区");
  });
});
