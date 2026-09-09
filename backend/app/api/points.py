from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, asc, desc, or_, select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import Principal, require_permission
from app.api.errors import ApiError
from app.domain.ids import uuid7
from app.domain.points import POINT_ENTRY_TYPES
from app.repositories.models import (
    PointAccount,
    PointAdjustmentRequest,
    PointTransaction,
    User,
)
from app.services.configuration import runtime_config_value
from app.services.points import PointService
from app.services.rbac import active_super_admin_ids

router = APIRouter(tags=["points"])
PointsOwner = Annotated[Principal, Depends(require_permission("points.read_own"))]
PointsReader = Annotated[Principal, Depends(require_permission("points.read"))]
PointsAdjuster = Annotated[Principal, Depends(require_permission("points.adjust"))]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]
service = PointService()


class AdjustmentRequest(BaseModel):
    amount: int = Field(strict=True, ge=-1_000_000_000, le=1_000_000_000)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("amount")
    @classmethod
    def reject_zero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("调整积分不能为零")
        return value

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("必须填写调整原因")
        return normalized


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("必须填写操作原因")
        return normalized


def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or uuid7().hex


def clean_idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ApiError(422, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key 不能为空")
    return normalized


def account_payload(account: PointAccount, user: User | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": account.id,
        "user_id": account.user_id,
        "balance": account.balance,
        "lifetime_earned": account.lifetime_earned,
        "lifetime_spent": account.lifetime_spent,
        "version": account.version,
        "status": account.status,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }
    if user is not None:
        payload["user"] = {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "status": user.status,
        }
    return payload


def transaction_payload(transaction: PointTransaction) -> dict[str, Any]:
    return {
        "id": transaction.id,
        "account_id": transaction.account_id,
        "user_id": transaction.user_id,
        "entry_type": transaction.entry_type,
        "delta": transaction.delta,
        "balance_before": transaction.balance_before,
        "balance_after": transaction.balance_after,
        "reference_type": transaction.reference_type,
        "reference_id": transaction.reference_id,
        "description": transaction.description,
        "metadata": transaction.transaction_metadata,
        "actor_user_id": transaction.actor_user_id,
        "created_at": transaction.created_at,
    }


def adjustment_payload(adjustment: PointAdjustmentRequest) -> dict[str, Any]:
    return {
        "id": adjustment.id,
        "user_id": adjustment.user_id,
        "amount": adjustment.amount,
        "reason": adjustment.reason,
        "status": adjustment.status,
        "requested_by": adjustment.requested_by,
        "approved_by": adjustment.approved_by,
        "transaction_id": adjustment.transaction_id,
        "created_at": adjustment.created_at,
        "reviewed_at": adjustment.reviewed_at,
    }


async def transaction_page(
    session,
    *,
    user_id: uuid.UUID | None,
    cursor: uuid.UUID | None,
    limit: int,
    entry_type: str | None = None,
    reference_type: str | None = None,
    order: Literal["asc", "desc"] = "desc",
) -> tuple[list[PointTransaction], str | None]:
    statement = select(PointTransaction)
    if user_id is not None:
        statement = statement.where(PointTransaction.user_id == user_id)
    if entry_type is not None:
        if entry_type not in POINT_ENTRY_TYPES:
            raise ApiError(422, "VALIDATION_ERROR", "无效的积分流水类型")
        statement = statement.where(PointTransaction.entry_type == entry_type)
    if reference_type is not None:
        statement = statement.where(PointTransaction.reference_type == reference_type)
    if cursor is not None:
        anchor = await session.get(PointTransaction, cursor)
        if anchor is None or (user_id is not None and anchor.user_id != user_id):
            raise ApiError(422, "INVALID_CURSOR", "分页游标无效")
        if order == "desc":
            statement = statement.where(
                or_(
                    PointTransaction.created_at < anchor.created_at,
                    and_(
                        PointTransaction.created_at == anchor.created_at,
                        PointTransaction.id < anchor.id,
                    ),
                )
            )
        else:
            statement = statement.where(
                or_(
                    PointTransaction.created_at > anchor.created_at,
                    and_(
                        PointTransaction.created_at == anchor.created_at,
                        PointTransaction.id > anchor.id,
                    ),
                )
            )
    direction = desc if order == "desc" else asc
    rows = list(
        (
            await session.scalars(
                statement.order_by(
                    direction(PointTransaction.created_at), direction(PointTransaction.id)
                ).limit(limit + 1)
            )
        ).all()
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    return items, str(items[-1].id) if has_more and items else None


async def ensure_user_or_404(session, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise ApiError(404, "USER_NOT_FOUND", "用户不存在")
    return user


async def commit_point_change(session) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(409, "POINT_TRANSACTION_CONFLICT", "积分状态已变化，请刷新后重试") from exc


@router.get("/api/v1/points/balance")
async def get_balance(request: Request, principal: PointsOwner) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    onboarding_points = int(
        await runtime_config_value(
            request.app.state.runtime_services,
            "general",
            "default_points",
            request.app.state.settings.onboarding_points,
        )
    )
    async with database.session_factory() as session:
        await service.ensure_onboarding_grant(
            session,
            principal.user_id,
            points=onboarding_points,
            request_id=request_id(request),
        )
        await commit_point_change(session)
        account = (
            await session.scalars(
                select(PointAccount).where(PointAccount.user_id == principal.user_id)
            )
        ).one()
    return {"account": account_payload(account)}


@router.get("/api/v1/points/transactions")
async def list_my_transactions(
    request: Request,
    principal: PointsOwner,
    cursor: uuid.UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    onboarding_points = int(
        await runtime_config_value(
            request.app.state.runtime_services,
            "general",
            "default_points",
            request.app.state.settings.onboarding_points,
        )
    )
    async with database.session_factory() as session:
        await service.ensure_onboarding_grant(
            session,
            principal.user_id,
            points=onboarding_points,
            request_id=request_id(request),
        )
        await commit_point_change(session)
        items, next_cursor = await transaction_page(
            session,
            user_id=principal.user_id,
            cursor=cursor,
            limit=limit,
        )
    return {
        "items": [transaction_payload(item) for item in items],
        "next_cursor": next_cursor,
    }


@router.get("/api/v1/admin/points/accounts")
async def list_accounts(
    request: Request,
    _principal: PointsReader,
    cursor: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    status_filter: str | None = Query(default=None, alias="status"),
) -> dict[str, Any]:
    if status_filter not in {None, "active", "frozen"}:
        raise ApiError(422, "VALIDATION_ERROR", "无效的积分账户状态")
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        statement = select(PointAccount, User).join(User, User.id == PointAccount.user_id)
        if status_filter:
            statement = statement.where(PointAccount.status == status_filter)
        if cursor is not None:
            anchor = await session.get(PointAccount, cursor)
            if anchor is None:
                raise ApiError(422, "INVALID_CURSOR", "分页游标无效")
            statement = statement.where(
                or_(
                    PointAccount.created_at < anchor.created_at,
                    and_(
                        PointAccount.created_at == anchor.created_at,
                        PointAccount.id < anchor.id,
                    ),
                )
            )
        rows = list(
            (
                await session.execute(
                    statement.order_by(
                        PointAccount.created_at.desc(), PointAccount.id.desc()
                    ).limit(limit + 1)
                )
            ).all()
        )
    has_more = len(rows) > limit
    items = rows[:limit]
    return {
        "items": [account_payload(account, user) for account, user in items],
        "next_cursor": str(items[-1][0].id) if has_more and items else None,
    }


@router.get("/api/v1/admin/points/transactions")
async def list_admin_transactions(
    request: Request,
    _principal: PointsReader,
    user_id: uuid.UUID | None = None,
    entry_type: str | None = None,
    reference_type: str | None = Query(default=None, max_length=64),
    cursor: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    order: Literal["asc", "desc"] = "desc",
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        items, next_cursor = await transaction_page(
            session,
            user_id=user_id,
            cursor=cursor,
            limit=limit,
            entry_type=entry_type,
            reference_type=reference_type,
            order=order,
        )
    return {
        "items": [transaction_payload(item) for item in items],
        "next_cursor": next_cursor,
    }


@router.get("/api/v1/admin/users/{user_id}/points")
async def get_user_points(
    user_id: uuid.UUID,
    request: Request,
    _principal: PointsReader,
    cursor: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    onboarding_points = int(
        await runtime_config_value(
            request.app.state.runtime_services,
            "general",
            "default_points",
            request.app.state.settings.onboarding_points,
        )
    )
    async with database.session_factory() as session:
        user = await ensure_user_or_404(session, user_id)
        if user.password_hash is not None:
            await service.ensure_onboarding_grant(
                session,
                user.id,
                points=onboarding_points,
                request_id=request_id(request),
            )
        else:
            await service.ensure_account(session, user.id)
        await commit_point_change(session)
        account = (
            await session.scalars(select(PointAccount).where(PointAccount.user_id == user.id))
        ).one()
        items, next_cursor = await transaction_page(
            session,
            user_id=user.id,
            cursor=cursor,
            limit=limit,
        )
    return {
        "account": account_payload(account, user),
        "transactions": [transaction_payload(item) for item in items],
        "next_cursor": next_cursor,
    }


@router.post(
    "/api/v1/admin/users/{user_id}/points/adjustments",
    status_code=status.HTTP_201_CREATED,
)
async def create_adjustment(
    user_id: uuid.UUID,
    payload: AdjustmentRequest,
    request: Request,
    principal: PointsAdjuster,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    key = clean_idempotency_key(idempotency_key)
    fingerprint = service.request_fingerprint(
        "point-adjustment",
        user_id,
        payload.model_dump(mode="json"),
    )
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        try:
            adjustment = await service.create_adjustment(
                session,
                user_id=user_id,
                amount=payload.amount,
                reason=payload.reason,
                requested_by=principal.user_id,
                idempotency_key=key,
                request_fingerprint=fingerprint,
                approval_threshold=(
                    None
                    if principal.user_id in await active_super_admin_ids(session)
                    else request.app.state.settings.point_adjustment_approval_threshold
                ),
                request_id=request_id(request),
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ApiError(
                409, "POINT_ADJUSTMENT_CONFLICT", "积分调整状态已变化，请刷新后重试"
            ) from exc
        await session.refresh(adjustment)
    return {"adjustment": adjustment_payload(adjustment)}


@router.get("/api/v1/admin/point-adjustments")
async def list_adjustments(
    request: Request,
    _principal: PointsReader,
    status_filter: str | None = Query(default=None, alias="status"),
    cursor: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    if status_filter not in {None, "pending", "approved", "rejected", "applied"}:
        raise ApiError(422, "VALIDATION_ERROR", "无效的积分调整状态")
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        statement = select(PointAdjustmentRequest)
        if status_filter:
            statement = statement.where(PointAdjustmentRequest.status == status_filter)
        if cursor is not None:
            anchor = await session.get(PointAdjustmentRequest, cursor)
            if anchor is None:
                raise ApiError(422, "INVALID_CURSOR", "分页游标无效")
            statement = statement.where(
                or_(
                    PointAdjustmentRequest.created_at < anchor.created_at,
                    and_(
                        PointAdjustmentRequest.created_at == anchor.created_at,
                        PointAdjustmentRequest.id < anchor.id,
                    ),
                )
            )
        rows = list(
            (
                await session.scalars(
                    statement.order_by(
                        PointAdjustmentRequest.created_at.desc(),
                        PointAdjustmentRequest.id.desc(),
                    ).limit(limit + 1)
                )
            ).all()
        )
    has_more = len(rows) > limit
    items = rows[:limit]
    return {
        "items": [adjustment_payload(item) for item in items],
        "next_cursor": str(items[-1].id) if has_more and items else None,
    }


@router.post("/api/v1/admin/point-adjustments/{adjustment_id}/approve")
async def approve_adjustment(
    adjustment_id: uuid.UUID,
    payload: ReasonRequest,
    request: Request,
    principal: PointsAdjuster,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    return await review_adjustment(
        adjustment_id,
        "approve",
        payload,
        request,
        principal,
        idempotency_key,
    )


@router.post("/api/v1/admin/point-adjustments/{adjustment_id}/reject")
async def reject_adjustment(
    adjustment_id: uuid.UUID,
    payload: ReasonRequest,
    request: Request,
    principal: PointsAdjuster,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    return await review_adjustment(
        adjustment_id,
        "reject",
        payload,
        request,
        principal,
        idempotency_key,
    )


async def review_adjustment(
    adjustment_id: uuid.UUID,
    decision: str,
    payload: ReasonRequest,
    request: Request,
    principal: Principal,
    idempotency_key: str,
) -> dict[str, Any]:
    key = clean_idempotency_key(idempotency_key)
    fingerprint = service.request_fingerprint(
        f"point-adjustment-{decision}",
        adjustment_id,
        payload.model_dump(mode="json"),
    )
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        try:
            adjustment = await service.review_adjustment(
                session,
                adjustment_id=adjustment_id,
                decision=decision,
                reason=payload.reason,
                reviewed_by=principal.user_id,
                idempotency_key=key,
                review_fingerprint=fingerprint,
                request_id=request_id(request),
                allow_self_review=principal.user_id in await active_super_admin_ids(session),
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ApiError(
                409, "POINT_ADJUSTMENT_CONFLICT", "积分调整状态已变化，请刷新后重试"
            ) from exc
        await session.refresh(adjustment)
    return {"adjustment": adjustment_payload(adjustment)}


@router.post("/api/v1/admin/point-transactions/{transaction_id}/reverse")
async def reverse_transaction(
    transaction_id: uuid.UUID,
    payload: ReasonRequest,
    request: Request,
    principal: PointsAdjuster,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    key = clean_idempotency_key(idempotency_key)
    fingerprint = service.request_fingerprint(
        "point-transaction-reverse",
        transaction_id,
        payload.model_dump(mode="json"),
    )
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        try:
            transaction = await service.reverse_transaction(
                session,
                transaction_id=transaction_id,
                actor_user_id=principal.user_id,
                reason=payload.reason,
                idempotency_key=key,
                request_fingerprint=fingerprint,
                request_id=request_id(request),
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ApiError(
                409, "POINT_TRANSACTION_CONFLICT", "积分状态已变化，请刷新后重试"
            ) from exc
        await session.refresh(transaction)
    return {"transaction": transaction_payload(transaction)}
