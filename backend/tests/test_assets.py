from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import BytesIO
from urllib.parse import quote as url_quote

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select

from app.api.assets import router as assets_router
from app.api.auth import router as auth_router
from app.api.errors import install_exception_handlers
from app.api.jobs import router as jobs_router
from app.api.middleware import RequestContextMiddleware
from app.catalog import StudioCatalog
from app.config import Settings
from app.domain.ids import uuid7
from app.migrations.local_results import migrate_local_results
from app.object_storage import ObjectStorageError, StoredObject
from app.repositories.database import Database
from app.repositories.models import (
    Asset,
    AssetAccessLog,
    Base,
    ImageJob,
    ObjectDeletionQueue,
    OutboxEvent,
    Role,
    User,
    UserRole,
)
from app.services.asset_files import prepare_asset
from app.services.assets import AssetService
from app.services.auth import AuthService
from app.services.image_executor import ImageJobExecutor
from app.services.jobs import sync_builtin_operations
from app.services.memberships import EntitlementService, sync_builtin_membership_plans
from app.services.points import PointService
from app.services.rbac import sync_builtin_rbac
from app.storage import ResultStore
from app.workers.worker import execute_image_job


@dataclass
class FakeObjectStorage:
    bucket: str = "private-test-bucket"
    objects: dict[str, bytes] = field(default_factory=dict)
    metadata: dict[str, dict[str, str]] = field(default_factory=dict)
    modified: dict[str, datetime] = field(default_factory=dict)
    fail_puts: int = 0
    fail_deletes: int = 0

    async def ping(self) -> None:
        return None

    async def put_object(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        metadata: dict[str, str],
    ) -> None:
        if self.fail_puts:
            self.fail_puts -= 1
            raise ObjectStorageError("put failed")
        self.objects[key] = data
        self.metadata[key] = dict(metadata)
        self.modified[key] = datetime.now(UTC)

    async def get_object(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError as exc:
            raise ObjectStorageError("object missing") from exc

    async def head_object(self, key: str) -> StoredObject | None:
        if key not in self.objects:
            return None
        return StoredObject(
            key=key,
            size=len(self.objects[key]),
            last_modified=self.modified[key],
            metadata=self.metadata.get(key, {}),
        )

    async def delete_object(self, key: str) -> None:
        if self.fail_deletes:
            self.fail_deletes -= 1
            raise ObjectStorageError("delete failed")
        self.objects.pop(key, None)
        self.metadata.pop(key, None)
        self.modified.pop(key, None)

    async def presign_get(
        self,
        key: str,
        *,
        expires_seconds: int,
        download_filename: str | None = None,
    ) -> str:
        if key not in self.objects:
            raise ObjectStorageError("object missing")
        return (
            f"https://r2.invalid/{url_quote(key)}?X-Amz-Expires={expires_seconds}"
            "&X-Amz-Signature=test-only"
        )

    async def list_objects(self, prefix: str = "") -> list[StoredObject]:
        return [
            StoredObject(
                key=key,
                size=len(data),
                last_modified=self.modified[key],
                metadata=self.metadata.get(key, {}),
            )
            for key, data in self.objects.items()
            if key.startswith(prefix)
        ]


@dataclass
class AssetContext:
    app: FastAPI
    database: Database
    settings: Settings
    auth_service: AuthService
    storage: FakeObjectStorage


@pytest_asyncio.fixture
async def asset_context(tmp_path) -> AssetContext:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'assets.db').as_posix()}"
    settings = Settings(
        _env_file=None,
        APP_ENV="test",
        DATABASE_URL=database_url,
        ONBOARDING_POINTS=200,
        ASSET_ORPHAN_GRACE_HOURS=0,
    )
    database = Database(database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with database.session_factory() as session:
        await sync_builtin_rbac(session)
        await sync_builtin_membership_plans(session)
        await sync_builtin_operations(session)
        await session.commit()
    auth_service = AuthService(settings)
    storage = FakeObjectStorage()
    app = FastAPI()
    app.state.settings = settings
    app.state.auth_service = auth_service
    app.state.object_storage = storage
    app.state.runtime_services = type("Runtime", (), {"database": database})()

    async def enqueue(_job_id: uuid.UUID, _queue_name: str) -> bool:
        return True

    app.state.job_enqueuer = enqueue
    install_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(auth_router)
    app.include_router(jobs_router)
    app.include_router(assets_router)
    yield AssetContext(app, database, settings, auth_service, storage)
    await database.dispose()


async def seed_user(
    context: AssetContext,
    *,
    email: str,
    roles: tuple[str, ...] = ("user",),
) -> User:
    async with context.database.session_factory() as session:
        user = User(
            id=uuid7(),
            email=email,
            username=email.split("@", 1)[0],
            password_hash=context.auth_service.hash_password("correct horse battery staple"),
            display_name=email.split("@", 1)[0],
            status="active",
            session_version=1,
            permission_version=1,
        )
        session.add(user)
        await session.flush()
        role_rows = list((await session.scalars(select(Role).where(Role.code.in_(roles)))).all())
        session.add_all(
            UserRole(user_id=user.id, role_id=role.id, assigned_by=user.id) for role in role_rows
        )
        await EntitlementService().ensure_default_membership(
            session, user.id, assigned_by=user.id, request_id="asset-test-seed"
        )
        await PointService().ensure_onboarding_grant(
            session, user.id, points=200, request_id="asset-test-seed"
        )
        await session.commit()
        return user


def client_for(context: AssetContext, user_agent: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=context.app),
        base_url="http://testserver",
        headers={"user-agent": user_agent},
    )


async def login(client: AsyncClient, email: str) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"identifier": email, "password": "correct horse battery staple"},
    )
    assert response.status_code == 200, response.text


def raster_bytes(
    *,
    mode: str = "RGB",
    size: tuple[int, int] = (12, 8),
    image_format: str = "PNG",
    exif: Image.Exif | None = None,
) -> bytes:
    color = (220, 30, 40, 128) if mode == "RGBA" else (220, 30, 40)
    image = Image.new(mode, size, color)
    output = BytesIO()
    image.save(output, format=image_format, exif=exif)
    return output.getvalue()


async def upload(
    client: AsyncClient,
    data: bytes,
    *,
    filename: str = "source.png",
    kind: str = "original",
    parent_asset_id: str | None = None,
):
    form = {"kind": kind}
    if parent_asset_id:
        form["parent_asset_id"] = parent_asset_id
    return await client.post(
        "/api/v1/assets/upload",
        data=form,
        files={"image": (filename, data, "application/octet-stream")},
    )


@pytest.mark.asyncio
async def test_private_upload_normalizes_exif_and_enforces_access(
    asset_context: AssetContext,
) -> None:
    owner = await seed_user(asset_context, email="asset-owner@example.com")
    stranger = await seed_user(asset_context, email="asset-stranger@example.com")
    auditor = await seed_user(
        asset_context, email="asset-auditor@example.com", roles=("user", "auditor")
    )
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "private camera note"
    raw = raster_bytes(size=(12, 8), image_format="JPEG", exif=exif)

    async with (
        client_for(asset_context, "owner-device") as owner_client,
        client_for(asset_context, "stranger-device") as stranger_client,
        client_for(asset_context, "auditor-device") as auditor_client,
    ):
        await login(owner_client, owner.email)
        await login(stranger_client, stranger.email)
        await login(auditor_client, auditor.email)
        response = await upload(owner_client, raw, filename="../../camera-secret.jpg")
        assert response.status_code == 201, response.text
        asset = response.json()["asset"]
        assert asset["mime_type"] == "image/png"
        assert asset["extension"] == "png"
        assert asset["original_filename"] == "camera-secret.jpg"
        assert (asset["width"], asset["height"]) == (8, 12)
        assert "object_key" not in asset and "bucket" not in asset

        asset_id = asset["id"]
        key = next(iter(asset_context.storage.objects))
        assert key.startswith(f"users/{owner.id}/")
        assert key.endswith(f"/{asset_id}/original.png")
        with Image.open(BytesIO(asset_context.storage.objects[key])) as normalized:
            assert not normalized.getexif()

        assert (await stranger_client.get(f"/api/v1/assets/{asset_id}")).status_code == 404
        assert (
            await stranger_client.post(f"/api/v1/assets/{asset_id}/download-url")
        ).status_code == 404
        detail = await owner_client.get(f"/api/v1/assets/{asset_id}")
        assert detail.status_code == 200
        signed = await owner_client.post(f"/api/v1/assets/{asset_id}/download-url")
        assert signed.status_code == 200
        assert signed.json()["expires_in"] == 600
        assert "X-Amz-Signature" in signed.json()["url"]
        admin_detail = await auditor_client.get(f"/api/v1/admin/assets/{asset_id}")
        assert admin_detail.status_code == 200
        admin_signed = await auditor_client.post(f"/api/v1/admin/assets/{asset_id}/download-url")
        assert admin_signed.status_code == 200
        denied = await auditor_client.post(
            f"/api/v1/admin/assets/{asset_id}/quarantine",
            json={"reason": "read-only auditor"},
        )
        assert denied.status_code == 403

    async with asset_context.database.session_factory() as session:
        logs = list((await session.scalars(select(AssetAccessLog))).all())
        assert [log.action for log in logs] == [
            "preview",
            "download",
            "admin_preview",
            "admin_preview",
        ]
        stored = await session.get(Asset, uuid.UUID(asset_id))
        assert stored is not None
        assert stored.sha256 == hashlib.sha256(asset_context.storage.objects[key]).hexdigest()


@pytest.mark.asyncio
async def test_lineage_soft_delete_restore_quarantine_and_source_ownership(
    asset_context: AssetContext,
) -> None:
    owner = await seed_user(asset_context, email="lineage-owner@example.com")
    stranger = await seed_user(asset_context, email="lineage-stranger@example.com")
    operator = await seed_user(
        asset_context, email="lineage-operator@example.com", roles=("user", "operator")
    )
    async with (
        client_for(asset_context, "lineage-owner") as owner_client,
        client_for(asset_context, "lineage-stranger") as stranger_client,
        client_for(asset_context, "lineage-operator") as operator_client,
    ):
        await login(owner_client, owner.email)
        await login(stranger_client, stranger.email)
        await login(operator_client, operator.email)
        root_response = await upload(owner_client, raster_bytes())
        root = root_response.json()["asset"]
        child_response = await upload(
            owner_client,
            raster_bytes(mode="RGBA"),
            filename="mask.png",
            kind="mask",
            parent_asset_id=root["id"],
        )
        assert child_response.status_code == 201, child_response.text
        child = child_response.json()["asset"]
        assert child["root_asset_id"] == root["id"]
        lineage = await owner_client.get(f"/api/v1/assets/{child['id']}/lineage")
        assert [item["id"] for item in lineage.json()["items"]] == [root["id"], child["id"]]

        foreign_quote = await stranger_client.post(
            "/api/v1/jobs/quote",
            json={
                "operation_code": "color.effect",
                "source_asset_id": root["id"],
                "parameters": {"mode": "grayscale"},
            },
        )
        assert foreign_quote.status_code == 404
        own_quote = await owner_client.post(
            "/api/v1/jobs/quote",
            json={
                "operation_code": "color.effect",
                "source_asset_id": root["id"],
                "parameters": {"mode": "grayscale"},
            },
        )
        assert own_quote.status_code == 201

        deleted = await owner_client.delete(f"/api/v1/assets/{root['id']}")
        assert deleted.status_code == 200
        assert deleted.json()["asset"]["status"] == "deleted"
        assert (
            await owner_client.post(f"/api/v1/assets/{root['id']}/download-url")
        ).status_code == 404
        restored = await owner_client.post(f"/api/v1/assets/{root['id']}/restore")
        assert restored.status_code == 200
        assert restored.json()["asset"]["status"] == "ready"

        quarantine = await operator_client.post(
            f"/api/v1/admin/assets/{root['id']}/quarantine",
            json={"reason": "policy review"},
        )
        assert quarantine.status_code == 200
        assert (await owner_client.get(f"/api/v1/assets/{root['id']}")).status_code == 404
        released = await operator_client.post(
            f"/api/v1/admin/assets/{root['id']}/quarantine",
            json={"quarantined": False, "reason": "review complete"},
        )
        assert released.status_code == 200

    async with asset_context.database.session_factory() as session:
        queue = (
            await session.scalars(
                select(ObjectDeletionQueue).where(
                    ObjectDeletionQueue.asset_id == uuid.UUID(root["id"])
                )
            )
        ).one()
        assert queue.status == "cancelled"
        deleted_at = datetime.fromisoformat(deleted.json()["asset"]["deleted_at"])
        if deleted_at.tzinfo is None:
            deleted_at = deleted_at.replace(tzinfo=UTC)
        assert (
            timedelta(days=6, hours=23)
            < queue.execute_after.replace(tzinfo=UTC) - deleted_at
            < timedelta(days=7, minutes=1)
        )


@pytest.mark.asyncio
async def test_upload_failure_deletion_retry_and_orphan_reconciliation(
    asset_context: AssetContext,
) -> None:
    owner = await seed_user(asset_context, email="lifecycle-owner@example.com")
    operator = await seed_user(
        asset_context, email="lifecycle-operator@example.com", roles=("user", "operator")
    )
    async with (
        client_for(asset_context, "lifecycle-owner") as owner_client,
        client_for(asset_context, "lifecycle-operator") as operator_client,
    ):
        await login(owner_client, owner.email)
        await login(operator_client, operator.email)
        asset_context.storage.fail_puts = 1
        failed_upload = await upload(owner_client, raster_bytes())
        assert failed_upload.status_code == 503

        created = await upload(owner_client, raster_bytes(), filename="ready.png")
        assert created.status_code == 201
        asset_id = uuid.UUID(created.json()["asset"]["id"])
        async with asset_context.database.session_factory() as session:
            asset = await session.get(Asset, asset_id)
            assert asset is not None
            object_key = asset.object_key
            await AssetService().soft_delete(
                session,
                asset_id=asset.id,
                owner_id=owner.id,
                grace_days=7,
                now=datetime.now(UTC),
                immediate=True,
            )
            await session.commit()
        asset_context.storage.fail_deletes = 1
        first = await AssetService().process_deletion_queue(
            asset_context.database, asset_context.storage
        )
        second = await AssetService().process_deletion_queue(
            asset_context.database, asset_context.storage
        )
        assert first["failed"] == 1
        assert second["completed"] >= 1
        assert object_key not in asset_context.storage.objects

        missing_asset = await upload(owner_client, raster_bytes(), filename="missing.png")
        missing_id = uuid.UUID(missing_asset.json()["asset"]["id"])
        async with asset_context.database.session_factory() as session:
            missing_row = await session.get(Asset, missing_id)
            assert missing_row is not None
            asset_context.storage.objects.pop(missing_row.object_key)
            asset_context.storage.metadata.pop(missing_row.object_key)
            asset_context.storage.modified.pop(missing_row.object_key)
        orphan_key = "users/orphan/2026/09/untracked/result.png"
        asset_context.storage.objects[orphan_key] = b"orphan"
        asset_context.storage.metadata[orphan_key] = {}
        asset_context.storage.modified[orphan_key] = datetime.now(UTC) - timedelta(days=2)

        scan = await operator_client.post("/api/v1/admin/storage/orphans/scan")
        assert scan.status_code == 200
        assert [item["object_key"] for item in scan.json()["orphan_objects"]] == [orphan_key]
        assert scan.json()["missing_assets"][0]["id"] == str(missing_id)
        reconciled = await operator_client.post("/api/v1/admin/storage/orphans/reconcile")
        assert reconciled.status_code == 200
        assert reconciled.json()["result"] == {
            "orphan_objects_removed": 1,
            "orphan_objects_skipped": 0,
            "missing_assets_quarantined": 1,
        }
        assert orphan_key not in asset_context.storage.objects

    async with asset_context.database.session_factory() as session:
        failed_rows = list(
            (
                await session.scalars(
                    select(Asset).where(Asset.asset_metadata["upload_failed"].as_boolean())
                )
            ).all()
        )
        assert len(failed_rows) == 1
        missing = await session.get(Asset, missing_id)
        assert missing is not None and missing.status == "quarantined"


@pytest.mark.asyncio
async def test_svg_active_content_is_rejected_and_retention_notice_is_once(
    asset_context: AssetContext,
) -> None:
    owner = await seed_user(asset_context, email="svg-owner@example.com")
    async with client_for(asset_context, "svg-owner") as client:
        await login(client, owner.email)
        unsafe_svg = (
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
            b'<script>alert(1)</script><path d="M0 0 L10 10"/></svg>'
        )
        rejected = await upload(client, unsafe_svg, filename="unsafe.svg", kind="vector")
        assert rejected.status_code == 422
        uploaded = await upload(client, raster_bytes(), filename="notice.png")
        assert uploaded.status_code == 201

    notice_time = datetime.now(UTC) + timedelta(days=24)
    service = AssetService()
    assert await service.notify_expiring_assets(asset_context.database, now=notice_time) == 1
    assert (
        await service.notify_expiring_assets(
            asset_context.database, now=notice_time + timedelta(hours=1)
        )
        == 0
    )
    async with asset_context.database.session_factory() as session:
        events = list(
            (
                await session.scalars(
                    select(OutboxEvent).where(OutboxEvent.topic == "assets.retention_expiring")
                )
            ).all()
        )
        assert len(events) == 1


@pytest.mark.asyncio
async def test_real_worker_executor_reads_source_and_stores_result(
    asset_context: AssetContext,
) -> None:
    owner = await seed_user(asset_context, email="executor-owner@example.com")
    async with client_for(asset_context, "executor-owner") as client:
        await login(client, owner.email)
        source_response = await upload(client, raster_bytes())
        source = source_response.json()["asset"]
        quote_response = await client.post(
            "/api/v1/jobs/quote",
            json={
                "operation_code": "color.effect",
                "source_asset_id": source["id"],
                "parameters": {"mode": "grayscale"},
            },
        )
        job_response = await client.post(
            "/api/v1/jobs",
            headers={"Idempotency-Key": "real-executor-color"},
            json={
                "quote_id": quote_response.json()["quote"]["id"],
                "parameters": {"mode": "grayscale"},
            },
        )
        job_id = job_response.json()["job"]["id"]

    runtime = type(
        "WorkerRuntime",
        (),
        {
            "database": asset_context.database,
            "object_storage": asset_context.storage,
            "instance_name": "asset-worker-test",
        },
    )()
    executor = ImageJobExecutor(
        asset_context.settings, asset_context.database, asset_context.storage
    )
    result = await execute_image_job({"runtime": runtime, "image_job_executor": executor}, job_id)
    assert result["status"] == "succeeded"

    async with asset_context.database.session_factory() as session:
        job = await session.get(ImageJob, uuid.UUID(job_id))
        assert job is not None and job.output_asset_id is not None
        output = await session.get(Asset, job.output_asset_id)
        assert output is not None
        assert output.parent_asset_id == uuid.UUID(source["id"])
        assert output.source_job_id == job.id
        assert output.kind == "result"
        output_data = asset_context.storage.objects[output.object_key]
    with Image.open(BytesIO(output_data)) as image:
        red, green, blue = image.convert("RGB").getpixel((0, 0))
        assert red == green == blue


@pytest.mark.asyncio
async def test_job_output_is_hidden_until_success_transaction_commits(
    asset_context: AssetContext,
) -> None:
    owner = await seed_user(asset_context, email="pending-output@example.com")
    raw = raster_bytes()
    async with client_for(asset_context, "pending-output") as client:
        await login(client, owner.email)
        source_response = await upload(client, raw)
        source_id = uuid.UUID(source_response.json()["asset"]["id"])
        quote_response = await client.post(
            "/api/v1/jobs/quote",
            json={
                "operation_code": "color.effect",
                "source_asset_id": str(source_id),
                "parameters": {"mode": "grayscale"},
            },
        )
        job_response = await client.post(
            "/api/v1/jobs",
            headers={"Idempotency-Key": "pending-output-job"},
            json={
                "quote_id": quote_response.json()["quote"]["id"],
                "parameters": {"mode": "grayscale"},
            },
        )
        job_id = uuid.UUID(job_response.json()["job"]["id"])
        pending = await AssetService().store(
            asset_context.database,
            asset_context.storage,
            owner_id=owner.id,
            prepared=prepare_asset(raw, kind="result", max_megapixels=40),
            kind="result",
            operation_code="color.effect",
            retention_days=30,
            parent_asset_id=source_id,
            source_job_id=job_id,
            publish=False,
        )
        assert pending.status == "uploading"
        assert (await client.get(f"/api/v1/assets/{pending.id}")).status_code == 404
        assert not await AssetService().publish_job_output(
            asset_context.database, asset_id=pending.id, job_id=job_id
        )

        async with asset_context.database.session_factory() as session:
            job = await session.get(ImageJob, job_id)
            assert job is not None
            job.status = "succeeded"
            job.output_asset_id = pending.id
            job.completed_at = datetime.now(UTC)
            await session.commit()
        assert await AssetService().publish_job_output(
            asset_context.database, asset_id=pending.id, job_id=job_id
        )
        assert (await client.get(f"/api/v1/assets/{pending.id}")).status_code == 200


@pytest.mark.asyncio
async def test_local_result_migration_preserves_and_verifies_sha256(
    asset_context: AssetContext, tmp_path
) -> None:
    owner = await seed_user(asset_context, email="migration-owner@example.com")
    result_dir = tmp_path / "legacy-results"
    store = ResultStore(result_dir, ttl_hours=24)
    raw = raster_bytes(size=(9, 7))
    filename, path = store.save(raw, "png")
    catalog_path = tmp_path / "legacy.db"
    catalog = StudioCatalog(catalog_path)
    legacy = catalog.add_asset(
        filename=filename,
        operation="upload",
        mime_type="image/png",
        width=9,
        height=7,
        size_bytes=path.stat().st_size,
        data=raw,
    )

    report = await migrate_local_results(
        asset_context.database,
        asset_context.storage,
        owner_id=owner.id,
        catalog_path=catalog_path,
        result_dir=result_dir,
        retention_days=30,
    )
    assert report.discovered == report.migrated == report.verified == 1
    migrated_id = report.id_map[legacy["id"]]
    async with asset_context.database.session_factory() as session:
        migrated = await session.get(Asset, migrated_id)
        assert migrated is not None
        assert migrated.asset_metadata["legacy_asset_id"] == legacy["id"]
        assert migrated.sha256 == hashlib.sha256(raw).hexdigest()
        assert asset_context.storage.objects[migrated.object_key] == raw
