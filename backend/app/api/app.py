from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, StrictBool, field_validator, model_validator
from sqlalchemy import func, or_, select, update

from app.api.dependencies import Principal, get_current_principal
from app.api.errors import ApiError
from app.repositories.models import (
    MembershipPlan,
    PointAccount,
    User,
    UserMembership,
    UserNotification,
    UserPreference,
)
from app.services.configuration import (
    runtime_config_value,
)
from app.services.configuration import (
    sub2api_configured as is_sub2api_configured,
)
from app.services.memberships import EntitlementService
from app.services.points import PointService

router = APIRouter(prefix="/api/v1", tags=["user-app"])
CurrentUser = Annotated[Principal, Depends(get_current_principal)]

STUDIO_LAYOUT_RULES: dict[str, tuple[type, set[Any] | tuple[int, int] | None]] = {
    "nav_collapsed": (bool, None),
    "asset_view": (str, {"grid", "list"}),
    "panel_position": (str, {"right", "bottom"}),
    "panel_width": (int, (280, 480)),
    "canvas_fit": (str, {"contain", "actual"}),
    "last_tool": (
        str,
        {
            "ai.generate",
            "ai.redraw",
            "ai.extract_print",
            "cutout.smart",
            "upscale.2x",
            "upscale.4x",
            "ai.repair",
            "ai.text_fix",
            "color.effect",
            "ai.variant",
            "vectorize.svg",
        },
    ),
}
NOTIFICATION_PREFERENCE_KEYS = {
    "task_completed",
    "task_failed",
    "points_changed",
    "membership_changed",
}


class PreferencePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: Literal["light", "dark", "system"] | None = None
    locale: Literal["zh-CN", "en-US"] | None = None
    studio_layout: dict[str, Any] | None = None
    notification_preferences: dict[str, StrictBool] | None = None

    @field_validator("studio_layout")
    @classmethod
    def validate_studio_layout(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return value
        for key, item in value.items():
            rule = STUDIO_LAYOUT_RULES.get(key)
            if rule is None:
                raise ValueError(f"不支持的工作台布局字段：{key}")
            expected_type, constraint = rule
            if type(item) is not expected_type:
                raise ValueError(f"工作台布局字段 {key} 类型无效")
            if isinstance(constraint, set) and item not in constraint:
                raise ValueError(f"工作台布局字段 {key} 值无效")
            if isinstance(constraint, tuple) and not constraint[0] <= item <= constraint[1]:
                raise ValueError(f"工作台布局字段 {key} 超出允许范围")
        return value

    @field_validator("notification_preferences")
    @classmethod
    def validate_notification_preferences(
        cls, value: dict[str, bool] | None
    ) -> dict[str, bool] | None:
        if value is None:
            return value
        unsupported = set(value) - NOTIFICATION_PREFERENCE_KEYS
        if unsupported:
            raise ValueError(f"不支持的通知偏好字段：{min(unsupported)}")
        if any(type(item) is not bool for item in value.values()):
            raise ValueError("通知偏好必须为布尔值")
        return value

    @model_validator(mode="after")
    def require_change(self) -> PreferencePatch:
        if not self.model_fields_set:
            raise ValueError("至少提供一个要修改的字段")
        return self


def preference_payload(preference: UserPreference) -> dict[str, Any]:
    return {
        "user_id": preference.user_id,
        "theme": preference.theme,
        "locale": preference.locale,
        "studio_layout": preference.studio_layout,
        "notification_preferences": preference.notification_preferences,
        "updated_at": preference.updated_at,
    }


def notification_payload(notification: UserNotification) -> dict[str, Any]:
    return {
        "id": notification.id,
        "type": notification.type,
        "title": notification.title,
        "body": notification.body,
        "target_url": notification.target_url,
        "read_at": notification.read_at,
        "created_at": notification.created_at,
    }


async def ensure_preference(session, user_id: uuid.UUID) -> UserPreference:
    preference = await session.get(UserPreference, user_id)
    if preference is None:
        preference = UserPreference(
            user_id=user_id,
            theme="light",
            locale="zh-CN",
            studio_layout={},
            notification_preferences={},
        )
        session.add(preference)
        await session.flush()
    return preference


async def service_status(request: Request) -> str:
    status_reader = getattr(request.app.state.runtime_services, "public_status", None)
    if status_reader is None:
        return "ok"
    try:
        return str(await status_reader())
    except Exception:  # noqa: BLE001
        return "degraded"


async def service_features(request: Request) -> dict[str, bool]:
    settings = request.app.state.settings
    runtime = request.app.state.runtime_services
    sub2api_configured = settings.sub2api_configured
    password_reset = settings.password_reset_enabled
    config_cache = getattr(runtime, "config_cache", None)
    if config_cache is not None:
        try:
            config = await config_cache.get("sub2api")
            sub2api_configured = is_sub2api_configured(config)
        except Exception:  # noqa: BLE001
            sub2api_configured = settings.sub2api_configured
        try:
            general = await config_cache.get("general")
            password_reset = bool(general.values.get("password_reset_enabled", password_reset))
        except Exception:  # noqa: BLE001
            password_reset = settings.password_reset_enabled
    return {
        "task_queue": True,
        "sub2api_configured": sub2api_configured,
        "password_reset": password_reset,
    }


@router.get("/app/bootstrap")
async def bootstrap(request: Request, principal: CurrentUser) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    request_id = getattr(request.state, "request_id", "app-bootstrap")
    current_time = datetime.now(UTC)
    async with database.session_factory() as session:
        user = await session.get(User, principal.user_id)
        if user is None or user.status != "active" or user.deleted_at is not None:
            raise ApiError(403, "ACCOUNT_NOT_ACTIVE", "账号当前不可使用工作台")
        entitlement = await EntitlementService().current_snapshot(
            session, user.id, now=current_time, request_id=request_id
        )
        membership = await session.get(UserMembership, entitlement.membership_id)
        plan = await session.get(MembershipPlan, entitlement.plan_id)
        await PointService().ensure_onboarding_grant(
            session,
            user.id,
            points=int(
                await runtime_config_value(
                    request.app.state.runtime_services,
                    "general",
                    "default_points",
                    request.app.state.settings.onboarding_points,
                )
            ),
            request_id=request_id,
        )
        account = (
            await session.scalars(select(PointAccount).where(PointAccount.user_id == user.id))
        ).one()
        preference = await ensure_preference(session, user.id)
        unread_count = int(
            await session.scalar(
                select(func.count(UserNotification.id)).where(
                    UserNotification.user_id == user.id,
                    UserNotification.read_at.is_(None),
                )
            )
            or 0
        )
        await session.commit()
        await session.refresh(preference)
    assert membership is not None and plan is not None
    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "username": user.username,
            "display_name": user.display_name,
            "avatar_asset_id": user.avatar_asset_id,
            "status": user.status,
        },
        "permissions": sorted(principal.permissions),
        "membership": {
            "id": membership.id,
            "status": membership.status,
            "starts_at": membership.starts_at,
            "ends_at": membership.ends_at,
            "plan": {"id": plan.id, "code": plan.code, "name": plan.name, "level": plan.level},
            "entitlements": {
                "discount_bps": entitlement.discount_bps,
                "max_concurrent_jobs": entitlement.max_concurrent_jobs,
                "max_upload_mb": entitlement.max_upload_mb,
                "max_image_megapixels": entitlement.max_image_megapixels,
                "retention_days": entitlement.retention_days,
                "periodic_points": entitlement.periodic_points,
                "extra": entitlement.entitlements,
            },
        },
        "points": {
            "balance": account.balance,
            "status": account.status,
            "lifetime_earned": account.lifetime_earned,
            "lifetime_spent": account.lifetime_spent,
        },
        "service": {
            "status": await service_status(request),
            "features": await service_features(request),
        },
        "notifications": {"unread_count": unread_count},
        "preferences": preference_payload(preference),
    }


@router.get("/me/preferences")
async def get_preferences(request: Request, principal: CurrentUser) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        preference = await ensure_preference(session, principal.user_id)
        await session.commit()
        await session.refresh(preference)
    return {"preferences": preference_payload(preference)}


@router.patch("/me/preferences")
async def update_preferences(
    payload: PreferencePatch, request: Request, principal: CurrentUser
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        preference = await ensure_preference(session, principal.user_id)
        if "theme" in payload.model_fields_set:
            preference.theme = payload.theme or "light"
        if "locale" in payload.model_fields_set:
            preference.locale = payload.locale or "zh-CN"
        if payload.studio_layout is not None:
            preference.studio_layout = {**preference.studio_layout, **payload.studio_layout}
        if payload.notification_preferences is not None:
            preference.notification_preferences = {
                **preference.notification_preferences,
                **payload.notification_preferences,
            }
        await session.commit()
        await session.refresh(preference)
    return {"preferences": preference_payload(preference)}


@router.get("/me/notifications")
async def list_notifications(
    request: Request,
    principal: CurrentUser,
    unread_only: bool = False,
    cursor: uuid.UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        statement = select(UserNotification).where(UserNotification.user_id == principal.user_id)
        if unread_only:
            statement = statement.where(UserNotification.read_at.is_(None))
        if cursor is not None:
            anchor = await session.get(UserNotification, cursor)
            if anchor is None or anchor.user_id != principal.user_id:
                raise ApiError(422, "INVALID_CURSOR", "分页游标无效")
            statement = statement.where(
                or_(
                    UserNotification.created_at < anchor.created_at,
                    (UserNotification.created_at == anchor.created_at)
                    & (UserNotification.id < anchor.id),
                )
            )
        rows = list(
            (
                await session.scalars(
                    statement.order_by(
                        UserNotification.created_at.desc(), UserNotification.id.desc()
                    ).limit(limit + 1)
                )
            ).all()
        )
    items = rows[:limit]
    return {
        "items": [notification_payload(item) for item in items],
        "next_cursor": str(items[-1].id) if len(rows) > limit and items else None,
    }


@router.post("/me/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: uuid.UUID, request: Request, principal: CurrentUser
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        notification = await session.get(UserNotification, notification_id)
        if notification is None or notification.user_id != principal.user_id:
            raise ApiError(404, "NOTIFICATION_NOT_FOUND", "通知不存在")
        if notification.read_at is None:
            notification.read_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(notification)
    return {"notification": notification_payload(notification)}


@router.post("/me/notifications/read-all")
async def mark_all_notifications_read(request: Request, principal: CurrentUser) -> dict[str, int]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        result = await session.execute(
            update(UserNotification)
            .where(
                UserNotification.user_id == principal.user_id,
                UserNotification.read_at.is_(None),
            )
            .values(read_at=datetime.now(UTC))
        )
        await session.commit()
    return {"updated": int(result.rowcount or 0)}
