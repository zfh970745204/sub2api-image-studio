from __future__ import annotations

import calendar
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError
from app.domain.ids import uuid7
from app.domain.memberships import MEMBERSHIP_PLAN_SEEDS
from app.repositories.models import (
    ConfigGroup,
    ConfigVersion,
    MembershipEvent,
    MembershipPlan,
    OutboxEvent,
    PlanEntitlement,
    User,
    UserMembership,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class EntitlementSnapshot:
    membership_id: uuid.UUID
    plan_id: uuid.UUID
    plan_code: str
    discount_bps: int
    max_concurrent_jobs: int
    max_upload_mb: int
    max_image_megapixels: int
    retention_days: int
    periodic_points: int
    starts_at: datetime
    ends_at: datetime | None
    entitlements: dict[str, Any]


async def sync_builtin_membership_plans(session: AsyncSession) -> dict[str, MembershipPlan]:
    plans = {plan.code: plan for plan in (await session.scalars(select(MembershipPlan))).all()}
    for seed in MEMBERSHIP_PLAN_SEEDS:
        if seed.code in plans:
            continue
        plan = MembershipPlan(
            id=uuid7(),
            code=seed.code,
            name=seed.name,
            description=seed.description,
            level=seed.level,
            status="active",
            billing_period=seed.billing_period,
            periodic_points=seed.periodic_points,
            operation_discount_bps=seed.operation_discount_bps,
            max_concurrent_jobs=seed.max_concurrent_jobs,
            max_upload_mb=seed.max_upload_mb,
            max_image_megapixels=seed.max_image_megapixels,
            asset_retention_days=seed.asset_retention_days,
            display_order=seed.display_order,
        )
        session.add(plan)
        plans[seed.code] = plan
    await session.flush()
    return plans


class EntitlementService:
    async def current_snapshot(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        *,
        now: datetime | None = None,
        request_id: str = "entitlement-read-repair",
    ) -> EntitlementSnapshot:
        membership = await self.reconcile_user(
            session,
            user_id,
            now=now,
            request_id=request_id,
        )
        plan = await session.get(MembershipPlan, membership.plan_id)
        if plan is None:
            raise ApiError(500, "MEMBERSHIP_PLAN_MISSING", "当前会员套餐配置缺失")
        snapshot = membership.entitlement_snapshot
        return EntitlementSnapshot(
            membership_id=membership.id,
            plan_id=plan.id,
            plan_code=str(snapshot["plan_code"]),
            discount_bps=int(snapshot["discount_bps"]),
            max_concurrent_jobs=int(snapshot["max_concurrent_jobs"]),
            max_upload_mb=int(snapshot["max_upload_mb"]),
            max_image_megapixels=int(snapshot["max_image_megapixels"]),
            retention_days=int(snapshot["retention_days"]),
            periodic_points=int(snapshot["periodic_points"]),
            starts_at=membership.starts_at,
            ends_at=membership.ends_at,
            entitlements=dict(snapshot.get("entitlements", {})),
        )

    async def reconcile_user(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        *,
        now: datetime | None = None,
        request_id: str = "membership-scheduler",
    ) -> UserMembership:
        current_time = now or utcnow()
        await self._lock_user(session, user_id)
        active = (
            await session.scalars(
                select(UserMembership)
                .where(
                    UserMembership.user_id == user_id,
                    UserMembership.status == "active",
                )
                .with_for_update()
            )
        ).one_or_none()
        previous_plan_id: uuid.UUID | None = None
        if active is not None and as_utc(active.starts_at) > current_time:
            active.status = "scheduled"
            active = None
            await session.flush()
        if (
            active is not None
            and active.ends_at is not None
            and as_utc(active.ends_at) <= current_time
        ):
            previous_plan_id = active.plan_id
            active.status = "expired"
            await self._record_event(
                session,
                membership=active,
                event_type="expired",
                old_plan_id=active.plan_id,
                new_plan_id=None,
                effective_at=active.ends_at,
                actor_user_id=None,
                reason="会员周期到期",
                request_id=request_id,
            )
            await session.flush()
            active = None
        scheduled = (
            await session.scalars(
                select(UserMembership)
                .where(
                    UserMembership.user_id == user_id,
                    UserMembership.status == "scheduled",
                    UserMembership.starts_at <= current_time,
                )
                .order_by(UserMembership.starts_at, UserMembership.created_at)
                .limit(1)
                .with_for_update()
            )
        ).one_or_none()
        if active is not None and scheduled is None:
            return active
        if active is not None and scheduled is not None:
            previous_plan_id = active.plan_id
            active.status = "cancelled"
            await self._record_event(
                session,
                membership=active,
                event_type="cancelled",
                old_plan_id=active.plan_id,
                new_plan_id=scheduled.plan_id,
                effective_at=scheduled.starts_at,
                actor_user_id=None,
                reason="待生效会员变更已执行",
                request_id=request_id,
            )
            await session.flush()
        if scheduled is not None:
            scheduled.status = "active"
            await self._record_event(
                session,
                membership=scheduled,
                event_type="activated",
                old_plan_id=previous_plan_id,
                new_plan_id=scheduled.plan_id,
                effective_at=scheduled.starts_at,
                actor_user_id=None,
                reason=scheduled.reason,
                request_id=request_id,
            )
            await session.flush()
            return scheduled
        return await self.ensure_default_membership(
            session,
            user_id,
            now=current_time,
            previous_plan_id=previous_plan_id,
            request_id=request_id,
        )

    async def ensure_default_membership(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        *,
        now: datetime | None = None,
        assigned_by: uuid.UUID | None = None,
        previous_plan_id: uuid.UUID | None = None,
        request_id: str = "default-membership",
    ) -> UserMembership:
        current_time = now or utcnow()
        active = (
            await session.scalars(
                select(UserMembership).where(
                    UserMembership.user_id == user_id,
                    UserMembership.status == "active",
                )
            )
        ).one_or_none()
        if active is not None:
            return active
        plans = await sync_builtin_membership_plans(session)
        plan = plans["free"]
        membership_count = await session.scalar(
            select(func.count(UserMembership.id)).where(UserMembership.user_id == user_id)
        )
        if not membership_count:
            configured_values = await session.scalar(
                select(ConfigVersion.values)
                .join(ConfigGroup, ConfigGroup.id == ConfigVersion.group_id)
                .where(
                    ConfigGroup.code == "general",
                    ConfigVersion.version == ConfigGroup.active_version,
                    ConfigVersion.status == "active",
                )
            )
            if isinstance(configured_values, dict):
                candidate = plans.get(str(configured_values.get("default_membership_code")))
                if candidate is not None and candidate.status == "active":
                    plan = candidate
        ends_at = None
        if plan.billing_period in {"month", "year"}:
            month_index = current_time.year * 12 + current_time.month - 1
            month_index += 1 if plan.billing_period == "month" else 12
            year, month = divmod(month_index, 12)
            ends_at = current_time.replace(
                year=year,
                month=month + 1,
                day=min(current_time.day, calendar.monthrange(year, month + 1)[1]),
            )
        snapshot = await self._snapshot_for_plan(session, plan)
        membership = UserMembership(
            id=uuid7(),
            user_id=user_id,
            plan_id=plan.id,
            status="active",
            starts_at=current_time,
            ends_at=ends_at,
            auto_renew=False,
            source="system",
            assigned_by=assigned_by,
            reason=f"默认 {plan.name} 会员",
            entitlement_snapshot=snapshot,
        )
        session.add(membership)
        await session.flush()
        await self._record_event(
            session,
            membership=membership,
            event_type="activated",
            old_plan_id=previous_plan_id,
            new_plan_id=membership.plan_id,
            effective_at=current_time,
            actor_user_id=assigned_by,
            reason=membership.reason,
            request_id=request_id,
        )
        return membership

    async def assign_membership(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        plan_id: uuid.UUID,
        starts_at: datetime | None,
        ends_at: datetime | None,
        actor_user_id: uuid.UUID,
        reason: str,
        idempotency_key: str,
        request_fingerprint: str,
        request_id: str,
    ) -> UserMembership:
        replay = await self._prepare_command(
            session, actor_user_id, idempotency_key, request_fingerprint
        )
        if replay is not None:
            return replay
        await self._lock_user(session, user_id)
        plan = await self._active_plan(session, plan_id)
        current_time = utcnow()
        effective_at = as_utc(starts_at) if starts_at is not None else current_time
        normalized_end = as_utc(ends_at) if ends_at is not None else None
        self._validate_period(plan, effective_at, normalized_end)
        current = await self._active_membership(session, user_id)
        if current is not None and current.plan_id == plan.id and effective_at <= current_time:
            raise ApiError(409, "MEMBERSHIP_ALREADY_ACTIVE", "用户已经处于该会员套餐")
        scheduled = await self._scheduled_membership(session, user_id)
        if effective_at > current_time and scheduled is not None:
            raise ApiError(409, "MEMBERSHIP_CHANGE_ALREADY_SCHEDULED", "用户已有待生效会员变更")
        if effective_at <= current_time and current is not None:
            current.status = "cancelled"
            await self._record_event(
                session,
                membership=current,
                event_type="cancelled",
                old_plan_id=current.plan_id,
                new_plan_id=plan.id,
                effective_at=effective_at,
                actor_user_id=actor_user_id,
                reason=reason,
                request_id=request_id,
            )
            await session.flush()
        membership = UserMembership(
            id=uuid7(),
            user_id=user_id,
            plan_id=plan.id,
            status="scheduled" if effective_at > current_time else "active",
            starts_at=effective_at,
            ends_at=normalized_end,
            auto_renew=False,
            source="admin",
            assigned_by=actor_user_id,
            reason=reason,
            entitlement_snapshot=await self._snapshot_for_plan(session, plan),
        )
        session.add(membership)
        await session.flush()
        await self._record_event(
            session,
            membership=membership,
            event_type="created",
            old_plan_id=current.plan_id if current is not None else None,
            new_plan_id=plan.id,
            effective_at=effective_at,
            actor_user_id=actor_user_id,
            reason=reason,
            request_id=request_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if membership.status == "active":
            await self._record_event(
                session,
                membership=membership,
                event_type="activated",
                old_plan_id=current.plan_id if current is not None else None,
                new_plan_id=plan.id,
                effective_at=effective_at,
                actor_user_id=actor_user_id,
                reason=reason,
                request_id=request_id,
            )
        return membership

    async def renew_membership(
        self,
        session: AsyncSession,
        *,
        membership_id: uuid.UUID,
        ends_at: datetime,
        actor_user_id: uuid.UUID,
        reason: str,
        idempotency_key: str,
        request_fingerprint: str,
        request_id: str,
    ) -> UserMembership:
        replay = await self._prepare_command(
            session, actor_user_id, idempotency_key, request_fingerprint
        )
        if replay is not None:
            return replay
        membership = await self._membership_for_update(session, membership_id)
        await self._lock_user(session, membership.user_id)
        if membership.status not in {"active", "scheduled"}:
            raise ApiError(409, "MEMBERSHIP_NOT_RENEWABLE", "只有有效或待生效会员可以续期")
        plan = await session.get(MembershipPlan, membership.plan_id)
        if plan is None or plan.code == "free" or membership.ends_at is None:
            raise ApiError(409, "MEMBERSHIP_NOT_RENEWABLE", "Free 会员无需续期")
        normalized_end = as_utc(ends_at)
        if normalized_end <= as_utc(membership.ends_at):
            raise ApiError(422, "INVALID_MEMBERSHIP_PERIOD", "续期时间必须晚于当前到期时间")
        if membership.status == "active" and await self._scheduled_membership(
            session, membership.user_id
        ):
            raise ApiError(409, "MEMBERSHIP_CHANGE_ALREADY_SCHEDULED", "请先处理待生效会员变更")
        membership.ends_at = normalized_end
        await self._record_event(
            session,
            membership=membership,
            event_type="renewed",
            old_plan_id=plan.id,
            new_plan_id=plan.id,
            effective_at=utcnow(),
            actor_user_id=actor_user_id,
            reason=reason,
            request_id=request_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        return membership

    async def change_plan(
        self,
        session: AsyncSession,
        *,
        membership_id: uuid.UUID,
        target_plan_id: uuid.UUID,
        effective_mode: str,
        ends_at: datetime | None,
        actor_user_id: uuid.UUID,
        reason: str,
        idempotency_key: str,
        request_fingerprint: str,
        request_id: str,
    ) -> UserMembership:
        replay = await self._prepare_command(
            session, actor_user_id, idempotency_key, request_fingerprint
        )
        if replay is not None:
            return replay
        current = await self._membership_for_update(session, membership_id)
        await self._lock_user(session, current.user_id)
        if current.status != "active":
            raise ApiError(409, "MEMBERSHIP_NOT_ACTIVE", "只能变更当前有效会员")
        target = await self._active_plan(session, target_plan_id)
        old_plan = await session.get(MembershipPlan, current.plan_id)
        if old_plan is None:
            raise ApiError(500, "MEMBERSHIP_PLAN_MISSING", "当前会员套餐配置缺失")
        if target.id == old_plan.id:
            raise ApiError(409, "MEMBERSHIP_ALREADY_ACTIVE", "用户已经处于该会员套餐")
        if await self._scheduled_membership(session, current.user_id) is not None:
            raise ApiError(409, "MEMBERSHIP_CHANGE_ALREADY_SCHEDULED", "用户已有待生效会员变更")
        current_time = utcnow()
        if effective_mode == "period_end":
            if current.ends_at is None or as_utc(current.ends_at) <= current_time:
                raise ApiError(
                    409, "MEMBERSHIP_HAS_NO_PERIOD_END", "当前会员没有可用的周期结束时间"
                )
            effective_at = as_utc(current.ends_at)
        else:
            effective_at = current_time
        normalized_end = as_utc(ends_at) if ends_at is not None else None
        self._validate_period(target, effective_at, normalized_end)
        if effective_mode == "now":
            current.status = "cancelled"
            await self._record_event(
                session,
                membership=current,
                event_type="cancelled",
                old_plan_id=old_plan.id,
                new_plan_id=target.id,
                effective_at=effective_at,
                actor_user_id=actor_user_id,
                reason=reason,
                request_id=request_id,
            )
            await session.flush()
        membership = UserMembership(
            id=uuid7(),
            user_id=current.user_id,
            plan_id=target.id,
            status="active" if effective_mode == "now" else "scheduled",
            starts_at=effective_at,
            ends_at=normalized_end,
            auto_renew=False,
            source="admin",
            assigned_by=actor_user_id,
            reason=reason,
            entitlement_snapshot=await self._snapshot_for_plan(session, target),
        )
        session.add(membership)
        await session.flush()
        event_type = "upgraded" if target.level > old_plan.level else "downgraded"
        await self._record_event(
            session,
            membership=membership,
            event_type=event_type,
            old_plan_id=old_plan.id,
            new_plan_id=target.id,
            effective_at=effective_at,
            actor_user_id=actor_user_id,
            reason=reason,
            request_id=request_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if membership.status == "active":
            await self._record_event(
                session,
                membership=membership,
                event_type="activated",
                old_plan_id=old_plan.id,
                new_plan_id=target.id,
                effective_at=effective_at,
                actor_user_id=actor_user_id,
                reason=reason,
                request_id=request_id,
            )
        return membership

    async def cancel_membership(
        self,
        session: AsyncSession,
        *,
        membership_id: uuid.UUID,
        effective_mode: str,
        actor_user_id: uuid.UUID,
        reason: str,
        idempotency_key: str,
        request_fingerprint: str,
        request_id: str,
    ) -> UserMembership:
        replay = await self._prepare_command(
            session, actor_user_id, idempotency_key, request_fingerprint
        )
        if replay is not None:
            return replay
        membership = await self._membership_for_update(session, membership_id)
        await self._lock_user(session, membership.user_id)
        plan = await session.get(MembershipPlan, membership.plan_id)
        if plan is None:
            raise ApiError(500, "MEMBERSHIP_PLAN_MISSING", "当前会员套餐配置缺失")
        if plan.code == "free":
            raise ApiError(409, "DEFAULT_MEMBERSHIP_REQUIRED", "Free 会员不能取消")
        current_time = utcnow()
        if membership.status == "scheduled":
            effective_at = current_time
            membership.status = "cancelled"
        elif membership.status == "active" and effective_mode == "now":
            effective_at = current_time
            membership.status = "cancelled"
            await session.flush()
        elif membership.status == "active" and effective_mode == "period_end":
            if membership.ends_at is None or as_utc(membership.ends_at) <= current_time:
                raise ApiError(
                    409, "MEMBERSHIP_HAS_NO_PERIOD_END", "当前会员没有可用的周期结束时间"
                )
            if await self._scheduled_membership(session, membership.user_id) is not None:
                raise ApiError(409, "MEMBERSHIP_CHANGE_ALREADY_SCHEDULED", "用户已有待生效会员变更")
            effective_at = as_utc(membership.ends_at)
            plans = await sync_builtin_membership_plans(session)
            snapshot = await self._snapshot_for_plan(session, plans["free"])
            session.add(
                UserMembership(
                    id=uuid7(),
                    user_id=membership.user_id,
                    plan_id=plans["free"].id,
                    status="scheduled",
                    starts_at=effective_at,
                    ends_at=None,
                    auto_renew=False,
                    source="admin",
                    assigned_by=actor_user_id,
                    reason=reason,
                    entitlement_snapshot=snapshot,
                )
            )
        else:
            raise ApiError(409, "MEMBERSHIP_NOT_CANCELLABLE", "该会员记录不能取消")
        await self._record_event(
            session,
            membership=membership,
            event_type="cancelled",
            old_plan_id=membership.plan_id,
            new_plan_id=None,
            effective_at=effective_at,
            actor_user_id=actor_user_id,
            reason=reason,
            request_id=request_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if membership.status == "cancelled" and effective_mode == "now":
            await self.ensure_default_membership(
                session,
                membership.user_id,
                now=current_time,
                assigned_by=actor_user_id,
                previous_plan_id=membership.plan_id,
                request_id=request_id,
            )
        return membership

    async def reconcile_due_memberships(
        self,
        session: AsyncSession,
        *,
        now: datetime | None = None,
    ) -> int:
        current_time = now or utcnow()
        expiring = await session.scalars(
            select(UserMembership.user_id).where(
                UserMembership.status == "active",
                UserMembership.ends_at.is_not(None),
                UserMembership.ends_at <= current_time,
            )
        )
        starting = await session.scalars(
            select(UserMembership.user_id).where(
                UserMembership.status == "scheduled",
                UserMembership.starts_at <= current_time,
            )
        )
        user_ids = set(expiring.all()) | set(starting.all())
        for user_id in user_ids:
            await self.reconcile_user(session, user_id, now=current_time)
        return len(user_ids)

    @staticmethod
    def request_fingerprint(operation: str, resource_id: uuid.UUID, payload: Any) -> str:
        encoded = json.dumps(
            {"operation": operation, "resource_id": str(resource_id), "payload": payload},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    async def _prepare_command(
        self,
        session: AsyncSession,
        actor_user_id: uuid.UUID,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> UserMembership | None:
        await self._lock_user(session, actor_user_id)
        event = (
            await session.scalars(
                select(MembershipEvent).where(
                    MembershipEvent.actor_user_id == actor_user_id,
                    MembershipEvent.idempotency_key == idempotency_key,
                )
            )
        ).one_or_none()
        if event is None:
            return None
        if event.request_fingerprint != request_fingerprint:
            raise ApiError(
                409,
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key 已用于不同的会员操作",
            )
        membership = await session.get(UserMembership, event.membership_id)
        if membership is None:
            raise ApiError(500, "IDEMPOTENCY_RECORD_INVALID", "会员幂等记录不完整")
        return membership

    @staticmethod
    async def _lock_user(session: AsyncSession, user_id: uuid.UUID) -> User:
        user = (
            await session.scalars(select(User).where(User.id == user_id).with_for_update())
        ).one_or_none()
        if user is None or user.deleted_at is not None:
            raise ApiError(404, "USER_NOT_FOUND", "用户不存在")
        return user

    @staticmethod
    async def _active_plan(session: AsyncSession, plan_id: uuid.UUID) -> MembershipPlan:
        plan = await session.get(MembershipPlan, plan_id)
        if plan is None:
            raise ApiError(404, "MEMBERSHIP_PLAN_NOT_FOUND", "会员套餐不存在")
        if plan.status != "active":
            raise ApiError(409, "MEMBERSHIP_PLAN_INACTIVE", "会员套餐当前不可分配")
        return plan

    @staticmethod
    async def _active_membership(
        session: AsyncSession, user_id: uuid.UUID
    ) -> UserMembership | None:
        return (
            await session.scalars(
                select(UserMembership)
                .where(
                    UserMembership.user_id == user_id,
                    UserMembership.status == "active",
                )
                .with_for_update()
            )
        ).one_or_none()

    @staticmethod
    async def _scheduled_membership(
        session: AsyncSession, user_id: uuid.UUID
    ) -> UserMembership | None:
        return (
            await session.scalars(
                select(UserMembership)
                .where(
                    UserMembership.user_id == user_id,
                    UserMembership.status == "scheduled",
                )
                .order_by(UserMembership.starts_at)
                .limit(1)
                .with_for_update()
            )
        ).one_or_none()

    @staticmethod
    async def _membership_for_update(
        session: AsyncSession, membership_id: uuid.UUID
    ) -> UserMembership:
        membership = (
            await session.scalars(
                select(UserMembership).where(UserMembership.id == membership_id).with_for_update()
            )
        ).one_or_none()
        if membership is None:
            raise ApiError(404, "MEMBERSHIP_NOT_FOUND", "会员记录不存在")
        return membership

    @staticmethod
    def _validate_period(
        plan: MembershipPlan, starts_at: datetime, ends_at: datetime | None
    ) -> None:
        if plan.billing_period == "none":
            if ends_at is not None:
                raise ApiError(422, "INVALID_MEMBERSHIP_PERIOD", "长期会员不能设置到期时间")
            return
        if ends_at is None:
            raise ApiError(422, "INVALID_MEMBERSHIP_PERIOD", "周期会员必须设置到期时间")
        if ends_at <= starts_at:
            raise ApiError(422, "INVALID_MEMBERSHIP_PERIOD", "到期时间必须晚于生效时间")

    @staticmethod
    async def _snapshot_for_plan(session: AsyncSession, plan: MembershipPlan) -> dict[str, Any]:
        extra = {
            item.entitlement_code: item.value
            for item in (
                await session.scalars(
                    select(PlanEntitlement).where(PlanEntitlement.plan_id == plan.id)
                )
            ).all()
        }
        return {
            "plan_code": plan.code,
            "discount_bps": plan.operation_discount_bps,
            "max_concurrent_jobs": plan.max_concurrent_jobs,
            "max_upload_mb": plan.max_upload_mb,
            "max_image_megapixels": plan.max_image_megapixels,
            "retention_days": plan.asset_retention_days,
            "periodic_points": plan.periodic_points,
            "entitlements": extra,
        }

    async def _record_event(
        self,
        session: AsyncSession,
        *,
        membership: UserMembership,
        event_type: str,
        old_plan_id: uuid.UUID | None,
        new_plan_id: uuid.UUID | None,
        effective_at: datetime,
        actor_user_id: uuid.UUID | None,
        reason: str | None,
        request_id: str,
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
    ) -> MembershipEvent:
        event = MembershipEvent(
            id=uuid7(),
            membership_id=membership.id,
            event_type=event_type,
            old_plan_id=old_plan_id,
            new_plan_id=new_plan_id,
            effective_at=effective_at,
            actor_user_id=actor_user_id,
            reason=reason,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        session.add(event)
        self.record_audit(
            session,
            action=event_type,
            aggregate_type="membership",
            aggregate_id=membership.id,
            actor_user_id=actor_user_id,
            subject_user_id=membership.user_id,
            request_id=request_id,
            details={
                "old_plan_id": str(old_plan_id) if old_plan_id else None,
                "new_plan_id": str(new_plan_id) if new_plan_id else None,
                "effective_at": effective_at.isoformat(),
                "reason": reason,
            },
        )
        return event

    @staticmethod
    def record_audit(
        session: AsyncSession,
        *,
        action: str,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
        subject_user_id: uuid.UUID | None,
        request_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            OutboxEvent(
                id=uuid7(),
                topic="membership.audit",
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                payload={
                    "action": action,
                    "actor_user_id": str(actor_user_id) if actor_user_id else None,
                    "subject_user_id": str(subject_user_id) if subject_user_id else None,
                    "request_id": request_id,
                    "details": details or {},
                },
                status="pending",
                attempts=0,
                available_at=utcnow(),
                version=1,
            )
        )
