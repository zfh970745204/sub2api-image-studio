from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.auth import router as auth_router
from app.api.errors import ApiError, install_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.security import router as security_router
from app.config import Settings
from app.domain.ids import uuid7
from app.repositories.database import Database
from app.repositories.models import (
    AuditLog,
    Base,
    OutboxEvent,
    RateLimitPolicy,
    Role,
    SecurityEvent,
    User,
    UserRole,
)
from app.services.auth import AuthService
from app.services.image_executor import UpstreamCircuitBreaker
from app.services.jobs import JobService, RetryableJobError
from app.services.rbac import sync_builtin_rbac
from app.services.security import SecurityService


def test_prompt_guard_and_upstream_circuit_recovery() -> None:
    with pytest.raises(ApiError, match="5000"):
        JobService.canonical_parameters({"prompt": "x" * 5_001})
    with pytest.raises(ApiError, match="控制字符"):
        JobService.canonical_parameters({"instruction": "visible\x00hidden"})

    circuit = UpstreamCircuitBreaker(failure_threshold=5, recovery_seconds=60)
    for _ in range(4):
        assert circuit.failed() is False
    assert circuit.failed() is True
    with pytest.raises(RetryableJobError) as blocked:
        circuit.before_call()
    assert blocked.value.code == "SUB2API_CIRCUIT_OPEN"
    circuit.opened_until = 0
    circuit.before_call()
    circuit.succeeded()
    assert circuit.snapshot() == {
        "open": False,
        "consecutive_failures": 0,
        "retry_after_seconds": 0,
    }


@dataclass
class SecurityRuntime:
    database: Database


@dataclass
class SecurityContext:
    app: FastAPI
    database: Database
    auth_service: AuthService
    security_service: SecurityService


@pytest_asyncio.fixture
async def security_context(tmp_path) -> SecurityContext:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'security.db').as_posix()}"
    settings = Settings(_env_file=None, APP_ENV="test", DATABASE_URL=database_url)
    database = Database(database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with database.session_factory() as session:
        await sync_builtin_rbac(session)
        await session.commit()
    runtime = SecurityRuntime(database)
    auth_service = AuthService(settings)
    security_service = SecurityService(settings, runtime)
    app = FastAPI()
    app.state.settings = settings
    app.state.runtime_services = runtime
    app.state.auth_service = auth_service
    app.state.security_service = security_service
    install_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(auth_router)
    app.include_router(security_router)
    yield SecurityContext(app, database, auth_service, security_service)
    await database.dispose()


async def seed_user(context: SecurityContext, email: str, roles: tuple[str, ...]) -> User:
    async with context.database.session_factory() as session:
        user = User(
            id=uuid7(),
            email=email,
            username=email.split("@", 1)[0],
            password_hash=context.auth_service.hash_password("correct horse battery staple"),
            display_name=email.split("@", 1)[0],
            status="active",
            session_version=1,
            permission_version=1,
        )
        session.add(user)
        await session.flush()
        role_rows = list((await session.scalars(select(Role).where(Role.code.in_(roles)))).all())
        session.add_all(
            UserRole(user_id=user.id, role_id=role.id, assigned_by=user.id) for role in role_rows
        )
        await session.commit()
        return user


def client_for(context: SecurityContext) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=context.app),
        base_url="http://testserver",
        headers={"user-agent": "security-test-device"},
    )


async def login(client: AsyncClient, email: str) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"identifier": email, "password": "correct horse battery staple"},
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_audit_projection_redacts_secrets_and_is_append_only(
    security_context: SecurityContext,
) -> None:
    admin = await seed_user(security_context, "audit.admin@example.com", ("super_admin",))
    async with security_context.database.session_factory() as session:
        session.add(
            OutboxEvent(
                id=uuid7(),
                topic="config.audit",
                aggregate_type="config_group",
                aggregate_id=uuid7(),
                payload={
                    "action": "config.changed",
                    "actor_user_id": str(admin.id),
                    "request_id": "secret-redaction-request",
                    "details": {
                        "api_key": "must-never-appear",
                        "prompt": "private customer prompt",
                        "change": "enabled",
                    },
                },
                status="pending",
                attempts=0,
                version=1,
            )
        )
        await session.commit()

    async with client_for(security_context) as client:
        await login(client, admin.email)
        response = await client.get("/api/v1/admin/audit-logs?action=config.changed")
        assert response.status_code == 200, response.text
        assert "must-never-appear" not in response.text
        assert "private customer prompt" not in response.text
        item = response.json()["items"][0]
        assert item["changes_redacted"]["api_key"] == "[REDACTED]"
        assert item["actor_role_snapshot"] == ["super_admin"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]

        exported = await client.post("/api/v1/admin/audit-logs/export")
        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith("text/csv")
        assert "must-never-appear" not in exported.text

    async with security_context.database.session_factory() as session:
        item = (
            await session.scalars(select(AuditLog).where(AuditLog.action == "config.changed"))
        ).one()
        item.reason = "attempted mutation"
        with pytest.raises(ValueError, match="append-only"):
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_security_event_resolution_permissions_and_user_block(
    security_context: SecurityContext,
) -> None:
    admin = await seed_user(security_context, "security.admin@example.com", ("super_admin",))
    auditor = await seed_user(security_context, "security.auditor@example.com", ("auditor",))
    target = await seed_user(security_context, "security.target@example.com", ("user",))
    async with security_context.database.session_factory() as session:
        event = SecurityEvent(
            id=uuid7(),
            event_type="job_failure_burst",
            severity="high",
            user_id=target.id,
            request_id="security-event-test",
            status="open",
            details_redacted={"failure_count": 5},
        )
        session.add(event)
        await session.commit()
        event_id = event.id

    async with (
        client_for(security_context) as admin_client,
        client_for(security_context) as auditor_client,
        client_for(security_context) as target_client,
    ):
        await login(admin_client, admin.email)
        await login(auditor_client, auditor.email)
        await login(target_client, target.email)
        assert (await auditor_client.get("/api/v1/admin/security/events")).status_code == 200
        denied = await auditor_client.post(
            f"/api/v1/admin/security/events/{event_id}/resolve",
            json={"status": "resolved", "reason": "read-only attempt"},
        )
        assert denied.status_code == 403
        resolved = await admin_client.post(
            f"/api/v1/admin/security/events/{event_id}/resolve",
            json={"status": "resolved", "reason": "reviewed supporting evidence"},
        )
        assert resolved.status_code == 200
        assert resolved.json()["security_event"]["status"] == "resolved"

        blocked = await admin_client.post(
            "/api/v1/admin/security/blocks",
            json={
                "subject_type": "user",
                "subject": str(target.id),
                "reason": "automated abuse confirmed",
                "ends_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
            },
        )
        assert blocked.status_code == 201, blocked.text
        assert blocked.json()["block"]["subject_hash"] != str(target.id)
        refused = await target_client.get("/api/v1/auth/me")
        assert refused.status_code == 403
        assert refused.json()["code"] == "ACCESS_BLOCKED"

        revoked = await admin_client.post(
            f"/api/v1/admin/security/blocks/{blocked.json()['block']['id']}/revoke",
            json={"reason": "temporary response completed"},
        )
        assert revoked.status_code == 200
        assert (await target_client.get("/api/v1/auth/me")).status_code == 200


@pytest.mark.asyncio
async def test_dynamic_login_rate_limit_emits_event_and_retry_after(
    security_context: SecurityContext,
) -> None:
    admin = await seed_user(security_context, "rate.admin@example.com", ("super_admin",))
    target = await seed_user(security_context, "rate.target@example.com", ("user",))
    async with client_for(security_context) as client:
        await login(client, admin.email)
        updated = await client.patch(
            "/api/v1/admin/security/rate-limits/login",
            json={
                "request_limit": 1,
                "window_seconds": 60,
                "enabled": True,
                "reason": "exercise rate-limit recovery",
            },
        )
        assert updated.status_code == 200, updated.text
        first = await client.post(
            "/api/v1/auth/login",
            json={"identifier": target.email, "password": "correct horse battery staple"},
        )
        assert first.status_code == 200
        limited = await client.post(
            "/api/v1/auth/login",
            json={"identifier": target.email, "password": "correct horse battery staple"},
        )
        assert limited.status_code == 429
        assert limited.headers["retry-after"]
        assert limited.json()["details"]["policy"] == "login"

    async with security_context.database.session_factory() as session:
        policy = await session.get(RateLimitPolicy, "login")
        assert policy is not None and policy.request_limit == 1
        event_types = set((await session.scalars(select(SecurityEvent.event_type))).all())
        assert "rate_limit_exceeded" in event_types
        results = set((await session.scalars(select(AuditLog.result))).all())
        assert "denied" in results
