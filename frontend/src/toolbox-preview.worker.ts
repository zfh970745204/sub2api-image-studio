import { toolboxDimensions, type ToolboxOptions } from "./toolbox-api";

function filterValue(options: ToolboxOptions) {
  const filters = [
    `brightness(${Math.max(0, 100 + options.brightness)}%)`,
    `contrast(${Math.max(0, 100 + options.contrast)}%)`,
    `saturate(${Math.max(0, 100 + options.saturation)}%)`,
    options.grayscale ? "grayscale(100%)" : "",
    options.invert ? "invert(100%)" : "",
    options.blur ? `blur(${options.blur}px)` : "",
  ].filter(Boolean);
  return filters.length ? filters.join(" ") : "none";
}

function sharpen(canvas: OffscreenCanvas, amount: number) {
  if (!amount) return;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) return;
  const image = ctx.getImageData(0, 0, canvas.width, canvas.height);
  const source = new Uint8ClampedArray(image.data);
  const strength = Math.min(0.8, amount * 0.16);
  for (let y = 1; y < canvas.height - 1; y++) for (let x = 1; x < canvas.width - 1; x++) {
    const offset = (y * canvas.width + x) * 4;
    for (let channel = 0; channel < 3; channel++) {
      const center = source[offset + channel];
      const neighbours = source[offset - 4 + channel] + source[offset + 4 + channel] + source[offset - canvas.width * 4 + channel] + source[offset + canvas.width * 4 + channel];
      image.data[offset + channel] = Math.max(0, Math.min(255, center + strength * (4 * center - neighbours)));
    }
  }
  ctx.putImageData(image, 0, 0);
}

function overlayPosition(canvasWidth: number, canvasHeight: number, overlayWidth: number, overlayHeight: number, position: ToolboxOptions["watermark_position"]) {
  const margin = Math.max(12, Math.floor(Math.min(canvasWidth, canvasHeight) / 30));
  const horizontal = { left: margin, center: Math.floor((canvasWidth - overlayWidth) / 2), right: canvasWidth - overlayWidth - margin };
  const vertical = { top: margin, center: Math.floor((canvasHeight - overlayHeight) / 2), bottom: canvasHeight - overlayHeight - margin };
  let row: "top" | "center" | "bottom" = "center", column: "left" | "center" | "right" = "center";
  if (position.includes("-")) [row, column] = position.split("-") as [typeof row, typeof column];
  else if (position === "top" || position === "bottom") row = position;
  else if (position === "left" || position === "right") column = position;
  return [Math.max(0, horizontal[column]), Math.max(0, vertical[row])] as const;
}

function drawWatermark(ctx: OffscreenCanvasRenderingContext2D, bitmap: ImageBitmap | null, width: number, height: number, options: ToolboxOptions) {
  if (options.watermark === "none") return;
  ctx.save();
  ctx.globalAlpha = options.watermark_opacity / 100;
  if (options.watermark === "text") {
    const fontSize = Math.max(12, Math.round(Math.min(width, height) * options.watermark_scale / 250));
    ctx.font = `${fontSize}px sans-serif`;
    ctx.textBaseline = "top";
    const padding = Math.max(6, Math.floor(fontSize / 5));
    const measured = ctx.measureText(options.watermark_text);
    const overlayWidth = measured.width + padding * 2, overlayHeight = fontSize + padding * 2;
    const [x, y] = overlayPosition(width, height, overlayWidth, overlayHeight, options.watermark_position);
    ctx.fillStyle = options.watermark_color;
    ctx.fillText(options.watermark_text, x + padding, y + padding);
  } else if (bitmap) {
    const maxWidth = Math.max(1, width * options.watermark_scale / 100), maxHeight = Math.max(1, height * options.watermark_scale / 100);
    const scale = Math.min(maxWidth / bitmap.width, maxHeight / bitmap.height, 1);
    const overlayWidth = bitmap.width * scale, overlayHeight = bitmap.height * scale;
    const [x, y] = overlayPosition(width, height, overlayWidth, overlayHeight, options.watermark_position);
    ctx.drawImage(bitmap, x, y, overlayWidth, overlayHeight);
  }
  ctx.restore();
}

self.onmessage = async (event: MessageEvent<{ id: number; source: Blob; background: Blob | null; watermark: Blob | null; options: ToolboxOptions }>) => {
  const { id, source, background, watermark, options: o } = event.data;
  let bitmap: ImageBitmap | null = null, backdrop: ImageBitmap | null = null, watermarkBitmap: ImageBitmap | null = null;
  try {
    bitmap = await createImageBitmap(source);
    if (o.watermark === "image") {
      if (!watermark) throw new Error("请选择水印图片");
      watermarkBitmap = await createImageBitmap(watermark);
    }
    let sx = 0, sy = 0, sw = bitmap.width, sh = bitmap.height;
    if (o.trim) {
      const scale = Math.min(1, 1000 / Math.max(sw, sh));
      const scan = new OffscreenCanvas(Math.max(1, Math.round(sw * scale)), Math.max(1, Math.round(sh * scale)));
      const ctx = scan.getContext("2d", { willReadFrequently: true })!;
      ctx.drawImage(bitmap, 0, 0, scan.width, scan.height);
      const pixels = ctx.getImageData(0, 0, scan.width, scan.height).data;
      let left = scan.width, top = scan.height, right = -1, bottom = -1;
      for (let y = 0; y < scan.height; y++) for (let x = 0; x < scan.width; x++) if (pixels[(y * scan.width + x) * 4 + 3]) { left = Math.min(left, x); top = Math.min(top, y); right = Math.max(right, x); bottom = Math.max(bottom, y); }
      if (right < 0) throw new Error("图片完全透明，无法裁边");
      sx = left / scale; sy = top / scale; sw = (right - left + 1) / scale; sh = (bottom - top + 1) / scale;
    }
    const [width, height] = toolboxDimensions(sw, sh, o);
    if (!Number.isFinite(width * height) || width < 1 || height < 1 || width * height > 16_000_000 || Math.max(width, height) > 12000) throw new Error("输出尺寸超过限制，请调整尺寸或留白");
    const scale = Math.min(1, 1000 / Math.max(width, height));
    const canvas = new OffscreenCanvas(Math.max(1, Math.round(width * scale)), Math.max(1, Math.round(height * scale)));
    const ctx = canvas.getContext("2d")!;
    ctx.scale(canvas.width / width, canvas.height / height);
    if (o.background === "color" || o.format === "jpg") { ctx.fillStyle = o.color; ctx.fillRect(0, 0, width, height); }
    if (o.background === "image" && background) {
      backdrop = await createImageBitmap(background);
      const cover = Math.max(width / backdrop.width, height / backdrop.height);
      ctx.drawImage(backdrop, (width - backdrop.width * cover) / 2, (height - backdrop.height * cover) / 2, backdrop.width * cover, backdrop.height * cover);
    }
    const [dw, dh] = toolboxDimensions(sw, sh, { ...o, rotation: 0, padding: 0 });
    if (o.resize === "fill") {
      const cover = Math.max(dw / sw, dh / sh), cropW = dw / cover, cropH = dh / cover;
      sx += (sw - cropW) / 2; sy += (sh - cropH) / 2; sw = cropW; sh = cropH;
    }
    const foreground = new OffscreenCanvas(canvas.width, canvas.height);
    const foregroundContext = foreground.getContext("2d")!;
    foregroundContext.scale(canvas.width / width, canvas.height / height);
    foregroundContext.filter = filterValue(o);
    foregroundContext.translate(width / 2, height / 2);
    foregroundContext.scale(o.flip_horizontal ? -1 : 1, o.flip_vertical ? -1 : 1);
    foregroundContext.rotate(o.rotation * Math.PI / 180);
    foregroundContext.drawImage(bitmap, sx, sy, sw, sh, -dw / 2, -dh / 2, dw, dh);
    sharpen(foreground, o.sharpen);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.drawImage(foreground, 0, 0);
    ctx.setTransform(canvas.width / width, 0, 0, canvas.height / height, 0, 0);
    drawWatermark(ctx, watermarkBitmap, width, height, o);
    self.postMessage({ id, blob: await canvas.convertToBlob(), width, height });
  } catch (error) { self.postMessage({ id, error: error instanceof Error ? error.message : "布局预览失败" }); }
  finally { bitmap?.close(); backdrop?.close(); watermarkBitmap?.close(); }
};
