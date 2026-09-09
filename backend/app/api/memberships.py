from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import Principal, require_permission
from app.api.errors import ApiError
from app.domain.ids import uuid7
from app.repositories.models import (
    MembershipEvent,
    MembershipPlan,
    PlanEntitlement,
    User,
    UserMembership,
)
from app.services.memberships import EntitlementService

router = APIRouter(tags=["memberships"])
MembershipOwner = Annotated[Principal, Depends(require_permission("membership.read_own"))]
MembershipReader = Annotated[Principal, Depends(require_permission("memberships.read"))]
MembershipManager = Annotated[Principal, Depends(require_permission("memberships.manage"))]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]
PLAN_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
ENTITLEMENT_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,99}$")
service = EntitlementService()


def normalized_reason(value: str) -> str:
    reason = value.strip()
    if not reason:
        raise ValueError("必须填写操作原因")
    return reason


def validate_entitlements(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if any(not ENTITLEMENT_CODE_PATTERN.fullmatch(code) for code in value):
        raise ValueError("权益代码格式无效")
    return value


class CreatePlanRequest(BaseModel):
    code: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=5000)
    level: int = Field(ge=0)
    billing_period: Literal["none", "month", "year"]
    periodic_points: int = Field(default=0, ge=0)
    operation_discount_bps: int = Field(ge=0, le=10000)
    max_concurrent_jobs: int = Field(ge=1, le=1000)
    max_upload_mb: int = Field(ge=1, le=10000)
    max_image_megapixels: int = Field(ge=1, le=10000)
    asset_retention_days: int = Field(ge=1, le=36500)
    display_order: int = Field(default=0, ge=0)
    entitlements: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not PLAN_CODE_PATTERN.fullmatch(normalized):
            raise ValueError("套餐代码格式无效")
        return normalized

    @field_validator("name", "description")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("periodic_points")
    @classmethod
    def disable_periodic_points(cls, value: int) -> int:
        if value != 0:
            raise ValueError("P0 不启用周期积分发放")
        return value

    @field_validator("entitlements")
    @classmethod
    def validate_extra_entitlements(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_entitlements(value) or {}

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return normalized_reason(value)


class UpdatePlanRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=5000)
    level: int | None = Field(default=None, ge=0)
    billing_period: Literal["none", "month", "year"] | None = None
    periodic_points: int | None = Field(default=None, ge=0)
    operation_discount_bps: int | None = Field(default=None, ge=0, le=10000)
    max_concurrent_jobs: int | None = Field(default=None, ge=1, le=1000)
    max_upload_mb: int | None = Field(default=None, ge=1, le=10000)
    max_image_megapixels: int | None = Field(default=None, ge=1, le=10000)
    asset_retention_days: int | None = Field(default=None, ge=1, le=36500)
    display_order: int | None = Field(default=None, ge=0)
    entitlements: dict[str, Any] | None = None
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("name", "description")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("periodic_points")
    @classmethod
    def disable_periodic_points(cls, value: int | None) -> int | None:
        if value not in {None, 0}:
            raise ValueError("P0 不启用周期积分发放")
        return value

    @field_validator("entitlements")
    @classmethod
    def validate_extra_entitlements(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return validate_entitlements(value)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return normalized_reason(value)


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return normalized_reason(value)


class AssignMembershipRequest(BaseModel):
    plan_id: uuid.UUID
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return normalized_reason(value)


class RenewMembershipRequest(BaseModel):
    ends_at: datetime
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return normalized_reason(value)


class ChangePlanRequest(BaseModel):
    target_plan_id: uuid.UUID
    effective_mode: Literal["now", "period_end"]
    ends_at: datetime | None = None
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return normalized_reason(value)


class CancelMembershipRequest(BaseModel):
    effective_mode: Literal["now", "period_end"] = "period_end"
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        return normalized_reason(value)


def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or uuid7().hex


def clean_idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ApiError(422, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key 不能为空")
    return normalized


async def plan_entitlement_map(
    session, plan_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    if not plan_ids:
        return {}
    rows = (
        await session.scalars(select(PlanEntitlement).where(PlanEntitlement.plan_id.in_(plan_ids)))
    ).all()
    result: dict[uuid.UUID, dict[str, Any]] = {plan_id: {} for plan_id in plan_ids}
    for row in rows:
        result[row.plan_id][row.entitlement_code] = row.value
    return result


def plan_payload(plan: MembershipPlan, entitlements: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": plan.id,
        "code": plan.code,
        "name": plan.name,
        "description": plan.description,
        "level": plan.level,
        "status": plan.status,
        "billing_period": plan.billing_period,
        "periodic_points": plan.periodic_points,
        "operation_discount_bps": plan.operation_discount_bps,
        "max_concurrent_jobs": plan.max_concurrent_jobs,
        "max_upload_mb": plan.max_upload_mb,
        "max_image_megapixels": plan.max_image_megapixels,
        "asset_retention_days": plan.asset_retention_days,
        "display_order": plan.display_order,
        "entitlements": entitlements,
        "created_by": plan.created_by,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
    }


def membership_payload(
    membership: UserMembership, plan: MembershipPlan | None = None
) -> dict[str, Any]:
    result = {
        "id": membership.id,
        "user_id": membership.user_id,
        "plan_id": membership.plan_id,
        "status": membership.status,
        "starts_at": membership.starts_at,
        "ends_at": membership.ends_at,
        "auto_renew": membership.auto_renew,
        "source": membership.source,
        "assigned_by": membership.assigned_by,
        "reason": membership.reason,
        "entitlement_snapshot": membership.entitlement_snapshot,
        "created_at": membership.created_at,
        "updated_at": membership.updated_at,
    }
    if plan is not None:
        result["plan"] = {"id": plan.id, "code": plan.code, "name": plan.name, "level": plan.level}
    return result


def event_payload(event: MembershipEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "membership_id": event.membership_id,
        "event_type": event.event_type,
        "old_plan_id": event.old_plan_id,
        "new_plan_id": event.new_plan_id,
        "effective_at": event.effective_at,
        "actor_user_id": event.actor_user_id,
        "reason": event.reason,
        "created_at": event.created_at,
    }


async def get_plan_or_404(session, plan_id: uuid.UUID, *, for_update: bool = False):
    statement = select(MembershipPlan).where(MembershipPlan.id == plan_id)
    if for_update:
        statement = statement.with_for_update()
    plan = (await session.scalars(statement)).one_or_none()
    if plan is None:
        raise ApiError(404, "MEMBERSHIP_PLAN_NOT_FOUND", "会员套餐不存在")
    return plan


async def replace_entitlements(session, plan_id: uuid.UUID, entitlements: dict[str, Any]) -> None:
    await session.execute(delete(PlanEntitlement).where(PlanEntitlement.plan_id == plan_id))
    session.add_all(
        PlanEntitlement(
            id=uuid7(),
            plan_id=plan_id,
            entitlement_code=code,
            value=value,
        )
        for code, value in sorted(entitlements.items())
    )


def validate_plan_definition(plan: MembershipPlan) -> None:
    if plan.periodic_points != 0:
        raise ApiError(422, "PERIODIC_POINTS_DISABLED", "P0 不启用周期积分发放")
    if plan.code == "free" and plan.billing_period != "none":
        raise ApiError(422, "INVALID_FREE_PLAN", "Free 套餐必须长期有效")


async def commit_membership_change(session) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(409, "MEMBERSHIP_CONFLICT", "会员状态已发生变化，请刷新后重试") from exc


@router.get("/api/v1/membership/plans")
async def list_visible_plans(request: Request, _principal: MembershipOwner) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        plans = list(
            (
                await session.scalars(
                    select(MembershipPlan)
                    .where(MembershipPlan.status == "active")
                    .order_by(MembershipPlan.display_order, MembershipPlan.level)
                )
            ).all()
        )
        entitlements = await plan_entitlement_map(session, [plan.id for plan in plans])
    return {"items": [plan_payload(plan, entitlements[plan.id]) for plan in plans]}


@router.get("/api/v1/membership/me")
async def get_my_membership(request: Request, principal: MembershipOwner) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        snapshot = await service.current_snapshot(
            session,
            principal.user_id,
            request_id=request_id(request),
        )
        membership = await session.get(UserMembership, snapshot.membership_id)
        plan = await session.get(MembershipPlan, snapshot.plan_id)
        assert membership is not None and plan is not None
        await session.commit()
    return {
        "membership": membership_payload(membership, plan),
        "entitlements": {
            "plan_code": snapshot.plan_code,
            "discount_bps": snapshot.discount_bps,
            "max_concurrent_jobs": snapshot.max_concurrent_jobs,
            "max_upload_mb": snapshot.max_upload_mb,
            "max_image_megapixels": snapshot.max_image_megapixels,
            "retention_days": snapshot.retention_days,
            "periodic_points": snapshot.periodic_points,
            "extra": snapshot.entitlements,
        },
    }


@router.get("/api/v1/admin/membership-plans")
async def list_admin_plans(
    request: Request,
    _principal: MembershipReader,
    status_filter: str | None = Query(default=None, alias="status"),
) -> dict[str, Any]:
    if status_filter not in {None, "draft", "active", "inactive"}:
        raise ApiError(422, "VALIDATION_ERROR", "无效的会员套餐状态")
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        statement = select(MembershipPlan)
        if status_filter:
            statement = statement.where(MembershipPlan.status == status_filter)
        plans = list(
            (
                await session.scalars(
                    statement.order_by(MembershipPlan.display_order, MembershipPlan.level)
                )
            ).all()
        )
        entitlements = await plan_entitlement_map(session, [plan.id for plan in plans])
    return {"items": [plan_payload(plan, entitlements[plan.id]) for plan in plans]}


@router.post("/api/v1/admin/membership-plans", status_code=status.HTTP_201_CREATED)
async def create_plan(
    payload: CreatePlanRequest,
    request: Request,
    principal: MembershipManager,
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        plan = MembershipPlan(
            id=uuid7(),
            code=payload.code,
            name=payload.name,
            description=payload.description,
            level=payload.level,
            status="draft",
            billing_period=payload.billing_period,
            periodic_points=payload.periodic_points,
            operation_discount_bps=payload.operation_discount_bps,
            max_concurrent_jobs=payload.max_concurrent_jobs,
            max_upload_mb=payload.max_upload_mb,
            max_image_megapixels=payload.max_image_megapixels,
            asset_retention_days=payload.asset_retention_days,
            display_order=payload.display_order,
            created_by=principal.user_id,
        )
        validate_plan_definition(plan)
        session.add(plan)
        try:
            await session.flush()
            await replace_entitlements(session, plan.id, payload.entitlements)
            service.record_audit(
                session,
                action="plan.created",
                aggregate_type="membership_plan",
                aggregate_id=plan.id,
                actor_user_id=principal.user_id,
                subject_user_id=None,
                request_id=request_id(request),
                details={"reason": payload.reason, "code": plan.code},
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ApiError(409, "MEMBERSHIP_PLAN_EXISTS", "套餐代码或等级已存在") from exc
        await session.refresh(plan)
    return {"plan": plan_payload(plan, payload.entitlements)}


@router.get("/api/v1/admin/membership-plans/{plan_id}")
async def get_admin_plan(
    plan_id: uuid.UUID, request: Request, _principal: MembershipReader
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        plan = await get_plan_or_404(session, plan_id)
        entitlements = await plan_entitlement_map(session, [plan.id])
    return {"plan": plan_payload(plan, entitlements[plan.id])}


@router.patch("/api/v1/admin/membership-plans/{plan_id}")
async def update_plan(
    plan_id: uuid.UUID,
    payload: UpdatePlanRequest,
    request: Request,
    principal: MembershipManager,
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        plan = await get_plan_or_404(session, plan_id, for_update=True)
        editable_fields = {
            "name",
            "description",
            "level",
            "billing_period",
            "periodic_points",
            "operation_discount_bps",
            "max_concurrent_jobs",
            "max_upload_mb",
            "max_image_megapixels",
            "asset_retention_days",
            "display_order",
        }
        changed_fields: list[str] = []
        for field_name in sorted(editable_fields & payload.model_fields_set):
            value = getattr(payload, field_name)
            if value is not None and value != getattr(plan, field_name):
                setattr(plan, field_name, value)
                changed_fields.append(field_name)
        validate_plan_definition(plan)
        if "entitlements" in payload.model_fields_set and payload.entitlements is not None:
            await replace_entitlements(session, plan.id, payload.entitlements)
            changed_fields.append("entitlements")
        service.record_audit(
            session,
            action="plan.updated",
            aggregate_type="membership_plan",
            aggregate_id=plan.id,
            actor_user_id=principal.user_id,
            subject_user_id=None,
            request_id=request_id(request),
            details={"reason": payload.reason, "changed_fields": changed_fields},
        )
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ApiError(409, "MEMBERSHIP_PLAN_EXISTS", "套餐等级已被使用") from exc
        await session.refresh(plan)
        entitlements = await plan_entitlement_map(session, [plan.id])
    return {"plan": plan_payload(plan, entitlements[plan.id])}


@router.post("/api/v1/admin/membership-plans/{plan_id}/activate")
async def activate_plan(
    plan_id: uuid.UUID,
    payload: ReasonRequest,
    request: Request,
    principal: MembershipManager,
) -> dict[str, Any]:
    return await set_plan_status(plan_id, "active", payload.reason, request, principal)


@router.post("/api/v1/admin/membership-plans/{plan_id}/deactivate")
async def deactivate_plan(
    plan_id: uuid.UUID,
    payload: ReasonRequest,
    request: Request,
    principal: MembershipManager,
) -> dict[str, Any]:
    return await set_plan_status(plan_id, "inactive", payload.reason, request, principal)


async def set_plan_status(
    plan_id: uuid.UUID,
    new_status: str,
    reason: str,
    request: Request,
    principal: Principal,
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        plan = await get_plan_or_404(session, plan_id, for_update=True)
        if plan.code == "free" and new_status != "active":
            raise ApiError(409, "DEFAULT_PLAN_REQUIRED", "Free 套餐不能停用")
        plan.status = new_status
        service.record_audit(
            session,
            action=f"plan.{new_status}",
            aggregate_type="membership_plan",
            aggregate_id=plan.id,
            actor_user_id=principal.user_id,
            subject_user_id=None,
            request_id=request_id(request),
            details={"reason": reason},
        )
        await session.commit()
        await session.refresh(plan)
        entitlements = await plan_entitlement_map(session, [plan.id])
    return {"plan": plan_payload(plan, entitlements[plan.id])}


@router.get("/api/v1/admin/users/{user_id}/memberships")
async def list_user_memberships(
    user_id: uuid.UUID, request: Request, _principal: MembershipReader
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        user = await session.get(User, user_id)
        if user is None or user.deleted_at is not None:
            raise ApiError(404, "USER_NOT_FOUND", "用户不存在")
        memberships = list(
            (
                await session.scalars(
                    select(UserMembership)
                    .where(UserMembership.user_id == user_id)
                    .order_by(UserMembership.created_at.desc())
                )
            ).all()
        )
        plan_ids = {membership.plan_id for membership in memberships}
        plans = (
            {
                plan.id: plan
                for plan in (
                    await session.scalars(
                        select(MembershipPlan).where(MembershipPlan.id.in_(plan_ids))
                    )
                ).all()
            }
            if plan_ids
            else {}
        )
        membership_ids = [membership.id for membership in memberships]
        events = (
            list(
                (
                    await session.scalars(
                        select(MembershipEvent)
                        .where(MembershipEvent.membership_id.in_(membership_ids))
                        .order_by(MembershipEvent.created_at.desc())
                    )
                ).all()
            )
            if membership_ids
            else []
        )
    return {
        "items": [membership_payload(item, plans.get(item.plan_id)) for item in memberships],
        "events": [event_payload(event) for event in events],
    }


@router.post(
    "/api/v1/admin/users/{user_id}/memberships",
    status_code=status.HTTP_201_CREATED,
)
async def assign_user_membership(
    user_id: uuid.UUID,
    payload: AssignMembershipRequest,
    request: Request,
    principal: MembershipManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    key = clean_idempotency_key(idempotency_key)
    fingerprint = service.request_fingerprint("assign", user_id, payload.model_dump(mode="json"))
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        membership = await service.assign_membership(
            session,
            user_id=user_id,
            plan_id=payload.plan_id,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            actor_user_id=principal.user_id,
            reason=payload.reason,
            idempotency_key=key,
            request_fingerprint=fingerprint,
            request_id=request_id(request),
        )
        await commit_membership_change(session)
        await session.refresh(membership)
        plan = await session.get(MembershipPlan, membership.plan_id)
    return {"membership": membership_payload(membership, plan)}


@router.post("/api/v1/admin/memberships/{membership_id}/renew")
async def renew_membership(
    membership_id: uuid.UUID,
    payload: RenewMembershipRequest,
    request: Request,
    principal: MembershipManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    key = clean_idempotency_key(idempotency_key)
    fingerprint = service.request_fingerprint(
        "renew", membership_id, payload.model_dump(mode="json")
    )
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        membership = await service.renew_membership(
            session,
            membership_id=membership_id,
            ends_at=payload.ends_at,
            actor_user_id=principal.user_id,
            reason=payload.reason,
            idempotency_key=key,
            request_fingerprint=fingerprint,
            request_id=request_id(request),
        )
        await commit_membership_change(session)
        await session.refresh(membership)
        plan = await session.get(MembershipPlan, membership.plan_id)
    return {"membership": membership_payload(membership, plan)}


@router.post("/api/v1/admin/memberships/{membership_id}/change-plan")
async def change_membership_plan(
    membership_id: uuid.UUID,
    payload: ChangePlanRequest,
    request: Request,
    principal: MembershipManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    key = clean_idempotency_key(idempotency_key)
    fingerprint = service.request_fingerprint(
        "change-plan", membership_id, payload.model_dump(mode="json")
    )
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        membership = await service.change_plan(
            session,
            membership_id=membership_id,
            target_plan_id=payload.target_plan_id,
            effective_mode=payload.effective_mode,
            ends_at=payload.ends_at,
            actor_user_id=principal.user_id,
            reason=payload.reason,
            idempotency_key=key,
            request_fingerprint=fingerprint,
            request_id=request_id(request),
        )
        await commit_membership_change(session)
        await session.refresh(membership)
        plan = await session.get(MembershipPlan, membership.plan_id)
    return {"membership": membership_payload(membership, plan)}


@router.post("/api/v1/admin/memberships/{membership_id}/cancel")
async def cancel_membership(
    membership_id: uuid.UUID,
    payload: CancelMembershipRequest,
    request: Request,
    principal: MembershipManager,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    key = clean_idempotency_key(idempotency_key)
    fingerprint = service.request_fingerprint(
        "cancel", membership_id, payload.model_dump(mode="json")
    )
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        membership = await service.cancel_membership(
            session,
            membership_id=membership_id,
            effective_mode=payload.effective_mode,
            actor_user_id=principal.user_id,
            reason=payload.reason,
            idempotency_key=key,
            request_fingerprint=fingerprint,
            request_id=request_id(request),
        )
        await commit_membership_change(session)
        await session.refresh(membership)
        plan = await session.get(MembershipPlan, membership.plan_id)
    return {"membership": membership_payload(membership, plan)}
