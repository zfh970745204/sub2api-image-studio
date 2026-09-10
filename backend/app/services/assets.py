from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Literal

from sqlalchemy import and_, asc, desc, func, or_, select

from app.api.errors import ApiError
from app.domain.assets import ASSET_KINDS, OBJECT_KIND_NAMES
from app.domain.ids import uuid7
from app.object_storage import ObjectStorage, ObjectStorageError, StoredObject
from app.repositories.models import (
    Asset,
    AssetAccessLog,
    ImageJob,
    ObjectDeletionQueue,
    OutboxEvent,
)
from app.services.asset_files import PreparedAsset, make_thumbnail
from app.services.security import SecurityService


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class OrphanScan:
    orphan_objects: tuple[StoredObject, ...]
    missing_assets: tuple[Asset, ...]


class AssetService:
    @staticmethod
    def thumbnail_key(object_key: str) -> str:
        return f"{object_key.rsplit('/', 1)[0]}/preview-v1.webp"

    async def ensure_thumbnail(self, session, storage, *, asset_id, owner_id) -> str:
        asset = await self.require_usable(session, asset_id, owner_id=owner_id)
        if asset.extension == "svg":
            raise ApiError(404, "THUMBNAIL_UNAVAILABLE", "矢量素材使用文件图标预览")
        key = self.thumbnail_key(asset.object_key)
        if not asset.asset_metadata.get("thumbnail_ready"):
            # A row lock prevents concurrent first-view backfills racing deletion.
            asset = await session.get(Asset, asset_id, with_for_update=True, populate_existing=True)
            await self.require_usable(session, asset_id, owner_id=owner_id)
            if not asset.asset_metadata.get("thumbnail_ready"):
                raw = await storage.get_object(asset.object_key)
                thumbnail = await asyncio.to_thread(make_thumbnail, raw)
                await storage.put_object(
                    key,
                    thumbnail,
                    content_type="image/webp",
                    metadata={"asset-id": str(asset.id), "purpose": "thumbnail"},
                )
                asset.asset_metadata = {**asset.asset_metadata, "thumbnail_ready": True}
                await session.commit()
        return key

    @staticmethod
    def clean_filename(value: str | None) -> str | None:
        if not value:
            return None
        name = PurePosixPath(value.replace("\\", "/")).name
        name = "".join(character for character in name if ord(character) >= 32).strip()
        return name[:255] or None

    @staticmethod
    def object_key(
        *,
        owner_id: uuid.UUID,
        asset_id: uuid.UUID,
        kind: str,
        extension: str,
        created_at: datetime,
    ) -> str:
        object_name = OBJECT_KIND_NAMES[kind]
        return (
            f"users/{owner_id}/{created_at:%Y}/{created_at:%m}/{asset_id}/{object_name}.{extension}"
        )

    async def store(
        self,
        database,
        storage: ObjectStorage,
        *,
        owner_id: uuid.UUID,
        prepared: PreparedAsset,
        kind: str,
        operation_code: str,
        retention_days: int,
        original_filename: str | None = None,
        parent_asset_id: uuid.UUID | None = None,
        source_job_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
        publish: bool = True,
        request_id: str = "asset-store",
        now: datetime | None = None,
    ) -> Asset:
        if kind not in ASSET_KINDS:
            raise ApiError(422, "INVALID_ASSET_KIND", "无效的素材类型")
        if not storage.bucket:
            raise ApiError(503, "OBJECT_STORAGE_NOT_CONFIGURED", "R2 对象存储尚未配置")
        current_time = now or utcnow()
        asset_id = uuid7()
        async with database.session_factory() as session:
            root_asset_id = asset_id
            if parent_asset_id is not None:
                parent = await self.require_usable(session, parent_asset_id, owner_id=owner_id)
                root_asset_id = parent.root_asset_id
            object_key = self.object_key(
                owner_id=owner_id,
                asset_id=asset_id,
                kind=kind,
                extension=prepared.extension,
                created_at=current_time,
            )
            complete_metadata = dict(metadata or {})
            complete_metadata["retention_days_snapshot"] = retention_days
            asset = Asset(
                id=asset_id,
                owner_id=owner_id,
                root_asset_id=root_asset_id,
                parent_asset_id=parent_asset_id,
                source_job_id=source_job_id,
                kind=kind,
                operation_code=operation_code[:64],
                storage_provider="r2",
                bucket=storage.bucket,
                object_key=object_key,
                original_filename=self.clean_filename(original_filename),
                mime_type=prepared.mime_type,
                extension=prepared.extension,
                size_bytes=len(prepared.data),
                sha256=prepared.sha256,
                width=prepared.width,
                height=prepared.height,
                has_alpha=prepared.has_alpha,
                status="uploading",
                asset_metadata=complete_metadata,
                retention_until=current_time + timedelta(days=retention_days),
                deleted_at=None,
                created_at=current_time,
                updated_at=current_time,
            )
            session.add(asset)
            await session.commit()

        try:
            await storage.put_object(
                object_key,
                prepared.data,
                content_type=prepared.mime_type,
                metadata={
                    "asset-id": str(asset_id),
                    "owner-id": str(owner_id),
                    "sha256": prepared.sha256,
                },
            )
        except Exception:
            async with database.session_factory() as session:
                failed = await session.get(Asset, asset_id, with_for_update=True)
                if failed is not None and failed.status == "uploading":
                    failed.status = "deleted"
                    failed.deleted_at = current_time
                    failed.updated_at = current_time
                    failed.asset_metadata = {
                        **failed.asset_metadata,
                        "upload_failed": True,
                    }
                    await self._queue_deletion(session, failed, execute_after=current_time)
                    await session.commit()
            raise

        thumbnail_ready = False
        if prepared.extension != "svg" and kind != "thumbnail":
            try:
                thumbnail = await asyncio.to_thread(make_thumbnail, prepared.data)
                await storage.put_object(
                    self.thumbnail_key(object_key),
                    thumbnail,
                    content_type="image/webp",
                    metadata={"asset-id": str(asset_id), "purpose": "thumbnail"},
                )
                thumbnail_ready = True
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "thumbnail deferred", extra={"asset_id": str(asset_id)}
                )

        async with database.session_factory() as session:
            stored = await session.get(Asset, asset_id, with_for_update=True)
            if stored is None:
                raise RuntimeError("asset record disappeared after object upload")
            stored.status = "ready" if publish else "uploading"
            stored.asset_metadata = {
                **stored.asset_metadata,
                "object_uploaded": True,
                "pending_job_completion": not publish,
                "thumbnail_ready": thumbnail_ready,
            }
            stored.updated_at = current_time
            duplicate_count = await session.scalar(
                select(func.count(Asset.id)).where(
                    Asset.owner_id == owner_id,
                    Asset.sha256 == prepared.sha256,
                    Asset.created_at >= current_time - timedelta(minutes=10),
                )
            )
            if int(duplicate_count or 0) == 3:
                SecurityService.record_event(
                    session,
                    event_type="duplicate_upload_burst",
                    severity="medium",
                    user_id=owner_id,
                    ip_hash=None,
                    request_id=request_id,
                    details={"window_minutes": 10, "duplicate_count": 3},
                )
            session.add(
                OutboxEvent(
                    id=uuid7(),
                    topic="assets.audit",
                    aggregate_type="asset",
                    aggregate_id=stored.id,
                    payload={
                        "action": "asset.uploaded"
                        if operation_code == "upload"
                        else "asset.created",
                        "actor_user_id": str(owner_id),
                        "subject_user_id": str(owner_id),
                        "request_id": request_id,
                        "details": {
                            "kind": kind,
                            "size_bytes": stored.size_bytes,
                            "sha256": stored.sha256,
                        },
                    },
                    status="pending",
                    attempts=0,
                    available_at=current_time,
                    version=1,
                )
            )
            await session.commit()
            await session.refresh(stored)
            return stored

    async def publish_job_output(
        self,
        database,
        *,
        asset_id: uuid.UUID,
        job_id: uuid.UUID,
        now: datetime | None = None,
    ) -> bool:
        current_time = now or utcnow()
        async with database.session_factory() as session:
            asset = await session.get(Asset, asset_id, with_for_update=True)
            if asset is None or asset.source_job_id != job_id:
                return False
            job = await session.get(ImageJob, job_id)
            if job is None or job.status != "succeeded" or job.output_asset_id != asset.id:
                return False
            if asset.status == "ready":
                return True
            if asset.status != "uploading" or not asset.asset_metadata.get("object_uploaded"):
                return False
            asset.status = "ready"
            asset.updated_at = current_time
            asset.asset_metadata = {
                **asset.asset_metadata,
                "pending_job_completion": False,
            }
            await session.commit()
            return True

    async def require_usable(
        self,
        session,
        asset_id: uuid.UUID,
        *,
        owner_id: uuid.UUID | None,
    ) -> Asset:
        statement = select(Asset).where(Asset.id == asset_id, Asset.status == "ready")
        if owner_id is not None:
            statement = statement.where(Asset.owner_id == owner_id)
        asset = (await session.scalars(statement)).one_or_none()
        if asset is None:
            raise ApiError(404, "ASSET_NOT_FOUND", "素材不存在")
        return asset

    async def get_visible(
        self,
        session,
        asset_id: uuid.UUID,
        *,
        owner_id: uuid.UUID | None,
        include_deleted: bool = False,
    ) -> Asset:
        statement = select(Asset).where(Asset.id == asset_id)
        if owner_id is not None:
            statement = statement.where(Asset.owner_id == owner_id)
        if not include_deleted:
            statement = statement.where(Asset.status == "ready")
        asset = (await session.scalars(statement)).one_or_none()
        if asset is None:
            raise ApiError(404, "ASSET_NOT_FOUND", "素材不存在")
        return asset

    async def soft_delete(
        self,
        session,
        *,
        asset_id: uuid.UUID,
        owner_id: uuid.UUID | None,
        grace_days: int,
        now: datetime | None = None,
        immediate: bool = False,
    ) -> Asset:
        current_time = now or utcnow()
        statement = select(Asset).where(Asset.id == asset_id).with_for_update()
        if owner_id is not None:
            statement = statement.where(Asset.owner_id == owner_id)
        asset = (await session.scalars(statement)).one_or_none()
        if asset is None:
            raise ApiError(404, "ASSET_NOT_FOUND", "素材不存在")
        if asset.status == "deleted":
            if immediate:
                await self._queue_deletion(session, asset, execute_after=current_time)
            return asset
        previous_status = asset.status
        asset.status = "deleted"
        asset.deleted_at = current_time
        asset.updated_at = current_time
        asset.asset_metadata = {
            **asset.asset_metadata,
            "status_before_delete": previous_status,
        }
        await self._queue_deletion(
            session,
            asset,
            execute_after=current_time if immediate else current_time + timedelta(days=grace_days),
        )
        return asset

    async def restore(
        self,
        session,
        storage: ObjectStorage,
        *,
        asset_id: uuid.UUID,
        owner_id: uuid.UUID | None,
        admin: bool = False,
        now: datetime | None = None,
    ) -> Asset:
        current_time = now or utcnow()
        statement = select(Asset).where(Asset.id == asset_id).with_for_update()
        if owner_id is not None:
            statement = statement.where(Asset.owner_id == owner_id)
        asset = (await session.scalars(statement)).one_or_none()
        if asset is None:
            raise ApiError(404, "ASSET_NOT_FOUND", "素材不存在")
        if asset.status != "deleted":
            return asset
        queue = (
            await session.scalars(
                select(ObjectDeletionQueue).where(ObjectDeletionQueue.asset_id == asset.id)
            )
        ).one_or_none()
        if queue is not None and queue.status == "completed":
            raise ApiError(409, "ASSET_ALREADY_PURGED", "素材对象已物理删除，无法恢复")
        stored = await storage.head_object(asset.object_key)
        if stored is None:
            raise ApiError(409, "ASSET_OBJECT_MISSING", "素材对象已不存在，无法恢复")
        previous_status = str(asset.asset_metadata.get("status_before_delete", "ready"))
        if previous_status == "quarantined" and not admin:
            raise ApiError(403, "ASSET_QUARANTINED", "隔离素材只能由管理员恢复")
        asset.status = previous_status if previous_status in {"ready", "quarantined"} else "ready"
        asset.deleted_at = None
        asset.updated_at = current_time
        if queue is not None:
            queue.status = "cancelled"
            queue.last_error = None
        return asset

    async def quarantine(
        self,
        session,
        *,
        asset_id: uuid.UUID,
        quarantined: bool,
        reason: str,
        now: datetime | None = None,
    ) -> Asset:
        current_time = now or utcnow()
        asset = await session.get(Asset, asset_id, with_for_update=True)
        if asset is None:
            raise ApiError(404, "ASSET_NOT_FOUND", "素材不存在")
        if asset.status == "deleted":
            raise ApiError(409, "ASSET_DELETED", "已删除素材不能变更隔离状态")
        asset.status = "quarantined" if quarantined else "ready"
        asset.updated_at = current_time
        asset.asset_metadata = {
            **asset.asset_metadata,
            "quarantine_reason": reason[:500] if quarantined else None,
        }
        return asset

    @staticmethod
    def log_access(
        session,
        *,
        asset_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        action: str,
        ip_hash: str,
        request_id: str = "asset-access",
    ) -> None:
        session.add(
            AssetAccessLog(
                id=uuid7(),
                asset_id=asset_id,
                actor_user_id=actor_user_id,
                action=action,
                ip_hash=ip_hash,
            )
        )
        session.add(
            OutboxEvent(
                id=uuid7(),
                topic="assets.audit",
                aggregate_type="asset",
                aggregate_id=asset_id,
                payload={
                    "action": f"asset.{action}",
                    "actor_user_id": str(actor_user_id),
                    "subject_user_id": str(actor_user_id),
                    "request_id": request_id,
                    "details": {"ip_hash": ip_hash},
                },
                status="pending",
                attempts=0,
                available_at=utcnow(),
                version=1,
            )
        )

    async def process_deletion_queue(
        self,
        database,
        storage: ObjectStorage,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> dict[str, int]:
        current_time = now or utcnow()
        async with database.session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(ObjectDeletionQueue)
                        .where(
                            ObjectDeletionQueue.status.in_({"pending", "failed"}),
                            ObjectDeletionQueue.execute_after <= current_time,
                            ObjectDeletionQueue.attempts < 10,
                        )
                        .order_by(ObjectDeletionQueue.execute_after)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            completed = 0
            failed = 0
            for row in rows:
                try:
                    await storage.delete_object(row.object_key)
                    await storage.delete_object(self.thumbnail_key(row.object_key))
                except ObjectStorageError as exc:
                    row.attempts += 1
                    row.status = "failed"
                    row.last_error = exc.__class__.__name__[:1000]
                    failed += 1
                else:
                    row.attempts += 1
                    row.status = "completed"
                    row.last_error = None
                    completed += 1
            await session.commit()
        return {"checked": len(rows), "completed": completed, "failed": failed}

    async def scan_orphans(self, session, storage: ObjectStorage) -> OrphanScan:
        stored_objects = await storage.list_objects("users/")
        assets = list((await session.scalars(select(Asset))).all())
        database_keys = {
            key
            for asset in assets
            for key in (asset.object_key, self.thumbnail_key(asset.object_key))
        }
        storage_keys = {item.key for item in stored_objects}
        return OrphanScan(
            orphan_objects=tuple(item for item in stored_objects if item.key not in database_keys),
            missing_assets=tuple(
                asset
                for asset in assets
                if asset.status in {"uploading", "ready", "quarantined"}
                and asset.object_key not in storage_keys
            ),
        )

    async def reconcile_orphans(
        self,
        session,
        storage: ObjectStorage,
        *,
        grace_hours: int,
        now: datetime | None = None,
    ) -> dict[str, int]:
        current_time = now or utcnow()
        scan = await self.scan_orphans(session, storage)
        removed = 0
        skipped = 0
        for item in scan.orphan_objects:
            if item.last_modified is None or (
                current_time - as_utc(item.last_modified)
            ) < timedelta(hours=grace_hours):
                skipped += 1
                continue
            await storage.delete_object(item.key)
            removed += 1
        quarantined = 0
        for asset in scan.missing_assets:
            if asset.status == "uploading":
                continue
            if asset.status == "quarantined":
                continue
            asset.status = "quarantined"
            asset.updated_at = current_time
            asset.asset_metadata = {
                **asset.asset_metadata,
                "quarantine_reason": "object_missing_during_reconciliation",
            }
            quarantined += 1
        return {
            "orphan_objects_removed": removed,
            "orphan_objects_skipped": skipped,
            "missing_assets_quarantined": quarantined,
        }

    async def reconcile_stale_uploads(
        self,
        database,
        storage: ObjectStorage,
        *,
        now: datetime | None = None,
        stale_minutes: int = 15,
    ) -> dict[str, int]:
        current_time = now or utcnow()
        async with database.session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(Asset)
                        .where(
                            Asset.status == "uploading",
                            Asset.created_at <= current_time - timedelta(minutes=stale_minutes),
                        )
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            recovered = 0
            abandoned = 0
            for asset in rows:
                stored = await storage.head_object(asset.object_key)
                metadata = stored.metadata or {} if stored is not None else {}
                object_valid = (
                    stored is not None
                    and stored.size == asset.size_bytes
                    and metadata.get("sha256") == asset.sha256
                )
                source_job = (
                    await session.get(ImageJob, asset.source_job_id)
                    if asset.source_job_id is not None
                    else None
                )
                if source_job is not None and source_job.status in {
                    "queued",
                    "running",
                    "retry_wait",
                }:
                    continue
                if object_valid and (
                    asset.source_job_id is None
                    or (
                        source_job is not None
                        and source_job.status == "succeeded"
                        and source_job.output_asset_id == asset.id
                    )
                ):
                    asset.status = "ready"
                    asset.updated_at = current_time
                    asset.asset_metadata = {
                        **asset.asset_metadata,
                        "pending_job_completion": False,
                    }
                    recovered += 1
                else:
                    asset.status = "deleted"
                    asset.deleted_at = current_time
                    asset.updated_at = current_time
                    await self._queue_deletion(session, asset, execute_after=current_time)
                    abandoned += 1
            await session.commit()
        return {"checked": len(rows), "recovered": recovered, "abandoned": abandoned}

    async def expire_assets(
        self,
        database,
        *,
        grace_days: int,
        now: datetime | None = None,
    ) -> int:
        current_time = now or utcnow()
        async with database.session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(Asset)
                        .where(
                            Asset.status.in_({"ready", "quarantined"}),
                            Asset.retention_until.is_not(None),
                            Asset.retention_until <= current_time,
                        )
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for asset in rows:
                await self.soft_delete(
                    session,
                    asset_id=asset.id,
                    owner_id=None,
                    grace_days=grace_days,
                    now=current_time,
                )
            await session.commit()
        return len(rows)

    async def notify_expiring_assets(
        self,
        database,
        *,
        now: datetime | None = None,
        notice_days: int = 7,
    ) -> int:
        current_time = now or utcnow()
        async with database.session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(Asset)
                        .where(
                            Asset.status == "ready",
                            Asset.retention_until > current_time,
                            Asset.retention_until <= current_time + timedelta(days=notice_days),
                        )
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            notified = 0
            for asset in rows:
                if asset.asset_metadata.get("retention_notice_sent_at"):
                    continue
                asset.asset_metadata = {
                    **asset.asset_metadata,
                    "retention_notice_sent_at": current_time.isoformat(),
                }
                session.add(
                    OutboxEvent(
                        id=uuid7(),
                        topic="assets.retention_expiring",
                        aggregate_type="asset",
                        aggregate_id=asset.id,
                        payload={
                            "user_id": str(asset.owner_id),
                            "asset_id": str(asset.id),
                            "retention_until": asset.retention_until.isoformat()
                            if asset.retention_until
                            else None,
                        },
                        status="pending",
                        attempts=0,
                        available_at=current_time,
                        version=1,
                    )
                )
                notified += 1
            await session.commit()
        return notified

    @staticmethod
    async def _queue_deletion(
        session,
        asset: Asset,
        *,
        execute_after: datetime,
    ) -> ObjectDeletionQueue:
        queue = (
            await session.scalars(
                select(ObjectDeletionQueue).where(ObjectDeletionQueue.asset_id == asset.id)
            )
        ).one_or_none()
        if queue is None:
            queue = ObjectDeletionQueue(
                id=uuid7(),
                asset_id=asset.id,
                object_key=asset.object_key,
                execute_after=execute_after,
                status="pending",
                attempts=0,
                last_error=None,
            )
            session.add(queue)
        else:
            queue.object_key = asset.object_key
            queue.execute_after = execute_after
            queue.status = "pending"
            queue.last_error = None
        return queue


def asset_page_statement(
    *,
    owner_id: uuid.UUID | None,
    kind: str | None,
    root_id: uuid.UUID | None,
    status: str | None,
    anchor: Asset | None,
    order: Literal["asc", "desc"] = "desc",
):
    statement = select(Asset)
    if owner_id is not None:
        statement = statement.where(Asset.owner_id == owner_id)
    if kind is not None:
        statement = statement.where(Asset.kind == kind)
    if root_id is not None:
        statement = statement.where(Asset.root_asset_id == root_id)
    if status is not None:
        statement = statement.where(Asset.status == status)
    if anchor is not None:
        if order == "desc":
            statement = statement.where(
                or_(
                    Asset.created_at < anchor.created_at,
                    and_(Asset.created_at == anchor.created_at, Asset.id < anchor.id),
                )
            )
        else:
            statement = statement.where(
                or_(
                    Asset.created_at > anchor.created_at,
                    and_(Asset.created_at == anchor.created_at, Asset.id > anchor.id),
                )
            )
    direction = desc if order == "desc" else asc
    return statement.order_by(direction(Asset.created_at), direction(Asset.id))
