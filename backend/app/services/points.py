from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError
from app.domain.ids import uuid7
from app.domain.points import POINT_ENTRY_TYPES
from app.repositories.models import (
    OutboxEvent,
    PointAccount,
    PointAdjustmentRequest,
    PointTransaction,
    User,
)
from app.services.security import SecurityService


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    checked: int
    frozen: int


class PointService:
    async def ensure_account(self, session: AsyncSession, user_id: uuid.UUID) -> PointAccount:
        account = (
            await session.scalars(select(PointAccount).where(PointAccount.user_id == user_id))
        ).one_or_none()
        if account is not None:
            return account
        await self._lock_user(session, user_id)
        account = (
            await session.scalars(select(PointAccount).where(PointAccount.user_id == user_id))
        ).one_or_none()
        if account is None:
            account = PointAccount(
                id=uuid7(),
                user_id=user_id,
                balance=0,
                lifetime_earned=0,
                lifetime_spent=0,
                version=1,
                status="active",
            )
            session.add(account)
            await session.flush()
        return account

    async def ensure_onboarding_grant(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        *,
        points: int,
        request_id: str,
    ) -> PointTransaction:
        existing = (
            await session.scalars(
                select(PointTransaction).where(
                    PointTransaction.user_id == user_id,
                    PointTransaction.entry_type == "grant",
                    PointTransaction.reference_type == "onboarding",
                    PointTransaction.reference_id == user_id,
                )
            )
        ).one_or_none()
        if existing is not None:
            return existing
        fingerprint = self.request_fingerprint(
            "onboarding",
            user_id,
            {"points": points},
        )
        transaction, _ = await self.apply_transaction(
            session,
            user_id=user_id,
            entry_type="grant",
            delta=points,
            reference_type="onboarding",
            reference_id=user_id,
            idempotency_key=f"onboarding:{user_id}",
            request_fingerprint=fingerprint,
            description="首次激活赠送" if points else "首次激活赠送已禁用",
            metadata={"policy": "P0-v1"},
            actor_user_id=None,
            request_id=request_id,
        )
        return transaction

    async def consume_points(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        amount: int,
        job_id: uuid.UUID,
        idempotency_key: str,
        request_fingerprint: str,
        description: str,
        metadata: dict[str, Any] | None,
        request_id: str,
    ) -> PointTransaction:
        if amount < 0:
            raise ValueError("amount must not be negative")
        transaction, _ = await self.apply_transaction(
            session,
            user_id=user_id,
            entry_type="consume",
            delta=-amount,
            reference_type="job",
            reference_id=job_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            description=description,
            metadata=metadata,
            actor_user_id=user_id,
            request_id=request_id,
        )
        return transaction

    async def refund_points(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        amount: int,
        job_id: uuid.UUID,
        idempotency_key: str,
        description: str,
        metadata: dict[str, Any] | None,
        request_id: str,
    ) -> PointTransaction:
        if amount < 0:
            raise ValueError("amount must not be negative")
        charge = (
            await session.scalars(
                select(PointTransaction).where(
                    PointTransaction.user_id == user_id,
                    PointTransaction.entry_type == "consume",
                    PointTransaction.reference_type == "job",
                    PointTransaction.reference_id == job_id,
                )
            )
        ).one_or_none()
        if charge is None:
            raise ApiError(409, "POINT_CHARGE_NOT_FOUND", "任务没有可退款的消费流水")
        if -charge.delta != amount:
            raise ApiError(409, "POINT_REFUND_AMOUNT_MISMATCH", "退款积分必须等于原消费积分")
        fingerprint = self.request_fingerprint(
            "refund",
            job_id,
            {"user_id": str(user_id), "amount": amount},
        )
        transaction, _ = await self.apply_transaction(
            session,
            user_id=user_id,
            entry_type="refund",
            delta=amount,
            reference_type="job",
            reference_id=job_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            description=description,
            metadata=metadata,
            actor_user_id=None,
            request_id=request_id,
        )
        return transaction

    async def apply_transaction(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        entry_type: str,
        delta: int,
        reference_type: str,
        reference_id: uuid.UUID,
        idempotency_key: str,
        request_fingerprint: str | None,
        description: str,
        metadata: dict[str, Any] | None,
        actor_user_id: uuid.UUID | None,
        request_id: str,
    ) -> tuple[PointTransaction, bool]:
        if entry_type not in POINT_ENTRY_TYPES:
            raise ValueError(f"unknown point entry type: {entry_type}")
        self._validate_entry_delta(entry_type, delta)
        key = idempotency_key.strip()
        if not key:
            raise ApiError(422, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key 不能为空")
        if len(key) > 255:
            raise ApiError(422, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key 过长")

        account = await self._account_for_update(session, user_id)
        existing = (
            await session.scalars(
                select(PointTransaction).where(
                    PointTransaction.user_id == user_id,
                    PointTransaction.idempotency_key == key,
                )
            )
        ).one_or_none()
        if existing is not None:
            self._validate_replay(
                existing,
                user_id=user_id,
                entry_type=entry_type,
                delta=delta,
                reference_type=reference_type,
                reference_id=reference_id,
                request_fingerprint=request_fingerprint,
            )
            return existing, False
        business_entry = (
            await session.scalars(
                select(PointTransaction).where(
                    PointTransaction.reference_type == reference_type,
                    PointTransaction.reference_id == reference_id,
                    PointTransaction.entry_type == entry_type,
                )
            )
        ).one_or_none()
        if business_entry is not None:
            self._validate_replay(
                business_entry,
                user_id=user_id,
                entry_type=entry_type,
                delta=delta,
                reference_type=reference_type,
                reference_id=reference_id,
                request_fingerprint=None,
            )
            return business_entry, False
        if account.status != "active":
            raise ApiError(409, "POINT_ACCOUNT_FROZEN", "积分账户已冻结，请联系管理员")

        balance_after = account.balance + delta
        if balance_after < 0:
            raise ApiError(
                409,
                "INSUFFICIENT_POINTS",
                "积分不足",
                {"balance": account.balance, "required": -delta},
            )
        earned_change, spent_change = self._lifetime_changes(
            entry_type=entry_type,
            delta=delta,
            metadata=metadata or {},
        )
        statement = update(PointAccount).where(
            PointAccount.id == account.id,
            PointAccount.status == "active",
        )
        if delta < 0:
            statement = statement.where(PointAccount.balance >= -delta)
        result = await session.execute(
            statement.values(
                balance=PointAccount.balance + delta,
                lifetime_earned=PointAccount.lifetime_earned + earned_change,
                lifetime_spent=PointAccount.lifetime_spent + spent_change,
                version=PointAccount.version + 1,
                updated_at=utcnow(),
            )
            .returning(PointAccount.balance, PointAccount.version)
            .execution_options(synchronize_session=False)
        )
        updated = result.one_or_none()
        if updated is None:
            await session.refresh(account)
            if account.status != "active":
                raise ApiError(409, "POINT_ACCOUNT_FROZEN", "积分账户已冻结，请联系管理员")
            raise ApiError(
                409,
                "INSUFFICIENT_POINTS",
                "积分不足",
                {"balance": account.balance, "required": max(0, -delta)},
            )
        balance_after, _version = updated
        await session.refresh(account)
        now = utcnow()
        if entry_type == "consume":
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            spent_before = await session.scalar(
                select(func.coalesce(func.sum(-PointTransaction.delta), 0)).where(
                    PointTransaction.user_id == user_id,
                    PointTransaction.entry_type == "consume",
                    PointTransaction.created_at >= day_start,
                )
            )
            projected_spend = int(spent_before or 0) - delta
            if int(spent_before or 0) < 1_000 <= projected_spend:
                SecurityService.record_event(
                    session,
                    event_type="daily_points_spend_anomaly",
                    severity="high",
                    user_id=user_id,
                    ip_hash=None,
                    request_id=request_id,
                    details={"threshold": 1_000, "projected_spend": projected_spend},
                )
        transaction = PointTransaction(
            id=uuid7(),
            account_id=account.id,
            user_id=user_id,
            entry_type=entry_type,
            delta=delta,
            balance_before=balance_after - delta,
            balance_after=balance_after,
            reference_type=reference_type,
            reference_id=reference_id,
            idempotency_key=key,
            request_fingerprint=request_fingerprint,
            description=description.strip()[:500],
            transaction_metadata=metadata or {},
            actor_user_id=actor_user_id,
            created_at=now,
        )
        session.add(transaction)
        # Callers assign this ID to job/adjustment foreign keys. Persist the ledger
        # entry first: scalar FK assignments alone do not order ORM mapper flushes.
        # This is still the caller's transaction; a later failure rolls it all back.
        await session.flush()
        self._record_transaction_event(session, transaction, request_id=request_id)
        return transaction, True

    async def create_adjustment(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        amount: int,
        reason: str,
        requested_by: uuid.UUID,
        idempotency_key: str,
        request_fingerprint: str,
        approval_threshold: int | None,
        request_id: str,
    ) -> PointAdjustmentRequest:
        if amount == 0:
            raise ApiError(422, "INVALID_POINT_ADJUSTMENT", "调整积分不能为零")
        await self._lock_user(session, requested_by)
        replay = (
            await session.scalars(
                select(PointAdjustmentRequest).where(
                    PointAdjustmentRequest.requested_by == requested_by,
                    PointAdjustmentRequest.idempotency_key == idempotency_key,
                )
            )
        ).one_or_none()
        if replay is not None:
            if replay.request_fingerprint != request_fingerprint:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key 已用于不同的积分调整",
                )
            return replay
        await self.ensure_account(session, user_id)
        adjustment = PointAdjustmentRequest(
            id=uuid7(),
            user_id=user_id,
            amount=amount,
            reason=reason,
            status="pending",
            requested_by=requested_by,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        session.add(adjustment)
        if approval_threshold is None or abs(amount) < approval_threshold:
            transaction, _ = await self.apply_transaction(
                session,
                user_id=user_id,
                entry_type="adjust",
                delta=amount,
                reference_type="admin_adjustment",
                reference_id=adjustment.id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                description=reason,
                metadata={"requested_by": str(requested_by), "approval_required": False},
                actor_user_id=requested_by,
                request_id=request_id,
            )
            adjustment.status = "applied"
            adjustment.transaction_id = transaction.id
            adjustment.approved_by = requested_by
            adjustment.reviewed_at = utcnow()
        self.record_audit(
            session,
            action="adjustment.applied"
            if adjustment.status == "applied"
            else "adjustment.requested",
            aggregate_type="point_adjustment",
            aggregate_id=adjustment.id,
            actor_user_id=requested_by,
            subject_user_id=user_id,
            request_id=request_id,
            details={
                "amount": amount,
                "reason": reason,
                "approval_required": adjustment.status == "pending",
            },
        )
        return adjustment

    async def review_adjustment(
        self,
        session: AsyncSession,
        *,
        adjustment_id: uuid.UUID,
        decision: str,
        reason: str,
        reviewed_by: uuid.UUID,
        idempotency_key: str,
        review_fingerprint: str,
        request_id: str,
        allow_self_review: bool = False,
    ) -> PointAdjustmentRequest:
        adjustment = (
            await session.scalars(
                select(PointAdjustmentRequest)
                .where(PointAdjustmentRequest.id == adjustment_id)
                .with_for_update()
            )
        ).one_or_none()
        if adjustment is None:
            raise ApiError(404, "POINT_ADJUSTMENT_NOT_FOUND", "积分调整申请不存在")
        if adjustment.review_idempotency_key == idempotency_key:
            if adjustment.review_fingerprint != review_fingerprint:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key 已用于不同的审核操作",
                )
            return adjustment
        if adjustment.status != "pending":
            raise ApiError(409, "POINT_ADJUSTMENT_ALREADY_REVIEWED", "积分调整申请已处理")
        if adjustment.requested_by == reviewed_by and not allow_self_review:
            raise ApiError(409, "POINT_ADJUSTMENT_SELF_REVIEW", "申请人不能审核自己的调整")
        if decision not in {"approve", "reject"}:
            raise ValueError(f"unknown decision: {decision}")

        adjustment.approved_by = reviewed_by
        adjustment.reviewed_at = utcnow()
        adjustment.review_idempotency_key = idempotency_key
        adjustment.review_fingerprint = review_fingerprint
        if decision == "approve":
            transaction, _ = await self.apply_transaction(
                session,
                user_id=adjustment.user_id,
                entry_type="adjust",
                delta=adjustment.amount,
                reference_type="admin_adjustment",
                reference_id=adjustment.id,
                idempotency_key=idempotency_key,
                request_fingerprint=review_fingerprint,
                description=adjustment.reason,
                metadata={
                    "requested_by": str(adjustment.requested_by),
                    "approved_by": str(reviewed_by),
                    "review_reason": reason,
                    "approval_required": True,
                },
                actor_user_id=reviewed_by,
                request_id=request_id,
            )
            adjustment.status = "applied"
            adjustment.transaction_id = transaction.id
        else:
            adjustment.status = "rejected"
        self.record_audit(
            session,
            action=f"adjustment.{'approved' if decision == 'approve' else 'rejected'}",
            aggregate_type="point_adjustment",
            aggregate_id=adjustment.id,
            actor_user_id=reviewed_by,
            subject_user_id=adjustment.user_id,
            request_id=request_id,
            details={
                "amount": adjustment.amount,
                "request_reason": adjustment.reason,
                "review_reason": reason,
            },
        )
        return adjustment

    async def reverse_transaction(
        self,
        session: AsyncSession,
        *,
        transaction_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        reason: str,
        idempotency_key: str,
        request_fingerprint: str,
        request_id: str,
    ) -> PointTransaction:
        original = await session.get(PointTransaction, transaction_id)
        if original is None:
            raise ApiError(404, "POINT_TRANSACTION_NOT_FOUND", "积分流水不存在")
        if original.entry_type == "reversal":
            raise ApiError(409, "POINT_REVERSAL_NOT_ALLOWED", "冲正流水不能再次冲正")
        transaction, created = await self.apply_transaction(
            session,
            user_id=original.user_id,
            entry_type="reversal",
            delta=-original.delta,
            reference_type="transaction",
            reference_id=original.id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            description=reason,
            metadata={
                "original_transaction_id": str(original.id),
                "original_entry_type": original.entry_type,
                "original_delta": original.delta,
            },
            actor_user_id=actor_user_id,
            request_id=request_id,
        )
        if created:
            self.record_audit(
                session,
                action="transaction.reversed",
                aggregate_type="point_transaction",
                aggregate_id=transaction.id,
                actor_user_id=actor_user_id,
                subject_user_id=original.user_id,
                request_id=request_id,
                details={
                    "original_transaction_id": str(original.id),
                    "delta": transaction.delta,
                    "reason": reason,
                },
            )
        return transaction

    async def reconcile_accounts(
        self,
        session: AsyncSession,
        *,
        request_id: str = "points-daily-reconciliation",
    ) -> ReconciliationResult:
        rows = await session.execute(
            select(
                PointAccount,
                func.coalesce(func.sum(PointTransaction.delta), 0).label("ledger_balance"),
            )
            .outerjoin(PointTransaction, PointTransaction.account_id == PointAccount.id)
            .group_by(PointAccount.id)
        )
        checked = 0
        frozen = 0
        for account, ledger_balance in rows:
            checked += 1
            if account.balance == int(ledger_balance):
                continue
            if account.status != "frozen":
                account.status = "frozen"
                frozen += 1
            self.record_audit(
                session,
                action="reconciliation.mismatch",
                aggregate_type="point_account",
                aggregate_id=account.id,
                actor_user_id=None,
                subject_user_id=account.user_id,
                request_id=request_id,
                details={
                    "account_balance": account.balance,
                    "ledger_balance": int(ledger_balance),
                },
            )
        return ReconciliationResult(checked=checked, frozen=frozen)

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

    async def _account_for_update(self, session: AsyncSession, user_id: uuid.UUID) -> PointAccount:
        account = (
            await session.scalars(
                select(PointAccount).where(PointAccount.user_id == user_id).with_for_update()
            )
        ).one_or_none()
        if account is None:
            await self.ensure_account(session, user_id)
            account = (
                await session.scalars(
                    select(PointAccount).where(PointAccount.user_id == user_id).with_for_update()
                )
            ).one()
        return account

    @staticmethod
    async def _lock_user(session: AsyncSession, user_id: uuid.UUID) -> User:
        user = (
            await session.scalars(select(User).where(User.id == user_id).with_for_update())
        ).one_or_none()
        if user is None or user.deleted_at is not None:
            raise ApiError(404, "USER_NOT_FOUND", "用户不存在")
        return user

    @staticmethod
    def _validate_entry_delta(entry_type: str, delta: int) -> None:
        if entry_type == "consume" and delta > 0:
            raise ValueError("consume delta must not be positive")
        if entry_type in {"grant", "refund", "renewal", "promotion"} and delta < 0:
            raise ValueError(f"{entry_type} delta must not be negative")
        if entry_type in {"adjust", "reversal"} and delta == 0:
            raise ValueError(f"{entry_type} delta must not be zero")

    @staticmethod
    def _validate_replay(
        transaction: PointTransaction,
        *,
        user_id: uuid.UUID,
        entry_type: str,
        delta: int,
        reference_type: str,
        reference_id: uuid.UUID,
        request_fingerprint: str | None,
    ) -> None:
        matches = (
            transaction.user_id == user_id
            and transaction.entry_type == entry_type
            and transaction.delta == delta
            and transaction.reference_type == reference_type
            and transaction.reference_id == reference_id
        )
        fingerprint_matches = (
            request_fingerprint is None
            or transaction.request_fingerprint is None
            or transaction.request_fingerprint == request_fingerprint
        )
        if not matches or not fingerprint_matches:
            raise ApiError(
                409,
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key 或业务引用已用于不同的积分操作",
            )

    @staticmethod
    def _lifetime_changes(
        *, entry_type: str, delta: int, metadata: dict[str, Any]
    ) -> tuple[int, int]:
        earned_change = 0
        spent_change = 0
        if entry_type in {"grant", "renewal", "promotion"}:
            earned_change = delta
        elif entry_type == "consume" or entry_type == "refund":
            spent_change = -delta
        elif entry_type == "adjust":
            if delta > 0:
                earned_change = delta
            else:
                spent_change = -delta
        elif entry_type == "reversal":
            original_type = metadata.get("original_entry_type")
            original_delta = int(metadata.get("original_delta", 0))
            if original_type in {"grant", "renewal", "promotion"}:
                earned_change = -original_delta
            elif original_type == "consume" or original_type == "refund":
                spent_change = original_delta
            elif original_type == "adjust":
                if original_delta > 0:
                    earned_change = -original_delta
                else:
                    spent_change = original_delta
        return earned_change, spent_change

    @staticmethod
    def _record_transaction_event(
        session: AsyncSession,
        transaction: PointTransaction,
        *,
        request_id: str,
    ) -> None:
        session.add(
            OutboxEvent(
                id=uuid7(),
                topic="points.transaction",
                aggregate_type="point_transaction",
                aggregate_id=transaction.id,
                payload={
                    "user_id": str(transaction.user_id),
                    "entry_type": transaction.entry_type,
                    "delta": transaction.delta,
                    "balance_before": transaction.balance_before,
                    "balance_after": transaction.balance_after,
                    "reference_type": transaction.reference_type,
                    "reference_id": str(transaction.reference_id),
                    "request_id": request_id,
                },
                status="pending",
                attempts=0,
                available_at=transaction.created_at,
                version=1,
            )
        )

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
                topic="points.audit",
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
