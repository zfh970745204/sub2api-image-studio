from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from arq.connections import RedisSettings, create_pool
from fastapi import APIRouter, Depends, Request, Response, status

from app.api.dependencies import Principal, get_current_principal, require_permission
from app.config import Settings
from app.repositories.service_instances import list_recent_instances
from app.schemas import HealthResponse
from app.services.configuration import runtime_config_value
from app.services.runtime import RuntimeServices

public_router = APIRouter(prefix="/api", tags=["system"])
v1_router = APIRouter(prefix="/api/v1", tags=["system"])
admin_router = APIRouter(prefix="/api/v1/admin/system", tags=["admin-system"])


def _runtime(request: Request) -> RuntimeServices:
    return request.app.state.runtime_services


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@public_router.get("/health", response_model=HealthResponse)
async def public_health(request: Request, response: Response) -> HealthResponse:
    dependency_status = await _runtime(request).public_status()
    if dependency_status == "degraded":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(status=dependency_status)


@v1_router.get("/system/status")
async def system_status(
    request: Request,
    _principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict[str, Any]:
    settings = _settings(request)
    sub2api_configured = settings.sub2api_configured
    password_reset_enabled = bool(
        await runtime_config_value(
            _runtime(request), "general", "password_reset_enabled", settings.password_reset_enabled
        )
    )
    try:
        sub2api_config = await _runtime(request).config_cache.get("sub2api")
        sub2api_configured = bool(
            sub2api_config.values.get("enabled")
            and sub2api_config.values.get("base_url")
            and sub2api_config.secrets.get("api_key")
        )
    except Exception:  # noqa: BLE001
        sub2api_configured = settings.sub2api_configured
    return {
        "status": await _runtime(request).public_status(),
        "features": {
            "task_queue": True,
            "legacy_sync_api": settings.legacy_sync_api_enabled,
            "sub2api_configured": sub2api_configured,
            "password_reset": password_reset_enabled,
        },
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


@admin_router.get("/health")
async def detailed_health(
    request: Request,
    _principal: Annotated[Principal, Depends(require_permission("system.diagnostics.read"))],
) -> dict[str, Any]:
    checks = await _runtime(request).health_checks()
    overall = "ok" if all(item.status != "degraded" for item in checks) else "degraded"
    return {"status": overall, "dependencies": [item.as_dict() for item in checks]}


@admin_router.get("/workers")
async def workers(
    request: Request,
    _principal: Annotated[Principal, Depends(require_permission("system.health.read"))],
) -> dict[str, Any]:
    runtime = _runtime(request)
    async with runtime.database.session_factory() as session:
        instances = await list_recent_instances(session)
    return {
        "items": [
            {
                "id": str(instance.id),
                "service_type": instance.service_type,
                "instance_name": instance.instance_name,
                "version": instance.version,
                "last_heartbeat_at": instance.last_heartbeat_at,
                "metadata": instance.instance_metadata,
            }
            for instance in instances
        ]
    }


@admin_router.post("/jobs/reconcile", status_code=status.HTTP_202_ACCEPTED)
async def reconcile_jobs(
    request: Request,
    _principal: Annotated[Principal, Depends(require_permission("system.maintenance.execute"))],
) -> dict[str, str]:
    settings = _settings(request)
    queue = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        job = await queue.enqueue_job(
            "reconcile_image_job_ledger", _queue_name=settings.scheduler_queue_name
        )
    finally:
        await queue.aclose()
    if job is None:
        return {"status": "duplicate"}
    return {"status": "queued", "job_id": job.job_id}
