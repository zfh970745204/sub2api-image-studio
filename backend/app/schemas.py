from typing import Any, Literal

from pydantic import BaseModel, Field

ImageSize = Literal["auto", "1024x1024", "1024x1536", "1536x1024"]
ImageQuality = Literal["auto", "low", "medium", "high"]
ImageFormat = Literal["png", "jpeg", "webp"]
Operation = Literal[
    "upload",
    "generate",
    "edit",
    "remove-background",
    "upscale",
    "restore",
    "extract-print",
    "color-effect",
    "vectorize",
]
RestoreMode = Literal["faithful", "illustration", "logo"]


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=5000)
    size: ImageSize = "1024x1024"
    quality: ImageQuality = "medium"
    output_format: ImageFormat = "png"


class ImageResult(BaseModel):
    id: str
    operation: Operation
    url: str
    download_url: str
    mime_type: str
    width: int
    height: int
    size_bytes: int
    requested_size: str | None = None
    revised_prompt: str | None = None
    warning: str | None = None
    parent_id: str | None = None
    root_id: str = ""
    job_id: str | None = None
    created_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class PreflightRequest(BaseModel):
    asset_id: str
    target_width_cm: float = Field(default=30, gt=0, le=200)
    target_dpi: int = Field(default=300, ge=72, le=1200)


class PreflightReport(BaseModel):
    asset_id: str
    width: int
    height: int
    has_alpha: bool
    target_width_cm: float
    target_dpi: int
    required_width_pixels: int
    effective_dpi: float
    max_width_cm_at_target_dpi: float
    status: Literal["ready", "review", "insufficient"]
    warnings: list[str] = Field(default_factory=list)


class JobResult(BaseModel):
    id: str
    operation: str
    source_asset_id: str | None
    status: Literal["running", "completed", "failed"]
    parameters: dict[str, Any]
    output_asset_id: str | None
    error: str | None
    created_at: str
    completed_at: str | None


class Capabilities(BaseModel):
    sub2api_configured: bool
    sub2api_model: str
    generation: bool
    editing: bool
    transparent_upstream_output: bool = False
    local_background_removal: bool
    local_upscale: bool = True
    print_extraction: bool = True
    print_restoration: bool = True
    persistent_workflow: bool = True
    ai_upscale_available: bool = False
    password_reset: bool = False
    background_model: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"] = "ok"
