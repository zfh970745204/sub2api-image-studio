from __future__ import annotations

from dataclasses import dataclass

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.app import router as app_router
from app.api.auth import router as auth_router
from app.api.errors import install_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.config import Settings
from app.domain.ids import uuid7
from app.repositories.database import Database
from app.repositories.models import Base, Role, User, UserNotification, UserPreference, UserRole
from app.services.auth import AuthService
from app.services.memberships import sync_builtin_membership_plans
from app.services.rbac import sync_builtin_rbac


@dataclass
class AppContext:
    app: FastAPI
    database: Database
    auth_service: AuthService


@pytest_asyncio.fixture
async def app_context(tmp_path) -> AppContext:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'user-app.db').as_posix()}"
    settings = Settings(
        _env_file=None,
        APP_ENV="test",
        DATABASE_URL=database_url,
        ONBOARDING_POINTS=80,
    )
    database = Database(database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with database.session_factory() as session:
        await sync_builtin_rbac(session)
        await sync_builtin_membership_plans(session)
        await session.commit()
    auth_service = AuthService(settings)
    app = FastAPI()
    app.state.settings = settings
    app.state.auth_service = auth_service
    app.state.runtime_services = type("Runtime", (), {"database": database})()
    install_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(auth_router)
    app.include_router(app_router)
    yield AppContext(app, database, auth_service)
    await database.dispose()


async def seed_user(context: AppContext, email: str, roles: tuple[str, ...] = ("user",)) -> User:
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


def client_for(context: AppContext, user_agent: str) -> AsyncClient:
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
async def test_bootstrap_is_authenticated_minimal_and_initializes_defaults(
    app_context: AppContext,
) -> None:
    user = await seed_user(app_context, "member@example.com")
    admin = await seed_user(app_context, "operator@example.com", ("user", "operator"))
    async with (
        client_for(app_context, "anonymous") as anonymous,
        client_for(app_context, "member-device") as member_client,
        client_for(app_context, "operator-device") as admin_client,
    ):
        assert (await anonymous.get("/api/v1/app/bootstrap")).status_code == 401
        await login(member_client, user.email)
        await login(admin_client, admin.email)

        response = await member_client.get("/api/v1/app/bootstrap")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["user"]["email"] == user.email
        assert payload["points"]["balance"] == 80
        assert payload["membership"]["plan"]["code"] == "free"
        assert payload["preferences"]["studio_layout"] == {}
        assert "password_hash" not in response.text
        assert not any(item.startswith("admin.") for item in payload["permissions"])

        admin_payload = (await admin_client.get("/api/v1/app/bootstrap")).json()
        assert "admin.dashboard.read" in admin_payload["permissions"]


@pytest.mark.asyncio
async def test_preferences_are_merged_and_reject_untrusted_layout_fields(
    app_context: AppContext,
) -> None:
    user = await seed_user(app_context, "preferences@example.com")
    async with client_for(app_context, "preferences-device") as client:
        await login(client, user.email)
        updated = await client.patch(
            "/api/v1/me/preferences",
            json={
                "theme": "dark",
                "studio_layout": {"asset_view": "list", "panel_width": 360},
                "notification_preferences": {"task_completed": True},
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["preferences"]["studio_layout"] == {
            "asset_view": "list",
            "panel_width": 360,
        }

        merged = await client.patch(
            "/api/v1/me/preferences",
            json={"studio_layout": {"nav_collapsed": True, "print_output_mode": "opaque"}},
        )
        assert merged.status_code == 200
        assert merged.json()["preferences"]["studio_layout"]["asset_view"] == "list"
        assert merged.json()["preferences"]["studio_layout"]["nav_collapsed"] is True
        assert merged.json()["preferences"]["studio_layout"]["print_output_mode"] == "opaque"
        assert (
            await client.patch(
                "/api/v1/me/preferences", json={"studio_layout": {"print_output_mode": "fake"}}
            )
        ).status_code == 422

        unsafe = await client.patch(
            "/api/v1/me/preferences",
            json={"studio_layout": {"custom_css": "body { display: none }"}},
        )
        assert unsafe.status_code == 422
        assert "custom_css" in unsafe.text
        non_boolean = await client.patch(
            "/api/v1/me/preferences",
            json={"notification_preferences": {"task_completed": 1}},
        )
        assert non_boolean.status_code == 422


@pytest.mark.asyncio
async def test_notifications_are_owner_scoped_and_support_read_all(
    app_context: AppContext,
) -> None:
    owner = await seed_user(app_context, "notification-owner@example.com")
    stranger = await seed_user(app_context, "notification-stranger@example.com")
    async with app_context.database.session_factory() as session:
        owner_notification = UserNotification(
            id=uuid7(),
            user_id=owner.id,
            type="job.succeeded",
            title="图片处理完成",
            body="高清重绘任务已完成",
            target_url="/app/jobs",
        )
        stranger_notification = UserNotification(
            id=uuid7(),
            user_id=stranger.id,
            type="points.adjusted",
            title="积分已调整",
            body="账户积分发生变化",
            target_url="/app/points",
        )
        session.add_all((owner_notification, stranger_notification))
        await session.commit()

    async with client_for(app_context, "notification-device") as client:
        await login(client, owner.email)
        listed = await client.get("/api/v1/me/notifications")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == [str(owner_notification.id)]
        assert (
            await client.post(f"/api/v1/me/notifications/{stranger_notification.id}/read")
        ).status_code == 404
        read = await client.post(f"/api/v1/me/notifications/{owner_notification.id}/read")
        assert read.status_code == 200
        assert read.json()["notification"]["read_at"] is not None

    async with app_context.database.session_factory() as session:
        session.add(
            UserNotification(
                id=uuid7(),
                user_id=owner.id,
                type="job.failed",
                title="图片处理失败",
                body="任务积分已退款",
                target_url="/app/jobs",
            )
        )
        await session.commit()
    async with client_for(app_context, "notification-device-2") as client:
        await login(client, owner.email)
        response = await client.post("/api/v1/me/notifications/read-all")
        assert response.status_code == 200
        assert response.json()["updated"] == 1
        unread = await client.get("/api/v1/me/notifications?unread_only=true")
        assert unread.json()["items"] == []

    async with app_context.database.session_factory() as session:
        preference = await session.get(UserPreference, owner.id)
        assert preference is None
