from __future__ import annotations

import logging
from typing import Annotated, Any

from arq.connections import RedisSettings, create_pool
from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from app.api.dependencies import Principal, require_permission
from app.api.errors import ApiError
from app.domain.ids import uuid7
from app.repositories.models import ConfigGroup, ConfigTestRun
from app.services.configuration import ConfigConnectionTester, ConfigService

router = APIRouter(prefix="/api/v1/admin/config", tags=["admin-config"])
logger = logging.getLogger(__name__)

ConfigReader = Annotated[Principal, Depends(require_permission("config.read"))]
ConfigTester = Annotated[Principal, Depends(require_permission("config.test"))]
ConfigManager = Annotated[Principal, Depends(require_permission("config.manage"))]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftRequest(StrictRequest):
    values: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    change_reason: str = Field(min_length=1, max_length=500)
    confirm_sensitive_change: bool = False

    @field_validator("change_reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("必须填写变更原因")
        return normalized


class PatchDraftRequest(StrictRequest):
    values: dict[str, Any] | None = None
    secrets: dict[str, str] | None = None
    change_reason: str | None = Field(default=None, min_length=1, max_length=500)
    confirm_sensitive_change: bool = False

    @field_validator("change_reason")
    @classmethod
    def strip_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("变更原因不能为空")
        return normalized

    @model_validator(mode="after")
    def require_change(self) -> PatchDraftRequest:
        if self.values is None and self.secrets is None and self.change_reason is None:
            raise ValueError("至少提供一项变更")
        return self


class SaveConfigRequest(StrictRequest):
    values: dict[str, Any]
    secrets: dict[str, str] = Field(default_factory=dict)
    base_version: int | None = Field(ge=1)
    change_reason: str = Field(default="管理员保存配置", min_length=1, max_length=500)


class ProfileTestRequest(StrictRequest):
    profile_id: str = Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_-]{0,31}$")


class ReasonRequest(StrictRequest):
    reason: str = Field(min_length=1, max_length=500)
    confirm_sensitive_change: bool = False

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("必须填写操作原因")
        return normalized


def _runtime(request: Request):
    return request.app.state.runtime_services


def _service(request: Request) -> ConfigService:
    return _runtime(request).config_service


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or uuid7().hex


async def _broadcast(request: Request, group: str, version: int) -> None:
    await _runtime(request).broadcast_config_invalidation(group, version)


async def _group_payload(request: Request, group: ConfigGroup) -> dict[str, Any]:
    service = _service(request)
    async with _runtime(request).database.session_factory() as session:
        versions = await service.versions(session, group.id)
        active = next((item for item in versions if item.version == group.active_version), None)
        draft = next((item for item in versions if item.status == "draft"), None)
        return {
            "id": group.id,
            "code": group.code,
            "name": group.name,
            "defaults": service.definition(group.code).model().model_dump(mode="json"),
            "active_version": group.active_version,
            "updated_at": group.updated_at,
            "active": await service.masked_version(session, group.code, active) if active else None,
            "latest_draft": (
                await service.masked_version(session, group.code, draft) if draft else None
            ),
        }


def _test_payload(run: ConfigTestRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "config_version_id": run.config_version_id,
        "status": run.status,
        "result_code": run.result_code,
        "result_message_redacted": run.result_message_redacted,
        "latency_ms": run.latency_ms,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
    }


@router.get("")
async def list_config_groups(request: Request, _principal: ConfigReader) -> dict[str, Any]:
    service = _service(request)
    async with _runtime(request).database.session_factory() as session:
        await service.ensure_groups(session)
        await session.commit()
        groups = list((await session.scalars(select(ConfigGroup).order_by(ConfigGroup.code))).all())
    return {"items": [await _group_payload(request, group) for group in groups]}


@router.get("/{group_code}")
async def get_config_group(
    request: Request, group_code: str, _principal: ConfigReader
) -> dict[str, Any]:
    async with _runtime(request).database.session_factory() as session:
        group = await _service(request).group(session, group_code)
        await session.commit()
    return {"group": await _group_payload(request, group)}


@router.post("/{group_code}/drafts", status_code=status.HTTP_201_CREATED)
async def create_config_draft(
    request: Request,
    group_code: str,
    payload: DraftRequest,
    principal: ConfigManager,
) -> dict[str, Any]:
    service = _service(request)
    async with _runtime(request).database.session_factory() as session:
        version = await service.create_draft(
            session,
            code=group_code,
            actor_user_id=principal.user_id,
            values=payload.values,
            secrets=payload.secrets,
            change_reason=payload.change_reason,
            confirmed=payload.confirm_sensitive_change,
            request_id=_request_id(request),
        )
        await session.commit()
        body = await service.masked_version(session, group_code, version)
    return {"version": body}


@router.put("/{group_code}")
async def save_config_group(
    request: Request,
    group_code: str,
    payload: SaveConfigRequest,
    principal: ConfigManager,
) -> dict[str, Any]:
    service = _service(request)
    async with _runtime(request).database.session_factory() as session:
        group = await service.group(session, group_code, lock=True)
        if group.active_version != payload.base_version:
            raise ApiError(409, "CONFIG_VERSION_CONFLICT", "配置已被其他管理员修改，请刷新后重试")
        version = await service.create_draft(
            session,
            code=group_code,
            actor_user_id=principal.user_id,
            values=payload.values,
            secrets=payload.secrets,
            change_reason=payload.change_reason.strip() or "管理员保存配置",
            confirmed=True,
            request_id=_request_id(request),
        )
        version = await service.publish(
            session,
            code=group_code,
            version_number=version.version,
            actor_user_id=principal.user_id,
            request_id=_request_id(request),
            require_connection_test=False,
        )
        await session.commit()
        body = await service.masked_version(session, group_code, version)
    await _broadcast(request, group_code, version.version)
    return {"version": body}


@router.post("/{group_code}/test")
async def test_active_config(
    request: Request, group_code: str, principal: ConfigTester
) -> dict[str, Any]:
    service = _service(request)
    async with _runtime(request).database.session_factory() as session:
        group = await service.group(session, group_code)
        if group.active_version is None:
            raise ApiError(409, "CONFIG_NOT_SAVED", "请先保存配置，再测试连接")
        resolved = await service.resolved(session, group_code)
        tester = getattr(request.app.state, "config_connection_tester", None)
        tester = tester or ConfigConnectionTester(request.app.state.settings)
        outcome = await tester.test(resolved)
        # Connection tests report health; they never gate saving configuration.
    return {
        "status": "succeeded" if outcome.succeeded else "failed",
        "message": outcome.message,
        "latency_ms": outcome.latency_ms,
    }


@router.post("/{group_code}/test-profile")
async def test_active_sub2api_profile(
    request: Request,
    group_code: str,
    payload: ProfileTestRequest,
    _principal: ConfigTester,
) -> dict[str, Any]:
    if group_code != "sub2api":
        raise ApiError(404, "CONFIG_PROFILE_NOT_FOUND", "只有 Sub2API 支持线路测试")
    service = _service(request)
    async with _runtime(request).database.session_factory() as session:
        group = await service.group(session, group_code)
        if group.active_version is None:
            raise ApiError(409, "CONFIG_NOT_SAVED", "请先保存配置，再测试线路")
        resolved = await service.resolved(session, group_code)
    tester = getattr(request.app.state, "config_connection_tester", None)
    if tester is None or not hasattr(tester, "test_profile"):
        tester = ConfigConnectionTester(request.app.state.settings)
    outcome = await tester.test_profile(resolved, payload.profile_id)
    return {
        "profile_id": payload.profile_id,
        "status": "succeeded" if outcome.succeeded else "failed",
        "message": outcome.message,
        "latency_ms": outcome.latency_ms,
    }


@router.patch("/{group_code}/drafts/{version_number}")
async def update_config_draft(
    request: Request,
    group_code: str,
    version_number: int,
    payload: PatchDraftRequest,
    principal: ConfigManager,
) -> dict[str, Any]:
    service = _service(request)
    async with _runtime(request).database.session_factory() as session:
        version = await service.update_draft(
            session,
            code=group_code,
            version_number=version_number,
            actor_user_id=principal.user_id,
            values=payload.values,
            secrets=payload.secrets,
            change_reason=payload.change_reason,
            confirmed=payload.confirm_sensitive_change,
            request_id=_request_id(request),
        )
        await session.commit()
        body = await service.masked_version(session, group_code, version)
    return {"version": body}


@router.post(
    "/{group_code}/drafts/{version_number}/test",
    status_code=status.HTTP_202_ACCEPTED,
)
async def test_config_draft(
    request: Request,
    group_code: str,
    version_number: int,
    principal: ConfigTester,
) -> dict[str, Any]:
    service = _service(request)
    async with _runtime(request).database.session_factory() as session:
        group, version, run, resolved = await service.start_test(
            session,
            code=group_code,
            version_number=version_number,
            actor_user_id=principal.user_id,
        )
        await session.commit()
        tester = getattr(request.app.state, "config_connection_tester", None)
        if tester is not None:
            outcome = await tester.test(resolved)
            service.finish_test(
                session,
                group=group,
                version=version,
                run=run,
                outcome=outcome,
                actor_user_id=principal.user_id,
                request_id=_request_id(request),
            )
            await session.commit()
        else:
            try:
                enqueuer = getattr(request.app.state, "config_test_enqueuer", None)
                if enqueuer is not None:
                    queued = bool(await enqueuer(run.id))
                else:
                    pool = await create_pool(
                        RedisSettings.from_dsn(request.app.state.settings.redis_url)
                    )
                    try:
                        job = await pool.enqueue_job(
                            "execute_config_test",
                            str(run.id),
                            _job_id=f"config-test:{run.id}",
                            _queue_name=request.app.state.settings.worker_queue_name,
                        )
                        queued = job is not None
                    finally:
                        await pool.aclose()
            except Exception:
                logger.exception(
                    "config connection test enqueue failed",
                    extra={"operation": "config_test_enqueue", "status": "failed"},
                )
                queued = False
            if not queued:
                service.finish_test(
                    session,
                    group=group,
                    version=version,
                    run=run,
                    outcome=ConfigConnectionTester.queue_failed_outcome(),
                    actor_user_id=principal.user_id,
                    request_id=_request_id(request),
                )
                await session.commit()
    return {"test_run": _test_payload(run)}


@router.post("/{group_code}/drafts/{version_number}/publish")
async def publish_config_draft(
    request: Request,
    group_code: str,
    version_number: int,
    principal: ConfigManager,
) -> dict[str, Any]:
    service = _service(request)
    async with _runtime(request).database.session_factory() as session:
        version = await service.publish(
            session,
            code=group_code,
            version_number=version_number,
            actor_user_id=principal.user_id,
            request_id=_request_id(request),
        )
        await session.commit()
        body = await service.masked_version(session, group_code, version)
    await _broadcast(request, group_code, version.version)
    return {"version": body}


@router.post("/{group_code}/versions/{version_number}/rollback")
async def rollback_config_version(
    request: Request,
    group_code: str,
    version_number: int,
    payload: ReasonRequest,
    principal: ConfigManager,
) -> dict[str, Any]:
    service = _service(request)
    async with _runtime(request).database.session_factory() as session:
        version = await service.rollback(
            session,
            code=group_code,
            target_version=version_number,
            actor_user_id=principal.user_id,
            reason=payload.reason,
            confirmed=payload.confirm_sensitive_change,
            request_id=_request_id(request),
        )
        await session.commit()
        body = await service.masked_version(session, group_code, version)
    await _broadcast(request, group_code, version.version)
    return {"version": body}


@router.post("/{group_code}/secrets/{key_name}/clear")
async def clear_config_secret(
    request: Request,
    group_code: str,
    key_name: str,
    payload: ReasonRequest,
    principal: ConfigManager,
) -> dict[str, Any]:
    service = _service(request)
    async with _runtime(request).database.session_factory() as session:
        version = await service.clear_secret(
            session,
            code=group_code,
            key_name=key_name,
            actor_user_id=principal.user_id,
            reason=payload.reason,
            confirmed=payload.confirm_sensitive_change,
            request_id=_request_id(request),
        )
        await session.commit()
        body = await service.masked_version(session, group_code, version)
    await _broadcast(request, group_code, version.version)
    return {"version": body}


@router.get("/{group_code}/history")
async def config_history(
    request: Request, group_code: str, _principal: ConfigReader
) -> dict[str, Any]:
    service = _service(request)
    async with _runtime(request).database.session_factory() as session:
        group = await service.group(session, group_code)
        versions = await service.versions(session, group.id)
        items = [await service.masked_version(session, group_code, item) for item in versions]
    return {"items": items}
