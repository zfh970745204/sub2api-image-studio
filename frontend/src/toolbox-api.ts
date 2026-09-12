import { request, type ImageJob, type Quote } from "./user-api";

export interface ToolboxOptions {
  trim: boolean; resize: "original" | "fit" | "fill" | "stretch" | "percent";
  width: number; height: number; percent: number; rotation: 0 | 90 | 180 | 270;
  flip_horizontal: boolean; flip_vertical: boolean; padding: number;
  brightness: number; contrast: number; saturation: number; blur: number; sharpen: number;
  grayscale: boolean; invert: boolean;
  watermark: "none" | "text" | "image"; watermark_text: string; watermark_color: string;
  watermark_asset_id: string | null;
  watermark_position: "top-left" | "top" | "top-right" | "left" | "center" | "right" | "bottom-left" | "bottom" | "bottom-right";
  watermark_opacity: number; watermark_scale: number;
  background: "transparent" | "color" | "image"; color: string; background_asset_id: string | null;
  format: "png" | "jpg" | "webp"; compression: "quality" | "target"; quality: number; target_kb: number;
}
export const defaultToolboxOptions: ToolboxOptions = { trim: false, resize: "original", width: 2000, height: 2000, percent: 100, rotation: 0, flip_horizontal: false, flip_vertical: false, padding: 0, brightness: 0, contrast: 0, saturation: 0, blur: 0, sharpen: 0, grayscale: false, invert: false, watermark: "none", watermark_text: "", watermark_color: "#ffffff", watermark_asset_id: null, watermark_position: "bottom-right", watermark_opacity: 35, watermark_scale: 25, background: "transparent", color: "#ffffff", background_asset_id: null, format: "png", compression: "quality", quality: 85, target_kb: 500 };
export function normalizeToolboxOptions(value: Partial<ToolboxOptions>): ToolboxOptions { return { ...defaultToolboxOptions, ...value }; }
export interface ToolboxParameters { options: ToolboxOptions; batch_id: string; batch_name: string; output_name: string }
export interface ToolboxQuote { batch_id: string; total_points: number; items: { source_asset_id: string; parameters: ToolboxParameters; quote: Quote }[] }
export const toolboxApi = {
  quote: (asset_ids: string[], options: ToolboxOptions, batch_name: string, filename_prefix: string) => request<ToolboxQuote>("/api/v1/toolbox/quote", { method: "POST", body: JSON.stringify({ asset_ids, options, batch_name, filename_prefix }) }),
  submit: (quote: ToolboxQuote) => request<{ items: ImageJob[] }>("/api/v1/toolbox/submit", { method: "POST", body: JSON.stringify({ items: quote.items.map(item => ({ quote_id: item.quote.id, parameters: item.parameters })) }) }),
};
export async function downloadBundle(ids: string[]) {
  const response = await fetch("/api/v1/assets/download-bundle", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ asset_ids: ids }) });
  if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(data.message || "打包下载失败"); }
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a"); link.href = url; link.download = "images.zip"; link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 60000);
}
export function toolboxDimensions(width: number, height: number, options: ToolboxOptions): [number, number] {
  if (options.resize === "percent") { width = Math.max(1, Math.round(width * options.percent / 100)); height = Math.max(1, Math.round(height * options.percent / 100)); }
  else if (options.resize === "fit") { const scale = Math.min(options.width / width, options.height / height); width = Math.max(1, Math.round(width * scale)); height = Math.max(1, Math.round(height * scale)); }
  else if (["fill", "stretch"].includes(options.resize)) { width = options.width; height = options.height; }
  if (options.rotation === 90 || options.rotation === 270) [width, height] = [height, width];
  return [width + options.padding * 2, height + options.padding * 2];
}
export const processingSteps = (o: ToolboxOptions) => [o.trim && "去透明边", o.brightness || o.contrast || o.saturation || o.blur || o.sharpen || o.grayscale || o.invert ? "调色增强" : false, o.resize !== "original" && "调整尺寸", (o.rotation || o.flip_horizontal || o.flip_vertical) && "旋转翻转", o.padding > 0 && "增加留白", o.background !== "transparent" && "合成背景", (o.watermark === "text" || o.watermark === "image") && "添加水印", `${o.format.toUpperCase()}${o.compression === "target" ? ` ≤ ${o.target_kb} KB` : o.format === "png" ? " 无损压缩" : ` · 质量 ${o.quality}`}`].filter(Boolean).join(" → ");
