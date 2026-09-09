from __future__ import annotations

import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.admin_users import router as admin_users_router
from app.api.auth import router as auth_router
from app.api.dependencies import require_permission
from app.api.errors import install_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.rbac import router as rbac_router
from app.api.system import admin_router as admin_system_router
from app.config import Settings
from app.domain.ids import uuid7
from app.domain.rbac import (
    AUDITOR_PERMISSIONS,
    FINANCE_PERMISSIONS,
    OPERATOR_PERMISSIONS,
    PERMISSION_CODES,
    USER_PERMISSIONS,
)
from app.repositories.database import Database
from app.repositories.models import AuthSession, Base, OutboxEvent, Role, User, UserRole
from app.services.auth import AuthService
from app.services.rbac import sync_builtin_rbac


@dataclass
class RbacContext:
    app: FastAPI
    database: Database
    service: AuthService


@pytest_asyncio.fixture
async def rbac_context(tmp_path) -> RbacContext:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'rbac.db').as_posix()}"
    settings = Settings(
        _env_file=None,
        APP_ENV="test",
        DATABASE_URL=database_url,
        SUB2API_API_KEY="rbac-secret-never-return",
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
    app.include_router(admin_system_router)
    for permission in PERMISSION_CODES:
        app.add_api_route(
            f"/test/permissions/{permission}",
            make_permission_probe(permission),
            methods=["GET"],
            name=f"probe_{permission}",
            dependencies=[Depends(require_permission(permission))],
        )
    yield RbacContext(app=app, database=database, service=service)
    await database.dispose()


def make_permission_probe(permission: str):
    async def probe() -> dict[str, str]:
        return {"permission": permission}

    return probe


async def seed_user(
    context: RbacContext,
    *,
    email: str,
    role_codes: tuple[str, ...],
    password: str = "correct horse battery staple",
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
        roles = list((await session.scalars(select(Role).where(Role.code.in_(role_codes)))).all())
        assert {role.code for role in roles} == set(role_codes)
        session.add_all(
            UserRole(user_id=user.id, role_id=role.id, assigned_by=user.id) for role in roles
        )
        await session.commit()
        return user


def client_for(context: RbacContext, *, user_agent: str) -> AsyncClient:
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
async def test_builtin_role_matrix_and_every_permission_allow_deny(
    rbac_context: RbacContext,
) -> None:
    expected = {
        "user": USER_PERMISSIONS,
        "operator": USER_PERMISSIONS | OPERATOR_PERMISSIONS,
        "finance": USER_PERMISSIONS | FINANCE_PERMISSIONS,
        "auditor": USER_PERMISSIONS | AUDITOR_PERMISSIONS,
        "super_admin": PERMISSION_CODES,
    }
    users: dict[str, User] = {}
    for role_code in expected:
        roles = ("user",) if role_code == "user" else ("user", role_code)
        users[role_code] = await seed_user(
            rbac_context,
            email=f"{role_code}@example.com",
            role_codes=roles,
        )
    permissionless = await seed_user(
        rbac_context,
        email="permissionless@example.com",
        role_codes=(),
    )

    async with AsyncExitStack() as stack:
        clients: dict[str, AsyncClient] = {}
        for role_code, user in users.items():
            client = await stack.enter_async_context(
                client_for(rbac_context, user_agent=f"{role_code}-device")
            )
            await login(client, user.email)
            clients[role_code] = client
            me = await client.get("/api/v1/auth/me")
            assert set(me.json()["permissions"]) == expected[role_code]

        empty_client = await stack.enter_async_context(
            client_for(rbac_context, user_agent="permissionless-device")
        )
        await login(empty_client, permissionless.email)
        assert (await empty_client.get("/api/v1/auth/me")).json()["permissions"] == []

        for permission in sorted(PERMISSION_CODES):
            allowed = await clients["super_admin"].get(f"/test/permissions/{permission}")
            denied = await empty_client.get(f"/test/permissions/{permission}")
            assert allowed.status_code == 200, permission
            assert denied.status_code == 403, permission
            assert denied.json()["code"] == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_role_management_is_audited_and_updates_active_sessions(
    rbac_context: RbacContext,
) -> None:
    admin_user = await seed_user(
        rbac_context,
        email="admin@example.com",
        role_codes=("user", "super_admin"),
    )
    auditor_user = await seed_user(
        rbac_context,
        email="auditor@example.com",
        role_codes=("user", "auditor"),
    )
    member_user = await seed_user(
        rbac_context,
        email="member@example.com",
        role_codes=("user",),
    )
    async with (
        client_for(rbac_context, user_agent="admin-device") as admin,
        client_for(rbac_context, user_agent="auditor-device") as auditor,
        client_for(rbac_context, user_agent="member-device") as member,
    ):
        await login(admin, admin_user.email)
        await login(auditor, auditor_user.email)
        await login(member, member_user.email)

        permission_list = await auditor.get("/api/v1/admin/permissions")
        assert permission_list.status_code == 200
        assert len(permission_list.json()["items"]) == len(PERMISSION_CODES) == 34
        assert "rbac-secret-never-return" not in permission_list.text
        denied_create = await auditor.post(
            "/api/v1/admin/roles",
            json={"code": "support", "name": "Support"},
        )
        assert denied_create.status_code == 403

        roles_response = await admin.get("/api/v1/admin/roles")
        assert roles_response.status_code == 200
        role_by_code = {item["code"]: item for item in roles_response.json()["items"]}
        assert set(role_by_code) == {"user", "operator", "finance", "auditor", "super_admin"}

        created = await admin.post(
            "/api/v1/admin/roles",
            json={
                "code": "support",
                "name": "Support",
                "description": "Support read access",
                "permission_codes": ["users.read"],
            },
        )
        assert created.status_code == 201
        support_role = created.json()["role"]
        assert support_role["is_system"] is False
        assert (await member.get("/test/permissions/users.read")).status_code == 403

        assigned = await admin.put(
            f"/api/v1/admin/users/{member_user.id}/roles",
            json={
                "roles": [
                    {"role_id": role_by_code["user"]["id"]},
                    {"role_id": support_role["id"]},
                ]
            },
        )
        assert assigned.status_code == 200, assigned.text
        assert assigned.json()["permission_version"] == 2
        assert (await member.get("/test/permissions/users.read")).status_code == 200
        assert (await member.get("/test/permissions/users.manage")).status_code == 403

        remove_base_role = await admin.put(
            f"/api/v1/admin/users/{member_user.id}/roles",
            json={"roles": [{"role_id": support_role["id"]}]},
        )
        assert remove_base_role.status_code == 422
        assert remove_base_role.json()["code"] == "BASE_ROLE_REQUIRED"

        permissions_replaced = await admin.put(
            f"/api/v1/admin/roles/{support_role['id']}/permissions",
            json={"permission_codes": ["users.read", "users.manage"]},
        )
        assert permissions_replaced.status_code == 200
        assert (await member.get("/test/permissions/users.manage")).status_code == 200

        updated = await admin.patch(
            f"/api/v1/admin/roles/{support_role['id']}",
            json={"name": "Customer Support"},
        )
        assert updated.status_code == 200
        assert updated.json()["role"]["name"] == "Customer Support"

        system_change = await admin.put(
            f"/api/v1/admin/roles/{role_by_code['user']['id']}/permissions",
            json={"permission_codes": []},
        )
        assert system_change.status_code == 409
        assert system_change.json()["code"] == "SYSTEM_ROLE_IMMUTABLE"

        unknown_permission = await admin.put(
            f"/api/v1/admin/roles/{support_role['id']}/permissions",
            json={"permission_codes": ["secrets.read"]},
        )
        assert unknown_permission.status_code == 422
        assert unknown_permission.json()["code"] == "UNKNOWN_PERMISSION"

        disable_last_admin = await member.post(f"/api/v1/admin/users/{admin_user.id}/disable")
        assert disable_last_admin.status_code == 409
        assert disable_last_admin.json()["code"] == "LAST_SUPER_ADMIN"

        last_admin = await admin.put(
            f"/api/v1/admin/users/{admin_user.id}/roles",
            json={"roles": [{"role_id": role_by_code["user"]["id"]}]},
        )
        assert last_admin.status_code == 409
        assert last_admin.json()["code"] == "LAST_SUPER_ADMIN"

    async with rbac_context.database.session_factory() as session:
        member = await session.get(User, member_user.id)
        assert member is not None
        assert member.permission_version == 3
        topics = set((await session.scalars(select(OutboxEvent.topic))).all())
    assert {
        "identity.role.created",
        "identity.role.updated",
        "identity.role.permissions_replaced",
        "identity.user.roles_replaced",
    } <= topics


@pytest.mark.asyncio
async def test_read_and_manage_permissions_are_separate(rbac_context: RbacContext) -> None:
    finance_user = await seed_user(
        rbac_context,
        email="finance@example.com",
        role_codes=("user", "finance"),
    )
    operator_user = await seed_user(
        rbac_context,
        email="operator@example.com",
        role_codes=("user", "operator"),
    )
    target = await seed_user(
        rbac_context,
        email="target@example.com",
        role_codes=("user",),
    )
    async with (
        client_for(rbac_context, user_agent="finance-device") as finance,
        client_for(rbac_context, user_agent="operator-device") as operator,
    ):
        await login(finance, finance_user.email)
        await login(operator, operator_user.email)
        assert (await finance.get("/api/v1/admin/users")).status_code == 200
        assert (
            await finance.patch(f"/api/v1/admin/users/{target.id}", json={"display_name": "Denied"})
        ).status_code == 403
        assert (
            await operator.patch(
                f"/api/v1/admin/users/{target.id}", json={"display_name": "Managed"}
            )
        ).status_code == 200
        assert (await operator.get("/api/v1/admin/system/workers")).status_code == 200
        assert (await finance.get("/api/v1/admin/system/workers")).status_code == 403


@pytest.mark.asyncio
async def test_expired_roles_and_cross_owner_session_ids_do_not_authorize(
    rbac_context: RbacContext,
) -> None:
    first_user = await seed_user(
        rbac_context,
        email="first@example.com",
        role_codes=("user", "operator"),
    )
    second_user = await seed_user(
        rbac_context,
        email="second@example.com",
        role_codes=("user",),
    )
    async with rbac_context.database.session_factory() as session:
        operator = (await session.scalars(select(Role).where(Role.code == "operator"))).one()
        assignment = await session.get(UserRole, (first_user.id, operator.id))
        assert assignment is not None
        assignment.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    async with (
        client_for(rbac_context, user_agent="first-device") as first,
        client_for(rbac_context, user_agent="second-device") as second,
    ):
        await login(first, first_user.email)
        await login(second, second_user.email)
        assert (await first.get("/test/permissions/users.manage")).status_code == 403

        second_sessions = await second.get("/api/v1/auth/sessions")
        second_session_id = second_sessions.json()["items"][0]["id"]
        cross_owner = await first.delete(f"/api/v1/auth/sessions/{second_session_id}")
        assert cross_owner.status_code == 404
        assert (await second.get("/api/v1/auth/me")).status_code == 200

    async with rbac_context.database.session_factory() as session:
        second_session = await session.get(AuthSession, uuid.UUID(second_session_id))
        assert second_session is not None
        assert second_session.revoked_at is None
