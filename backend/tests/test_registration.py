import uuid

import pytest
from sqlalchemy import func, select
from test_auth import auth_context as auth_fixture
from test_auth import client_for
from test_configuration import client_for as config_client
from test_configuration import config_context as config_fixture
from test_configuration import login as config_login
from test_configuration import seed_user as config_user

from app.api import auth
from app.repositories.models import (
    PointAccount,
    PointTransaction,
    RegistrationChallenge,
    User,
    UserMembership,
)
from app.services.memberships import sync_builtin_membership_plans
from app.services.security import SecurityService

auth_context = auth_fixture
config_context = config_fixture
PAYLOAD = {
    "email": "new@example.test",
    "display_name": "设计师",
    "password": "new secure password",
    "verification_code": "000000",
}


class Inbox:
    def __init__(self):
        self.codes = {}

    async def send(self, runtime, *, email, code):
        self.codes[email] = code


async def verified_payload(context, client):
    inbox = Inbox()
    context.app.state.registration_mailer = inbox
    sent = await client.post("/api/v1/auth/register/email-code", json={"email": PAYLOAD["email"]})
    assert sent.status_code == 202, sent.text
    code = inbox.codes[PAYLOAD["email"]]
    assert code not in sent.text
    return {**PAYLOAD, "verification_code": code}


@pytest.mark.asyncio
async def test_registration_creates_only_a_normal_user_and_grants_once(auth_context):
    async with client_for(auth_context) as client:
        assert (await client.get("/api/v1/auth/options")).json() == {"registration_enabled": True}
        payload = await verified_payload(auth_context, client)
        created = await client.post(
            "/api/v1/auth/register", json={**payload, "email": " NEW@Example.Test "}
        )
        assert created.status_code == 201, created.text
        assert "HttpOnly" in created.headers["set-cookie"]
        assert "samesite=lax" in created.headers["set-cookie"].lower()
        me = (await client.get("/api/v1/auth/me")).json()
        assert me["user"]["roles"] == ["user"]
        assert me["user"]["email"] == PAYLOAD["email"]
        assert "studio.use" in me["permissions"]
        assert "config.manage" not in me["permissions"]
        assert me["user"]["email_verified_at"] is not None
        assert PAYLOAD["password"] not in created.text
        duplicate = await client.post("/api/v1/auth/register", json=PAYLOAD)
        assert duplicate.status_code == 409
    user_id = uuid.UUID(me["user"]["id"])
    async with auth_context.database.session_factory() as session:
        assert await session.scalar(select(func.count(User.id))) == 1
        assert (
            await session.scalar(
                select(PointAccount.balance).where(PointAccount.user_id == user_id)
            )
            == 20
        )
        assert await session.scalar(select(func.count(PointTransaction.id))) == 1
        assert await session.scalar(select(func.count(UserMembership.id))) == 1


@pytest.mark.asyncio
async def test_admin_registration_switch_and_defaults_are_enforced_at_submit(config_context):
    admin = await config_user(config_context, "admin@example.test", ("super_admin",))
    async with config_context.database.session_factory() as session:
        await sync_builtin_membership_plans(session)
        await session.commit()
    async with (
        config_client(config_context, "admin") as manager,
        config_client(config_context, "visitor") as visitor,
    ):
        await config_login(manager, admin.email)
        saved = await manager.put(
            "/api/v1/admin/config/general",
            json={
                "base_version": None,
                "values": {
                    "registration_enabled": False,
                    "default_points": 73,
                    "default_membership_code": "pro",
                },
            },
        )
        assert saved.status_code == 200, saved.text
        assert (await visitor.get("/api/v1/auth/options")).json() == {"registration_enabled": False}
        closed = await visitor.post("/api/v1/auth/register", json=PAYLOAD)
        assert closed.status_code == 403 and closed.json()["code"] == "REGISTRATION_CLOSED"
        opened = await manager.put(
            "/api/v1/admin/config/general",
            json={"base_version": 1, "values": {"registration_enabled": True}},
        )
        assert opened.status_code == 200, opened.text
        payload = await verified_payload(config_context, visitor)
        created = await visitor.post("/api/v1/auth/register", json=payload)
        assert created.status_code == 201, created.text
        user_id = uuid.UUID(created.json()["user"]["id"])
        closed_again = await manager.put(
            "/api/v1/admin/config/general",
            json={"base_version": 2, "values": {"registration_enabled": False}},
        )
        assert closed_again.status_code == 200
        closed_send = await visitor.post(
            "/api/v1/auth/register/email-code", json={"email": "another@example.test"}
        )
        assert closed_send.status_code == 403
        assert (
            await visitor.post(
                "/api/v1/auth/register", json={**PAYLOAD, "email": "another@example.test"}
            )
        ).status_code == 403
        # Closing signup never revokes an existing member's session.
        assert (await visitor.get("/api/v1/auth/me")).status_code == 200
    async with config_context.database.session_factory() as session:
        from app.repositories.models import MembershipPlan

        assert (
            await session.scalar(
                select(PointAccount.balance).where(PointAccount.user_id == user_id)
            )
            == 73
        )
        membership = await session.scalar(
            select(UserMembership).where(UserMembership.user_id == user_id)
        )
        plan = await session.get(MembershipPlan, membership.plan_id)
        assert plan.code == "pro"


@pytest.mark.asyncio
async def test_registration_rejects_privilege_injection_and_rolls_back_grant_failure(
    auth_context, monkeypatch
):
    async with client_for(auth_context) as client:
        invalid = await client.post(
            "/api/v1/auth/register", json={**PAYLOAD, "roles": ["super_admin"]}
        )
        assert invalid.status_code == 422
        assert (
            await client.post("/api/v1/auth/register", json={**PAYLOAD, "password": "short"})
        ).status_code == 422

        async def fail(*args, **kwargs):
            raise RuntimeError("simulated grant failure")

        monkeypatch.setattr(auth.point_service, "ensure_onboarding_grant", fail)
        payload = await verified_payload(auth_context, client)
        with pytest.raises(RuntimeError, match="simulated grant failure"):
            await client.post("/api/v1/auth/register", json=payload)
    async with auth_context.database.session_factory() as session:
        assert await session.scalar(select(func.count(User.id))) == 0
        assert await session.scalar(select(func.count(UserMembership.id))) == 0
        challenge = await session.get(RegistrationChallenge, PAYLOAD["email"])
        assert challenge.consumed_at is None


@pytest.mark.asyncio
async def test_registration_is_rate_limited_by_ip(auth_context):
    auth_context.app.state.security_service = SecurityService(auth_context.app.state.settings)
    async with client_for(auth_context) as client:
        payload = await verified_payload(auth_context, client)
        for _ in range(5):
            response = await client.post("/api/v1/auth/register", json=payload)
            assert response.status_code in {201, 409}
        blocked = await client.post(
            "/api/v1/auth/register", json={**PAYLOAD, "email": "blocked@example.test"}
        )
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) > 0
