import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app import main
from app.config import Settings
from app.domain.ids import uuid7
from app.repositories.service_instances import record_heartbeat


def test_public_health_is_minimal_and_has_request_id() -> None:
    response = TestClient(main.app).get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(response.headers["X-Request-ID"]) == 32


def test_valid_incoming_request_id_is_preserved() -> None:
    response = TestClient(main.app).get(
        "/api/health", headers={"X-Request-ID": "request-test-1234"}
    )

    assert response.headers["X-Request-ID"] == "request-test-1234"


def test_errors_use_the_global_contract() -> None:
    response = TestClient(main.app).get("/api/results/not-a-result.png")

    assert response.status_code == 404
    assert response.json() == {
        "code": "NOT_FOUND",
        "message": "Result not found or expired.",
        "request_id": response.headers["X-Request-ID"],
        "details": None,
    }


def test_framework_method_errors_use_the_global_contract() -> None:
    response = TestClient(main.app).delete("/api/health")

    assert response.status_code == 405
    assert response.json()["code"] == "METHOD_NOT_ALLOWED"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_admin_health_is_closed_until_authentication_is_implemented() -> None:
    response = TestClient(main.app).get("/api/v1/admin/system/health")

    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"


def test_degraded_public_health_uses_service_unavailable(monkeypatch) -> None:
    class DegradedRuntime:
        async def public_status(self) -> str:
            return "degraded"

    monkeypatch.setattr(main.app.state, "runtime_services", DegradedRuntime())
    response = TestClient(main.app).get("/api/health")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded"}


def test_production_rejects_legacy_sync_api() -> None:
    with pytest.raises(ValueError, match="LEGACY_SYNC_API_ENABLED"):
        Settings(
            _env_file=None,
            APP_ENV="production",
            LEGACY_SYNC_API_ENABLED=True,
            DEPENDENCY_CHECKS_ENABLED=True,
        )


def test_settings_reject_partial_r2_configuration() -> None:
    with pytest.raises(ValueError, match="must be configured together"):
        Settings(
            _env_file=None,
            R2_ENDPOINT_URL="https://account.r2.cloudflarestorage.com",
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"DEPENDENCY_CHECKS_ENABLED": False}, "DEPENDENCY_CHECKS_ENABLED"),
        ({"AUTH_TOKEN_PEPPER": "too-short"}, "AUTH_TOKEN_PEPPER"),
        ({"PUBLIC_APP_URL": "http://studio.example.com"}, "PUBLIC_APP_URL"),
        (
            {
                "SUB2API_BASE_URL": "https://sub2api.example.com",
                "SUB2API_API_KEY": "production-upstream-key",
            },
            "business credentials",
        ),
    ],
)
def test_settings_reject_unsafe_production_configuration(
    overrides: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "_env_file": None,
        "APP_ENV": "production",
        "LEGACY_SYNC_API_ENABLED": False,
        "DEPENDENCY_CHECKS_ENABLED": True,
        "SESSION_COOKIE_SECURE": True,
        "AUTH_TOKEN_PEPPER": "token-pepper-0123456789-abcdefghijklmnop",
        "AUTH_HASH_SALT": "hash-salt-0123456789-abcdefghijklmnopq",
        "APP_CONFIG_MASTER_KEY": "6b" * 32,
        "PUBLIC_APP_URL": "https://studio.example.com",
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        Settings(**values)


def test_startup_secrets_are_hidden_from_settings_repr() -> None:
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://user:database-secret@db/app",
        REDIS_URL="redis://:redis-secret@cache/0",
        SUB2API_API_KEY="upstream-secret",
        R2_ENDPOINT_URL="https://account.r2.cloudflarestorage.com",
        R2_ACCESS_KEY_ID="r2-access-secret",
        R2_SECRET_ACCESS_KEY="r2-signing-secret",
        R2_BUCKET="private-assets",
    )

    rendered = repr(settings)
    assert "database-secret" not in rendered
    assert "redis-secret" not in rendered
    assert "upstream-secret" not in rendered
    assert "r2-access-secret" not in rendered
    assert "r2-signing-secret" not in rendered


@pytest.mark.asyncio
async def test_service_heartbeat_uses_postgres_upsert() -> None:
    class CaptureSession:
        statement = None

        async def execute(self, statement) -> None:
            self.statement = statement

    session = CaptureSession()
    await record_heartbeat(
        session,
        service_type="worker",
        instance_name="worker-test",
        version="0.2.0",
    )

    sql = str(session.statement.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT ON CONSTRAINT uq_service_instances_type_name DO UPDATE" in sql


def test_uuid7_has_expected_version_and_variant() -> None:
    value = uuid7()

    assert value.version == 7
    assert value.variant == "specified in RFC 4122"
