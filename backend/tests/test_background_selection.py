from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import UUID

import pytest
from PIL import Image, ImageOps
from sqlalchemy import select
from test_assets import asset_context as asset_fixture
from test_assets import client_for, login, raster_bytes, seed_user, upload

from app.object_storage import ObjectStorageError
from app.repositories.models import Asset, PointAccount
from app.services.asset_files import inspect_stored_asset, prepare_asset
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


@pytest.mark.asyncio
@pytest.mark.parametrize("with_sidecar", [False, True])
async def test_selection_serves_stored_png_bytes_without_normalization(
    asset_context, monkeypatch, with_sidecar
):
    owner = await seed_user(asset_context, email="selection-png-fast-path@example.test")
    prepared = prepare_asset(raster_bytes(mode="RGBA"), kind="original", max_megapixels=16)
    source = prepare_asset(raster_bytes(), kind="original", max_megapixels=16)
    asset = await AssetService().store(
        asset_context.database,
        asset_context.storage,
        owner_id=owner.id,
        prepared=prepared,
        edit_source=source if with_sidecar else None,
        kind="result" if with_sidecar else "original",
        operation_code="ai.extract_print" if with_sidecar else "upload",
        retention_days=30,
    )

    def unexpected_normalization(*args, **kwargs):
        pytest.fail("Reading stored PNG pixels must not decode or normalize the image again")

    monkeypatch.setattr("app.api.assets.prepare_asset", unexpected_normalization)
    async with client_for(asset_context, "selection-png-fast-path") as client:
        await login(client, owner.email)
        response = await client.get(f"/api/v1/assets/{asset.id}/selection")
        assert response.status_code == 200, response.text
        context = response.json()
        assert (context["width"], context["height"]) == (12, 8)
        assert context["has_initial_selection"] is with_sidecar
        assert context["restore_limited"] is False
        if with_sidecar:
            assert context["source_url"] != context["result_url"]
        else:
            assert context["source_url"] == context["result_url"]
        for layer, expected in (
            ("source", source.data if with_sidecar else prepared.data),
            ("result", prepared.data),
        ):
            response = await client.get(f"/api/v1/assets/{asset.id}/selection/{layer}")
            assert response.status_code == 200, response.text
            assert response.content == expected
            assert response.headers["content-type"] == "image/png"
            assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("image_format,extension", [("JPEG", "jpg"), ("WEBP", "webp")])
async def test_selection_still_orients_and_normalizes_legacy_non_png_sources(
    asset_context, image_format, extension
):
    owner = await seed_user(asset_context, email="selection-legacy-format@example.test")
    exif = Image.Exif()
    exif[274] = 6
    image = Image.new("RGB", (12, 8), "white")
    image.paste("red", (0, 0, 6, 4))
    buffer = BytesIO()
    image.save(buffer, image_format, exif=exif)
    raw = buffer.getvalue()
    prepared = prepare_asset(raw, kind="original", max_megapixels=16)
    asset = await AssetService().store(
        asset_context.database,
        asset_context.storage,
        owner_id=owner.id,
        prepared=prepared,
        kind="original",
        operation_code="upload",
        retention_days=30,
    )
    # Represent a pre-normalization object, retaining its correctly oriented catalog geometry.
    asset_context.storage.objects[asset.object_key] = raw
    async with asset_context.database.session_factory() as session:
        row = await session.get(Asset, asset.id)
        row.mime_type = "image/jpeg" if image_format == "JPEG" else "image/webp"
        row.extension = extension
        await session.commit()
    async with client_for(asset_context, "selection-legacy-format") as client:
        await login(client, owner.email)
        context = (await client.get(f"/api/v1/assets/{asset.id}/selection")).json()
        assert context["source_url"] == context["result_url"]
        assert (context["width"], context["height"]) == (8, 12)
        for layer in ("source", "result"):
            response = await client.get(f"/api/v1/assets/{asset.id}/selection/{layer}")
            assert response.status_code == 200, response.text
            assert response.content == prepared.data
            with (
                Image.open(BytesIO(response.content)) as result,
                Image.open(BytesIO(raw)) as stored,
            ):
                assert result.format == "PNG" and result.size == (8, 12)
                assert result.getexif().get(274) is None
                assert result.tobytes() == ImageOps.exif_transpose(stored).convert("RGB").tobytes()


@pytest.mark.asyncio
async def test_editable_asset_filter_runs_before_cursor_pagination(asset_context):
    owner = await seed_user(asset_context, email="editable-page-owner@example.test")
    stranger = await seed_user(asset_context, email="editable-page-stranger@example.test")
    service = AssetService()
    raster = prepare_asset(raster_bytes(), kind="original", max_megapixels=16)
    mask = prepare_asset(raster_bytes(mode="RGBA"), kind="mask", max_megapixels=16)
    thumbnail = prepare_asset(raster_bytes(), kind="thumbnail", max_megapixels=16)
    vector = prepare_asset(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8"><path d="M0 0L12 8"/></svg>',
        kind="vector",
        max_megapixels=16,
    )
    timestamp = datetime.now(UTC) - timedelta(hours=1)
    sequence = 0

    async def store(prepared, kind, *, owner_id=None):
        nonlocal sequence
        sequence += 1
        return await service.store(
            asset_context.database,
            asset_context.storage,
            owner_id=owner_id or owner.id,
            prepared=prepared,
            kind=kind,
            operation_code="upload",
            retention_days=30,
            now=timestamp + timedelta(seconds=sequence),
        )

    eligible = []
    all_owner_ids = []
    for kind in ("original", "result"):
        for image_format, extension in (("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp")):
            # Surround each eligible row with ineligible rows so filtering after LIMIT loses pages.
            all_owner_ids.append(str((await store(mask, "mask")).id))
            prepared = inspect_stored_asset(
                raster_bytes(image_format=image_format, exif=Image.Exif()), extension=extension
            )
            asset = await store(prepared, kind)
            eligible.append(asset)
            all_owner_ids.append(str(asset.id))
            all_owner_ids.append(str((await store(thumbnail, "thumbnail")).id))
            all_owner_ids.append(str((await store(vector, "vector")).id))
    unsupported = await store(raster, "result")
    all_owner_ids.append(str(unsupported.id))
    deleted = await store(raster, "original")
    await store(raster, "original", owner_id=stranger.id)
    async with asset_context.database.session_factory() as session:
        row = await session.get(Asset, unsupported.id)
        row.mime_type = "image/gif"
        await service.soft_delete(session, asset_id=deleted.id, owner_id=owner.id, grace_days=7)
        await session.commit()

    expected = [str(asset.id) for asset in reversed(eligible)]
    async with client_for(asset_context, "editable-page-owner") as client:
        await login(client, owner.email)
        seen = []
        cursor = None
        for page_index in range(3):
            parameters = {"editable_only": "true", "limit": 2}
            if cursor:
                parameters["cursor"] = cursor
            response = await client.get("/api/v1/assets", params=parameters)
            assert response.status_code == 200, response.text
            page = response.json()
            ids = [item["id"] for item in page["items"]]
            assert ids == expected[page_index * 2 : page_index * 2 + 2]
            seen.extend(ids)
            cursor = page["next_cursor"]
            assert cursor == (ids[-1] if page_index < 2 else None)
        assert seen == expected and len(set(seen)) == 6
        unfiltered = await client.get("/api/v1/assets", params={"limit": 100})
        assert {item["id"] for item in unfiltered.json()["items"]} == set(all_owner_ids)
        result_only = await client.get(
            "/api/v1/assets", params={"editable_only": "true", "kind": "result", "limit": 100}
        )
        assert {item["id"] for item in result_only.json()["items"]} == {
            str(asset.id) for asset in eligible if asset.kind == "result"
        }
