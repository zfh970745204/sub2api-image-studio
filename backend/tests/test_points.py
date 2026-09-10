from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select, update

from app.api.auth import router as auth_router
from app.api.errors import ApiError, install_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.points import router as points_router
from app.config import Settings
from app.domain.ids import uuid7
from app.repositories.database import Database
from app.repositories.models import (
    Base,
    OutboxEvent,
    PointAccount,
    PointTransaction,
    Role,
    User,
    UserRole,
)
from app.services.auth import AuthService
from app.services.points import PointService
from app.services.rbac import sync_builtin_rbac


@dataclass
class PointContext:
    app: FastAPI
    database: Database
    auth_service: AuthService
    point_service: PointService


@pytest_asyncio.fixture
async def point_context(tmp_path) -> PointContext:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'points.db').as_posix()}"
    settings = Settings(
        _env_file=None,
        APP_ENV="test",
        DATABASE_URL=database_url,
        ONBOARDING_POINTS=20,
        POINT_ADJUSTMENT_APPROVAL_THRESHOLD=1000,
    )
    database = Database(database_url)

    @event.listens_for(database.engine.sync_engine, "connect")
    def enforce_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    auth_service = AuthService(settings)
    point_service = PointService()
    async with database.session_factory() as session:
        await sync_builtin_rbac(session)
        await session.commit()

    app = FastAPI()
    app.state.settings = settings
    app.state.auth_service = auth_service
    app.state.runtime_services = type("Runtime", (), {"database": database})()
    install_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(auth_router)
    app.include_router(points_router)
    yield PointContext(app, database, auth_service, point_service)
    await database.dispose()


async def seed_user(
    context: PointContext,
    *,
    email: str,
    role_codes: tuple[str, ...] = ("user",),
    onboarding: bool = True,
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
        session.add_all(
            UserRole(user_id=user.id, role_id=role.id, assigned_by=user.id) for role in roles
        )
        if onboarding:
            await context.point_service.ensure_onboarding_grant(
                session,
                user.id,
                points=20,
                request_id="test-seed",
            )
        else:
            await context.point_service.ensure_account(session, user.id)
        await session.commit()
        return user


def client_for(context: PointContext, *, user_agent: str) -> AsyncClient:
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
async def test_onboarding_balance_and_transaction_are_idempotent(
    point_context: PointContext,
) -> None:
    user = await seed_user(point_context, email="member@example.com")
    point_context.app.state.settings.onboarding_points = 99
    async with client_for(point_context, user_agent="member-device") as client:
        await login(client, user.email)
        first = await client.get("/api/v1/points/balance")
        second = await client.get("/api/v1/points/balance")
        transactions = await client.get("/api/v1/points/transactions")

    assert first.status_code == second.status_code == 200
    assert first.json()["account"]["balance"] == 20
    assert first.json()["account"]["lifetime_earned"] == 20
    assert transactions.status_code == 200
    assert len(transactions.json()["items"]) == 1
    assert transactions.json()["items"][0]["reference_type"] == "onboarding"
    async with point_context.database.session_factory() as session:
        count = await session.scalar(
            select(func.count(PointTransaction.id)).where(PointTransaction.user_id == user.id)
        )
        assert count == 1


@pytest.mark.asyncio
async def test_permissions_small_adjustment_and_cursor_pagination(
    point_context: PointContext,
) -> None:
    finance = await seed_user(
        point_context,
        email="finance@example.com",
        role_codes=("user", "finance"),
    )
    operator = await seed_user(
        point_context,
        email="operator@example.com",
        role_codes=("user", "operator"),
    )
    target = await seed_user(point_context, email="target@example.com")
    async with (
        client_for(point_context, user_agent="finance-device") as finance_client,
        client_for(point_context, user_agent="operator-device") as operator_client,
    ):
        await login(finance_client, finance.email)
        await login(operator_client, operator.email)
        assert (
            await operator_client.get(f"/api/v1/admin/users/{target.id}/points")
        ).status_code == 200
        denied = await operator_client.post(
            f"/api/v1/admin/users/{target.id}/points/adjustments",
            headers={"Idempotency-Key": "operator-adjustment"},
            json={"amount": 10, "reason": "不应允许"},
        )
        assert denied.status_code == 403

        responses = []
        for index in range(3):
            response = await finance_client.post(
                f"/api/v1/admin/users/{target.id}/points/adjustments",
                headers={"Idempotency-Key": f"small-adjustment-{index}"},
                json={"amount": 10, "reason": f"小额赠送 {index}"},
            )
            assert response.status_code == 201, response.text
            assert response.json()["adjustment"]["status"] == "applied"
            responses.append(response)

        replay = await finance_client.post(
            f"/api/v1/admin/users/{target.id}/points/adjustments",
            headers={"Idempotency-Key": "small-adjustment-0"},
            json={"amount": 10, "reason": "小额赠送 0"},
        )
        assert replay.status_code == 201
        assert replay.json()["adjustment"]["id"] == responses[0].json()["adjustment"]["id"]
        reused = await finance_client.post(
            f"/api/v1/admin/users/{target.id}/points/adjustments",
            headers={"Idempotency-Key": "small-adjustment-0"},
            json={"amount": 11, "reason": "不同请求"},
        )
        assert reused.status_code == 409
        assert reused.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

        insufficient = await finance_client.post(
            f"/api/v1/admin/users/{target.id}/points/adjustments",
            headers={"Idempotency-Key": "too-large-deduction"},
            json={"amount": -100, "reason": "不能形成负数"},
        )
        assert insufficient.status_code == 409
        assert insufficient.json()["code"] == "INSUFFICIENT_POINTS"
        detail = await finance_client.get(f"/api/v1/admin/users/{target.id}/points?limit=2")
        assert detail.json()["account"]["balance"] == 50
        assert len(detail.json()["transactions"]) == 2
        next_cursor = detail.json()["next_cursor"]
        assert next_cursor is not None
        next_page = await finance_client.get(
            f"/api/v1/admin/users/{target.id}/points?limit=2&cursor={next_cursor}"
        )
        assert len(next_page.json()["transactions"]) == 2


@pytest.mark.asyncio
async def test_large_adjustment_requires_distinct_reviewer_and_can_be_rejected(
    point_context: PointContext,
) -> None:
    requester = await seed_user(
        point_context,
        email="requester@example.com",
        role_codes=("user", "finance"),
    )
    reviewer = await seed_user(
        point_context,
        email="reviewer@example.com",
        role_codes=("user", "finance"),
    )
    target = await seed_user(point_context, email="target@example.com")
    async with (
        client_for(point_context, user_agent="requester-device") as requester_client,
        client_for(point_context, user_agent="reviewer-device") as reviewer_client,
    ):
        await login(requester_client, requester.email)
        await login(reviewer_client, reviewer.email)
        pending = await requester_client.post(
            f"/api/v1/admin/users/{target.id}/points/adjustments",
            headers={"Idempotency-Key": "large-adjustment-approve"},
            json={"amount": 1000, "reason": "大额活动赠送"},
        )
        assert pending.status_code == 201
        adjustment_id = pending.json()["adjustment"]["id"]
        assert pending.json()["adjustment"]["status"] == "pending"

        self_review = await requester_client.post(
            f"/api/v1/admin/point-adjustments/{adjustment_id}/approve",
            headers={"Idempotency-Key": "self-review-denied"},
            json={"reason": "自己审批"},
        )
        assert self_review.status_code == 409
        assert self_review.json()["code"] == "POINT_ADJUSTMENT_SELF_REVIEW"

        approved = await reviewer_client.post(
            f"/api/v1/admin/point-adjustments/{adjustment_id}/approve",
            headers={"Idempotency-Key": "large-adjustment-review"},
            json={"reason": "复核通过"},
        )
        replay = await reviewer_client.post(
            f"/api/v1/admin/point-adjustments/{adjustment_id}/approve",
            headers={"Idempotency-Key": "large-adjustment-review"},
            json={"reason": "复核通过"},
        )
        assert approved.status_code == replay.status_code == 200
        assert approved.json()["adjustment"]["status"] == "applied"
        assert approved.json()["adjustment"]["id"] == replay.json()["adjustment"]["id"]

        pending_rejection = await requester_client.post(
            f"/api/v1/admin/users/{target.id}/points/adjustments",
            headers={"Idempotency-Key": "large-adjustment-reject"},
            json={"amount": -1000, "reason": "大额扣减申请"},
        )
        rejection_id = pending_rejection.json()["adjustment"]["id"]
        rejected = await reviewer_client.post(
            f"/api/v1/admin/point-adjustments/{rejection_id}/reject",
            headers={"Idempotency-Key": "large-adjustment-rejection-review"},
            json={"reason": "证据不足"},
        )
        assert rejected.status_code == 200
        assert rejected.json()["adjustment"]["status"] == "rejected"
        balance = await reviewer_client.get(f"/api/v1/admin/users/{target.id}/points")
        assert balance.json()["account"]["balance"] == 1020

    async with point_context.database.session_factory() as session:
        topics = set((await session.scalars(select(OutboxEvent.topic))).all())
        assert {"points.transaction", "points.audit"} <= topics


@pytest.mark.asyncio
async def test_reversal_is_append_only_and_idempotent(point_context: PointContext) -> None:
    finance = await seed_user(
        point_context,
        email="finance@example.com",
        role_codes=("user", "finance"),
    )
    target = await seed_user(point_context, email="target@example.com")
    async with client_for(point_context, user_agent="finance-device") as client:
        await login(client, finance.email)
        adjusted = await client.post(
            f"/api/v1/admin/users/{target.id}/points/adjustments",
            headers={"Idempotency-Key": "grant-before-reversal"},
            json={"amount": 100, "reason": "临时赠送"},
        )
        transaction_id = adjusted.json()["adjustment"]["transaction_id"]
        reversed_response = await client.post(
            f"/api/v1/admin/point-transactions/{transaction_id}/reverse",
            headers={"Idempotency-Key": "reverse-grant"},
            json={"reason": "赠送错误，创建冲正"},
        )
        replay = await client.post(
            f"/api/v1/admin/point-transactions/{transaction_id}/reverse",
            headers={"Idempotency-Key": "reverse-grant"},
            json={"reason": "赠送错误，创建冲正"},
        )
        assert reversed_response.status_code == replay.status_code == 200
        reversal = reversed_response.json()["transaction"]
        assert reversal["entry_type"] == "reversal"
        assert reversal["delta"] == -100
        assert reversal["id"] == replay.json()["transaction"]["id"]
        reverse_reversal = await client.post(
            f"/api/v1/admin/point-transactions/{reversal['id']}/reverse",
            headers={"Idempotency-Key": "reverse-a-reversal"},
            json={"reason": "不允许"},
        )
        assert reverse_reversal.status_code == 409
        balance = await client.get(f"/api/v1/admin/users/{target.id}/points")
        assert balance.json()["account"]["balance"] == 20

    async with point_context.database.session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(PointTransaction).where(PointTransaction.user_id == target.id)
                )
            ).all()
        )
        assert len(rows) == 3


@pytest.mark.asyncio
async def test_one_hundred_atomic_consumes_never_make_balance_negative_and_refund_once(
    point_context: PointContext,
) -> None:
    target = await seed_user(point_context, email="target@example.com")
    job_ids = [uuid.uuid4() for _ in range(100)]

    async def consume(index: int) -> tuple[bool, uuid.UUID]:
        async with point_context.database.session_factory() as session:
            try:
                await point_context.point_service.consume_points(
                    session,
                    user_id=target.id,
                    amount=1,
                    job_id=job_ids[index],
                    idempotency_key=f"consume-{index}",
                    request_fingerprint=f"fingerprint-{index}",
                    description="并发消费",
                    metadata={},
                    request_id=f"consume-request-{index}",
                )
                await session.commit()
                return True, job_ids[index]
            except ApiError as exc:
                await session.rollback()
                assert exc.code == "INSUFFICIENT_POINTS"
                return False, job_ids[index]

    results = await asyncio.gather(*(consume(index) for index in range(100)))
    successful_jobs = [job_id for succeeded, job_id in results if succeeded]
    assert len(successful_jobs) == 20

    async with point_context.database.session_factory() as session:
        account = (
            await session.scalars(select(PointAccount).where(PointAccount.user_id == target.id))
        ).one()
        assert account.balance == 0
        assert account.lifetime_spent == 20
        consume_count = await session.scalar(
            select(func.count(PointTransaction.id)).where(
                PointTransaction.user_id == target.id,
                PointTransaction.entry_type == "consume",
            )
        )
        assert consume_count == 20
        await point_context.point_service.refund_points(
            session,
            user_id=target.id,
            amount=1,
            job_id=successful_jobs[0],
            idempotency_key="refund-first",
            description="任务失败退款",
            metadata={},
            request_id="refund-request",
        )
        await session.commit()

    async with point_context.database.session_factory() as session:
        await point_context.point_service.refund_points(
            session,
            user_id=target.id,
            amount=1,
            job_id=successful_jobs[0],
            idempotency_key="refund-retry-with-another-key",
            description="任务失败退款",
            metadata={},
            request_id="refund-retry",
        )
        await session.commit()
        account = (
            await session.scalars(select(PointAccount).where(PointAccount.user_id == target.id))
        ).one()
        refunds = await session.scalar(
            select(func.count(PointTransaction.id)).where(
                PointTransaction.user_id == target.id,
                PointTransaction.entry_type == "refund",
            )
        )
        assert account.balance == 1
        assert account.lifetime_spent == 19
        assert refunds == 1


@pytest.mark.asyncio
async def test_reconciliation_freezes_mismatched_account_without_rewriting_ledger(
    point_context: PointContext,
) -> None:
    finance = await seed_user(
        point_context,
        email="finance@example.com",
        role_codes=("user", "finance"),
    )
    target = await seed_user(point_context, email="target@example.com")
    async with point_context.database.session_factory() as session:
        await session.execute(
            update(PointAccount).where(PointAccount.user_id == target.id).values(balance=999)
        )
        await session.commit()
    async with point_context.database.session_factory() as session:
        result = await point_context.point_service.reconcile_accounts(session)
        await session.commit()
        assert result.checked == 2
        assert result.frozen == 1

    async with client_for(point_context, user_agent="finance-device") as client:
        await login(client, finance.email)
        blocked = await client.post(
            f"/api/v1/admin/users/{target.id}/points/adjustments",
            headers={"Idempotency-Key": "blocked-frozen-adjustment"},
            json={"amount": 1, "reason": "冻结后禁止变更"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "POINT_ACCOUNT_FROZEN"

    async with point_context.database.session_factory() as session:
        transactions = await session.scalar(
            select(func.count(PointTransaction.id)).where(PointTransaction.user_id == target.id)
        )
        account = (
            await session.scalars(select(PointAccount).where(PointAccount.user_id == target.id))
        ).one()
        assert transactions == 1
        assert account.balance == 999
        assert account.status == "frozen"
