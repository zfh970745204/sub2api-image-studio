from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError
from app.domain.ids import uuid7
from app.domain.jobs import ACTIVE_JOB_STATUSES, OPERATION_SEEDS, REFUNDABLE_JOB_STATUSES
from app.repositories.models import (
    Asset,
    ConfigGroup,
    ImageJob,
    JobAttempt,
    JobQuote,
    OperationCatalog,
    OperationPrice,
    OutboxEvent,
    PointTransaction,
    User,
    UserNotification,
)
from app.services.memberships import EntitlementService
from app.services.points import PointService
from app.services.security import SecurityService


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _sub2api_config_version(snapshot: dict[str, Any]) -> int | None:
    value = snapshot.get("config_versions", {}).get("sub2api")
    return int(value) if value is not None else None


class RetryableJobError(Exception):
    def __init__(self, code: str, message: str, *, provider_request_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider_request_id = provider_request_id


class PermanentJobError(Exception):
    def __init__(self, code: str, message: str, *, provider_request_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider_request_id = provider_request_id


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    job_id: uuid.UUID
    user_id: uuid.UUID
    operation_code: str
    source_asset_id: uuid.UUID | None
    parameters: dict[str, Any]
    attempt_no: int
    timeout_seconds: int
    retention_days: int
    sub2api_config_version: int | None = None


@dataclass(frozen=True, slots=True)
class JobReconciliationResult:
    checked: int = 0
    requeued: int = 0
    timed_out: int = 0
    refunds_created: int = 0
    mismatches: int = 0


def default_quality_rules(base_points: int) -> dict[str, Any]:
    fine = max(1, math.ceil(base_points / 2))
    return {
        "rules": [
            {
                "parameter": "quality",
                "type": "choice",
                "points": {"low": 0, "medium": 0, "high": fine, "auto": fine},
            }
        ]
    }


async def sync_builtin_operations(
    session: AsyncSession, *, now: datetime | None = None
) -> dict[str, OperationCatalog]:
    current_time = now or utcnow()
    existing = {item.code: item for item in (await session.scalars(select(OperationCatalog))).all()}
    for seed in OPERATION_SEEDS:
        if seed.code in existing:
            continue
        operation = OperationCatalog(
            id=uuid7(),
            code=seed.code,
            name=seed.name,
            engine_type=seed.engine_type,
            queue_name=seed.queue_name,
            enabled=True,
            timeout_seconds=seed.timeout_seconds,
            max_attempts=seed.max_attempts,
        )
        session.add(operation)
        await session.flush()
        session.add(
            OperationPrice(
                id=uuid7(),
                operation_id=operation.id,
                version=1,
                base_points=seed.base_points,
                parameter_rules=default_quality_rules(seed.base_points)
                if seed.code.startswith("ai.")
                else {},
                effective_from=current_time,
                effective_to=None,
                created_by=None,
                reason="P0-v1 initial price",
            )
        )
        existing[seed.code] = operation
    await session.flush()
    return existing


class JobService:
    def __init__(self) -> None:
        self.entitlements = EntitlementService()
        self.points = PointService()

    async def create_quote(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        operation_code: str,
        source_asset_id: uuid.UUID | None,
        parameters: dict[str, Any],
        ttl_seconds: int,
        request_id: str,
        now: datetime | None = None,
    ) -> JobQuote:
        current_time = now or utcnow()
        canonical = self.canonical_parameters(parameters)
        operation = (
            await session.scalars(
                select(OperationCatalog).where(OperationCatalog.code == operation_code)
            )
        ).one_or_none()
        if operation is None or not operation.enabled:
            raise ApiError(404, "OPERATION_NOT_AVAILABLE", "图片操作不存在或已停用")
        await self._validate_source_asset(session, source_asset_id=source_asset_id, user_id=user_id)
        count = await self._validate_generation(session, operation_code, canonical, source_asset_id, user_id)
        price = await self.current_price(session, operation.id, now=current_time)
        entitlement = await self.entitlements.current_snapshot(
            session, user_id, now=current_time, request_id=request_id
        )
        effective = dict(canonical)
        if operation_code.startswith("ai."):
            quality = effective.setdefault(
                "quality", "medium" if operation_code == "ai.generate" else "high"
            )
            if not isinstance(quality, str) or quality not in {"low", "medium", "high", "auto"}:
                raise ApiError(422, "INVALID_OPERATION_PARAMETERS", "生成质量无效")
        surcharge = self.calculate_surcharge(price.parameter_rules, effective)
        discounted_base = math.ceil(price.base_points * entitlement.discount_bps / 10_000)
        final_points = max(0, discounted_base + surcharge) * count
        quote = JobQuote(
            id=uuid7(),
            user_id=user_id,
            operation_code=operation.code,
            source_asset_id=source_asset_id,
            parameters_hash=self.parameters_hash(canonical),
            membership_snapshot={
                "membership_id": str(entitlement.membership_id),
                "plan_id": str(entitlement.plan_id),
                "plan_code": entitlement.plan_code,
                "discount_bps": entitlement.discount_bps,
                "max_concurrent_jobs": entitlement.max_concurrent_jobs,
                "retention_days": entitlement.retention_days,
            },
            pricing_version=price.version,
            base_points=price.base_points * count,
            discount_points=(price.base_points - discounted_base) * count,
            surcharge_points=surcharge * count,
            final_points=final_points,
            expires_at=current_time + timedelta(seconds=ttl_seconds),
            created_at=current_time,
        )
        session.add(quote)
        self.record_event(
            session,
            topic="jobs.quote_created",
            job_id=quote.id,
            user_id=user_id,
            request_id=request_id,
            details={
                "operation_code": operation.code,
                "pricing_version": price.version,
                "final_points": final_points,
            },
        )
        return quote

    async def create_job(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        quote_id: uuid.UUID,
        parameters: dict[str, Any],
        idempotency_key: str,
        request_fingerprint: str,
        request_id: str,
        system_concurrency_limit: int | None = None,
        require_sub2api_config: bool = False,
        now: datetime | None = None,
    ) -> tuple[ImageJob, bool]:
        current_time = now or utcnow()
        canonical = self.canonical_parameters(parameters)
        key = self.clean_idempotency_key(idempotency_key)
        await self._lock_active_user(session, user_id)
        replay = (
            await session.scalars(
                select(ImageJob).where(
                    ImageJob.user_id == user_id,
                    ImageJob.idempotency_key == key,
                )
            )
        ).one_or_none()
        if replay is not None:
            if replay.request_fingerprint != request_fingerprint:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key 已用于不同的图片任务",
                )
            return replay, False
        quote = (
            await session.scalars(select(JobQuote).where(JobQuote.id == quote_id).with_for_update())
        ).one_or_none()
        if quote is None or quote.user_id != user_id:
            raise ApiError(404, "JOB_QUOTE_NOT_FOUND", "任务报价不存在")
        used_quote = (
            await session.scalars(select(ImageJob).where(ImageJob.quote_id == quote.id))
        ).one_or_none()
        if used_quote is not None:
            raise ApiError(409, "JOB_QUOTE_ALREADY_USED", "任务报价已被使用")
        if as_utc(quote.expires_at) <= current_time:
            raise ApiError(409, "JOB_QUOTE_EXPIRED", "任务报价已过期，请重新报价")
        if quote.parameters_hash != self.parameters_hash(canonical):
            raise ApiError(409, "JOB_QUOTE_MISMATCH", "任务参数与报价不一致")
        await self._validate_source_asset(
            session, source_asset_id=quote.source_asset_id, user_id=user_id
        )
        await self._validate_generation(session, quote.operation_code, canonical, quote.source_asset_id, user_id)
        operation = (
            await session.scalars(
                select(OperationCatalog).where(OperationCatalog.code == quote.operation_code)
            )
        ).one()
        if not operation.enabled:
            raise ApiError(409, "OPERATION_DISABLED", "图片操作已停用")
        active_count = await session.scalar(
            select(func.count(ImageJob.id)).where(
                ImageJob.user_id == user_id,
                ImageJob.status.in_(ACTIVE_JOB_STATUSES),
            )
        )
        max_concurrent = int(quote.membership_snapshot["max_concurrent_jobs"])
        if int(active_count or 0) >= max_concurrent:
            raise ApiError(
                409,
                "JOB_CONCURRENCY_LIMIT",
                "当前运行或排队任务已达到会员并发上限",
                {"limit": max_concurrent},
            )
        if system_concurrency_limit is not None:
            system_active_count = await session.scalar(
                select(func.count(ImageJob.id)).where(ImageJob.status.in_(ACTIVE_JOB_STATUSES))
            )
            if int(system_active_count or 0) >= system_concurrency_limit:
                SecurityService.record_event(
                    session,
                    event_type="system_job_concurrency_open",
                    severity="high",
                    user_id=user_id,
                    ip_hash=None,
                    request_id=request_id,
                    details={"limit": system_concurrency_limit},
                )
                raise ApiError(
                    503,
                    "JOB_SYSTEM_CONCURRENCY_LIMIT",
                    "图片任务服务当前繁忙，请稍后重试",
                    {"limit": system_concurrency_limit},
                )
        sub2api_config_version = None
        if operation.engine_type == "sub2api":
            sub2api_config_version = await session.scalar(
                select(ConfigGroup.active_version).where(ConfigGroup.code == "sub2api")
            )
            if require_sub2api_config and sub2api_config_version is None:
                raise ApiError(503, "SUB2API_NOT_CONFIGURED", "图片服务维护中，请稍后重试")
        job = ImageJob(
            id=uuid7(),
            user_id=user_id,
            operation_code=quote.operation_code,
            source_asset_id=quote.source_asset_id,
            output_asset_id=None,
            quote_id=quote.id,
            status="queued",
            refund_status="none",
            parameters=canonical,
            pricing_snapshot={
                "quote_id": str(quote.id),
                "pricing_version": quote.pricing_version,
                "base_points": quote.base_points,
                "discount_points": quote.discount_points,
                "surcharge_points": quote.surcharge_points,
                "final_points": quote.final_points,
                "membership": quote.membership_snapshot,
                "operation": {
                    "engine_type": operation.engine_type,
                    "queue_name": operation.queue_name,
                    "timeout_seconds": operation.timeout_seconds,
                    "max_attempts": operation.max_attempts,
                },
                "config_versions": {"sub2api": sub2api_config_version},
            },
            charged_points=quote.final_points,
            charge_transaction_id=None,
            refund_transaction_id=None,
            attempt_count=0,
            worker_id=None,
            progress=0,
            error_code=None,
            error_message=None,
            queued_at=current_time,
            started_at=None,
            completed_at=None,
            next_attempt_at=None,
            refunded_at=None,
            created_at=current_time,
            updated_at=current_time,
            idempotency_key=key,
            request_fingerprint=request_fingerprint,
        )
        session.add(job)
        await session.flush()
        transaction = await self.points.consume_points(
            session,
            user_id=user_id,
            amount=quote.final_points,
            job_id=job.id,
            idempotency_key=f"job-charge:{job.id}",
            request_fingerprint=request_fingerprint,
            description=f"图片任务扣费：{quote.operation_code}",
            metadata={
                "quote_id": str(quote.id),
                "pricing_version": quote.pricing_version,
                "operation_code": quote.operation_code,
            },
            request_id=request_id,
        )
        job.charge_transaction_id = transaction.id
        self.record_job_status(session, job, "queued", request_id=request_id)
        return job, True

    async def cancel_job(
        self,
        session: AsyncSession,
        *,
        job_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        require_owner: bool,
        request_id: str,
        now: datetime | None = None,
    ) -> ImageJob:
        current_time = now or utcnow()
        job = await self._job_for_update(session, job_id)
        if require_owner and job.user_id != actor_user_id:
            raise ApiError(404, "IMAGE_JOB_NOT_FOUND", "图片任务不存在")
        if job.status == "cancelled":
            return job
        if job.status != "queued":
            raise ApiError(409, "JOB_NOT_CANCELLABLE", "只能取消仍在排队的任务")
        changed = await self._transition(
            session,
            job,
            from_statuses={"queued"},
            status="cancelled",
            progress=0,
            completed_at=current_time,
            updated_at=current_time,
        )
        if not changed:
            raise ApiError(409, "JOB_STATE_CHANGED", "任务状态已变化，请刷新后重试")
        await self._refund_job(session, job, reason="排队任务已取消", request_id=request_id)
        self.record_job_status(
            session,
            job,
            "cancelled",
            request_id=request_id,
            actor_user_id=actor_user_id,
        )
        self._notify_job(
            session,
            job,
            notification_type="job.cancelled",
            title="图片任务已取消",
            body="排队任务已取消，本次消耗积分已退回。",
        )
        return job

    async def claim_job(
        self,
        session: AsyncSession,
        *,
        job_id: uuid.UUID,
        worker_id: str,
        request_id: str,
        now: datetime | None = None,
    ) -> ClaimedJob | None:
        current_time = now or utcnow()
        result = await session.execute(
            update(ImageJob)
            .where(ImageJob.id == job_id, ImageJob.status == "queued")
            .values(
                status="running",
                attempt_count=ImageJob.attempt_count + 1,
                worker_id=worker_id[:255],
                progress=1,
                started_at=func.coalesce(ImageJob.started_at, current_time),
                next_attempt_at=None,
                error_code=None,
                error_message=None,
                updated_at=current_time,
            )
            .returning(ImageJob.attempt_count)
            .execution_options(synchronize_session=False)
        )
        attempt_no = result.scalar_one_or_none()
        if attempt_no is None:
            return None
        job = await session.get(ImageJob, job_id)
        if job is None:
            return None
        operation = (
            await session.scalars(
                select(OperationCatalog).where(OperationCatalog.code == job.operation_code)
            )
        ).one()
        session.add(
            JobAttempt(
                id=uuid7(),
                job_id=job.id,
                attempt_no=attempt_no,
                status="running",
                provider_request_id=None,
                started_at=current_time,
                completed_at=None,
                error_code=None,
                error_detail_redacted=None,
                metrics={},
            )
        )
        self.record_job_status(session, job, "running", request_id=request_id)
        return ClaimedJob(
            job_id=job.id,
            user_id=job.user_id,
            operation_code=job.operation_code,
            source_asset_id=job.source_asset_id,
            parameters=dict(job.parameters),
            attempt_no=attempt_no,
            timeout_seconds=operation.timeout_seconds,
            retention_days=int(
                job.pricing_snapshot.get("membership", {}).get("retention_days", 30)
            ),
            sub2api_config_version=_sub2api_config_version(job.pricing_snapshot),
        )

    async def complete_job(
        self,
        session: AsyncSession,
        *,
        claim: ClaimedJob,
        output_asset_id: uuid.UUID | None,
        provider_request_id: str | None,
        metrics: dict[str, Any] | None,
        request_id: str,
        output_asset_ids: list[uuid.UUID] | None = None,
        now: datetime | None = None,
    ) -> bool:
        current_time = now or utcnow()
        result = await session.execute(
            update(ImageJob)
            .where(
                ImageJob.id == claim.job_id,
                ImageJob.status == "running",
                ImageJob.attempt_count == claim.attempt_no,
            )
            .values(
                status="succeeded",
                output_asset_id=output_asset_id,
                output_asset_ids=[str(item) for item in (output_asset_ids or ([output_asset_id] if output_asset_id else []))],
                progress=100,
                completed_at=current_time,
                worker_id=None,
                updated_at=current_time,
            )
            .returning(ImageJob.id)
            .execution_options(synchronize_session=False)
        )
        if result.scalar_one_or_none() is None:
            return False
        await session.execute(
            update(JobAttempt)
            .where(
                JobAttempt.job_id == claim.job_id,
                JobAttempt.attempt_no == claim.attempt_no,
                JobAttempt.status == "running",
            )
            .values(
                status="succeeded",
                provider_request_id=provider_request_id,
                completed_at=current_time,
                metrics=metrics or {},
            )
        )
        job = await session.get(ImageJob, claim.job_id)
        if job is not None:
            self.record_job_status(session, job, "succeeded", request_id=request_id)
            self._notify_job(
                session,
                job,
                notification_type="job.succeeded",
                title="图片处理完成",
                body=f"{job.operation_code} 已生成新素材。",
            )
        return True

    async def fail_job(
        self,
        session: AsyncSession,
        *,
        claim: ClaimedJob,
        code: str,
        message: str,
        retryable: bool,
        timed_out: bool,
        provider_request_id: str | None,
        metrics: dict[str, Any] | None,
        request_id: str,
        now: datetime | None = None,
    ) -> str | None:
        current_time = now or utcnow()
        job = await self._job_for_update(session, claim.job_id, required=False)
        if job is None or job.status != "running" or job.attempt_count != claim.attempt_no:
            return None
        operation = (
            await session.scalars(
                select(OperationCatalog).where(OperationCatalog.code == job.operation_code)
            )
        ).one()
        will_retry = retryable and not timed_out and claim.attempt_no < operation.max_attempts
        target_status = "retry_wait" if will_retry else "timed_out" if timed_out else "failed"
        next_attempt_at = (
            current_time + timedelta(seconds=min(300, 5 * 2 ** (claim.attempt_no - 1)))
            if will_retry
            else None
        )
        changed = await self._transition(
            session,
            job,
            from_statuses={"running"},
            status=target_status,
            progress=job.progress,
            error_code=code[:100],
            error_message=message[:1000],
            completed_at=None if will_retry else current_time,
            next_attempt_at=next_attempt_at,
            worker_id=None,
            updated_at=current_time,
        )
        if not changed:
            return None
        await session.execute(
            update(JobAttempt)
            .where(
                JobAttempt.job_id == claim.job_id,
                JobAttempt.attempt_no == claim.attempt_no,
                JobAttempt.status == "running",
            )
            .values(
                status="timed_out" if timed_out else "failed",
                provider_request_id=provider_request_id,
                completed_at=current_time,
                error_code=code[:100],
                error_detail_redacted=message[:2000],
                metrics=metrics or {},
            )
        )
        if not will_retry:
            await self._refund_job(session, job, reason=message, request_id=request_id)
            self._notify_job(
                session,
                job,
                notification_type="job.failed",
                title="图片处理未完成",
                body=f"{message[:300]} 本次消耗积分已退回。",
            )
            recent_failures = await session.scalar(
                select(func.count(JobAttempt.id))
                .join(ImageJob, ImageJob.id == JobAttempt.job_id)
                .where(
                    ImageJob.user_id == job.user_id,
                    JobAttempt.status.in_(("failed", "timed_out")),
                    JobAttempt.completed_at >= current_time - timedelta(minutes=10),
                )
            )
            if int(recent_failures or 0) == 5:
                SecurityService.record_event(
                    session,
                    event_type="job_failure_burst",
                    severity="high",
                    user_id=job.user_id,
                    ip_hash=None,
                    request_id=request_id,
                    details={"window_minutes": 10, "failure_count": 5},
                )
        self.record_job_status(session, job, target_status, request_id=request_id)
        return target_status

    async def release_due_retries(
        self, session: AsyncSession, *, now: datetime | None = None, request_id: str
    ) -> int:
        current_time = now or utcnow()
        jobs = list(
            (
                await session.scalars(
                    select(ImageJob)
                    .where(
                        ImageJob.status == "retry_wait",
                        ImageJob.next_attempt_at <= current_time,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for job in jobs:
            job.status = "queued"
            job.queued_at = current_time
            job.next_attempt_at = None
            job.updated_at = current_time
            self.record_job_status(session, job, "queued", request_id=request_id)
        return len(jobs)

    async def dispatchable_jobs(
        self, session: AsyncSession, *, limit: int = 100
    ) -> list[tuple[uuid.UUID, str]]:
        rows = list(
            (
                await session.execute(
                    select(ImageJob.id, ImageJob.pricing_snapshot)
                    .where(ImageJob.status == "queued")
                    .order_by(ImageJob.queued_at, ImageJob.id)
                    .limit(limit)
                )
            ).all()
        )
        return [
            (
                job_id,
                str(snapshot.get("operation", {}).get("queue_name") or "image-jobs"),
            )
            for job_id, snapshot in rows
        ]

    async def reconcile_stale_jobs(
        self,
        session: AsyncSession,
        *,
        now: datetime | None = None,
        request_id: str,
    ) -> JobReconciliationResult:
        current_time = now or utcnow()
        rows = list(
            (
                await session.execute(
                    select(ImageJob, OperationCatalog, JobAttempt)
                    .join(OperationCatalog, OperationCatalog.code == ImageJob.operation_code)
                    .join(
                        JobAttempt,
                        and_(
                            JobAttempt.job_id == ImageJob.id,
                            JobAttempt.attempt_no == ImageJob.attempt_count,
                            JobAttempt.status == "running",
                        ),
                    )
                    .where(ImageJob.status == "running")
                )
            ).all()
        )
        timed_out_count = 0
        refunds = 0
        for job, operation, attempt in rows:
            if (
                as_utc(attempt.started_at) + timedelta(seconds=operation.timeout_seconds)
                > current_time
            ):
                continue
            claim = ClaimedJob(
                job_id=job.id,
                user_id=job.user_id,
                operation_code=job.operation_code,
                source_asset_id=job.source_asset_id,
                parameters=dict(job.parameters),
                attempt_no=job.attempt_count,
                timeout_seconds=operation.timeout_seconds,
                retention_days=int(
                    job.pricing_snapshot.get("membership", {}).get("retention_days", 30)
                ),
                sub2api_config_version=_sub2api_config_version(job.pricing_snapshot),
            )
            result = await self.fail_job(
                session,
                claim=claim,
                code="JOB_TIMED_OUT",
                message="任务执行超时",
                retryable=False,
                timed_out=True,
                provider_request_id=None,
                metrics={},
                request_id=request_id,
                now=current_time,
            )
            if result == "timed_out":
                timed_out_count += 1
                refunds += 1
        return JobReconciliationResult(
            checked=len(rows), timed_out=timed_out_count, refunds_created=refunds
        )

    async def reconcile_job(
        self,
        session: AsyncSession,
        *,
        job_id: uuid.UUID,
        request_id: str,
    ) -> JobReconciliationResult:
        job = await self._job_for_update(session, job_id)
        charge = (
            await session.scalars(
                select(PointTransaction).where(
                    PointTransaction.reference_type == "job",
                    PointTransaction.reference_id == job.id,
                    PointTransaction.entry_type == "consume",
                )
            )
        ).one_or_none()
        refund = (
            await session.scalars(
                select(PointTransaction).where(
                    PointTransaction.reference_type == "job",
                    PointTransaction.reference_id == job.id,
                    PointTransaction.entry_type == "refund",
                )
            )
        ).one_or_none()
        mismatches = 0
        refunds_created = 0
        charge_matches = charge is not None and charge.delta == -job.charged_points
        if not charge_matches:
            mismatches += 1
            self.record_event(
                session,
                topic="jobs.audit",
                job_id=job.id,
                user_id=job.user_id,
                request_id=request_id,
                details={"action": "reconciliation.charge_mismatch"},
            )
        elif job.charge_transaction_id != charge.id:
            job.charge_transaction_id = charge.id
        if job.status in REFUNDABLE_JOB_STATUSES:
            if refund is None and charge_matches:
                await self._refund_job(
                    session, job, reason="任务对账补偿退款", request_id=request_id
                )
                refunds_created += 1
            elif refund is not None:
                job.refund_status = "refunded"
                job.refund_transaction_id = refund.id
                job.refunded_at = job.refunded_at or refund.created_at
        elif refund is not None:
            mismatches += 1
            self.record_event(
                session,
                topic="jobs.audit",
                job_id=job.id,
                user_id=job.user_id,
                request_id=request_id,
                details={"action": "reconciliation.unexpected_refund"},
            )
        return JobReconciliationResult(
            checked=1, refunds_created=refunds_created, mismatches=mismatches
        )

    async def retry_waiting_job(
        self,
        session: AsyncSession,
        *,
        job_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        reason: str,
        request_id: str,
        now: datetime | None = None,
    ) -> ImageJob:
        current_time = now or utcnow()
        job = await self._job_for_update(session, job_id)
        if job.status == "queued":
            return job
        if job.status in REFUNDABLE_JOB_STATUSES and job.refund_status == "refunded":
            raise ApiError(
                409,
                "JOB_ALREADY_REFUNDED",
                "最终失败或取消的任务已经退款，必须重新报价后创建新任务",
            )
        if job.status != "retry_wait":
            raise ApiError(409, "JOB_NOT_RETRYABLE", "当前任务状态不能重试")
        job.status = "queued"
        job.queued_at = current_time
        job.next_attempt_at = None
        job.updated_at = current_time
        self.record_event(
            session,
            topic="jobs.audit",
            job_id=job.id,
            user_id=job.user_id,
            request_id=request_id,
            details={
                "action": "job.retry_requested",
                "actor_user_id": str(actor_user_id),
                "reason": reason,
            },
        )
        self.record_job_status(session, job, "queued", request_id=request_id)
        return job

    async def update_operation(
        self,
        session: AsyncSession,
        *,
        code: str,
        values: dict[str, Any],
        actor_user_id: uuid.UUID,
        reason: str,
        request_id: str,
    ) -> OperationCatalog:
        operation = (
            await session.scalars(
                select(OperationCatalog).where(OperationCatalog.code == code).with_for_update()
            )
        ).one_or_none()
        if operation is None:
            raise ApiError(404, "OPERATION_NOT_FOUND", "图片操作不存在")
        for field, value in values.items():
            setattr(operation, field, value)
        self.record_event(
            session,
            topic="pricing.audit",
            job_id=operation.id,
            user_id=actor_user_id,
            request_id=request_id,
            details={"action": "operation.updated", "reason": reason, "changes": values},
        )
        return operation

    async def create_price(
        self,
        session: AsyncSession,
        *,
        operation_code: str,
        base_points: int,
        parameter_rules: dict[str, Any],
        effective_from: datetime | None,
        actor_user_id: uuid.UUID,
        reason: str,
        request_id: str,
        now: datetime | None = None,
    ) -> OperationPrice:
        current_time = now or utcnow()
        starts_at = effective_from or current_time
        operation = (
            await session.scalars(
                select(OperationCatalog)
                .where(OperationCatalog.code == operation_code)
                .with_for_update()
            )
        ).one_or_none()
        if operation is None:
            raise ApiError(404, "OPERATION_NOT_FOUND", "图片操作不存在")
        parameter_rules = self.quality_price_rules(operation_code, base_points, parameter_rules)
        prices = list(
            (
                await session.scalars(
                    select(OperationPrice)
                    .where(OperationPrice.operation_id == operation.id)
                    .order_by(OperationPrice.version.desc())
                    .with_for_update()
                )
            ).all()
        )
        if prices and starts_at <= as_utc(prices[0].effective_from):
            raise ApiError(409, "PRICE_EFFECTIVE_TIME_CONFLICT", "新价格生效时间必须晚于上一版本")
        previous = prices[0] if prices else None
        if previous is not None:
            previous.effective_to = starts_at
        price = OperationPrice(
            id=uuid7(),
            operation_id=operation.id,
            version=(previous.version + 1) if previous else 1,
            base_points=base_points,
            parameter_rules=parameter_rules,
            effective_from=starts_at,
            effective_to=None,
            created_by=actor_user_id,
            reason=reason,
            created_at=current_time,
        )
        session.add(price)
        self.record_event(
            session,
            topic="pricing.audit",
            job_id=price.id,
            user_id=actor_user_id,
            request_id=request_id,
            details={
                "action": "price.created",
                "operation_code": operation.code,
                "version": price.version,
                "base_points": base_points,
                "reason": reason,
            },
        )
        return price

    async def current_price(
        self, session: AsyncSession, operation_id: uuid.UUID, *, now: datetime | None = None
    ) -> OperationPrice:
        current_time = now or utcnow()
        price = (
            await session.scalars(
                select(OperationPrice)
                .where(
                    OperationPrice.operation_id == operation_id,
                    OperationPrice.effective_from <= current_time,
                    or_(
                        OperationPrice.effective_to.is_(None),
                        OperationPrice.effective_to > current_time,
                    ),
                )
                .order_by(OperationPrice.version.desc())
                .limit(1)
            )
        ).one_or_none()
        if price is None:
            raise ApiError(409, "OPERATION_PRICE_NOT_CONFIGURED", "图片操作当前没有有效价格")
        return price

    @staticmethod
    def canonical_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
        for key, value in parameters.items():
            if key.casefold() in {"prompt", "instruction"} and isinstance(value, str):
                if len(value) > 5_000:
                    raise ApiError(
                        422,
                        "PROMPT_TOO_LONG",
                        "提示词或操作说明不能超过 5000 个字符",
                    )
                if any(ord(character) < 32 and character not in "\r\n\t" for character in value):
                    raise ApiError(
                        422,
                        "INVALID_PROMPT_CONTENT",
                        "提示词包含不允许的控制字符",
                    )
        try:
            encoded = json.dumps(
                parameters,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ApiError(422, "INVALID_OPERATION_PARAMETERS", "任务参数必须是有效 JSON") from exc
        if len(encoded.encode()) > 65_536:
            raise ApiError(422, "INVALID_OPERATION_PARAMETERS", "任务参数不能超过 64 KiB")
        return json.loads(encoded)

    @classmethod
    def parameters_hash(cls, parameters: dict[str, Any]) -> str:
        encoded = json.dumps(
            cls.canonical_parameters(parameters),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def request_fingerprint(
        user_id: uuid.UUID, quote_id: uuid.UUID, parameters: dict[str, Any]
    ) -> str:
        encoded = json.dumps(
            {
                "user_id": str(user_id),
                "quote_id": str(quote_id),
                "parameters": parameters,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def quality_price_rules(
        cls, operation_code: str, base_points: int, rules: dict[str, Any]
    ) -> dict[str, Any]:
        cls.validate_parameter_rules(rules)
        if not operation_code.startswith("ai."):
            return rules
        quality = next(
            (rule for rule in rules.get("rules", []) if rule["parameter"] == "quality"), None
        )
        if quality is None:
            return {
                "rules": [*rules.get("rules", []), *default_quality_rules(base_points)["rules"]]
            }
        points = quality.get("points", {})
        if (
            quality["type"] != "choice"
            or not {"low", "medium", "high", "auto"}.issubset(points)
            or points["high"] <= points["medium"]
            or points["auto"] < points["high"]
        ):
            raise ApiError(
                422,
                "INVALID_PARAMETER_RULES",
                "精细质量附加积分必须高于标准，自动质量至少按精细计价",
            )
        return rules

    @classmethod
    def calculate_surcharge(
        cls, parameter_rules: dict[str, Any], parameters: dict[str, Any]
    ) -> int:
        cls.validate_parameter_rules(parameter_rules)
        surcharge = 0
        for rule in parameter_rules.get("rules", []):
            parameter = rule["parameter"]
            if parameter not in parameters:
                continue
            value = parameters[parameter]
            if rule["type"] == "choice":
                key = str(value)
                if key not in rule["points"]:
                    raise ApiError(
                        422,
                        "INVALID_OPERATION_PARAMETERS",
                        f"参数 {parameter} 不在计价规则允许范围内",
                    )
                surcharge += rule["points"][key]
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ApiError(422, "INVALID_OPERATION_PARAMETERS", f"参数 {parameter} 必须是数字")
            maximum = rule.get("maximum")
            if maximum is not None and value > maximum:
                raise ApiError(
                    422,
                    "INVALID_OPERATION_PARAMETERS",
                    f"参数 {parameter} 超过允许上限",
                )
            chargeable = max(0, value - rule.get("included", 0))
            surcharge += math.ceil(chargeable / rule["unit"]) * rule["points"]
        if surcharge > 1_000_000_000:
            raise ApiError(422, "INVALID_OPERATION_PARAMETERS", "参数加价超过系统上限")
        return surcharge

    @staticmethod
    def validate_parameter_rules(parameter_rules: dict[str, Any]) -> None:
        if not isinstance(parameter_rules, dict):
            raise ApiError(422, "INVALID_PARAMETER_RULES", "参数计价规则必须是对象")
        unknown_root = set(parameter_rules) - {"rules"}
        rules = parameter_rules.get("rules", [])
        if unknown_root or not isinstance(rules, list) or len(rules) > 50:
            raise ApiError(422, "INVALID_PARAMETER_RULES", "参数计价规则格式无效")
        seen: set[str] = set()
        for rule in rules:
            if not isinstance(rule, dict):
                raise ApiError(422, "INVALID_PARAMETER_RULES", "每条参数规则必须是对象")
            parameter = rule.get("parameter")
            rule_type = rule.get("type")
            if (
                not isinstance(parameter, str)
                or not parameter
                or len(parameter) > 100
                or parameter in seen
                or rule_type not in {"choice", "per_unit"}
            ):
                raise ApiError(422, "INVALID_PARAMETER_RULES", "参数规则字段无效或重复")
            seen.add(parameter)
            if rule_type == "choice":
                points = rule.get("points")
                if (
                    set(rule) != {"parameter", "type", "points"}
                    or not isinstance(points, dict)
                    or not points
                    or any(
                        not isinstance(key, str)
                        or isinstance(value, bool)
                        or not isinstance(value, int)
                        or value < 0
                        for key, value in points.items()
                    )
                ):
                    raise ApiError(422, "INVALID_PARAMETER_RULES", "choice 计价规则无效")
                continue
            if set(rule) - {"parameter", "type", "points", "unit", "included", "maximum"}:
                raise ApiError(422, "INVALID_PARAMETER_RULES", "per_unit 计价规则字段无效")
            for field in ("points", "unit"):
                if (
                    isinstance(rule.get(field), bool)
                    or not isinstance(rule.get(field), int)
                    or rule[field] <= 0
                ):
                    raise ApiError(422, "INVALID_PARAMETER_RULES", f"{field} 必须为正整数")
            for field in ("included", "maximum"):
                value = rule.get(field)
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
                ):
                    raise ApiError(422, "INVALID_PARAMETER_RULES", f"{field} 必须为非负数字")

    @staticmethod
    def clean_idempotency_key(value: str) -> str:
        key = value.strip()
        if not key:
            raise ApiError(422, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key 不能为空")
        if len(key) > 255:
            raise ApiError(422, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key 过长")
        return key

    async def _refund_job(
        self, session: AsyncSession, job: ImageJob, *, reason: str, request_id: str
    ) -> PointTransaction:
        transaction = await self.points.refund_points(
            session,
            user_id=job.user_id,
            amount=job.charged_points,
            job_id=job.id,
            idempotency_key=f"job-refund:{job.id}",
            description=reason,
            metadata={"operation_code": job.operation_code, "terminal_status": job.status},
            request_id=request_id,
        )
        job.refund_status = "refunded"
        job.refund_transaction_id = transaction.id
        job.refunded_at = transaction.created_at
        return transaction

    @staticmethod
    def _notify_job(
        session: AsyncSession,
        job: ImageJob,
        *,
        notification_type: str,
        title: str,
        body: str,
    ) -> None:
        session.add(
            UserNotification(
                id=uuid7(),
                user_id=job.user_id,
                type=notification_type,
                title=title,
                body=body,
                target_url="/app/jobs",
            )
        )

    @staticmethod
    async def _validate_generation(session, code, parameters, source_id, user_id) -> int:
        from app.domain.jobs import ECOMMERCE_PLATFORMS

        if code not in {"ai.generate", "ai.ecommerce"}:
            if parameters.get("reference_asset_ids") or parameters.get("image_count", 1) != 1:
                raise ApiError(422, "INVALID_OPERATION_PARAMETERS", "当前工具不支持多图参数")
            return 1
        refs = parameters.get("reference_asset_ids", [])
        if not isinstance(refs, list) or len(refs) > 6:
            raise ApiError(422, "INVALID_REFERENCE_ASSETS", "最多上传 6 张参考图")
        try:
            ids = [uuid.UUID(str(value)) for value in refs]
        except (ValueError, TypeError) as exc:
            raise ApiError(422, "INVALID_REFERENCE_ASSETS", "参考图标识无效") from exc
        if len(set(ids)) != len(ids) or (source_id and ids and ids[0] != source_id):
            raise ApiError(422, "INVALID_REFERENCE_ASSETS", "首张参考图必须与来源素材一致，且不能重复")
        for asset_id in set(ids + ([source_id] if source_id else [])):
            asset = await session.get(Asset, asset_id)
            if asset is None or asset.owner_id != user_id or asset.status != "ready":
                raise ApiError(404, "REFERENCE_ASSET_NOT_FOUND", "参考图不存在或已不可用")
            if asset.mime_type not in {"image/png", "image/jpeg", "image/webp"} or asset.kind in {"mask", "thumbnail"}:
                raise ApiError(422, "INVALID_REFERENCE_ASSETS", "参考图必须是 PNG、JPEG 或 WebP 图片")
        prompt = parameters.get("prompt", "")
        if code == "ai.ecommerce" and (not isinstance(prompt, str) or (not prompt.strip() and not ids and not source_id)):
            raise ApiError(422, "INVALID_OPERATION_PARAMETERS", "请描述图片内容或提供参考图")
        if parameters.get("size", "1024x1024") not in {"auto", "1024x1024", "1024x1536", "1536x1024"}:
            raise ApiError(422, "INVALID_OPERATION_PARAMETERS", "画布尺寸无效")
        count = parameters.get("image_count", 1)
        if type(count) is not int or not 1 <= count <= (8 if code == "ai.ecommerce" else 1):
            raise ApiError(422, "INVALID_IMAGE_COUNT", "电商主图每次支持 1–8 张，AI 生成每次 1 张")
        if code == "ai.ecommerce" and parameters.get("platform", "amazon") not in ECOMMERCE_PLATFORMS:
            raise ApiError(422, "INVALID_PLATFORM", "请选择支持的电商平台")
        return count

    @staticmethod
    async def _validate_source_asset(
        session: AsyncSession,
        *,
        source_asset_id: uuid.UUID | None,
        user_id: uuid.UUID,
    ) -> None:
        if source_asset_id is None:
            return
        owned = await session.scalar(
            select(Asset.id).where(
                Asset.id == source_asset_id,
                Asset.owner_id == user_id,
                Asset.status == "ready",
            )
        )
        if owned is None:
            raise ApiError(404, "SOURCE_ASSET_NOT_FOUND", "来源素材不存在")

    @staticmethod
    async def _lock_active_user(session: AsyncSession, user_id: uuid.UUID) -> User:
        locked = await session.scalar(
            update(User)
            .where(
                User.id == user_id,
                User.deleted_at.is_(None),
                User.status == "active",
            )
            .values(session_version=User.session_version)
            .returning(User.id)
            .execution_options(synchronize_session=False)
        )
        if locked is None:
            raise ApiError(403, "USER_NOT_ACTIVE", "当前账号不可创建图片任务")
        return (await session.scalars(select(User).where(User.id == user_id))).one()

    @staticmethod
    async def _job_for_update(
        session: AsyncSession, job_id: uuid.UUID, *, required: bool = True
    ) -> ImageJob | None:
        job = (
            await session.scalars(select(ImageJob).where(ImageJob.id == job_id).with_for_update())
        ).one_or_none()
        if job is None and required:
            raise ApiError(404, "IMAGE_JOB_NOT_FOUND", "图片任务不存在")
        return job

    @staticmethod
    async def _transition(
        session: AsyncSession,
        job: ImageJob,
        *,
        from_statuses: set[str],
        status: str,
        **values: Any,
    ) -> bool:
        result = await session.execute(
            update(ImageJob)
            .where(ImageJob.id == job.id, ImageJob.status.in_(from_statuses))
            .values(status=status, **values)
            .returning(ImageJob.id)
            .execution_options(synchronize_session=False)
        )
        if result.scalar_one_or_none() is None:
            return False
        await session.refresh(job)
        return True

    @classmethod
    def record_job_status(
        cls,
        session: AsyncSession,
        job: ImageJob,
        status: str,
        *,
        request_id: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        cls.record_event(
            session,
            topic="jobs.status",
            job_id=job.id,
            user_id=job.user_id,
            request_id=request_id,
            details={
                "status": status,
                "refund_status": job.refund_status,
                "attempt_count": job.attempt_count,
                "actor_user_id": str(actor_user_id) if actor_user_id else None,
            },
        )

    @staticmethod
    def record_event(
        session: AsyncSession,
        *,
        topic: str,
        job_id: uuid.UUID,
        user_id: uuid.UUID,
        request_id: str,
        details: dict[str, Any],
    ) -> None:
        session.add(
            OutboxEvent(
                id=uuid7(),
                topic=topic,
                aggregate_type="image_job" if topic.startswith("jobs.") else "operation_price",
                aggregate_id=job_id,
                payload={"user_id": str(user_id), "request_id": request_id, **details},
                status="pending",
                attempts=0,
                available_at=utcnow(),
                version=1,
            )
        )
