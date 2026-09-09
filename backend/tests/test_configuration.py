from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.api.auth import router as auth_router
from app.api.configuration import router as configuration_router
from app.api.errors import install_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.config import Settings
from app.domain.ids import uuid7
from app.repositories.database import Database
from app.repositories.models import (
    Base,
    ConfigGroup,
    EncryptedSecret,
    OutboxEvent,
    Role,
    User,
    UserRole,
)
from app.services.auth import AuthService
from app.services.configuration import (
    ConfigCipher,
    ConfigLoadError,
    ConfigService,
    RuntimeConfigCache,
)
from app.services.configuration import (
    TestOutcome as ConnectionTestOutcome,
)
from app.services.rbac import sync_builtin_rbac
from app.services.runtime import RuntimeServices
from app.workers.worker import execute_config_test

MASTER_KEY = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")
PLAINTEXT = "sub2-secret-value-1234"


class FakeTester:
    async def test(self, _config) -> ConnectionTestOutcome:
        return ConnectionTestOutcome(True, "CONNECTED", "连接测试通过", 4)


@dataclass
class ConfigRuntime:
    database: Database
    config_service: ConfigService
    config_cache: RuntimeConfigCache
    invalidations: list[tuple[str, int]] = field(default_factory=list)

    async def broadcast_config_invalidation(self, group: str, version: int) -> None:
        self.config_cache.invalidate(group)
        self.invalidations.append((group, version))


@dataclass
class ConfigContext:
    app: FastAPI
    database: Database
    auth_service: AuthService
    runtime: ConfigRuntime


@pytest_asyncio.fixture
async def config_context(tmp_path) -> ConfigContext:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'config.db').as_posix()}"
    settings = Settings(
        _env_file=None,
        APP_ENV="test",
        DATABASE_URL=database_url,
        APP_CONFIG_MASTER_KEY=MASTER_KEY,
    )
    database = Database(database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    auth_service = AuthService(settings)
    config_service = ConfigService(ConfigCipher.from_settings(settings))
    config_cache = RuntimeConfigCache(database.session_factory, config_service, ttl_seconds=45)
    runtime = ConfigRuntime(database, config_service, config_cache)
    async with database.session_factory() as session:
        await sync_builtin_rbac(session)
        await config_service.ensure_groups(session)
        await session.commit()
    app = FastAPI()
    app.state.settings = settings
    app.state.auth_service = auth_service
    app.state.runtime_services = runtime
    app.state.config_connection_tester = FakeTester()
    install_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(auth_router)
    app.include_router(configuration_router)
    yield ConfigContext(app, database, auth_service, runtime)
    await database.dispose()


async def seed_user(context: ConfigContext, email: str, roles: tuple[str, ...]) -> User:
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


def client_for(context: ConfigContext, user_agent: str) -> AsyncClient:
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


def sub2api_draft(*, model: str = "gpt-image-2", secret: str = PLAINTEXT) -> dict:
    return {
        "values": {
            "enabled": True,
            "base_url": "https://sub2api.example.test/v1",
            "image_model": model,
            "timeout_seconds": 120,
        },
        "secrets": {"api_key": secret},
        "change_reason": "轮换上游配置",
        "confirm_sensitive_change": True,
    }


def test_aes_gcm_uses_unique_nonce_aad_and_rejects_tampering() -> None:
    cipher = ConfigCipher(b"x" * 32)
    first = cipher.encrypt("sub2api", "api_key", PLAINTEXT)
    second = cipher.encrypt("sub2api", "api_key", PLAINTEXT)
    assert first[1] != second[1]
    assert PLAINTEXT.encode() not in first[0]
    secret = EncryptedSecret(
        id=uuid7(),
        config_version_id=uuid7(),
        key_name="api_key",
        ciphertext=first[0],
        nonce=first[1],
        key_version=1,
        value_fingerprint=first[2],
        last_four=first[3],
    )
    assert cipher.decrypt("sub2api", secret) == PLAINTEXT
    with pytest.raises(ConfigLoadError):
        cipher.decrypt("email", secret)
    secret.ciphertext = bytes([secret.ciphertext[0] ^ 1]) + secret.ciphertext[1:]
    with pytest.raises(ConfigLoadError):
        cipher.decrypt("sub2api", secret)


def test_production_requires_valid_config_master_key() -> None:
    with pytest.raises(ValueError, match="APP_CONFIG_MASTER_KEY"):
        Settings(
            _env_file=None,
            APP_ENV="production",
            LEGACY_SYNC_API_ENABLED=False,
            DEPENDENCY_CHECKS_ENABLED=True,
            SESSION_COOKIE_SECURE=True,
            AUTH_TOKEN_PEPPER="token-pepper-0123456789-abcdefghijklmnop",
            AUTH_HASH_SALT="hash-salt-0123456789-abcdefghijklmnopq",
            PUBLIC_APP_URL="https://studio.example.com",
        )


@pytest.mark.asyncio
async def test_permissions_masking_publish_inherit_rollback_and_clear(
    config_context: ConfigContext,
) -> None:
    operator = await seed_user(config_context, "operator@example.com", ("user", "operator"))
    auditor = await seed_user(config_context, "auditor@example.com", ("user", "auditor"))
    admin = await seed_user(config_context, "admin@example.com", ("user", "super_admin"))
    async with (
        client_for(config_context, "operator-device") as operator_client,
        client_for(config_context, "auditor-device") as auditor_client,
        client_for(config_context, "admin-device") as admin_client,
    ):
        await login(operator_client, operator.email)
        await login(auditor_client, auditor.email)
        await login(admin_client, admin.email)
        assert (await operator_client.get("/api/v1/admin/config")).status_code == 403
        assert (await auditor_client.get("/api/v1/admin/config")).status_code == 200
        assert (
            await auditor_client.post("/api/v1/admin/config/sub2api/drafts", json=sub2api_draft())
        ).status_code == 403

        unconfirmed = sub2api_draft()
        unconfirmed["confirm_sensitive_change"] = False
        denied = await admin_client.post("/api/v1/admin/config/sub2api/drafts", json=unconfirmed)
        assert denied.status_code == 409
        created = await admin_client.post(
            "/api/v1/admin/config/sub2api/drafts", json=sub2api_draft()
        )
        assert created.status_code == 201, created.text
        assert PLAINTEXT not in created.text
        assert created.json()["version"]["secrets"]["api_key"] == {
            "has_value": True,
            "last_four": "1234",
            "updated_at": created.json()["version"]["secrets"]["api_key"]["updated_at"],
        }
        blocked_publish = await admin_client.post("/api/v1/admin/config/sub2api/drafts/1/publish")
        assert blocked_publish.status_code == 409
        tested = await admin_client.post("/api/v1/admin/config/sub2api/drafts/1/test")
        assert tested.status_code == 202
        assert tested.json()["test_run"]["status"] == "succeeded"
        published = await admin_client.post("/api/v1/admin/config/sub2api/drafts/1/publish")
        assert published.status_code == 200, published.text
        loaded = await config_context.runtime.config_cache.get("sub2api")
        assert loaded.version == 1
        assert loaded.secrets == {"api_key": PLAINTEXT}

        inherited_payload = sub2api_draft(model="gpt-image-3", secret="")
        inherited_payload["confirm_sensitive_change"] = False
        inherited = await admin_client.post(
            "/api/v1/admin/config/sub2api/drafts", json=inherited_payload
        )
        assert inherited.status_code == 201, inherited.text
        assert inherited.json()["version"]["secrets"]["api_key"]["has_value"] is True
        assert (
            await admin_client.post("/api/v1/admin/config/sub2api/drafts/2/test")
        ).status_code == 202
        assert (
            await admin_client.post("/api/v1/admin/config/sub2api/drafts/2/publish")
        ).status_code == 200

        rolled_back = await admin_client.post(
            "/api/v1/admin/config/sub2api/versions/1/rollback",
            json={
                "reason": "恢复已验证版本",
                "confirm_sensitive_change": True,
            },
        )
        assert rolled_back.status_code == 200, rolled_back.text
        assert rolled_back.json()["version"]["version"] == 3
        assert rolled_back.json()["version"]["values"]["image_model"] == "gpt-image-2"
        cleared = await admin_client.post(
            "/api/v1/admin/config/sub2api/secrets/api_key/clear",
            json={"reason": "停用泄露凭据", "confirm_sensitive_change": True},
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["version"]["version"] == 4
        assert cleared.json()["version"]["values"]["enabled"] is False
        assert cleared.json()["version"]["secrets"]["api_key"]["has_value"] is False
        history = await auditor_client.get("/api/v1/admin/config/sub2api/history")
        assert history.status_code == 200
        assert PLAINTEXT not in history.text

    assert config_context.runtime.invalidations == [
        ("sub2api", 1),
        ("sub2api", 2),
        ("sub2api", 3),
        ("sub2api", 4),
    ]
    async with config_context.database.session_factory() as session:
        encrypted = list((await session.scalars(select(EncryptedSecret))).all())
        assert encrypted
        assert all(PLAINTEXT.encode() not in bytes(item.ciphertext) for item in encrypted)
        audit_payloads = [
            item.payload
            for item in (await session.scalars(select(OutboxEvent))).all()
            if item.topic == "config.audit"
        ]
        assert audit_payloads
        assert PLAINTEXT not in json.dumps(audit_payloads, default=str)


@pytest.mark.asyncio
async def test_cache_retains_last_good_config_after_ciphertext_corruption(
    config_context: ConfigContext,
) -> None:
    admin = await seed_user(config_context, "cache-admin@example.com", ("super_admin",))
    service = config_context.runtime.config_service
    async with config_context.database.session_factory() as session:
        draft = await service.create_draft(
            session,
            code="sub2api",
            actor_user_id=admin.id,
            values=sub2api_draft()["values"],
            secrets={"api_key": PLAINTEXT},
            change_reason="缓存基线",
            confirmed=True,
            request_id="cache-baseline",
        )
        draft.status = "active"
        draft.published_by = admin.id
        group = (
            await session.scalars(select(ConfigGroup).where(ConfigGroup.code == "sub2api"))
        ).one()
        group.active_version = draft.version
        await session.commit()
    loaded = await config_context.runtime.config_cache.get("sub2api")
    assert loaded.secrets["api_key"] == PLAINTEXT
    async with config_context.database.session_factory() as session:
        secret = (await session.scalars(select(EncryptedSecret))).one()
        damaged = bytes([secret.ciphertext[0] ^ 1]) + secret.ciphertext[1:]
        await session.execute(
            update(EncryptedSecret)
            .where(EncryptedSecret.id == secret.id)
            .values(ciphertext=damaged)
        )
        await session.commit()
    config_context.runtime.config_cache.invalidate("sub2api")
    retained = await config_context.runtime.config_cache.get("sub2api")
    assert retained is loaded
    assert retained.secrets["api_key"] == PLAINTEXT


@pytest.mark.asyncio
async def test_worker_completes_queued_connection_test(config_context: ConfigContext) -> None:
    admin = await seed_user(config_context, "worker-admin@example.com", ("super_admin",))
    service = config_context.runtime.config_service
    async with config_context.database.session_factory() as session:
        draft = await service.create_draft(
            session,
            code="general",
            actor_user_id=admin.id,
            values={},
            secrets={},
            change_reason="验证异步测试",
            confirmed=False,
            request_id="worker-config-test",
        )
        _group, _version, run, _resolved = await service.start_test(
            session,
            code="general",
            version_number=draft.version,
            actor_user_id=admin.id,
        )
        await session.commit()
    result = await execute_config_test(
        {
            "runtime": config_context.runtime,
            "settings": config_context.app.state.settings,
        },
        str(run.id),
    )
    assert result["status"] == "succeeded"


@pytest.mark.asyncio
async def test_test_result_is_rejected_when_draft_changes_during_run(
    config_context: ConfigContext,
) -> None:
    admin = await seed_user(config_context, "race-admin@example.com", ("super_admin",))
    service = config_context.runtime.config_service
    async with config_context.database.session_factory() as session:
        draft = await service.create_draft(
            session,
            code="general",
            actor_user_id=admin.id,
            values={},
            secrets={},
            change_reason="测试竞态基线",
            confirmed=False,
            request_id="race-baseline",
        )
        group, version, run, _resolved = await service.start_test(
            session,
            code="general",
            version_number=draft.version,
            actor_user_id=admin.id,
        )
        await service.update_draft(
            session,
            code="general",
            version_number=draft.version,
            actor_user_id=admin.id,
            values={"default_points": 99},
            secrets=None,
            change_reason="测试期间修改",
            confirmed=False,
            request_id="race-update",
        )
        service.finish_test(
            session,
            group=group,
            version=version,
            run=run,
            outcome=ConnectionTestOutcome(True, "CONNECTED", "连接测试通过", 2),
            actor_user_id=admin.id,
            request_id="race-finish",
        )
        await session.commit()
    assert run.status == "failed"
    assert run.result_code == "CONFIG_CHANGED_DURING_TEST"


@pytest.mark.asyncio
async def test_publish_broadcasts_redis_invalidation_and_refreshes_r2() -> None:
    class Cache:
        def __init__(self) -> None:
            self.invalidated: list[str] = []

        def invalidate(self, group: str) -> None:
            self.invalidated.append(group)

    class Redis:
        def __init__(self) -> None:
            self.published: list[tuple[str, str]] = []

        async def publish(self, channel: str, payload: str) -> None:
            self.published.append((channel, payload))

    class Storage:
        refreshed = 0

        async def refresh(self) -> None:
            self.refreshed += 1

    runtime = object.__new__(RuntimeServices)
    runtime.config_cache = Cache()
    runtime.redis = Redis()
    runtime.object_storage = Storage()
    await runtime.broadcast_config_invalidation("r2", 8)
    assert runtime.config_cache.invalidated == ["r2"]
    assert runtime.object_storage.refreshed == 1
    assert runtime.redis.published == [("config.invalidate", '{"group":"r2","version":8}')]
