import type {
  Capabilities,
  ImageQuality,
  ImageResult,
  ImageSize,
  OutputFormat,
  PreflightReport,
  RestoreMode,
  ToolId,
  WorkflowToolId,
  CropRect,
} from "./types";

export interface RunOptions {
  tool: ToolId;
  file: File | null;
  prompt: string;
  size: ImageSize;
  quality: ImageQuality;
  outputFormat: OutputFormat;
  scale: 2 | 4;
  sharpen: boolean;
}

async function parseResponse<T = ImageResult>(response: Response): Promise<T> {
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`;
    try {
      const payload = (await response.json()) as { detail?: string; message?: string };
      if (payload.message) message = payload.message;
      else if (payload.detail) message = payload.detail;
    } catch {
      // Keep the status-based fallback for non-JSON upstream failures.
    }
    throw new Error(message);
  }
  return (await response.json()) as T;
}

export async function getCapabilities(): Promise<Capabilities> {
  const response = await fetch("/api/capabilities");
  if (!response.ok) throw new Error("无法读取服务能力");
  return (await response.json()) as Capabilities;
}

export async function runTool(options: RunOptions): Promise<ImageResult> {
  if (options.tool === "generate") {
    return parseResponse(
      await fetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt: options.prompt,
          size: options.size,
          quality: options.quality,
          output_format: options.outputFormat,
        }),
      }),
    );
  }

  if (!options.file) throw new Error("请先选择图片");
  const form = new FormData();
  form.append("image", options.file);

  if (options.tool === "edit") {
    form.append("prompt", options.prompt);
    form.append("size", options.size);
    form.append("quality", options.quality);
    form.append("output_format", options.outputFormat);
  } else if (options.tool === "upscale") {
    form.append("scale", String(options.scale));
    form.append("sharpen", String(options.sharpen));
  }

  return parseResponse(
    await fetch(`/api/${options.tool}`, {
      method: "POST",
      body: form,
    }),
  );
}

export interface WorkflowOptions {
  tool: WorkflowToolId;
  sourceAssetId: string | null;
  prompt: string;
  size: ImageSize;
  quality: ImageQuality;
  restoreMode: RestoreMode;
  scale: 2 | 4;
  denoise: number;
  deblur: number;
  crop: CropRect;
  backgroundTolerance: number;
  textureReduction: number;
  shadowReduction: number;
  edgeCleanup: number;
}

export async function uploadAsset(file: File): Promise<ImageResult> {
  const form = new FormData();
  form.append("image", file);
  return parseResponse(await fetch("/api/assets", { method: "POST", body: form }));
}

export async function getAssets(): Promise<ImageResult[]> {
  const response = await fetch("/api/assets?limit=40");
  if (!response.ok) throw new Error("无法读取素材库");
  return (await response.json()) as ImageResult[];
}

export async function getLineage(assetId: string): Promise<ImageResult[]> {
  const response = await fetch(`/api/assets/${assetId}/lineage`);
  if (!response.ok) throw new Error("无法读取处理路径");
  return (await response.json()) as ImageResult[];
}

export async function runWorkflow(options: WorkflowOptions): Promise<ImageResult> {
  if (options.tool === "generate") {
    return parseResponse(
      await fetch("/api/chain/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt: options.prompt,
          size: options.size,
          quality: options.quality,
          output_format: "png",
        }),
      }),
    );
  }
  if (!options.sourceAssetId) throw new Error("请先选择素材");
  const form = new FormData();
  form.append("source_asset_id", options.sourceAssetId);
  let endpoint: string;
  if (options.tool === "restore") {
    endpoint = "/api/restore";
    form.append("mode", options.restoreMode);
    form.append("scale", String(options.scale));
    form.append("denoise", String(options.denoise));
    form.append("deblur", String(options.deblur));
  } else if (options.tool === "extract-print") {
    endpoint = "/api/extract-print";
    form.append("crop_x", String(options.crop.x));
    form.append("crop_y", String(options.crop.y));
    form.append("crop_width", String(options.crop.width));
    form.append("crop_height", String(options.crop.height));
    form.append("background_tolerance", String(options.backgroundTolerance));
    form.append("texture_reduction", String(options.textureReduction));
    form.append("shadow_reduction", String(options.shadowReduction));
    form.append("edge_cleanup", String(options.edgeCleanup));
  } else if (options.tool === "ai-reconstruct") {
    endpoint = "/api/chain/ai-reconstruct";
    form.append("prompt", options.prompt);
    form.append("size", options.size);
    form.append("quality", options.quality);
  } else {
    endpoint = "/api/chain/remove-background";
  }
  return parseResponse(await fetch(endpoint, { method: "POST", body: form }));
}

export async function runPreflight(
  assetId: string,
  targetWidthCm: number,
  targetDpi: number,
): Promise<PreflightReport> {
  const response = await fetch("/api/preflight", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      asset_id: assetId,
      target_width_cm: targetWidthCm,
      target_dpi: targetDpi,
    }),
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as {
      detail?: string;
      message?: string;
    };
    throw new Error(payload.message || payload.detail || "印前检查失败");
  }
  return (await response.json()) as PreflightReport;
}

const POD_REDRAW_PROMPT = `Recreate only the printed ink artwork visible on the product in the reference image as a clean, flat, front-facing 2D source artwork. Treat the reference as the source of truth. Preserve the exact text spelling, letter shapes, line breaks, composition, proportions, ink colors, outlines, characters, objects, and every small decorative element. Keep white printed ink white and black printed ink black; never invert colors because of the garment color. Remove the product, garment, fabric texture, folds, wrinkles, seams, perspective distortion, lighting, shadows, highlights, and photographic background. Do not redesign, restyle, simplify, crop, add, or remove anything from the print. Center the complete recovered artwork with generous margin on one perfectly uniform, fully saturated chroma-key background color that does not occur anywhere in the original artwork. Prefer pure green #00FF00; if the artwork contains green, use pure magenta #FF00FF. The chroma-key background must be flat, with no texture, gradient, shadow, or lighting. Render the artwork with crisp antialiased edges and print-ready detail.`;

export async function redrawPodPrint(sourceAssetId: string): Promise<ImageResult> {
  const form = new FormData();
  form.append("source_asset_id", sourceAssetId);
  form.append("prompt", POD_REDRAW_PROMPT);
  form.append("size", "auto");
  form.append("quality", "high");
  return parseResponse(
    await fetch("/api/chain/ai-reconstruct", { method: "POST", body: form }),
  );
}

export async function cutoutAsset(sourceAssetId: string): Promise<ImageResult> {
  const form = new FormData();
  form.append("source_asset_id", sourceAssetId);
  return parseResponse(
    await fetch("/api/chain/smart-cutout", { method: "POST", body: form }),
  );
}

export async function upscaleAsset(
  sourceAssetId: string,
  scale: 2 | 4,
): Promise<ImageResult> {
  const form = new FormData();
  form.append("source_asset_id", sourceAssetId);
  form.append("scale", String(scale));
  form.append("sharpen", "true");
  return parseResponse(
    await fetch("/api/chain/upscale", { method: "POST", body: form }),
  );
}

export type AiTransformMode =
  | "faithful-redraw"
  | "photo-enhance"
  | "illustration-enhance"
  | "logo-cleanup"
  | "line-art"
  | "text-fix"
  | "local-repair"
  | "recolor"
  | "variant";

export async function transformAsset(
  sourceAssetId: string,
  mode: AiTransformMode,
  instruction = "",
  mask?: File,
): Promise<ImageResult> {
  const form = new FormData();
  form.append("source_asset_id", sourceAssetId);
  form.append("mode", mode);
  form.append("instruction", instruction);
  if (mask) form.append("mask", mask);
  return parseResponse(
    await fetch("/api/chain/ai-transform", { method: "POST", body: form }),
  );
}

const ARTWORK_STYLES: Record<string, string> = {
  graphic: "bold commercial graphic illustration with crisp outlines and strong composition",
  vintage: "distressed vintage screen-print illustration with intentional texture",
  line: "clean black and white line-art illustration with confident strokes",
  cute: "friendly expressive cartoon illustration with polished shapes",
};

export async function generateArtwork(prompt: string, style: string): Promise<ImageResult> {
  const stylePrompt = ARTWORK_STYLES[style] ?? ARTWORK_STYLES.graphic;
  return parseResponse(
    await fetch("/api/chain/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt: `Create an original isolated artwork based on this request: ${prompt}. Style: ${stylePrompt}. Show only the complete uncropped artwork, centered with generous margin. Use a perfectly uniform fully saturated green #00FF00 background that does not appear in the artwork; use magenta #FF00FF instead if green is required in the artwork. No product mockup, clothing, frame, room, hands, shadows, or photographic scene.`,
        size: "auto",
        quality: "high",
        output_format: "png",
      }),
    }),
  );
}

export type ColorEffectMode = "grayscale" | "invert" | "threshold" | "monochrome";

export async function applyColorEffect(
  sourceAssetId: string,
  mode: ColorEffectMode,
  color = "#111111",
): Promise<ImageResult> {
  const form = new FormData();
  form.append("source_asset_id", sourceAssetId);
  form.append("mode", mode);
  form.append("color", color);
  return parseResponse(
    await fetch("/api/chain/color-effect", { method: "POST", body: form }),
  );
}

export async function vectorizeAsset(sourceAssetId: string): Promise<ImageResult> {
  const form = new FormData();
  form.append("source_asset_id", sourceAssetId);
  return parseResponse(
    await fetch("/api/chain/vectorize", { method: "POST", body: form }),
  );
}
