"""Known foreground/coverage fixtures measure edge recovery, not just PNG alpha."""

from io import BytesIO

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from app.image_ops import finalize_print_extraction, upscale


def png(pixels):
    output = BytesIO()
    Image.fromarray(pixels).save(output, "PNG")
    return output.getvalue()


def rgba(raw):
    with Image.open(BytesIO(raw)) as image:
        return np.asarray(image.convert("RGBA"))


def soft_print(ink, key, size=160):
    yy, xx = np.mgrid[:size, :size]
    depth = np.minimum.reduce([xx - 30, size - 30 - xx, yy - 30, size - 30 - yy])
    coverage = np.clip(depth / 6, 0, 1).astype(np.float32)
    rgb = np.broadcast_to(np.array(ink, dtype=np.float32), (size, size, 3))
    mixed = rgb * coverage[:, :, None] + np.array(key) * (1 - coverage[:, :, None])
    return np.uint8(np.rint(mixed)), np.uint8(np.rint(coverage * 255)), rgb


@pytest.mark.parametrize("key", [(0, 255, 0), (255, 0, 255)])
@pytest.mark.parametrize(
    "ink", [(248, 248, 248), (12, 12, 12), (105, 49, 22), (21, 145, 72), (160, 38, 128)]
)
def test_six_pixel_soft_edges_recover_ink_and_coverage_on_light_and_dark(key, ink):
    mixed, expected_alpha, expected_rgb = soft_print(ink, key)
    result, metadata = finalize_print_extraction(png(mixed))
    output = rgba(result)
    edge = (expected_alpha >= 42) & (expected_alpha < 255)
    assert np.max(np.abs(output[:, :, 3][edge].astype(int) - expected_alpha[edge])) <= 3
    assert np.max(np.abs(output[:, :, :3][edge].astype(int) - expected_rgb[edge])) <= 6
    np.testing.assert_array_equal(output[80, 80], (*ink, 255))
    for backdrop in [0, 255, 70]:
        actual = output[:, :, :3] * (output[:, :, 3:] / 255) + backdrop * (
            1 - output[:, :, 3:] / 255
        )
        expected = expected_rgb * (expected_alpha[:, :, None] / 255) + backdrop * (
            1 - expected_alpha[:, :, None] / 255
        )
        assert np.max(np.abs(actual[edge] - expected[edge])) <= 4
    assert metadata["key_color_residual_pixels"] > 0


def thin_print(key):
    scale = 4
    mask = Image.new("L", (192 * scale, 192 * scale))
    draw = ImageDraw.Draw(mask)
    draw.line(
        [
            (36 * scale, 165 * scale),
            (48 * scale, 90 * scale),
            (28 * scale, 52 * scale),
            (42 * scale, 22 * scale),
        ],
        fill=255,
        width=8 * scale,
    )
    for end in [(130, 22), (145, 42), (160, 72), (170, 104)]:
        draw.line(
            [(85 * scale, 164 * scale), (end[0] * scale, end[1] * scale)], fill=255, width=2 * scale
        )
    coverage = np.asarray(
        mask.resize((192, 192), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(0.35))
    )
    ink = np.array([60, 35, 20], dtype=np.float32)
    mixed = ink * (coverage[:, :, None] / 255) + np.array(key) * (1 - coverage[:, :, None] / 255)
    return np.uint8(np.rint(mixed)), coverage, ink


@pytest.mark.parametrize("key", [(0, 255, 0), (255, 0, 255)])
def test_thin_feathers_and_steam_keep_their_coverage(key):
    mixed, coverage, ink = thin_print(key)
    output = rgba(finalize_print_extraction(png(np.uint8(np.rint(mixed))))[0])
    # Hair is never removed wholesale by erosion; total printed coverage stays close.
    assert 0.9 <= output[:, :, 3].sum() / coverage.sum() <= 1.1
    visible = (coverage >= 64) & (output[:, :, 3] >= 32)
    error = np.abs(output[:, :, :3].astype(float) - ink).max(axis=2)
    assert np.percentile(error[visible], 95) <= 8
    assert np.count_nonzero(output[:, :, 3][coverage >= 200] == 0) == 0


@pytest.mark.parametrize("key_color,key", [("#00FF00", (0, 255, 0)), ("#FF00FF", (255, 0, 255))])
def test_known_key_native_alpha_edges_are_cleaned_without_double_matting(key_color, key):
    mixed, coverage, expected_rgb = soft_print((245, 245, 245), key)
    raw = np.dstack([mixed, coverage])
    output = rgba(finalize_print_extraction(png(raw), key_color=key_color)[0])
    np.testing.assert_array_equal(output[:, :, 3], coverage)
    edge = (coverage >= 42) & (coverage < 255)
    assert np.max(np.abs(output[:, :, :3][edge].astype(int) - expected_rgb[edge])) <= 6


@pytest.mark.parametrize("key_color,key", [("#00FF00", (0, 255, 0)), ("#FF00FF", (255, 0, 255))])
def test_pure_key_residual_with_alpha_does_not_become_a_black_outline(key_color, key):
    image = Image.new("RGBA", (80, 80))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 60, 60), fill=(255, 255, 255, 255))
    draw.line((19, 22, 19, 58), fill=(*key, 128))
    output = rgba(finalize_print_extraction(png(np.array(image)), key_color=key_color)[0])
    assert output[40, 19, 3] == 0
    np.testing.assert_array_equal(output[40, 20], (255, 255, 255, 255))


@pytest.mark.parametrize("key_color", [None, "#00FF00", "#FF00FF"])
def test_clean_native_alpha_and_design_colors_remain_exact(key_color):
    image = Image.new("RGBA", (160, 160))
    draw = ImageDraw.Draw(image)
    for i, ink in enumerate([(0, 240, 30), (230, 30, 220), (40, 120, 85), (255, 255, 255)]):
        draw.rectangle((10 + i * 36, 20, 30 + i * 36, 130), fill=(*ink, 255))
        draw.line((9 + i * 36, 20, 9 + i * 36, 130), fill=(*ink, 110), width=1)
    expected = np.array(image)
    output = rgba(finalize_print_extraction(png(expected), key_color=key_color)[0])
    np.testing.assert_array_equal(output, expected)


@pytest.mark.parametrize("key", [(0, 255, 0), (255, 0, 255)])
def test_same_hue_isolated_strokes_and_enclosed_ink_are_preserved(key):
    image = Image.new("RGB", (160, 160), key)
    draw = ImageDraw.Draw(image)
    draw.rectangle((12, 12, 66, 146), fill="white")
    ink = (18, 180, 55) if key[1] else (220, 40, 210)
    draw.line((92, 14, 92, 146), fill=ink, width=1)
    for point in [(22, 30), (31, 46), (60, 60)]:
        draw.point(point, fill=ink)
    output = rgba(finalize_print_extraction(png(np.array(image)))[0])
    for x, y in [(92, 60), (22, 30), (31, 46), (60, 60)]:
        np.testing.assert_array_equal(output[y, x], (*ink, 255))


def test_no_donor_does_not_erase_an_ambiguous_isolated_detail():
    image = np.zeros((40, 40, 4), dtype=np.uint8)
    image[20, 20] = (160, 230, 160, 90)
    result = rgba(finalize_print_extraction(png(image), key_color="#00FF00")[0])
    np.testing.assert_array_equal(result, image)


@pytest.mark.parametrize("key", [(0, 255, 0), (255, 0, 255)])
@pytest.mark.parametrize("scale,sharpen", [(2, False), (4, True)])
def test_upscale_does_not_bleed_invisible_key_rgb_into_print_edges(key, scale, sharpen):
    image = Image.new("RGBA", (40, 40), (*key, 0))
    ImageDraw.Draw(image).rectangle((12, 12, 28, 28), fill=(255, 255, 255, 255))
    output = rgba(upscale(png(np.asarray(image)), scale=scale, sharpen=sharpen))
    edge = (output[:, :, 3] > 20) & (output[:, :, 3] < 250)
    assert np.any(edge)
    assert np.min(output[:, :, :3][edge]) >= 253
    assert output.shape == (40 * scale, 40 * scale, 4)
