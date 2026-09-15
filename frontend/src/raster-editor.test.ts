import { describe, expect, it } from "vitest";
import { RasterEditor, type Point } from "./raster-editor";

function fixture(rows: number[][]) {
  return new RasterEditor(rows[0].length, rows.length, new Uint8ClampedArray(rows.flat().flatMap((value) => [value, value, value, 255])));
}
function pixel(model: RasterEditor, x: number, y: number) { return [...model.pixels.subarray((y * model.width + x) * 4, (y * model.width + x + 1) * 4)]; }
function rectangle(model: RasterEditor, from: Point, to: Point, mode: "replace" | "add" | "subtract" | "intersect" = "replace") {
  model.apply({ type: "shape", shape: "rectangle", points: [from, to], mode });
}
const brush = { color: "#FF0000", opacity: 50, size: 1, erase: false };

describe("raster editor pixels", () => {
  it("fills only selected pixels, supports selection subtraction, and never treats an empty selection as the whole image", () => {
    const model = fixture([[100,100,100], [100,100,100], [100,100,100]]);
    rectangle(model, { x: 0, y: 0 }, { x: 1, y: 1 });
    rectangle(model, { x: 1, y: 1 }, { x: 1, y: 1 }, "subtract");
    model.apply({ type: "fill", color: "#336699", opacity: 100 });
    expect(pixel(model, 0, 0)).toEqual([51,102,153,255]);
    expect(pixel(model, 1, 1)).toEqual([100,100,100,255]);
    expect(pixel(model, 2, 0)).toEqual([100,100,100,255]);
    rectangle(model, { x: 0, y: 0 }, { x: 2, y: 2 }, "subtract");
    expect(model.selectedCount).toBe(0);
    const before = model.pixels;
    model.apply({ type: "fill", color: "#000000", opacity: 100 });
    expect(model.pixels).toBe(before);
    model.apply({ type: "selection", operation: "none" });
    model.apply({ type: "fill", color: "#000000", opacity: 100 });
    expect(pixel(model, 2, 2)).toEqual([0,0,0,255]);
  });

  it("combines ellipse, reverse rectangle, lasso, and inverted selections without row overflow", () => {
    const model = fixture(Array.from({ length: 5 }, () => [0,0,0,0,0]));
    model.apply({ type: "shape", shape: "ellipse", points: [{x:0,y:0},{x:4,y:4}], mode: "replace" });
    expect(model.selection![0]).toBe(0); expect(model.selection![12]).toBe(255);
    rectangle(model, {x:2,y:2}, {x:0,y:0}, "intersect");
    expect(model.selection![12]).toBe(255); expect(model.selection![13]).toBe(0);
    rectangle(model, {x:4,y:4}, {x:4,y:4}, "add");
    expect(model.selection![24]).toBe(255);
    model.apply({ type: "shape", shape: "lasso", points: [{x:0,y:0},{x:4,y:0},{x:0,y:4}], mode: "replace" });
    expect(model.selection![6]).toBe(255); expect(model.selection![18]).toBe(0);
    model.apply({ type: "selection", operation: "invert" });
    expect(model.selection![6]).toBe(0); expect(model.selection![18]).toBe(255);
  });

  it("magic wand distinguishes enclosed holes from exterior and selects them globally only when requested", () => {
    const model = fixture([[255,255,255,255,255],[255,0,0,0,255],[255,0,255,0,255],[255,0,0,0,255],[255,255,255,255,255]]);
    model.apply({ type: "wand", point: {x:0,y:0}, tolerance: 0, contiguous: true, mode: "replace" });
    expect(model.selectedCount).toBe(16); expect(model.selection![12]).toBe(0);
    model.apply({ type: "wand", point: {x:0,y:0}, tolerance: 0, contiguous: false, mode: "replace" });
    expect(model.selectedCount).toBe(17); expect(model.selection![12]).toBe(255);
    model.apply({ type: "delete" });
    expect(pixel(model, 2, 2)[3]).toBe(0); expect(pixel(model, 1, 2)[3]).toBe(255);
  });

  it("wand tolerance does not cross a different color or select transparent hidden RGB", () => {
    const data = new Uint8ClampedArray([255,255,255,255, 244,244,244,255, 100,100,100,255, 255,255,255,0]);
    const model = new RasterEditor(4, 1, data);
    model.apply({ type: "wand", point: {x:0,y:0}, tolerance: 5, contiguous: false, mode: "replace" });
    expect([...model.selection!]).toEqual([255,255,0,0]);
    model.apply({ type: "wand", point: {x:3,y:0}, tolerance: 100, contiguous: false, mode: "replace" });
    expect([...model.selection!]).toEqual([0,0,0,255]);
  });

  it("composites partial fill into transparent pixels and samples the untouched original separately", () => {
    const original = new Uint8ClampedArray([0,0,255,255, 20,90,80,0]);
    const model = new RasterEditor(2, 1, original);
    model.apply({ type: "fill", color: "#FF0000", opacity: 50 });
    expect(pixel(model, 0, 0)).toEqual([128,0,128,255]);
    expect(pixel(model, 1, 0)).toEqual([255,0,0,128]);
    expect(model.sample({x:0,y:0}, true)).toBe("#0000FF");
    expect(model.sample({x:0,y:0}, false)).toBe("#800080");
    expect(() => model.sample({x:1,y:0}, true)).toThrow("透明区域");
    expect([...original]).toEqual([0,0,255,255,20,90,80,0]);
  });

  it("continuous brush opacity is independent of repeated pointer samples and the entire stroke undoes once", () => {
    const run = (points: Point[]) => {
      const model = fixture([[0,0,0,0,0]]);
      model.beginStroke({x:0,y:0}, brush); model.extendStroke(points); model.endStroke(); return model;
    };
    const sparse = run([{x:4,y:0}]), dense = run([{x:1,y:0},{x:2,y:0},{x:2,y:0},{x:3,y:0},{x:4,y:0}]);
    expect(sparse.pixels).toEqual(dense.pixels);
    for (let x = 0; x < 5; x++) expect(pixel(sparse, x, 0)).toEqual([128,0,0,255]);
    sparse.apply({ type: "undo" }); expect(sparse.pixels).toEqual(sparse.original); expect(sparse.canUndo).toBe(false);
    sparse.apply({ type: "redo" }); expect(sparse.pixels).toEqual(dense.pixels);
  });

  it("eraser respects selection and original alpha; cancelled strokes restore pixels and history", () => {
    const model = new RasterEditor(3, 1, new Uint8ClampedArray([20,30,40,128, 20,30,40,128, 20,30,40,128]));
    rectangle(model, {x:1,y:0}, {x:1,y:0});
    model.beginStroke({x:0,y:0}, {...brush, size:5, erase:true}); model.extendStroke([{x:2,y:0}]); model.endStroke();
    expect(pixel(model, 0, 0)[3]).toBe(128); expect(pixel(model, 1, 0)[3]).toBe(64);
    const before = model.pixels;
    model.beginStroke({x:1,y:0}, {...brush, opacity:100}); model.endStroke(true);
    expect(model.pixels).toBe(before);
    model.apply({ type: "undo" }); expect(pixel(model, 1, 0)[3]).toBe(128);
    model.apply({ type: "redo" }); expect(pixel(model, 1, 0)[3]).toBe(64);
    model.apply({ type: "reset" }); expect(model.changed).toBe(false);
    model.apply({ type: "undo" }); expect(model.pixels).toBe(before);
  });

  it("new operations clear stale redo, history is bounded, and invalid input leaves the image untouched", () => {
    const model = fixture([[0]]);
    expect(() => model.apply({ type: "delete" })).toThrow("先框选");
    expect(() => model.apply({ type: "fill", color:"bad", opacity:100 })).toThrow("颜色");
    expect(model.changed).toBe(false);
    for (let i = 0; i < 40; i++) model.apply({ type:"fill", color: i % 2 ? "#112233" : "#FF0000", opacity:100 });
    for (let i = 0; i < 30; i++) model.apply({ type:"undo" });
    expect(model.canUndo).toBe(false); expect(model.canRedo).toBe(true);
    rectangle(model, {x:0,y:0}, {x:0,y:0}); expect(model.canRedo).toBe(false);
    expect(() => new RasterEditor(4001,4000,new Uint8ClampedArray())).toThrow("1600");
  });
});

describe("layers and paint bucket", () => {
  const bucket = { type:"bucket" as const, point:{x:0,y:0}, color:"#FF0000", opacity:100, tolerance:0, contiguous:true, sampleMerged:true };
  it("paints on a separate layer using the merged boundary, preserving source and enclosed holes", () => {
    const model = fixture([[255,0,255],[255,0,255],[255,0,255]]);
    model.apply({type:"layer", operation:"add"});
    model.apply(bucket);
    expect(pixel(model,0,0)).toEqual([255,0,0,255]);
    expect(pixel(model,2,0)).toEqual([255,255,255,255]);
    expect(model.layers[0].pixels).toEqual(model.original);
    expect(model.activeLayer.pixels[2*4+3]).toBe(0);
    model.apply({type:"layer", operation:"opacity", value:50});
    expect(pixel(model,0,0)).toEqual([255,128,128,255]);
    model.apply({type:"layer", operation:"visible"});
    expect(model.pixels).toEqual(model.original);
    model.apply({type:"undo"}); expect(pixel(model,0,0)).toEqual([255,128,128,255]);
    model.apply({type:"layer", operation:"down"}); expect(model.pixels).toEqual(model.original);
    model.apply({type:"undo"}); expect(pixel(model,0,0)).toEqual([255,128,128,255]);
    model.apply({type:"layer", operation:"locked"});
    expect(() => model.apply(bucket)).toThrow("锁定");
    expect(() => model.beginStroke({x:0,y:0},brush)).toThrow("锁定");
  });
  it("marquee selects only active-layer pixels, excludes interior holes and preserves partial alpha", () => {
    const pixels = new Uint8ClampedArray([5,6,7,0, 10,20,30,128, 10,20,30,255, 50,60,70,0]);
    const model = new RasterEditor(4,1,pixels);
    rectangle(model,{x:0,y:0},{x:3,y:0});
    expect([...model.selection!]).toEqual([0,255,255,0]);
    model.apply({type:"delete"}); expect([...model.pixels.filter((_,i)=>i%4===3)]).toEqual([0,0,0,0]);
    model.apply({type:"undo"}); expect(model.pixels).toEqual(pixels);
    model.apply({type:"layer", operation:"add"});
    rectangle(model,{x:0,y:0},{x:3,y:0}); expect(model.selectedCount).toBe(0);
    model.apply(bucket); expect(model.activeLayer.pixels.every((x)=>!x)).toBe(true);
    model.apply({type:"shape",shape:"rectangle",points:[{x:0,y:0},{x:3,y:0}],mode:"replace",contentOnly:false});
    model.apply({...bucket,sampleMerged:false}); expect(model.activeLayer.pixels[3]).toBe(255);
    model.apply({type:"layer",operation:"select",id:"base"}); expect(model.selection).toBeNull();
    model.apply({type:"layer",operation:"content"}); expect([...model.selection!]).toEqual([0,255,255,0]);
  });
  it("bucket tolerance, global matching and selection boundaries never spill into unselected islands", () => {
    const model = fixture([[255,247,100,255,255]]);
    model.apply({...bucket,tolerance:4});
    expect(pixel(model,1,0)).toEqual([255,0,0,255]); expect(pixel(model,3,0)).toEqual([255,255,255,255]);
    model.apply({type:"undo"});
    model.apply({...bucket,contiguous:false,tolerance:4}); expect(pixel(model,4,0)).toEqual([255,0,0,255]);
    const restricted = fixture([[255,255,255,255,255]]);
    rectangle(restricted,{x:0,y:0},{x:1,y:0}); rectangle(restricted,{x:3,y:0},{x:4,y:0},"add");
    restricted.apply(bucket); expect(pixel(restricted,3,0)).toEqual([255,255,255,255]);
    const before = restricted.pixels;
    restricted.apply({...bucket,point:{x:2,y:0},contiguous:false}); expect(restricted.pixels).toBe(before);
    restricted.apply({...bucket,point:{x:3,y:0}}); expect(pixel(restricted,4,0)).toEqual([255,0,0,255]);
    expect(pixel(restricted,2,0)).toEqual([255,255,255,255]);
  });
  it("duplicates use independent pixels, structural edits undo, and reopening restores the document", () => {
    const model = fixture([[0,0,0]]);
    model.apply({type:"layer",operation:"duplicate"});
    model.apply({type:"layer",operation:"rename",name:"色彩"});
    model.beginStroke({x:1,y:0},{...brush,opacity:100}); model.endStroke();
    expect([...model.layers[0].pixels]).toEqual([...model.original]);
    expect(pixel(model,1,0)).toEqual([255,0,0,255]);
    const reopened = new RasterEditor(3,1,model.pixels,model.layers,model.activeId);
    expect(reopened.changed).toBe(false); expect(reopened.activeLayer.name).toBe("色彩");
    reopened.apply({type:"layer",operation:"remove"}); expect(reopened.layers).toHaveLength(1);
    reopened.apply({type:"undo"}); expect(reopened.activeLayer.name).toBe("色彩"); expect(reopened.changed).toBe(false);
    reopened.apply({type:"redo"}); expect(reopened.layers).toHaveLength(1);
    expect(() => reopened.apply({type:"layer",operation:"remove"})).toThrow("至少");
    reopened.apply({type:"reset"}); expect(reopened.layers).toHaveLength(2); expect(reopened.changed).toBe(false);
    for (let i=2;i<12;i++) reopened.apply({type:"layer",operation:"add"});
    expect(() => reopened.apply({type:"layer",operation:"add"})).toThrow("最多");
  });
  it("invalidates the merged preview during a stroke and can cancel without affecting other layers", () => {
    const model = fixture([[0,0,0]]); model.apply({type:"layer",operation:"add"});
    const original = model.pixels;
    model.beginStroke({x:0,y:0},{...brush,opacity:100});
    expect(pixel(model,0,0)).toEqual([255,0,0,255]);
    model.extendStroke([{x:2,y:0}]); expect(pixel(model,2,0)).toEqual([255,0,0,255]);
    model.endStroke(true); expect(model.pixels).toBe(original);
  });
});
