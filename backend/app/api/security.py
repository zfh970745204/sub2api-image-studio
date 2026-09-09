from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import asc, desc, or_, select

from app.api.dependencies import Principal, require_permission
from app.api.errors import ApiError
from app.domain.ids import uuid7
from app.repositories.models import AccessBlock, AuditLog, RateLimitPolicy, SecurityEvent, User
from app.services.security import POLICY_BY_CODE, SecurityService, redact, utcnow

router = APIRouter(prefix="/api/v1/admin", tags=["audit-security"])
AuditReader = Annotated[Principal, Depends(require_permission("audit.read"))]
AuditExporter = Annotated[Principal, Depends(require_permission("audit.export"))]
SecurityReader = Annotated[Principal, Depends(require_permission("security.events.read"))]
SecurityManager = Annotated[Principal, Depends(require_permission("security.policies.manage"))]
HEX_64 = re.compile(r"^[0-9a-f]{64}$")


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResolveEventRequest(StrictRequest):
    status: Literal["resolved", "ignored"] = "resolved"
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str) -> str:
        return value.strip()


class CreateBlockRequest(StrictRequest):
    subject_type: Literal["user", "ip_fingerprint"]
    subject: str = Field(min_length=1, max_length=320)
    reason: str = Field(min_length=3, max_length=500)
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @field_validator("subject", "reason")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_period(self) -> CreateBlockRequest:
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("封禁结束时间必须晚于开始时间")
        return self


class RevokeBlockRequest(StrictRequest):
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str) -> str:
        return value.strip()


class UpdateRateLimitRequest(StrictRequest):
    request_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    window_seconds: int | None = Field(default=None, ge=1, le=86_400)
    enabled: bool | None = None
    reason: str = Field(min_length=3, max_length=500)

    @model_validator(mode="after")
    def require_change(self) -> UpdateRateLimitRequest:
        if not (self.model_fields_set - {"reason"}):
            raise ValueError("至少提供一项策略变更")
        return self


def service(request: Request) -> SecurityService:
    return request.app.state.security_service


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def audit_payload(item: AuditLog) -> dict[str, Any]:
    return {
        "id": item.id,
        "occurred_at": item.occurred_at,
        "actor_user_id": item.actor_user_id,
        "actor_role_snapshot": item.actor_role_snapshot,
        "action": item.action,
        "target_type": item.target_type,
        "target_id": item.target_id,
        "result": item.result,
        "request_id": item.request_id,
        "ip_hash": item.ip_hash,
        "user_agent": item.user_agent,
        "reason": item.reason,
        "changes_redacted": redact(item.changes_redacted),
        "metadata": redact(item.audit_metadata),
    }


def event_payload(item: SecurityEvent) -> dict[str, Any]:
    return {
        "id": item.id,
        "event_type": item.event_type,
        "severity": item.severity,
        "user_id": item.user_id,
        "ip_hash": item.ip_hash,
        "request_id": item.request_id,
        "status": item.status,
        "details_redacted": redact(item.details_redacted),
        "resolved_by": item.resolved_by,
        "resolved_at": item.resolved_at,
        "created_at": item.created_at,
    }


def block_payload(item: AccessBlock) -> dict[str, Any]:
    now = utcnow()
    starts_at = as_utc(item.starts_at)
    ends_at = as_utc(item.ends_at) if item.ends_at is not None else None
    active = starts_at <= now and (ends_at is None or ends_at > now)
    return {
        "id": item.id,
        "subject_type": item.subject_type,
        "subject_hash": item.subject_hash,
        "subject_label": f"{item.subject_type}:{item.subject_hash[:10]}",
        "reason": item.reason,
        "starts_at": item.starts_at,
        "ends_at": item.ends_at,
        "active": active,
        "created_by": item.created_by,
        "created_at": item.created_at,
    }


def policy_payload(item: RateLimitPolicy, counters: dict[str, int]) -> dict[str, Any]:
    return {
        "code": item.code,
        "name": item.name,
        "scope": item.scope,
        "request_limit": item.request_limit,
        "window_seconds": item.window_seconds,
        "enabled": item.enabled,
        "current_local_count": counters.get(item.code, 0),
        "updated_by": item.updated_by,
        "updated_at": item.updated_at,
    }


@router.get("/audit-logs")
async def list_audit_logs(
    request: Request,
    _principal: AuditReader,
    action: str | None = Query(default=None, max_length=200),
    target_type: str | None = Query(default=None, max_length=100),
    result: Literal["success", "denied", "failed"] | None = None,
    actor_user_id: uuid.UUID | None = None,
    request_id: str | None = Query(default=None, max_length=128),
    cursor: uuid.UUID | None = None,
    order: Literal["asc", "desc"] = "desc",
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    statement = select(AuditLog)
    if action:
        statement = statement.where(AuditLog.action == action)
    if target_type:
        statement = statement.where(AuditLog.target_type == target_type)
    if result:
        statement = statement.where(AuditLog.result == result)
    if actor_user_id:
        statement = statement.where(AuditLog.actor_user_id == actor_user_id)
    if request_id:
        statement = statement.where(AuditLog.request_id == request_id)
    if cursor:
        statement = statement.where(
            AuditLog.id > cursor if order == "asc" else AuditLog.id < cursor
        )
    ordering = asc if order == "asc" else desc
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        rows = list(
            (
                await session.scalars(statement.order_by(ordering(AuditLog.id)).limit(limit + 1))
            ).all()
        )
    items = rows[:limit]
    return {
        "items": [audit_payload(item) for item in items],
        "next_cursor": str(items[-1].id) if len(rows) > limit and items else None,
    }


@router.get("/audit-logs/{audit_id}")
async def get_audit_log(
    audit_id: uuid.UUID, request: Request, _principal: AuditReader
) -> dict[str, Any]:
    async with request.app.state.runtime_services.database.session_factory() as session:
        item = await session.get(AuditLog, audit_id)
    if item is None:
        raise ApiError(404, "AUDIT_LOG_NOT_FOUND", "审计日志不存在")
    return {"audit_log": audit_payload(item)}


@router.post("/audit-logs/export")
async def export_audit_logs(request: Request, principal: AuditExporter) -> StreamingResponse:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(AuditLog).order_by(AuditLog.occurred_at.desc()).limit(10_000)
                )
            ).all()
        )
        service(request).record_audit(
            session,
            action="audit.exported",
            target_type="audit_log",
            target_id=None,
            actor_user_id=principal.user_id,
            request_id=request.state.request_id,
            details={"row_count": len(rows)},
        )
        await session.commit()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "occurred_at",
            "actor_user_id",
            "actor_roles",
            "action",
            "target_type",
            "target_id",
            "result",
            "request_id",
            "ip_fingerprint",
            "reason",
        ]
    )
    for item in rows:
        writer.writerow(
            [
                item.id,
                item.occurred_at.isoformat(),
                item.actor_user_id or "",
                ",".join(item.actor_role_snapshot),
                item.action,
                item.target_type,
                item.target_id or "",
                item.result,
                item.request_id,
                f"{item.ip_hash[:12]}...",
                item.reason or "",
            ]
        )
    filename = f"audit-{datetime.now(UTC):%Y%m%d-%H%M%S}.csv"
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/security/events")
async def list_security_events(
    request: Request,
    _principal: SecurityReader,
    status_filter: Literal["open", "investigating", "resolved", "ignored"] | None = Query(
        default=None, alias="status"
    ),
    severity: Literal["low", "medium", "high", "critical"] | None = None,
    cursor: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    statement = select(SecurityEvent)
    if status_filter:
        statement = statement.where(SecurityEvent.status == status_filter)
    if severity:
        statement = statement.where(SecurityEvent.severity == severity)
    if cursor:
        statement = statement.where(SecurityEvent.id < cursor)
    async with request.app.state.runtime_services.database.session_factory() as session:
        rows = list(
            (
                await session.scalars(statement.order_by(SecurityEvent.id.desc()).limit(limit + 1))
            ).all()
        )
    items = rows[:limit]
    return {
        "items": [event_payload(item) for item in items],
        "next_cursor": str(items[-1].id) if len(rows) > limit and items else None,
    }


@router.post("/security/events/{event_id}/resolve")
async def resolve_security_event(
    event_id: uuid.UUID,
    payload: ResolveEventRequest,
    request: Request,
    principal: SecurityManager,
) -> dict[str, Any]:
    async with request.app.state.runtime_services.database.session_factory() as session:
        item = await session.get(SecurityEvent, event_id, with_for_update=True)
        if item is None:
            raise ApiError(404, "SECURITY_EVENT_NOT_FOUND", "安全事件不存在")
        if item.status in {"resolved", "ignored"}:
            raise ApiError(409, "SECURITY_EVENT_CLOSED", "安全事件已完成处置")
        item.status = payload.status
        item.resolved_by = principal.user_id
        item.resolved_at = utcnow()
        item.details_redacted = {
            **item.details_redacted,
            "resolution_reason": payload.reason,
        }
        service(request).record_audit(
            session,
            action=f"security_event.{payload.status}",
            target_type="security_event",
            target_id=item.id,
            actor_user_id=principal.user_id,
            request_id=request.state.request_id,
            details={"reason": payload.reason},
        )
        await session.commit()
        await session.refresh(item)
    return {"security_event": event_payload(item)}


@router.get("/security/blocks")
async def list_access_blocks(
    request: Request,
    _principal: SecurityReader,
    active: bool | None = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    now = utcnow()
    statement = select(AccessBlock)
    if active is True:
        statement = statement.where(
            AccessBlock.starts_at <= now,
            or_(AccessBlock.ends_at.is_(None), AccessBlock.ends_at > now),
        )
    elif active is False:
        statement = statement.where(AccessBlock.ends_at <= now)
    async with request.app.state.runtime_services.database.session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    statement.order_by(AccessBlock.created_at.desc()).limit(limit)
                )
            ).all()
        )
    return {"items": [block_payload(item) for item in rows]}


@router.post("/security/blocks", status_code=status.HTTP_201_CREATED)
async def create_access_block(
    payload: CreateBlockRequest, request: Request, principal: SecurityManager
) -> dict[str, Any]:
    security = service(request)
    if payload.subject_type == "user":
        try:
            subject_user_id = uuid.UUID(payload.subject)
        except ValueError as exc:
            raise ApiError(422, "INVALID_BLOCK_SUBJECT", "用户 ID 无效") from exc
    else:
        subject_user_id = None
        if not HEX_64.fullmatch(payload.subject.casefold()):
            raise ApiError(422, "INVALID_BLOCK_SUBJECT", "IP 指纹必须为 64 位十六进制哈希")
    now = utcnow()
    starts_at = payload.starts_at or now
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=UTC)
    ends_at = payload.ends_at
    if ends_at is not None and ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=UTC)
    subject_hash = security.subject_hash(payload.subject_type, payload.subject)
    async with request.app.state.runtime_services.database.session_factory() as session:
        if subject_user_id is not None:
            user = await session.get(User, subject_user_id)
            if user is None or user.deleted_at is not None:
                raise ApiError(404, "USER_NOT_FOUND", "用户不存在")
        existing = (
            await session.scalars(
                select(AccessBlock).where(
                    AccessBlock.subject_type == payload.subject_type,
                    AccessBlock.subject_hash == subject_hash,
                    AccessBlock.starts_at <= (ends_at or starts_at),
                    or_(AccessBlock.ends_at.is_(None), AccessBlock.ends_at > starts_at),
                )
            )
        ).first()
        if existing is not None:
            raise ApiError(409, "ACCESS_BLOCK_OVERLAP", "该主体已有重叠的封禁策略")
        item = AccessBlock(
            id=uuid7(),
            subject_type=payload.subject_type,
            subject_hash=subject_hash,
            reason=payload.reason,
            starts_at=starts_at,
            ends_at=ends_at,
            created_by=principal.user_id,
            created_at=now,
        )
        session.add(item)
        security.record_audit(
            session,
            action="access_block.created",
            target_type="access_block",
            target_id=item.id,
            actor_user_id=principal.user_id,
            request_id=request.state.request_id,
            details={"subject_type": item.subject_type, "reason": item.reason},
        )
        await session.commit()
        await session.refresh(item)
    return {"block": block_payload(item)}


@router.post("/security/blocks/{block_id}/revoke")
async def revoke_access_block(
    block_id: uuid.UUID,
    payload: RevokeBlockRequest,
    request: Request,
    principal: SecurityManager,
) -> dict[str, Any]:
    now = utcnow()
    async with request.app.state.runtime_services.database.session_factory() as session:
        item = await session.get(AccessBlock, block_id, with_for_update=True)
        if item is None:
            raise ApiError(404, "ACCESS_BLOCK_NOT_FOUND", "封禁策略不存在")
        if item.ends_at is not None and as_utc(item.ends_at) <= now:
            raise ApiError(409, "ACCESS_BLOCK_INACTIVE", "封禁策略已失效")
        item.ends_at = now
        service(request).record_audit(
            session,
            action="access_block.revoked",
            target_type="access_block",
            target_id=item.id,
            actor_user_id=principal.user_id,
            request_id=request.state.request_id,
            details={"reason": payload.reason},
        )
        await session.commit()
        await session.refresh(item)
    return {"block": block_payload(item)}


@router.get("/security/rate-limits")
async def list_rate_limits(request: Request, _principal: SecurityReader) -> dict[str, Any]:
    security = service(request)
    async with request.app.state.runtime_services.database.session_factory() as session:
        rows = await security.ensure_default_policies(session)
        await session.commit()
    counters = security.counter_snapshot()
    return {"items": [policy_payload(item, counters) for item in rows]}


@router.patch("/security/rate-limits/{policy_code}")
async def update_rate_limit(
    policy_code: str,
    payload: UpdateRateLimitRequest,
    request: Request,
    principal: SecurityManager,
) -> dict[str, Any]:
    if policy_code not in POLICY_BY_CODE:
        raise ApiError(404, "RATE_LIMIT_POLICY_NOT_FOUND", "限流策略不存在")
    security = service(request)
    async with request.app.state.runtime_services.database.session_factory() as session:
        await security.ensure_default_policies(session)
        item = await session.get(RateLimitPolicy, policy_code, with_for_update=True)
        assert item is not None
        before = {
            "request_limit": item.request_limit,
            "window_seconds": item.window_seconds,
            "enabled": item.enabled,
        }
        for field in ("request_limit", "window_seconds", "enabled"):
            value = getattr(payload, field)
            if value is not None:
                setattr(item, field, value)
        item.updated_by = principal.user_id
        item.updated_at = utcnow()
        security.record_audit(
            session,
            action="rate_limit_policy.updated",
            target_type="rate_limit_policy",
            target_id=None,
            actor_user_id=principal.user_id,
            request_id=request.state.request_id,
            details={
                "policy": policy_code,
                "reason": payload.reason,
                "changes": {"before": before, "after": policy_payload(item, {})},
            },
        )
        await session.commit()
        await session.refresh(item)
    return {"policy": policy_payload(item, security.counter_snapshot())}
