import io
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError
from sqlalchemy import select
from test_assets import asset_context as asset_fixture
from test_assets import client_for, login, raster_bytes, seed_user, upload

from app.api.points import router as points_router
from app.api.site import prepare_site_image
from app.api.site import router as site_router
from app.image_ops import finalize_print_extraction, print_extraction_key_color
from app.repositories.models import Asset, ImageJob, PointTransaction
from app.services.assets import AssetService
from app.services.configuration import BrandingValues
from app.services.image_executor import ImageJobExecutor

asset_context = asset_fixture


def png(image):
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


@pytest.mark.parametrize(
    "key,inks",
    [
        ((0, 255, 0), [(21, 145, 72), (18, 75, 36), (35, 182, 139), (214, 31, 206)]),
        ((255, 0, 255), [(0, 255, 0), (21, 145, 72), (160, 38, 128), (60, 195, 155)]),
    ],
)
def test_chroma_removal_preserves_colored_ink_and_boundary_artwork(key, inks):
    image = Image.new("RGB", (160, 160), key)
    draw = ImageDraw.Draw(image)
    for index, color in enumerate(inks):
        draw.rectangle((12 + index * 32, 25, 33 + index * 32, 130), fill=color)
    # A small line touching the canvas edge must not be blindly cropped.
    draw.rectangle((0, 76, 17, 80), fill=(255, 255, 255))
    result, _ = finalize_print_extraction(png(image))
    with Image.open(io.BytesIO(result)) as output:
        for index, color in enumerate(inks):
            assert output.getpixel((22 + index * 32, 60)) == (*color, 255)
        assert output.getpixel((0, 78)) == (255, 255, 255, 255)
        assert output.getpixel((1, 1))[3] == 0


def test_key_choice_avoids_green_art_and_edge_matting_is_local():
    assert print_extraction_key_color(png(Image.new("RGB", (50, 50), (12, 150, 68)))) == "#FF00FF"
    assert print_extraction_key_color(png(Image.new("RGB", (50, 50), (160, 20, 151)))) == "#00FF00"
    image = Image.new("RGB", (100, 100), (0, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 80, 80), fill=(100, 152, 25))  # Half-alpha red ink over green.
    draw.rectangle((21, 21, 79, 79), fill=(200, 50, 50))
    result, _ = finalize_print_extraction(png(image))
    with Image.open(io.BytesIO(result)) as output:
        red, green, blue, alpha = output.getpixel((20, 50))
        assert 120 <= alpha <= 135
        assert abs(red - 200) < 4 and abs(green - 50) < 4 and abs(blue - 50) < 4
        assert output.getpixel((50, 50)) == (200, 50, 50, 255)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "//external.test/a.png",
        "/api/v1/assets/id",
        "/brand/../x",
        "/brand/%2e%2e/x",
        "https://user:pass@example.test/image",
    ],
)
def test_branding_rejects_unsafe_urls(url):
    with pytest.raises(ValidationError):
        BrandingValues(logo_url=url)


def test_brand_image_is_bounded_webp_and_rejects_svg():
    raw = prepare_site_image(png(Image.new("RGB", (2400, 1200), "navy")))
    with Image.open(io.BytesIO(raw)) as image:
        assert image.format == "WEBP" and image.size == (1920, 960)
    with pytest.raises(ValueError):
        prepare_site_image(b'<svg xmlns="http://www.w3.org/2000/svg"/>')


@pytest.mark.asyncio
async def test_public_brand_media_upload_auth_cache_and_allowlist(asset_context):
    asset_context.app.include_router(site_router)
    admin = await seed_user(asset_context, email="brand-admin@example.test", roles=("super_admin",))
    owner = await seed_user(asset_context, email="brand-user@example.test")
    async with client_for(asset_context, "anonymous") as client:
        defaults = await client.get("/api/v1/site")
        assert defaults.status_code == 200
        assert set(defaults.json()) == set(BrandingValues().model_dump())
        assert (
            await client.post(
                "/api/v1/admin/site-media", files={"image": ("a.png", raster_bytes())}
            )
        ).status_code == 401
        await login(client, owner.email)
        assert (
            await client.post(
                "/api/v1/admin/site-media", files={"image": ("a.png", raster_bytes())}
            )
        ).status_code == 403
    async with client_for(asset_context, "admin") as client:
        await login(client, admin.email)
        uploaded = await client.post(
            "/api/v1/admin/site-media", files={"image": ("a.png", raster_bytes())}
        )
        assert uploaded.status_code == 201, uploaded.text
        url = uploaded.json()["url"]
        assert (
            await client.post(
                "/api/v1/admin/site-media", files={"image": ("a.svg", b"<svg/>", "image/svg+xml")}
            )
        ).status_code == 422
    async with client_for(asset_context, "public-image") as client:
        response = await client.get(url)
        assert response.status_code == 200 and response.headers["content-type"] == "image/webp"
        assert "immutable" in response.headers["cache-control"]
        cached = await client.get(url, headers={"If-None-Match": response.headers["etag"]})
        assert cached.status_code == 304 and not cached.content


@pytest.mark.asyncio
async def test_thumbnails_ownership_lazy_backfill_and_cleanup(asset_context):
    owner = await seed_user(asset_context, email="thumb-owner@example.test")
    outsider = await seed_user(asset_context, email="thumb-other@example.test")
    async with client_for(asset_context, "owner") as client:
        await login(client, owner.email)
        raw = png(Image.new("RGBA", (1400, 700), (30, 140, 60, 180)))
        response = await upload(client, raw)
        asset_id = uuid.UUID(response.json()["asset"]["id"])
        url = f"/api/v1/assets/{asset_id}/thumbnail"
        thumbnail = await client.get(url)
        assert thumbnail.status_code == 307 and "preview-v1.webp" in thumbnail.headers["location"]
        async with asset_context.database.session_factory() as session:
            asset = await session.get(Asset, asset_id)
            key = AssetService.thumbnail_key(asset.object_key)
            with Image.open(io.BytesIO(asset_context.storage.objects[key])) as image:
                assert image.size == (384, 192) and image.mode == "RGBA"
            # Simulate an older asset that did not yet have a preview.
            asset.asset_metadata = {}
            await asset_context.storage.delete_object(key)
            await session.commit()
        assert (await client.get(url)).status_code == 307
        assert key in asset_context.storage.objects
        async with asset_context.database.session_factory() as session:
            scan = await AssetService().scan_orphans(session, asset_context.storage)
        assert not scan.orphan_objects
    async with client_for(asset_context, "other") as client:
        await login(client, outsider.email)
        assert (await client.get(url)).status_code == 404
    async with asset_context.database.session_factory() as session:
        await AssetService().soft_delete(
            session, asset_id=asset_id, owner_id=owner.id, immediate=True, grace_days=1
        )
        await session.commit()
    await AssetService().process_deletion_queue(asset_context.database, asset_context.storage)
    assert key not in asset_context.storage.objects


@pytest.mark.asyncio
async def test_quality_quote_default_charge_and_progress_fences(asset_context):
    owner = await seed_user(asset_context, email="quality-owner@example.test")
    async with client_for(asset_context, "owner") as client:
        await login(client, owner.email)
        source_id = (await upload(client, raster_bytes())).json()["asset"]["id"]
        quotes = {}
        for quality in ("medium", "high", "auto", "omitted"):
            parameters = {} if quality == "omitted" else {"quality": quality}
            response = await client.post(
                "/api/v1/jobs/quote",
                json={
                    "operation_code": "ai.extract_print",
                    "source_asset_id": source_id,
                    "parameters": parameters,
                },
            )
            assert response.status_code == 201, response.text
            quotes[quality] = response.json()["quote"]
        assert quotes["high"]["final_points"] > quotes["medium"]["final_points"]
        assert (
            quotes["omitted"]["final_points"]
            == quotes["auto"]["final_points"]
            == quotes["high"]["final_points"]
        )
        created = await client.post(
            "/api/v1/jobs",
            headers={"Idempotency-Key": "quality-test"},
            json={"quote_id": quotes["high"]["id"], "parameters": {"quality": "high"}},
        )
        assert created.status_code == 201, created.text
        job_id = uuid.UUID(created.json()["job"]["id"])
        assert created.json()["job"]["charged_points"] == quotes["high"]["final_points"]
    executor = ImageJobExecutor(
        asset_context.settings, asset_context.database, asset_context.storage
    )
    claim = SimpleNamespace(job_id=job_id, attempt_no=1)
    async with asset_context.database.session_factory() as session:
        job = await session.get(ImageJob, job_id)
        job.status = "running"
        job.attempt_count = 1
        await session.commit()
    await executor._progress(claim, 20)
    await executor._progress(claim, 8)
    await executor._progress(SimpleNamespace(job_id=job_id, attempt_no=2), 90)
    async with asset_context.database.session_factory() as session:
        job = await session.get(ImageJob, job_id)
        assert job.progress == 20
        job.status = "failed"
        await session.commit()
    await executor._progress(claim, 96)
    async with asset_context.database.session_factory() as session:
        assert (await session.get(ImageJob, job_id)).progress == 20


@pytest.mark.asyncio
async def test_asset_and_ledger_filters_are_applied_before_cursor_limit(asset_context):
    asset_context.app.include_router(points_router)
    owner = await seed_user(asset_context, email="pages-owner@example.test")
    async with client_for(asset_context, "owner") as client:
        await login(client, owner.email)
        for _ in range(4):
            assert (await upload(client, raster_bytes())).status_code == 201
        first = (await client.get("/api/v1/assets?limit=2")).json()
        second = (await client.get(f"/api/v1/assets?limit=2&cursor={first['next_cursor']}")).json()
        assert len(first["items"]) == len(second["items"]) == 2
        assert not {row["id"] for row in first["items"]} & {row["id"] for row in second["items"]}
        assert second["next_cursor"] is None
        target = second["items"][0]
        async with asset_context.database.session_factory() as session:
            asset = await session.get(Asset, uuid.UUID(target["id"]))
            asset.created_at = datetime.now(UTC) - timedelta(days=3)
            day = asset.created_at.date().isoformat()
            await session.commit()
        filtered = (
            await client.get(
                f"/api/v1/assets?limit=1&created_day={day}&root_query={target['root_asset_id'][:8]}"
            )
        ).json()
        assert [row["id"] for row in filtered["items"]] == [target["id"]]
        async with asset_context.database.session_factory() as session:
            from app.services.points import PointService

            for index in range(4):
                await PointService().apply_transaction(
                    session,
                    user_id=owner.id,
                    entry_type="adjust",
                    delta=2,
                    reference_type="manual_adjustment",
                    reference_id=uuid.uuid4(),
                    request_fingerprint=None,
                    metadata={},
                    actor_user_id=owner.id,
                    description="page test",
                    idempotency_key=f"page-adjust-{index}",
                    request_id="page-test",
                )
            await session.commit()
        page = await client.get("/api/v1/points/transactions?limit=2&category=adjust")
        assert page.status_code == 200, page.text
        assert len(page.json()["items"]) == 2 and page.json()["next_cursor"]
        assert all(row["entry_type"] == "adjust" for row in page.json()["items"])
        next_page = await client.get(
            f"/api/v1/points/transactions?limit=2&category=adjust&cursor={page.json()['next_cursor']}"
        )
        assert len(next_page.json()["items"]) == 2 and next_page.json()["next_cursor"] is None
        async with asset_context.database.session_factory() as session:
            assert (
                len(
                    (
                        await session.scalars(
                            select(PointTransaction).where(PointTransaction.user_id == owner.id)
                        )
                    ).all()
                )
                == 5
            )
