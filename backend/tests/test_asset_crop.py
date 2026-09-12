from io import BytesIO
from uuid import UUID

import pytest
from PIL import Image, ImageOps
from sqlalchemy import select
from test_assets import asset_context as asset_fixture
from test_assets import client_for, login, seed_user

from app.repositories.models import Asset, PointAccount
from app.services.asset_crop import crop_asset
from app.services.asset_files import AssetInputError, prepare_asset
from app.services.assets import AssetService

asset_context = asset_fixture


def pixels(image):
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def test_rectangle_preserves_pixels_and_circle_multiplies_existing_alpha():
    original = Image.new("RGBA", (100, 80), (20, 80, 140, 128))
    original.putpixel((40, 40), (240, 30, 90, 0))
    for shape in ("rectangle", "circle"):
        prepared = crop_asset(
            pixels(original),
            x=10,
            y=0,
            width=80,
            height=80,
            shape=shape,
            expected_size=(100, 80),
            max_megapixels=16,
        )
        with Image.open(BytesIO(prepared.data)) as result:
            assert result.size == (80, 80)
            assert result.getpixel((30, 40)) == (240, 30, 90, 0)
            assert result.getpixel((40, 40)) == (20, 80, 140, 128)
            if shape == "rectangle":
                assert result.tobytes() == original.crop((10, 0, 90, 80)).tobytes()
            else:
                assert result.getpixel((0, 0))[3] == 0
                assert any(0 < a < 128 for a in result.getchannel("A").tobytes())


def test_crop_uses_exif_oriented_coordinates():
    original = Image.new("RGB", (12, 8), "red")
    original.paste("blue", (0, 0, 6, 4))
    exif = Image.Exif()
    exif[274] = 6
    raw = BytesIO()
    original.save(raw, "JPEG", exif=exif)
    prepared = crop_asset(
        raw.getvalue(),
        x=1,
        y=2,
        width=4,
        height=6,
        shape="rectangle",
        expected_size=(8, 12),
        max_megapixels=16,
    )
    with (
        Image.open(BytesIO(raw.getvalue())) as source,
        Image.open(BytesIO(prepared.data)) as result,
    ):
        assert (
            result.tobytes()
            == ImageOps.exif_transpose(source).crop((1, 2, 5, 8)).convert("RGBA").tobytes()
        )


@pytest.mark.parametrize(
    "options",
    [
        {"x": -1},
        {"width": 101},
        {"shape": "circle", "height": 40},
        {"expected_size": (99, 80)},
        {"max_megapixels": 0},
    ],
)
def test_invalid_crop_is_rejected(options):
    values = {
        "x": 0,
        "y": 0,
        "width": 80,
        "height": 80,
        "shape": "rectangle",
        "expected_size": (100, 80),
        "max_megapixels": 16,
    }
    with pytest.raises(AssetInputError):
        crop_asset(pixels(Image.new("RGBA", (100, 80), "red")), **(values | options))


def test_empty_crop_is_rejected():
    with pytest.raises(AssetInputError, match="完全透明"):
        crop_asset(
            pixels(Image.new("RGBA", (80, 80))),
            x=0,
            y=0,
            width=80,
            height=80,
            shape="circle",
            expected_size=(80, 80),
            max_megapixels=16,
        )


@pytest.mark.asyncio
async def test_crop_keeps_lineage_restore_geometry_ownership_and_balance(asset_context):
    owner = await seed_user(asset_context, email="crop-owner@example.test")
    stranger = await seed_user(asset_context, email="crop-stranger@example.test")
    source = prepare_asset(
        pixels(Image.new("RGBA", (100, 80), "blue")), kind="original", max_megapixels=16
    )
    result = prepare_asset(
        pixels(Image.new("RGBA", (100, 80), (240, 20, 30, 128))), kind="result", max_megapixels=16
    )
    service = AssetService()
    base = await service.store(
        asset_context.database,
        asset_context.storage,
        owner_id=owner.id,
        prepared=result,
        edit_source=source,
        kind="result",
        operation_code="ai.extract_print",
        retention_days=30,
    )
    params = {"x": 10, "y": 0, "width": 80, "height": 80, "shape": "circle"}
    async with client_for(asset_context, "crop-owner") as client:
        await login(client, owner.email)
        for invalid in ({"x": 0.5}, {"width": 999}, {"height": 79}, {"x": True}):
            response = await client.post(f"/api/v1/assets/{base.id}/crop", json=params | invalid)
            assert response.status_code == 422
        response = await client.post(f"/api/v1/assets/{base.id}/crop", json=params)
        assert response.status_code == 201, response.text
        saved = response.json()["asset"]
        assert saved["parent_asset_id"] == str(base.id)
        assert saved["root_asset_id"] == str(base.root_asset_id)
        assert saved["operation_code"] == "image.crop" and saved["source_job_id"] is None
        context = (await client.get(f"/api/v1/assets/{saved['id']}/selection")).json()
        assert (context["width"], context["height"]) == (80, 80)
        assert context["has_initial_selection"] and not context["restore_limited"]
        for url, expected in (
            (context["source_url"], (0, 0, 255, 255)),
            (context["result_url"], (240, 20, 30, 128)),
        ):
            with Image.open(BytesIO((await client.get(url)).content)) as image:
                assert image.size == (80, 80)
                assert image.getpixel((40, 40)) == expected
                assert image.getpixel((0, 0))[3] == 0
        assert asset_context.storage.objects[base.object_key] == result.data
        await client.delete(f"/api/v1/assets/{base.id}")
        assert (await client.get(context["source_url"])).status_code == 200
        assert (await client.post(f"/api/v1/assets/{base.id}/crop", json=params)).status_code == 404
    async with client_for(asset_context, "crop-stranger") as client:
        await login(client, stranger.email)
        assert (
            await client.post(f"/api/v1/assets/{saved['id']}/crop", json=params)
        ).status_code == 404
    async with asset_context.database.session_factory() as session:
        assert (
            await session.scalar(
                select(PointAccount.balance).where(PointAccount.user_id == owner.id)
            )
            == 200
        )
        assert not (await service.scan_orphans(session, asset_context.storage)).orphan_objects
        row = await session.get(Asset, UUID(saved["id"]))
        assert row.asset_metadata["edit_source_ready"]
