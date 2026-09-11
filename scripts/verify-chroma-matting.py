"""Generate deterministic before/after QA; uses local fixtures, no image API.

Run with the dev environment: .venv/Scripts/python.exe scripts/verify-chroma-matting.py
The baseline defaults to the repository HEAD; override --baseline-ref for later comparisons.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]

import numpy as np
from app.image_ops import finalize_print_extraction
from PIL import Image, ImageDraw, ImageFont
from test_chroma_matting import png, rgba, soft_print, thin_print


def composite(rgb, alpha, background):
    coverage = alpha[:, :, None] / 255
    return Image.fromarray(
        np.uint8(np.clip(np.rint(rgb * coverage + background * (1 - coverage)), 0, 255))
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="HEAD")
    parser.add_argument("--output", type=Path, default=ROOT / "output/chroma-review")
    args = parser.parse_args()
    original = subprocess.run(
        ["git", "show", f"{args.baseline_ref}:backend/app/image_ops.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    baseline = ModuleType("chroma_baseline")
    # Deliberately compare against this repository's trusted historical implementation.
    exec(compile(original, "<baseline-image-ops>", "exec"), baseline.__dict__)  # noqa: S102
    args.output.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGB", (1540, 1260), "#edf0f4")
    draw = ImageDraw.Draw(canvas)
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    title = (
        ImageFont.truetype(str(font_path), 25)
        if font_path.exists()
        else ImageFont.load_default(size=25)
    )
    label = (
        ImageFont.truetype(str(font_path), 17)
        if font_path.exists()
        else ImageFont.load_default(size=17)
    )
    draw.text(
        (24, 18),
        "Edge cleanup: controlled green / magenta tests",
        font=title,
        fill="#18212c",
    )
    draw.text(
        (24, 52),
        "Synthetic coverage reference, not a reconstruction of the uploaded screenshot. Detail previews enlarged 2x.",
        font=label,
        fill="#556170",
    )
    for column, text in enumerate(
        [
            "OLD / light",
            "NEW / light",
            "TARGET / light",
            "OLD / dark",
            "NEW / dark",
            "TARGET / dark",
        ]
    ):
        draw.text((24 + column * 253, 89), text, font=label, fill="#18212c")
    metrics = []
    for row, (key, shape) in enumerate(
        [
            ((0, 255, 0), "soft edge"),
            ((255, 0, 255), "soft edge"),
            ((0, 255, 0), "fine strokes"),
            ((255, 0, 255), "fine strokes"),
        ]
    ):
        if shape == "soft edge":
            mixed, alpha, rgb = soft_print((235, 235, 235), key)
            crop = (10, 10, 130, 130)
        else:
            mixed, alpha, rgb = thin_print(key)
            rgb = np.broadcast_to(rgb, (*alpha.shape, 3))
            crop = (65, 55, 185, 175)
        raw = png(mixed)
        old = rgba(baseline.finalize_print_extraction(raw)[0])
        new, metadata = finalize_print_extraction(raw)
        new = rgba(new)
        edge = (alpha >= 42) & (alpha < 250)
        entry = {"key": key, "shape": shape, "metadata": metadata}
        for name, result in [("old", old), ("new", new)]:
            entry[name] = {
                "edge_rgb_error_p95": float(
                    np.percentile(
                        np.max(np.abs(result[:, :, :3].astype(float) - rgb), axis=2)[
                            edge
                        ],
                        95,
                    )
                ),
                "alpha_error_mean": float(
                    np.mean(np.abs(result[:, :, 3].astype(float) - alpha))
                ),
            }
        metrics.append(entry)
        for background, start in [(255, 0), (28, 3)]:
            for index, (colors, coverage) in enumerate(
                [
                    (old[:, :, :3], old[:, :, 3]),
                    (new[:, :, :3], new[:, :, 3]),
                    (rgb, alpha),
                ]
            ):
                preview = (
                    composite(colors, coverage, background)
                    .crop(crop)
                    .resize((240, 240), Image.Resampling.NEAREST)
                )
                canvas.paste(preview, (24 + (start + index) * 253, 144 + row * 278))
        draw.text(
            (24, 120 + row * 278),
            f"{'GREEN' if key[1] else 'MAGENTA'} / {shape}",
            font=label,
            fill="#556170",
        )
    timings = {}
    benchmark_input = png(soft_print((245, 245, 245), (0, 255, 0), size=1254)[0])
    for name, function in [
        ("old", baseline.finalize_print_extraction),
        ("new", finalize_print_extraction),
    ]:
        function(benchmark_input)
        elapsed = []
        for _ in range(3):
            started = time.perf_counter()
            function(benchmark_input)
            elapsed.append(time.perf_counter() - started)
        tracemalloc.start()
        function(benchmark_input)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        timings[name] = {
            "median_seconds_1254px": round(statistics.median(elapsed), 4),
            "traced_peak_mb": round(peak / 1024**2, 1),
        }
    report = {
        "baseline": args.baseline_ref,
        "cases": metrics,
        "timings": timings,
        "limits": "Local synthetic tests; timings exclude external generation/network. Tracemalloc excludes some native library allocations.",
    }
    canvas.save(args.output / "edge-comparison.png")
    (args.output / "metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    print(args.output / "edge-comparison.png")


if __name__ == "__main__":
    main()
