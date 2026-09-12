"""Choose background removal without asking a subject model to select printed ink."""

from __future__ import annotations

from io import BytesIO
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageOps

from app.image_ops import ImageInputError, _unmix_chroma_edges


def _border(pixels: np.ndarray) -> np.ndarray:
    return np.concatenate([pixels[0], pixels[-1], pixels[:, 0], pixels[:, -1]])


def prepare_smart_cutout(raw: bytes) -> tuple[bytes, dict[str, Any]] | None:
    """Return a local result for native alpha/flat canvases, else request a model.

    A white frame alone does not establish a flat background: require substantial
    matching color inside the canvas, connected to multiple outer edges.
    """
    with Image.open(BytesIO(raw)) as source:
        source.load()
        image = ImageOps.exif_transpose(source).convert("RGBA")
    pixels = np.asarray(image)
    alpha = pixels[:, :, 3]
    if not np.any(alpha):
        raise ImageInputError("图片没有可见内容，请选择包含图案的原图。")
    if np.mean(_border(alpha) < 16) >= 0.25 and np.mean(alpha < 16) >= 0.005:
        buffer = BytesIO()
        image.save(buffer, "PNG", optimize=True)
        return buffer.getvalue(), {
            "method": "native-alpha",
            "color_preservation": "native-rgba-unchanged",
            "transparent_background": True,
        }
    # Partial transparency without a usable transparent surround is ambiguous.
    # Never flatten it just to detect a convenient background color.
    if np.any(alpha < 255):
        return None

    sample = image.convert("RGB")
    sample.thumbnail((512, 512))
    rgb = np.asarray(sample).astype(np.float32)
    sides = (rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1])
    background = np.median(_border(rgb), axis=0)
    distance = np.linalg.norm(rgb - background, axis=2)
    # Tight tolerance admits slight JPEG noise, not a gradient or textured sky.
    matches = [np.mean(np.linalg.norm(side - background, axis=1) <= 10) for side in sides]
    if min(matches) < 0.8 or np.mean(matches) < 0.95:
        return None
    candidates = np.uint8(distance <= 10)
    _, labels = cv2.connectedComponents(candidates, connectivity=8)
    boundary = np.unique(_border(labels))
    connected = np.isin(labels, boundary[boundary != 0]) & (candidates != 0)
    height, width = connected.shape
    inset_y, inset_x = max(1, height // 10), max(1, width // 10)
    interior = connected[inset_y:-inset_y, inset_x:-inset_x]
    if not interior.size or np.mean(interior) < 0.2:
        return None
    if np.count_nonzero(distance > 28) < max(4, distance.size * 0.0001):
        raise ImageInputError("图片只有底色，没有可提取的图案。")

    full_rgb = pixels[:, :, :3]
    full_distance = np.linalg.norm(full_rgb.astype(np.float32) - background, axis=2)
    candidates = np.uint8(full_distance <= 10)
    _, labels = cv2.connectedComponents(candidates, connectivity=8)
    boundary = np.unique(_border(labels))
    exterior = np.isin(labels, boundary[boundary != 0]) & (candidates != 0)
    # Start with every non-background component, including detached text, rather
    # than a model's selected subject. Enclosed same-color design fills survive.
    initial_alpha = np.where(exterior, 0, 255).astype(np.uint8)
    foreground, matte, cleanup = _unmix_chroma_edges(
        full_rgb, background, exterior, native_alpha=initial_alpha, flat_artwork=True
    )
    # Same-color enclosed fills (e.g. white eyes on white) are not evidence of
    # removable background, even if they fall inside the narrow cleanup band.
    enclosed_fill = (full_distance <= 10) & ~exterior
    foreground[enclosed_fill] = full_rgb[enclosed_fill]
    matte[enclosed_fill] = 255
    buffer = BytesIO()
    Image.fromarray(np.dstack([foreground, matte])).save(buffer, "PNG", optimize=True)
    return buffer.getvalue(), {
        **cleanup,
        "method": "solid-background-to-alpha",
        "estimated_background_color": [int(value) for value in background],
        "key_mode": "connected-flat",
        "selection": "flat-canvas-all-artwork",
        "transparent_background": True,
    }
