"""Bounded, lossless layer documents. PNG downloads remain ordinary composite images."""

from __future__ import annotations

import hashlib
import json
import struct
from io import BytesIO
from typing import Literal

import numpy as np
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.asset_files import AssetInputError, PreparedAsset


class LayerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=40)
    visible: bool
    locked: bool
    opacity: int = Field(ge=0, le=100)
    byte_length: int = Field(alias="byteLength", ge=1)


class ProjectManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    active_id: str = Field(alias="activeId")
    layers: list[LayerRecord] = Field(min_length=1, max_length=12)


def validate_raster_project(
    raw: bytes, width: int, height: int, max_megapixels: int
) -> tuple[bytes, PreparedAsset]:
    if width * height > min(16, max_megapixels) * 1_000_000:
        raise AssetInputError("图层图片超过像素限制")
    if len(raw) < 6 or len(raw) > 128 * 1024 * 1024:
        raise AssetInputError("图层文件大小无效")
    length = struct.unpack_from("<I", raw)[0]
    if not 2 <= length <= 16384 or length + 4 >= len(raw):
        raise AssetInputError("图层文件头无效")
    try:
        manifest = ProjectManifest.model_validate_json(raw[4 : 4 + length])
    except ValidationError as exc:
        raise AssetInputError("图层信息无效") from exc
    ids = {layer.id for layer in manifest.layers}
    if (
        (manifest.width, manifest.height) != (width, height)
        or len(manifest.layers) > min(12, 192_000_000 // (width * height * 4))
        or len(ids) != len(manifest.layers)
        or manifest.active_id not in ids
        or any(not layer.name.strip() for layer in manifest.layers)
    ):
        raise AssetInputError("图层尺寸、数量或标识无效")
    offset = 4 + length
    if offset + sum(layer.byte_length for layer in manifest.layers) != len(raw):
        raise AssetInputError("图层文件不完整")
    composite = np.zeros((height, width, 4), dtype=np.uint8)
    normalized: list[bytes] = []
    for layer in manifest.layers:
        data = raw[offset : offset + layer.byte_length]
        offset += layer.byte_length
        try:
            with Image.open(BytesIO(data)) as source:
                if (
                    source.format != "PNG"
                    or source.size != (width, height)
                    or source.mode != "RGBA"
                    or getattr(source, "is_animated", False)
                ):
                    raise AssetInputError("图层必须为同尺寸的 RGBA PNG")
                pixels = np.array(source)
            output = BytesIO()
            Image.fromarray(pixels).save(output, "PNG")
            normalized.append(output.getvalue())
            layer.byte_length = len(normalized[-1])
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise AssetInputError("图层图片损坏") from exc
        if not layer.visible or not layer.opacity:
            continue
        # Match the worker's unpremultiplied source-over and uint8 clamping.
        # Work in strips to avoid full-frame float buffers on large images.
        for row in range(0, height, 128):
            src = pixels[row : row + 128].astype(np.float64)
            dst = composite[row : row + 128].astype(np.float64)
            amount = src[:, :, 3:4] / 255 * layer.opacity / 100
            old_alpha = dst[:, :, 3:4] / 255
            alpha = amount + old_alpha * (1 - amount)
            rgb = src[:, :, :3] * amount + dst[:, :, :3] * old_alpha * (1 - amount)
            np.divide(rgb, alpha, out=rgb, where=alpha > 0)
            composite[row : row + 128, :, :3] = np.rint(rgb).clip(0, 255).astype(np.uint8)
            composite[row : row + 128, :, 3:4] = np.rint(alpha * 255).clip(0, 255).astype(np.uint8)
    if not composite[:, :, 3].any():
        raise AssetInputError("图片已完全透明，请显示图层或添加内容后再保存")
    header = json.dumps(manifest.model_dump(by_alias=True), ensure_ascii=False).encode()
    project = struct.pack("<I", len(header)) + header + b"".join(normalized)
    output = BytesIO()
    Image.fromarray(composite).save(output, "PNG")
    result = output.getvalue()
    return project, PreparedAsset(
        data=result,
        mime_type="image/png",
        extension="png",
        width=width,
        height=height,
        has_alpha=True,
        sha256=hashlib.sha256(result).hexdigest(),
    )
