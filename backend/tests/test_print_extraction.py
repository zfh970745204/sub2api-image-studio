import base64
import uuid
from io import BytesIO
from types import SimpleNamespace

import httpx
import numpy as np
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


def test_small_magenta_ink_inside_white_is_not_mistaken_for_edge_spill():
    image = Image.new("RGBA", (128, 128), (255, 0, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, 30, 98, 96), fill=(255, 255, 255, 255))
    for x, y in ((42, 42), (56, 58), (78, 74), (90, 88)):
        draw.point((x, y), fill=(255, 85, 255, 255))
    raw = BytesIO()
    image.save(raw, format="PNG")

    result, metadata = finalize_print_extraction(raw.getvalue())
    with Image.open(BytesIO(result)) as output:
        for point in ((42, 42), (56, 58), (78, 74), (90, 88)):
            assert output.getpixel(point) == (255, 85, 255, 255)
        assert output.getpixel((1, 1))[3] == 0
    assert metadata["color_preservation"] == "interior-ink-unchanged-v3"


def _blank_green():
    output = BytesIO()
    Image.new("RGB", (128, 128), (0, 255, 0)).save(output, format="PNG")
    return output.getvalue()


def native_print_image():
    with Image.open(BytesIO(print_image((0, 0, 0, 0)))) as image:
        draw = ImageDraw.Draw(image)
        # Pale green next to white is real ink here, not white mixed with a key.
        draw.line((29, 32, 29, 94), fill=(128, 255, 128, 128))
        draw.line((99, 32, 99, 94), fill=(255, 0, 255, 110))
        draw.line((40, 15, 60, 25), fill=(245, 245, 245, 32))
        draw.line((72, 15, 90, 25), fill=(0, 0, 0, 64))
        draw.rectangle((40, 76, 50, 85), fill=(0, 255, 0, 255))
        draw.rectangle((76, 76, 86, 85), fill=(255, 0, 255, 255))
        output = BytesIO()
        image.save(output, "PNG")
        return output.getvalue()


@pytest.mark.parametrize("key_color", [None, "#00FF00", "#FF00FF"])
def test_direct_extraction_keeps_every_native_ink_and_soft_alpha_pixel(key_color):
    raw = native_print_image()
    result, metadata = finalize_print_extraction(
        raw, key_color=key_color, require_native_alpha=True
    )
    with Image.open(BytesIO(raw)) as original, Image.open(BytesIO(result)) as output:
        expected, actual = np.array(original), np.array(output)
        np.testing.assert_array_equal(actual, expected)
        for background in ("white", "black", "#666666"):
            canvas = Image.new("RGBA", original.size, background)
            np.testing.assert_array_equal(
                np.array(Image.alpha_composite(canvas, output)),
                np.array(Image.alpha_composite(canvas, original)),
            )
    assert metadata["method"] == "native-alpha"
    assert metadata["color_preservation"] == "native-rgba-unchanged"


@pytest.mark.parametrize("background", ["white", "black", "#00FF00", "#FF00FF", "#DDDDDD"])
def test_direct_extraction_rejects_opaque_results_instead_of_keying_them(background):
    with pytest.raises(ImageInputError, match="透明底印花"):
        finalize_print_extraction(print_image(background), require_native_alpha=True)


@pytest.mark.parametrize("hole", [(0, 0), (10, 10)])
def test_one_transparent_pixel_does_not_validate_an_opaque_product_photo(hole):
    with Image.open(BytesIO(print_image("white"))) as image:
        image.putpixel(hole, (0, 0, 0, 0))
        raw = BytesIO()
        image.save(raw, "PNG")
    with pytest.raises(ImageInputError, match="透明底印花"):
        finalize_print_extraction(raw.getvalue(), require_native_alpha=True)


def test_direct_extraction_rejects_painted_checkerboard_and_empty_alpha():
    checkerboard = Image.new("RGBA", (128, 128), "white")
    draw = ImageDraw.Draw(checkerboard)
    for y in range(0, 128, 8):
        for x in range(0, 128, 8):
            if (x // 8 + y // 8) % 2:
                draw.rectangle((x, y, x + 7, y + 7), fill="#DDDDDD")
    draw.rectangle((30, 30, 96, 96), fill="black")
    for image in (checkerboard, Image.new("RGBA", (128, 128))):
        raw = BytesIO()
        image.save(raw, "PNG")
        with pytest.raises(ImageInputError):
            finalize_print_extraction(raw.getvalue(), require_native_alpha=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,result_kind,valid,mode",
    [
        ("ai.extract_print", "native", True, "transparent"),
        ("ai.extract_print", "native", True, "opaque"),
        ("ai.extract_print", "green", False, "transparent"),
        ("ai.extract_print", "magenta", False, "transparent"),
        ("ai.extract_print", "opaque", True, "transparent"),
        ("ai.extract_print", "opaque", True, "opaque"),
        ("ai.extract_print", "empty", False, "transparent"),
        ("ai.redraw", "opaque", True, "opaque"),
    ],
)
async def test_image_edit_pipeline_charges_publishes_alpha_or_refunds(
    asset_context, operation, result_kind, valid, mode
):
    owner = await seed_user(asset_context, email="print-owner@example.test")
    async with client_for(asset_context, "print-owner") as client:
        await login(client, owner.email)
        source = (await upload(client, raster_bytes())).json()["asset"]
        parameters = {"quality": "high", "instruction": "保留原文字"}
        if operation == "ai.extract_print":
            parameters.update(output_mode=mode, background_color="#FFFFFF")
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
    if result_kind == "native":
        raw = native_print_image()
    elif result_kind == "empty":
        buffer = BytesIO()
        Image.new("RGBA", (128, 128)).save(buffer, "PNG")
        raw = buffer.getvalue()
    else:
        raw = print_image(
            {"green": "#00FF00", "magenta": "#FF00FF", "opaque": "white"}[result_kind]
        )

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
        assert "保持原有的色彩和内容不变".encode() in calls[0]
        assert "均匀纯色 #FFFFFF 背景".encode() in calls[0]
        assert b"#00FF00" not in calls[0] and b"#FF00FF" not in calls[0]
        assert b"Use ONLY a perfectly uniform" not in calls[0]
    else:
        assert b"Do not extract artwork" in calls[0]
        assert b"#00FF00" not in calls[0]
    assert "User instruction: 保留原文字".encode() in calls[0]
    assert b'name="quality"\r\n\r\nhigh' in calls[0]
    assert b'name="output_format"\r\n\r\npng' in calls[0]
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
            with (
                Image.open(BytesIO(raw)) as original,
                Image.open(
                    BytesIO(await asset_context.storage.get_object(output.object_key))
                ) as image,
            ):
                if operation == "ai.redraw" or (result_kind == "native" and mode == "transparent"):
                    np.testing.assert_array_equal(np.array(image), np.array(original))
                elif mode == "opaque":
                    expected = Image.alpha_composite(
                        Image.new("RGBA", original.size, "white"), original
                    ).convert("RGB")
                    np.testing.assert_array_equal(np.array(image), np.array(expected))
                    assert image.mode == "RGB" and output.has_alpha is False
                if operation == "ai.extract_print" and mode == "transparent":
                    assert image.getpixel((1, 1))[3] == 0
        else:
            assert job.status == "failed" and job.refund_status == "refunded"
            assert job.output_asset_id is None and balance == 200
    # Duplicate queue delivery never re-generates, charges or refunds twice.
    await execute_image_job({"runtime": runtime, "image_job_executor": executor}, str(job_id))
    assert len(calls) == 1
