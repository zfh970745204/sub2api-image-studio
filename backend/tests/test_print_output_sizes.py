import base64
from io import BytesIO
from types import SimpleNamespace
from uuid import UUID

import httpx
import numpy as np
import pytest
from PIL import Image, ImageDraw
from test_assets import asset_context as asset_fixture
from test_assets import client_for, login, seed_user, upload
from test_product_print import garment, png

from app.print_extraction import finish_print, size_print_outputs
from app.repositories.models import Asset, ImageJob
from app.services.assets import AssetService
from app.services.image_executor import ImageJobExecutor
from app.sub2api import Sub2APIClient
from app.workers.worker import execute_image_job

asset_context = asset_fixture

SIZE_CASES = [
    ("2048x2048", "1024x1024", (256, 128)),
    ("2048x3072", "1024x1536", (128, 128)),
    ("3072x2048", "1536x1024", (128, 128)),
    ("3072x3072", "1024x1024", (128, 256)),
]
PRODUCT_COLOR = "#245EAA"


def artwork(size, *, native_alpha=True):
    image = Image.new("RGBA", size, (0, 0, 0, 0) if native_alpha else PRODUCT_COLOR)
    draw = ImageDraw.Draw(image)
    # Off-center ink and distant registration marks expose cropping and stretching.
    draw.rectangle((16, 32, 111, 63), fill=(230, 30, 40, 255))
    draw.rectangle((16, 80, 47, 95), fill=(40, 210, 80, 110 if native_alpha else 255))
    draw.rectangle((size[0] - 12, size[1] - 12, size[0] - 5, size[1] - 5), fill="white")
    return png(image)


def assert_sized_layers(output, source, *, requested, original_size, mode, native_alpha):
    target = tuple(map(int, requested.split("x")))
    scale = min(target[0] / original_size[0], target[1] / original_size[1])
    fitted = tuple(round(value * scale) for value in original_size)
    offset = ((target[0] - fitted[0]) // 2, (target[1] - fitted[1]) // 2)

    def point(x, y):
        return (round(offset[0] + x * scale), round(offset[1] + y * scale))

    with Image.open(BytesIO(output)) as result, Image.open(BytesIO(source)) as restore:
        assert result.format == restore.format == "PNG"
        assert result.size == restore.size == target
        assert result.mode == ("RGBA" if mode == "transparent" else "RGB")
        assert restore.mode == "RGBA"
        assert restore.getpixel((0, 0)) == (36, 94, 170, 255)
        expected_corner = (0, 0, 0, 0) if mode == "transparent" else (36, 94, 170)
        assert result.getpixel((0, 0)) == expected_corner
        assert restore.getpixel(point(25, 85))[3] == (110 if native_alpha else 255)
        if mode == "transparent":
            assert result.getpixel(point(25, 85))[3] == (110 if native_alpha else 255)
            assert result.getpixel(point(5, 5))[3] == 0
        else:
            assert result.getpixel(point(5, 5)) == (36, 94, 170)
            expected = (38, 144, 131) if native_alpha else (40, 210, 80)
            np.testing.assert_allclose(result.getpixel(point(25, 85)), expected, atol=1)

        expected_bounds = (
            offset[0] + round(16 * scale),
            offset[1] + round(32 * scale),
            offset[0] + round(112 * scale),
            offset[1] + round(64 * scale),
        )
        bounds = []
        for layer in (result, restore):
            pixels = np.asarray(layer)
            # Threshold near half coverage, matching the alpha threshold on transparent ink.
            red = (pixels[:, :, 0] > 130) & (pixels[:, :, 1] < 70) & (pixels[:, :, 2] < 115)
            if layer.mode == "RGBA":
                red &= pixels[:, :, 3] > 128
            bbox = Image.fromarray(red).getbbox()
            assert bbox is not None
            np.testing.assert_allclose(bbox, expected_bounds, atol=2)
            assert (bbox[2] - bbox[0]) / (bbox[3] - bbox[1]) == pytest.approx(3, abs=0.02)
            bounds.append(bbox)
            assert layer.getpixel(point(original_size[0] - 8, original_size[1] - 8))[:3] == (
                255,
                255,
                255,
            )
        np.testing.assert_allclose(bounds[0], bounds[1], atol=2)


@pytest.mark.parametrize("requested,upstream_size,original_size", SIZE_CASES)
@pytest.mark.parametrize("mode", ["transparent", "opaque"])
def test_output_canvas_preserves_ink_aspect_soft_alpha_and_restore_alignment(
    requested, upstream_size, original_size, mode
):
    raw = artwork(original_size)
    finished, _ = finish_print(raw, mode=mode, color=PRODUCT_COLOR)
    output, source = size_print_outputs(
        finished, raw, size=requested, mode=mode, color=PRODUCT_COLOR
    )
    assert_sized_layers(
        output,
        source,
        requested=requested,
        original_size=original_size,
        mode=mode,
        native_alpha=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,upstream_size,original_size", SIZE_CASES)
@pytest.mark.parametrize("mode", ["transparent", "opaque"])
async def test_executor_requests_upstream_size_and_stores_aligned_print_sidecar(
    asset_context, requested, upstream_size, original_size, mode
):
    owner = await seed_user(asset_context, email="sized-print-worker@example.test")
    # Deliberately return a different aspect ratio: the stored canvas must still match the quote.
    raw = artwork(original_size, native_alpha=False)
    calls = []

    def upstream(request):
        assert request.url.path == "/v1/images/edits"
        calls.append(request.content)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(raw).decode()}]})

    parameters = {
        "output_size": requested,
        "output_mode": mode,
        "quality": "high",
        # output_size must take precedence over the older upstream size field.
        "size": "auto",
    }
    async with client_for(asset_context, "sized-print-worker") as client:
        await login(client, owner.email)
        source = (await upload(client, garment(PRODUCT_COLOR))).json()["asset"]
        quoted = await client.post(
            "/api/v1/jobs/quote",
            json={
                "operation_code": "ai.extract_print",
                "source_asset_id": source["id"],
                "parameters": parameters,
            },
        )
        assert quoted.status_code == 201, quoted.text
        created = await client.post(
            "/api/v1/jobs",
            headers={"Idempotency-Key": "sized-print-worker"},
            json={"quote_id": quoted.json()["quote"]["id"], "parameters": parameters},
        )
        assert created.status_code == 201, created.text
        job_id = UUID(created.json()["job"]["id"])

        settings = asset_context.settings.model_copy(
            update={
                "sub2api_api_key": "test-print-output-key",
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
            instance_name="print-output-sizes-test",
        )
        result = await execute_image_job(
            {"runtime": runtime, "image_job_executor": executor}, str(job_id)
        )
        assert result["status"] == "succeeded", result
        assert len(calls) == 1
        assert f'name="size"\r\n\r\n{upstream_size}\r\n'.encode() in calls[0]
        assert b'name="output_format"\r\n\r\npng\r\n' in calls[0]
        assert "均匀纯色 #245EAA 背景".encode() in calls[0]

        async with asset_context.database.session_factory() as session:
            job = await session.get(ImageJob, job_id)
            output = await session.get(Asset, job.output_asset_id)
            assert output.status == "ready"
            assert output.parent_asset_id == UUID(source["id"])
            assert output.source_job_id == job_id
            assert (output.width, output.height) == tuple(map(int, requested.split("x")))
            assert output.has_alpha is (mode == "transparent")
            assert output.asset_metadata["output_size"] == requested
            assert output.asset_metadata["background_color"] == PRODUCT_COLOR
            assert output.asset_metadata["edit_source_ready"] is True
            assert "_edit_source_data" not in output.asset_metadata
        output_data = await asset_context.storage.get_object(output.object_key)
        sidecar_data = await asset_context.storage.get_object(
            AssetService.edit_source_key(output.object_key)
        )
        assert_sized_layers(
            output_data,
            sidecar_data,
            requested=requested,
            original_size=original_size,
            mode=mode,
            native_alpha=False,
        )
        response = await client.get(f"/api/v1/assets/{output.id}/selection")
        assert response.status_code == 200, response.text
        context = response.json()
        assert (context["width"], context["height"]) == (output.width, output.height)
        assert context["restore_limited"] is False
        assert context["source_url"] != context["result_url"]
        assert (await client.get(context["source_url"])).content == sidecar_data
        assert (await client.get(context["result_url"])).content == output_data
