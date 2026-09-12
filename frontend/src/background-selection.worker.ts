import { BackgroundSelection, type SelectionAction } from "./background-selection";

export type SelectionCommand =
  // An absent result means the same full-resolution image as source.
  | { type: "init"; source: Blob; result?: Blob; width: number; height: number; hasSelection: boolean }
  | { type: "action"; action: SelectionAction }
  | { type: "edge"; value: number }
  | { type: "export" };
export type SelectionFrame = {
  type: "frame"; preview: ImageBitmap; overlay: ImageBitmap; source?: ImageBitmap;
  canUndo: boolean; canRedo: boolean; canRetune: boolean; removed: number; visible: number;
};
export type SelectionReply = SelectionFrame | { type: "export"; blob: Blob } | { type: "error"; message: string };

let selection: BackgroundSelection | null = null;
let edge = 0;
let previewImage: ImageData;
let overlayImage: ImageData;
let previewCanvas: OffscreenCanvas;
let overlayCanvas: OffscreenCanvas;
let visiblePixels = 0;
let chain = Promise.resolve();
const scope = self as unknown as { onmessage: (event: MessageEvent<SelectionCommand>) => void; postMessage: (data: SelectionReply, transfer?: Transferable[]) => void };

async function decode(blob: Blob, width: number, height: number) {
  const bitmap = await createImageBitmap(blob);
  try {
    if (bitmap.width !== width || bitmap.height !== height) throw new Error("修边原图与结果尺寸不一致");
    const canvas = new OffscreenCanvas(width, height);
    const ctx = canvas.getContext("2d")!;
    ctx.drawImage(bitmap, 0, 0);
    return { pixels: ctx.getImageData(0, 0, width, height).data, bitmap };
  } catch (reason) { bitmap.close(); throw reason; }
}

function raster(image: ImageData, canvas: OffscreenCanvas) {
  canvas.getContext("2d")!.putImageData(image, 0, 0);
  return canvas;
}

async function run(command: SelectionCommand) {
  let sourceBitmap: ImageBitmap | undefined;
  let preview: ImageBitmap | undefined;
  let overlayBitmap: ImageBitmap | undefined;
  try {
    if (command.type === "init") {
      const { width, height } = command;
      selection = null; edge = 0; visiblePixels = 0;
      if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1 || width * height > 16_000_000) throw new Error("修边图片尺寸超出支持范围");
      const [source, result] = await Promise.allSettled([
        decode(command.source, width, height),
        command.result && command.result !== command.source ? decode(command.result, width, height) : Promise.resolve(null),
      ]);
      if (source.status === "rejected" || result.status === "rejected") {
        if (source.status === "fulfilled") source.value.bitmap.close();
        if (result.status === "fulfilled") result.value?.bitmap.close();
        throw source.status === "rejected" ? source.reason : (result as PromiseRejectedResult).reason;
      }
      sourceBitmap = source.value.bitmap;
      result.value?.bitmap.close();
      selection = new BackgroundSelection(width, height, source.value.pixels, result.value?.pixels ?? source.value.pixels, command.hasSelection);
      // Reuse full-size render buffers. ImageData owns them, so rasterization does
      // not need another RGBA copy on every frame; source uses its decoded bitmap.
      previewImage = new ImageData(width, height);
      overlayImage = new ImageData(width, height);
      previewCanvas = new OffscreenCanvas(width, height);
      overlayCanvas = new OffscreenCanvas(width, height);
    }
    if (!selection) throw new Error("修边图片尚未载入");
    if (command.type === "export") {
      if (!visiblePixels) throw new Error("不能保存完全透明的图片，请先取消部分选区");
      scope.postMessage({ type: "export", blob: await raster(previewImage, previewCanvas).convertToBlob({ type: "image/png" }) });
      return;
    }
    if (command.type === "action") {
      if (command.action.type === "reset" || command.action.type === "clear") edge = 0;
      selection.apply(command.action);
    }
    if (command.type === "edge") edge = command.value;
    const { pixels, removed, visible } = selection.render(edge, previewImage.data);
    visiblePixels = visible;
    const overlay = overlayImage.data;
    for (let i = 0; i < pixels.length; i += 4) {
      overlay[i] = 244; overlay[i + 1] = 63; overlay[i + 2] = 94;
      overlay[i + 3] = Math.round(Math.max(0, selection.source[i + 3] - pixels[i + 3]) * .55);
    }
    preview = raster(previewImage, previewCanvas).transferToImageBitmap();
    overlayBitmap = raster(overlayImage, overlayCanvas).transferToImageBitmap();
    const transfer: Transferable[] = [preview, overlayBitmap];
    if (sourceBitmap) transfer.push(sourceBitmap);
    scope.postMessage({ type: "frame", preview, overlay: overlayBitmap, source: sourceBitmap, canUndo: selection.canUndo, canRedo: selection.canRedo, canRetune: selection.canRetune, removed, visible }, transfer);
    sourceBitmap = preview = overlayBitmap = undefined; // Ownership was transferred to the editor.
  } finally { sourceBitmap?.close(); preview?.close(); overlayBitmap?.close(); }
}

scope.onmessage = ({ data }) => {
  chain = chain.then(() => run(data)).catch((reason: unknown) => {
    scope.postMessage({ type: "error", message: reason instanceof Error ? reason.message : "选区处理失败，请重试" });
  });
};
