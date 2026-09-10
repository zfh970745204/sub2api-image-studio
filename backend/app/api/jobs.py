from __future__ import annotations

import logging
import re
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from arq.connections import RedisSettings, create_pool
from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import and_, asc, desc, or_, select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import Principal, get_current_principal, require_permission
from app.api.errors import ApiError
from app.domain.ids import uuid7
from app.domain.jobs import JOB_STATUSES
from app.object_storage import ObjectStorageError
from app.repositories.models import (
    ImageJob,
    JobAttempt,
    JobQuote,
    OperationCatalog,
    OperationPrice,
)
from app.services.assets import AssetService
from app.services.configuration import runtime_config_value
from app.services.jobs import JobService

logger = logging.getLogger(__name__)
router = APIRouter(tags=["image-jobs"])
service = JobService()

CurrentUser = Annotated[Principal, Depends(get_current_principal)]
JobCreator = Annotated[Principal, Depends(require_permission("tasks.create"))]
JobOwnerReader = Annotated[Principal, Depends(require_permission("tasks.read_own"))]
JobOwnerCanceller = Annotated[Principal, Depends(require_permission("tasks.cancel_own"))]
PricingReader = Annotated[Principal, Depends(require_permission("pricing.read"))]
PricingManager = Annotated[Principal, Depends(require_permission("pricing.manage"))]
TaskReader = Annotated[Principal, Depends(require_permission("tasks.read"))]
TaskManager = Annotated[Principal, Depends(require_permission("tasks.manage"))]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)]
CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
QUEUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")


class QuoteRequest(BaseModel):
    operation_code: str = Field(min_length=2, max_length=64)
    source_asset_id: uuid.UUID | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("operation_code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        normalized = value.strip()
        if not CODE_PATTERN.fullmatch(normalized):
            raise ValueError("操作代码格式无效")
        return normalized


class CreateJobRequest(BaseModel):
    quote_id: uuid.UUID
    parameters: dict[str, Any] = Field(default_factory=dict)


class UpdateOperationRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    queue_name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=30, le=3600)
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("name", "reason")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @field_validator("queue_name")
    @classmethod
    def validate_queue(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not QUEUE_PATTERN.fullmatch(normalized):
            raise ValueError("队列名称格式无效")
        return normalized

    @model_validator(mode="after")
    def require_change(self) -> UpdateOperationRequest:
        if not (self.model_fields_set - {"reason"}):
            raise ValueError("至少提供一个要修改的字段")
        return self


class SaveOperationRequest(UpdateOperationRequest):
    base_points: int = Field(strict=True, ge=0, le=1_000_000_000)
    parameter_rules: dict[str, Any] = Field(default_factory=dict)


class CreatePriceRequest(BaseModel):
    operation_code: str = Field(min_length=2, max_length=64)
    base_points: int = Field(strict=True, ge=0, le=1_000_000_000)
    parameter_rules: dict[str, Any] = Field(default_factory=dict)
    effective_from: datetime | None = None
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("operation_code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        normalized = value.strip()
        if not CODE_PATTERN.fullmatch(normalized):
            raise ValueError("操作代码格式无效")
        return normalized

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("必须填写改价原因")
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


def operation_payload(
    operation: OperationCatalog,
    price: OperationPrice | None,
    *,
    discount_bps: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": operation.id,
        "code": operation.code,
        "name": operation.name,
        "engine_type": operation.engine_type,
        "queue_name": operation.queue_name,
        "enabled": operation.enabled,
        "timeout_seconds": operation.timeout_seconds,
        "max_attempts": operation.max_attempts,
        "created_at": operation.created_at,
        "updated_at": operation.updated_at,
        "current_price": price_payload(price) if price else None,
    }
    if price is not None and discount_bps is not None:
        payload["member_base_points"] = (price.base_points * discount_bps + 9_999) // 10_000
    return payload


def price_payload(price: OperationPrice) -> dict[str, Any]:
    return {
        "id": price.id,
        "operation_id": price.operation_id,
        "version": price.version,
        "base_points": price.base_points,
        "parameter_rules": price.parameter_rules,
        "effective_from": price.effective_from,
        "effective_to": price.effective_to,
        "created_by": price.created_by,
        "reason": price.reason,
        "created_at": price.created_at,
    }


def quote_payload(quote: JobQuote) -> dict[str, Any]:
    return {
        "id": quote.id,
        "operation_code": quote.operation_code,
        "source_asset_id": quote.source_asset_id,
        "membership_snapshot": quote.membership_snapshot,
        "pricing_version": quote.pricing_version,
        "base_points": quote.base_points,
        "discount_points": quote.discount_points,
        "surcharge_points": quote.surcharge_points,
        "final_points": quote.final_points,
        "expires_at": quote.expires_at,
        "created_at": quote.created_at,
    }


def job_payload(job: ImageJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "user_id": job.user_id,
        "operation_code": job.operation_code,
        "source_asset_id": job.source_asset_id,
        "output_asset_id": job.output_asset_id,
        "output_asset_ids": job.output_asset_ids
        or ([str(job.output_asset_id)] if job.output_asset_id else []),
        "quote_id": job.quote_id,
        "status": job.status,
        "refund_status": job.refund_status,
        "parameters": job.parameters,
        "pricing_snapshot": job.pricing_snapshot,
        "charged_points": job.charged_points,
        "attempt_count": job.attempt_count,
        "progress": job.progress,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "queued_at": job.queued_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "refunded_at": job.refunded_at,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def attempt_payload(attempt: JobAttempt) -> dict[str, Any]:
    return {
        "id": attempt.id,
        "attempt_no": attempt.attempt_no,
        "status": attempt.status,
        "provider_request_id": attempt.provider_request_id,
        "started_at": attempt.started_at,
        "completed_at": attempt.completed_at,
        "error_code": attempt.error_code,
        "error_detail_redacted": attempt.error_detail_redacted,
        "metrics": attempt.metrics,
    }


async def get_job_or_404(session, job_id: uuid.UUID, *, user_id: uuid.UUID | None) -> ImageJob:
    statement = select(ImageJob).where(ImageJob.id == job_id)
    if user_id is not None:
        statement = statement.where(ImageJob.user_id == user_id)
    job = (await session.scalars(statement)).one_or_none()
    if job is None:
        raise ApiError(404, "IMAGE_JOB_NOT_FOUND", "图片任务不存在")
    return job


async def operation_with_price(session, operation: OperationCatalog, *, now: datetime):
    try:
        return await service.current_price(session, operation.id, now=now)
    except ApiError as exc:
        if exc.code != "OPERATION_PRICE_NOT_CONFIGURED":
            raise
        return None


async def enqueue_job(request: Request, job: ImageJob) -> bool:
    queue_name = str(job.pricing_snapshot.get("operation", {}).get("queue_name") or "image-jobs")
    callback = getattr(request.app.state, "job_enqueuer", None)
    try:
        if callback is not None:
            return bool(await callback(job.id, queue_name))
        pool = await create_pool(RedisSettings.from_dsn(request.app.state.settings.redis_url))
        try:
            queued = await pool.enqueue_job(
                "execute_image_job",
                str(job.id),
                _job_id=f"image-job:{job.id}",
                _queue_name=queue_name,
            )
        finally:
            await pool.aclose()
        return queued is not None
    except Exception:
        logger.exception(
            "image job enqueue failed; scheduler will retry",
            extra={"operation": "job_enqueue", "job_id": str(job.id), "status": "pending"},
        )
        return False


async def job_page(
    session,
    *,
    user_id: uuid.UUID | None,
    status_filter: str | None,
    cursor: uuid.UUID | None,
    limit: int,
    order: Literal["asc", "desc"] = "desc",
) -> tuple[list[ImageJob], str | None]:
    if status_filter is not None and status_filter not in {*JOB_STATUSES, "refunded"}:
        raise ApiError(422, "VALIDATION_ERROR", "无效的任务状态")
    statement = select(ImageJob)
    if user_id is not None:
        statement = statement.where(ImageJob.user_id == user_id)
    if status_filter == "refunded":
        statement = statement.where(ImageJob.refund_status == "refunded")
    elif status_filter is not None:
        statement = statement.where(ImageJob.status == status_filter)
    if cursor is not None:
        anchor = await session.get(ImageJob, cursor)
        if anchor is None or (user_id is not None and anchor.user_id != user_id):
            raise ApiError(422, "INVALID_CURSOR", "分页游标无效")
        if order == "desc":
            statement = statement.where(
                or_(
                    ImageJob.created_at < anchor.created_at,
                    and_(ImageJob.created_at == anchor.created_at, ImageJob.id < anchor.id),
                )
            )
        else:
            statement = statement.where(
                or_(
                    ImageJob.created_at > anchor.created_at,
                    and_(ImageJob.created_at == anchor.created_at, ImageJob.id > anchor.id),
                )
            )
    direction = desc if order == "desc" else asc
    rows = list(
        (
            await session.scalars(
                statement.order_by(direction(ImageJob.created_at), direction(ImageJob.id)).limit(
                    limit + 1
                )
            )
        ).all()
    )
    items = rows[:limit]
    return items, str(items[-1].id) if len(rows) > limit and items else None


@router.get("/api/v1/operations")
async def list_operations(request: Request, principal: CurrentUser) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    current_time = datetime.now(UTC)
    async with database.session_factory() as session:
        entitlement = await service.entitlements.current_snapshot(
            session, principal.user_id, now=current_time, request_id=request_id(request)
        )
        operations = list(
            (
                await session.scalars(
                    select(OperationCatalog)
                    .where(OperationCatalog.enabled.is_(True))
                    .order_by(OperationCatalog.code)
                )
            ).all()
        )
        items = [
            operation_payload(
                operation,
                await operation_with_price(session, operation, now=current_time),
                discount_bps=entitlement.discount_bps,
            )
            for operation in operations
        ]
        await session.commit()
    return {"items": items}


@router.post("/api/v1/jobs/quote", status_code=status.HTTP_201_CREATED)
async def create_quote(
    payload: QuoteRequest, request: Request, principal: JobCreator
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        quote = await service.create_quote(
            session,
            user_id=principal.user_id,
            operation_code=payload.operation_code,
            source_asset_id=payload.source_asset_id,
            parameters=payload.parameters,
            ttl_seconds=request.app.state.settings.job_quote_ttl_seconds,
            request_id=request_id(request),
        )
        await session.commit()
        await session.refresh(quote)
    return {"quote": quote_payload(quote)}


@router.post("/api/v1/jobs", status_code=status.HTTP_201_CREATED)
async def create_job(
    payload: CreateJobRequest,
    request: Request,
    principal: JobCreator,
    idempotency_key: IdempotencyKey,
) -> dict[str, Any]:
    fingerprint = service.request_fingerprint(
        principal.user_id, payload.quote_id, payload.parameters
    )
    database = request.app.state.runtime_services.database
    concurrency_limit = int(
        await runtime_config_value(
            request.app.state.runtime_services,
            "general",
            "task_concurrency",
            request.app.state.settings.worker_max_jobs,
        )
    )
    async with database.session_factory() as session:
        try:
            job, created = await service.create_job(
                session,
                user_id=principal.user_id,
                quote_id=payload.quote_id,
                parameters=payload.parameters,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                request_id=request_id(request),
                system_concurrency_limit=concurrency_limit,
                require_sub2api_config=not request.app.state.settings.legacy_sync_api_enabled,
            )
            await session.commit()
        except ApiError as exc:
            if exc.code == "JOB_SYSTEM_CONCURRENCY_LIMIT":
                await session.commit()
            raise
        except IntegrityError as exc:
            await session.rollback()
            # Do not mislabel FK/check failures as a user-caused state conflict,
            # or log the SQL parameters (which may contain image prompts).
            cause = exc.orig.__cause__ or exc.orig
            logger.error(
                "image job persistence failed",
                extra={
                    "request_id": request_id(request),
                    "error_code": getattr(cause, "sqlstate", "integrity_error"),
                    "constraint": getattr(cause, "constraint_name", None),
                    "operation": "jobs.create",
                },
            )
            raise ApiError(
                500,
                "IMAGE_JOB_SAVE_FAILED",
                "任务保存失败，本次提交未扣费。请重试；若仍失败，请将请求编号提供给管理员",
            ) from exc
        await session.refresh(job)
    dispatched = await enqueue_job(request, job) if created else False
    return {"job": job_payload(job), "created": created, "dispatched": dispatched}


@router.get("/api/v1/jobs")
async def list_my_jobs(
    request: Request,
    principal: JobOwnerReader,
    status_filter: str | None = Query(default=None, alias="status"),
    cursor: uuid.UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        items, next_cursor = await job_page(
            session,
            user_id=principal.user_id,
            status_filter=status_filter,
            cursor=cursor,
            limit=limit,
        )
    return {"items": [job_payload(item) for item in items], "next_cursor": next_cursor}


@router.get("/api/v1/jobs/{job_id}")
async def get_my_job(
    job_id: uuid.UUID, request: Request, principal: JobOwnerReader
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        job = await get_job_or_404(session, job_id, user_id=principal.user_id)
    return {"job": job_payload(job)}


@router.get(
    "/api/v1/jobs/{job_id}/download", dependencies=[Depends(require_permission("assets.read_own"))]
)
async def download_job_results(job_id: uuid.UUID, request: Request, principal: JobOwnerReader):
    runtime = request.app.state.runtime_services
    async with runtime.database.session_factory() as session:
        job = await get_job_or_404(session, job_id, user_id=principal.user_id)
        ids = job.output_asset_ids or ([str(job.output_asset_id)] if job.output_asset_id else [])
        if job.status != "succeeded" or not ids:
            raise ApiError(409, "JOB_RESULTS_NOT_READY", "任务结果尚未就绪")
        assets = [
            await AssetService().require_usable(
                session, uuid.UUID(value), owner_id=principal.user_id
            )
            for value in ids
        ]
    # The response generator owns and closes this file after the client has read it.
    archive = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)  # noqa: SIM115
    try:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
            for index, asset in enumerate(assets, 1):
                data = await runtime.object_storage.get_object(asset.object_key)
                bundle.writestr(f"image-{index:02d}.{asset.extension}", data)
        archive.seek(0)
    except BaseException as exc:
        archive.close()
        if isinstance(exc, ObjectStorageError):
            raise ApiError(
                503, "OBJECT_STORAGE_UNAVAILABLE", "结果下载暂不可用，请稍后重试"
            ) from exc
        raise

    def chunks():
        try:
            while chunk := archive.read(256 * 1024):
                yield chunk
        finally:
            archive.close()

    return StreamingResponse(
        chunks(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="studio-{job_id}.zip"',
            "Cache-Control": "private, no-store",
        },
    )


@router.post("/api/v1/jobs/{job_id}/cancel")
async def cancel_my_job(
    job_id: uuid.UUID, request: Request, principal: JobOwnerCanceller
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        job = await service.cancel_job(
            session,
            job_id=job_id,
            actor_user_id=principal.user_id,
            require_owner=True,
            request_id=request_id(request),
        )
        await session.commit()
        await session.refresh(job)
    return {"job": job_payload(job)}


@router.get("/api/v1/jobs/{job_id}/events")
async def get_job_events(
    job_id: uuid.UUID, request: Request, principal: JobOwnerReader
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        job = await get_job_or_404(session, job_id, user_id=principal.user_id)
        attempts = list(
            (
                await session.scalars(
                    select(JobAttempt)
                    .where(JobAttempt.job_id == job.id)
                    .order_by(JobAttempt.attempt_no)
                )
            ).all()
        )
    return {
        "job": job_payload(job),
        "attempts": [attempt_payload(item) for item in attempts],
        "next_poll_after_ms": 2000 if job.status in {"queued", "running", "retry_wait"} else None,
    }


@router.get("/api/v1/admin/operations")
async def list_admin_operations(request: Request, _principal: PricingReader) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    current_time = datetime.now(UTC)
    async with database.session_factory() as session:
        operations = list(
            (await session.scalars(select(OperationCatalog).order_by(OperationCatalog.code))).all()
        )
        items = [
            operation_payload(
                operation, await operation_with_price(session, operation, now=current_time)
            )
            for operation in operations
        ]
    return {"items": items}


@router.patch("/api/v1/admin/operations/{code}")
async def update_operation(
    code: str,
    payload: UpdateOperationRequest,
    request: Request,
    principal: PricingManager,
) -> dict[str, Any]:
    values = payload.model_dump(exclude={"reason"}, exclude_none=True)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        operation = await service.update_operation(
            session,
            code=code,
            values=values,
            actor_user_id=principal.user_id,
            reason=payload.reason,
            request_id=request_id(request),
        )
        await session.commit()
        await session.refresh(operation)
        price = await operation_with_price(session, operation, now=datetime.now(UTC))
    return {"operation": operation_payload(operation, price)}


@router.put("/api/v1/admin/operations/{code}/configuration")
async def save_operation_configuration(
    code: str,
    payload: SaveOperationRequest,
    request: Request,
    principal: PricingManager,
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    rules = service.quality_price_rules(code, payload.base_points, payload.parameter_rules)
    async with database.session_factory() as session:
        operation = await service.update_operation(
            session,
            code=code,
            values=payload.model_dump(
                exclude={"reason", "base_points", "parameter_rules"}, exclude_none=True
            ),
            actor_user_id=principal.user_id,
            reason=payload.reason,
            request_id=request_id(request),
        )
        price = await operation_with_price(session, operation, now=datetime.now(UTC))
        if (
            price is None
            or price.base_points != payload.base_points
            or price.parameter_rules != rules
        ):
            price = await service.create_price(
                session,
                operation_code=code,
                base_points=payload.base_points,
                parameter_rules=rules,
                effective_from=None,
                actor_user_id=principal.user_id,
                reason=payload.reason,
                request_id=request_id(request),
            )
        await session.commit()
        await session.refresh(operation)
    return {"operation": operation_payload(operation, price)}


@router.get("/api/v1/admin/operation-prices")
async def list_prices(
    request: Request,
    _principal: PricingReader,
    operation_code: str | None = None,
    cursor: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        statement = select(OperationPrice, OperationCatalog.code).join(
            OperationCatalog, OperationCatalog.id == OperationPrice.operation_id
        )
        if operation_code:
            statement = statement.where(OperationCatalog.code == operation_code)
        if cursor is not None:
            anchor = await session.get(OperationPrice, cursor)
            if anchor is None:
                raise ApiError(422, "INVALID_CURSOR", "分页游标无效")
            statement = statement.where(
                or_(
                    OperationPrice.created_at < anchor.created_at,
                    and_(
                        OperationPrice.created_at == anchor.created_at,
                        OperationPrice.id < anchor.id,
                    ),
                )
            )
        rows = list(
            (
                await session.execute(
                    statement.order_by(
                        OperationPrice.created_at.desc(), OperationPrice.id.desc()
                    ).limit(limit + 1)
                )
            ).all()
        )
    items = rows[:limit]
    return {
        "items": [{**price_payload(price), "operation_code": code} for price, code in items],
        "next_cursor": str(items[-1][0].id) if len(rows) > limit and items else None,
    }


@router.post("/api/v1/admin/operation-prices", status_code=status.HTTP_201_CREATED)
async def create_price(
    payload: CreatePriceRequest,
    request: Request,
    principal: PricingManager,
) -> dict[str, Any]:
    effective_from = payload.effective_from
    if effective_from is not None and effective_from.tzinfo is None:
        effective_from = effective_from.replace(tzinfo=UTC)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        try:
            price = await service.create_price(
                session,
                operation_code=payload.operation_code,
                base_points=payload.base_points,
                parameter_rules=payload.parameter_rules,
                effective_from=effective_from,
                actor_user_id=principal.user_id,
                reason=payload.reason,
                request_id=request_id(request),
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ApiError(409, "OPERATION_PRICE_CONFLICT", "操作价格版本发生冲突") from exc
        await session.refresh(price)
    return {"price": price_payload(price)}


@router.get("/api/v1/admin/jobs")
async def list_admin_jobs(
    request: Request,
    _principal: TaskReader,
    user_id: uuid.UUID | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    cursor: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    order: Literal["asc", "desc"] = "desc",
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        items, next_cursor = await job_page(
            session,
            user_id=user_id,
            status_filter=status_filter,
            cursor=cursor,
            limit=limit,
            order=order,
        )
    return {"items": [job_payload(item) for item in items], "next_cursor": next_cursor}


@router.post("/api/v1/admin/jobs/{job_id}/retry")
async def retry_job(
    job_id: uuid.UUID,
    payload: ReasonRequest,
    request: Request,
    principal: TaskManager,
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        job = await service.retry_waiting_job(
            session,
            job_id=job_id,
            actor_user_id=principal.user_id,
            reason=payload.reason,
            request_id=request_id(request),
        )
        await session.commit()
        await session.refresh(job)
    dispatched = await enqueue_job(request, job)
    return {"job": job_payload(job), "dispatched": dispatched}


@router.post("/api/v1/admin/jobs/{job_id}/reconcile")
async def reconcile_job(
    job_id: uuid.UUID,
    payload: ReasonRequest,
    request: Request,
    principal: TaskManager,
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        job = await get_job_or_404(session, job_id, user_id=None)
        result = await service.reconcile_job(session, job_id=job_id, request_id=request_id(request))
        service.record_event(
            session,
            topic="jobs.audit",
            job_id=job_id,
            user_id=job.user_id,
            request_id=request_id(request),
            details={
                "action": "job.reconciled",
                "actor_user_id": str(principal.user_id),
                "reason": payload.reason,
            },
        )
        await session.commit()
    return {
        "result": {
            "checked": result.checked,
            "refunds_created": result.refunds_created,
            "mismatches": result.mismatches,
        }
    }
