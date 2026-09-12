import { BackgroundSelection, type SelectionAction } from "./background-selection";

export type SelectionCommand =
  | { type: "init"; source: Blob; result: Blob; width: number; height: number; hasSelection: boolean }
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
let chain = Promise.resolve();
const scope = self as unknown as { onmessage: (event: MessageEvent<SelectionCommand>) => void; postMessage: (data: SelectionReply, transfer?: Transferable[]) => void };

async function decode(blob: Blob, width: number, height: number) {
  const bitmap = await createImageBitmap(blob);
  try {
    if (bitmap.width !== width || bitmap.height !== height) throw new Error("修边原图与结果尺寸不一致");
    const canvas = new OffscreenCanvas(width, height);
    const ctx = canvas.getContext("2d")!;
    ctx.drawImage(bitmap, 0, 0);
    return ctx.getImageData(0, 0, width, height).data;
  } finally { bitmap.close(); }
}

function raster(pixels: Uint8ClampedArray, width: number, height: number) {
  const canvas = new OffscreenCanvas(width, height);
  canvas.getContext("2d")!.putImageData(new ImageData(new Uint8ClampedArray(pixels), width, height), 0, 0);
  return canvas;
}

async function run(command: SelectionCommand) {
  let sourceBitmap: ImageBitmap | undefined;
  if (command.type === "init") {
    const { width, height } = command;
    const [source, result] = await Promise.all([decode(command.source, width, height), decode(command.result, width, height)]);
    selection = new BackgroundSelection(width, height, source, result, command.hasSelection);
    sourceBitmap = raster(source, width, height).transferToImageBitmap();
  }
  if (!selection) throw new Error("修边图片尚未载入");
  if (command.type === "action") {
    if (command.action.type === "reset" || command.action.type === "clear") edge = 0;
    selection.apply(command.action);
  }
  if (command.type === "edge") edge = command.value;
  const { pixels, removed, visible } = selection.render(edge);
  const canvas = raster(pixels, selection.width, selection.height);
  if (command.type === "export") {
    if (!visible) throw new Error("不能保存完全透明的图片，请先取消部分选区");
    scope.postMessage({ type: "export", blob: await canvas.convertToBlob({ type: "image/png" }) });
    return;
  }
  const overlay = new Uint8ClampedArray(pixels.length);
  for (let i = 0; i < pixels.length; i += 4) {
    overlay[i] = 244; overlay[i + 1] = 63; overlay[i + 2] = 94;
    overlay[i + 3] = Math.round(Math.max(0, selection.source[i + 3] - pixels[i + 3]) * .55);
  }
  const preview = canvas.transferToImageBitmap();
  const overlayBitmap = raster(overlay, selection.width, selection.height).transferToImageBitmap();
  const transfer: Transferable[] = [preview, overlayBitmap];
  if (sourceBitmap) transfer.push(sourceBitmap);
  scope.postMessage({ type: "frame", preview, overlay: overlayBitmap, source: sourceBitmap, canUndo: selection.canUndo, canRedo: selection.canRedo, canRetune: selection.canRetune, removed, visible }, transfer);
}

scope.onmessage = ({ data }) => {
  chain = chain.then(() => run(data)).catch((reason: unknown) => {
    scope.postMessage({ type: "error", message: reason instanceof Error ? reason.message : "选区处理失败，请重试" });
  });
};
