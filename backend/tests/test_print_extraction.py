import base64
import uuid
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select
from test_assets import asset_context as asset_fixture
from test_assets import client_for, login, raster_bytes, seed_user, upload

from app.image_ops import ImageInputError, finalize_print_extraction
from app.repositories.models import Asset, ImageJob, PointAccount
from app.services.image_executor import ImageJobExecutor
from app.sub2api import Sub2APIClient
from app.workers.worker import execute_image_job

asset_context = asset_fixture


def print_image(background):
    image = Image.new("RGBA", (128, 128), background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, 30, 60, 96), fill=(255, 255, 255, 255))
    draw.rectangle((68, 30, 98, 96), fill=(0, 0, 0, 255))
    draw.rectangle((40, 50, 88, 70), fill=(210, 70, 35, 255))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize("background", [(0, 255, 0, 255), (255, 0, 255, 255), (0, 0, 0, 0)])
def test_transparent_print_preserves_white_black_and_colored_ink(background):
    result, metadata = finalize_print_extraction(print_image(background))
    with Image.open(BytesIO(result)) as image:
        assert image.format == "PNG"
        assert image.getpixel((1, 1))[3] == 0
        assert image.getpixel((45, 40)) == (255, 255, 255, 255)
        assert image.getpixel((80, 40)) == (0, 0, 0, 255)
        assert image.getpixel((50, 60)) == (210, 70, 35, 255)
    assert metadata["transparent_background"] is True


def test_extraction_rejects_opaque_background_and_empty_artwork():
    with pytest.raises(ImageInputError, match="无法安全"):
        finalize_print_extraction(print_image((220, 220, 220, 255)))
    with pytest.raises(ImageInputError, match="未提取到有效印花"):
        finalize_print_extraction(_blank_green())


def _blank_green():
    output = BytesIO()
    Image.new("RGB", (128, 128), (0, 255, 0)).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,valid",
    [("ai.extract_print", True), ("ai.extract_print", False), ("ai.redraw", True)],
)
async def test_image_edit_pipeline_charges_publishes_alpha_or_refunds(
    asset_context, operation, valid
):
    owner = await seed_user(asset_context, email="print-owner@example.test")
    async with client_for(asset_context, "print-owner") as client:
        await login(client, owner.email)
        source = (await upload(client, raster_bytes())).json()["asset"]
        parameters = {"quality": "high", "instruction": "保留原文字"}
        quote_response = await client.post(
            "/api/v1/jobs/quote",
            json={
                "operation_code": operation,
                "source_asset_id": source["id"],
                "parameters": parameters,
            },
        )
        assert quote_response.status_code == 201, quote_response.text
        created = await client.post(
            "/api/v1/jobs",
            headers={"Idempotency-Key": "extract-print-job"},
            json={"quote_id": quote_response.json()["quote"]["id"], "parameters": parameters},
        )
        assert created.status_code == 201, created.text
        job_id = uuid.UUID(created.json()["job"]["id"])

    calls = []
    raw = print_image((0, 255, 0, 255) if valid else (220, 220, 220, 255))

    def upstream(request):
        calls.append(request.content)
        assert request.url.path == "/v1/images/edits"
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(raw).decode()}]})

    settings = asset_context.settings.model_copy(
        update={
            "sub2api_api_key": "dummy-test-key",
            "sub2api_base_url": "https://upstream.example.test/v1",
        }
    )
    executor = ImageJobExecutor(
        settings,
        asset_context.database,
        asset_context.storage,
        sub2api=Sub2APIClient(settings, transport=httpx.MockTransport(upstream)),
    )
    runtime = SimpleNamespace(
        database=asset_context.database,
        object_storage=asset_context.storage,
        instance_name="print-test",
    )
    await execute_image_job({"runtime": runtime, "image_job_executor": executor}, str(job_id))
    assert len(calls) == 1
    if operation == "ai.extract_print":
        assert b"eye colors, ink hues, saturation, brightness" in calls[0]
        assert b"Keep white and black ink intact" in calls[0]
        assert b"Use ONLY a perfectly uniform" in calls[0]
    else:
        assert b"Do not extract artwork" in calls[0]
        assert b"#00FF00" not in calls[0]
    async with asset_context.database.session_factory() as session:
        job = await session.get(ImageJob, job_id)
        balance = await session.scalar(
            select(PointAccount.balance).where(PointAccount.user_id == owner.id)
        )
        if valid:
            assert job.status == "succeeded"
            output = await session.get(Asset, job.output_asset_id)
            assert output.status == "ready" and output.parent_asset_id == uuid.UUID(source["id"])
            assert balance == 200 - quote_response.json()["quote"]["final_points"]
            with Image.open(
                BytesIO(await asset_context.storage.get_object(output.object_key))
            ) as image:
                assert image.getpixel((1, 1))[3] == (0 if operation == "ai.extract_print" else 255)
        else:
            assert job.status == "failed" and job.refund_status == "refunded"
            assert job.output_asset_id is None and balance == 200
