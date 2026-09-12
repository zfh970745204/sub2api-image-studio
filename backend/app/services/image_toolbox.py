from __future__ import annotations

import hashlib
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from app.domain.toolbox import ToolboxOptions, check_dimensions, output_dimensions
from app.services.asset_files import AssetInputError, PreparedAsset


def _decode(raw: bytes) -> Image.Image:
    try:
        with Image.open(BytesIO(raw)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"} or getattr(image, "n_frames", 1) != 1:
                raise AssetInputError("工具箱支持静态 PNG、JPG、WebP 图片")
            check_dimensions(image.width, image.height)
            return ImageOps.exif_transpose(image).convert("RGBA")
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise AssetInputError("图片无法解码，请更换文件") from exc


def encoded_asset(data: bytes, extension: str) -> PreparedAsset:
    """Trusted encoder output: inspect without recompressing or converting back to PNG."""
    with Image.open(BytesIO(data)) as image:
        expected = {"png": "PNG", "jpg": "JPEG", "webp": "WEBP"}[extension]
        if image.format != expected:
            raise AssetInputError("输出图片格式不匹配")
        check_dimensions(image.width, image.height)
        return PreparedAsset(
            data=data,
            mime_type={"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[extension],
            extension=extension,
            width=image.width,
            height=image.height,
            has_alpha="A" in image.getbands() or "transparency" in image.info,
            sha256=hashlib.sha256(data).hexdigest(),
        )


def process_image(
    raw: bytes,
    options: ToolboxOptions,
    background: bytes | None = None,
    *,
    max_megapixels: int = 16,
) -> tuple[PreparedAsset, dict]:
    image = _decode(raw)
    if options.trim:
        bounds = image.getchannel("A").getbbox()
        if bounds is None:
            raise AssetInputError("图片完全透明，无法自动裁边")
        image = image.crop(bounds)
    final_size = output_dimensions(*image.size, options)
    check_dimensions(*final_size, max_megapixels)
    # Geometry is deterministic; alpha stays attached to every resize and transform.
    if options.resize != "original":
        geometry = options.model_copy(update={"rotation": 0, "padding": 0})
        target = output_dimensions(*image.size, geometry)
        if options.resize == "fill":
            image = ImageOps.fit(image, target, method=Image.Resampling.LANCZOS)
        else:
            image = image.resize(target, Image.Resampling.LANCZOS)
    if options.rotation:
        image = image.transpose(
            {
                90: Image.Transpose.ROTATE_270,
                180: Image.Transpose.ROTATE_180,
                270: Image.Transpose.ROTATE_90,
            }[options.rotation]
        )
    if options.flip_horizontal:
        image = ImageOps.mirror(image)
    if options.flip_vertical:
        image = ImageOps.flip(image)
    canvas = Image.new("RGBA", final_size)
    if options.background == "image":
        if background is None:
            raise AssetInputError("背景图片不可用")
        canvas = ImageOps.fit(_decode(background), final_size, method=Image.Resampling.LANCZOS)
    elif options.background == "color":
        canvas = Image.new("RGBA", final_size, options.color)
    canvas.alpha_composite(image, (options.padding, options.padding))
    if options.format == "jpg":
        flat = Image.new("RGBA", final_size, options.color)
        flat.alpha_composite(canvas)
        canvas = flat.convert("RGB")

    def encode(quality: int) -> bytes:
        stream = BytesIO()
        if options.format == "png":
            canvas.save(stream, "PNG", optimize=True)
        elif options.format == "jpg":
            canvas.save(stream, "JPEG", quality=quality, optimize=True)
        else:
            canvas.save(stream, "WEBP", quality=quality, method=4)
        return stream.getvalue()

    quality = 100 if options.compression == "target" else options.quality
    data = encode(quality)
    if options.compression == "target" and len(data) > options.target_kb * 1024:
        # Preserve dimensions; never silently shrink artwork to claim the target was met.
        low, high, best = 1, quality - 1, None
        while low <= high:
            mid = (low + high) // 2
            candidate = encode(mid)
            if len(candidate) <= options.target_kb * 1024:
                best = (candidate, mid)
                low = mid + 1
            else:
                high = mid - 1
        if best is None:
            raise AssetInputError(
                "在保留尺寸的情况下无法压到目标大小，请提高目标 KB、缩小尺寸或更换格式"
            )
        data, quality = best
    return encoded_asset(data, options.format), {
        "input_bytes": len(raw),
        "output_bytes": len(data),
        "encoding_quality": quality if options.format != "png" else None,
    }
