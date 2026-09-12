from __future__ import annotations

import hashlib
from io import BytesIO

from PIL import Image, ImageChops, ImageDraw, ImageOps, UnidentifiedImageError

from app.services.asset_files import AssetInputError, PreparedAsset


def crop_asset(
    raw: bytes,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    shape: str,
    expected_size: tuple[int, int],
    max_megapixels: int,
) -> PreparedAsset:
    """Crop oriented pixels, multiplying existing alpha by an antialiased circle."""
    try:
        with Image.open(BytesIO(raw)) as source:
            if source.format not in {"PNG", "JPEG", "WEBP"}:
                raise AssetInputError("裁切仅支持 PNG、JPEG 和 WebP")
            if source.width * source.height > min(16, max_megapixels) * 1_000_000:
                raise AssetInputError("图片超过裁切像素限制，请先缩小图片")
            oriented = ImageOps.exif_transpose(source)
            if oriented.size != expected_size:
                raise AssetInputError("素材尺寸已变化，请重新载入后裁切")
            if (
                x < 0
                or y < 0
                or width < 1
                or height < 1
                or x + width > oriented.width
                or y + height > oriented.height
                or shape not in {"rectangle", "circle"}
                or (shape == "circle" and width != height)
            ):
                raise AssetInputError("裁切区域必须位于图片内，圆形的宽高必须相同")
            result = oriented.crop((x, y, x + width, y + height)).convert("RGBA")
        if shape == "circle":
            # At most 64 MB for the supersampled mask under the 16 MP input cap.
            mask = Image.new("L", (width * 2, height * 2), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, width * 2 - 1, height * 2 - 1), fill=255)
            mask = mask.resize((width, height), Image.Resampling.LANCZOS)
            result.putalpha(ImageChops.multiply(result.getchannel("A"), mask))
        if result.getchannel("A").getbbox() is None:
            raise AssetInputError("裁切区域完全透明，请选择包含图案的区域")
        output = BytesIO()
        result.save(output, format="PNG")
        data = output.getvalue()
        return PreparedAsset(
            data=data,
            mime_type="image/png",
            extension="png",
            width=width,
            height=height,
            has_alpha=True,
            sha256=hashlib.sha256(data).hexdigest(),
        )
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise AssetInputError("裁切图片无法读取，请更换图片") from exc
