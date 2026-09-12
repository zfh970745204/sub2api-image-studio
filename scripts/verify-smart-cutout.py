"""Compare flat-artwork cutout with subject segmentation on an image or screenshot crop."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np
from PIL import Image, ImageDraw

from app.config import get_settings
from app.services.cutout_process import BackgroundRemovalRunner
from app.smart_cutout import prepare_smart_cutout


def encoded(image):
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def composite(image, color=None):
    canvas = Image.new("RGBA", image.size, color or "#FFFFFF")
    if color is None:
        draw = ImageDraw.Draw(canvas)
        for y in range(0, image.height, 12):
            for x in range(0, image.width, 12):
                if (x // 12 + y // 12) % 2:
                    draw.rectangle((x, y, x + 11, y + 11), fill="#CDD3DC")
    return Image.alpha_composite(canvas, image).convert("RGB")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--crop", type=int, nargs=4)
    parser.add_argument("--compare-model", action="store_true")
    args = parser.parse_args()
    directory = ROOT / "output/image-tools-review/smart-cutout-artwork"
    directory.mkdir(parents=True, exist_ok=True)
    with Image.open(args.image) as image:
        source = image.convert("RGBA")
    if args.crop:
        source = source.crop(args.crop)
    raw = encoded(source)
    started = time.perf_counter()
    result = prepare_smart_cutout(raw)
    if result is None:
        raise RuntimeError("This fixture does not have a confidently flat background")
    output, metadata = result
    elapsed = time.perf_counter() - started
    source.save(directory / "source.png")
    with Image.open(BytesIO(output)) as image:
        cutout = image.convert("RGBA")
    cutout.save(directory / "transparent.png")
    pixels, actual = np.asarray(source), np.asarray(cutout)
    dark_or_color = np.min(pixels[:, :, :3], axis=2) < 180
    metrics = {
        "source_kind": "screenshot crop" if args.crop else "original image",
        "dimensions": source.size,
        "seconds": round(elapsed, 3),
        "metadata": metadata,
        "high_contrast_pixels_removed": int(
            np.count_nonzero(actual[:, :, 3][dark_or_color] < 16)
        ),
        "upstream_ai_called": False,
    }
    panels = [(source.convert("RGB"), "Source crop" if args.crop else "Source")]
    if args.compare_model:

        async def old_model():
            async with asyncio.timeout(180):
                return await BackgroundRemovalRunner(get_settings()).remove(raw)

        baseline = asyncio.run(old_model())
        with Image.open(BytesIO(baseline)) as image:
            original_cutout = image.convert("RGBA")
        original_cutout.save(directory / "subject-model.png")
        baseline_alpha = np.asarray(original_cutout)[:, :, 3]
        metrics["subject_model_high_contrast_pixels_removed"] = int(
            np.count_nonzero(baseline_alpha[dark_or_color] < 16)
        )
        panels.append((composite(original_cutout), "Subject model"))
    panels.extend(
        [
            (composite(cutout), "Fixed / checkerboard"),
            (composite(cutout, "#262B35"), "Fixed / dark background"),
        ]
    )
    sheet = Image.new(
        "RGB", (len(panels) * (source.width + 20) + 20, source.height + 85), "#EDF0F5"
    )
    draw = ImageDraw.Draw(sheet)
    draw.text(
        (20, 12),
        "Actual screenshot crop - no AI redraw"
        if args.crop
        else "Local cutout comparison - no AI redraw",
        fill="black",
    )
    for column, (panel, label) in enumerate(panels):
        x = 20 + column * (source.width + 20)
        draw.text((x, 40), label, fill="black")
        sheet.paste(panel, (x, 65))
    sheet.save(directory / "comparison.png")
    (directory / "metrics.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )
    print(json.dumps(metrics))
    print(directory / "comparison.png")


if __name__ == "__main__":
    main()
