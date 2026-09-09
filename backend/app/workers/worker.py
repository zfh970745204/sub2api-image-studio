from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select

from app.config import get_settings
from app.repositories.models import ConfigGroup, ConfigTestRun, ConfigVersion, OutboxEvent
from app.services.configuration import ConfigConnectionTester, TestOutcome
from app.services.image_executor import ImageJobExecutor
from app.services.jobs import (
    ClaimedJob,
    JobService,
    PermanentJobError,
    RetryableJobError,
)
from app.services.logging import job_id_context
from app.workers.common import record_process_heartbeat, shutdown_service, startup_service

logger = logging.getLogger(__name__)
settings = get_settings()
job_service = JobService()


@dataclass(frozen=True, slots=True)
class JobExecutionResult:
    output_asset_id: uuid.UUID | None = None
    provider_request_id: str | None = None
    metrics: dict[str, Any] | None = None


async def startup(ctx: dict[str, Any]) -> None:
    await startup_service(ctx, "worker")
    runtime = ctx["runtime"]
    ctx["image_job_executor"] = ImageJobExecutor(
        ctx["settings"],
        runtime.database,
        runtime.object_storage,
        config_cache=runtime.config_cache,
    )


async def publish_outbox_events(ctx: dict[str, Any]) -> int:
    runtime = ctx["runtime"]
    published = 0
    async with runtime.database.session_factory() as session:
        events = list(
            await session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.status == "pending",
                    OutboxEvent.available_at <= datetime.now(UTC),
                )
                .order_by(OutboxEvent.created_at)
                .limit(50)
                .with_for_update(skip_locked=True)
            )
        )
        for event in events:
            try:
                await runtime.redis.publish(
                    f"events:{event.topic}",
                    json.dumps(
                        {
                            "id": str(event.id),
                            "aggregate_type": event.aggregate_type,
                            "aggregate_id": str(event.aggregate_id),
                            "payload": event.payload,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    ),
                )
            except Exception:
                event.attempts += 1
                event.status = "failed" if event.attempts >= 10 else "pending"
                logger.exception(
                    "outbox publish failed",
                    extra={
                        "operation": "outbox_publish",
                        "status": "failed",
                        "job_id": str(event.id),
                    },
                )
            else:
                event.attempts += 1
                event.status = "published"
                published += 1
        await session.commit()
    return published


async def reconcile_foundation(ctx: dict[str, Any]) -> dict[str, int]:
    published = await publish_outbox_events(ctx)
    return {"outbox_events_published": published}


async def execute_image_job(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    runtime = ctx["runtime"]
    parsed_job_id = uuid.UUID(job_id)
    request_id = f"worker:{parsed_job_id}"
    async with runtime.database.session_factory() as session:
        claim = await job_service.claim_job(
            session,
            job_id=parsed_job_id,
            worker_id=runtime.instance_name,
            request_id=request_id,
        )
        await session.commit()
    if claim is None:
        return {"job_id": job_id, "status": "ignored"}

    token = job_id_context.set(job_id)
    try:
        try:
            executor = ctx.get("image_job_executor")
            if executor is None:
                raise PermanentJobError(
                    "ASSET_EXECUTOR_NOT_READY",
                    "图片任务执行器未配置",
                )
            async with asyncio.timeout(claim.timeout_seconds):
                raw_result = await executor(claim)
            result = _execution_result(raw_result)
        except TimeoutError:
            return await _record_execution_failure(
                runtime,
                claim,
                code="JOB_TIMED_OUT",
                message="任务执行超时",
                retryable=False,
                timed_out=True,
                provider_request_id=None,
                request_id=request_id,
            )
        except RetryableJobError as exc:
            return await _record_execution_failure(
                runtime,
                claim,
                code=exc.code,
                message=exc.message,
                retryable=True,
                timed_out=False,
                provider_request_id=exc.provider_request_id,
                request_id=request_id,
            )
        except PermanentJobError as exc:
            return await _record_execution_failure(
                runtime,
                claim,
                code=exc.code,
                message=exc.message,
                retryable=False,
                timed_out=False,
                provider_request_id=exc.provider_request_id,
                request_id=request_id,
            )
        except Exception:
            logger.exception(
                "unexpected image job execution failure",
                extra={"operation": claim.operation_code, "status": "failed"},
            )
            return await _record_execution_failure(
                runtime,
                claim,
                code="JOB_EXECUTION_ERROR",
                message="图片任务执行失败",
                retryable=True,
                timed_out=False,
                provider_request_id=None,
                request_id=request_id,
            )

        try:
            async with runtime.database.session_factory() as session:
                accepted = await job_service.complete_job(
                    session,
                    claim=claim,
                    output_asset_id=result.output_asset_id,
                    provider_request_id=result.provider_request_id,
                    metrics=result.metrics,
                    request_id=request_id,
                )
                await session.commit()
        except Exception:
            await _discard_output(executor, result.output_asset_id)
            raise
        if not accepted:
            await _discard_output(executor, result.output_asset_id)
        elif result.output_asset_id is not None:
            try:
                await _publish_output(executor, result.output_asset_id, claim.job_id)
            except Exception:
                logger.exception(
                    "image job output publish failed; scheduler will reconcile",
                    extra={"operation": claim.operation_code, "status": "pending"},
                )
        return {"job_id": job_id, "status": "succeeded" if accepted else "ignored"}
    finally:
        job_id_context.reset(token)


async def execute_config_test(ctx: dict[str, Any], test_run_id: str) -> dict[str, Any]:
    runtime = ctx["runtime"]
    run_id = uuid.UUID(test_run_id)
    async with runtime.database.session_factory() as session:
        row = (
            await session.execute(
                select(ConfigTestRun, ConfigGroup, ConfigVersion)
                .join(ConfigGroup, ConfigGroup.id == ConfigTestRun.group_id)
                .join(ConfigVersion, ConfigVersion.id == ConfigTestRun.config_version_id)
                .where(ConfigTestRun.id == run_id)
            )
        ).one_or_none()
        if row is None or row[0].status != "running":
            return {"test_run_id": test_run_id, "status": "ignored"}
        run, group, version = row
        actor_user_id = run.requested_by
        try:
            resolved = await runtime.config_service.resolved(session, group.code, version.version)
        except Exception as exc:  # noqa: BLE001
            outcome = TestOutcome(
                False,
                "CONFIG_LOAD_FAILED",
                f"配置加载失败（{exc.__class__.__name__}）",
                0,
            )
        else:
            outcome = await ConfigConnectionTester(ctx["settings"]).test(resolved)
        runtime.config_service.finish_test(
            session,
            group=group,
            version=version,
            run=run,
            outcome=outcome,
            actor_user_id=actor_user_id,
            request_id=f"worker:config-test:{run.id}",
        )
        await session.commit()
    return {"test_run_id": test_run_id, "status": run.status}


def _execution_result(value: Any) -> JobExecutionResult:
    if isinstance(value, JobExecutionResult):
        return value
    if isinstance(value, dict):
        output_asset_id = value.get("output_asset_id")
        return JobExecutionResult(
            output_asset_id=uuid.UUID(str(output_asset_id)) if output_asset_id else None,
            provider_request_id=value.get("provider_request_id"),
            metrics=dict(value.get("metrics") or {}),
        )
    raise PermanentJobError("INVALID_EXECUTOR_RESULT", "图片执行器返回了无效结果")


async def _discard_output(executor: Any, asset_id: uuid.UUID | None) -> None:
    callback = getattr(executor, "discard_output", None)
    if callback is not None and asset_id is not None:
        await callback(asset_id)


async def _publish_output(executor: Any, asset_id: uuid.UUID, job_id: uuid.UUID) -> None:
    callback = getattr(executor, "publish_output", None)
    if callback is not None:
        await callback(asset_id, job_id)


async def _record_execution_failure(
    runtime,
    claim: ClaimedJob,
    *,
    code: str,
    message: str,
    retryable: bool,
    timed_out: bool,
    provider_request_id: str | None,
    request_id: str,
) -> dict[str, Any]:
    async with runtime.database.session_factory() as session:
        result = await job_service.fail_job(
            session,
            claim=claim,
            code=code,
            message=message,
            retryable=retryable,
            timed_out=timed_out,
            provider_request_id=provider_request_id,
            metrics={},
            request_id=request_id,
        )
        await session.commit()
    return {"job_id": str(claim.job_id), "status": result or "ignored"}


class WorkerSettings:
    functions: ClassVar[list[Any]] = [
        execute_image_job,
        execute_config_test,
        publish_outbox_events,
        reconcile_foundation,
    ]
    cron_jobs: ClassVar[list[Any]] = [
        cron(record_process_heartbeat, second={0, 30}, unique=False),
        cron(publish_outbox_events, second={5, 20, 35, 50}, unique=False),
    ]
    on_startup = startup
    on_shutdown = shutdown_service
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    queue_name = settings.worker_queue_name
    max_jobs = settings.worker_max_jobs
    job_timeout = settings.worker_job_timeout_seconds
    keep_result = 0
    allow_abort_jobs = True
