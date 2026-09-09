export type ToolId = "generate" | "edit" | "remove-background" | "upscale";
export type WorkflowToolId =
  | "extract-print"
  | "restore"
  | "remove-background"
  | "ai-reconstruct"
  | "generate";
export type OperationId = ToolId | WorkflowToolId | "upload" | "color-effect" | "vectorize";
export type ImageQuality = "auto" | "low" | "medium" | "high";
export type ImageSize = "auto" | "1024x1024" | "1024x1536" | "1536x1024";
export type OutputFormat = "png" | "jpeg" | "webp";

export interface Capabilities {
  sub2api_configured: boolean;
  sub2api_model: string;
  generation: boolean;
  editing: boolean;
  transparent_upstream_output: boolean;
  local_background_removal: boolean;
  local_upscale: boolean;
  print_extraction: boolean;
  print_restoration: boolean;
  persistent_workflow: boolean;
  ai_upscale_available: boolean;
  background_model: string;
}

export interface ImageResult {
  id: string;
  operation: OperationId;
  url: string;
  download_url: string;
  mime_type: string;
  width: number;
  height: number;
  size_bytes: number;
  requested_size?: string | null;
  revised_prompt?: string | null;
  warning?: string | null;
  parent_id?: string | null;
  root_id?: string;
  job_id?: string | null;
  created_at?: string;
  metadata?: Record<string, unknown>;
}

export interface CropRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export type RestoreMode = "faithful" | "illustration" | "logo";

export interface PreflightReport {
  asset_id: string;
  width: number;
  height: number;
  has_alpha: boolean;
  target_width_cm: number;
  target_dpi: number;
  required_width_pixels: number;
  effective_dpi: number;
  max_width_cm_at_target_dpi: number;
  status: "ready" | "review" | "insufficient";
  warnings: string[];
}
