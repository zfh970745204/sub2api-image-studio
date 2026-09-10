from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError
from app.domain.ids import uuid7
from app.repositories.models import AccessBlock, OutboxEvent, RateLimitPolicy, SecurityEvent

logger = logging.getLogger(__name__)
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "ciphertext",
        "cookie",
        "image",
        "password",
        "prompt",
        "secret",
        "secret_access_key",
        "token",
    }
)


@dataclass(frozen=True, slots=True)
class PolicyDefault:
    code: str
    name: str
    scope: str
    request_limit: int
    window_seconds: int


POLICY_DEFAULTS = (
    PolicyDefault("login", "登录", "ip", 25, 900),
    PolicyDefault("register", "公开注册", "ip", 5, 3600),
    PolicyDefault("upload", "素材上传", "both", 20, 3600),
    PolicyDefault("job_create", "任务创建", "both", 30, 60),
    PolicyDefault("quote", "任务报价", "both", 60, 60),
    PolicyDefault("download", "素材下载", "both", 120, 60),
)
POLICY_BY_CODE = {item.code: item for item in POLICY_DEFAULTS}


def utcnow() -> datetime:
    return datetime.now(UTC)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).casefold() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, bytes):
        return "[BINARY REDACTED]"
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class SecurityService:
    def __init__(self, settings, runtime=None) -> None:
        self.settings = settings
        self.runtime = runtime
        self._local_counters: dict[str, tuple[int, float]] = {}
        self._counter_lock = asyncio.Lock()

    async def ensure_default_policies(self, session: AsyncSession) -> list[RateLimitPolicy]:
        rows = {item.code: item for item in (await session.scalars(select(RateLimitPolicy))).all()}
        for default in POLICY_DEFAULTS:
            if default.code not in rows:
                item = RateLimitPolicy(
                    code=default.code,
                    name=default.name,
                    scope=default.scope,
                    request_limit=default.request_limit,
                    window_seconds=default.window_seconds,
                    enabled=True,
                )
                session.add(item)
                rows[item.code] = item
        await session.flush()
        return [rows[item.code] for item in POLICY_DEFAULTS]

    async def policy(self, session: AsyncSession, code: str) -> RateLimitPolicy | PolicyDefault:
        item = await session.get(RateLimitPolicy, code)
        if item is not None:
            return item
        default = POLICY_BY_CODE.get(code)
        if default is None:
            raise RuntimeError(f"unknown rate-limit policy: {code}")
        return default

    def subject_hash(self, subject_type: str, value: str) -> str:
        normalized = value.strip().casefold()
        if subject_type == "ip_fingerprint" and HEX_64.fullmatch(normalized):
            return normalized
        return self._fingerprint(normalized)

    def _fingerprint(self, value: str) -> str:
        import hashlib
        import hmac

        return hmac.new(
            self.settings.auth_hash_salt.encode(), value.encode(), hashlib.sha256
        ).hexdigest()

    async def ensure_not_blocked(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | None,
        ip_hash: str,
        request_id: str,
    ) -> None:
        now = utcnow()
        subjects = [("ip_fingerprint", ip_hash)]
        if user_id is not None:
            subjects.append(("user", self.subject_hash("user", str(user_id))))
        clauses = [
            and_(AccessBlock.subject_type == subject_type, AccessBlock.subject_hash == subject_hash)
            for subject_type, subject_hash in subjects
        ]
        block = (
            await session.scalars(
                select(AccessBlock)
                .where(
                    or_(*clauses),
                    AccessBlock.starts_at <= now,
                    or_(AccessBlock.ends_at.is_(None), AccessBlock.ends_at > now),
                )
                .order_by(AccessBlock.created_at.desc())
                .limit(1)
            )
        ).one_or_none()
        if block is None:
            return
        self.record_audit(
            session,
            action="access_block.denied",
            target_type=block.subject_type,
            target_id=block.id,
            actor_user_id=user_id,
            request_id=request_id,
            result="denied",
            details={"block_id": str(block.id), "reason": block.reason},
        )
        raise ApiError(403, "ACCESS_BLOCKED", "当前访问已被安全策略阻止")

    async def enforce_request(
        self,
        session: AsyncSession,
        *,
        policy_code: str,
        user_id: uuid.UUID | None,
        ip_hash: str,
        request_id: str,
    ) -> None:
        policy = await self.policy(session, policy_code)
        if not getattr(policy, "enabled", True):
            return
        subjects: list[tuple[str, str]] = []
        if policy.scope in {"ip", "both"}:
            subjects.append(("ip", ip_hash))
        if policy.scope in {"user", "both"} and user_id is not None:
            subjects.append(("user", self.subject_hash("user", str(user_id))))
        for dimension, subject in subjects:
            count, retry_after = await self._increment(
                policy.code, dimension, subject, policy.window_seconds
            )
            if count <= policy.request_limit:
                continue
            if count == policy.request_limit + 1:
                self.record_event(
                    session,
                    event_type="rate_limit_exceeded",
                    severity="medium",
                    user_id=user_id,
                    ip_hash=ip_hash,
                    request_id=request_id,
                    details={"policy": policy.code, "dimension": dimension},
                )
            self.record_audit(
                session,
                action="rate_limit.denied",
                target_type="rate_limit_policy",
                target_id=None,
                actor_user_id=user_id,
                request_id=request_id,
                result="denied",
                details={"policy": policy.code, "dimension": dimension},
            )
            raise ApiError(
                429,
                "RATE_LIMITED",
                "请求过于频繁，请稍后重试",
                {"policy": policy.code, "retry_after": retry_after},
            )

    async def enforce_login(self, request, *, policy_code: str = "login") -> None:
        database = request.app.state.runtime_services.database
        async with database.session_factory() as session:
            try:
                await self.ensure_not_blocked(
                    session,
                    user_id=None,
                    ip_hash=request.state.ip_hash,
                    request_id=request.state.request_id,
                )
                await self.enforce_request(
                    session,
                    policy_code=policy_code,
                    user_id=None,
                    ip_hash=request.state.ip_hash,
                    request_id=request.state.request_id,
                )
            except ApiError:
                await session.commit()
                raise
            else:
                await session.commit()

    async def enforce_authenticated(
        self, request, session: AsyncSession, user_id: uuid.UUID
    ) -> None:
        await self.ensure_not_blocked(
            session,
            user_id=user_id,
            ip_hash=request.state.ip_hash,
            request_id=request.state.request_id,
        )
        policy = self.policy_for_request(request.method, request.url.path)
        if policy:
            await self.enforce_request(
                session,
                policy_code=policy,
                user_id=user_id,
                ip_hash=request.state.ip_hash,
                request_id=request.state.request_id,
            )

    @staticmethod
    def policy_for_request(method: str, path: str) -> str | None:
        if method != "POST":
            return None
        if path == "/api/v1/assets/upload":
            return "upload"
        if path == "/api/v1/jobs/quote":
            return "quote"
        if path == "/api/v1/jobs":
            return "job_create"
        if path.endswith("/download-url") and "/assets/" in path:
            return "download"
        return None

    async def _increment(
        self, policy: str, dimension: str, subject: str, window_seconds: int
    ) -> tuple[int, int]:
        now = time.time()
        bucket = int(now // window_seconds)
        key = f"security:rate:{policy}:{dimension}:{subject}:{bucket}"
        if self.runtime is not None and self.settings.dependency_checks_enabled:
            try:
                async with self.runtime.redis.pipeline(transaction=True) as pipeline:
                    pipeline.incr(key)
                    pipeline.expire(key, window_seconds + 5)
                    result = await pipeline.execute()
                return int(result[0]), max(1, int((bucket + 1) * window_seconds - now))
            except Exception:
                logger.exception(
                    "distributed rate limiter unavailable; using local fallback",
                    extra={"operation": "rate_limit", "status": "degraded"},
                )
        async with self._counter_lock:
            count, expires_at = self._local_counters.get(key, (0, (bucket + 1) * window_seconds))
            count += 1
            self._local_counters[key] = (count, expires_at)
            if len(self._local_counters) > 10_000:
                self._local_counters = {
                    item_key: item
                    for item_key, item in self._local_counters.items()
                    if item[1] > now
                }
        return count, max(1, int(expires_at - now))

    @staticmethod
    def record_event(
        session: AsyncSession,
        *,
        event_type: str,
        severity: str,
        user_id: uuid.UUID | None,
        ip_hash: str | None,
        request_id: str | None,
        details: dict[str, Any],
    ) -> SecurityEvent:
        event = SecurityEvent(
            id=uuid7(),
            event_type=event_type,
            severity=severity,
            user_id=user_id,
            ip_hash=ip_hash,
            request_id=request_id,
            status="open",
            details_redacted=redact(details),
            created_at=utcnow(),
        )
        session.add(event)
        return event

    @staticmethod
    def record_audit(
        session: AsyncSession,
        *,
        action: str,
        target_type: str,
        target_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
        request_id: str,
        result: str = "success",
        details: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            OutboxEvent(
                id=uuid7(),
                topic="security.audit",
                aggregate_type=target_type,
                aggregate_id=target_id or uuid7(),
                payload={
                    "action": action,
                    "actor_user_id": str(actor_user_id) if actor_user_id else None,
                    "request_id": request_id,
                    "result": result,
                    "details": redact(details or {}),
                },
                status="pending",
                attempts=0,
                available_at=utcnow(),
                version=1,
            )
        )

    def counter_snapshot(self) -> dict[str, int]:
        now = time.time()
        snapshot: dict[str, int] = {}
        for key, (count, expires_at) in self._local_counters.items():
            if expires_at > now:
                code = key.split(":", 4)[2]
                snapshot[code] = snapshot.get(code, 0) + count
        return snapshot
