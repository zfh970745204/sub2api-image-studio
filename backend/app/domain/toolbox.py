from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class ToolboxOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trim: StrictBool = False
    resize: Literal["original", "fit", "fill", "stretch", "percent"] = "original"
    width: int = Field(default=2000, ge=1, le=12000, strict=True)
    height: int = Field(default=2000, ge=1, le=12000, strict=True)
    percent: int = Field(default=100, ge=1, le=800, strict=True)
    rotation: Literal[0, 90, 180, 270] = 0
    flip_horizontal: StrictBool = False
    flip_vertical: StrictBool = False
    padding: int = Field(default=0, ge=0, le=2000, strict=True)
    background: Literal["transparent", "color", "image"] = "transparent"
    color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    background_asset_id: uuid.UUID | None = None
    format: Literal["png", "jpg", "webp"] = "png"
    compression: Literal["quality", "target"] = "quality"
    quality: int = Field(default=85, ge=1, le=100, strict=True)
    target_kb: int = Field(default=500, ge=1, le=20480, strict=True)

    @model_validator(mode="after")
    def supported_combination(self):
        if self.compression == "target" and self.format == "png":
            raise ValueError("指定文件大小请选择 JPG 或 WebP；PNG 使用无损压缩")
        if self.background == "image" and self.background_asset_id is None:
            raise ValueError("请选择背景图片")
        return self


class ToolboxParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    options: ToolboxOptions
    batch_id: uuid.UUID
    batch_name: str = Field(min_length=1, max_length=80)
    output_name: str = Field(min_length=1, max_length=160)


def output_dimensions(width: int, height: int, options: ToolboxOptions) -> tuple[int, int]:
    if options.resize == "percent":
        width, height = (
            max(1, round(width * options.percent / 100)),
            max(1, round(height * options.percent / 100)),
        )
    elif options.resize == "fit":
        scale = min(options.width / width, options.height / height)
        width, height = max(1, round(width * scale)), max(1, round(height * scale))
    elif options.resize in {"fill", "stretch"}:
        width, height = options.width, options.height
    if options.rotation in {90, 270}:
        width, height = height, width
    return width + options.padding * 2, height + options.padding * 2


def check_dimensions(width: int, height: int, max_megapixels: int = 16) -> None:
    if (
        width < 1
        or height < 1
        or max(width, height) > 12000
        or width * height > min(16, max_megapixels) * 1_000_000
    ):
        raise ValueError(
            f"输出尺寸超过限制：最长边 12000 像素，总像素不超过 {min(16, max_megapixels)} 百万"
        )
