import { waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SelectionCommand, SelectionReply } from "./background-selection.worker";

class TestImageData {
  data: Uint8ClampedArray;
  constructor(readonly width: number, readonly height: number) { this.data = new Uint8ClampedArray(width * height * 4); }
}
type TestBitmap = ImageBitmap & { pixels: Uint8ClampedArray };
function bitmap(width: number, height: number, pixels: Uint8ClampedArray): TestBitmap {
  return { width, height, pixels: pixels.slice(), close: vi.fn() } as unknown as TestBitmap;
}
class TestCanvas {
  static instances: TestCanvas[] = [];
  pixels: Uint8ClampedArray;
  context = {
    drawImage: vi.fn((image: TestBitmap) => { this.pixels.set(image.pixels); }),
    getImageData: vi.fn(() => ({ data: this.pixels.slice() })),
    putImageData: vi.fn((image: TestImageData) => { this.pixels.set(image.data); }),
  };
  getContext() { return this.context; }
  transferToImageBitmap() {
    const image = bitmap(this.width, this.height, this.pixels);
    this.pixels.fill(0); // Real transferToImageBitmap leaves a blank backing store.
    return image;
  }
  convertToBlob = vi.fn(async (options: { type: string }) => new Blob([this.pixels.slice()], { type: options.type }));
  constructor(readonly width: number, readonly height: number) {
    this.pixels = new Uint8ClampedArray(width * height * 4);
    TestCanvas.instances.push(this);
  }
}

describe("background selection worker", () => {
  let scope: { onmessage: ((event: { data: SelectionCommand }) => void) | null; postMessage: ReturnType<typeof vi.fn> };
  let decode: ReturnType<typeof vi.fn>;
  const source = new Uint8ClampedArray([220,180,40,255, 240,230,210,255, 10,30,40,0]);
  const sourceBlob = new Blob(["source"]);

  beforeEach(async () => {
    vi.resetModules();
    TestCanvas.instances = [];
    scope = { onmessage: null, postMessage: vi.fn() };
    decode = vi.fn(async () => bitmap(3, 1, source));
    vi.stubGlobal("self", scope);
    vi.stubGlobal("createImageBitmap", decode);
    vi.stubGlobal("OffscreenCanvas", TestCanvas);
    vi.stubGlobal("ImageData", TestImageData);
    await import("./background-selection.worker");
  });

  async function send(data: SelectionCommand) {
    const count = scope.postMessage.mock.calls.length;
    scope.onmessage!({ data });
    await waitFor(() => expect(scope.postMessage).toHaveBeenCalledTimes(count + 1));
    return scope.postMessage.mock.calls.at(-1)![0] as SelectionReply;
  }

  it.each([false, true])("decodes identical source/result once (explicit shared blob: %s) and preserves original pixels for restore", async (explicitResult) => {
    const first = await send({ type: "init", source: sourceBlob, result: explicitResult ? sourceBlob : undefined, width: 3, height: 1, hasSelection: true });
    expect(decode).toHaveBeenCalledTimes(1);
    expect(first.type).toBe("frame");
    if (first.type !== "frame") throw new Error("Missing frame");
    expect(first.source).toBe(await decode.mock.results[0].value);
    expect(scope.postMessage.mock.calls[0][1]).toContain(first.source);
    expect(first.source!.close).not.toHaveBeenCalled();
    const edited = await send({ type: "action", action: { type: "wand", x: 0, y: 0, tolerance: 0, contiguous: true, mode: "remove" } });
    expect(edited).toMatchObject({ type: "frame", removed: 1, visible: 1 });
    const restored = await send({ type: "action", action: { type: "wand", x: 0, y: 0, tolerance: 0, contiguous: true, mode: "restore" } });
    if (restored.type !== "frame") throw new Error("Missing frame");
    expect((restored.preview as TestBitmap).pixels).toEqual(source);
    expect(restored.source).toBeUndefined();
  });

  it("reuses frame buffers and exports the latest full-resolution RGBA while preserving distinct result edge colors", async () => {
    const resultPixels = new Uint8ClampedArray([0,0,0,0, 210,190,100,128, 0,0,0,0]);
    const resultBitmap = bitmap(3, 1, resultPixels);
    decode.mockResolvedValueOnce(bitmap(3, 1, source)).mockResolvedValueOnce(resultBitmap);
    const { BackgroundSelection } = await import("./background-selection");
    const render = vi.spyOn(BackgroundSelection.prototype, "render");
    await send({ type: "init", source: sourceBlob, result: new Blob(["result"]), width: 3, height: 1, hasSelection: true });
    expect(decode).toHaveBeenCalledTimes(2);
    expect(resultBitmap.close).toHaveBeenCalledOnce();
    const restored = await send({ type: "action", action: { type: "wand", x: 0, y: 0, tolerance: 0, contiguous: true, mode: "restore" } });
    if (restored.type !== "frame") throw new Error("Missing frame");
    const expected = new Uint8ClampedArray([220,180,40,255, 210,190,100,128, 0,0,0,0]);
    expect((restored.preview as TestBitmap).pixels).toEqual(expected);
    expect(render.mock.calls[0][1]).toBe(render.mock.calls[1][1]);
    const exported = await send({ type: "export" });
    expect(exported).toMatchObject({ type: "export", blob: expect.any(Blob) });
    expect(render).toHaveBeenCalledTimes(2); // Saving reuses the last rendered pixels.
    const canvas = TestCanvas.instances.find((value) => value.convertToBlob.mock.calls.length)!;
    expect([canvas.width, canvas.height]).toEqual([3, 1]);
    expect(canvas.pixels).toEqual(expected);
    expect(canvas.convertToBlob).toHaveBeenCalledWith({ type: "image/png" });
    expect(canvas.context.putImageData.mock.calls[0][0]).toBe(canvas.context.putImageData.mock.calls[1][0]);
  });

  it("exports the current edge shrink and blocks fully transparent output until undo", async () => {
    await send({ type: "init", source: sourceBlob, width: 3, height: 1, hasSelection: true });
    const shrunk = await send({ type: "edge", value: 1 });
    if (shrunk.type !== "frame") throw new Error("Missing frame");
    expect(shrunk.visible).toBe(1);
    await send({ type: "export" });
    const canvas = TestCanvas.instances.find((value) => value.convertToBlob.mock.calls.length)!;
    expect(canvas.pixels).toEqual((shrunk.preview as TestBitmap).pixels);
    await send({ type: "action", action: { type: "wand", x: 0, y: 0, tolerance: 100, contiguous: false, mode: "remove" } });
    expect(await send({ type: "export" })).toMatchObject({ type: "error", message: expect.stringContaining("完全透明") });
    await send({ type: "action", action: { type: "undo" } });
    expect(await send({ type: "export" })).toMatchObject({ type: "export" });
  });

  it("closes both decoded bitmaps after a dimension mismatch and can retry initialization", async () => {
    const good = bitmap(3, 1, source), bad = bitmap(1, 1, source.subarray(0, 4));
    decode.mockResolvedValueOnce(good).mockResolvedValueOnce(bad);
    expect(await send({ type: "init", source: sourceBlob, result: new Blob(["wrong-size"]), width: 3, height: 1, hasSelection: true })).toMatchObject({ type: "error", message: expect.stringContaining("尺寸不一致") });
    expect(good.close).toHaveBeenCalledOnce();
    expect(bad.close).toHaveBeenCalledOnce();
    expect(await send({ type: "init", source: sourceBlob, width: 3, height: 1, hasSelection: true })).toMatchObject({ type: "frame" });
  });
});
