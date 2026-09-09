from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from redis.asyncio import Redis

from app.config import Settings
from app.object_storage import build_object_storage
from app.repositories.database import Database
from app.repositories.service_instances import record_heartbeat
from app.services.configuration import (
    ConfigCipher,
    ConfigService,
    DynamicObjectStorage,
    RuntimeConfigCache,
)

logger = logging.getLogger(__name__)

DependencyState = Literal["ok", "degraded", "not_configured"]


@dataclass(slots=True)
class DependencyCheck:
    name: str
    status: DependencyState
    latency_ms: float | None
    checked_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class RuntimeServices:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(
            settings.database_url,
            timeout_seconds=settings.dependency_timeout_seconds,
        )
        self.redis: Redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=settings.dependency_timeout_seconds,
            socket_connect_timeout=settings.dependency_timeout_seconds,
        )
        self.config_service = ConfigService(ConfigCipher.from_settings(settings))
        self.config_cache = RuntimeConfigCache(
            self.database.session_factory,
            self.config_service,
            ttl_seconds=settings.config_cache_ttl_seconds,
            load_timeout_seconds=settings.dependency_timeout_seconds,
        )
        fallback_storage = build_object_storage(settings)
        self.object_storage = DynamicObjectStorage(
            settings,
            self.config_cache,
            fallback=fallback_storage,
        )
        self.instance_name = f"{settings.service_instance_name}-{os.getpid()}"

    async def close(self) -> None:
        await self.redis.aclose()
        await self.database.dispose()

    async def public_status(self) -> Literal["ok", "degraded"]:
        if not self.settings.dependency_checks_enabled:
            return "ok"
        checks = await self.health_checks()
        return "ok" if all(check.status != "degraded" for check in checks) else "degraded"

    async def health_checks(self) -> list[DependencyCheck]:
        database, redis = await asyncio.gather(
            self._check("postgres", self.database.ping),
            self._check("redis", self.redis.ping),
        )
        worker, scheduler = await asyncio.gather(
            self._check_heartbeat("worker"),
            self._check_heartbeat("scheduler"),
        )
        sub2api_configured = self.settings.sub2api_configured
        r2_configured = self.settings.r2_configured
        try:
            sub2api_config = await self.config_cache.get("sub2api")
            sub2api_configured = bool(
                sub2api_config.values.get("enabled")
                and sub2api_config.values.get("base_url")
                and sub2api_config.secrets.get("api_key")
            )
        except Exception:  # noqa: BLE001
            logger.debug("active Sub2API config is not available")
        try:
            r2_config = await self.config_cache.get("r2")
            r2_configured = bool(
                r2_config.values.get("enabled")
                and r2_config.values.get("endpoint_url")
                and r2_config.values.get("bucket")
                and r2_config.secrets.get("access_key_id")
                and r2_config.secrets.get("secret_access_key")
            )
        except Exception:  # noqa: BLE001
            logger.debug("active R2 config is not available")
        sub2api_state: DependencyState = "ok" if sub2api_configured else "not_configured"
        if r2_configured:
            r2 = await self._check("r2", self.object_storage.ping)
        else:
            r2 = DependencyCheck(
                "r2",
                "not_configured",
                None,
                datetime.now(UTC).isoformat(timespec="seconds"),
            )
        now = datetime.now(UTC).isoformat(timespec="seconds")
        return [
            database,
            redis,
            worker,
            scheduler,
            r2,
            DependencyCheck("sub2api", sub2api_state, None, now),
        ]

    async def record_service_heartbeat(
        self, service_type: str, *, metadata: dict[str, Any] | None = None
    ) -> None:
        combined_metadata = {"configuration": self.config_cache.status(), **(metadata or {})}
        heartbeat = {
            "service_type": service_type,
            "instance_name": self.instance_name,
            "version": self.settings.app_version,
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "metadata": combined_metadata,
        }
        async with self.database.session_factory() as session:
            await record_heartbeat(
                session,
                service_type=service_type,
                instance_name=self.instance_name,
                version=self.settings.app_version,
                metadata=combined_metadata,
            )
            await session.commit()
        ttl = self.settings.service_heartbeat_seconds * 3
        serialized = json.dumps(heartbeat, ensure_ascii=False, separators=(",", ":"))
        async with self.redis.pipeline(transaction=False) as pipeline:
            pipeline.set(f"service:{service_type}:{self.instance_name}", serialized, ex=ttl)
            pipeline.set(f"service:{service_type}:latest", serialized, ex=ttl)
            await pipeline.execute()

    async def heartbeat_loop(self, service_type: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.record_service_heartbeat(service_type)
            except Exception:
                logger.exception(
                    "service heartbeat failed",
                    extra={"operation": "service_heartbeat", "status": "failed"},
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.service_heartbeat_seconds)
            except TimeoutError:
                continue

    async def broadcast_config_invalidation(self, group: str, version: int) -> None:
        self.config_cache.invalidate(group)
        if group == "r2":
            await self.object_storage.refresh()
        try:
            await self.redis.publish(
                "config.invalidate",
                json.dumps({"group": group, "version": version}, separators=(",", ":")),
            )
        except Exception:
            logger.exception(
                "config invalidation publish failed; TTL refresh remains active",
                extra={"operation": "config_invalidate", "status": "degraded"},
            )

    async def config_invalidation_loop(self, stop: asyncio.Event) -> None:
        pubsub = self.redis.pubsub()
        try:
            await pubsub.subscribe("config.invalidate")
            while not stop.is_set():
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is not None:
                    try:
                        payload = json.loads(message["data"])
                        group = payload.get("group")
                    except (KeyError, TypeError, ValueError):
                        group = None
                    self.config_cache.invalidate(group if isinstance(group, str) else None)
                    if isinstance(group, str):
                        try:
                            await self.config_cache.get(group)
                            if group == "r2":
                                await self.object_storage.refresh()
                        except Exception:
                            logger.exception(
                                "published configuration could not be loaded; retaining last good",
                                extra={"operation": "config_load", "status": "degraded"},
                            )
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "config invalidation listener stopped; TTL refresh remains active",
                extra={"operation": "config_listener", "status": "degraded"},
            )
        finally:
            await pubsub.aclose()

    async def preload_configuration(self) -> None:
        for group in ("sub2api", "r2", "email", "general"):
            try:
                await self.config_cache.get(group)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "configuration group is not active during preload", extra={"group": group}
                )
                continue
        await self.object_storage.refresh()

    async def _check(self, name: str, operation) -> DependencyCheck:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self.settings.dependency_timeout_seconds):
                await operation()
        # Health checks collapse driver-specific failures into a stable public state.
        except Exception:  # noqa: BLE001
            status: DependencyState = "degraded"
        else:
            status = "ok"
        return DependencyCheck(
            name=name,
            status=status,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    async def _check_heartbeat(self, service_type: str) -> DependencyCheck:
        return await self._check(
            service_type,
            lambda: self._require_heartbeat(service_type),
        )

    async def _require_heartbeat(self, service_type: str) -> None:
        value = await self.redis.get(f"service:{service_type}:latest")
        if value is None:
            raise RuntimeError(f"{service_type} heartbeat missing")
