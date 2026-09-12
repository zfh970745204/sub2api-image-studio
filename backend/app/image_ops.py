from __future__ import annotations

import importlib.util
import math
from functools import lru_cache
from io import BytesIO
from threading import RLock
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageColor, ImageFilter, ImageOps, UnidentifiedImageError

from .config import Settings, get_settings
from .services.background_models import resolve_background_model
from .services.image_runtime import configure_image_runtime

_background_lock = RLock()


class ImageInputError(ValueError):
    pass


def normalize_image(raw: bytes, *, max_megapixels: int) -> bytes:
    if not raw:
        raise ImageInputError("The uploaded image is empty.")

    try:
        with Image.open(BytesIO(raw)) as source:
            source.load()
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > max_megapixels * 1_000_000:
                raise ImageInputError(
                    f"Image exceeds the {max_megapixels} megapixel processing limit."
                )
            image = ImageOps.exif_transpose(source)
            if "A" in image.getbands():
                image = image.convert("RGBA")
            else:
                image = image.convert("RGB")
            output = BytesIO()
            image.save(output, format="PNG", optimize=True)
            return output.getvalue()
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageInputError("Unsupported or corrupted image file.") from exc


def inspect_image(raw: bytes) -> tuple[int, int, str]:
    with Image.open(BytesIO(raw)) as image:
        image.load()
        width, height = image.size
        has_alpha = "A" in image.getbands()
        return width, height, "RGBA" if has_alpha else "RGB"


def validate_edit_mask(source_png: bytes, mask_png: bytes) -> dict[str, Any]:
    with Image.open(BytesIO(source_png)) as source, Image.open(BytesIO(mask_png)) as mask:
        source.load()
        mask.load()
        if source.size != mask.size:
            raise ImageInputError("局部修复遮罩必须与原图尺寸一致。")
        if "A" not in mask.getbands():
            raise ImageInputError("局部修复遮罩必须包含透明通道。")
        alpha = np.asarray(mask.getchannel("A"), dtype=np.uint8)
        selected = int(np.count_nonzero(alpha < 250))
        if selected == 0:
            raise ImageInputError("请先在图片上涂抹需要修改的区域。")
        return {
            "mask_selected_pixels": selected,
            "mask_selected_ratio": round(selected / alpha.size, 4),
        }


def real_esrgan_available() -> bool:
    return importlib.util.find_spec("realesrgan_ncnn_py") is not None


@lru_cache(maxsize=1)
def _background_session(model_name: str, model_path: str, threads: int):
    import onnxruntime as ort
    from rembg.sessions import sessions_class

    session_class = next((item for item in sessions_class if item.name() == model_name), None)
    if session_class is None:
        raise RuntimeError("未安装所选抠图模型的推理引擎。")

    class LocalSession(session_class):
        @classmethod
        def download_models(cls, *args, **kwargs):
            return model_path

    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    return LocalSession(
        model_name, options, providers=["CPUExecutionProvider"], model_path=model_path
    )


def remove_background(
    raw_png: bytes, model_name: str, *, settings: Settings | None = None
) -> bytes:
    settings = settings or get_settings()
    configure_image_runtime(settings)
    model_path = resolve_background_model(settings, model_name)
    try:
        from rembg import remove
    except ImportError as exc:
        raise RuntimeError("Background removal dependencies are not installed.") from exc
    except SystemExit as exc:
        raise RuntimeError("The ONNX background removal runtime could not be loaded.") from exc

    try:
        with _background_lock:
            session = _background_session(
                model_name, str(model_path), settings.background_model_threads
            )
        result = remove(raw_png, session=session)
    except SystemExit as exc:
        raise RuntimeError("The ONNX background removal runtime could not be loaded.") from exc
    if not isinstance(result, bytes):
        output = BytesIO()
        result.save(output, format="PNG", optimize=True)
        return output.getvalue()
    return result


def upscale(raw_png: bytes, scale: int, sharpen: bool = True) -> bytes:
    if scale not in (2, 4):
        raise ImageInputError("Upscale factor must be 2 or 4.")

    with Image.open(BytesIO(raw_png)) as source:
        source.load()
        image = source.convert("RGBA" if "A" in source.getbands() else "RGB")
        target = (image.width * scale, image.height * scale)
        if target[0] * target[1] > 80_000_000:
            raise ImageInputError("Upscaled output exceeds the 80 megapixel processing limit.")

        if image.mode == "RGBA":
            # Pillow resizes RGBA in premultiplied-alpha space. Resizing RGB and
            # alpha separately bleeds invisible green/magenta pixels into the edge.
            result = image.resize(target, Image.Resampling.LANCZOS)
            if sharpen:
                rgb = result.convert("RGB")
                interior = result.getchannel("A").filter(ImageFilter.MinFilter(7))
                interior = interior.point(lambda value: 255 if value == 255 else 0)
                sharpened = rgb.filter(ImageFilter.UnsharpMask(radius=1.1, percent=90, threshold=3))
                alpha = result.getchannel("A")
                result = Image.composite(sharpened, rgb, interior).convert("RGBA")
                result.putalpha(alpha)
        else:
            result = image.resize(target, Image.Resampling.LANCZOS)
            if sharpen:
                result = result.filter(ImageFilter.UnsharpMask(radius=1.1, percent=90, threshold=3))

        output = BytesIO()
        result.save(output, format="PNG", optimize=True)
        return output.getvalue()


@lru_cache(maxsize=2)
def _super_resolution_model(model: int):
    try:
        from realesrgan_ncnn_py import Realesrgan
    except ImportError as exc:
        raise RuntimeError("Real-ESRGAN is not installed.") from exc

    # CPU mode is slower but predictable on machines without a Vulkan-capable GPU.
    return Realesrgan(gpuid=-1, tta_mode=False, tilesize=128, model=model)


def restore_print_artwork(
    raw_png: bytes,
    *,
    mode: str,
    scale: int,
    denoise: int = 35,
    deblur: int = 35,
) -> tuple[bytes, dict[str, Any]]:
    if mode not in {"faithful", "illustration", "logo"}:
        raise ImageInputError("Unsupported restoration mode.")
    if scale not in {2, 4}:
        raise ImageInputError("Restoration scale must be 2 or 4.")
    if not 0 <= denoise <= 100 or not 0 <= deblur <= 100:
        raise ImageInputError("Restoration strengths must be between 0 and 100.")

    with Image.open(BytesIO(raw_png)) as source:
        source.load()
        has_alpha = "A" in source.getbands()
        rgba = source.convert("RGBA")
        if rgba.width * rgba.height > 8_000_000:
            raise ImageInputError(
                "AI restoration accepts up to 8 megapixels per task. Crop the print first."
            )

        rgb = np.asarray(rgba.convert("RGB"), dtype=np.uint8)
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

        if denoise:
            h = 2 + round(denoise / 100 * 7)
            if mode == "logo":
                bgr = cv2.bilateralFilter(bgr, 7, 18 + denoise, 18 + denoise)
            else:
                bgr = cv2.fastNlMeansDenoisingColored(bgr, None, h, h, 7, 21)

        if mode == "logo":
            result_rgb = _reconstruct_flat_artwork(bgr, scale, deblur)
            engine = "structural-palette-reconstruction"
            generative = False
        else:
            model_id = 3 if mode == "illustration" else 4
            cleaned = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), "RGB")
            try:
                restored = _super_resolution_model(model_id).process_pil(cleaned)
                engine = (
                    "real-esrgan-x4plus-anime" if mode == "illustration" else "real-esrgan-x4plus"
                )
            except (RuntimeError, OSError, ValueError) as exc:
                raise RuntimeError(f"Real-ESRGAN could not process this image: {exc}") from exc
            target_size = (rgba.width * scale, rgba.height * scale)
            if restored.size != target_size:
                restored = restored.resize(target_size, Image.Resampling.LANCZOS)
            result_rgb = np.asarray(restored, dtype=np.uint8)
            result_rgb = _controlled_sharpen(result_rgb, deblur)
            generative = True

        result = Image.fromarray(result_rgb, "RGB")
        if has_alpha:
            alpha = rgba.getchannel("A").resize(result.size, Image.Resampling.LANCZOS)
            result = result.convert("RGBA")
            result.putalpha(alpha)

        output = BytesIO()
        result.save(output, format="PNG", optimize=True)
        return output.getvalue(), {
            "restoration_mode": mode,
            "engine": engine,
            "scale": scale,
            "denoise": denoise,
            "deblur": deblur,
            "generative_detail_reconstruction": generative,
            "source_dimensions": [rgba.width, rgba.height],
            "output_dimensions": [result.width, result.height],
        }


def _controlled_sharpen(rgb: np.ndarray, strength: int) -> np.ndarray:
    if strength <= 0:
        return rgb
    sigma = 0.8 + strength / 100 * 1.4
    amount = 0.25 + strength / 100 * 0.7
    blurred = cv2.GaussianBlur(rgb, (0, 0), sigma)
    return cv2.addWeighted(rgb, 1 + amount, blurred, -amount, 0)


def _reconstruct_flat_artwork(bgr: np.ndarray, scale: int, deblur: int) -> np.ndarray:
    pixels = bgr.reshape((-1, 3)).astype(np.float32)
    color_count = int(np.clip(round(math.sqrt(len(pixels)) / 45), 6, 24))
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 0.6)
    _, labels, centers = cv2.kmeans(
        pixels,
        color_count,
        None,
        criteria,
        3,
        cv2.KMEANS_PP_CENTERS,
    )
    quantized = centers[labels.flatten()].reshape(bgr.shape).astype(np.uint8)
    quantized = cv2.bilateralFilter(quantized, 9, 55, 55)
    enlarged = cv2.resize(
        quantized,
        (bgr.shape[1] * scale, bgr.shape[0] * scale),
        interpolation=cv2.INTER_CUBIC,
    )
    rgb = cv2.cvtColor(enlarged, cv2.COLOR_BGR2RGB)
    return _controlled_sharpen(rgb, max(45, deblur))


def extract_print_artwork(
    raw_png: bytes,
    *,
    crop: tuple[float, float, float, float],
    background_tolerance: int = 35,
    texture_reduction: int = 45,
    shadow_reduction: int = 55,
    edge_cleanup: int = 35,
) -> tuple[bytes, dict[str, Any]]:
    if any(value < 0 or value > 1 for value in crop):
        raise ImageInputError("Crop coordinates must be normalized between 0 and 1.")
    x, y, width, height = crop
    if width <= 0.02 or height <= 0.02 or x + width > 1.0001 or y + height > 1.0001:
        raise ImageInputError("The print selection is outside the source image.")
    for value in (background_tolerance, texture_reduction, shadow_reduction, edge_cleanup):
        if not 0 <= value <= 100:
            raise ImageInputError("Extraction strengths must be between 0 and 100.")

    with Image.open(BytesIO(raw_png)) as source:
        source.load()
        image = np.asarray(source.convert("RGB"), dtype=np.uint8)
    left = round(x * image.shape[1])
    top = round(y * image.shape[0])
    right = max(left + 1, round((x + width) * image.shape[1]))
    bottom = max(top + 1, round((y + height) * image.shape[0]))
    image = image[top:bottom, left:right].copy()

    if texture_reduction:
        diameter = 5 if texture_reduction < 60 else 7
        strength = 18 + texture_reduction * 0.8
        image = cv2.bilateralFilter(image, diameter, strength, strength)

    if shadow_reduction:
        luminance = cv2.cvtColor(image, cv2.COLOR_RGB2LAB)[:, :, 0].astype(np.float32)
        sigma = max(9, min(image.shape[:2]) * 0.05)
        illumination = cv2.GaussianBlur(luminance, (0, 0), sigma)
        target = float(np.median(illumination))
        correction = (target - illumination) * (shadow_reduction / 100) * 0.65
        lab = cv2.cvtColor(image, cv2.COLOR_RGB2LAB).astype(np.float32)
        lab[:, :, 0] = np.clip(lab[:, :, 0] + correction, 0, 255)
        image = cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2RGB)

    border = max(2, round(min(image.shape[:2]) * 0.035))
    border_pixels = np.concatenate(
        [
            image[:border].reshape(-1, 3),
            image[-border:].reshape(-1, 3),
            image[:, :border].reshape(-1, 3),
            image[:, -border:].reshape(-1, 3),
        ],
        axis=0,
    )
    base_rgb = np.median(border_pixels, axis=0).astype(np.uint8)
    lab_image = cv2.cvtColor(image, cv2.COLOR_RGB2LAB).astype(np.float32)
    base_lab = cv2.cvtColor(base_rgb.reshape(1, 1, 3), cv2.COLOR_RGB2LAB).astype(np.float32)[0, 0]
    distance = np.linalg.norm(lab_image - base_lab, axis=2)
    low = 3 + background_tolerance * 0.22
    high = low + 8 + (100 - background_tolerance) * 0.18
    alpha = np.clip((distance - low) / max(1, high - low), 0, 1)
    alpha = np.power(alpha, 0.82)

    if edge_cleanup:
        kernel_size = 3 if edge_cleanup < 70 else 5
        alpha_u8 = np.uint8(alpha * 255)
        alpha_u8 = cv2.morphologyEx(
            alpha_u8,
            cv2.MORPH_OPEN,
            np.ones((kernel_size, kernel_size), np.uint8),
        )
        blur_sigma = 0.35 + edge_cleanup / 100 * 0.9
        alpha = cv2.GaussianBlur(alpha_u8, (0, 0), blur_sigma).astype(np.float32) / 255

    # Remove garment-color contamination from translucent boundary pixels.
    alpha_safe = np.maximum(alpha[:, :, None], 0.08)
    foreground = (image.astype(np.float32) - (1 - alpha[:, :, None]) * base_rgb) / alpha_safe
    foreground = np.where(alpha[:, :, None] > 0.02, foreground, 0)
    rgba = np.dstack([np.clip(foreground, 0, 255).astype(np.uint8), np.uint8(alpha * 255)])

    result = Image.fromarray(rgba, "RGBA")
    output = BytesIO()
    result.save(output, format="PNG", optimize=True)
    return output.getvalue(), {
        "crop": {"x": x, "y": y, "width": width, "height": height},
        "estimated_garment_color": [int(value) for value in base_rgb],
        "background_tolerance": background_tolerance,
        "texture_reduction": texture_reduction,
        "shadow_reduction": shadow_reduction,
        "edge_cleanup": edge_cleanup,
        "method": "guided-garment-color-separation",
    }


def print_extraction_key_color(raw_png: bytes) -> str:
    """Choose the least represented key hue instead of asking the model to guess."""
    with Image.open(BytesIO(raw_png)) as source:
        image = source.convert("RGBA")
        image.thumbnail((512, 512))
        pixels = np.asarray(image, dtype=np.int16)
    red, green, blue, alpha = np.moveaxis(pixels, -1, 0)
    greens = np.count_nonzero((green - np.maximum(red, blue) > 25) & (alpha > 128))
    magentas = np.count_nonzero((np.minimum(red, blue) - green > 25) & (alpha > 128))
    return "#FF00FF" if greens > magentas else "#00FF00"


def finalize_print_extraction(
    raw_png: bytes, *, key_color: str | None = None, require_native_alpha: bool = False
) -> tuple[bytes, dict[str, Any]]:
    """Validate direct AI extraction without matting; legacy callers can still key flat backgrounds."""
    with Image.open(BytesIO(raw_png)) as source:
        source.load()
        rgba = source.convert("RGBA")
        alpha_min, alpha_max = rgba.getchannel("A").getextrema()
        if alpha_max == 0:
            raise ImageInputError("未提取到有效印花，请上传印花更清晰的产品照片后重试。")
        if require_native_alpha:
            alpha = np.asarray(rgba.getchannel("A"))
            border = np.concatenate([alpha[0, :], alpha[-1, :], alpha[:, 0], alpha[:, -1]])
            # An alpha channel or one transparent pixel inside a product photo is
            # insufficient: the requested margin must actually be transparent.
            if alpha_min != 0 or np.mean(border == 0) < 0.5:
                raise ImageInputError(
                    "图片服务未返回有效的透明底印花，请重试；若仍失败，请联系管理员检查图片服务。"
                )
        if alpha_min == 0 and alpha_max > 0:
            metadata: dict[str, Any] = {"method": "native-alpha", "transparent_background": True}
            # Native alpha alone is not evidence of color spill. Only use a known
            # background explicitly requested for this generation.
            if require_native_alpha:
                metadata["color_preservation"] = "native-rgba-unchanged"
            elif key_color in {"#00FF00", "#FF00FF"}:
                pixels = np.asarray(rgba, dtype=np.uint8)
                rgb, alpha, cleanup = _unmix_chroma_edges(
                    pixels[:, :, :3],
                    np.asarray(ImageColor.getrgb(key_color), dtype=np.float32),
                    pixels[:, :, 3] == 0,
                    native_alpha=pixels[:, :, 3],
                )
                rgba = Image.fromarray(np.dstack([rgb, alpha]), "RGBA")
                metadata.update(cleanup)
            output = BytesIO()
            rgba.save(output, format="PNG", optimize=True)
            return output.getvalue(), metadata
    if not has_chroma_key_background(raw_png):
        raise ImageInputError(
            "图片服务未返回透明或纯色底的印花，无法安全去除产品背景。请重试，或裁切到印花区域后重新上传。"
        )
    output, metadata = remove_solid_background(raw_png)
    with Image.open(BytesIO(output)) as image:
        alpha_min, alpha_max = image.getchannel("A").getextrema()
        if alpha_min != 0 or alpha_max == 0:
            raise ImageInputError("未提取到有效印花，请上传印花更清晰的产品照片后重试。")
    return output, {**metadata, "transparent_background": True}


def _unmix_chroma_edges(
    image: np.ndarray,
    background: np.ndarray,
    background_mask: np.ndarray,
    *,
    native_alpha: np.ndarray | None = None,
    flat_artwork: bool = False,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Recover C = alpha * F + (1 - alpha) * B in a bounded, evidenced edge band.

    Interior ink is never inpainted or globally desaturated. Narrow strokes can
    donate their clean ridge colors without requiring a thick eroded interior.
    Ambiguous edges without a nearby donor from the same component stay intact.
    """
    radius = int(np.clip(round(min(image.shape[:2]) * 0.006), 8, 12))
    pixels = image.astype(np.float32)
    color_distance = np.linalg.norm(pixels - background, axis=2)
    alpha = (
        native_alpha.astype(np.float32) / 255
        if native_alpha is not None
        else (color_distance > 28).astype(np.float32)
    )
    foreground = image.copy()
    corrected_count = 0
    if np.any(background_mask) and not np.all(background_mask):
        visible = (~background_mask).astype(np.uint8)
        depth = cv2.distanceTransform(visible, cv2.DIST_L2, 5)
        kernel = np.ones((3, 3), np.uint8)
        donor_window = 3 if flat_artwork else 2 * radius + 1
        ridge = (depth >= cv2.dilate(depth, kernel) - 0.01) & (
            color_distance
            >= cv2.dilate(color_distance, np.ones((donor_window,) * 2, np.uint8)) - 0.5
        )
        red, green, blue = np.moveaxis(pixels, -1, 0)
        key_signal = (
            green - np.maximum(red, blue)
            if background[1] > background[0]
            else np.minimum(red, blue) - green
        )
        core = depth > radius
        stable = cv2.dilate(color_distance, kernel) - cv2.erode(color_distance, kernel) <= 8
        trusted_core = core & stable
        # On a flat artwork canvas, an adjacent darker letter must not prevent a
        # red digit or separate fine stroke from donating its own edge color.
        ridge_ink = color_distance >= 32 if flat_artwork else key_signal <= 25
        solid = trusted_core | (ridge & ridge_ink)
        solid &= ~background_mask & (alpha >= 0.98)
        if native_alpha is not None:
            solid &= native_alpha >= 250
        if np.any(solid):
            # Label only the donor boundary, keeping the color lookup small even
            # for large opaque areas. Distance-transform work remains linear.
            donors = solid & (cv2.erode(solid.astype(np.uint8), kernel) == 0)
            donor_distance, nearest = cv2.distanceTransformWithLabels(
                (~donors).astype(np.uint8), cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL
            )
            _, components = cv2.connectedComponents(visible, connectivity=8)
            colors = np.zeros((int(nearest.max()) + 1, 3), dtype=np.float32)
            labels = np.zeros(len(colors), dtype=np.int32)
            colors[nearest[donors]] = pixels[donors]
            labels[nearest[donors]] = components[donors]
            # Very thin antialiased strokes may have no fully opaque local pixel.
            # Borrow a cleaner color along the same stroke only when both colors
            # lie on the same key/ink mixing line. Never replace interior colors.
            donor_colors = pixels[donors]
            vectors = donor_colors - background
            lengths = np.maximum(np.linalg.norm(vectors, axis=1), 1)
            bins = np.rint(vectors / lengths[:, None] * 16).astype(np.int32)
            _, groups = np.unique(
                np.column_stack([components[donors], bins]), axis=0, return_inverse=True
            )
            strongest = np.zeros(int(groups.max()) + 1, dtype=np.float32)
            np.maximum.at(strongest, groups, lengths)
            indices = np.full(len(strongest), len(groups), dtype=np.int32)
            np.minimum.at(
                indices,
                groups,
                np.where(lengths == strongest[groups], np.arange(len(groups)), len(groups)),
            )
            cleaner = donor_colors[indices[groups]]
            cleaner_vectors = cleaner - background
            ratio = lengths / np.maximum(np.linalg.norm(cleaner_vectors, axis=1), 1)
            consistent = np.linalg.norm(vectors - ratio[:, None] * cleaner_vectors, axis=1) < 2
            refine = ~trusted_core[donors] & consistent & (ratio >= 0.75)
            colors[nearest[donors][refine]] = cleaner[refine]
            edge = (
                (depth <= radius)
                & ~background_mask
                & (donor_distance <= radius * 3)
                & (labels[nearest] == components)
            )
            # The expensive color math only needs the narrow edge, not every
            # full-resolution pixel; concurrent image jobs keep a smaller footprint.
            samples = pixels[edge]
            estimate = colors[nearest[edge]]
            direction = estimate - background
            fraction = np.clip(
                np.sum((samples - background) * direction, axis=1)
                / np.maximum(np.sum(direction * direction, axis=1), 1),
                0,
                1,
            )
            reconstructed = background + fraction[:, None] * direction
            residual = np.linalg.norm(reconstructed - samples, axis=1)
            # A near-perfect color-line fit is required at low coverage; otherwise
            # dividing by alpha amplifies tiny errors into a new colored fringe.
            confident = (fraction < 0.995) & (residual <= np.maximum(2, fraction * 12))
            unmixed = (samples - (1 - fraction[:, None]) * background) / np.maximum(
                fraction[:, None], 0.02
            )
            edge[edge] = confident
            foreground[edge] = np.uint8(np.clip(np.rint(unmixed[confident]), 0, 255))
            if native_alpha is None:
                alpha[edge] = fraction[confident]
            else:
                # A supplied soft alpha already describes coverage. Do not apply
                # the matte twice and erase smoke/hair. Opaque leftover rims still
                # need their coverage recovered.
                opaque_edge = edge & (native_alpha == 255)
                alpha[opaque_edge] = fraction[confident][native_alpha[edge] == 255]
                # A pixel containing only the known screen color is residual
                # background, not translucent black ink after division by alpha.
                near_key = edge.copy()
                near_key[edge] = fraction[confident] < 0.02
                alpha[near_key] = 0
            corrected_count = int(np.count_nonzero(edge))
    result_alpha = np.uint8(np.clip(np.rint(alpha * 255), 0, 255))
    foreground[result_alpha == 0] = 0
    return (
        foreground,
        result_alpha,
        {
            "edge_cleanup": "local-color-unmix-v3",
            "edge_cleanup_radius": radius,
            "key_color_residual_pixels": corrected_count,
            "color_preservation": "interior-ink-unchanged-v3",
        },
    )


def remove_solid_background(raw_png: bytes) -> tuple[bytes, dict[str, Any]]:
    """Turn a flat background into alpha without erasing neutral printed ink."""
    with Image.open(BytesIO(raw_png)) as source:
        source.load()
        image = np.asarray(source.convert("RGB"), dtype=np.uint8)

    border = max(2, round(min(image.shape[:2]) * 0.025))
    border_pixels = np.concatenate(
        [
            image[:border].reshape(-1, 3),
            image[-border:].reshape(-1, 3),
            image[:, :border].reshape(-1, 3),
            image[:, -border:].reshape(-1, 3),
        ],
        axis=0,
    )
    background = np.median(border_pixels, axis=0).astype(np.uint8)
    background_float = background.astype(np.float32)
    red, green, blue = (float(value) for value in background)
    green_signal = green - max(red, blue)
    magenta_signal = min(red, blue) - green

    if green_signal >= 60:
        key_mode = "green"
    elif magenta_signal >= 60:
        key_mode = "magenta"
    else:
        # Neutral backgrounds can also be printed ink. Remove only regions connected
        # to the canvas boundary so enclosed white fills remain opaque.
        lab_image = cv2.cvtColor(image, cv2.COLOR_RGB2LAB).astype(np.float32)
        lab_background = cv2.cvtColor(background.reshape(1, 1, 3), cv2.COLOR_RGB2LAB).astype(
            np.float32
        )[0, 0]
        distance = np.linalg.norm(lab_image - lab_background, axis=2)
        candidates = np.uint8(distance < 22)
        _, labels = cv2.connectedComponents(candidates, connectivity=8)
        border_labels = np.unique(
            np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
        )
        border_labels = border_labels[border_labels != 0]
        connected_background = np.isin(labels, border_labels) & (candidates == 1)
        soft_edge = np.clip((distance - 2.5) / 16.5, 0, 1)
        alpha = np.where(connected_background, soft_edge, 1.0)
        alpha = cv2.GaussianBlur(alpha.astype(np.float32), (0, 0), 0.35)
        key_mode = "connected-neutral"

    if key_mode != "connected-neutral":
        # Only colors very close to the actual key are background. Hue dominance
        # over the entire image destroys green ink, eyes and magenta design details.
        # Near-key pixels are not automatically thrown away: a faint green edge
        # can be close to a green screen. Keep them eligible for local recovery;
        # isolated background noise without a foreground donor stays transparent.
        key_pixels = np.linalg.norm(image.astype(np.float32) - background_float, axis=2) <= 6
        foreground, alpha_bytes, cleanup = _unmix_chroma_edges(image, background_float, key_pixels)
        output = BytesIO()
        Image.fromarray(np.dstack([foreground, alpha_bytes]), "RGBA").save(
            output, format="PNG", optimize=True
        )
        return output.getvalue(), {
            "method": "solid-background-to-alpha",
            "estimated_background_color": [int(value) for value in background],
            "key_mode": key_mode,
            **cleanup,
        }

    alpha = alpha.astype(np.float32)
    pixels = image.astype(np.float32)
    alpha[alpha < 0.035] = 0
    alpha[alpha > 0.995] = 1
    alpha_safe = np.maximum(alpha[:, :, None], 0.04)
    foreground = (pixels - (1 - alpha[:, :, None]) * background_float) / alpha_safe
    foreground = np.where(alpha[:, :, None] > 0.01, foreground, 0)

    rgba = np.dstack([np.clip(foreground, 0, 255).astype(np.uint8), np.uint8(alpha * 255)])

    output = BytesIO()
    Image.fromarray(rgba, "RGBA").save(output, format="PNG", optimize=True)
    return output.getvalue(), {
        "method": "solid-background-to-alpha",
        "estimated_background_color": [int(value) for value in background],
        "key_mode": key_mode,
        "key_color_residual_pixels": 0,
        "color_preservation": "opaque-foreground-unchanged-v2",
    }


def has_chroma_key_background(raw_png: bytes) -> bool:
    with Image.open(BytesIO(raw_png)) as source:
        source.load()
        image = np.asarray(source.convert("RGB"), dtype=np.uint8)

    border = max(2, round(min(image.shape[:2]) * 0.025))
    border_pixels = np.concatenate(
        [
            image[:border].reshape(-1, 3),
            image[-border:].reshape(-1, 3),
            image[:, :border].reshape(-1, 3),
            image[:, -border:].reshape(-1, 3),
        ],
        axis=0,
    ).astype(np.float32)
    background = np.median(border_pixels, axis=0)
    red, green, blue = (float(value) for value in background)
    chroma_signal = max(green - max(red, blue), min(red, blue) - green)
    variation = np.percentile(np.linalg.norm(border_pixels - background, axis=1), 90)
    return bool(chroma_signal >= 60 and variation <= 42)


def apply_color_effect(
    raw_png: bytes,
    *,
    mode: str,
    color: str = "#111111",
) -> tuple[bytes, dict[str, Any]]:
    if mode not in {"grayscale", "invert", "threshold", "monochrome"}:
        raise ImageInputError("不支持的颜色处理方式。")
    try:
        target_color = ImageColor.getrgb(color)
    except ValueError as exc:
        raise ImageInputError("颜色值无效。") from exc

    with Image.open(BytesIO(raw_png)) as source:
        source.load()
        rgba = np.asarray(source.convert("RGBA"), dtype=np.uint8).copy()

    rgb = rgba[:, :, :3]
    alpha = rgba[:, :, 3]
    if mode == "grayscale":
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        rgb[:] = np.repeat(gray[:, :, None], 3, axis=2)
    elif mode == "invert":
        rgb[:] = 255 - rgb
    elif mode == "threshold":
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        rgb[:] = np.repeat(binary[:, :, None], 3, axis=2)
    else:
        rgb[alpha > 0] = np.asarray(target_color, dtype=np.uint8)
    rgb[alpha == 0] = 0

    output = BytesIO()
    Image.fromarray(rgba, "RGBA").save(output, format="PNG", optimize=True)
    return output.getvalue(), {"color_mode": mode, "target_color": color.lower()}


def vectorize_artwork(
    raw_png: bytes,
    *,
    max_colors: int = 6,
) -> tuple[bytes, dict[str, Any]]:
    if not 2 <= max_colors <= 8:
        raise ImageInputError("矢量化颜色数量必须在 2 到 8 之间。")

    with Image.open(BytesIO(raw_png)) as source:
        source.load()
        rgba = np.asarray(source.convert("RGBA"), dtype=np.uint8)
    height, width = rgba.shape[:2]
    if width * height > 8_000_000:
        raise ImageInputError("矢量化最多支持 800 万像素的图片。")

    rgb = rgba[:, :, :3]
    alpha = rgba[:, :, 3]
    if int(alpha.min()) < 250:
        foreground = alpha > 20
    else:
        border = max(2, round(min(height, width) * 0.025))
        border_pixels = np.concatenate(
            [
                rgb[:border].reshape(-1, 3),
                rgb[-border:].reshape(-1, 3),
                rgb[:, :border].reshape(-1, 3),
                rgb[:, -border:].reshape(-1, 3),
            ],
            axis=0,
        )
        background = np.median(border_pixels, axis=0).astype(np.uint8)
        lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
        background_lab = cv2.cvtColor(background.reshape(1, 1, 3), cv2.COLOR_RGB2LAB).astype(
            np.float32
        )[0, 0]
        foreground = np.linalg.norm(lab - background_lab, axis=2) > 18
        ratio = float(foreground.mean())
        if ratio < 0.005 or ratio > 0.95:
            foreground = np.ones((height, width), dtype=bool)

    pixels = rgb[foreground].astype(np.float32)
    if not pixels.size:
        raise ImageInputError("图片中没有可矢量化的内容。")
    unique = np.unique(pixels.astype(np.uint8), axis=0)
    color_count = min(max_colors, len(unique))
    if len(unique) <= color_count:
        centers = unique.astype(np.float32)
    else:
        step = max(1, len(pixels) // 100_000)
        sample = pixels[::step]
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 25, 0.7)
        cv2.setRNGSeed(17)
        _, _, centers = cv2.kmeans(
            sample,
            color_count,
            None,
            criteria,
            3,
            cv2.KMEANS_PP_CENTERS,
        )

    assigned = np.empty(len(pixels), dtype=np.int16)
    for start in range(0, len(pixels), 200_000):
        chunk = pixels[start : start + 200_000]
        distance = np.sum((chunk[:, None, :] - centers[None, :, :]) ** 2, axis=2)
        assigned[start : start + len(chunk)] = np.argmin(distance, axis=1)

    labels = np.full((height, width), -1, dtype=np.int16)
    labels[foreground] = assigned
    paths: list[str] = []
    palette: list[str] = []
    minimum_area = max(0.8, width * height * 0.0000015)
    for index, center in enumerate(centers):
        mask = np.uint8(labels == index) * 255
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        commands: list[str] = []
        kept = 0
        for contour in contours:
            if abs(cv2.contourArea(contour)) < minimum_area:
                continue
            polygon = cv2.approxPolyDP(contour, 0.55, True).reshape(-1, 2)
            if len(polygon) < 3:
                continue
            points = " ".join(
                f"{'M' if point_index == 0 else 'L'}{int(x)} {int(y)}"
                for point_index, (x, y) in enumerate(polygon)
            )
            commands.append(f"{points} Z")
            kept += 1
            if kept >= 4000:
                break
        if not commands:
            continue
        red, green, blue = (int(np.clip(round(value), 0, 255)) for value in center)
        fill = f"#{red:02x}{green:02x}{blue:02x}"
        palette.append(fill)
        paths.append(f'<path fill="{fill}" fill-rule="evenodd" d="{" ".join(commands)}"/>')

    if not paths:
        raise ImageInputError("图片细节过少，无法生成矢量路径。")
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}">{"".join(paths)}</svg>'
    ).encode()
    return svg, {
        "vector_colors": palette,
        "vector_path_groups": len(paths),
        "source_dimensions": [width, height],
    }


def build_preflight_report(
    raw_png: bytes,
    *,
    asset_id: str,
    target_width_cm: float,
    target_dpi: int,
) -> dict[str, Any]:
    with Image.open(BytesIO(raw_png)) as source:
        source.load()
        width, height = source.size
        has_alpha = "A" in source.getbands()
        alpha_extrema = source.getchannel("A").getextrema() if has_alpha else (255, 255)

    required = math.ceil(target_width_cm / 2.54 * target_dpi)
    effective_dpi = width / (target_width_cm / 2.54)
    ratio = width / required
    if ratio >= 1:
        status = "ready"
    elif ratio >= 0.7:
        status = "review"
    else:
        status = "insufficient"
    warnings: list[str] = []
    if ratio < 1:
        warnings.append(f"目标宽度需要至少 {required} px，当前缺少 {required - width} px。")
    if not has_alpha or alpha_extrema == (255, 255):
        warnings.append("文件没有有效透明背景；用于服装印花前请检查底色。")
    if min(width, height) < 1000:
        warnings.append("短边低于 1000 px，细字、细线和边缘可能无法稳定印刷。")
    return {
        "asset_id": asset_id,
        "width": width,
        "height": height,
        "has_alpha": has_alpha and alpha_extrema != (255, 255),
        "target_width_cm": target_width_cm,
        "target_dpi": target_dpi,
        "required_width_pixels": required,
        "effective_dpi": round(effective_dpi, 1),
        "max_width_cm_at_target_dpi": round(width / target_dpi * 2.54, 1),
        "status": status,
        "warnings": warnings,
    }
