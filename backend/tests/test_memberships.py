from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.auth import router as auth_router
from app.api.errors import install_exception_handlers
from app.api.memberships import router as memberships_router
from app.api.middleware import RequestContextMiddleware
from app.config import Settings
from app.domain.ids import uuid7
from app.repositories.database import Database
from app.repositories.models import (
    Base,
    MembershipEvent,
    MembershipPlan,
    OutboxEvent,
    Role,
    User,
    UserMembership,
    UserRole,
)
from app.services.auth import AuthService
from app.services.memberships import EntitlementService, sync_builtin_membership_plans
from app.services.rbac import sync_builtin_rbac
from app.workers.scheduler import reconcile_memberships


@dataclass
class MembershipContext:
    app: FastAPI
    database: Database
    auth_service: AuthService
    membership_service: EntitlementService


@pytest_asyncio.fixture
async def membership_context(tmp_path) -> MembershipContext:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'memberships.db').as_posix()}"
    settings = Settings(_env_file=None, APP_ENV="test", DATABASE_URL=database_url)
    database = Database(database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    auth_service = AuthService(settings)
    membership_service = EntitlementService()
    async with database.session_factory() as session:
        await sync_builtin_rbac(session)
        await sync_builtin_membership_plans(session)
        await session.commit()

    app = FastAPI()
    app.state.settings = settings
    app.state.auth_service = auth_service
    app.state.runtime_services = type("Runtime", (), {"database": database})()
    install_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(auth_router)
    app.include_router(memberships_router)
    yield MembershipContext(app, database, auth_service, membership_service)
    await database.dispose()


async def seed_user(
    context: MembershipContext,
    *,
    email: str,
    role_codes: tuple[str, ...] = ("user",),
) -> User:
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
        roles = list((await session.scalars(select(Role).where(Role.code.in_(role_codes)))).all())
        assert {role.code for role in roles} == set(role_codes)
        session.add_all(
            UserRole(user_id=user.id, role_id=role.id, assigned_by=user.id) for role in roles
        )
        await context.membership_service.ensure_default_membership(
            session,
            user.id,
            assigned_by=user.id,
            request_id="test-seed",
        )
        await session.commit()
        return user


def client_for(context: MembershipContext, *, user_agent: str) -> AsyncClient:
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


async def plans_by_code(context: MembershipContext) -> dict[str, MembershipPlan]:
    async with context.database.session_factory() as session:
        return {plan.code: plan for plan in (await session.scalars(select(MembershipPlan))).all()}


@pytest.mark.asyncio
async def test_seeded_plans_default_membership_and_single_active_constraint(
    membership_context: MembershipContext,
) -> None:
    member = await seed_user(membership_context, email="member@example.com")
    async with client_for(membership_context, user_agent="member-device") as client:
        await login(client, member.email)
        plans = await client.get("/api/v1/membership/plans")
        current = await client.get("/api/v1/membership/me")

    assert plans.status_code == 200
    assert [item["code"] for item in plans.json()["items"]] == ["free", "basic", "pro"]
    assert current.status_code == 200
    assert current.json()["membership"]["status"] == "active"
    assert current.json()["entitlements"] == {
        "plan_code": "free",
        "discount_bps": 10000,
        "max_concurrent_jobs": 1,
        "max_upload_mb": 20,
        "max_image_megapixels": 40,
        "retention_days": 30,
        "periodic_points": 0,
        "extra": {},
    }

    plan = (await plans_by_code(membership_context))["free"]
    async with membership_context.database.session_factory() as session:
        session.add(
            UserMembership(
                id=uuid7(),
                user_id=member.id,
                plan_id=plan.id,
                status="active",
                starts_at=datetime.now(UTC),
                ends_at=None,
                auto_renew=False,
                source="system",
                reason="invalid duplicate",
                entitlement_snapshot={"plan_code": "free"},
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_membership_permissions_plan_crud_and_snapshot_isolation(
    membership_context: MembershipContext,
) -> None:
    finance = await seed_user(
        membership_context,
        email="finance@example.com",
        role_codes=("user", "finance"),
    )
    operator = await seed_user(
        membership_context,
        email="operator@example.com",
        role_codes=("user", "operator"),
    )
    target = await seed_user(membership_context, email="target@example.com")
    plans = await plans_by_code(membership_context)
    pro = plans["pro"]
    ends_at = datetime.now(UTC) + timedelta(days=30)

    async with (
        client_for(membership_context, user_agent="finance-device") as finance_client,
        client_for(membership_context, user_agent="operator-device") as operator_client,
    ):
        await login(finance_client, finance.email)
        await login(operator_client, operator.email)
        assert (await operator_client.get("/api/v1/admin/membership-plans")).status_code == 200
        denied = await operator_client.post(
            f"/api/v1/admin/users/{target.id}/memberships",
            headers={"Idempotency-Key": "operator-denied"},
            json={"plan_id": str(pro.id), "ends_at": ends_at.isoformat(), "reason": "测试"},
        )
        assert denied.status_code == 403

        created = await finance_client.post(
            "/api/v1/admin/membership-plans",
            json={
                "code": "team",
                "name": "Team",
                "description": "未来套餐",
                "level": 30,
                "billing_period": "month",
                "periodic_points": 0,
                "operation_discount_bps": 8000,
                "max_concurrent_jobs": 4,
                "max_upload_mb": 50,
                "max_image_megapixels": 100,
                "asset_retention_days": 120,
                "display_order": 40,
                "entitlements": {"priority_queue": True},
                "reason": "新增套餐",
            },
        )
        assert created.status_code == 201, created.text
        team = created.json()["plan"]
        assert team["status"] == "draft"
        activated = await finance_client.post(
            f"/api/v1/admin/membership-plans/{team['id']}/activate",
            json={"reason": "准备开放"},
        )
        assert activated.status_code == 200
        deactivated = await finance_client.post(
            f"/api/v1/admin/membership-plans/{team['id']}/deactivate",
            json={"reason": "暂不展示"},
        )
        assert deactivated.status_code == 200
        assert deactivated.json()["plan"]["status"] == "inactive"
        protected_free = await finance_client.post(
            f"/api/v1/admin/membership-plans/{plans['free'].id}/deactivate",
            json={"reason": "不允许"},
        )
        assert protected_free.status_code == 409

        assigned = await finance_client.post(
            f"/api/v1/admin/users/{target.id}/memberships",
            headers={"Idempotency-Key": "assign-pro-for-snapshot"},
            json={"plan_id": str(pro.id), "ends_at": ends_at.isoformat(), "reason": "开通 Pro"},
        )
        assert assigned.status_code == 201, assigned.text
        assert assigned.json()["membership"]["entitlement_snapshot"]["discount_bps"] == 8500

        updated = await finance_client.patch(
            f"/api/v1/admin/membership-plans/{pro.id}",
            json={"operation_discount_bps": 7000, "reason": "调整后续周期折扣"},
        )
        assert updated.status_code == 200, updated.text
        history = await finance_client.get(f"/api/v1/admin/users/{target.id}/memberships")
        current = next(item for item in history.json()["items"] if item["status"] == "active")
        assert current["entitlement_snapshot"]["discount_bps"] == 8500

    async with client_for(membership_context, user_agent="target-device") as target_client:
        await login(target_client, target.email)
        assert (await target_client.get("/api/v1/admin/membership-plans")).status_code == 403


@pytest.mark.asyncio
async def test_assign_renew_period_end_change_and_idempotency(
    membership_context: MembershipContext,
) -> None:
    finance = await seed_user(
        membership_context,
        email="finance@example.com",
        role_codes=("user", "finance"),
    )
    target = await seed_user(membership_context, email="target@example.com")
    plans = await plans_by_code(membership_context)
    initial_end = datetime.now(UTC) + timedelta(days=30)
    renewed_end = initial_end + timedelta(days=30)
    payload = {
        "plan_id": str(plans["basic"].id),
        "ends_at": initial_end.isoformat(),
        "reason": "人工开通 Basic",
    }

    async with client_for(membership_context, user_agent="finance-device") as client:
        await login(client, finance.email)
        first = await client.post(
            f"/api/v1/admin/users/{target.id}/memberships",
            headers={"Idempotency-Key": "assign-basic-001"},
            json=payload,
        )
        replay = await client.post(
            f"/api/v1/admin/users/{target.id}/memberships",
            headers={"Idempotency-Key": "assign-basic-001"},
            json=payload,
        )
        assert first.status_code == replay.status_code == 201
        assert first.json()["membership"]["id"] == replay.json()["membership"]["id"]
        membership_id = first.json()["membership"]["id"]

        reused = await client.post(
            f"/api/v1/admin/users/{target.id}/memberships",
            headers={"Idempotency-Key": "assign-basic-001"},
            json={**payload, "reason": "不同请求"},
        )
        assert reused.status_code == 409
        assert reused.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

        renewed = await client.post(
            f"/api/v1/admin/memberships/{membership_id}/renew",
            headers={"Idempotency-Key": "renew-basic-001"},
            json={"ends_at": renewed_end.isoformat(), "reason": "续期 30 天"},
        )
        assert renewed.status_code == 200, renewed.text

        downgrade = await client.post(
            f"/api/v1/admin/memberships/{membership_id}/change-plan",
            headers={"Idempotency-Key": "downgrade-free-001"},
            json={
                "target_plan_id": str(plans["free"].id),
                "effective_mode": "period_end",
                "reason": "周期结束后降级",
            },
        )
        assert downgrade.status_code == 200, downgrade.text
        assert downgrade.json()["membership"]["status"] == "scheduled"
        assert (
            downgrade.json()["membership"]["starts_at"] == renewed.json()["membership"]["ends_at"]
        )

    async with membership_context.database.session_factory() as session:
        idempotent_events = await session.scalar(
            select(func.count(MembershipEvent.id)).where(
                MembershipEvent.idempotency_key == "assign-basic-001"
            )
        )
        assert idempotent_events == 1
        processed = await membership_context.membership_service.reconcile_due_memberships(
            session,
            now=renewed_end + timedelta(seconds=1),
        )
        await session.commit()
        assert processed == 1
        active = (
            await session.scalars(
                select(UserMembership).where(
                    UserMembership.user_id == target.id,
                    UserMembership.status == "active",
                )
            )
        ).one()
        assert active.plan_id == plans["free"].id
        event_types = set(
            (
                await session.scalars(
                    select(MembershipEvent.event_type)
                    .join(UserMembership)
                    .where(UserMembership.user_id == target.id)
                )
            ).all()
        )
        assert {"created", "activated", "renewed", "downgraded", "expired"} <= event_types


@pytest.mark.asyncio
async def test_immediate_change_cancel_and_expiry_fall_back_to_free(
    membership_context: MembershipContext,
) -> None:
    finance = await seed_user(
        membership_context,
        email="finance@example.com",
        role_codes=("user", "finance"),
    )
    target = await seed_user(membership_context, email="target@example.com")
    plans = await plans_by_code(membership_context)
    ends_at = datetime.now(UTC) + timedelta(days=2)

    async with client_for(membership_context, user_agent="finance-device") as client:
        await login(client, finance.email)
        assigned = await client.post(
            f"/api/v1/admin/users/{target.id}/memberships",
            headers={"Idempotency-Key": "assign-pro-001"},
            json={
                "plan_id": str(plans["pro"].id),
                "ends_at": ends_at.isoformat(),
                "reason": "开通",
            },
        )
        assert assigned.status_code == 201
        pro_id = assigned.json()["membership"]["id"]
        missing_reason = await client.post(
            f"/api/v1/admin/memberships/{pro_id}/change-plan",
            headers={"Idempotency-Key": "bad-downgrade"},
            json={"target_plan_id": str(plans["basic"].id), "effective_mode": "now", "reason": ""},
        )
        assert missing_reason.status_code == 422
        changed = await client.post(
            f"/api/v1/admin/memberships/{pro_id}/change-plan",
            headers={"Idempotency-Key": "now-downgrade"},
            json={
                "target_plan_id": str(plans["basic"].id),
                "effective_mode": "now",
                "ends_at": ends_at.isoformat(),
                "reason": "立即降级的明确原因",
            },
        )
        assert changed.status_code == 200, changed.text
        basic_id = changed.json()["membership"]["id"]
        cancelled = await client.post(
            f"/api/v1/admin/memberships/{basic_id}/cancel",
            headers={"Idempotency-Key": "cancel-now-001"},
            json={"effective_mode": "now", "reason": "立即取消"},
        )
        assert cancelled.status_code == 200, cancelled.text

    async with membership_context.database.session_factory() as session:
        active = list(
            (
                await session.scalars(
                    select(UserMembership).where(
                        UserMembership.user_id == target.id,
                        UserMembership.status == "active",
                    )
                )
            ).all()
        )
        assert len(active) == 1
        assert active[0].plan_id == plans["free"].id
        assert "membership.audit" in set((await session.scalars(select(OutboxEvent.topic))).all())


@pytest.mark.asyncio
async def test_scheduler_expires_paid_membership_without_renewal(
    membership_context: MembershipContext,
) -> None:
    finance = await seed_user(
        membership_context,
        email="finance@example.com",
        role_codes=("user", "finance"),
    )
    target = await seed_user(membership_context, email="target@example.com")
    plans = await plans_by_code(membership_context)
    ends_at = datetime.now(UTC) + timedelta(days=1)
    async with client_for(membership_context, user_agent="finance-device") as client:
        await login(client, finance.email)
        assigned = await client.post(
            f"/api/v1/admin/users/{target.id}/memberships",
            headers={"Idempotency-Key": "assign-expiring-basic"},
            json={
                "plan_id": str(plans["basic"].id),
                "ends_at": ends_at.isoformat(),
                "reason": "短期会员",
            },
        )
        assert assigned.status_code == 201

    result = await reconcile_memberships(
        {"runtime": type("Runtime", (), {"database": membership_context.database})()}
    )
    assert result == {"memberships_reconciled": 0}
    async with membership_context.database.session_factory() as session:
        processed = await membership_context.membership_service.reconcile_due_memberships(
            session, now=ends_at + timedelta(seconds=1)
        )
        await session.commit()
        assert processed == 1
        current = await membership_context.membership_service.current_snapshot(
            session, target.id, now=ends_at + timedelta(seconds=1)
        )
        assert current.plan_code == "free"
