from __future__ import annotations

import logging
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select

from app.config import get_settings
from app.repositories.models import ImageJob
from app.repositories.service_instances import delete_stale_instances
from app.services.assets import AssetService
from app.services.jobs import JobService
from app.services.memberships import EntitlementService
from app.services.points import PointService
from app.storage import ResultStore
from app.workers.common import record_process_heartbeat, shutdown_service, startup_service
from app.workers.worker import reconcile_foundation

settings = get_settings()
logger = logging.getLogger(__name__)
membership_service = EntitlementService()
point_service = PointService()
job_service = JobService()
asset_service = AssetService()


async def startup(ctx: dict[str, Any]) -> None:
    await startup_service(ctx, "scheduler")


async def cleanup_runtime_records(ctx: dict[str, Any]) -> dict[str, int]:
    runtime = ctx["runtime"]
    async with runtime.database.session_factory() as session:
        stale_instances = await delete_stale_instances(session)
        await session.commit()
    removed_results = 0
    if settings.legacy_sync_api_enabled:
        store = ResultStore(settings.result_dir, settings.result_ttl_hours)
        removed_results = store.cleanup()
    return {
        "stale_service_instances": stale_instances,
        "legacy_results_removed": removed_results,
    }


async def reconcile_memberships(ctx: dict[str, Any]) -> dict[str, int]:
    runtime = ctx["runtime"]
    async with runtime.database.session_factory() as session:
        processed = await membership_service.reconcile_due_memberships(session)
        await session.commit()
    return {"memberships_reconciled": processed}


async def reconcile_point_accounts(ctx: dict[str, Any]) -> dict[str, int]:
    runtime = ctx["runtime"]
    async with runtime.database.session_factory() as session:
        result = await point_service.reconcile_accounts(session)
        await session.commit()
    return {"point_accounts_checked": result.checked, "point_accounts_frozen": result.frozen}


async def dispatch_image_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    runtime = ctx["runtime"]
    request_id = "scheduler:image-job-dispatch"
    async with runtime.database.session_factory() as session:
        released = await job_service.release_due_retries(session, request_id=request_id)
        jobs = await job_service.dispatchable_jobs(session)
        await session.commit()
    queued = 0
    for job_id, queue_name in jobs:
        result = await ctx["redis"].enqueue_job(
            "execute_image_job",
            str(job_id),
            _job_id=f"image-job:{job_id}",
            _queue_name=queue_name,
        )
        if result is not None:
            queued += 1
    return {"retries_released": released, "jobs_dispatched": queued}


async def reconcile_stale_image_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    runtime = ctx["runtime"]
    async with runtime.database.session_factory() as session:
        result = await job_service.reconcile_stale_jobs(
            session, request_id="scheduler:image-job-timeouts"
        )
        await session.commit()
    return {
        "running_checked": result.checked,
        "jobs_timed_out": result.timed_out,
        "refunds_created": result.refunds_created,
    }


async def reconcile_image_job_ledger(ctx: dict[str, Any]) -> dict[str, int]:
    runtime = ctx["runtime"]
    checked = 0
    refunds = 0
    mismatches = 0
    async with runtime.database.session_factory() as read_session:
        job_ids = list((await read_session.scalars(select(ImageJob.id))).all())
    for job_id in job_ids:
        async with runtime.database.session_factory() as session:
            try:
                result = await job_service.reconcile_job(
                    session,
                    job_id=job_id,
                    request_id="scheduler:image-job-ledger",
                )
                await session.commit()
            except Exception:
                await session.rollback()
                mismatches += 1
                logger.exception(
                    "image job reconciliation failed",
                    extra={
                        "operation": "image_job_reconciliation",
                        "job_id": str(job_id),
                        "status": "failed",
                    },
                )
                continue
        checked += result.checked
        refunds += result.refunds_created
        mismatches += result.mismatches
    return {
        "jobs_checked": checked,
        "refunds_created": refunds,
        "mismatches": mismatches,
    }


async def reconcile_asset_lifecycle(ctx: dict[str, Any]) -> dict[str, int]:
    runtime = ctx["runtime"]
    if not runtime.object_storage.bucket:
        return {
            "assets_expired": 0,
            "retention_notices_created": 0,
            "uploads_checked": 0,
            "objects_deleted": 0,
        }
    notices = await asset_service.notify_expiring_assets(runtime.database)
    expired = await asset_service.expire_assets(
        runtime.database,
        grace_days=ctx["settings"].asset_delete_grace_days,
    )
    uploads = await asset_service.reconcile_stale_uploads(runtime.database, runtime.object_storage)
    deleted = await asset_service.process_deletion_queue(runtime.database, runtime.object_storage)
    return {
        "assets_expired": expired,
        "retention_notices_created": notices,
        "uploads_checked": uploads["checked"],
        "objects_deleted": deleted["completed"],
    }


async def reconcile_storage_orphans(ctx: dict[str, Any]) -> dict[str, int]:
    runtime = ctx["runtime"]
    if not runtime.object_storage.bucket:
        return {
            "orphan_objects_removed": 0,
            "orphan_objects_skipped": 0,
            "missing_assets_quarantined": 0,
        }
    async with runtime.database.session_factory() as session:
        result = await asset_service.reconcile_orphans(
            session,
            runtime.object_storage,
            grace_hours=ctx["settings"].asset_orphan_grace_hours,
        )
        await session.commit()
    return result


class SchedulerSettings:
    functions: ClassVar[list[Any]] = [
        cleanup_runtime_records,
        reconcile_foundation,
        reconcile_memberships,
        reconcile_point_accounts,
        dispatch_image_jobs,
        reconcile_stale_image_jobs,
        reconcile_image_job_ledger,
        reconcile_asset_lifecycle,
        reconcile_storage_orphans,
    ]
    cron_jobs: ClassVar[list[Any]] = [
        cron(record_process_heartbeat, second={0, 30}, unique=False),
        cron(dispatch_image_jobs, second={0, 10, 20, 30, 40, 50}, unique=False),
        cron(reconcile_stale_image_jobs, minute=set(range(60))),
        cron(reconcile_foundation, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(reconcile_memberships, minute=set(range(60))),
        cron(cleanup_runtime_records, hour={2}, minute={15}),
        cron(reconcile_point_accounts, hour={3}, minute={15}),
        cron(reconcile_image_job_ledger, hour={3}, minute={30}),
        cron(reconcile_asset_lifecycle, minute=set(range(60))),
        cron(reconcile_storage_orphans, hour={4}, minute={15}),
    ]
    on_startup = startup
    on_shutdown = shutdown_service
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    queue_name = settings.scheduler_queue_name
    max_jobs = 1
    job_timeout = 300
    keep_result = 0
