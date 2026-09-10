from io import BytesIO

import numpy as np
from PIL import Image, ImageDraw

from app.image_ops import (
    ImageInputError,
    apply_color_effect,
    build_preflight_report,
    extract_print_artwork,
    has_chroma_key_background,
    inspect_image,
    normalize_image,
    remove_solid_background,
    restore_print_artwork,
    upscale,
    validate_edit_mask,
    vectorize_artwork,
)


def make_png(width: int = 16, height: int = 12, mode: str = "RGB") -> bytes:
    image = Image.new(
        mode, (width, height), (220, 30, 40, 180) if mode == "RGBA" else (220, 30, 40)
    )
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_normalize_removes_metadata_and_preserves_dimensions() -> None:
    normalized = normalize_image(make_png(), max_megapixels=1)
    assert inspect_image(normalized) == (16, 12, "RGB")


def test_upscale_preserves_alpha_and_scales_dimensions() -> None:
    result = upscale(make_png(mode="RGBA"), scale=4)
    assert inspect_image(result) == (64, 48, "RGBA")


def test_rejects_non_image_input() -> None:
    try:
        normalize_image(b"not an image", max_megapixels=1)
    except ImageInputError as exc:
        assert "Unsupported" in str(exc)
    else:
        raise AssertionError("Invalid input was accepted")


def test_extract_print_builds_alpha_from_garment_border() -> None:
    image = Image.new("RGB", (80, 60), (45, 48, 51))
    for x in range(20, 60):
        for y in range(15, 45):
            image.putpixel((x, y), (220, 35, 55))
    output = BytesIO()
    image.save(output, format="PNG")

    result, metadata = extract_print_artwork(
        output.getvalue(),
        crop=(0, 0, 1, 1),
        texture_reduction=0,
        shadow_reduction=0,
        edge_cleanup=20,
    )
    with Image.open(BytesIO(result)) as extracted:
        alpha = extracted.getchannel("A")
        assert alpha.getpixel((40, 30)) > 220
        assert alpha.getpixel((2, 2)) < 20
    assert metadata["method"] == "guided-garment-color-separation"


def test_logo_restoration_reconstructs_structure_and_scales() -> None:
    result, metadata = restore_print_artwork(
        make_png(20, 15),
        mode="logo",
        scale=2,
        denoise=10,
        deblur=30,
    )
    assert inspect_image(result)[:2] == (40, 30)
    assert metadata["engine"] == "structural-palette-reconstruction"
    assert metadata["generative_detail_reconstruction"] is False


def test_preflight_reports_effective_print_resolution() -> None:
    report = build_preflight_report(
        make_png(3000, 2000, "RGBA"),
        asset_id="asset",
        target_width_cm=25.4,
        target_dpi=300,
    )
    assert report["status"] == "ready"
    assert report["effective_dpi"] == 300
    assert report["has_alpha"] is True


def test_solid_background_removal_preserves_dark_line_art() -> None:
    image = Image.new("RGB", (60, 40), (255, 255, 255))
    for x in range(12, 48):
        for y in range(16, 24):
            image.putpixel((x, y), (5, 5, 5))
    source = BytesIO()
    image.save(source, format="PNG")

    result, metadata = remove_solid_background(source.getvalue())
    with Image.open(BytesIO(result)) as cutout:
        alpha = cutout.getchannel("A")
        assert alpha.getpixel((1, 1)) < 5
        assert alpha.getpixel((30, 20)) > 245
        assert cutout.getpixel((30, 20))[0] < 15
    assert metadata["method"] == "solid-background-to-alpha"


def test_solid_background_removal_keeps_enclosed_background_colored_ink() -> None:
    image = Image.new("RGB", (50, 50), (255, 255, 255))
    for x in range(12, 38):
        for y in range(12, 38):
            image.putpixel((x, y), (0, 0, 0))
    for x in range(17, 33):
        for y in range(17, 33):
            image.putpixel((x, y), (255, 255, 255))
    source = BytesIO()
    image.save(source, format="PNG")

    result, _ = remove_solid_background(source.getvalue())
    with Image.open(BytesIO(result)) as cutout:
        alpha = cutout.getchannel("A")
        assert alpha.getpixel((1, 1)) < 5
        assert alpha.getpixel((25, 25)) > 245


def test_solid_background_removal_removes_enclosed_chroma_key() -> None:
    green = (0, 242, 19)
    image = Image.new("RGB", (60, 60), green)
    image.putpixel((1, 1), (4, 225, 12))
    for x in range(10, 50):
        for y in range(10, 50):
            image.putpixel((x, y), (5, 5, 5))
    for x in range(16, 44):
        for y in range(16, 44):
            image.putpixel((x, y), green)
    for x in range(25, 35):
        for y in range(25, 35):
            image.putpixel((x, y), (255, 255, 255))
    source = BytesIO()
    image.save(source, format="PNG")

    result, metadata = remove_solid_background(source.getvalue())
    with Image.open(BytesIO(result)) as cutout:
        alpha = cutout.getchannel("A")
        assert alpha.getpixel((1, 1)) < 5
        assert alpha.getpixel((18, 18)) < 5
        assert alpha.getpixel((12, 12)) > 245
        assert alpha.getpixel((30, 30)) > 245
    assert metadata["key_mode"] == "green"


def test_solid_background_removal_despills_antialiased_edge() -> None:
    green = np.array((0, 242, 19), dtype=np.float32)
    white = np.array((255, 255, 255), dtype=np.float32)
    image = Image.new("RGB", (30, 30), tuple(green.astype(np.uint8)))
    # A real foreground reference is required; a lone pale-green pixel is ambiguous.
    ImageDraw.Draw(image).rectangle((16, 10, 24, 20), fill=(255, 255, 255))
    blended = tuple(np.uint8((green + white) / 2))
    image.putpixel((15, 15), blended)
    source = BytesIO()
    image.save(source, format="PNG")

    result, _ = remove_solid_background(source.getvalue())
    with Image.open(BytesIO(result)) as cutout:
        red, green_value, blue, alpha = cutout.getpixel((15, 15))
        assert 115 < alpha < 140
        assert min(red, green_value, blue) > 245
        assert max(red, green_value, blue) - min(red, green_value, blue) < 5


def test_detects_generated_chroma_key_background() -> None:
    chroma = Image.new("RGB", (80, 60), (0, 245, 12))
    chroma.paste((20, 20, 20), (20, 15, 60, 45))
    source = BytesIO()
    chroma.save(source, format="PNG")
    assert has_chroma_key_background(source.getvalue()) is True

    photo_like = Image.new("RGB", (80, 60), (170, 150, 120))
    photo_like.paste((80, 110, 140), (0, 0, 40, 12))
    source = BytesIO()
    photo_like.save(source, format="PNG")
    assert has_chroma_key_background(source.getvalue()) is False


def test_edit_mask_requires_matching_alpha_selection() -> None:
    source = make_png(24, 18)
    mask = Image.new("RGBA", (24, 18), (0, 0, 0, 255))
    mask.putpixel((12, 9), (0, 0, 0, 0))
    buffer = BytesIO()
    mask.save(buffer, format="PNG")
    metadata = validate_edit_mask(source, buffer.getvalue())
    assert metadata["mask_selected_pixels"] == 1

    wrong_size = Image.new("RGBA", (12, 9), (0, 0, 0, 0))
    buffer = BytesIO()
    wrong_size.save(buffer, format="PNG")
    try:
        validate_edit_mask(source, buffer.getvalue())
    except ImageInputError as exc:
        assert "尺寸一致" in str(exc)
    else:
        raise AssertionError("Expected mismatched masks to be rejected")


def test_color_effect_preserves_transparency() -> None:
    image = Image.new("RGBA", (16, 12), (100, 40, 20, 255))
    image.putpixel((0, 0), (90, 20, 10, 0))
    source = BytesIO()
    image.save(source, format="PNG")
    result, metadata = apply_color_effect(source.getvalue(), mode="monochrome", color="#1d66cc")
    with Image.open(BytesIO(result)) as colored:
        assert colored.getpixel((8, 6)) == (29, 102, 204, 255)
        assert colored.getpixel((0, 0)) == (0, 0, 0, 0)
    assert metadata["color_mode"] == "monochrome"


def test_vectorize_artwork_returns_downloadable_svg() -> None:
    image = Image.new("RGB", (80, 60), (255, 255, 255))
    for x in range(18, 62):
        for y in range(14, 46):
            image.putpixel((x, y), (20, 40, 180))
    source = BytesIO()
    image.save(source, format="PNG")
    result, metadata = vectorize_artwork(source.getvalue(), max_colors=4)
    assert result.startswith(b'<?xml version="1.0"')
    assert b'<path fill="#' in result
    assert metadata["vector_path_groups"] >= 1
