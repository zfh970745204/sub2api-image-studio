from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import UUID

import pytest
from PIL import Image
from sqlalchemy import select
from test_assets import asset_context as asset_fixture
from test_assets import client_for, login, raster_bytes, seed_user, upload

from app.object_storage import ObjectStorageError
from app.repositories.models import Asset, PointAccount
from app.services.asset_files import prepare_asset
from app.services.assets import AssetService

asset_context = asset_fixture


@pytest.mark.asyncio
async def test_refinement_keeps_restore_pixels_and_lineage_without_charging(asset_context):
    owner = await seed_user(asset_context, email="selection-owner@example.test")
    stranger = await seed_user(asset_context, email="selection-stranger@example.test")
    source = prepare_asset(raster_bytes(), kind="original", max_megapixels=16)
    result = prepare_asset(raster_bytes(mode="RGBA"), kind="result", max_megapixels=16)
    service = AssetService()
    extracted = await service.store(
        asset_context.database,
        asset_context.storage,
        owner_id=owner.id,
        prepared=result,
        edit_source=source,
        kind="result",
        operation_code="ai.extract_print",
        retention_days=30,
    )
    async with client_for(asset_context, "selection-owner") as client:
        await login(client, owner.email)
        context = (await client.get(f"/api/v1/assets/{extracted.id}/selection")).json()
        assert context["restore_limited"] is False and context["has_initial_selection"] is True
        pixels = await client.get(context["source_url"])
        assert pixels.headers["cache-control"] == "private, no-store"
        assert pixels.content == source.data
        saved_response = await client.post(
            f"/api/v1/assets/{extracted.id}/selection",
            files={"image": ("refined.png", result.data, "image/png")},
        )
        assert saved_response.status_code == 201, saved_response.text
        saved = saved_response.json()["asset"]
        assert saved["parent_asset_id"] == str(extracted.id)
        assert saved["root_asset_id"] == str(extracted.root_asset_id)
        assert saved["operation_code"] == "cutout.refine" and saved["source_job_id"] is None
        next_context = (await client.get(f"/api/v1/assets/{saved['id']}/selection")).json()
        assert (await client.get(next_context["source_url"])).content == source.data
        assert (await client.get(next_context["result_url"])).content == result.data
        # A derived version owns a copy, so removal of its parent does not break restore.
        await client.delete(f"/api/v1/assets/{extracted.id}")
        assert (await client.get(context["source_url"])).status_code == 404
        assert (await client.get(next_context["source_url"])).content == source.data
    async with client_for(asset_context, "selection-stranger") as client:
        await login(client, stranger.email)
        for suffix in ("selection", "selection/source", "selection/result"):
            assert (await client.get(f"/api/v1/assets/{saved['id']}/{suffix}")).status_code == 404
        assert (
            await client.post(
                f"/api/v1/assets/{saved['id']}/selection",
                files={"image": ("refined.png", result.data, "image/png")},
            )
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
        await service.soft_delete(session, asset_id=row.id, owner_id=owner.id, grace_days=0)
        await session.commit()
    await service.process_deletion_queue(
        asset_context.database, asset_context.storage, now=datetime.now(UTC) + timedelta(days=10)
    )
    assert service.edit_source_key(row.object_key) not in asset_context.storage.objects
    assert row.object_key not in asset_context.storage.objects


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,limited", [("cutout.smart", False), ("ai.extract_print", True)])
async def test_legacy_restoration_never_uses_a_product_photo_for_extracted_artwork(
    asset_context, operation, limited
):
    owner = await seed_user(asset_context, email="legacy-selection@example.test")
    async with client_for(asset_context, "legacy-selection") as client:
        await login(client, owner.email)
        parent = (await upload(client, raster_bytes())).json()["asset"]
        result = prepare_asset(raster_bytes(mode="RGBA"), kind="result", max_megapixels=16)
        asset = await AssetService().store(
            asset_context.database,
            asset_context.storage,
            owner_id=owner.id,
            prepared=result,
            kind="result",
            operation_code=operation,
            retention_days=30,
            parent_asset_id=UUID(parent["id"]),
        )
        context = (await client.get(f"/api/v1/assets/{asset.id}/selection")).json()
        assert context["restore_limited"] is limited
        response = await client.get(context["source_url"])
        with Image.open(BytesIO(response.content)) as image:
            assert (image.mode == "RGBA") is limited
        saved = await client.post(
            f"/api/v1/assets/{asset.id}/selection",
            files={"image": ("result.png", result.data, "image/png")},
        )
        assert saved.status_code == 201
        saved_context = (
            await client.get(f"/api/v1/assets/{saved.json()['asset']['id']}/selection")
        ).json()
        assert saved_context["restore_limited"] is limited


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["dimensions", "rgb", "empty", "jpeg", "undecodable", "megapixels", "bytes"]
)
async def test_invalid_refinements_never_create_results(asset_context, invalid):
    owner = await seed_user(asset_context, email="invalid-selection@example.test")
    async with client_for(asset_context, "invalid-selection") as client:
        await login(client, owner.email)
        source = (await upload(client, raster_bytes())).json()["asset"]
        if invalid == "dimensions":
            raw = raster_bytes(mode="RGBA", size=(13, 8))
        elif invalid == "rgb":
            raw = raster_bytes()
        elif invalid == "jpeg":
            raw = raster_bytes(image_format="JPEG", exif=Image.Exif())
        elif invalid == "empty":
            output = BytesIO()
            Image.new("RGBA", (12, 8)).save(output, format="PNG")
            raw = output.getvalue()
        elif invalid == "undecodable":
            raw = b"not an image"
        else:
            raw = raster_bytes(mode="RGBA")
            if invalid == "megapixels":
                asset_context.settings.max_image_megapixels = 0
            else:
                asset_context.settings.max_upload_mb = 0
        response = await client.post(
            f"/api/v1/assets/{source['id']}/selection",
            files={"image": ("bad.png", raw, "image/png")},
        )
        assert response.status_code == (413 if invalid == "bytes" else 422), response.text
        assert len((await client.get("/api/v1/assets")).json()["items"]) == 1


@pytest.mark.asyncio
async def test_restore_sidecar_is_cleaned_if_main_upload_fails(asset_context, monkeypatch):
    owner = await seed_user(asset_context, email="selection-storage@example.test")
    source = prepare_asset(raster_bytes(), kind="original", max_megapixels=16)
    result = prepare_asset(raster_bytes(mode="RGBA"), kind="result", max_megapixels=16)
    put = asset_context.storage.put_object

    async def fail_main(key, *args, **kwargs):
        if key.endswith("result.png"):
            raise ObjectStorageError("main failed after source upload")
        await put(key, *args, **kwargs)

    monkeypatch.setattr(asset_context.storage, "put_object", fail_main)
    service = AssetService()
    with pytest.raises(ObjectStorageError):
        await service.store(
            asset_context.database,
            asset_context.storage,
            owner_id=owner.id,
            prepared=result,
            edit_source=source,
            kind="result",
            operation_code="cutout.refine",
            retention_days=30,
        )
    assert len(asset_context.storage.objects) == 1
    assert (await service.process_deletion_queue(asset_context.database, asset_context.storage))[
        "completed"
    ] == 1
    assert not asset_context.storage.objects
