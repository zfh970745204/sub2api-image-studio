from __future__ import annotations

from dataclasses import dataclass

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.errors import install_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.config import Settings
from app.domain.ids import uuid7
from app.repositories.database import Database
from app.repositories.models import Base, OutboxEvent, Role, User, UserRole
from app.services.auth import AuthService
from app.services.rbac import sync_builtin_rbac


@dataclass
class AdminRuntime:
    database: Database


@dataclass
class AdminContext:
    app: FastAPI
    database: Database
    auth_service: AuthService


@pytest_asyncio.fixture
async def admin_context(tmp_path) -> AdminContext:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'admin.db').as_posix()}"
    settings = Settings(_env_file=None, APP_ENV="test", DATABASE_URL=database_url)
    database = Database(database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    auth_service = AuthService(settings)
    async with database.session_factory() as session:
        await sync_builtin_rbac(session)
        await session.commit()
    app = FastAPI()
    app.state.settings = settings
    app.state.auth_service = auth_service
    app.state.runtime_services = AdminRuntime(database)
    install_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(auth_router)
    app.include_router(admin_router)
    yield AdminContext(app, database, auth_service)
    await database.dispose()


async def seed_user(context: AdminContext, email: str, roles: tuple[str, ...]) -> User:
    async with context.database.session_factory() as session:
        user = User(
            id=uuid7(),
            email=email,
            username=email.split("@", 1)[0],
            password_hash=context.auth_service.hash_password("correct horse battery staple"),
            display_name=email.split("@", 1)[0].replace(".", " ").title(),
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


def client_for(context: AdminContext, user_agent: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=context.app),
        base_url="http://testserver",
        headers={"user-agent": user_agent},
    )


async def login(client: AsyncClient, email: str) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"identifier": email, "password": "correct horse battery staple"},
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_dashboard_search_and_saved_views_are_permission_scoped(
    admin_context: AdminContext,
) -> None:
    ordinary = await seed_user(admin_context, "ordinary@example.com", ("user",))
    operator = await seed_user(admin_context, "operator@example.com", ("operator",))
    target = await seed_user(admin_context, "find.me@example.com", ("user",))
    async with (
        client_for(admin_context, "ordinary-device") as ordinary_client,
        client_for(admin_context, "operator-device") as operator_client,
    ):
        await login(ordinary_client, ordinary.email)
        await login(operator_client, operator.email)
        assert (await ordinary_client.get("/api/v1/admin/dashboard/summary")).status_code == 403

        summary = await operator_client.get("/api/v1/admin/dashboard/summary?range=7d")
        assert summary.status_code == 200, summary.text
        assert summary.json()["users"]["total"] == 3
        series = await operator_client.get(
            "/api/v1/admin/dashboard/timeseries?metric=users&range=7d"
        )
        assert series.status_code == 200
        assert len(series.json()["points"]) == 7

        found = await operator_client.get("/api/v1/admin/search?q=find.me")
        assert found.status_code == 200
        assert any(item["id"] == str(target.id) for item in found.json()["items"])
        assert all(item["type"] != "role" for item in found.json()["items"])

        saved = await operator_client.post(
            "/api/v1/admin/saved-views",
            json={
                "module": "users",
                "name": "待处理账号",
                "filters": {"status": "pending"},
                "columns": ["email", "status", "created_at"],
            },
        )
        assert saved.status_code == 201, saved.text
        listed = await operator_client.get("/api/v1/admin/saved-views?module=users")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["name"] == "待处理账号"
        denied = await operator_client.post(
            "/api/v1/admin/saved-views",
            json={"module": "roles", "name": "角色", "filters": {}, "columns": []},
        )
        assert denied.status_code == 403


@pytest.mark.asyncio
async def test_high_risk_action_requires_confirmation_and_distinct_reviewer(
    admin_context: AdminContext,
) -> None:
    requester = await seed_user(admin_context, "requester@example.com", ("operator",))
    reviewer = await seed_user(admin_context, "reviewer@example.com", ("operator",))
    auditor = await seed_user(admin_context, "auditor@example.com", ("auditor",))
    target = await seed_user(admin_context, "target@example.com", ("user",))
    payload = {
        "action_type": "user.disable",
        "target_type": "user",
        "target_id": str(target.id),
        "payload": {"notify_user": True},
        "reason": "账号存在异常自动化行为",
        "risk_level": "normal",
    }
    async with (
        client_for(admin_context, "requester-device") as requester_client,
        client_for(admin_context, "reviewer-device") as reviewer_client,
        client_for(admin_context, "auditor-device") as auditor_client,
    ):
        await login(requester_client, requester.email)
        await login(reviewer_client, reviewer.email)
        await login(auditor_client, auditor.email)
        unconfirmed = await requester_client.post("/api/v1/admin/action-requests", json=payload)
        assert unconfirmed.status_code == 409
        with_secret = await requester_client.post(
            "/api/v1/admin/action-requests",
            json={**payload, "confirmed": True, "payload": {"api_key": "must-not-store"}},
        )
        assert with_secret.status_code == 422
        created = await requester_client.post(
            "/api/v1/admin/action-requests", json={**payload, "confirmed": True}
        )
        assert created.status_code == 201, created.text
        item = created.json()["action_request"]
        assert item["risk_level"] == "high"
        assert item["status"] == "pending"

        self_review = await requester_client.post(
            f"/api/v1/admin/action-requests/{item['id']}/approve",
            json={"reason": "确认执行"},
        )
        assert self_review.status_code == 409
        forbidden = await auditor_client.post(
            f"/api/v1/admin/action-requests/{item['id']}/approve",
            json={"reason": "越权尝试"},
        )
        assert forbidden.status_code == 403
        approved = await reviewer_client.post(
            f"/api/v1/admin/action-requests/{item['id']}/approve",
            json={"reason": "已复核证据"},
        )
        assert approved.status_code == 200
        assert approved.json()["action_request"]["status"] == "approved"
        listed = await auditor_client.get("/api/v1/admin/action-requests?status=approved")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["approved_by"] == str(reviewer.id)


@pytest.mark.asyncio
async def test_audit_redaction_and_export_token_binding(
    admin_context: AdminContext,
) -> None:
    first_admin = await seed_user(admin_context, "first.admin@example.com", ("super_admin",))
    second_admin = await seed_user(admin_context, "second.admin@example.com", ("super_admin",))
    async with admin_context.database.session_factory() as session:
        session.add(
            OutboxEvent(
                id=uuid7(),
                topic="config.audit",
                aggregate_type="config",
                aggregate_id=uuid7(),
                payload={
                    "action": "config.tested",
                    "actor_user_id": str(first_admin.id),
                    "request_id": "redaction-test",
                    "details": {"api_key": "plaintext-secret", "result": "ok"},
                },
                status="pending",
                attempts=0,
                available_at=first_admin.created_at,
                version=1,
            )
        )
        await session.commit()
    async with (
        client_for(admin_context, "first-admin-device") as first_client,
        client_for(admin_context, "second-admin-device") as second_client,
    ):
        await login(first_client, first_admin.email)
        await login(second_client, second_admin.email)
        audit = await first_client.get("/api/v1/admin/audit")
        assert audit.status_code == 200
        assert "plaintext-secret" not in audit.text
        assert "[REDACTED]" in audit.text

        requested = await first_client.get("/api/v1/admin/export/users")
        assert requested.status_code == 202, requested.text
        download_url = requested.json()["download_url"]
        cross_user = await second_client.get(download_url)
        assert cross_user.status_code == 404
        downloaded = await first_client.get(download_url)
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"].startswith("text/csv")
        assert "first.admin@example.com" not in downloaded.text
        assert "f***@example.com" in downloaded.text

    async with admin_context.database.session_factory() as session:
        actions = [
            event.payload.get("action")
            for event in (await session.scalars(select(OutboxEvent))).all()
        ]
        assert "admin_export.requested" in actions
        assert "admin_export.downloaded" in actions
