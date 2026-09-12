import { toolboxDimensions, type ToolboxOptions } from "./toolbox-api";

self.onmessage = async (event: MessageEvent<{ id: number; source: Blob; background: Blob | null; options: ToolboxOptions }>) => {
  const { id, source, background, options: o } = event.data;
  let bitmap: ImageBitmap | null = null, backdrop: ImageBitmap | null = null;
  try {
    bitmap = await createImageBitmap(source);
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
    ctx.translate(width / 2, height / 2);
    ctx.scale(o.flip_horizontal ? -1 : 1, o.flip_vertical ? -1 : 1);
    ctx.rotate(o.rotation * Math.PI / 180);
    ctx.drawImage(bitmap, sx, sy, sw, sh, -dw / 2, -dh / 2, dw, dh);
    self.postMessage({ id, blob: await canvas.convertToBlob(), width, height });
  } catch (error) { self.postMessage({ id, error: error instanceof Error ? error.message : "布局预览失败" }); }
  finally { bitmap?.close(); backdrop?.close(); }
};
