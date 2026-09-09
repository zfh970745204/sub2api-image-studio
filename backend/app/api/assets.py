from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.api.dependencies import Principal, require_permission
from app.api.errors import ApiError
from app.domain.assets import ASSET_KINDS, ASSET_STATUSES
from app.object_storage import ObjectStorageError
from app.repositories.models import Asset, ObjectDeletionQueue
from app.services.asset_files import AssetInputError, prepare_asset
from app.services.assets import AssetService, asset_page_statement
from app.services.configuration import runtime_config_value
from app.services.memberships import EntitlementService

router = APIRouter(tags=["assets"])
service = AssetService()
entitlements = EntitlementService()

AssetReader = Annotated[Principal, Depends(require_permission("assets.read_own"))]
AssetWriter = Annotated[Principal, Depends(require_permission("assets.write_own"))]
AssetDeleter = Annotated[Principal, Depends(require_permission("assets.delete_own"))]
AdminAssetReader = Annotated[Principal, Depends(require_permission("assets.read"))]
AdminAssetManager = Annotated[Principal, Depends(require_permission("assets.manage"))]


class QuarantineRequest(BaseModel):
    quarantined: bool = True
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("必须填写隔离原因")
        return normalized


def asset_payload(asset: Asset) -> dict[str, Any]:
    return {
        "id": asset.id,
        "owner_id": asset.owner_id,
        "root_asset_id": asset.root_asset_id,
        "parent_asset_id": asset.parent_asset_id,
        "source_job_id": asset.source_job_id,
        "kind": asset.kind,
        "operation_code": asset.operation_code,
        "storage_provider": asset.storage_provider,
        "original_filename": asset.original_filename,
        "mime_type": asset.mime_type,
        "extension": asset.extension,
        "size_bytes": asset.size_bytes,
        "sha256": asset.sha256,
        "width": asset.width,
        "height": asset.height,
        "has_alpha": asset.has_alpha,
        "status": asset.status,
        "metadata": asset.asset_metadata,
        "retention_until": asset.retention_until,
        "deleted_at": asset.deleted_at,
        "created_at": asset.created_at,
        "updated_at": asset.updated_at,
    }


def _storage(request: Request):
    storage = getattr(request.app.state, "object_storage", None)
    if storage is None:
        storage = getattr(request.app.state.runtime_services, "object_storage", None)
    if storage is None or not storage.bucket:
        raise ApiError(503, "OBJECT_STORAGE_NOT_CONFIGURED", "R2 对象存储尚未配置")
    return storage


def _ip_hash(request: Request) -> str:
    address = request.client.host if request.client else "unknown"
    return request.app.state.auth_service.fingerprint(address)


def _validate_filters(kind: str | None, status_filter: str | None) -> None:
    if kind is not None and kind not in ASSET_KINDS:
        raise ApiError(422, "INVALID_ASSET_KIND", "无效的素材类型")
    if status_filter is not None and status_filter not in ASSET_STATUSES:
        raise ApiError(422, "INVALID_ASSET_STATUS", "无效的素材状态")


async def _asset_page(
    session,
    *,
    owner_id: uuid.UUID | None,
    kind: str | None,
    root_id: uuid.UUID | None,
    status_filter: str | None,
    cursor: uuid.UUID | None,
    limit: int,
    order: Literal["asc", "desc"] = "desc",
) -> tuple[list[Asset], str | None]:
    _validate_filters(kind, status_filter)
    anchor = None
    if cursor is not None:
        anchor = await session.get(Asset, cursor)
        if anchor is None or (owner_id is not None and anchor.owner_id != owner_id):
            raise ApiError(422, "INVALID_CURSOR", "分页游标无效")
    rows = list(
        (
            await session.scalars(
                asset_page_statement(
                    owner_id=owner_id,
                    kind=kind,
                    root_id=root_id,
                    status=status_filter,
                    anchor=anchor,
                    order=order,
                ).limit(limit + 1)
            )
        ).all()
    )
    items = rows[:limit]
    return items, str(items[-1].id) if len(rows) > limit and items else None


@router.post("/api/v1/assets/upload", status_code=status.HTTP_201_CREATED)
async def upload_asset(
    request: Request,
    principal: AssetWriter,
    image: Annotated[UploadFile, File()],
    kind: Annotated[str, Form()] = "original",
    parent_asset_id: Annotated[uuid.UUID | None, Form()] = None,
) -> dict[str, Any]:
    _validate_filters(kind, None)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        entitlement = await entitlements.current_snapshot(
            session,
            principal.user_id,
            request_id=getattr(request.state, "request_id", "asset-upload"),
        )
        await session.commit()
    global_upload_mb = int(
        await runtime_config_value(
            request.app.state.runtime_services,
            "general",
            "max_upload_mb",
            request.app.state.settings.max_upload_mb,
        )
    )
    global_megapixels = int(
        await runtime_config_value(
            request.app.state.runtime_services,
            "general",
            "max_image_megapixels",
            request.app.state.settings.max_image_megapixels,
        )
    )
    effective_upload_mb = min(entitlement.max_upload_mb, global_upload_mb)
    effective_megapixels = min(entitlement.max_image_megapixels, global_megapixels)
    limit = effective_upload_mb * 1024 * 1024
    raw = await image.read(limit + 1)
    if len(raw) > limit:
        raise ApiError(
            413,
            "ASSET_TOO_LARGE",
            f"文件超过 {effective_upload_mb} MB 上传限制",
        )
    try:
        prepared = await asyncio.to_thread(
            prepare_asset,
            raw,
            kind=kind,
            max_megapixels=effective_megapixels,
        )
        asset = await service.store(
            database,
            _storage(request),
            owner_id=principal.user_id,
            prepared=prepared,
            kind=kind,
            operation_code="upload",
            retention_days=entitlement.retention_days,
            original_filename=image.filename,
            parent_asset_id=parent_asset_id,
            metadata={
                "membership_id": str(entitlement.membership_id),
                "plan_code": entitlement.plan_code,
            },
            request_id=getattr(request.state, "request_id", "asset-upload"),
        )
    except AssetInputError as exc:
        raise ApiError(422, "INVALID_ASSET_FILE", str(exc)) from exc
    except ObjectStorageError as exc:
        raise ApiError(503, "OBJECT_STORAGE_UNAVAILABLE", "对象存储暂时不可用") from exc
    return {"asset": asset_payload(asset)}


@router.get("/api/v1/assets")
async def list_assets(
    request: Request,
    principal: AssetReader,
    cursor: uuid.UUID | None = None,
    kind: str | None = None,
    root_id: uuid.UUID | None = None,
    status_filter: str = Query(default="ready", alias="status"),
    limit: int = Query(default=30, ge=1, le=100),
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        items, next_cursor = await _asset_page(
            session,
            owner_id=principal.user_id,
            kind=kind,
            root_id=root_id,
            status_filter=status_filter,
            cursor=cursor,
            limit=limit,
        )
    return {"items": [asset_payload(item) for item in items], "next_cursor": next_cursor}


@router.get("/api/v1/assets/{asset_id}")
async def get_asset(
    asset_id: uuid.UUID, request: Request, principal: AssetReader
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        asset = await service.get_visible(
            session, asset_id, owner_id=principal.user_id, include_deleted=True
        )
        if asset.status not in {"ready", "deleted"}:
            raise ApiError(404, "ASSET_NOT_FOUND", "素材不存在")
        service.log_access(
            session,
            asset_id=asset.id,
            actor_user_id=principal.user_id,
            action="preview",
            ip_hash=_ip_hash(request),
            request_id=getattr(request.state, "request_id", "asset-preview"),
        )
        await session.commit()
    return {"asset": asset_payload(asset)}


@router.get("/api/v1/assets/{asset_id}/lineage")
async def get_asset_lineage(
    asset_id: uuid.UUID, request: Request, principal: AssetReader
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        asset = await service.get_visible(
            session, asset_id, owner_id=principal.user_id, include_deleted=True
        )
        rows = list(
            (
                await session.scalars(
                    select(Asset)
                    .where(
                        Asset.owner_id == principal.user_id,
                        Asset.root_asset_id == asset.root_asset_id,
                        Asset.status != "quarantined",
                    )
                    .order_by(Asset.created_at, Asset.id)
                )
            ).all()
        )
    return {"items": [asset_payload(item) for item in rows]}


@router.post("/api/v1/assets/{asset_id}/download-url")
async def create_download_url(
    asset_id: uuid.UUID, request: Request, principal: AssetReader
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    settings = request.app.state.settings
    ttl_seconds = int(
        await runtime_config_value(
            request.app.state.runtime_services,
            "general",
            "signed_url_ttl_seconds",
            settings.asset_download_url_ttl_seconds,
        )
    )
    async with database.session_factory() as session:
        asset = await service.require_usable(session, asset_id, owner_id=principal.user_id)
        try:
            url = await _storage(request).presign_get(
                asset.object_key,
                expires_seconds=ttl_seconds,
                download_filename=f"{asset.id}.{asset.extension}",
            )
        except ObjectStorageError as exc:
            raise ApiError(503, "OBJECT_STORAGE_UNAVAILABLE", "对象存储暂时不可用") from exc
        service.log_access(
            session,
            asset_id=asset.id,
            actor_user_id=principal.user_id,
            action="download",
            ip_hash=_ip_hash(request),
            request_id=getattr(request.state, "request_id", "asset-download"),
        )
        await session.commit()
    expires_at = datetime.now(UTC).timestamp() + ttl_seconds
    return {
        "url": url,
        "expires_at": datetime.fromtimestamp(expires_at, UTC),
        "expires_in": ttl_seconds,
    }


@router.delete("/api/v1/assets/{asset_id}")
async def delete_asset(
    asset_id: uuid.UUID, request: Request, principal: AssetDeleter
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        asset = await service.soft_delete(
            session,
            asset_id=asset_id,
            owner_id=principal.user_id,
            grace_days=request.app.state.settings.asset_delete_grace_days,
        )
        await session.commit()
        await session.refresh(asset)
    return {"asset": asset_payload(asset)}


@router.post("/api/v1/assets/{asset_id}/restore")
async def restore_asset(
    asset_id: uuid.UUID, request: Request, principal: AssetDeleter
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    try:
        async with database.session_factory() as session:
            asset = await service.restore(
                session,
                _storage(request),
                asset_id=asset_id,
                owner_id=principal.user_id,
            )
            await session.commit()
            await session.refresh(asset)
    except ObjectStorageError as exc:
        raise ApiError(503, "OBJECT_STORAGE_UNAVAILABLE", "对象存储暂时不可用") from exc
    return {"asset": asset_payload(asset)}


@router.get("/api/v1/admin/assets")
async def list_admin_assets(
    request: Request,
    _principal: AdminAssetReader,
    owner_id: uuid.UUID | None = None,
    cursor: uuid.UUID | None = None,
    kind: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    order: Literal["asc", "desc"] = "desc",
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        items, next_cursor = await _asset_page(
            session,
            owner_id=owner_id,
            kind=kind,
            root_id=None,
            status_filter=status_filter,
            cursor=cursor,
            limit=limit,
            order=order,
        )
    return {"items": [asset_payload(item) for item in items], "next_cursor": next_cursor}


@router.get("/api/v1/admin/assets/{asset_id}")
async def get_admin_asset(
    asset_id: uuid.UUID, request: Request, principal: AdminAssetReader
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        asset = await service.get_visible(session, asset_id, owner_id=None, include_deleted=True)
        service.log_access(
            session,
            asset_id=asset.id,
            actor_user_id=principal.user_id,
            action="admin_preview",
            ip_hash=_ip_hash(request),
            request_id=getattr(request.state, "request_id", "admin-asset-preview"),
        )
        await session.commit()
    return {"asset": asset_payload(asset)}


@router.post("/api/v1/admin/assets/{asset_id}/quarantine")
async def quarantine_asset(
    asset_id: uuid.UUID,
    payload: QuarantineRequest,
    request: Request,
    _principal: AdminAssetManager,
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        asset = await service.quarantine(
            session,
            asset_id=asset_id,
            quarantined=payload.quarantined,
            reason=payload.reason,
        )
        await session.commit()
        await session.refresh(asset)
    return {"asset": asset_payload(asset)}


@router.post("/api/v1/admin/assets/{asset_id}/download-url")
async def create_admin_download_url(
    asset_id: uuid.UUID, request: Request, principal: AdminAssetReader
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    settings = request.app.state.settings
    ttl_seconds = int(
        await runtime_config_value(
            request.app.state.runtime_services,
            "general",
            "signed_url_ttl_seconds",
            settings.asset_download_url_ttl_seconds,
        )
    )
    async with database.session_factory() as session:
        asset = await service.get_visible(session, asset_id, owner_id=None, include_deleted=True)
        if asset.status == "deleted":
            raise ApiError(409, "ASSET_DELETED", "已删除素材不能生成访问地址")
        try:
            url = await _storage(request).presign_get(
                asset.object_key,
                expires_seconds=ttl_seconds,
                download_filename=f"{asset.id}.{asset.extension}",
            )
        except ObjectStorageError as exc:
            raise ApiError(503, "OBJECT_STORAGE_UNAVAILABLE", "对象存储暂时不可用") from exc
        service.log_access(
            session,
            asset_id=asset.id,
            actor_user_id=principal.user_id,
            action="admin_preview",
            ip_hash=_ip_hash(request),
            request_id=getattr(request.state, "request_id", "admin-asset-download"),
        )
        await session.commit()
    return {
        "url": url,
        "expires_at": datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        "expires_in": ttl_seconds,
    }


@router.post("/api/v1/admin/assets/{asset_id}/restore")
async def restore_admin_asset(
    asset_id: uuid.UUID, request: Request, _principal: AdminAssetManager
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    try:
        async with database.session_factory() as session:
            asset = await service.restore(
                session,
                _storage(request),
                asset_id=asset_id,
                owner_id=None,
                admin=True,
            )
            await session.commit()
            await session.refresh(asset)
    except ObjectStorageError as exc:
        raise ApiError(503, "OBJECT_STORAGE_UNAVAILABLE", "对象存储暂时不可用") from exc
    return {"asset": asset_payload(asset)}


@router.delete("/api/v1/admin/assets/{asset_id}")
async def force_delete_admin_asset(
    asset_id: uuid.UUID, request: Request, _principal: AdminAssetManager
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    storage = _storage(request)
    try:
        async with database.session_factory() as session:
            asset = await service.soft_delete(
                session,
                asset_id=asset_id,
                owner_id=None,
                grace_days=request.app.state.settings.asset_delete_grace_days,
                immediate=True,
            )
            await session.flush()
            queue = (
                await session.scalars(
                    select(ObjectDeletionQueue).where(ObjectDeletionQueue.asset_id == asset.id)
                )
            ).one()
            await storage.delete_object(asset.object_key)
            queue.status = "completed"
            queue.attempts += 1
            queue.last_error = None
            await session.commit()
            await session.refresh(asset)
    except ObjectStorageError as exc:
        raise ApiError(503, "OBJECT_STORAGE_UNAVAILABLE", "对象存储暂时不可用") from exc
    return {"asset": asset_payload(asset), "purged": True}


@router.post("/api/v1/admin/storage/orphans/scan")
async def scan_orphans(request: Request, _principal: AdminAssetReader) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    try:
        async with database.session_factory() as session:
            result = await service.scan_orphans(session, _storage(request))
    except ObjectStorageError as exc:
        raise ApiError(503, "OBJECT_STORAGE_UNAVAILABLE", "对象存储暂时不可用") from exc
    return {
        "orphan_objects": [
            {
                "object_key": item.key,
                "size_bytes": item.size,
                "last_modified": item.last_modified,
            }
            for item in result.orphan_objects
        ],
        "missing_assets": [asset_payload(asset) for asset in result.missing_assets],
    }


@router.post("/api/v1/admin/storage/orphans/reconcile")
async def reconcile_orphans(request: Request, _principal: AdminAssetManager) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    try:
        async with database.session_factory() as session:
            result = await service.reconcile_orphans(
                session,
                _storage(request),
                grace_hours=request.app.state.settings.asset_orphan_grace_hours,
            )
            await session.commit()
    except ObjectStorageError as exc:
        raise ApiError(503, "OBJECT_STORAGE_UNAVAILABLE", "对象存储暂时不可用") from exc
    return {"result": result}
