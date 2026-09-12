import hashlib
import random
import uuid
import zipfile
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import func, select
from test_assets import asset_context as asset_fixture
from test_assets import client_for, login, seed_user, upload

from app.domain.toolbox import ToolboxOptions
from app.repositories.models import (
    Asset,
    ImageJob,
    MembershipPlan,
    OperationCatalog,
    OperationPrice,
    PointAccount,
)
from app.services.asset_files import AssetInputError, inspect_stored_asset
from app.services.image_executor import ImageJobExecutor
from app.services.image_toolbox import process_image
from app.workers.worker import execute_image_job

asset_context = asset_fixture


def png(image):
    stream = BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


@pytest.mark.parametrize("extension", ["png", "jpg", "webp"])
def test_stored_image_inspection_keeps_encoded_bytes(extension):
    prepared, _ = process_image(
        png(Image.new("RGBA", (23, 17), "#bd427c")),
        ToolboxOptions(format=extension, quality=31),
    )
    inspected = inspect_stored_asset(prepared.data, extension=extension)
    assert inspected == prepared


def test_geometry_pipeline_and_transparency_are_exact():
    source = Image.new("RGBA", (8, 6))
    source.paste((255, 0, 0, 128), (2, 1, 6, 5))
    source.putpixel((2, 1), (0, 255, 0, 255))
    result, _ = process_image(
        png(source), ToolboxOptions(trim=True, rotation=90, flip_horizontal=True, padding=2)
    )
    with Image.open(BytesIO(result.data)) as image:
        assert image.size == (8, 8)
        assert image.getpixel((0, 0))[3] == 0
        assert image.getpixel((2, 2)) == (0, 255, 0, 255)
        assert image.getpixel((4, 4)) == (255, 0, 0, 128)


@pytest.mark.parametrize(
    "mode,expected",
    [("fit", (50, 25)), ("fill", (50, 50)), ("stretch", (50, 50)), ("percent", (40, 20))],
)
def test_resize_modes(mode, expected):
    result, _ = process_image(
        png(Image.new("RGB", (100, 50), "red")),
        ToolboxOptions(resize=mode, width=50, height=50, percent=40),
    )
    assert (result.width, result.height) == expected


def test_jpg_flattens_alpha_and_webp_keeps_it():
    source = png(Image.new("RGBA", (40, 40), (255, 0, 0, 128)))
    jpg, _ = process_image(source, ToolboxOptions(format="jpg", color="#0000ff", quality=100))
    with Image.open(BytesIO(jpg.data)) as image:
        r, g, b = image.getpixel((20, 20))
        assert abs(r - 128) < 3 and g < 3 and abs(b - 127) < 3
    webp, _ = process_image(source, ToolboxOptions(format="webp"))
    with Image.open(BytesIO(webp.data)) as image:
        assert image.getpixel((20, 20))[3] == 128
    assert jpg.extension == "jpg" and jpg.mime_type == "image/jpeg" and not jpg.has_alpha
    assert webp.extension == "webp" and webp.has_alpha


def test_adjustments_change_rgb_without_touching_alpha():
    source = Image.new("RGBA", (3, 3), (40, 80, 120, 77))
    source.putpixel((1, 1), (200, 100, 20, 200))
    result, _ = process_image(
        png(source),
        ToolboxOptions(brightness=50, saturation=-100, grayscale=True, invert=True),
    )
    with Image.open(BytesIO(result.data)) as image:
        assert image.getpixel((0, 0))[3] == 77
        assert image.getpixel((1, 1))[3] == 200
        assert image.getpixel((0, 0))[:3] == (146, 146, 146)
        assert image.getpixel((1, 1))[:3] == (87, 87, 87)


def test_blur_and_sharpen_are_available_for_raster_detail():
    source = Image.new("RGBA", (9, 9), "black")
    source.putpixel((4, 4), (255, 255, 255, 255))
    blurred, _ = process_image(png(source), ToolboxOptions(blur=4))
    sharpened, _ = process_image(png(source), ToolboxOptions(sharpen=5))
    with Image.open(BytesIO(blurred.data)) as image:
        assert image.getpixel((3, 4))[0] > 0
    with Image.open(BytesIO(sharpened.data)) as image:
        assert image.getpixel((4, 4))[0] == 255
        assert image.getpixel((3, 4))[0] == 0


def test_text_and_image_watermarks_honor_position_scale_and_opacity():
    source = png(Image.new("RGBA", (100, 100), (0, 0, 0, 0)))
    text, _ = process_image(
        source,
        ToolboxOptions(
            watermark="text",
            watermark_text="MARK",
            watermark_color="#ff0000",
            watermark_position="top-left",
            watermark_opacity=100,
            watermark_scale=25,
        ),
    )
    with Image.open(BytesIO(text.data)) as image:
        assert any(image.getpixel((x, y))[0] > 200 for y in range(35) for x in range(55))
        assert not any(image.getpixel((x, y))[0] > 200 for y in range(65, 100) for x in range(55, 100))

    watermark = png(Image.new("RGBA", (80, 40), (0, 255, 0, 255)))
    image_result, _ = process_image(
        source,
        ToolboxOptions(
            watermark="image",
            watermark_asset_id=uuid.uuid4(),
            watermark_position="center",
            watermark_opacity=50,
            watermark_scale=25,
        ),
        watermark=watermark,
    )
    with Image.open(BytesIO(image_result.data)) as image:
        assert image.getpixel((50, 50)) == (0, 255, 0, 128)
        bbox = image.getchannel("A").getbbox()
        assert bbox is not None and bbox[2] - bbox[0] <= 25 and bbox[3] - bbox[1] <= 13
        assert 35 <= bbox[0] <= 40 and 43 <= bbox[1] <= 46


def test_image_background_and_padding_are_exported():
    source = Image.new("RGBA", (40, 20))
    source.paste("red", (10, 5, 30, 15))
    result, _ = process_image(
        png(source),
        ToolboxOptions(background="image", background_asset_id=uuid.uuid4(), padding=5),
        png(Image.new("RGB", (30, 30), "blue")),
    )
    with Image.open(BytesIO(result.data)) as image:
        assert image.size == (50, 30)
        assert image.getpixel((0, 0)) == (0, 0, 255, 255)
        assert image.getpixel((25, 15)) == (255, 0, 0, 255)


def test_target_size_is_honest_without_silent_dimension_changes():
    source = png(Image.frombytes("RGB", (300, 300), random.Random(3).randbytes(270000)))
    result, metadata = process_image(
        source, ToolboxOptions(format="jpg", compression="target", target_kb=25, quality=95)
    )
    assert len(result.data) <= 25 * 1024
    assert (result.width, result.height) == (300, 300)
    assert metadata["encoding_quality"] < 95
    with pytest.raises(AssetInputError, match="无法压到"):
        process_image(source, ToolboxOptions(format="jpg", compression="target", target_kb=1))
    with pytest.raises(ValidationError):
        ToolboxOptions(format="png", compression="target")


@pytest.mark.parametrize(
    "options",
    [
        {"width": True},
        {"rotation": 45},
        {"width": 0},
        {"format": "gif"},
        {"background": "image"},
        {"color": "red"},
    ],
)
def test_invalid_parameters(options):
    with pytest.raises(ValidationError):
        ToolboxOptions(**options)


def test_output_memory_limit_is_checked_before_resize():
    with pytest.raises(ValueError, match="超过限制"):
        process_image(
            png(Image.new("RGB", (10, 10))),
            ToolboxOptions(resize="stretch", width=12000, height=12000),
        )
    with pytest.raises(ValueError):
        process_image(
            png(Image.new("RGB", (10, 10))),
            ToolboxOptions(resize="stretch", width=2000, height=2000),
            max_megapixels=1,
        )


@pytest.mark.asyncio
async def test_batch_queue_idempotency_exact_format_lineage_and_private_zip(asset_context):
    owner = await seed_user(asset_context, email="toolbox-owner@example.test")
    stranger = await seed_user(asset_context, email="toolbox-stranger@example.test")
    async with client_for(asset_context, "toolbox-owner") as client:
        await login(client, owner.email)
        sources = [
            (
                await upload(
                    client, png(Image.new("RGBA", (120, 80), color)), filename=f"art-{i}.png"
                )
            ).json()["asset"]
            for i, color in enumerate(["red", "blue"])
        ]
        request = {
            "asset_ids": [source["id"] for source in sources],
            "options": {"format": "webp", "quality": 42, "rotation": 90},
            "batch_name": "批次 A",
            "filename_prefix": "../new",
        }
        response = await client.post("/api/v1/toolbox/quote", json=request)
        assert response.status_code == 201, response.text
        quote = response.json()
        assert quote["total_points"] == 0
        payload = {
            "items": [
                {"quote_id": item["quote"]["id"], "parameters": item["parameters"]}
                for item in quote["items"]
            ]
        }
        response = await client.post("/api/v1/toolbox/submit", json=payload)
        assert response.status_code == 201, response.text
        ids = [job["id"] for job in response.json()["items"]]
        replay = await client.post("/api/v1/toolbox/submit", json=payload)
        assert [job["id"] for job in replay.json()["items"]] == ids
        runtime = SimpleNamespace(
            database=asset_context.database,
            object_storage=asset_context.storage,
            instance_name="toolbox-worker-test",
        )
        executor = ImageJobExecutor(
            asset_context.settings, asset_context.database, asset_context.storage
        )
        for job_id in ids:
            finished = await execute_image_job(
                {"runtime": runtime, "image_job_executor": executor}, job_id
            )
            assert finished["status"] == "succeeded", finished
        response = await client.get(
            f"/api/v1/jobs?operation_code=image.toolbox&batch_id={quote['batch_id']}"
        )
        jobs = response.json()["items"]
        assert len(jobs) == 2 and all(job["status"] == "succeeded" for job in jobs)
        outputs = [job["output_asset_id"] for job in jobs]
        for job in jobs:
            asset = (await client.get(f"/api/v1/assets/{job['output_asset_id']}")).json()["asset"]
            assert asset["extension"] == "webp" and asset["mime_type"] == "image/webp"
            assert asset["parent_asset_id"] == job["source_asset_id"]
            assert (asset["width"], asset["height"]) == (80, 120)
            async with asset_context.database.session_factory() as session:
                row = await session.get(Asset, uuid.UUID(asset["id"]))
                raw = asset_context.storage.objects[row.object_key]
                assert len(raw) == asset["size_bytes"]
                assert hashlib.sha256(raw).hexdigest() == asset["sha256"]
                assert Image.open(BytesIO(raw)).format == "WEBP"
        bundle = await client.post("/api/v1/assets/download-bundle", json={"asset_ids": outputs})
        assert bundle.status_code == 200
        with zipfile.ZipFile(BytesIO(bundle.content)) as archive:
            assert len(archive.namelist()) == 2
            assert all("/" not in name and name.endswith(".webp") for name in archive.namelist())
    async with client_for(asset_context, "toolbox-stranger") as client:
        await login(client, stranger.email)
        assert (await client.post("/api/v1/toolbox/quote", json=request)).status_code == 404
        assert (await client.post("/api/v1/toolbox/submit", json=payload)).status_code == 404
        assert (
            await client.post("/api/v1/assets/download-bundle", json={"asset_ids": outputs})
        ).status_code == 404
        assert (await client.get(f"/api/v1/jobs?batch_id={quote['batch_id']}")).json()[
            "items"
        ] == []
    async with asset_context.database.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ImageJob)) == 2
        assert (
            await session.scalar(
                select(PointAccount.balance).where(PointAccount.user_id == owner.id)
            )
            == 200
        )


@pytest.mark.asyncio
async def test_batch_submission_rolls_back_all_items_if_later_source_disappears(asset_context):
    owner = await seed_user(asset_context, email="atomic-toolbox@example.test")
    async with client_for(asset_context, "atomic-toolbox") as client:
        await login(client, owner.email)
        sources = [
            (await upload(client, png(Image.new("RGB", (10, 10), color)))).json()["asset"]
            for color in ["red", "blue"]
        ]
        quote = (
            await client.post(
                "/api/v1/toolbox/quote",
                json={"asset_ids": [item["id"] for item in sources], "options": {}},
            )
        ).json()
        await client.delete(f"/api/v1/assets/{sources[1]['id']}")
        response = await client.post(
            "/api/v1/toolbox/submit",
            json={
                "items": [
                    {"quote_id": item["quote"]["id"], "parameters": item["parameters"]}
                    for item in quote["items"]
                ]
            },
        )
        assert response.status_code == 404
    async with asset_context.database.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ImageJob)) == 0


@pytest.mark.asyncio
async def test_quote_rejects_foreign_background_or_watermark_duplicates_and_account_size_limit(asset_context):
    async with asset_context.database.session_factory() as session:
        plan = await session.scalar(select(MembershipPlan).where(MembershipPlan.code == "free"))
        plan.max_image_megapixels = 1
        await session.commit()
    owner = await seed_user(asset_context, email="limits-owner@example.test")
    stranger = await seed_user(asset_context, email="limits-stranger@example.test")
    async with client_for(asset_context, "limits-stranger") as client:
        await login(client, stranger.email)
        foreign = (await upload(client, png(Image.new("RGB", (20, 20), "blue")))).json()["asset"]
    async with client_for(asset_context, "limits-owner") as client:
        await login(client, owner.email)
        source = (await upload(client, png(Image.new("RGB", (20, 20), "red")))).json()["asset"]
        for options in [
            {"background": "image", "background_asset_id": foreign["id"]},
            {"watermark": "image", "watermark_asset_id": foreign["id"]},
        ]:
            response = await client.post(
                "/api/v1/toolbox/quote",
                json={"asset_ids": [source["id"]], "options": options},
            )
            assert response.status_code == 404
        response = await client.post(
            "/api/v1/toolbox/quote",
            json={
                "asset_ids": [source["id"], source["id"]],
                "options": {},
            },
        )
        assert response.status_code == 422
        for trim in [False, True]:
            response = await client.post(
                "/api/v1/toolbox/quote",
                json={
                    "asset_ids": [source["id"]],
                    "options": {"resize": "stretch", "width": 2000, "height": 2000, "trim": trim},
                },
            )
            assert response.status_code == 422, response.text
            assert "1 百万" in response.json()["message"]


@pytest.mark.asyncio
async def test_bundle_storage_failure_has_retryable_error(asset_context):
    owner = await seed_user(asset_context, email="bundle-failure@example.test")
    async with client_for(asset_context, "bundle-failure") as client:
        await login(client, owner.email)
        source = (await upload(client, png(Image.new("RGB", (10, 10), "red")))).json()["asset"]
        asset_context.storage.objects.clear()
        response = await client.post(
            "/api/v1/assets/download-bundle", json={"asset_ids": [source["id"]]}
        )
        assert response.status_code == 503
        assert response.json()["code"] == "OBJECT_STORAGE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_priced_batch_insufficient_balance_rolls_back_jobs_and_charges(asset_context):
    owner = await seed_user(asset_context, email="priced-batch@example.test")
    async with asset_context.database.session_factory() as session:
        price = await session.scalar(
            select(OperationPrice)
            .join(OperationCatalog)
            .where(OperationCatalog.code == "image.toolbox")
        )
        price.base_points = 150
        await session.commit()
    async with client_for(asset_context, "priced-batch") as client:
        await login(client, owner.email)
        sources = [
            (await upload(client, png(Image.new("RGB", (10, 10), color)))).json()["asset"]
            for color in ["red", "blue"]
        ]
        quote = (
            await client.post(
                "/api/v1/toolbox/quote",
                json={
                    "asset_ids": [source["id"] for source in sources],
                    "options": {},
                },
            )
        ).json()
        assert quote["total_points"] == 300
        response = await client.post(
            "/api/v1/toolbox/submit",
            json={
                "items": [
                    {"quote_id": item["quote"]["id"], "parameters": item["parameters"]}
                    for item in quote["items"]
                ]
            },
        )
        assert response.status_code == 409, response.text
    async with asset_context.database.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ImageJob)) == 0
        assert (
            await session.scalar(
                select(PointAccount.balance).where(PointAccount.user_id == owner.id)
            )
            == 200
        )
