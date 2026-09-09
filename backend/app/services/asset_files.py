from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError


class AssetInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedAsset:
    data: bytes
    mime_type: str
    extension: str
    width: int | None
    height: int | None
    has_alpha: bool | None
    sha256: str


_SVG_FORBIDDEN_ELEMENTS = {
    "animate",
    "animatemotion",
    "animatetransform",
    "foreignobject",
    "iframe",
    "image",
    "script",
    "set",
    "style",
    "use",
}
_SVG_DIMENSION = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)")


def prepare_asset(raw: bytes, *, kind: str, max_megapixels: int) -> PreparedAsset:
    if not raw:
        raise AssetInputError("上传文件为空")
    if _looks_like_svg(raw):
        if kind != "vector":
            raise AssetInputError("SVG 只能作为矢量素材上传")
        return _prepare_svg(raw, max_megapixels=max_megapixels)
    if kind == "vector":
        raise AssetInputError("矢量素材必须是有效的 SVG")
    return _prepare_raster(raw, kind=kind, max_megapixels=max_megapixels)


def inspect_stored_asset(raw: bytes, *, extension: str) -> PreparedAsset:
    if extension.lower() == "svg":
        return _prepare_svg(raw, max_megapixels=200)
    return _prepare_raster(
        raw,
        kind="thumbnail" if extension.lower() == "webp" else "result",
        max_megapixels=200,
        preserve_format=True,
    )


def _prepare_raster(
    raw: bytes,
    *,
    kind: str,
    max_megapixels: int,
    preserve_format: bool = False,
) -> PreparedAsset:
    try:
        with Image.open(BytesIO(raw)) as source:
            if source.format not in {"PNG", "JPEG", "WEBP"}:
                raise AssetInputError("仅支持 PNG、JPEG、WebP 和 SVG")
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > max_megapixels * 1_000_000:
                raise AssetInputError(f"图片超过 {max_megapixels} 百万像素限制")
            source.load()
            image = ImageOps.exif_transpose(source)
            width, height = image.size
            has_alpha = "A" in image.getbands()
            if kind == "mask" and not has_alpha:
                raise AssetInputError("遮罩素材必须包含透明通道")
            image = image.convert("RGBA" if has_alpha else "RGB")
            output = BytesIO()
            if preserve_format:
                output_format = source.format
            else:
                output_format = "WEBP" if kind == "thumbnail" else "PNG"
            if output_format == "WEBP":
                image.save(output, format="WEBP", quality=88, method=6)
                mime_type, extension = "image/webp", "webp"
            elif output_format == "JPEG":
                image.convert("RGB").save(output, format="JPEG", quality=95, optimize=True)
                mime_type, extension = "image/jpeg", "jpg"
                has_alpha = False
            else:
                image.save(output, format="PNG", optimize=True)
                mime_type, extension = "image/png", "png"
            data = output.getvalue()
    except AssetInputError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
        raise AssetInputError("文件不是可解码的受支持图片") from exc
    return PreparedAsset(
        data=data,
        mime_type=mime_type,
        extension=extension,
        width=width,
        height=height,
        has_alpha=has_alpha,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _looks_like_svg(raw: bytes) -> bool:
    prefix = raw[:1024].lstrip().lower()
    return prefix.startswith(b"<svg") or (prefix.startswith(b"<?xml") and b"<svg" in prefix)


def _prepare_svg(raw: bytes, *, max_megapixels: int) -> PreparedAsset:
    lowered = raw.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise AssetInputError("SVG 不能包含 DTD 或实体声明")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise AssetInputError("SVG XML 无法解析") from exc
    if _local_name(root.tag).lower() != "svg":
        raise AssetInputError("文件根元素不是 SVG")
    for element in root.iter():
        if _local_name(element.tag).lower() in _SVG_FORBIDDEN_ELEMENTS:
            raise AssetInputError("SVG 包含不允许的动态或外部内容")
        for name, value in element.attrib.items():
            local_name = _local_name(name).lower()
            normalized = value.strip().lower()
            if local_name.startswith("on"):
                raise AssetInputError("SVG 不能包含事件处理器")
            if "javascript:" in normalized or "https:" in normalized or "http:" in normalized:
                raise AssetInputError("SVG 不能引用外部资源")
            if "url(" in normalized and "url(#" not in normalized:
                raise AssetInputError("SVG 不能引用外部资源")
            if local_name == "href" and normalized and not normalized.startswith("#"):
                raise AssetInputError("SVG 不能引用外部资源")
    width, height = _svg_dimensions(root)
    if width <= 0 or height <= 0 or width * height > max_megapixels * 1_000_000:
        raise AssetInputError(f"SVG 画布超过 {max_megapixels} 百万像素限制")
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return PreparedAsset(
        data=data,
        mime_type="image/svg+xml",
        extension="svg",
        width=round(width),
        height=round(height),
        has_alpha=None,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _svg_dimensions(root: ET.Element) -> tuple[float, float]:
    width = _numeric_dimension(root.attrib.get("width"))
    height = _numeric_dimension(root.attrib.get("height"))
    if width is not None and height is not None:
        return width, height
    view_box = root.attrib.get("viewBox", "").replace(",", " ").split()
    if len(view_box) != 4:
        raise AssetInputError("SVG 必须提供有效的 width/height 或 viewBox")
    try:
        view_width, view_height = float(view_box[2]), float(view_box[3])
    except ValueError as exc:
        raise AssetInputError("SVG viewBox 无效") from exc
    return view_width, view_height


def _numeric_dimension(value: str | None) -> float | None:
    if value is None:
        return None
    match = _SVG_DIMENSION.match(value)
    return float(match.group(1)) if match else None


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1]
