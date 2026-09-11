"""Local model smoke test and deterministic print/color preview; no upstream AI calls."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np
from PIL import Image, ImageDraw

from app.config import get_settings
from app.image_ops import remove_background
from app.print_extraction import finish_print, product_background


def encoded(image):
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-cutout", action="store_true")
    args = parser.parse_args()
    directory = ROOT / "output/image-tools-review"
    directory.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    os.environ.setdefault("NUMBA_CACHE_DIR", str(settings.numba_cache_dir))
    os.environ.setdefault("U2NET_HOME", str(settings.background_model_dir))
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    with Image.open(ROOT / "frontend/public/brand/shirt-source-v3.webp") as source:
        source = source.convert("RGB")
        source.thumbnail((768, 768))
        source_raw = encoded(source)
    metrics = {"upstream_ai_called": False, "product_color": product_background(source_raw)}
    if not args.skip_cutout:
        if not list(Path(os.environ["U2NET_HOME"]).rglob("u2net.onnx")):
            raise RuntimeError("Preload u2net.onnx in the configured model directory before this check")
        runs = []
        for _ in range(2):
            started = time.perf_counter()
            raw = remove_background(source_raw, "u2net")
            with Image.open(BytesIO(raw)) as result:
                alpha = np.asarray(result.convert("RGBA"))[:, :, 3]
                assert np.any(alpha == 0) and np.any(alpha > 200)
                result.save(directory / "smart-cutout.png")
            runs.append(round(time.perf_counter() - started, 3))
        metrics["cutout_seconds"] = runs

    with Image.open(ROOT / "frontend/public/brand/shirt-print-v3.png") as art:
        art = art.convert("RGBA")
        art.thumbnail((768, 768))
        native = art.copy()
    sheet = Image.new("RGB", (1040, 680), "#ECEFF4")
    draw = ImageDraw.Draw(sheet)
    draw.text((20, 10), "Product-color extraction - local fixtures, not a new AI generation", fill="black")
    reports = []
    for row, background in enumerate(("#000000", "#FFFFFF")):
        canvas = Image.new("RGBA", art.size, background)
        flat = Image.alpha_composite(canvas, native).convert("RGB")
        opaque, _ = finish_print(encoded(flat), mode="opaque", color=background)
        transparent, _ = finish_print(encoded(flat), mode="transparent", color=background)
        with Image.open(BytesIO(transparent)) as result:
            recomposed = Image.alpha_composite(canvas, result).convert("RGB")
            delta = np.abs(np.asarray(recomposed).astype(int) - np.asarray(flat).astype(int))
            reports.append({"background": background, "mean_channel_difference_on_product": float(delta.mean()), "max_channel_difference": int(delta.max())})
            result.save(directory / f"transparent-{row}.png")
            checker = Image.new("RGBA", art.size, "#FFFFFF")
            checkdraw = ImageDraw.Draw(checker)
            for y in range(0, checker.height, 16):
                for x in range(0, checker.width, 16):
                    if (x // 16 + y // 16) % 2:
                        checkdraw.rectangle((x, y, x + 15, y + 15), fill="#DADDE2")
            previews = [flat, Image.alpha_composite(checker, result), recomposed]
            for column, (preview, label) in enumerate(zip(previews, ("Opaque product background", "Transparent PNG preview", "On the same product color"), strict=True)):
                preview = preview.copy()
                preview.thumbnail((320, 280))
                sheet.paste(preview.convert("RGB"), (20 + column * 345, 70 + row * 315))
                draw.text((20 + column * 345, 42 + row * 315), f"{background} / {label}", fill="black")
        with Image.open(BytesIO(opaque)) as output:
            output.save(directory / f"opaque-{row}.png")
    metrics["print_cases"] = reports
    sheet.save(directory / "print-modes.png")
    (directory / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps(metrics))
    print(directory / "print-modes.png")


if __name__ == "__main__":
    main()
