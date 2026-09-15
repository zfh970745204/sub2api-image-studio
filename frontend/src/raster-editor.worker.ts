import { RasterEditor, layerLimit, type LayerInfo, type RasterLayer, type PaintOptions, type Point, type RasterAction } from "./raster-editor";
import { decodeProject, encodeProject } from "./raster-project";

export type RasterCommand =
  | { type: "init"; blob: Blob; width: number; height: number; project?: Blob }
  | { type: "action"; action: RasterAction }
  | { type: "stroke-begin"; point: Point; options: PaintOptions }
  | { type: "stroke-move"; points: Point[] }
  | { type: "stroke-end"; cancel?: boolean }
  | { type: "sample"; point: Point; original: boolean }
  | { type: "export" };
export type RasterState = { canUndo: boolean; canRedo: boolean; changed: boolean; selected: number | null; visible: number; layers: LayerInfo[]; activeId: string; maxLayers: number };
export type RasterReply =
  | { type: "frame"; preview: ImageBitmap; overlay: ImageBitmap; original?: ImageBitmap; state: RasterState }
  | { type: "sample"; color: string }
  | { type: "export"; blob: Blob; project: Blob }
  | { type: "error"; message: string };

let model: RasterEditor | null = null;
let canvas: OffscreenCanvas;
let preview: OffscreenCanvas;
let mask: OffscreenCanvas;
let imageData: ImageData;
let maskData: ImageData;
let chain = Promise.resolve();
const scope = self as unknown as { onmessage: (event: MessageEvent<RasterCommand>) => void; postMessage: (data: RasterReply, transfer?: Transferable[]) => void };

async function run(command: RasterCommand) {
  let original: ImageBitmap | undefined;
  let rendered: ImageBitmap | undefined;
  let overlay: ImageBitmap | undefined;
  try {
    if (command.type === "init") {
      model = null;
      const { width, height } = command;
      if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1 || width * height > 16_000_000) throw new Error("基础编辑支持最高 1600 万像素");
      const bitmap = await createImageBitmap(command.blob);
      try {
        if (bitmap.width !== width || bitmap.height !== height) throw new Error("原图尺寸不匹配，请重新载入");
        canvas = new OffscreenCanvas(width, height);
        const ctx = canvas.getContext("2d", { willReadFrequently: true })!;
        ctx.drawImage(bitmap, 0, 0);
        imageData = ctx.getImageData(0, 0, width, height);
        model = new RasterEditor(width, height, imageData.data);
        if (command.project) {
          const { manifest, blobs } = await decodeProject(command.project, width, height);
          const layerCanvas = new OffscreenCanvas(width, height), ctx = layerCanvas.getContext("2d", { willReadFrequently: true })!;
          const layers: RasterLayer[] = [];
          for (let i = 0; i < blobs.length; i++) {
            const bitmap = await createImageBitmap(blobs[i]);
            try {
              if (bitmap.width !== width || bitmap.height !== height) throw new Error("图层图片尺寸不匹配");
              ctx.clearRect(0, 0, width, height); ctx.drawImage(bitmap, 0, 0);
              const { byteLength: _, ...info } = manifest.layers[i];
              layers.push({ ...info, pixels: ctx.getImageData(0, 0, width, height).data });
            } finally { bitmap.close(); }
          }
          model = new RasterEditor(width, height, imageData.data, layers, manifest.activeId);
        }
      } finally { bitmap.close(); }
      const scale = Math.min(1, 1400 / Math.max(width, height));
      preview = new OffscreenCanvas(Math.max(1, Math.round(width * scale)), Math.max(1, Math.round(height * scale)));
      mask = new OffscreenCanvas(preview.width, preview.height);
      maskData = new ImageData(preview.width, preview.height);
      preview.getContext("2d")!.drawImage(canvas, 0, 0, preview.width, preview.height);
      original = preview.transferToImageBitmap();
    }
    if (!model) throw new Error("图片尚未载入");
    if (command.type === "sample") { scope.postMessage({ type: "sample", color: model.sample(command.point, command.original) }); return; }
    if (command.type === "action") model.apply(command.action);
    if (command.type === "stroke-begin") model.beginStroke(command.point, command.options);
    if (command.type === "stroke-move") model.extendStroke(command.points);
    if (command.type === "stroke-end") model.endStroke(command.cancel);
    const pixels = model.pixels;
    imageData.data.set(pixels);
    canvas.getContext("2d")!.putImageData(imageData, 0, 0);
    let visible = 0;
    for (let i = 3; i < pixels.length; i += 4) if (pixels[i]) visible++;
    if (command.type === "export") {
      if (model.drawing) throw new Error("请先完成当前笔画");
      if (!visible) throw new Error("图片已完全透明，请撤销或添加内容后再保存");
      const blob = await canvas.convertToBlob({ type: "image/png" }), blobs: Blob[] = [];
      for (const layer of model.layers) {
        imageData.data.set(layer.pixels); canvas.getContext("2d")!.putImageData(imageData, 0, 0);
        blobs.push(await canvas.convertToBlob({ type: "image/png" }));
      }
      const project = encodeProject({ version: 1, width: model.width, height: model.height, activeId: model.activeId, layers: model.layers.map(({ pixels: _, ...layer }, i) => ({ ...layer, byteLength: blobs[i].size })) }, blobs);
      scope.postMessage({ type: "export", blob, project });
      return;
    }
    preview.getContext("2d")!.clearRect(0, 0, preview.width, preview.height);
    preview.getContext("2d")!.drawImage(canvas, 0, 0, preview.width, preview.height);
    for (let y = 0; y < mask.height; y++) for (let x = 0; x < mask.width; x++) {
      const sourceX = Math.min(model.width - 1, Math.floor(x * model.width / mask.width));
      const sourceY = Math.min(model.height - 1, Math.floor(y * model.height / mask.height));
      const i = sourceY * model.width + sourceX, at = (y * mask.width + x) * 4;
      const selected = Boolean(model.selection?.[i]);
      const boundary = selected && (sourceX === 0 || sourceY === 0 || sourceX === model.width - 1 || sourceY === model.height - 1 || !model.selection?.[i - 1] || !model.selection?.[i + 1] || !model.selection?.[i - model.width] || !model.selection?.[i + model.width]);
      maskData.data[at] = 67; maskData.data[at + 1] = 97; maskData.data[at + 2] = 238;
      maskData.data[at + 3] = boundary ? 235 : selected ? 45 : 0;
    }
    mask.getContext("2d")!.putImageData(maskData, 0, 0);
    rendered = preview.transferToImageBitmap(); overlay = mask.transferToImageBitmap();
    const transfer: Transferable[] = [rendered, overlay];
    if (original) transfer.push(original);
    scope.postMessage({ type: "frame", preview: rendered, overlay, original, state: { canUndo: model.canUndo, canRedo: model.canRedo, changed: model.changed, selected: model.selectedCount, visible, activeId: model.activeId, maxLayers: layerLimit(model.width, model.height), layers: model.layers.map(({ pixels: _, ...layer }) => layer) } }, transfer);
    original = rendered = overlay = undefined;
  } finally { original?.close(); rendered?.close(); overlay?.close(); }
}
scope.onmessage = ({ data }) => {
  chain = chain.then(() => run(data)).catch((reason: unknown) => {
    model?.endStroke(true);
    scope.postMessage({ type: "error", message: reason instanceof Error ? reason.message : "编辑失败，请重试" });
  });
};
