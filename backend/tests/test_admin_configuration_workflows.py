from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from test_configuration import client_for as config_client
from test_configuration import config_context as config_fixture
from test_configuration import login as config_login
from test_configuration import seed_user as config_user
from test_jobs import client_for as job_client
from test_jobs import job_context as job_fixture
from test_jobs import login as job_login
from test_jobs import quote
from test_jobs import seed_user as job_user
from test_memberships import client_for as membership_client
from test_memberships import login as membership_login
from test_memberships import membership_context as membership_fixture
from test_memberships import seed_user as membership_user
from test_points import client_for as points_client
from test_points import login as points_login
from test_points import point_context as point_fixture
from test_points import seed_user as points_user
from test_rbac import client_for as role_client
from test_rbac import login as role_login
from test_rbac import rbac_context as role_fixture
from test_rbac import seed_user as role_user

from app.repositories.models import OperationPrice, OutboxEvent, PointTransaction
from app.services.memberships import EntitlementService, sync_builtin_membership_plans

config_context = config_fixture
job_context = job_fixture
membership_context = membership_fixture
point_context = point_fixture
rbac_context = role_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("group", "values", "secrets"),
    [
        ("sub2api", {"base_url": "https://upstream.example.test/v1"}, {"api_key": "test-key"}),
        (
            "r2",
            {
                "endpoint_url": "https://r2.example.test",
                "account_id": "account",
                "bucket": "images",
            },
            {"access_key_id": "test-access", "secret_access_key": "test-secret"},
        ),
        ("email", {"host": "smtp.example.test", "from_email": "admin@example.test"}, {}),
    ],
)
async def test_save_external_config_without_approval_or_test(
    config_context, group, values, secrets
):
    admin = await config_user(config_context, "admin@example.test", ("super_admin",))
    reader = await config_user(config_context, "reader@example.test", ("auditor",))
    url = f"/api/v1/admin/config/{group}"
    body = {"base_version": None, "values": {"enabled": True, **values}, "secrets": secrets}
    async with config_client(config_context, "admin") as client:
        await config_login(client, admin.email)
        saved = await client.put(url, json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()["version"]["status"] == "active"
        for secret in secrets.values():
            assert secret not in saved.text
        loaded = await config_context.runtime.config_cache.get(group)
        assert loaded.values["enabled"] is True
        assert loaded.secrets == secrets
        assert config_context.runtime.invalidations == [(group, 1)]
        stale = await client.put(url, json=body)
        assert stale.status_code == 409
        # Empty secret fields retain encrypted credentials on the next version.
        changed = await client.put(url, json={**body, "base_version": 1, "secrets": {}})
        assert changed.status_code == 200, changed.text
        loaded = await config_context.runtime.config_cache.get(group)
        assert loaded.version == 2
        assert loaded.secrets == secrets
        test = await client.post(f"{url}/test")
        assert test.status_code == 200
        assert test.json()["status"] == "succeeded"
    async with config_client(config_context, "reader") as client:
        await config_login(client, reader.email)
        assert (await client.get(url)).status_code == 200
        assert (await client.put(url, json={**body, "base_version": 2})).status_code == 403


@pytest.mark.asyncio
async def test_invalid_config_does_not_publish_and_general_defaults_reach_new_members(
    config_context,
):
    admin = await config_user(config_context, "admin@example.test", ("super_admin",))
    async with config_context.database.session_factory() as session:
        await sync_builtin_membership_plans(session)
        await session.commit()
    async with config_client(config_context, "admin") as client:
        await config_login(client, admin.email)
        missing = await client.put(
            "/api/v1/admin/config/sub2api",
            json={
                "base_version": None,
                "values": {"enabled": True, "base_url": "https://api.example.test"},
            },
        )
        assert missing.status_code == 409
        group = (await client.get("/api/v1/admin/config/sub2api")).json()["group"]
        assert group["active_version"] is None and group["latest_draft"] is None
        assert group["defaults"]["image_model"] == "gpt-image-2"
        saved = await client.put(
            "/api/v1/admin/config/general",
            json={
                "base_version": None,
                "values": {"default_points": 77, "default_membership_code": "pro"},
            },
        )
        assert saved.status_code == 200, saved.text
        invalid = await client.put(
            "/api/v1/admin/config/general",
            json={
                "base_version": 1,
                "values": {"default_membership_code": "missing"},
            },
        )
        assert invalid.status_code == 409
    assert (await config_context.runtime.config_cache.get("general")).values["default_points"] == 77
    user = await config_user(config_context, "new@example.test", ("user",))
    service = EntitlementService()
    async with config_context.database.session_factory() as session:
        start = datetime(2026, 1, 31, 12, tzinfo=UTC)
        membership = await service.ensure_default_membership(session, user.id, now=start)
        assert membership.entitlement_snapshot["plan_code"] == "pro"
        assert membership.ends_at == datetime(2026, 2, 28, 12, tzinfo=UTC)
        await session.commit()
        fallback = await service.current_snapshot(session, user.id, now=start + timedelta(days=40))
        assert fallback.plan_code == "free"  # A signup gift must not renew itself forever.


@pytest.mark.asyncio
async def test_super_admin_points_apply_once_while_finance_retains_approval(point_context):
    admin = await points_user(
        point_context, email="owner@example.test", role_codes=("super_admin",)
    )
    finance = await points_user(
        point_context, email="finance@example.test", role_codes=("finance",)
    )
    target = await points_user(point_context, email="target@example.test")
    url = f"/api/v1/admin/users/{target.id}/points/adjustments"
    async with points_client(point_context, user_agent="admin") as client:
        await points_login(client, admin.email)
        body = {"amount": 10000, "reason": "发放运营积分"}
        headers = {"Idempotency-Key": "large-admin-credit"}
        first = await client.post(url, json=body, headers=headers)
        replay = await client.post(url, json=body, headers=headers)
        assert first.status_code == replay.status_code == 201
        assert first.json()["adjustment"]["status"] == "applied"
        assert first.json()["adjustment"]["id"] == replay.json()["adjustment"]["id"]
        balance = (await client.get(f"/api/v1/admin/users/{target.id}/points")).json()["account"]
        assert balance["balance"] == 10020
        insufficient = await client.post(
            url,
            json={"amount": -20000, "reason": "不可透支"},
            headers={"Idempotency-Key": "no-overdraft"},
        )
        assert insufficient.status_code == 409
    async with points_client(point_context, user_agent="finance") as client:
        await points_login(client, finance.email)
        pending = await client.post(
            url,
            json={"amount": 10000, "reason": "需要审核"},
            headers={"Idempotency-Key": "finance-credit"},
        )
        assert pending.json()["adjustment"]["status"] == "pending"
        own_review = await client.post(
            f"/api/v1/admin/point-adjustments/{pending.json()['adjustment']['id']}/approve",
            json={"reason": "本人审批"},
            headers={"Idempotency-Key": "finance-self-review"},
        )
        assert own_review.status_code == 409
    async with point_context.database.session_factory() as session:
        count = await session.scalar(
            select(func.count(PointTransaction.id)).where(
                PointTransaction.user_id == target.id, PointTransaction.entry_type == "adjust"
            )
        )
        assert count == 1
        assert await session.scalar(
            select(func.count(OutboxEvent.id)).where(OutboxEvent.topic == "points.audit")
        )


@pytest.mark.asyncio
async def test_price_and_operation_save_atomically_and_change_quotes(job_context):
    admin = await job_user(
        job_context, email="owner@example.test", role_codes=("user", "super_admin")
    )
    async with job_client(job_context, user_agent="admin") as client:
        await job_login(client, admin.email)
        url = "/api/v1/admin/operations/ai.generate/configuration"
        body = {
            "name": "新版生成",
            "enabled": True,
            "base_points": 37,
            "parameter_rules": {},
            "reason": "管理员改价",
        }
        failed = await client.put(
            url, json={**body, "enabled": False, "parameter_rules": {"bad": True}}
        )
        assert failed.status_code == 422
        operations = (await client.get("/api/v1/admin/operations")).json()["items"]
        original = next(item for item in operations if item["code"] == "ai.generate")
        assert original["enabled"] is True and original["name"] != body["name"]
        saved = await client.put(url, json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()["operation"]["current_price"]["base_points"] == 37
        assert (await quote(client, "ai.generate"))["final_points"] == 37
        again = await client.put(url, json=body)
        assert again.json()["operation"]["current_price"]["version"] == 2
        async with job_context.database.session_factory() as session:
            assert (
                await session.scalar(
                    select(func.count(OperationPrice.id)).where(
                        OperationPrice.operation_id == UUID(saved.json()["operation"]["id"])
                    )
                )
                == 2
            )


@pytest.mark.asyncio
async def test_create_active_plan_and_assign_perpetual_membership(membership_context):
    admin = await membership_user(
        membership_context, email="owner@example.test", role_codes=("super_admin",)
    )
    target = await membership_user(membership_context, email="member@example.test")
    async with membership_client(membership_context, user_agent="admin") as client:
        await membership_login(client, admin.email)
        result = await client.post(
            "/api/v1/admin/membership-plans",
            json={
                "code": "team",
                "name": "团队会员",
                "level": 30,
                "status": "active",
                "billing_period": "none",
                "operation_discount_bps": 8000,
                "max_concurrent_jobs": 4,
                "max_upload_mb": 40,
                "max_image_megapixels": 80,
                "asset_retention_days": 120,
                "reason": "创建团队套餐",
            },
        )
        assert result.status_code == 201, result.text
        plan = result.json()["plan"]
        assert plan["status"] == "active"
        changed = await client.patch(
            f"/api/v1/admin/membership-plans/{plan['id']}",
            json={
                "name": "团队长期会员",
                "operation_discount_bps": 7500,
                "status": "active",
                "reason": "配置套餐",
            },
        )
        assert changed.status_code == 200
        assigned = await client.post(
            f"/api/v1/admin/users/{target.id}/memberships",
            json={"plan_id": plan["id"], "ends_at": None, "reason": "分配长期会员"},
            headers={"Idempotency-Key": "team-membership"},
        )
        assert assigned.status_code == 201, assigned.text
        assert assigned.json()["membership"]["entitlement_snapshot"]["discount_bps"] == 7500


@pytest.mark.asyncio
async def test_role_details_and_permissions_save_together(rbac_context):
    admin = await role_user(rbac_context, email="owner@example.test", role_codes=("super_admin",))
    async with role_client(rbac_context, user_agent="admin") as client:
        await role_login(client, admin.email)
        created = await client.post(
            "/api/v1/admin/roles",
            json={
                "code": "support",
                "name": "Support",
                "permission_codes": ["users.read"],
            },
        )
        assert created.status_code == 201
        role_id = created.json()["role"]["id"]
        invalid = await client.patch(
            f"/api/v1/admin/roles/{role_id}",
            json={
                "name": "Invalid update",
                "permission_codes": ["missing.permission"],
            },
        )
        assert invalid.status_code == 422
        saved = await client.patch(
            f"/api/v1/admin/roles/{role_id}",
            json={
                "name": "Support team",
                "permission_codes": ["users.read", "points.read"],
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["role"]["permissions"] == ["points.read", "users.read"]
        roles = (await client.get("/api/v1/admin/roles")).json()["items"]
        builtin = next(role for role in roles if role["code"] == "super_admin")
        assert (
            await client.patch(
                f"/api/v1/admin/roles/{builtin['id']}", json={"permission_codes": []}
            )
        ).status_code == 409


@pytest.mark.asyncio
async def test_super_admin_can_resolve_own_legacy_point_request(point_context):
    admin = await points_user(
        point_context, email="owner@example.test", role_codes=("super_admin",)
    )
    target = await points_user(point_context, email="member@example.test")
    async with point_context.database.session_factory() as session:
        legacy = await point_context.point_service.create_adjustment(
            session,
            user_id=target.id,
            amount=5000,
            reason="旧版待审调整",
            requested_by=admin.id,
            idempotency_key="legacy-request",
            request_fingerprint="legacy-fingerprint",
            approval_threshold=1000,
            request_id="legacy-test",
        )
        await session.commit()
        assert legacy.status == "pending"
    async with points_client(point_context, user_agent="admin") as client:
        await points_login(client, admin.email)
        result = await client.post(
            f"/api/v1/admin/point-adjustments/{legacy.id}/approve",
            json={"reason": "超级管理员处理历史调整"},
            headers={"Idempotency-Key": "resolve-legacy"},
        )
        assert result.status_code == 200, result.text
        assert result.json()["adjustment"]["status"] == "applied"
        balance = (await client.get(f"/api/v1/admin/users/{target.id}/points")).json()["account"]
        assert balance["balance"] == 5020
