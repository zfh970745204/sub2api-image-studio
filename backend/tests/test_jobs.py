from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateSchema, DropSchema
from starlette.requests import Request

from app.api.auth import router as auth_router
from app.api.errors import install_exception_handlers
from app.api.jobs import enqueue_job
from app.api.jobs import router as jobs_router
from app.api.middleware import RequestContextMiddleware
from app.config import Settings
from app.domain.ids import uuid7
from app.repositories.database import Database
from app.repositories.models import (
    Asset,
    Base,
    ConfigGroup,
    ConfigVersion,
    ImageJob,
    JobAttempt,
    JobQuote,
    MembershipPlan,
    OperationCatalog,
    OperationPrice,
    OutboxEvent,
    PointAccount,
    PointTransaction,
    Role,
    User,
    UserNotification,
    UserRole,
)
from app.services.auth import AuthService
from app.services.jobs import JobService, RetryableJobError, sync_builtin_operations
from app.services.memberships import EntitlementService, sync_builtin_membership_plans
from app.services.points import PointService
from app.services.rbac import sync_builtin_rbac
from app.workers.scheduler import dispatch_image_jobs
from app.workers.worker import execute_image_job


@dataclass
class JobContext:
    app: FastAPI
    database: Database
    auth_service: AuthService
    job_service: JobService
    point_service: PointService
    dispatched: list[tuple[uuid.UUID, str]] = field(default_factory=list)


@pytest_asyncio.fixture
async def job_context(tmp_path, request) -> JobContext:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'jobs.db').as_posix()}"
    schema = None
    if getattr(request, "param", "sqlite") == "postgresql":
        database_url = os.getenv("TEST_DATABASE_URL", "")
        if os.getenv("APP_ENV") != "test" or not database_url:
            pytest.skip("PostgreSQL job races require APP_ENV=test and TEST_DATABASE_URL")
        assert (make_url(database_url).database or "").endswith("_test")
        schema = f"job_queue_test_{uuid.uuid4().hex}"
    settings = Settings(
        _env_file=None,
        APP_ENV="test",
        DATABASE_URL=database_url,
        ONBOARDING_POINTS=200,
    )
    database = Database(database_url)
    if schema is None:

        @event.listens_for(database.engine.sync_engine, "connect")
        def enforce_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
    else:
        async with database.engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
        database.engine.update_execution_options(schema_translate_map={None: schema})

    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    auth_service = AuthService(settings)
    job_service = JobService()
    point_service = PointService()
    async with database.session_factory() as session:
        await sync_builtin_rbac(session)
        await sync_builtin_membership_plans(session)
        await sync_builtin_operations(session)
        await session.commit()

    app = FastAPI()
    app.state.settings = settings
    app.state.auth_service = auth_service
    app.state.runtime_services = type("Runtime", (), {"database": database})()
    dispatched: list[tuple[uuid.UUID, str]] = []

    async def enqueue(job_id: uuid.UUID, queue_name: str) -> bool:
        dispatched.append((job_id, queue_name))
        return True

    app.state.job_enqueuer = enqueue
    install_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(auth_router)
    app.include_router(jobs_router)
    try:
        yield JobContext(app, database, auth_service, job_service, point_service, dispatched)
    finally:
        if schema is not None:
            async with database.engine.begin() as connection:
                await connection.execute(DropSchema(schema, cascade=True))
        await database.dispose()


async def seed_user(
    context: JobContext,
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
        await EntitlementService().ensure_default_membership(
            session, user.id, assigned_by=user.id, request_id="test-seed"
        )
        await context.point_service.ensure_onboarding_grant(
            session,
            user.id,
            points=200,
            request_id="test-seed",
        )
        await session.commit()
        return user


def client_for(context: JobContext, *, user_agent: str) -> AsyncClient:
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


async def quote(
    client: AsyncClient,
    operation_code: str,
    parameters: dict | None = None,
) -> dict:
    response = await client.post(
        "/api/v1/jobs/quote",
        json={
            "operation_code": operation_code,
            "parameters": parameters
            if parameters is not None
            else ({"prompt": "test image"} if operation_code == "ai.generate" else {}),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["quote"]


async def create_job(
    client: AsyncClient,
    job_quote: dict,
    *,
    key: str,
    parameters: dict | None = None,
):
    return await client.post(
        "/api/v1/jobs",
        headers={"Idempotency-Key": key},
        json={
            "quote_id": job_quote["id"],
            "parameters": parameters
            if parameters is not None
            else ({"prompt": "test image"} if job_quote["operation_code"] == "ai.generate" else {}),
        },
    )


@pytest.mark.asyncio
async def test_ecommerce_quote_multiplies_per_image_price_and_rejects_foreign_reference(
    job_context: JobContext,
) -> None:
    owner = await seed_user(job_context, email="ecommerce-owner@example.com")
    stranger = await seed_user(job_context, email="ecommerce-stranger@example.com")
    foreign_asset_id = uuid7()
    async with job_context.database.session_factory() as session:
        session.add(
            Asset(
                id=foreign_asset_id,
                owner_id=stranger.id,
                root_asset_id=foreign_asset_id,
                kind="original",
                operation_code="upload",
                bucket="test",
                object_key=f"test/{foreign_asset_id}.png",
                mime_type="image/png",
                extension="png",
                size_bytes=1,
                sha256="0" * 64,
                status="ready",
            )
        )
        await session.commit()

    async with client_for(job_context, user_agent="ecommerce-owner-device") as client:
        await login(client, owner.email)
        priced = await client.post(
            "/api/v1/jobs/quote",
            json={
                "operation_code": "ai.ecommerce",
                "parameters": {
                    "prompt": "white studio product listing",
                    "platform": "amazon",
                    "image_count": 3,
                    "quality": "high",
                },
            },
        )
        assert priced.status_code == 201, priced.text
        quote_payload = priced.json()["quote"]
        assert quote_payload["base_points"] == 60
        assert quote_payload["surcharge_points"] == 30
        assert quote_payload["final_points"] == 90

        rejected = await client.post(
            "/api/v1/jobs/quote",
            json={
                "operation_code": "ai.ecommerce",
                "parameters": {
                    "prompt": "use this product reference",
                    "reference_asset_ids": [str(foreign_asset_id)],
                    "image_count": 2,
                },
            },
        )
        assert rejected.status_code == 404
        assert rejected.json()["code"] == "REFERENCE_ASSET_NOT_FOUND"


@pytest.mark.asyncio
async def test_persistence_failure_rolls_back_charge_and_keeps_quote_retryable(
    job_context: JobContext, monkeypatch, caplog
) -> None:
    user = await seed_user(job_context, email="rollback@example.test")
    async with client_for(job_context, user_agent="rollback-device") as client:
        await login(client, user.email)
        job_quote = await quote(client, "ai.generate")

        def fail_status(*args, **kwargs):
            raise IntegrityError("test statement", {}, Exception("test constraint failure"))

        with monkeypatch.context() as patch:
            patch.setattr(JobService, "record_job_status", fail_status)
            response = await create_job(client, job_quote, key="safe-retry")
        assert response.status_code == 500
        assert response.json()["code"] == "IMAGE_JOB_SAVE_FAILED"
        assert response.json()["request_id"]
        assert "image job persistence failed" in caplog.text
        async with job_context.database.session_factory() as session:
            assert await session.scalar(select(func.count(ImageJob.id))) == 0
            assert (
                await session.scalar(
                    select(PointAccount.balance).where(PointAccount.user_id == user.id)
                )
                == 200
            )
            assert (
                await session.scalar(
                    select(func.count(PointTransaction.id)).where(
                        PointTransaction.entry_type == "consume"
                    )
                )
                == 0
            )
        retried = await create_job(client, job_quote, key="safe-retry")
        assert retried.status_code == 201, retried.text
        replay = await create_job(client, job_quote, key="safe-retry")
        assert replay.json()["job"]["id"] == retried.json()["job"]["id"]
        assert replay.json()["created"] is False


@pytest.mark.asyncio
async def test_free_job_charge_and_refund_keep_valid_ledger_links(job_context: JobContext) -> None:
    user = await seed_user(job_context, email="free-job@example.test")
    async with job_context.database.session_factory() as session:
        await session.execute(update(OperationPrice).values(base_points=0))
        await session.commit()
    async with client_for(job_context, user_agent="free-job-device") as client:
        await login(client, user.email)
        job_quote = await quote(client, "ai.generate")
        created = await create_job(client, job_quote, key="free-job")
        assert created.status_code == 201, created.text
        job_id = created.json()["job"]["id"]
        for _ in range(2):
            cancelled = await client.post(f"/api/v1/jobs/{job_id}/cancel")
            assert cancelled.status_code == 200, cancelled.text
    async with job_context.database.session_factory() as session:
        job = await session.get(ImageJob, uuid.UUID(job_id))
        charge = await session.get(PointTransaction, job.charge_transaction_id)
        refund = await session.get(PointTransaction, job.refund_transaction_id)
        assert charge.delta == refund.delta == 0
        assert job.refund_status == "refunded"
        assert (
            await session.scalar(
                select(PointAccount.balance).where(PointAccount.user_id == user.id)
            )
            == 200
        )
        assert (
            await session.scalar(
                select(func.count(PointTransaction.id)).where(
                    PointTransaction.entry_type == "refund"
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_reconcile_missing_charge_reports_mismatch_without_fake_refund(
    job_context: JobContext,
) -> None:
    user = await seed_user(job_context, email="missing-charge@example.test")
    async with client_for(job_context, user_agent="missing-charge-device") as client:
        await login(client, user.email)
        created = await create_job(client, await quote(client, "ai.generate"), key="missing-charge")
        job_id = uuid.UUID(created.json()["job"]["id"])
    async with job_context.database.session_factory() as session:
        job = await session.get(ImageJob, job_id)
        charge = await session.get(PointTransaction, job.charge_transaction_id)
        job.charge_transaction_id = None
        job.status = "failed"
        await session.flush()
        await session.delete(charge)
        await session.flush()
        result = await job_context.job_service.reconcile_job(
            session, job_id=job_id, request_id="reconcile-test"
        )
        assert result.mismatches == 1
        assert result.refunds_created == 0
        assert job.refund_status == "none"
        await session.rollback()


@pytest.mark.asyncio
async def test_versioned_price_quote_snapshot_and_idempotent_charge(
    job_context: JobContext,
) -> None:
    user = await seed_user(job_context, email="member@example.com")
    finance = await seed_user(
        job_context, email="finance@example.com", role_codes=("user", "finance")
    )
    async with job_context.database.session_factory() as session:
        group = ConfigGroup(id=uuid7(), code="sub2api", name="Sub2API", active_version=7)
        session.add(group)
        await session.flush()
        session.add(
            ConfigVersion(
                id=uuid7(),
                group_id=group.id,
                version=7,
                status="active",
                values={"enabled": True},
                created_by=finance.id,
                published_by=finance.id,
                change_reason="任务配置版本测试",
                published_at=datetime.now(UTC),
            )
        )
        await session.commit()
    async with (
        client_for(job_context, user_agent="member-device") as member_client,
        client_for(job_context, user_agent="finance-device") as finance_client,
    ):
        await login(member_client, user.email)
        await login(finance_client, finance.email)
        operations = await member_client.get("/api/v1/operations")
        assert operations.status_code == 200
        by_code = {item["code"]: item for item in operations.json()["items"]}
        assert len(by_code) == 13
        assert by_code["image.toolbox"]["current_price"]["base_points"] == 0
        assert by_code["ai.generate"]["member_base_points"] == 20

        old_quote = await quote(member_client, "ai.generate")
        changed = await finance_client.post(
            "/api/v1/admin/operation-prices",
            json={
                "operation_code": "ai.generate",
                "base_points": 25,
                "parameter_rules": {
                    "rules": [
                        {
                            "parameter": "variants",
                            "type": "per_unit",
                            "included": 1,
                            "unit": 1,
                            "points": 2,
                            "maximum": 4,
                        }
                    ]
                },
                "reason": "调整生成成本",
            },
        )
        assert changed.status_code == 201, changed.text
        async with job_context.database.session_factory() as session:
            pro = (
                await session.scalars(select(MembershipPlan).where(MembershipPlan.code == "pro"))
            ).one()
            membership_service = EntitlementService()
            await membership_service.assign_membership(
                session,
                user_id=user.id,
                plan_id=pro.id,
                starts_at=None,
                ends_at=datetime.now(UTC) + timedelta(days=30),
                actor_user_id=finance.id,
                reason="测试 Pro 折扣",
                idempotency_key="test-pro-discount",
                request_fingerprint=membership_service.request_fingerprint(
                    "assign", user.id, {"plan_id": str(pro.id)}
                ),
                request_id="test-pro-discount",
            )
            await session.commit()
        new_quote = await quote(member_client, "ai.generate", {"variants": 3})
        assert old_quote["final_points"] == 20
        assert new_quote["base_points"] == 25
        assert new_quote["discount_points"] == 3
        assert new_quote["surcharge_points"] == 4
        assert new_quote["final_points"] == 26

        first = await create_job(member_client, old_quote, key="generate-once")
        replay = await create_job(member_client, old_quote, key="generate-once")
        assert first.status_code == replay.status_code == 201
        assert first.json()["created"] is True
        assert replay.json()["created"] is False
        assert first.json()["job"]["id"] == replay.json()["job"]["id"]
        assert first.json()["job"]["pricing_snapshot"]["config_versions"] == {"sub2api": 7}
        reused = await create_job(
            member_client,
            new_quote,
            key="generate-once",
            parameters={"variants": 3},
        )
        assert reused.status_code == 409
        assert reused.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

    async with job_context.database.session_factory() as session:
        account = (
            await session.scalars(select(PointAccount).where(PointAccount.user_id == user.id))
        ).one()
        assert account.balance == 180
        assert (
            await session.scalar(
                select(func.count(PointTransaction.id)).where(
                    PointTransaction.user_id == user.id,
                    PointTransaction.entry_type == "consume",
                )
            )
            == 1
        )
        versions = list(
            (
                await session.scalars(
                    select(OperationPrice)
                    .join(OperationCatalog)
                    .where(OperationCatalog.code == "ai.generate")
                    .order_by(OperationPrice.version)
                )
            ).all()
        )
        assert [item.base_points for item in versions] == [20, 25]
        assert versions[0].effective_to == versions[1].effective_from


@pytest.mark.asyncio
async def test_extra_jobs_queue_owner_isolation_cancel_and_expired_quote(
    job_context: JobContext,
) -> None:
    owner = await seed_user(job_context, email="owner@example.com")
    stranger = await seed_user(job_context, email="stranger@example.com")
    async with (
        client_for(job_context, user_agent="owner-device") as owner_client,
        client_for(job_context, user_agent="stranger-device") as stranger_client,
    ):
        await login(owner_client, owner.email)
        await login(stranger_client, stranger.email)
        first_quote = await quote(owner_client, "cutout.smart")
        created = await create_job(owner_client, first_quote, key="cutout-queued")
        assert created.status_code == 201
        job_id = created.json()["job"]["id"]

        second_quote = await quote(owner_client, "color.effect")
        additional = await create_job(owner_client, second_quote, key="over-free-limit")
        assert additional.status_code == 201
        assert additional.json()["job"]["status"] == "queued"
        assert (await stranger_client.get(f"/api/v1/jobs/{job_id}")).status_code == 404
        assert (await stranger_client.post(f"/api/v1/jobs/{job_id}/cancel")).status_code == 404

        cancelled = await owner_client.post(f"/api/v1/jobs/{job_id}/cancel")
        replay = await owner_client.post(f"/api/v1/jobs/{job_id}/cancel")
        assert cancelled.status_code == replay.status_code == 200
        assert cancelled.json()["job"]["status"] == "cancelled"
        assert cancelled.json()["job"]["refund_status"] == "refunded"

        expired_quote = await quote(owner_client, "color.effect")
        async with job_context.database.session_factory() as session:
            await session.execute(
                update(JobQuote)
                .where(JobQuote.id == uuid.UUID(expired_quote["id"]))
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        expired = await create_job(owner_client, expired_quote, key="expired-quote")
        assert expired.status_code == 409
        assert expired.json()["code"] == "JOB_QUOTE_EXPIRED"

    async with job_context.database.session_factory() as session:
        account = (
            await session.scalars(select(PointAccount).where(PointAccount.user_id == owner.id))
        ).one()
        assert account.balance == 199
        refunds = await session.scalar(
            select(func.count(PointTransaction.id)).where(
                PointTransaction.user_id == owner.id,
                PointTransaction.entry_type == "refund",
            )
        )
        assert refunds == 1


@pytest.mark.asyncio
async def test_different_users_execute_together_without_duplicate_charges_or_results(
    job_context: JobContext,
) -> None:
    users = [await seed_user(job_context, email=f"parallel-{i}@example.com") for i in range(2)]
    job_ids = []
    for user in users:
        async with client_for(job_context, user_agent=str(user.id)) as client:
            await login(client, user.email)
            created = await create_job(client, await quote(client, "ai.generate"), key=str(user.id))
            assert created.status_code == 201, created.text
            job_ids.append(created.json()["job"]["id"])

    entered = set()
    both_running = asyncio.Event()
    release = asyncio.Event()

    async def executor(claim):
        entered.add(claim.user_id)
        if len(entered) == 2:
            both_running.set()
        await release.wait()
        asset_id = uuid7()
        async with job_context.database.session_factory() as session:
            session.add(
                Asset(
                    id=asset_id,
                    owner_id=claim.user_id,
                    root_asset_id=asset_id,
                    source_job_id=claim.job_id,
                    kind="result",
                    operation_code="ai.generate",
                    bucket="test",
                    object_key=f"test/{asset_id}.png",
                    mime_type="image/png",
                    extension="png",
                    size_bytes=1,
                    sha256="0" * 64,
                    status="ready",
                )
            )
            await session.commit()
        return {"output_asset_id": str(asset_id)}

    ctx = {
        "runtime": type(
            "Runtime",
            (),
            {
                "database": job_context.database,
                "instance_name": "parallel-worker",
            },
        )(),
        "image_job_executor": executor,
    }
    tasks = [asyncio.create_task(execute_image_job(ctx, job_id)) for job_id in job_ids]
    try:
        # Neither user's upstream call has finished when both are executing.
        await asyncio.wait_for(both_running.wait(), timeout=5)
        duplicate = await execute_image_job(ctx, job_ids[0])
        assert duplicate["status"] == "ignored"
    finally:
        release.set()
        results = await asyncio.gather(*tasks)
    assert [result["status"] for result in results] == ["succeeded", "succeeded"]
    async with job_context.database.session_factory() as session:
        for job_id, user in zip(job_ids, users, strict=True):
            job = await session.get(ImageJob, uuid.UUID(job_id))
            asset = await session.get(Asset, job.output_asset_id)
            assert asset.owner_id == job.user_id == user.id
            assert asset.source_job_id == job.id
            assert job.attempt_count == 1
            assert (
                await session.scalar(
                    select(func.count(PointTransaction.id)).where(
                        PointTransaction.user_id == user.id,
                        PointTransaction.entry_type == "consume",
                    )
                )
                == 1
            )
        assert list(await session.scalars(select(PointAccount.balance))) == [180, 180]


@pytest.mark.asyncio
async def test_worker_retry_success_timeout_refund_and_late_result_rejection(
    job_context: JobContext,
) -> None:
    user = await seed_user(job_context, email="worker-user@example.com")
    runtime = type(
        "WorkerRuntime",
        (),
        {"database": job_context.database, "instance_name": "worker-test"},
    )()
    async with client_for(job_context, user_agent="worker-user-device") as client:
        await login(client, user.email)
        retry_quote = await quote(client, "ai.generate")
        retry_job = await create_job(client, retry_quote, key="retry-job")
        retry_job_id = retry_job.json()["job"]["id"]

        calls = 0

        async def flaky_executor(_claim):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RetryableJobError("UPSTREAM_BUSY", "上游暂时繁忙")
            asset_id = uuid7()
            async with job_context.database.session_factory() as session:
                session.add(
                    Asset(
                        id=asset_id,
                        owner_id=user.id,
                        root_asset_id=asset_id,
                        source_job_id=_claim.job_id,
                        kind="result",
                        operation_code="ai.generate",
                        bucket="test",
                        object_key=f"test/{asset_id}.png",
                        mime_type="image/png",
                        extension="png",
                        size_bytes=1,
                        sha256="0" * 64,
                        status="ready",
                    )
                )
                await session.commit()
            return {
                "output_asset_id": str(asset_id),
                "provider_request_id": "provider-2",
                "metrics": {"duration_ms": 25},
            }

        first = await execute_image_job(
            {"runtime": runtime, "image_job_executor": flaky_executor}, retry_job_id
        )
        assert first["status"] == "retry_wait"
        async with job_context.database.session_factory() as session:
            released = await job_context.job_service.release_due_retries(
                session,
                now=datetime.now(UTC) + timedelta(minutes=1),
                request_id="test-release",
            )
            await session.commit()
        assert released == 1
        second = await execute_image_job(
            {"runtime": runtime, "image_job_executor": flaky_executor}, retry_job_id
        )
        assert second["status"] == "succeeded"

        timeout_quote = await quote(client, "color.effect")
        timeout_created = await create_job(client, timeout_quote, key="timeout-job")
        timeout_job_id = uuid.UUID(timeout_created.json()["job"]["id"])
        started_at = datetime.now(UTC) - timedelta(minutes=5)
        async with job_context.database.session_factory() as session:
            claim = await job_context.job_service.claim_job(
                session,
                job_id=timeout_job_id,
                worker_id="slow-worker",
                request_id="test-claim",
                now=started_at,
            )
            await session.commit()
        assert claim is not None
        async with job_context.database.session_factory() as session:
            reconciled = await job_context.job_service.reconcile_stale_jobs(
                session,
                now=datetime.now(UTC),
                request_id="test-timeout",
            )
            await session.commit()
        assert reconciled.timed_out == 1
        async with job_context.database.session_factory() as session:
            accepted = await job_context.job_service.complete_job(
                session,
                claim=claim,
                output_asset_id=uuid7(),
                provider_request_id="late-provider-result",
                metrics={},
                request_id="test-late-result",
            )
            await session.commit()
        assert accepted is False

        events = await client.get(f"/api/v1/jobs/{retry_job_id}/events")
        assert events.status_code == 200
        assert events.json()["job"]["status"] == "succeeded"
        assert [item["status"] for item in events.json()["attempts"]] == [
            "failed",
            "succeeded",
        ]

    async with job_context.database.session_factory() as session:
        jobs = {str(item.id): item for item in (await session.scalars(select(ImageJob))).all()}
        assert jobs[retry_job_id].refund_status == "none"
        assert jobs[str(timeout_job_id)].status == "timed_out"
        assert jobs[str(timeout_job_id)].refund_status == "refunded"
        account = (
            await session.scalars(select(PointAccount).where(PointAccount.user_id == user.id))
        ).one()
        assert account.balance == 180
        assert (
            await session.scalar(
                select(func.count(JobAttempt.id)).where(
                    JobAttempt.job_id == uuid.UUID(retry_job_id)
                )
            )
            == 2
        )
        notifications = list(
            (
                await session.scalars(
                    select(UserNotification)
                    .where(UserNotification.user_id == user.id)
                    .order_by(UserNotification.created_at)
                )
            ).all()
        )
        assert [item.type for item in notifications] == ["job.succeeded", "job.failed"]
        assert notifications[1].target_url == "/app/jobs"


@pytest.mark.asyncio
async def test_one_hundred_identical_creates_make_one_job_and_one_charge(
    job_context: JobContext,
) -> None:
    user = await seed_user(job_context, email="concurrent@example.com")
    async with job_context.database.session_factory() as session:
        job_quote = await job_context.job_service.create_quote(
            session,
            user_id=user.id,
            operation_code="ai.generate",
            source_asset_id=None,
            parameters={"prompt": "one request"},
            ttl_seconds=300,
            request_id="concurrent-quote",
        )
        await session.commit()
        quote_id = job_quote.id
    parameters = {"prompt": "one request"}
    fingerprint = job_context.job_service.request_fingerprint(user.id, quote_id, parameters)

    async def submit(index: int) -> tuple[uuid.UUID, bool]:
        async with job_context.database.session_factory() as session:
            job, created = await job_context.job_service.create_job(
                session,
                user_id=user.id,
                quote_id=quote_id,
                parameters=parameters,
                idempotency_key="same-create-key",
                request_fingerprint=fingerprint,
                request_id=f"concurrent-{index}",
            )
            await session.commit()
            return job.id, created

    results = await asyncio.gather(*(submit(index) for index in range(100)))
    assert len({job_id for job_id, _created in results}) == 1
    assert sum(created for _job_id, created in results) == 1
    async with job_context.database.session_factory() as session:
        assert await session.scalar(select(func.count(ImageJob.id))) == 1
        assert (
            await session.scalar(
                select(func.count(PointTransaction.id)).where(
                    PointTransaction.user_id == user.id,
                    PointTransaction.entry_type == "consume",
                )
            )
            == 1
        )
        account = (
            await session.scalars(select(PointAccount).where(PointAccount.user_id == user.id))
        ).one()
        assert account.balance == 180


@pytest.mark.asyncio
async def test_admin_permissions_operation_control_and_job_reconciliation(
    job_context: JobContext,
) -> None:
    user = await seed_user(job_context, email="reconcile@example.com")
    finance = await seed_user(
        job_context, email="finance@example.com", role_codes=("user", "finance")
    )
    operator = await seed_user(
        job_context, email="operator@example.com", role_codes=("user", "operator")
    )
    async with (
        client_for(job_context, user_agent="user-device") as user_client,
        client_for(job_context, user_agent="finance-device") as finance_client,
        client_for(job_context, user_agent="operator-device") as operator_client,
    ):
        await login(user_client, user.email)
        await login(finance_client, finance.email)
        await login(operator_client, operator.email)
        denied = await operator_client.patch(
            "/api/v1/admin/operations/vectorize.svg",
            json={"enabled": False, "reason": "无价格管理权限"},
        )
        assert denied.status_code == 403
        disabled = await finance_client.patch(
            "/api/v1/admin/operations/vectorize.svg",
            json={"enabled": False, "reason": "维护执行引擎"},
        )
        assert disabled.status_code == 200
        unavailable = await user_client.post(
            "/api/v1/jobs/quote",
            json={"operation_code": "vectorize.svg", "parameters": {}},
        )
        assert unavailable.status_code == 404
        enabled = await finance_client.patch(
            "/api/v1/admin/operations/vectorize.svg",
            json={"enabled": True, "reason": "维护完成"},
        )
        assert enabled.status_code == 200

        job_quote = await quote(user_client, "vectorize.svg")
        created = await create_job(user_client, job_quote, key="missing-refund")
        job_id = uuid.UUID(created.json()["job"]["id"])
        async with job_context.database.session_factory() as session:
            await session.execute(
                update(ImageJob)
                .where(ImageJob.id == job_id)
                .values(
                    status="failed",
                    completed_at=datetime.now(UTC),
                    error_code="TEST_FAILURE",
                )
            )
            await session.commit()
        listed = await operator_client.get("/api/v1/admin/jobs?status=failed")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["id"] == str(job_id)
        reconciled = await operator_client.post(
            f"/api/v1/admin/jobs/{job_id}/reconcile",
            json={"reason": "补偿缺失退款"},
        )
        assert reconciled.status_code == 200, reconciled.text
        assert reconciled.json()["result"]["refunds_created"] == 1
        retry = await operator_client.post(
            f"/api/v1/admin/jobs/{job_id}/retry",
            json={"reason": "不应复用已退款任务"},
        )
        assert retry.status_code == 409
        assert retry.json()["code"] == "JOB_ALREADY_REFUNDED"

    async with job_context.database.session_factory() as session:
        account = (
            await session.scalars(select(PointAccount).where(PointAccount.user_id == user.id))
        ).one()
        assert account.balance == 200
        job = await session.get(ImageJob, job_id)
        assert job is not None
        assert job.refund_status == "refunded"


async def publish_task_concurrency(context: JobContext, user: User, limit: int) -> None:
    async with context.database.session_factory() as session:
        group = await session.scalar(select(ConfigGroup).where(ConfigGroup.code == "general"))
        if group is None:
            group = ConfigGroup(id=uuid7(), code="general", name="General")
            session.add(group)
            await session.flush()
        number = (group.active_version or 0) + 1
        session.add(
            ConfigVersion(
                group_id=group.id,
                version=number,
                status="active",
                values={"task_concurrency": limit},
                created_by=user.id,
                change_reason="Test queue capacity",
            )
        )
        group.active_version = number
        await session.commit()


async def submit_queue_job(context: JobContext, user: User, key: str) -> ImageJob:
    async with context.database.session_factory() as session:
        job_quote = await context.job_service.create_quote(
            session,
            user_id=user.id,
            operation_code="cutout.smart",
            source_asset_id=None,
            parameters={},
            ttl_seconds=300,
            request_id=key,
        )
        await session.flush()
        job, created = await context.job_service.create_job(
            session,
            user_id=user.id,
            quote_id=job_quote.id,
            parameters={},
            idempotency_key=key,
            request_fingerprint=context.job_service.request_fingerprint(user.id, job_quote.id, {}),
            request_id=key,
        )
        await session.commit()
        assert created
        return job


async def claim_queue_job(context: JobContext, job: ImageJob):
    async with context.database.session_factory() as session:
        claim = await context.job_service.claim_job(
            session,
            job_id=job.id,
            worker_id=f"worker-{uuid.uuid4()}",
            request_id="queue-test",
            system_concurrency_limit=64,
        )
        await session.commit()
        return claim


@pytest.mark.parametrize("owner", [None, uuid.UUID(int=1)])
def test_postgres_queue_eligibility_query_compiles(owner: uuid.UUID | None) -> None:
    eligible = JobService._eligible_queued_jobs(datetime.now(UTC), user_id=owner)
    statement = (
        select(eligible.c.id, eligible.c.pricing_snapshot)
        .order_by(eligible.c.position, eligible.c.queued_at, eligible.c.id)
        .limit(2)
    )
    sql = str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    # PostgreSQL needs JSON text extraction cast to integer before capacity arithmetic.
    assert "->> 'max_concurrent_jobs' AS INTEGER)" in sql
    assert "row_number() OVER (PARTITION BY image_jobs.user_id" in sql
    assert "LEFT OUTER JOIN" in sql
    assert "LIMIT 2" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job_context",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.integration)],
    indirect=True,
)
@pytest.mark.parametrize("scope", ["user", "system"])
async def test_claim_races_enforce_running_caps_and_duplicate_delivery(
    job_context: JobContext, scope: str
) -> None:
    async with job_context.database.session_factory() as session:
        await session.execute(update(MembershipPlan).values(max_concurrent_jobs=2))
        await session.commit()
    users = [
        await seed_user(job_context, email=f"race-{i}@example.test")
        for i in range(1 if scope == "user" else 3)
    ]
    await publish_task_concurrency(job_context, users[0], 64 if scope == "user" else 2)
    jobs = [
        await submit_queue_job(job_context, user, f"race-{i}-{n}")
        for i, user in enumerate(users)
        for n in range(6 if scope == "user" else 2)
    ]
    start = asyncio.Event()

    async def contender(job):
        await start.wait()
        return await claim_queue_job(job_context, job)

    # Independent connections and service calls, including duplicate deliveries.
    tasks = [asyncio.create_task(contender(job)) for job in reversed(jobs * 2)]
    start.set()
    claims = [claim for claim in await asyncio.gather(*tasks) if claim is not None]
    assert len(claims) == len({claim.job_id for claim in claims}) == 2
    if scope == "user":
        assert {claim.job_id for claim in claims} == {job.id for job in jobs[:2]}
    async with job_context.database.session_factory() as session:
        stored = list(await session.scalars(select(ImageJob)))
        assert sum(job.status == "running" for job in stored) == 2
        assert await session.scalar(select(func.count(JobAttempt.id))) == 2
        for job in stored:
            if job.status == "queued":
                assert job.attempt_count == 0
                assert job.started_at is None
                assert job.worker_id is None
                assert job.progress == 0
            assert job.refund_status == "none"
        assert await session.scalar(
            select(func.count(PointTransaction.id)).where(PointTransaction.entry_type == "consume")
        ) == len(jobs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job_context",
    ["sqlite", pytest.param("postgresql", marks=pytest.mark.integration)],
    indirect=True,
)
async def test_rolled_back_claim_releases_capacity_without_attempt_or_event(
    job_context: JobContext,
) -> None:
    user = await seed_user(job_context, email="rollback-claim@example.test")
    job = await submit_queue_job(job_context, user, "rollback-claim")
    await publish_task_concurrency(job_context, user, 1)
    async with job_context.database.session_factory() as session:
        claim = await job_context.job_service.claim_job(
            session, job_id=job.id, worker_id="rollback", request_id="rollback"
        )
        assert claim is not None
        contender = asyncio.create_task(claim_queue_job(job_context, job))
        try:
            # A second process must wait for the admission transaction to end.
            done, _ = await asyncio.wait({contender}, timeout=0.1)
            assert not done
        finally:
            await session.rollback()
    accepted = await asyncio.wait_for(contender, timeout=5)
    assert accepted is not None
    assert accepted.attempt_no == 1
    async with job_context.database.session_factory() as session:
        assert await session.scalar(select(func.count(JobAttempt.id))) == 1
        statuses = list(
            await session.scalars(
                select(OutboxEvent.payload)
                .where(OutboxEvent.aggregate_id == job.id, OutboxEvent.topic == "jobs.status")
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
            )
        )
        assert [payload["status"] for payload in statuses] == ["queued", "running"]
        assert statuses[-1]["attempt_count"] == 1


@pytest.mark.asyncio
async def test_full_system_accepts_jobs_and_published_limit_changes_apply_at_claim(
    job_context: JobContext,
) -> None:
    users = [await seed_user(job_context, email=f"capacity-{i}@example.test") for i in range(3)]
    await publish_task_concurrency(job_context, users[0], 1)
    first = await submit_queue_job(job_context, users[0], "capacity-first")
    assert await claim_queue_job(job_context, first) is not None
    async with client_for(job_context, user_agent="capacity-device") as client:
        await login(client, users[0].email)
        additional = await create_job(
            client, await quote(client, "color.effect"), key="capacity-api"
        )
        assert additional.status_code == 201, additional.text
        assert additional.json()["job"]["status"] == "queued"
    second = await submit_queue_job(job_context, users[1], "capacity-second")
    third = await submit_queue_job(job_context, users[2], "capacity-third")
    assert await claim_queue_job(job_context, second) is None
    # The API submission snapshot stays the same; the worker observes the new publication.
    await publish_task_concurrency(job_context, users[0], 2)
    assert await claim_queue_job(job_context, second) is not None
    await publish_task_concurrency(job_context, users[0], 1)
    assert await claim_queue_job(job_context, third) is None
    async with job_context.database.session_factory() as session:
        # Lowering capacity does not cancel work already running.
        assert (
            await session.scalar(
                select(func.count(ImageJob.id)).where(ImageJob.status == "running")
            )
            == 2
        )
        assert await job_context.job_service.dispatchable_jobs(session) == []


@pytest.mark.asyncio
async def test_dispatch_skips_saturated_backlog_and_shares_slots_between_users(
    job_context: JobContext,
) -> None:
    users = [await seed_user(job_context, email=f"fair-{i}@example.test") for i in range(3)]
    await publish_task_concurrency(job_context, users[0], 3)
    blocked = [await submit_queue_job(job_context, users[0], f"backlog-{n}") for n in range(5)]
    claim = await claim_queue_job(job_context, blocked[0])
    assert claim is not None
    second = await submit_queue_job(job_context, users[1], "fair-second")
    third = await submit_queue_job(job_context, users[2], "fair-third")
    async with job_context.database.session_factory() as session:
        # Apply LIMIT after filtering users whose capacity is full.
        selected = await job_context.job_service.dispatchable_jobs(session, limit=2)
        assert [job_id for job_id, _ in selected] == [second.id, third.id]
        assert (
            await job_context.job_service.fail_job(
                session,
                claim=claim,
                code="BUSY",
                message="retry",
                retryable=True,
                timed_out=False,
                provider_request_id=None,
                metrics={},
                request_id="fair-retry",
            )
            == "retry_wait"
        )
        await session.commit()
    async with job_context.database.session_factory() as session:
        selected = await job_context.job_service.dispatchable_jobs(session)
        assert [job_id for job_id, _ in selected] == [blocked[1].id, second.id, third.id]


@pytest.mark.asyncio
async def test_dispatch_offers_each_user_a_slot_before_filling_member_capacity(
    job_context: JobContext,
) -> None:
    async with job_context.database.session_factory() as session:
        await session.execute(update(MembershipPlan).values(max_concurrent_jobs=3))
        await session.commit()
    users = [await seed_user(job_context, email=f"share-{i}@example.test") for i in range(3)]
    await publish_task_concurrency(job_context, users[0], 4)
    jobs = [
        [await submit_queue_job(job_context, user, f"share-{i}-{n}") for n in range(3)]
        for i, user in enumerate(users)
    ]
    async with job_context.database.session_factory() as session:
        selected = await job_context.job_service.dispatchable_jobs(session)
        assert [job_id for job_id, _ in selected] == [
            jobs[0][0].id,
            jobs[1][0].id,
            jobs[2][0].id,
            jobs[0][1].id,
        ]


@pytest.mark.asyncio
async def test_queue_wait_does_not_start_timeout_or_bypass_deferred_delivery(
    job_context: JobContext,
) -> None:
    user = await seed_user(job_context, email="queued-timeout@example.test")
    job = await submit_queue_job(job_context, user, "queued-timeout")
    now = datetime.now(UTC)
    async with job_context.database.session_factory() as session:
        await session.execute(
            update(ImageJob)
            .where(ImageJob.id == job.id)
            .values(queued_at=now - timedelta(days=1), next_attempt_at=now + timedelta(minutes=1))
        )
        await session.commit()
    async with job_context.database.session_factory() as session:
        result = await job_context.job_service.reconcile_stale_jobs(
            session, now=now, request_id="queued-timeout"
        )
        assert result.checked == result.timed_out == result.refunds_created == 0
        assert await job_context.job_service.dispatchable_jobs(session, now=now) == []
        assert (
            await job_context.job_service.claim_job(
                session, job_id=job.id, worker_id="early", request_id="early", now=now
            )
            is None
        )
        await session.commit()
    async with job_context.database.session_factory() as session:
        claim = await job_context.job_service.claim_job(
            session,
            job_id=job.id,
            worker_id="due",
            request_id="due",
            now=now + timedelta(minutes=2),
        )
        assert claim is not None
        # Expiration uses the attempt start, regardless of how long it was queued.
        result = await job_context.job_service.reconcile_stale_jobs(
            session, now=now + timedelta(minutes=3), request_id="fresh-attempt"
        )
        assert result.checked == 1
        assert result.timed_out == result.refunds_created == 0
        await session.commit()


@pytest.mark.asyncio
async def test_waiting_delivery_and_due_retry_survive_arq_dedup_and_refund_once(
    job_context: JobContext,
    monkeypatch,
) -> None:
    user = await seed_user(job_context, email="delivery@example.test")
    await publish_task_concurrency(job_context, user, 1)
    first = await submit_queue_job(job_context, user, "delivery-first")
    second = await submit_queue_job(job_context, user, "delivery-second")

    class RetainedDeliveries:
        """ARQ refuses an ID while either its payload or result still exists."""

        def __init__(self):
            self.ids = set()
            self.deliveries = []

        async def enqueue_job(self, function, job_id, *, _job_id, _queue_name):
            if _job_id in self.ids:
                return None
            self.ids.add(_job_id)
            self.deliveries.append((job_id, _job_id))
            return SimpleNamespace(job_id=_job_id)

        async def aclose(self):
            pass

    redis = RetainedDeliveries()

    async def pool(_settings):
        return redis

    monkeypatch.setattr("app.api.jobs.create_pool", pool)
    del job_context.app.state.job_enqueuer
    request = Request({"type": "http", "app": job_context.app})
    assert await enqueue_job(request, second)
    first_delivery_id = redis.deliveries[-1][1]
    calls = []

    async def busy(claim):
        calls.append(claim.job_id)
        raise RetryableJobError("UPSTREAM_BUSY", "Try again")

    ctx = {
        "runtime": SimpleNamespace(database=job_context.database, instance_name="delivery-worker"),
        "settings": job_context.app.state.settings,
        "redis": redis,
        "image_job_executor": busy,
    }
    # Out-of-order API delivery stays queued, without consuming an application attempt.
    assert (await execute_image_job(ctx, str(second.id)))["status"] == "ignored"
    assert calls == []
    assert (await execute_image_job(ctx, str(first.id)))["status"] == "retry_wait"
    assert (await dispatch_image_jobs(ctx))["jobs_dispatched"] == 1
    assert redis.deliveries[-1][0] == str(second.id)
    assert redis.deliveries[-1][1] != first_delivery_id
    # Even if a previous ARQ result has not expired, redispatch is independent.
    assert (await dispatch_image_jobs(ctx))["jobs_dispatched"] == 1
    assert (await execute_image_job(ctx, str(second.id)))["status"] == "retry_wait"
    async with job_context.database.session_factory() as session:
        waiting = await session.get(ImageJob, second.id)
        assert waiting.attempt_count == 1
        assert waiting.refund_status == "none"
        assert (
            await job_context.job_service.release_due_retries(
                session, now=datetime.now(UTC) - timedelta(seconds=1), request_id="too-early"
            )
            == 0
        )
        # Leave the first retry in backoff; the scheduler must release only the due retry.
        waiting.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    dispatched = await dispatch_image_jobs(ctx)
    assert dispatched == {"retries_released": 1, "jobs_dispatched": 1}
    assert redis.deliveries[-1][0] == str(second.id)
    # cutout.smart permits two attempts, then refunds the original charge once.
    assert (await execute_image_job(ctx, str(second.id)))["status"] == "failed"
    assert (await execute_image_job(ctx, str(second.id)))["status"] == "ignored"
    async with job_context.database.session_factory() as session:
        completed = await session.get(ImageJob, second.id)
        assert completed.attempt_count == 2
        assert completed.refund_status == "refunded"
        assert (
            await session.scalar(
                select(func.count(PointTransaction.id)).where(
                    PointTransaction.reference_id == second.id,
                    PointTransaction.entry_type == "refund",
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(PointAccount.balance).where(PointAccount.user_id == user.id)
            )
            == 198
        )
