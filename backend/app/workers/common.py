from __future__ import annotations

import asyncio
from typing import Any

from app.config import get_settings
from app.services.logging import configure_logging
from app.services.runtime import RuntimeServices


async def startup_service(ctx: dict[str, Any], service_type: str) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    runtime = RuntimeServices(settings)
    ctx["settings"] = settings
    ctx["runtime"] = runtime
    ctx["service_type"] = service_type
    ctx["queue_name"] = (
        settings.worker_queue_name if service_type == "worker" else settings.scheduler_queue_name
    )
    await runtime.database.ping()
    await runtime.redis.ping()
    await runtime.preload_configuration()
    await runtime.record_service_heartbeat(
        service_type,
        metadata={"queue_name": ctx["queue_name"]},
    )
    ctx["config_stop"] = asyncio.Event()
    ctx["config_listener_task"] = asyncio.create_task(
        runtime.config_invalidation_loop(ctx["config_stop"])
    )


async def shutdown_service(ctx: dict[str, Any]) -> None:
    runtime: RuntimeServices = ctx["runtime"]
    stop = ctx.get("config_stop")
    if stop is not None:
        stop.set()
    task = ctx.get("config_listener_task")
    if task is not None:
        await task
    await runtime.close()


async def record_process_heartbeat(ctx: dict[str, Any]) -> None:
    runtime: RuntimeServices = ctx["runtime"]
    await runtime.record_service_heartbeat(
        ctx["service_type"],
        metadata={"queue_name": ctx["queue_name"]},
    )
