from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.models import ServiceInstance


async def record_heartbeat(
    session: AsyncSession,
    *,
    service_type: str,
    instance_name: str,
    version: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    now = datetime.now(UTC)
    statement = insert(ServiceInstance).values(
        service_type=service_type,
        instance_name=instance_name,
        version=version,
        last_heartbeat_at=now,
        instance_metadata=metadata or {},
        updated_at=now,
    )
    statement = statement.on_conflict_do_update(
        constraint="uq_service_instances_type_name",
        set_={
            "version": statement.excluded.version,
            "last_heartbeat_at": statement.excluded.last_heartbeat_at,
            "metadata": statement.excluded.metadata,
            "updated_at": statement.excluded.updated_at,
        },
    )
    await session.execute(statement)


async def list_recent_instances(
    session: AsyncSession, *, max_age_seconds: int = 90
) -> list[ServiceInstance]:
    threshold = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
    result = await session.scalars(
        select(ServiceInstance)
        .where(ServiceInstance.last_heartbeat_at >= threshold)
        .order_by(ServiceInstance.service_type, ServiceInstance.instance_name)
    )
    return list(result)


async def delete_stale_instances(session: AsyncSession, *, older_than_hours: int = 24) -> int:
    threshold = datetime.now(UTC) - timedelta(hours=older_than_hours)
    result = await session.execute(
        delete(ServiceInstance).where(ServiceInstance.last_heartbeat_at < threshold)
    )
    return result.rowcount or 0
