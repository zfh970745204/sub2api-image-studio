from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import math
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, asc, case, desc, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import Principal, get_current_principal, require_permission
from app.api.errors import ApiError
from app.domain.ids import uuid7
from app.repositories.models import (
    AdminActionRequest,
    AdminSavedView,
    Asset,
    ConfigGroup,
    ImageJob,
    MembershipPlan,
    ObjectDeletionQueue,
    OperationCatalog,
    OperationPrice,
    OutboxEvent,
    PointTransaction,
    Role,
    ServiceInstance,
    User,
    UserMembership,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin-console"])

DashboardReader = Annotated[Principal, Depends(require_permission("admin.dashboard.read"))]
AuditReader = Annotated[Principal, Depends(require_permission("audit.read"))]
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]

MODULE_PERMISSIONS = {
    "dashboard": "admin.dashboard.read",
    "users": "users.read",
    "memberships": "memberships.read",
    "points": "points.read",
    "pricing": "pricing.read",
    "jobs": "tasks.read",
    "assets": "assets.read",
    "settings": "config.read",
    "roles": "roles.read",
    "audit": "audit.read",
    "action-requests": "audit.read",
}
EXPORT_MODULES = frozenset(MODULE_PERMISSIONS) - {"dashboard", "settings", "roles"}
ACTION_PERMISSION_PREFIXES = (
    ("user.", "users.manage"),
    ("membership.", "memberships.manage"),
    ("points.", "points.adjust"),
    ("pricing.", "pricing.manage"),
    ("job.", "tasks.manage"),
    ("task.", "tasks.manage"),
    ("asset.", "assets.manage"),
    ("config.", "config.manage"),
    ("role.", "roles.manage"),
)
SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "access_key",
    "authorization",
    "cookie",
    "credential",
    "ciphertext",
    "nonce",
)
RANGE_DAYS = {"24h": 1, "7d": 7, "30d": 30, "90d": 90}


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateActionRequest(StrictRequest):
    action_type: str = Field(min_length=3, max_length=100, pattern=r"^[a-z][a-z0-9_.-]+$")
    target_type: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_-]+$")
    target_id: uuid.UUID
    payload: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=3, max_length=500)
    risk_level: Literal["normal", "high", "critical"] = "normal"
    confirmed: bool = False

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return value.strip()

    @field_validator("payload")
    @classmethod
    def reject_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        _assert_no_secrets(value)
        if len(json.dumps(value, ensure_ascii=False, default=str)) > 20_000:
            raise ValueError("操作参数不能超过 20 KB")
        return value


class ReviewActionRequest(StrictRequest):
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return value.strip()


class SaveViewRequest(StrictRequest):
    module: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    filters: dict[str, Any] = Field(default_factory=dict)
    columns: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("module", "name")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("filters")
    @classmethod
    def reject_filter_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        _assert_no_secrets(value)
        return value

    @field_validator("columns")
    @classmethod
    def normalize_columns(cls, value: list[str]) -> list[str]:
        normalized = [column.strip() for column in value if column.strip()]
        if any(len(column) > 64 for column in normalized):
            raise ValueError("列名不能超过 64 个字符")
        return list(dict.fromkeys(normalized))


def _assert_no_secrets(value: Any, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS):
                raise ValueError(f"{path}.{key} 不允许包含密钥或令牌")
            _assert_no_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_secrets(child, f"{path}[{index}]")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            result[str(key)] = (
                "[REDACTED]"
                if any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS)
                else _redact(child)
            )
        return result
    if isinstance(value, list):
        return [_redact(child) for child in value]
    return value


def _permission_for_action(action_type: str) -> str:
    for prefix, permission in ACTION_PERMISSION_PREFIXES:
        if action_type.startswith(prefix):
            return permission
    raise ApiError(422, "UNSUPPORTED_ADMIN_ACTION", "不支持的管理操作类型")


def _risk_for_action(action_type: str, requested: str) -> str:
    minimum = "normal"
    if action_type.startswith(("config.", "role.")):
        minimum = "critical"
    elif action_type.startswith(
        ("user.disable", "membership.", "points.", "job.refund", "task.refund", "asset.quarantine")
    ):
        minimum = "high"
    order = {"normal": 0, "high": 1, "critical": 2}
    return max((minimum, requested), key=order.__getitem__)


def _require(principal: Principal, permission: str) -> None:
    if permission not in principal.permissions:
        raise ApiError(403, "PERMISSION_DENIED", "没有执行此操作的权限")


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or uuid7().hex


def _record_audit(
    session,
    *,
    request: Request,
    actor: uuid.UUID,
    action: str,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        OutboxEvent(
            id=uuid7(),
            topic="admin.audit",
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload={
                "action": action,
                "actor_user_id": str(actor),
                "subject_user_id": None,
                "request_id": _request_id(request),
                "result": "success",
                "details": _redact(details or {}),
            },
            status="pending",
            attempts=0,
            available_at=datetime.now(UTC),
            version=1,
        )
    )


def _action_payload(item: AdminActionRequest) -> dict[str, Any]:
    return {
        "id": item.id,
        "action_type": item.action_type,
        "target_type": item.target_type,
        "target_id": item.target_id,
        "payload": _redact(item.payload),
        "reason": item.reason,
        "risk_level": item.risk_level,
        "status": item.status,
        "requested_by": item.requested_by,
        "approved_by": item.approved_by,
        "executed_at": item.executed_at,
        "created_at": item.created_at,
    }


def _saved_view_payload(item: AdminSavedView) -> dict[str, Any]:
    return {
        "id": item.id,
        "module": item.module,
        "name": item.name,
        "filters": item.filters,
        "columns": item.columns,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _parse_range(value: str) -> tuple[int, datetime]:
    days = RANGE_DAYS.get(value)
    if days is None:
        raise ApiError(422, "INVALID_RANGE", "时间范围仅支持 24h、7d、30d 或 90d")
    return days, datetime.now(UTC) - timedelta(days=days)


def _percentile(values: list[float], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return round(ordered[index])


@router.get("/dashboard/summary")
async def dashboard_summary(
    request: Request,
    _principal: DashboardReader,
    range_value: str = Query(default="7d", alias="range"),
) -> dict[str, Any]:
    days, since = _parse_range(range_value)
    now = datetime.now(UTC)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        user_total = int(
            await session.scalar(
                select(func.count()).select_from(User).where(User.deleted_at.is_(None))
            )
            or 0
        )
        active_users = int(
            await session.scalar(
                select(func.count())
                .select_from(User)
                .where(
                    User.status == "active", User.last_login_at >= since, User.deleted_at.is_(None)
                )
            )
            or 0
        )
        registrations = int(
            await session.scalar(
                select(func.count())
                .select_from(User)
                .where(User.created_at >= since, User.deleted_at.is_(None))
            )
            or 0
        )
        membership_rows = (
            await session.execute(
                select(MembershipPlan.code, func.count(UserMembership.id))
                .select_from(UserMembership)
                .join(MembershipPlan, MembershipPlan.id == UserMembership.plan_id)
                .where(UserMembership.status == "active")
                .group_by(MembershipPlan.code, MembershipPlan.level)
                .order_by(MembershipPlan.level)
            )
        ).all()
        job_rows = (
            await session.execute(
                select(ImageJob.status, func.count(ImageJob.id))
                .where(ImageJob.created_at >= since)
                .group_by(ImageJob.status)
            )
        ).all()
        job_counts = {name: int(count) for name, count in job_rows}
        job_total = sum(job_counts.values())
        completed_rows = (
            await session.execute(
                select(ImageJob.started_at, ImageJob.completed_at).where(
                    ImageJob.created_at >= since,
                    ImageJob.started_at.is_not(None),
                    ImageJob.completed_at.is_not(None),
                )
            )
        ).all()
        durations = [
            max(0.0, (completed - started).total_seconds() * 1000)
            for started, completed in completed_rows
            if started is not None and completed is not None
        ]
        failed_reasons = (
            await session.execute(
                select(ImageJob.error_code, func.count(ImageJob.id))
                .where(ImageJob.created_at >= since, ImageJob.status.in_(("failed", "timed_out")))
                .group_by(ImageJob.error_code)
                .order_by(desc(func.count(ImageJob.id)))
                .limit(5)
            )
        ).all()
        point_rows = (
            await session.execute(
                select(
                    PointTransaction.entry_type, func.coalesce(func.sum(PointTransaction.delta), 0)
                )
                .where(PointTransaction.created_at >= since)
                .group_by(PointTransaction.entry_type)
            )
        ).all()
        point_totals = {name: int(total) for name, total in point_rows}
        asset_count, asset_bytes = (
            await session.execute(
                select(func.count(Asset.id), func.coalesce(func.sum(Asset.size_bytes), 0)).where(
                    Asset.status != "deleted"
                )
            )
        ).one()
        new_asset_count, new_asset_bytes = (
            await session.execute(
                select(func.count(Asset.id), func.coalesce(func.sum(Asset.size_bytes), 0)).where(
                    Asset.created_at >= since, Asset.status != "deleted"
                )
            )
        ).one()
        quarantined_assets = int(
            await session.scalar(
                select(func.count()).select_from(Asset).where(Asset.status == "quarantined")
            )
            or 0
        )
        deletion_failures = int(
            await session.scalar(
                select(func.count())
                .select_from(ObjectDeletionQueue)
                .where(ObjectDeletionQueue.status == "failed")
            )
            or 0
        )
        sub2_total, sub2_succeeded, sub2_points = (
            await session.execute(
                select(
                    func.count(ImageJob.id),
                    func.coalesce(func.sum(case((ImageJob.status == "succeeded", 1), else_=0)), 0),
                    func.coalesce(func.sum(ImageJob.charged_points), 0),
                )
                .select_from(ImageJob)
                .join(OperationCatalog, OperationCatalog.code == ImageJob.operation_code)
                .where(ImageJob.created_at >= since, OperationCatalog.engine_type == "sub2api")
            )
        ).one()
        sub2_429 = int(
            await session.scalar(
                select(func.count())
                .select_from(ImageJob)
                .join(OperationCatalog, OperationCatalog.code == ImageJob.operation_code)
                .where(
                    ImageJob.created_at >= since,
                    OperationCatalog.engine_type == "sub2api",
                    ImageJob.error_code.contains("429"),
                )
            )
            or 0
        )
        sub2_5xx = int(
            await session.scalar(
                select(func.count())
                .select_from(ImageJob)
                .join(OperationCatalog, OperationCatalog.code == ImageJob.operation_code)
                .where(
                    ImageJob.created_at >= since,
                    OperationCatalog.engine_type == "sub2api",
                    or_(ImageJob.error_code.like("5%"), ImageJob.error_code.contains("5XX")),
                )
            )
            or 0
        )
        service_rows = (
            await session.execute(
                select(ServiceInstance.service_type, func.count(ServiceInstance.id))
                .where(ServiceInstance.last_heartbeat_at >= now - timedelta(seconds=90))
                .group_by(ServiceInstance.service_type)
            )
        ).all()
        configured_groups = {
            code: active_version
            for code, active_version in (
                await session.execute(select(ConfigGroup.code, ConfigGroup.active_version))
            ).all()
        }
    terminal = sum(
        job_counts.get(item, 0) for item in ("succeeded", "failed", "cancelled", "timed_out")
    )
    successful = job_counts.get("succeeded", 0)
    sub2_total_int = int(sub2_total or 0)
    return {
        "range": range_value,
        "generated_at": now,
        "users": {
            "total": user_total,
            "active": active_users,
            "registrations": registrations,
            "memberships": {code: int(count) for code, count in membership_rows},
        },
        "jobs": {
            "total": job_total,
            "success_rate": round(successful / terminal, 4) if terminal else None,
            "p50_duration_ms": _percentile(durations, 0.5),
            "p95_duration_ms": _percentile(durations, 0.95),
            "queue_length": job_counts.get("queued", 0) + job_counts.get("retry_wait", 0),
            "by_status": job_counts,
            "failure_reasons": [
                {"code": code or "UNKNOWN", "count": int(count)} for code, count in failed_reasons
            ],
        },
        "points": point_totals,
        "storage": {
            "asset_count": int(asset_count),
            "bytes": int(asset_bytes),
            "new_asset_count": int(new_asset_count),
            "new_bytes": int(new_asset_bytes),
            "quarantined": quarantined_assets,
            "deletion_failures": deletion_failures,
        },
        "sub2api": {
            "requests": sub2_total_int,
            "success_rate": round(int(sub2_succeeded or 0) / sub2_total_int, 4)
            if sub2_total_int
            else None,
            "responses_429": sub2_429,
            "responses_5xx": sub2_5xx,
            "estimated_points": int(sub2_points or 0),
            "configured": configured_groups.get("sub2api") is not None,
        },
        "system": {
            "services": {service_type: int(count) for service_type, count in service_rows},
            "r2_configured": configured_groups.get("r2") is not None,
            "period_days": days,
        },
    }


@router.get("/dashboard/timeseries")
async def dashboard_timeseries(
    request: Request,
    _principal: DashboardReader,
    metric: Literal["jobs", "users", "points", "assets"] = "jobs",
    range_value: str = Query(default="30d", alias="range"),
) -> dict[str, Any]:
    days, since = _parse_range(range_value)
    today = datetime.now(UTC).date()
    labels = [today - timedelta(days=offset) for offset in reversed(range(days))]
    points: dict[date, dict[str, int]] = {label: {} for label in labels}
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        if metric == "jobs":
            rows = (
                await session.execute(
                    select(ImageJob.created_at, ImageJob.status).where(ImageJob.created_at >= since)
                )
            ).all()
            for created_at, row_status in rows:
                bucket = points.get(created_at.date())
                if bucket is not None:
                    bucket["total"] = bucket.get("total", 0) + 1
                    key = (
                        "succeeded"
                        if row_status == "succeeded"
                        else ("failed" if row_status in {"failed", "timed_out"} else "other")
                    )
                    bucket[key] = bucket.get(key, 0) + 1
        elif metric == "users":
            rows = (
                await session.execute(
                    select(User.created_at, User.status).where(
                        User.created_at >= since, User.deleted_at.is_(None)
                    )
                )
            ).all()
            for created_at, row_status in rows:
                bucket = points.get(created_at.date())
                if bucket is not None:
                    bucket["registrations"] = bucket.get("registrations", 0) + 1
                    if row_status == "active":
                        bucket["activated"] = bucket.get("activated", 0) + 1
        elif metric == "points":
            rows = (
                await session.execute(
                    select(
                        PointTransaction.created_at,
                        PointTransaction.entry_type,
                        PointTransaction.delta,
                    ).where(PointTransaction.created_at >= since)
                )
            ).all()
            for created_at, entry_type, delta in rows:
                bucket = points.get(created_at.date())
                if bucket is not None:
                    bucket[entry_type] = bucket.get(entry_type, 0) + int(delta)
        else:
            rows = (
                await session.execute(
                    select(Asset.created_at, Asset.size_bytes).where(
                        Asset.created_at >= since, Asset.status != "deleted"
                    )
                )
            ).all()
            for created_at, size_bytes in rows:
                bucket = points.get(created_at.date())
                if bucket is not None:
                    bucket["count"] = bucket.get("count", 0) + 1
                    bucket["bytes"] = bucket.get("bytes", 0) + int(size_bytes)
    return {
        "metric": metric,
        "range": range_value,
        "points": [{"date": label.isoformat(), **points[label]} for label in labels],
    }


def _search_item(
    result_type: str,
    item_id: uuid.UUID,
    title: str,
    subtitle: str,
    href: str,
) -> dict[str, str]:
    return {
        "type": result_type,
        "id": str(item_id),
        "title": title,
        "subtitle": subtitle,
        "href": href,
    }


@router.get("/search")
async def global_search(
    request: Request,
    principal: DashboardReader,
    q: str = Query(min_length=2, max_length=200),
) -> dict[str, Any]:
    query = q.strip()
    if len(query) < 2:
        raise ApiError(422, "VALIDATION_ERROR", "搜索词至少需要 2 个字符")
    lowered = query.casefold()
    exact_id: uuid.UUID | None
    try:
        exact_id = uuid.UUID(query)
    except ValueError:
        exact_id = None
    results: list[dict[str, str]] = []
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        if "users.read" in principal.permissions:
            user_where = or_(
                func.lower(User.email).contains(lowered),
                func.lower(User.display_name).contains(lowered),
                func.lower(User.username).contains(lowered),
            )
            if exact_id:
                user_where = or_(user_where, User.id == exact_id)
            users = list(
                (
                    await session.scalars(
                        select(User)
                        .where(user_where, User.deleted_at.is_(None))
                        .order_by(User.created_at.desc())
                        .limit(6)
                    )
                ).all()
            )
            results.extend(
                _search_item(
                    "user", user.id, user.display_name, user.email, f"/admin/users?id={user.id}"
                )
                for user in users
            )
        if "tasks.read" in principal.permissions:
            job_where = or_(
                func.lower(ImageJob.operation_code).contains(lowered),
                func.lower(ImageJob.error_code).contains(lowered),
            )
            if exact_id:
                job_where = or_(job_where, ImageJob.id == exact_id)
            jobs = list(
                (
                    await session.scalars(
                        select(ImageJob)
                        .where(job_where)
                        .order_by(ImageJob.created_at.desc())
                        .limit(6)
                    )
                ).all()
            )
            results.extend(
                _search_item(
                    "job",
                    job.id,
                    job.operation_code,
                    f"{job.status} · {job.user_id}",
                    f"/admin/jobs?id={job.id}",
                )
                for job in jobs
            )
        if "assets.read" in principal.permissions:
            asset_where = func.lower(Asset.original_filename).contains(lowered)
            if exact_id:
                asset_where = or_(asset_where, Asset.id == exact_id)
            assets = list(
                (
                    await session.scalars(
                        select(Asset).where(asset_where).order_by(Asset.created_at.desc()).limit(6)
                    )
                ).all()
            )
            results.extend(
                _search_item(
                    "asset",
                    asset.id,
                    asset.original_filename or str(asset.id),
                    f"{asset.kind} · {asset.status}",
                    f"/admin/assets?id={asset.id}",
                )
                for asset in assets
            )
        if "memberships.read" in principal.permissions:
            plans = list(
                (
                    await session.scalars(
                        select(MembershipPlan)
                        .where(
                            or_(
                                func.lower(MembershipPlan.name).contains(lowered),
                                func.lower(MembershipPlan.code).contains(lowered),
                            )
                        )
                        .order_by(MembershipPlan.level)
                        .limit(4)
                    )
                ).all()
            )
            results.extend(
                _search_item(
                    "membership_plan",
                    plan.id,
                    plan.name,
                    f"{plan.code} · {plan.status}",
                    "/admin/memberships",
                )
                for plan in plans
            )
        if "roles.read" in principal.permissions:
            roles = list(
                (
                    await session.scalars(
                        select(Role)
                        .where(
                            or_(
                                func.lower(Role.name).contains(lowered),
                                func.lower(Role.code).contains(lowered),
                            )
                        )
                        .order_by(Role.code)
                        .limit(4)
                    )
                ).all()
            )
            results.extend(
                _search_item("role", role.id, role.name, role.code, "/admin/roles")
                for role in roles
            )
    return {"query": query, "items": results[:20]}


@router.get("/action-requests")
async def list_action_requests(
    request: Request,
    _principal: AuditReader,
    status_filter: str | None = Query(default=None, alias="status"),
    risk_level: str | None = None,
    action_type: str | None = Query(default=None, max_length=100),
    cursor: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    sort: Literal["created_at", "risk_level"] = "created_at",
    order: Literal["asc", "desc"] = "desc",
) -> dict[str, Any]:
    if status_filter not in {None, "pending", "approved", "rejected", "executed", "failed"}:
        raise ApiError(422, "VALIDATION_ERROR", "无效的操作申请状态")
    if risk_level not in {None, "normal", "high", "critical"}:
        raise ApiError(422, "VALIDATION_ERROR", "无效的风险等级")
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        statement = select(AdminActionRequest)
        if status_filter:
            statement = statement.where(AdminActionRequest.status == status_filter)
        if risk_level:
            statement = statement.where(AdminActionRequest.risk_level == risk_level)
        if action_type:
            statement = statement.where(AdminActionRequest.action_type == action_type)
        sort_column = (
            AdminActionRequest.created_at if sort == "created_at" else AdminActionRequest.risk_level
        )
        if cursor:
            anchor = await session.get(AdminActionRequest, cursor)
            if anchor is None:
                raise ApiError(422, "INVALID_CURSOR", "分页游标无效")
            anchor_value = getattr(anchor, sort)
            comparison = (
                sort_column < anchor_value if order == "desc" else sort_column > anchor_value
            )
            tie = (
                AdminActionRequest.id < anchor.id
                if order == "desc"
                else AdminActionRequest.id > anchor.id
            )
            statement = statement.where(or_(comparison, and_(sort_column == anchor_value, tie)))
        direction = desc if order == "desc" else asc
        rows = list(
            (
                await session.scalars(
                    statement.order_by(
                        direction(sort_column), direction(AdminActionRequest.id)
                    ).limit(limit + 1)
                )
            ).all()
        )
    items = rows[:limit]
    return {
        "items": [_action_payload(item) for item in items],
        "next_cursor": str(items[-1].id) if len(rows) > limit and items else None,
    }


@router.post("/action-requests", status_code=status.HTTP_201_CREATED)
async def create_action_request(
    payload: CreateActionRequest,
    request: Request,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    _require(principal, _permission_for_action(payload.action_type))
    risk_level = _risk_for_action(payload.action_type, payload.risk_level)
    if risk_level != "normal" and not payload.confirmed:
        raise ApiError(409, "EXPLICIT_CONFIRMATION_REQUIRED", "高风险操作需要明确确认")
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        item = AdminActionRequest(
            id=uuid7(),
            action_type=payload.action_type,
            target_type=payload.target_type,
            target_id=payload.target_id,
            payload=payload.payload,
            reason=payload.reason,
            risk_level=risk_level,
            status="pending",
            requested_by=principal.user_id,
        )
        session.add(item)
        _record_audit(
            session,
            request=request,
            actor=principal.user_id,
            action="admin_action.requested",
            aggregate_type="admin_action_request",
            aggregate_id=item.id,
            details={
                "action_type": item.action_type,
                "target_type": item.target_type,
                "target_id": str(item.target_id),
                "risk_level": item.risk_level,
                "reason": item.reason,
            },
        )
        await session.commit()
        await session.refresh(item)
    return {"action_request": _action_payload(item)}


async def _review_action(
    action_id: uuid.UUID,
    decision: Literal["approve", "reject"],
    payload: ReviewActionRequest,
    request: Request,
    principal: Principal,
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        item = (
            await session.execute(
                select(AdminActionRequest)
                .where(AdminActionRequest.id == action_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if item is None:
            raise ApiError(404, "ACTION_REQUEST_NOT_FOUND", "操作申请不存在")
        _require(principal, _permission_for_action(item.action_type))
        if item.status != "pending":
            raise ApiError(409, "ACTION_REQUEST_ALREADY_REVIEWED", "操作申请已处理")
        if item.requested_by == principal.user_id:
            raise ApiError(409, "ACTION_REQUEST_SELF_REVIEW", "申请人不能审批自己的操作")
        item.status = "approved" if decision == "approve" else "rejected"
        item.approved_by = principal.user_id if decision == "approve" else None
        _record_audit(
            session,
            request=request,
            actor=principal.user_id,
            action=f"admin_action.{item.status}",
            aggregate_type="admin_action_request",
            aggregate_id=item.id,
            details={"review_reason": payload.reason, "requested_by": str(item.requested_by)},
        )
        await session.commit()
        await session.refresh(item)
    return {"action_request": _action_payload(item)}


@router.post("/action-requests/{action_id}/approve")
async def approve_action_request(
    action_id: uuid.UUID,
    payload: ReviewActionRequest,
    request: Request,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    return await _review_action(action_id, "approve", payload, request, principal)


@router.post("/action-requests/{action_id}/reject")
async def reject_action_request(
    action_id: uuid.UUID,
    payload: ReviewActionRequest,
    request: Request,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    return await _review_action(action_id, "reject", payload, request, principal)


def _audit_payload(event: OutboxEvent) -> dict[str, Any]:
    payload = _redact(event.payload if isinstance(event.payload, dict) else {})
    details = payload.get("details") if isinstance(payload, dict) else {}
    return {
        "id": event.id,
        "actor_user_id": payload.get("actor_user_id"),
        "action": payload.get("action") or event.topic,
        "target_type": event.aggregate_type,
        "target_id": event.aggregate_id,
        "result": payload.get("result", "success"),
        "request_id": payload.get("request_id"),
        "details": details if isinstance(details, dict) else {},
        "created_at": event.created_at,
    }


@router.get("/audit")
async def list_audit_events(
    request: Request,
    _principal: AuditReader,
    action: str | None = Query(default=None, max_length=200),
    target_type: str | None = Query(default=None, max_length=100),
    result: Literal["success", "failed"] | None = None,
    cursor: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    order: Literal["asc", "desc"] = "desc",
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        statement = select(OutboxEvent).where(
            or_(
                OutboxEvent.topic.like("%.audit"),
                OutboxEvent.topic.like("identity.%"),
                OutboxEvent.topic == "admin.audit",
            )
        )
        if target_type:
            statement = statement.where(OutboxEvent.aggregate_type == target_type)
        if action:
            statement = statement.where(OutboxEvent.topic.contains(action))
        if cursor:
            anchor = await session.get(OutboxEvent, cursor)
            if anchor is None:
                raise ApiError(422, "INVALID_CURSOR", "分页游标无效")
            if order == "desc":
                statement = statement.where(
                    or_(
                        OutboxEvent.created_at < anchor.created_at,
                        and_(
                            OutboxEvent.created_at == anchor.created_at,
                            OutboxEvent.id < anchor.id,
                        ),
                    )
                )
            else:
                statement = statement.where(
                    or_(
                        OutboxEvent.created_at > anchor.created_at,
                        and_(
                            OutboxEvent.created_at == anchor.created_at,
                            OutboxEvent.id > anchor.id,
                        ),
                    )
                )
        direction = desc if order == "desc" else asc
        rows = list(
            (
                await session.scalars(
                    statement.order_by(
                        direction(OutboxEvent.created_at), direction(OutboxEvent.id)
                    ).limit(limit * 3 + 1)
                )
            ).all()
        )
    items = [_audit_payload(item) for item in rows]
    if result:
        items = [item for item in items if item["result"] == result]
    items = items[: limit + 1]
    page = items[:limit]
    return {
        "items": page,
        "next_cursor": str(page[-1]["id"]) if len(items) > limit and page else None,
    }


@router.get("/saved-views")
async def list_saved_views(
    request: Request,
    principal: DashboardReader,
    module: str | None = Query(default=None, max_length=64),
) -> dict[str, Any]:
    if module:
        permission = MODULE_PERMISSIONS.get(module)
        if permission is None:
            raise ApiError(422, "INVALID_ADMIN_MODULE", "无效的后台模块")
        _require(principal, permission)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        statement = select(AdminSavedView).where(AdminSavedView.owner_id == principal.user_id)
        if module:
            statement = statement.where(AdminSavedView.module == module)
        rows = list(
            (
                await session.scalars(
                    statement.order_by(AdminSavedView.module, AdminSavedView.name).limit(200)
                )
            ).all()
        )
    visible = [
        item for item in rows if MODULE_PERMISSIONS.get(item.module, "") in principal.permissions
    ]
    return {"items": [_saved_view_payload(item) for item in visible]}


@router.post("/saved-views", status_code=status.HTTP_201_CREATED)
async def save_view(
    payload: SaveViewRequest,
    request: Request,
    principal: DashboardReader,
) -> dict[str, Any]:
    permission = MODULE_PERMISSIONS.get(payload.module)
    if permission is None:
        raise ApiError(422, "INVALID_ADMIN_MODULE", "无效的后台模块")
    _require(principal, permission)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        item = AdminSavedView(
            id=uuid7(),
            owner_id=principal.user_id,
            module=payload.module,
            name=payload.name,
            filters=payload.filters,
            columns=payload.columns,
        )
        session.add(item)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ApiError(409, "SAVED_VIEW_EXISTS", "同名视图已存在") from exc
        await session.refresh(item)
    return {"saved_view": _saved_view_payload(item)}


@router.delete(
    "/saved-views/{view_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_saved_view(
    view_id: uuid.UUID,
    request: Request,
    principal: DashboardReader,
) -> None:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        item = await session.get(AdminSavedView, view_id)
        if item is None or item.owner_id != principal.user_id:
            raise ApiError(404, "SAVED_VIEW_NOT_FOUND", "保存的视图不存在")
        await session.delete(item)
        await session.commit()


def _encode_export_token(
    request: Request, principal: Principal, module: str
) -> tuple[str, datetime]:
    expires_at = datetime.now(UTC) + timedelta(
        seconds=request.app.state.settings.admin_export_ttl_seconds
    )
    payload = json.dumps(
        {
            "module": module,
            "user_id": str(principal.user_id),
            "expires": int(expires_at.timestamp()),
            "nonce": uuid7().hex,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(
        request.app.state.settings.auth_token_pepper.encode(),
        encoded.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{signature}", expires_at


def _decode_export_token(request: Request, token: str, principal: Principal) -> str:
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(
            request.app.state.settings.auth_token_pepper.encode(),
            encoded.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        if payload["user_id"] != str(principal.user_id):
            raise ValueError
        if int(payload["expires"]) < int(datetime.now(UTC).timestamp()):
            raise ApiError(410, "EXPORT_LINK_EXPIRED", "导出链接已过期")
        module = str(payload["module"])
        if module not in EXPORT_MODULES:
            raise ValueError
        return module
    except ApiError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError(404, "EXPORT_LINK_INVALID", "导出链接无效") from exc


def _mask_email(value: str) -> str:
    local, separator, domain = value.partition("@")
    if not separator:
        return "***"
    return f"{local[:1]}***@{domain}"


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(_redact(value), ensure_ascii=False, separators=(",", ":"), default=str)
    text = str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")) and not isinstance(value, (int, float)):
        return "'" + text
    return text


async def _export_rows(session, module: str) -> tuple[list[str], list[list[Any]]]:
    if module == "users":
        items = list(
            (
                await session.scalars(
                    select(User)
                    .where(User.deleted_at.is_(None))
                    .order_by(User.created_at.desc())
                    .limit(10_000)
                )
            ).all()
        )
        return ["id", "email_masked", "display_name", "status", "last_login_at", "created_at"], [
            [
                item.id,
                _mask_email(item.email),
                item.display_name,
                item.status,
                item.last_login_at,
                item.created_at,
            ]
            for item in items
        ]
    if module == "memberships":
        items = (
            await session.execute(
                select(UserMembership, MembershipPlan.code)
                .join(MembershipPlan, MembershipPlan.id == UserMembership.plan_id)
                .order_by(UserMembership.created_at.desc())
                .limit(10_000)
            )
        ).all()
        return ["id", "user_id", "plan", "status", "starts_at", "ends_at", "source"], [
            [item.id, item.user_id, code, item.status, item.starts_at, item.ends_at, item.source]
            for item, code in items
        ]
    if module == "points":
        items = list(
            (
                await session.scalars(
                    select(PointTransaction)
                    .order_by(PointTransaction.created_at.desc())
                    .limit(10_000)
                )
            ).all()
        )
        return [
            "id",
            "user_id",
            "entry_type",
            "delta",
            "balance_after",
            "reference_type",
            "reference_id",
            "actor_user_id",
            "created_at",
        ], [
            [
                item.id,
                item.user_id,
                item.entry_type,
                item.delta,
                item.balance_after,
                item.reference_type,
                item.reference_id,
                item.actor_user_id,
                item.created_at,
            ]
            for item in items
        ]
    if module == "pricing":
        items = (
            await session.execute(
                select(OperationPrice, OperationCatalog.code)
                .join(OperationCatalog, OperationCatalog.id == OperationPrice.operation_id)
                .order_by(OperationPrice.created_at.desc())
                .limit(10_000)
            )
        ).all()
        return [
            "id",
            "operation",
            "version",
            "base_points",
            "effective_from",
            "effective_to",
            "reason",
            "created_at",
        ], [
            [
                item.id,
                code,
                item.version,
                item.base_points,
                item.effective_from,
                item.effective_to,
                item.reason,
                item.created_at,
            ]
            for item, code in items
        ]
    if module == "jobs":
        items = list(
            (
                await session.scalars(
                    select(ImageJob).order_by(ImageJob.created_at.desc()).limit(10_000)
                )
            ).all()
        )
        return [
            "id",
            "user_id",
            "operation",
            "status",
            "refund_status",
            "charged_points",
            "attempt_count",
            "error_code",
            "queued_at",
            "completed_at",
        ], [
            [
                item.id,
                item.user_id,
                item.operation_code,
                item.status,
                item.refund_status,
                item.charged_points,
                item.attempt_count,
                item.error_code,
                item.queued_at,
                item.completed_at,
            ]
            for item in items
        ]
    if module == "assets":
        items = list(
            (
                await session.scalars(select(Asset).order_by(Asset.created_at.desc()).limit(10_000))
            ).all()
        )
        return [
            "id",
            "owner_id",
            "kind",
            "operation",
            "status",
            "storage_provider",
            "mime_type",
            "size_bytes",
            "retention_until",
            "created_at",
        ], [
            [
                item.id,
                item.owner_id,
                item.kind,
                item.operation_code,
                item.status,
                item.storage_provider,
                item.mime_type,
                item.size_bytes,
                item.retention_until,
                item.created_at,
            ]
            for item in items
        ]
    if module == "action-requests":
        items = list(
            (
                await session.scalars(
                    select(AdminActionRequest)
                    .order_by(AdminActionRequest.created_at.desc())
                    .limit(10_000)
                )
            ).all()
        )
        return [
            "id",
            "action_type",
            "target_type",
            "target_id",
            "risk_level",
            "status",
            "requested_by",
            "approved_by",
            "reason",
            "created_at",
        ], [
            [
                item.id,
                item.action_type,
                item.target_type,
                item.target_id,
                item.risk_level,
                item.status,
                item.requested_by,
                item.approved_by,
                item.reason,
                item.created_at,
            ]
            for item in items
        ]
    events = list(
        (
            await session.scalars(
                select(OutboxEvent)
                .where(
                    or_(
                        OutboxEvent.topic.like("%.audit"),
                        OutboxEvent.topic.like("identity.%"),
                        OutboxEvent.topic == "admin.audit",
                    )
                )
                .order_by(OutboxEvent.created_at.desc())
                .limit(10_000)
            )
        ).all()
    )
    rows = [_audit_payload(event) for event in events]
    return [
        "id",
        "actor_user_id",
        "action",
        "target_type",
        "target_id",
        "result",
        "request_id",
        "details",
        "created_at",
    ], [
        [
            item["id"],
            item["actor_user_id"],
            item["action"],
            item["target_type"],
            item["target_id"],
            item["result"],
            item["request_id"],
            item["details"],
            item["created_at"],
        ]
        for item in rows
    ]


@router.get("/export/{module}", status_code=status.HTTP_202_ACCEPTED)
async def request_export(
    module: str,
    request: Request,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    if module not in EXPORT_MODULES:
        raise ApiError(404, "EXPORT_MODULE_NOT_FOUND", "不支持导出该模块")
    _require(principal, "audit.export")
    _require(principal, MODULE_PERMISSIONS[module])
    token, expires_at = _encode_export_token(request, principal, module)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        _record_audit(
            session,
            request=request,
            actor=principal.user_id,
            action="admin_export.requested",
            aggregate_type="admin_export",
            aggregate_id=uuid7(),
            details={"module": module, "expires_at": expires_at.isoformat()},
        )
        await session.commit()
    return {
        "status": "ready",
        "module": module,
        "download_url": f"/api/v1/admin/exports/download?token={token}",
        "expires_at": expires_at,
    }


@router.get("/exports/download")
async def download_export(
    request: Request,
    principal: CurrentPrincipal,
    token: str = Query(min_length=64, max_length=2048),
) -> StreamingResponse:
    module = _decode_export_token(request, token, principal)
    _require(principal, "audit.export")
    _require(principal, MODULE_PERMISSIONS[module])
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        headers, rows = await _export_rows(session, module)
        _record_audit(
            session,
            request=request,
            actor=principal.user_id,
            action="admin_export.downloaded",
            aggregate_type="admin_export",
            aggregate_id=uuid7(),
            details={"module": module, "row_count": len(rows)},
        )
        await session.commit()
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows([[_csv_value(value) for value in row] for row in rows])
    content = "\ufeff" + stream.getvalue()
    filename = f"sub2image-{module}-{datetime.now(UTC):%Y%m%d-%H%M%S}.csv"
    return StreamingResponse(
        iter([content.encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
