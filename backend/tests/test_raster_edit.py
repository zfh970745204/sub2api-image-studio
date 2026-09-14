from io import BytesIO

import numpy as np
import pytest
from PIL import Image
from sqlalchemy import func, select
from test_assets import asset_context as asset_fixture
from test_assets import client_for, login, raster_bytes, seed_user, upload

from app.repositories.models import Asset, ImageJob, PointAccount
from app.services.asset_files import prepare_asset
from app.services.assets import AssetService

asset_context = asset_fixture


def png(color, size=(12, 8)):
    output = BytesIO()
    Image.new("RGBA", size, color).save(output, "PNG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_raster_edit_keeps_new_paint_and_restorable_erasure_without_charging(
    asset_context,
):
    owner = await seed_user(asset_context, email="raster-owner@example.test")
    service = AssetService()
    current_pixels = Image.new("RGBA", (12, 8), (255, 0, 0, 255))
    current_pixels.putpixel((0, 0), (0, 0, 0, 0))
    current_buffer = BytesIO()
    current_pixels.save(current_buffer, "PNG")
    current = prepare_asset(current_buffer.getvalue(), kind="result", max_megapixels=16)
    older = prepare_asset(png((0, 0, 255, 255)), kind="original", max_megapixels=16)
    source = await service.store(
        asset_context.database,
        asset_context.storage,
        owner_id=owner.id,
        prepared=current,
        edit_source=older,
        kind="result",
        operation_code="ai.extract_print",
        retention_days=30,
    )
    edited_pixels = Image.new("RGBA", (12, 8), (20, 180, 60, 128))
    edited_pixels.putpixel((0, 0), (40, 70, 220, 255))
    edited_pixels.putpixel((1, 0), (0, 0, 0, 0))
    edited_buffer = BytesIO()
    edited_pixels.save(edited_buffer, "PNG")
    edited = edited_buffer.getvalue()
    async with client_for(asset_context, "raster-owner") as client:
        await login(client, owner.email)
        response = await client.post(
            f"/api/v1/assets/{source.id}/edit", files={"image": ("edited.png", edited, "image/png")}
        )
        assert response.status_code == 201, response.text
        result = response.json()["asset"]
        assert result["operation_code"] == "image.edit"
        assert result["parent_asset_id"] == str(source.id)
        assert result["root_asset_id"] == str(source.root_asset_id)
        assert result["source_job_id"] is None
        context = (await client.get(f"/api/v1/assets/{result['id']}/selection")).json()
        assert context["has_initial_selection"] is True
        assert context["restore_limited"] is False
        restored_raw = (await client.get(context["source_url"])).content
        result_raw = (await client.get(context["result_url"])).content
        with (
            Image.open(BytesIO(restored_raw)) as restore,
            Image.open(BytesIO(result_raw)) as result,
        ):
            np.testing.assert_array_equal(np.array(result), np.array(edited_pixels))
            assert restore.getpixel((0, 0)) == (40, 70, 220, 255)
            assert restore.getpixel((1, 0)) == (255, 0, 0, 255)
            assert restore.getpixel((5, 5)) == (20, 180, 60, 255)
            assert np.all(np.array(restore)[:, :, 3] >= np.array(result)[:, :, 3])
        assert await asset_context.storage.get_object(source.object_key) == current.data
    async with asset_context.database.session_factory() as session:
        assert (
            await session.scalar(
                select(PointAccount.balance).where(PointAccount.user_id == owner.id)
            )
            == 200
        )
        assert await session.scalar(select(func.count()).select_from(ImageJob)) == 0
        assert not (await service.scan_orphans(session, asset_context.storage)).orphan_objects


@pytest.mark.asyncio
async def test_edit_is_owner_scoped_and_rejects_invalid_pixels_without_creating_assets(
    asset_context,
):
    owner = await seed_user(asset_context, email="edit-owner@example.test")
    other = await seed_user(asset_context, email="edit-other@example.test")
    async with client_for(asset_context, "edit-owner") as client:
        await login(client, owner.email)
        source = (await upload(client, raster_bytes())).json()["asset"]
        url = f"/api/v1/assets/{source['id']}/edit"
        for raw in [b"broken", raster_bytes(), png((0, 0, 0, 0)), png("red", (13, 8))]:
            response = await client.post(url, files={"image": ("edit.png", raw, "image/png")})
            assert response.status_code == 422, response.text
        asset_context.settings.max_upload_mb = 0
        response = await client.post(url, files={"image": ("edit.png", png("red"), "image/png")})
        assert response.status_code == 413
    async with client_for(asset_context, "edit-other") as client:
        assert (
            await client.post(url, files={"image": ("edit.png", png("red"), "image/png")})
        ).status_code == 401
        await login(client, other.email)
        assert (
            await client.post(url, files={"image": ("edit.png", png("red"), "image/png")})
        ).status_code == 404
    async with asset_context.database.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Asset)) == 1
