from io import BytesIO

import numpy as np
import pytest
from PIL import Image, ImageDraw
from test_assets import asset_context as asset_fixture
from test_assets import client_for, login, seed_user, upload

from app.image_ops import ImageInputError
from app.print_extraction import finish_print, product_background

asset_context = asset_fixture


def png(image):
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def garment(color):
    image = Image.new("RGB", (240, 240), "white")
    draw = ImageDraw.Draw(image)
    draw.polygon(
        [(65, 35), (175, 35), (210, 80), (185, 105), (180, 215), (60, 215), (55, 105), (30, 80)],
        fill=color,
    )
    draw.rectangle((100, 90, 140, 155), fill="#D13A40")
    return png(image)


@pytest.mark.parametrize("color", ["#000000", "#FFFFFF", "#245EAA"])
def test_product_color_is_not_the_studio_surround(color):
    assert product_background(garment(color))["color"] == color


def test_manual_sample_preserves_the_selected_color():
    raw = garment("#152331")
    assert product_background(raw, (0.3, 0.7))["color"] == "#152331"
    assert product_background(raw, (0.02, 0.02))["color"] == "#FFFFFF"
    with pytest.raises(ImageInputError):
        product_background(png(Image.new("RGBA", (80, 80))), (0.5, 0.5))


def test_large_colored_print_on_white_shirt_is_not_the_product_base():
    with Image.open(BytesIO(garment("#FFFFFF"))) as image:
        ImageDraw.Draw(image).rectangle((85, 70, 155, 190), fill="#D13A40")
        assert product_background(png(image))["color"] == "#FFFFFF"


@pytest.mark.parametrize(
    "color,ink", [("#000000", "white"), ("#FFFFFF", "black"), ("#245EAA", "white")]
)
def test_color_background_removed_while_enclosed_same_color_ink_is_preserved(color, ink):
    original = Image.new("RGB", (128, 128), color)
    draw = ImageDraw.Draw(original)
    draw.rectangle((24, 24, 104, 104), fill=ink)
    draw.ellipse((48, 48, 68, 68), fill=color)
    draw.line((12, 15, 15, 95), fill=ink, width=1)
    result, metadata = finish_print(png(original), mode="transparent", color=color)
    with Image.open(BytesIO(result)) as image:
        assert image.getpixel((1, 1))[3] == 0
        assert image.getpixel((58, 58))[3] == 255
        assert image.getpixel((14, 70))[3] == 255
        composite = Image.alpha_composite(Image.new("RGBA", image.size, color), image).convert(
            "RGB"
        )
        np.testing.assert_array_equal(np.array(composite), np.array(original))
    assert metadata["method"] == "connected-product-color"


@pytest.mark.parametrize("background", [0, 255])
def test_soft_matte_does_not_double_darkening_or_whiten_edges_on_product_color(background):
    color = "#000000" if background == 0 else "#FFFFFF"
    pixels = np.full((128, 128, 3), background, dtype=np.uint8)
    ink = 255 - background
    for inset, fraction in [(25, 0.025), (26, 0.04), (27, 0.055), (28, 0.075), (29, 0.2), (30, 1)]:
        pixels[inset:-inset, inset:-inset] = round(background * (1 - fraction) + ink * fraction)
    result, _ = finish_print(png(Image.fromarray(pixels)), mode="transparent", color=color)
    with Image.open(BytesIO(result)) as image:
        alpha = np.array(image)[:, :, 3]
        assert np.any((alpha > 0) & (alpha < 255))
        composite = Image.alpha_composite(Image.new("RGBA", image.size, color), image).convert(
            "RGB"
        )
        assert np.max(np.abs(np.array(composite).astype(int) - pixels)) <= 1


def test_native_alpha_is_preserved_or_composited_to_opaque_black():
    image = Image.new("RGBA", (128, 128))
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, 30, 90, 90), fill=(255, 255, 255, 255))
    draw.line((29, 30, 29, 90), fill=(20, 180, 40, 110))
    raw = png(image)
    transparent, _ = finish_print(raw, mode="transparent", color="#000000")
    opaque, _ = finish_print(raw, mode="opaque", color="#000000")
    with Image.open(BytesIO(transparent)) as result:
        np.testing.assert_array_equal(np.array(image), np.array(result))
    with Image.open(BytesIO(opaque)) as result:
        assert result.mode == "RGB" and result.getpixel((0, 0)) == (0, 0, 0)
        np.testing.assert_array_equal(
            np.array(result),
            np.array(
                Image.alpha_composite(Image.new("RGBA", image.size, "black"), image).convert("RGB")
            ),
        )


@pytest.mark.parametrize("mode", ["transparent", "opaque"])
def test_blank_wrong_color_and_unflattened_outputs_are_not_published(mode):
    checkerboard = Image.new("RGB", (128, 128), "white")
    draw = ImageDraw.Draw(checkerboard)
    for x in range(0, 128, 16):
        draw.rectangle((x, 0, x + 7, 127), fill="#666666")
    for image in (
        Image.new("RGBA", (128, 128)),
        Image.new("RGB", (128, 128), "black"),
        checkerboard,
        Image.new("RGB", (128, 128), "#00FF00"),
    ):
        with pytest.raises(ImageInputError):
            finish_print(png(image), mode=mode, color="#000000")


@pytest.mark.asyncio
async def test_color_endpoint_is_owner_scoped_and_options_are_bound_to_the_quote(asset_context):
    owner = await seed_user(asset_context, email="color-owner@example.test")
    other = await seed_user(asset_context, email="color-other@example.test")
    async with client_for(asset_context, "color-owner") as client:
        await login(client, owner.email)
        asset = (await upload(client, garment("#000000"))).json()["asset"]
        url = f"/api/v1/assets/{asset['id']}/print-background"
        assert (await client.get(url)).json()["color"] == "#000000"
        assert (await client.get(url + "?x=0.02&y=0.02")).json()["color"] == "#FFFFFF"
        assert (await client.get(url + "?x=0.5")).status_code == 422
        assert (await client.get(url + "?x=2&y=0.5")).status_code == 422
        for bad in ({"output_mode": "fake"}, {"background_color": "red"}, {"background_color": []}):
            response = await client.post(
                "/api/v1/jobs/quote",
                json={
                    "operation_code": "ai.extract_print",
                    "source_asset_id": asset["id"],
                    "parameters": bad,
                },
            )
            assert response.status_code == 422
        quotes = []
        for mode in ("transparent", "opaque"):
            parameters = {"quality": "high", "output_mode": mode, "background_color": "#000000"}
            response = await client.post(
                "/api/v1/jobs/quote",
                json={
                    "operation_code": "ai.extract_print",
                    "source_asset_id": asset["id"],
                    "parameters": parameters,
                },
            )
            assert response.status_code == 201, response.text
            quotes.append(response.json()["quote"])
        assert quotes[0]["final_points"] == quotes[1]["final_points"]
        for changed in (
            {**parameters, "background_color": "#FFFFFF"},
            {**parameters, "output_mode": "transparent"},
        ):
            response = await client.post(
                "/api/v1/jobs",
                headers={"Idempotency-Key": "color-change"},
                json={"quote_id": quotes[1]["id"], "parameters": changed},
            )
            assert response.status_code == 409 and response.json()["code"] == "JOB_QUOTE_MISMATCH"
    async with client_for(asset_context, "color-other") as client:
        assert (await client.get(url)).status_code == 401
        await login(client, other.email)
        assert (await client.get(url)).status_code == 404
