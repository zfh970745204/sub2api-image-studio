import uuid
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageColor, ImageDraw, ImageFont
from sqlalchemy import select
from test_assets import asset_context as asset_fixture
from test_assets import client_for, login, seed_user, upload

from app.image_ops import ImageInputError
from app.repositories.models import Asset, PointAccount
from app.services.image_executor import ImageJobExecutor
from app.smart_cutout import prepare_smart_cutout
from app.workers.worker import execute_image_job

asset_context = asset_fixture


def png(image):
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def artwork(background="white", ink="black"):
    image = Image.new("RGB", (360, 400), background)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=22)
    # Widely separated rows and colored digits are as important as the mascot.
    draw.text((24, 20), "IN MY MIND", font=font, fill=ink)
    draw.text((24, 48), "I'M STILL", font=font, fill=ink)
    draw.text((190, 48), "24", font=font, fill="#DE1020")
    draw.text((24, 76), "BUT MY BACK IS", font=font, fill=ink)
    draw.text((225, 76), "55", font=font, fill="#DE1020")
    draw.ellipse((110, 140, 250, 280), fill="#2376A0")
    draw.ellipse((140, 164, 171, 194), fill=background)
    draw.ellipse((150, 174, 160, 184), fill=ink)
    for y, text in ((306, "MY KNEE IS 67"), (334, "AND MY LEFT HIP"), (362, "TURNS 79 NEXT WEEK")):
        draw.text((24, y), text, font=font, fill=ink)
    draw.line((80, 150, 75, 265), fill="#777777", width=1)
    draw.ellipse((50, 120, 52, 122), fill="#DE1020")
    return image


@pytest.mark.parametrize(
    "background,ink",
    [("white", "black"), ("black", "white"), ("#245EAA", "white"), ("#00FF00", "black")],
)
def test_flat_artwork_keeps_all_text_digits_detached_marks_and_enclosed_ink(background, ink):
    original = artwork(background, ink)
    result, metadata = prepare_smart_cutout(png(original))
    with Image.open(BytesIO(result)) as image:
        pixels = np.asarray(image)
        original_pixels = np.asarray(original)
        difference = np.linalg.norm(
            original_pixels.astype(float) - np.asarray(original_pixels[0, 0], dtype=float), axis=2
        )
        # Every row, digit, mascot part and one-pixel detail survives, including
        # standalone content that has no connection to the central subject.
        assert np.all(pixels[:, :, 3][difference > 60] >= 32)
        solid_letters = np.all(original_pixels[307:390] == ImageColor.getrgb(ink), axis=2)
        assert np.all(pixels[307:390, :, 3][solid_letters] == 255)
        assert image.getpixel((1, 1))[3] == 0
        assert image.getpixel((143, 178)) == (*original.getpixel((143, 178)), 255)
        assert image.getpixel((51, 121)) == (222, 16, 32, 255)
        composite = Image.alpha_composite(Image.new("RGBA", image.size, background), image)
        delta = np.abs(np.asarray(composite)[:, :, :3].astype(int) - original_pixels)
        # The background detector tolerates ten RGB distance units of noise;
        # real foreground should recompose within rounding error.
        assert delta[difference > 10].max() <= 2
        assert delta[difference <= 10].max() <= 10
    assert metadata["selection"] == "flat-canvas-all-artwork"


@pytest.mark.parametrize("background,ink", [(255, 0), (0, 255)])
def test_flat_letter_soft_edge_has_no_background_color_outline(background, ink):
    # Known coverage is measured on both original and contrasting products.
    size = 160
    yy, xx = np.mgrid[:size, :size]
    depth = np.minimum.reduce([xx - 30, size - 30 - xx, yy - 30, size - 30 - yy])
    coverage = np.clip(depth / 5, 0, 1)
    mixed = np.uint8(np.rint(coverage * ink + (1 - coverage) * background))
    raw = png(Image.fromarray(np.repeat(mixed[:, :, None], 3, axis=2)))
    result, _ = prepare_smart_cutout(raw)
    with Image.open(BytesIO(result)) as image:
        pixels = np.asarray(image)
        edge = (coverage >= 0.2) & (coverage < 1)
        assert np.max(np.abs(pixels[:, :, 3][edge] - coverage[edge] * 255)) <= 2
        assert np.max(np.abs(pixels[:, :, :3][edge].astype(int) - ink)) <= 2


def test_already_transparent_print_is_preserved_exactly():
    original = Image.new("RGBA", (160, 160))
    draw = ImageDraw.Draw(original)
    draw.text((10, 10), "TOP 55", fill=(220, 10, 30, 255))
    draw.text((10, 130), "BOTTOM 79", fill=(0, 0, 0, 255))
    draw.line((30, 40, 80, 100), fill=(230, 230, 230, 64), width=2)
    result, metadata = prepare_smart_cutout(png(original))
    with Image.open(BytesIO(result)) as image:
        np.testing.assert_array_equal(np.asarray(image), np.asarray(original))
    assert metadata["method"] == "native-alpha"


def test_gradient_photo_white_frame_and_painted_checkerboard_are_not_flat_backgrounds():
    yy, xx = np.mgrid[:180, :180]
    photo = Image.fromarray(np.uint8(np.dstack([xx, yy, (xx + yy) / 2]) + 30))
    framed = Image.new("RGB", (200, 200), "white")
    framed.paste(photo, (10, 10))
    checkerboard = Image.fromarray(np.uint8(200 + ((xx // 12 + yy // 12) % 2) * 55))
    for source in (photo, framed, checkerboard):
        assert prepare_smart_cutout(png(source)) is None
    for filename in ("shirt-source-v3.webp", "mug-source-v3.webp"):
        original = Path(__file__).resolve().parents[2] / "frontend/public/brand" / filename
        assert prepare_smart_cutout(original.read_bytes()) is None


@pytest.mark.parametrize("mode,color", [("RGB", "white"), ("RGBA", (0, 0, 0, 0))])
def test_blank_images_do_not_produce_successful_empty_cutouts(mode, color):
    with pytest.raises(ImageInputError):
        prepare_smart_cutout(png(Image.new(mode, (128, 128), color)))


@pytest.mark.asyncio
async def test_white_print_job_keeps_all_words_without_running_subject_model(asset_context):
    owner = await seed_user(asset_context, email="flat-print-owner@example.test")
    original = artwork()
    async with client_for(asset_context, "flat-print-owner") as client:
        await login(client, owner.email)
        source = (await upload(client, png(original))).json()["asset"]
        quoted = await client.post(
            "/api/v1/jobs/quote",
            json={"operation_code": "cutout.smart", "source_asset_id": source["id"]},
        )
        quote = quoted.json()["quote"]
        created = await client.post(
            "/api/v1/jobs",
            headers={"Idempotency-Key": "white-print-cutout"},
            json={"quote_id": quote["id"]},
        )
        assert created.status_code == 201, created.text
        job_id = created.json()["job"]["id"]

    async def reject_model(source):
        raise AssertionError("A subject model must not discard separate printed words")

    executor = ImageJobExecutor(
        asset_context.settings, asset_context.database, asset_context.storage
    )
    executor.background_remover = SimpleNamespace(remove=reject_model)
    ctx = {
        "runtime": SimpleNamespace(
            database=asset_context.database, instance_name="flat-print-test"
        ),
        "image_job_executor": executor,
    }
    assert (await execute_image_job(ctx, job_id))["status"] == "succeeded"
    assert (await execute_image_job(ctx, job_id))["status"] == "ignored"
    async with asset_context.database.session_factory() as session:
        asset = await session.scalar(select(Asset).where(Asset.source_job_id == uuid.UUID(job_id)))
        assert asset.status == "ready" and asset.has_alpha
        assert asset.asset_metadata["edit_source_ready"] is True
        assert "_edit_source_data" not in asset.asset_metadata
        source_key = f"{asset.object_key.rsplit('/', 1)[0]}/cutout-source-v1.png"
        with Image.open(BytesIO(await asset_context.storage.get_object(source_key))) as edit_source:
            np.testing.assert_array_equal(np.asarray(edit_source), np.asarray(original))
        raw = await asset_context.storage.get_object(asset.object_key)
        with Image.open(BytesIO(raw)) as image:
            alpha = np.asarray(image)[:, :, 3]
            black_and_red = np.min(np.asarray(original), axis=2) < 100
            assert np.all(alpha[black_and_red] >= 128)
        balance = await session.scalar(
            select(PointAccount.balance).where(PointAccount.user_id == owner.id)
        )
        assert balance == 200 - quote["final_points"]
