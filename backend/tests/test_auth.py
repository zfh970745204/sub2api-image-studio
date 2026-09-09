from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.responses import Response

from app import cli
from app.api.admin_users import router as admin_users_router
from app.api.auth import router as auth_router
from app.api.auth import set_session_cookie
from app.api.errors import install_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.rbac import router as rbac_router
from app.config import Settings
from app.domain.ids import uuid7
from app.repositories.database import Database
from app.repositories.models import (
    AuthSession,
    Base,
    LoginAttempt,
    MembershipPlan,
    OutboxEvent,
    PointAccount,
    PointTransaction,
    Role,
    User,
    UserMembership,
    UserRole,
)
from app.services.auth import AuthService
from app.services.rbac import sync_builtin_rbac


@dataclass
class AuthContext:
    app: FastAPI
    database: Database
    service: AuthService


@pytest_asyncio.fixture
async def auth_context(tmp_path) -> AuthContext:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'auth.db').as_posix()}"
    settings = Settings(
        _env_file=None,
        APP_ENV="test",
        DATABASE_URL=database_url,
        LOGIN_ACCOUNT_FAILURE_LIMIT=2,
        LOGIN_IP_FAILURE_LIMIT=10,
    )
    database = Database(database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    service = AuthService(settings)
    async with database.session_factory() as session:
        await sync_builtin_rbac(session)
        await session.commit()
    app = FastAPI()
    app.state.settings = settings
    app.state.auth_service = service
    app.state.runtime_services = type("Runtime", (), {"database": database})()
    install_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(auth_router)
    app.include_router(admin_users_router)
    app.include_router(rbac_router)
    yield AuthContext(app=app, database=database, service=service)
    await database.dispose()


async def seed_user(
    context: AuthContext,
    *,
    email: str,
    password: str = "correct horse battery staple",
    super_admin: bool = False,
) -> User:
    async with context.database.session_factory() as session:
        user = User(
            id=uuid7(),
            email=email,
            username=email.split("@", 1)[0],
            password_hash=context.service.hash_password(password),
            display_name=email.split("@", 1)[0],
            status="active",
            session_version=1,
            permission_version=1,
        )
        session.add(user)
        await session.flush()
        role = await context.service.ensure_role(
            session,
            code="super_admin" if super_admin else "user",
            name="超级管理员" if super_admin else "普通用户",
            description="test role",
        )
        await context.service.assign_role(session, user_id=user.id, role=role, assigned_by=user.id)
        await session.commit()
        return user


def client_for(context: AuthContext, *, user_agent: str = "test-device") -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=context.app),
        base_url="http://testserver",
        headers={"user-agent": user_agent},
    )


async def login(client: AsyncClient, identifier: str, password: str) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"identifier": identifier, "password": password},
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_login_is_enumeration_safe_and_temporarily_locks_account(
    auth_context: AuthContext,
) -> None:
    user = await seed_user(auth_context, email="member@example.com")
    async with client_for(auth_context) as client:
        wrong = await client.post(
            "/api/v1/auth/login",
            json={"identifier": user.email, "password": "wrong-password"},
        )
        missing = await client.post(
            "/api/v1/auth/login",
            json={"identifier": "missing@example.com", "password": "wrong-password"},
        )
        locked = await client.post(
            "/api/v1/auth/login",
            json={"identifier": user.email, "password": "wrong-password"},
        )
        rate_limited = await client.post(
            "/api/v1/auth/login",
            json={"identifier": user.email, "password": "wrong-password"},
        )

    assert wrong.status_code == missing.status_code == locked.status_code == 401
    assert wrong.json()["code"] == missing.json()["code"] == "INVALID_CREDENTIALS"
    assert wrong.json()["message"] == missing.json()["message"]
    assert rate_limited.status_code == 429
    assert rate_limited.json()["message"] == wrong.json()["message"]
    async with auth_context.database.session_factory() as session:
        stored_user = await session.get(User, user.id)
        assert stored_user is not None
        assert stored_user.status == "locked"
        assert stored_user.locked_until is not None


@pytest.mark.asyncio
async def test_login_cookie_and_server_session_do_not_store_raw_token(
    auth_context: AuthContext,
) -> None:
    await seed_user(auth_context, email="member@example.com")
    async with client_for(auth_context) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "MEMBER@example.com",
                "password": "correct horse battery staple",
            },
        )
        assert response.status_code == 200
        cookie_header = response.headers["set-cookie"].lower()
        assert "httponly" in cookie_header
        assert "samesite=lax" in cookie_header
        assert response.headers["cache-control"] == "no-store"
        assert "sub2image_session" not in response.text
        raw_token = response.cookies["sub2image_session"]
        me = await client.get("/api/v1/auth/me")
        assert me.status_code == 200
        sessions = await client.get("/api/v1/auth/sessions")
        assert sessions.status_code == 200
        assert sessions.json()["recent_failed_logins"] == []
        logout_response = await client.post("/api/v1/auth/logout")
        assert logout_response.status_code == 204
        assert (await client.get("/api/v1/auth/me")).status_code == 401

    async with auth_context.database.session_factory() as session:
        stored_session = (await session.scalars(select(AuthSession))).one()
        assert stored_session.token_hash != raw_token
        assert stored_session.token_hash == auth_context.service.token_hash(raw_token)
        topics = set((await session.scalars(select(OutboxEvent.topic))).all())
        assert "identity.login.succeeded" in topics
        assert "identity.logout" in topics


@pytest.mark.asyncio
async def test_password_change_keeps_current_session_and_revokes_other_devices(
    auth_context: AuthContext,
) -> None:
    await seed_user(auth_context, email="member@example.com")
    async with (
        client_for(auth_context, user_agent="device-one") as first,
        client_for(auth_context, user_agent="device-two") as second,
    ):
        await login(first, "member@example.com", "correct horse battery staple")
        await login(second, "member@example.com", "correct horse battery staple")
        sessions = await first.get("/api/v1/auth/sessions")
        assert sessions.status_code == 200
        assert len(sessions.json()["items"]) == 2

        changed = await first.post(
            "/api/v1/auth/password/change",
            json={
                "current_password": "correct horse battery staple",
                "new_password": "a newer correct horse battery staple",
            },
        )
        assert changed.status_code == 204
        assert (await first.get("/api/v1/auth/me")).status_code == 200
        assert (await second.get("/api/v1/auth/me")).status_code == 401

        logout_all = await first.post("/api/v1/auth/logout-all")
        assert logout_all.status_code == 204
        assert (await first.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_user_can_revoke_an_individual_device(auth_context: AuthContext) -> None:
    await seed_user(auth_context, email="member@example.com")
    async with (
        client_for(auth_context, user_agent="device-one") as first,
        client_for(auth_context, user_agent="device-two") as second,
    ):
        await login(first, "member@example.com", "correct horse battery staple")
        await login(second, "member@example.com", "correct horse battery staple")
        items = (await first.get("/api/v1/auth/sessions")).json()["items"]
        second_id = next(item["id"] for item in items if item["user_agent"] == "device-two")
        revoked = await first.delete(f"/api/v1/auth/sessions/{second_id}")
        assert revoked.status_code == 204
        assert (await second.get("/api/v1/auth/me")).status_code == 401
        assert (await first.get("/api/v1/auth/me")).status_code == 200


@pytest.mark.asyncio
async def test_admin_invite_reset_disable_enable_and_audit(auth_context: AuthContext) -> None:
    await seed_user(auth_context, email="admin@example.com", super_admin=True)
    async with client_for(auth_context, user_agent="admin-device") as admin:
        await login(admin, "admin@example.com", "correct horse battery staple")
        created = await admin.post(
            "/api/v1/admin/users",
            json={
                "email": "invited@example.com",
                "username": "invited",
                "display_name": "Invited User",
            },
        )
        assert created.status_code == 201
        invited = created.json()["user"]
        assert invited["status"] == "pending"
        setup_token = created.json()["setup_token"]

        async with client_for(auth_context, user_agent="invited-device") as invited_client:
            reset = await invited_client.post(
                "/api/v1/auth/password/reset",
                json={"token": setup_token, "new_password": "invited user password"},
            )
            assert reset.status_code == 204
            await login(invited_client, "invited", "invited user password")

            revoked = await admin.post(f"/api/v1/admin/users/{invited['id']}/revoke-sessions")
            assert revoked.status_code == 204
            assert (await invited_client.get("/api/v1/auth/me")).status_code == 401

            await login(invited_client, "invited", "invited user password")
            disabled = await admin.post(f"/api/v1/admin/users/{invited['id']}/disable")
            assert disabled.status_code == 204
            assert (await invited_client.get("/api/v1/auth/me")).status_code == 401

            enabled = await admin.post(f"/api/v1/admin/users/{invited['id']}/enable")
            assert enabled.status_code == 200
            assert enabled.json()["user"]["status"] == "active"

    async with auth_context.database.session_factory() as session:
        topics = set((await session.scalars(select(OutboxEvent.topic))).all())
        invited_membership = (
            await session.scalars(
                select(UserMembership)
                .where(UserMembership.user_id == uuid.UUID(invited["id"]))
                .join(MembershipPlan)
            )
        ).one()
        invited_plan = await session.get(MembershipPlan, invited_membership.plan_id)
        invited_account = (
            await session.scalars(
                select(PointAccount).where(PointAccount.user_id == uuid.UUID(invited["id"]))
            )
        ).one()
        invited_grant = (
            await session.scalars(
                select(PointTransaction).where(
                    PointTransaction.user_id == uuid.UUID(invited["id"]),
                    PointTransaction.reference_type == "onboarding",
                )
            )
        ).one()
    assert {
        "identity.user.invited",
        "identity.password.reset",
        "identity.sessions.admin_revoked",
        "identity.user.disabled",
        "identity.user.enabled",
    } <= topics
    assert invited_membership.status == "active"
    assert invited_plan is not None and invited_plan.code == "free"
    assert invited_account.balance == invited_grant.delta == 20


def test_production_requires_secure_cookie_and_unique_auth_secrets() -> None:
    with pytest.raises(ValueError, match="SESSION_COOKIE_SECURE"):
        Settings(
            _env_file=None,
            APP_ENV="production",
            LEGACY_SYNC_API_ENABLED=False,
            DEPENDENCY_CHECKS_ENABLED=True,
        )

    settings = Settings(
        _env_file=None,
        APP_ENV="production",
        LEGACY_SYNC_API_ENABLED=False,
        DEPENDENCY_CHECKS_ENABLED=True,
        SESSION_COOKIE_SECURE=True,
        AUTH_TOKEN_PEPPER="token-pepper-0123456789-abcdefghijklmnop",
        AUTH_HASH_SALT="hash-salt-0123456789-abcdefghijklmnopq",
        APP_CONFIG_MASTER_KEY="6b" * 32,
        PUBLIC_APP_URL="https://studio.example.com",
    )
    assert settings.session_cookie_secure is True
    response = Response()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))
    set_session_cookie(response, request, "raw-session-token")
    cookie_header = response.headers["set-cookie"].lower()
    assert "secure" in cookie_header
    assert "httponly" in cookie_header


@pytest.mark.asyncio
async def test_create_admin_cli_is_one_time_and_assigns_system_roles(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'cli.db').as_posix()}"
    settings = Settings(_env_file=None, APP_ENV="test", DATABASE_URL=database_url)
    database = Database(database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await database.dispose()

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr("builtins.input", lambda _: "owner@example.com")
    passwords = iter(["owner secure password", "owner secure password"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: next(passwords))
    assert await cli.create_admin() == 0

    check_database = Database(database_url)
    async with check_database.session_factory() as session:
        user = (await session.scalars(select(User))).one()
        roles = set(
            (
                await session.scalars(
                    select(Role.code)
                    .join(UserRole, UserRole.role_id == Role.id)
                    .where(UserRole.user_id == user.id)
                )
            ).all()
        )
        assert user.status == "active"
        assert user.password_hash != "owner secure password"
        assert roles == {"user", "super_admin"}
        membership = (await session.scalars(select(UserMembership))).one()
        plan = await session.get(MembershipPlan, membership.plan_id)
        assert membership.user_id == user.id
        assert membership.status == "active"
        assert plan is not None and plan.code == "free"
        account = (await session.scalars(select(PointAccount))).one()
        grant = (await session.scalars(select(PointTransaction))).one()
        assert account.user_id == user.id
        assert account.balance == grant.delta == 20
        assert "identity.super_admin.created" in set(
            (await session.scalars(select(OutboxEvent.topic))).all()
        )
    await check_database.dispose()

    second_passwords = iter(["another secure password", "another secure password"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: next(second_passwords))
    assert await cli.create_admin() == 3


@pytest.mark.asyncio
async def test_reset_admin_password_cli_unlocks_account_and_revokes_sessions(
    auth_context: AuthContext, monkeypatch
) -> None:
    user = await seed_user(
        auth_context,
        email="locked-owner@example.com",
        password="original secure password",
        super_admin=True,
    )
    async with client_for(auth_context) as client:
        successful = await client.post(
            "/api/v1/auth/login",
            json={"identifier": user.email, "password": "original secure password"},
        )
        assert successful.status_code == 200
        for _ in range(2):
            failed = await client.post(
                "/api/v1/auth/login",
                json={"identifier": user.email, "password": "wrong password"},
            )
            assert failed.status_code == 401

        monkeypatch.setattr(cli, "get_settings", lambda: auth_context.service.settings)
        monkeypatch.setattr("builtins.input", lambda _: user.email)
        passwords = iter(["replacement secure password", "replacement secure password"])
        monkeypatch.setattr(cli.getpass, "getpass", lambda _: next(passwords))
        assert await cli.reset_admin_password() == 0

        recovered = await client.post(
            "/api/v1/auth/login",
            json={"identifier": user.email, "password": "replacement secure password"},
        )
        assert recovered.status_code == 200

    async with auth_context.database.session_factory() as session:
        stored = await session.get(User, user.id)
        attempts = list(
            (
                await session.scalars(
                    select(LoginAttempt).where(
                        LoginAttempt.account_fingerprint
                        == auth_context.service.fingerprint(user.email)
                    )
                )
            ).all()
        )
        revoked_sessions = list(
            (
                await session.scalars(
                    select(AuthSession).where(
                        AuthSession.user_id == user.id,
                        AuthSession.revoke_reason == "password_reset_by_cli",
                    )
                )
            ).all()
        )
        assert stored is not None
        assert stored.status == "active"
        assert stored.locked_until is None
        assert stored.session_version == 2
        assert all(attempt.success for attempt in attempts)
        assert len(revoked_sessions) == 1
