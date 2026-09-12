"""Extract against the product color; preserve ambiguous same-color interior ink."""

from __future__ import annotations

from io import BytesIO
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageColor, ImageOps

from app.image_ops import ImageInputError


def _png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, "PNG", optimize=True)
    return output.getvalue()


def _border(pixels: np.ndarray) -> np.ndarray:
    return np.concatenate([pixels[0], pixels[-1], pixels[:, 0], pixels[:, -1]])


def product_background(raw: bytes, point: tuple[float, float] | None = None) -> dict[str, Any]:
    with Image.open(BytesIO(raw)) as source:
        image = ImageOps.exif_transpose(source).convert("RGBA")
        image.thumbnail((384, 384))
        pixels = np.asarray(image)
    height, width = pixels.shape[:2]
    rgb = pixels[:, :, :3].astype(np.float32)
    visible = pixels[:, :, 3] > 128
    if point is not None:
        x, y = round(point[0] * (width - 1)), round(point[1] * (height - 1))
        patch = pixels[max(0, y - 1) : y + 2, max(0, x - 1) : x + 2]
        samples = patch[:, :, :3][patch[:, :, 3] > 128]
        if not len(samples):
            raise ImageInputError("这里是透明区域，请在产品底色上取色。")
        color = np.median(samples, axis=0)
        confidence = 1.0
    else:
        yy, xx = np.mgrid[:height, :width]
        region = (
            (xx >= width * 0.12)
            & (xx < width * 0.88)
            & (yy >= height * 0.12)
            & (yy < height * 0.9)
            & visible
        )
        border = _border(rgb)
        backdrop = np.median(border, axis=0)
        different = np.linalg.norm(rgb - backdrop, axis=2) > 35
        # Exclude the studio surround when a distinct product occupies the
        # central image. A white product on white falls back to central samples.
        flanks = (
            region
            & ((xx < width * 0.35) | (xx > width * 0.65))
            & (yy > height * 0.28)
            & (yy < height * 0.8)
        )
        if np.any(flanks) and np.mean(different[flanks]) > 0.25:
            region &= different
        # Downweight the central print so broad fabric areas win over ink.
        weights = np.where(
            (xx > width * 0.32) & (xx < width * 0.68) & (yy > height * 0.25) & (yy < height * 0.7),
            0.25,
            1.0,
        )
        samples = rgb[region]
        if not len(samples):
            raise ImageInputError("无法识别产品底色，请手动选择颜色。")
        bins = samples.astype(np.int32) // 24
        _, groups = np.unique(bins, axis=0, return_inverse=True)
        counts = np.bincount(groups, weights=weights[region])
        chosen = groups == int(np.argmax(counts))
        color = np.median(samples[chosen], axis=0)
        confidence = float(counts.max() / counts.sum())
        # Photograph lighting should not turn black/white garment presets into
        # incidental grey shadows. Manual sampling remains exact.
        if np.ptp(color) < 18:
            if color.max() < 45:
                color[:] = 0
            elif color.min() > 205:
                color[:] = 255
    return {
        "color": "#" + "".join(f"{round(c):02X}" for c in color),
        "confidence": round(confidence, 3),
        "method": "sampled" if point else "product-color-estimate",
    }


def extract_prompt(color: str, instruction: str = "") -> str:
    prompt = (
        "提取参考图片中的完整印花图案，只要印花，保持原有的色彩和内容不变。"
        "保留全部文字、字体、排版、比例和细节，包括白色与黑色印花。"
        "去掉产品外形、摄影背景、布料纹理、褶皱和阴影，校正透视，得到平整清晰的独立图案。"
        f"将完整印花放在均匀纯色 {color} 背景上，这是产品本身的底色。"
        "背景不要有纹理、渐变或棋盘格，不要改变印花内部颜色，不添加描边或新元素。"
        "保留发丝、细线和烟雾的自然柔边，完整图案居中，四周留少量纯色边距。"
    )
    return f"{prompt}\nUser instruction: {instruction}" if instruction else prompt


def size_print_outputs(
    output: bytes, edit_source: bytes, *, size: str, mode: str, color: str
) -> tuple[bytes, bytes]:
    """Fit both layers to the same canvas without stretching or cropping ink."""
    width, height = (int(value) for value in size.split("x"))
    results = []
    for raw, transparent, is_source in (
        (output, mode == "transparent", False),
        (edit_source, False, True),
    ):
        with Image.open(BytesIO(raw)) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGBA")
            resized = ImageOps.contain(image, (width, height), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0) if transparent else color)
            # Pasting without a mask retains native alpha in the restoration source.
            canvas.paste(resized, ((width - resized.width) // 2, (height - resized.height) // 2))
            results.append(_png(canvas if transparent or is_source else canvas.convert("RGB")))
    return results[0], results[1]


def finish_print(raw: bytes, *, mode: str, color: str) -> tuple[bytes, dict[str, Any]]:
    with Image.open(BytesIO(raw)) as source:
        source.load()
        image = source.convert("RGBA")
    pixels = np.asarray(image)
    alpha = pixels[:, :, 3]
    if not np.any(alpha):
        raise ImageInputError("未提取到有效印花，请换用更清晰的原图。")
    target = np.asarray(ImageColor.getrgb(color), dtype=np.float32)
    native = float(np.mean(_border(alpha) < 16)) >= 0.25 and float(np.mean(alpha < 16)) >= 0.005
    metadata: dict[str, Any] = {"output_mode": mode, "background_color": color}
    if native:
        if mode == "transparent":
            return _png(image), {
                **metadata,
                "method": "native-alpha",
                "transparent_background": True,
            }
        canvas = Image.new("RGBA", image.size, color)
        return _png(Image.alpha_composite(canvas, image).convert("RGB")), {
            **metadata,
            "method": "product-color-composite",
            "transparent_background": False,
        }

    # Flatten any incidental alpha before analyzing an opaque upstream result.
    canvas = Image.new("RGBA", image.size, color)
    rgb = np.asarray(Image.alpha_composite(canvas, image).convert("RGB"))
    border = _border(rgb).astype(np.float32)
    background = np.median(border, axis=0)
    if (
        np.mean(np.linalg.norm(border - background, axis=1) <= 24) < 0.8
        or np.linalg.norm(background - target) > 80
    ):
        raise ImageInputError("图片服务未返回平整的印花背景，请重试或换用更清晰的产品图片。")
    distance = np.linalg.norm(rgb.astype(np.float32) - background, axis=2)
    if np.count_nonzero(distance > 24) < max(4, distance.size * 0.0001):
        raise ImageInputError("未提取到有效印花，请换用更清晰的原图。")
    if mode == "opaque":
        return _png(Image.fromarray(rgb)), {
            **metadata,
            "method": "product-color-background",
            "transparent_background": False,
        }

    candidates = np.uint8(distance <= 28)
    _, labels = cv2.connectedComponents(candidates, connectivity=8)
    boundary_labels = np.unique(_border(labels))
    boundary_labels = boundary_labels[boundary_labels != 0]
    connected = np.isin(labels, boundary_labels) & (candidates != 0)
    # Enclosed same-color shapes may be pupils/letter ink. Keep them rather than
    # deleting every matching color; only evidenced exterior background is keyed.
    coverage = np.ones(distance.shape, dtype=np.float32)
    coverage[connected] = np.clip((distance[connected] - 4) / 24, 0, 1)
    coverage[coverage < 0.04] = 0
    foreground = rgb.astype(np.float32)
    edge = connected & (coverage > 0) & (coverage < 1)
    foreground[edge] = (foreground[edge] - (1 - coverage[edge, None]) * background) / coverage[
        edge, None
    ]
    foreground[coverage == 0] = 0
    result_alpha = np.uint8(np.rint(coverage * 255))
    if np.mean(result_alpha == 0) < 0.005 or not np.any(result_alpha > 0):
        raise ImageInputError("未能安全分离印花背景，请尝试原产品底色模式。")
    result = np.dstack([np.uint8(np.clip(np.rint(foreground), 0, 255)), result_alpha])
    return _png(Image.fromarray(result)), {
        **metadata,
        "method": "connected-product-color",
        "transparent_background": True,
        "estimated_background_color": [int(value) for value in background],
        "color_preservation": "interior-ink-preserved",
    }
