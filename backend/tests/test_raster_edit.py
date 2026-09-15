import json
import struct
import uuid
from datetime import UTC, datetime, timedelta
from io import BytesIO

import numpy as np
import pytest
from PIL import Image
from sqlalchemy import func, select
from test_assets import asset_context as asset_fixture
from test_assets import client_for, login, raster_bytes, seed_user, upload

from app.object_storage import ObjectStorageError
from app.repositories.models import Asset, ImageJob, PointAccount
from app.services.asset_files import AssetInputError, prepare_asset
from app.services.assets import AssetService
from app.services.raster_project import validate_raster_project

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


def project_bytes(manifest=None, images=None):
    images = images or [png("blue"), png("red"), png("green")]
    manifest = manifest or {
        "version": 1,
        "width": 12,
        "height": 8,
        "activeId": "paint",
        "layers": [
            {
                "id": "base",
                "name": "原图",
                "visible": True,
                "locked": True,
                "opacity": 100,
                "byteLength": len(images[0]),
            },
            {
                "id": "paint",
                "name": "填色",
                "visible": True,
                "locked": False,
                "opacity": 50,
                "byteLength": len(images[1]),
            },
            {
                "id": "hidden",
                "name": "隐藏",
                "visible": False,
                "locked": False,
                "opacity": 100,
                "byteLength": len(images[2]),
            },
        ],
    }
    header = json.dumps(manifest, ensure_ascii=False).encode()
    return struct.pack("<I", len(header)) + header + b"".join(images)


def test_project_validation_composites_only_visible_layers_and_rejects_malformed_documents():
    raw = project_bytes()
    normalized, prepared = validate_raster_project(raw, 12, 8, 16)
    with Image.open(BytesIO(prepared.data)) as result:
        assert result.getpixel((0, 0)) == (128, 0, 128, 255)
    assert validate_raster_project(normalized, 12, 8, 16)[1].data == prepared.data
    length = struct.unpack_from("<I", raw)[0]
    header = json.loads(raw[4 : 4 + length])
    invalid = [b"broken", struct.pack("<I", 99999) + b"abc", raw[:-1], raw + b"extra"]
    for field, value in [("activeId", "missing"), ("width", 13), ("layers", header["layers"] * 5)]:
        invalid.append(project_bytes({**header, field: value}))
    for field, value in [
        ("opacity", 101),
        ("visible", "yes"),
        ("name", "  "),
        ("id", "../path"),
        ("byteLength", 10**12),
    ]:
        invalid.append(
            project_bytes(
                {**header, "layers": [{**header["layers"][0], field: value}, *header["layers"][1:]]}
            )
        )
    invalid.append(project_bytes(images=[png("red", (13, 8)), png("red"), png("green")]))
    invalid.append(
        project_bytes(
            {**header, "layers": [{**layer, "visible": False} for layer in header["layers"]]}
        )
    )
    for bad in invalid:
        with pytest.raises(AssetInputError):
            validate_raster_project(bad, 12, 8, 16)


@pytest.mark.asyncio
async def test_layers_persist_with_the_result_and_follow_owner_deletion_and_retention(
    asset_context,
):
    owner = await seed_user(asset_context, email="layers-owner@example.test")
    other = await seed_user(asset_context, email="layers-other@example.test")
    service = AssetService()
    async with client_for(asset_context, "layers-owner") as client:
        await login(client, owner.email)
        source = (await upload(client, raster_bytes())).json()["asset"]
        response = await client.post(
            f"/api/v1/assets/{source['id']}/edit",
            files={
                "image": ("result.png", png("white"), "image/png"),
                "project": ("layers.raster", project_bytes(), "application/octet-stream"),
            },
        )
        assert response.status_code == 201, response.text
        result = response.json()["asset"]
        assert result["metadata"]["raster_project_ready"] is True
        project_url = f"/api/v1/assets/{result['id']}/edit/project"
        loaded = await client.get(project_url)
        assert loaded.headers["cache-control"] == "private, no-store"
        length = struct.unpack_from("<I", loaded.content)[0]
        manifest = json.loads(loaded.content[4 : 4 + length])
        assert manifest["activeId"] == "paint"
        assert manifest["layers"][0]["locked"] is True
        assert manifest["layers"][1]["opacity"] == 50
        assert manifest["layers"][2]["visible"] is False
        # The server derives the result from the layers, not an unrelated client preview.
        image = await client.get(f"/api/v1/assets/{result['id']}/selection/result")
        with Image.open(BytesIO(image.content)) as pixels:
            assert pixels.getpixel((0, 0)) == (128, 0, 128, 255)
        bad = await client.post(
            f"/api/v1/assets/{source['id']}/edit",
            files={
                "image": ("result.png", png("white"), "image/png"),
                "project": ("layers.raster", b"broken", "application/octet-stream"),
            },
        )
        assert bad.status_code == 422
        assert (await client.get(f"/api/v1/assets/{source['id']}/edit/project")).status_code == 404
        await client.delete(f"/api/v1/assets/{result['id']}")
        assert (await client.get(project_url)).status_code == 404
        await client.post(f"/api/v1/assets/{result['id']}/restore")
        assert (await client.get(project_url)).content == loaded.content
    async with client_for(asset_context, "layers-other") as client:
        assert (await client.get(project_url)).status_code == 401
        await login(client, other.email)
        assert (await client.get(project_url)).status_code == 404
    async with asset_context.database.session_factory() as session:
        assert not (await service.scan_orphans(session, asset_context.storage)).orphan_objects
        assert await session.scalar(select(func.count()).select_from(Asset)) == 2
        asset = await session.get(Asset, uuid.UUID(result["id"]))
        key = service.raster_project_key(asset.object_key)
        await service.soft_delete(session, asset_id=asset.id, owner_id=owner.id, grace_days=0)
        await session.commit()
    await service.process_deletion_queue(
        asset_context.database, asset_context.storage, now=datetime.now(UTC) + timedelta(days=1)
    )
    assert key not in asset_context.storage.objects


@pytest.mark.asyncio
async def test_partial_layer_upload_is_not_published_and_its_objects_are_cleaned(
    asset_context, monkeypatch
):
    owner = await seed_user(asset_context, email="layer-failure@example.test")
    project, prepared = validate_raster_project(project_bytes(), 12, 8, 16)
    storage = asset_context.storage
    put = storage.put_object

    async def fail_source(key, data, **kwargs):
        if key.endswith("cutout-source-v1.png"):
            raise ObjectStorageError("source write failed")
        await put(key, data, **kwargs)

    monkeypatch.setattr(storage, "put_object", fail_source)
    service = AssetService()
    with pytest.raises(ObjectStorageError):
        await service.store(
            asset_context.database,
            storage,
            owner_id=owner.id,
            prepared=prepared,
            edit_source=prepared,
            raster_project=project,
            kind="result",
            operation_code="image.edit",
            retention_days=30,
        )
    async with asset_context.database.session_factory() as session:
        asset = (await session.scalars(select(Asset))).one()
        assert asset.status == "deleted"
        assert service.raster_project_key(asset.object_key) in storage.objects
    await service.process_deletion_queue(asset_context.database, storage)
    assert not storage.objects


@pytest.mark.asyncio
async def test_admin_purge_removes_layer_document_and_preview_with_main_image(asset_context):
    operator = await seed_user(
        asset_context, email="layer-admin@example.test", roles=("user", "operator")
    )
    project, prepared = validate_raster_project(project_bytes(), 12, 8, 16)
    service = AssetService()
    asset = await service.store(
        asset_context.database,
        asset_context.storage,
        owner_id=operator.id,
        prepared=prepared,
        edit_source=prepared,
        raster_project=project,
        kind="result",
        operation_code="image.edit",
        retention_days=30,
    )
    async with client_for(asset_context, "layer-admin") as client:
        await login(client, operator.email)
        response = await client.delete(f"/api/v1/admin/assets/{asset.id}")
        assert response.status_code == 200, response.text
        assert response.json()["purged"] is True
    assert not any(
        key.startswith(asset.object_key.rsplit("/", 1)[0]) for key in asset_context.storage.objects
    )
