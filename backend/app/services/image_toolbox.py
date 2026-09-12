from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

from PIL import (
    Image,
    ImageColor,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageFont,
    ImageOps,
    UnidentifiedImageError,
)

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


def _apply_adjustments(image: Image.Image, options: ToolboxOptions) -> Image.Image:
    if not any(
        (
            options.brightness,
            options.contrast,
            options.saturation,
            options.blur,
            options.sharpen,
            options.grayscale,
            options.invert,
        )
    ):
        return image
    alpha = image.getchannel("A")
    rgb = image.convert("RGB")
    if options.brightness:
        rgb = ImageEnhance.Brightness(rgb).enhance(1 + options.brightness / 100)
    if options.contrast:
        rgb = ImageEnhance.Contrast(rgb).enhance(1 + options.contrast / 100)
    if options.saturation:
        rgb = ImageEnhance.Color(rgb).enhance(1 + options.saturation / 100)
    if options.grayscale:
        rgb = ImageOps.grayscale(rgb).convert("RGB")
    if options.invert:
        rgb = ImageOps.invert(rgb)
    result = rgb.convert("RGBA")
    result.putalpha(alpha)
    if options.blur:
        result = result.filter(ImageFilter.GaussianBlur(radius=options.blur))
    if options.sharpen:
        result = result.filter(
            ImageFilter.UnsharpMask(radius=2, percent=options.sharpen * 80, threshold=3)
        )
    return result


def _watermark_font(size: int) -> ImageFont.ImageFont:
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/arial.ttf",
    )
    for candidate in candidates:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    return ImageFont.load_default(size=max(10, size))


def _watermark_position(
    canvas_size: tuple[int, int], overlay_size: tuple[int, int], position: str
) -> tuple[int, int]:
    width, height = canvas_size
    overlay_width, overlay_height = overlay_size
    margin = max(12, min(width, height) // 30)
    horizontal = {
        "left": margin,
        "center": (width - overlay_width) // 2,
        "right": width - overlay_width - margin,
    }
    vertical = {
        "top": margin,
        "center": (height - overlay_height) // 2,
        "bottom": height - overlay_height - margin,
    }
    row, column = position.split("-") if "-" in position else (position, "center")
    if position == "top":
        row, column = "top", "center"
    elif position == "left":
        row, column = "center", "left"
    elif position == "center":
        row, column = "center", "center"
    elif position == "right":
        row, column = "center", "right"
    elif position == "bottom":
        row, column = "bottom", "center"
    return max(0, horizontal[column]), max(0, vertical[row])


def _apply_watermark(
    canvas: Image.Image,
    options: ToolboxOptions,
    watermark: bytes | None,
) -> Image.Image:
    if options.watermark == "none":
        return canvas
    if options.watermark == "text":
        font_size = max(12, round(min(canvas.size) * options.watermark_scale / 250))
        font = _watermark_font(font_size)
        probe = ImageDraw.Draw(canvas)
        bounds = probe.textbbox((0, 0), options.watermark_text, font=font)
        padding = max(6, font_size // 5)
        overlay = Image.new(
            "RGBA",
            (bounds[2] - bounds[0] + padding * 2, bounds[3] - bounds[1] + padding * 2),
        )
        draw = ImageDraw.Draw(overlay)
        draw.text(
            (padding - bounds[0], padding - bounds[1]),
            options.watermark_text,
            font=font,
            fill=ImageColor.getrgb(options.watermark_color) + (round(255 * options.watermark_opacity / 100),),
        )
    else:
        if watermark is None:
            raise AssetInputError("水印图片不可用")
        overlay = _decode(watermark)
        max_width = max(1, round(canvas.width * options.watermark_scale / 100))
        max_height = max(1, round(canvas.height * options.watermark_scale / 100))
        overlay.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        alpha = overlay.getchannel("A").point(
            lambda value: round(value * options.watermark_opacity / 100)
        )
        overlay.putalpha(alpha)
    canvas.alpha_composite(overlay, _watermark_position(canvas.size, overlay.size, options.watermark_position))
    return canvas


def process_image(
    raw: bytes,
    options: ToolboxOptions,
    background: bytes | None = None,
    watermark: bytes | None = None,
    *,
    max_megapixels: int = 16,
) -> tuple[PreparedAsset, dict]:
    image = _decode(raw)
    if options.trim:
        bounds = image.getchannel("A").getbbox()
        if bounds is None:
            raise AssetInputError("图片完全透明，无法自动裁边")
        image = image.crop(bounds)
    image = _apply_adjustments(image, options)
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
    _apply_watermark(canvas, options, watermark)
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
