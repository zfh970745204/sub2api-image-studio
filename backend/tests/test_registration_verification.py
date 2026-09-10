from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import func, select
from test_auth import auth_context as auth_fixture
from test_auth import client_for
from test_registration import PAYLOAD, Inbox, verified_payload

from app.api.errors import ApiError
from app.config import Settings
from app.repositories.models import PointAccount, RegistrationChallenge, User
from app.services import registration
from app.services.configuration import ConfigConnectionTester
from app.services.registration import RegistrationMailer
from app.services.security import SecurityService

auth_context = auth_fixture


@pytest.mark.asyncio
async def test_code_is_email_bound_hashed_and_required_before_account_creation(auth_context):
    async with client_for(auth_context) as client:
        payload = await verified_payload(auth_context, client)
        async with auth_context.database.session_factory() as session:
            challenge = await session.get(RegistrationChallenge, PAYLOAD["email"])
            assert challenge.code_hash != payload["verification_code"]
            assert len(challenge.code_hash) == 64
            assert await session.scalar(select(func.count(User.id))) == 0
            assert await session.scalar(select(func.count(PointAccount.user_id))) == 0
        for code in (None, "123", "abcdef"):
            invalid = {**payload, "verification_code": code}
            if code is None:
                del invalid["verification_code"]
            response = await client.post("/api/v1/auth/register", json=invalid)
            assert response.status_code == 422
        another = await client.post(
            "/api/v1/auth/register", json={**payload, "email": "other@example.test"}
        )
        assert another.status_code == 400
        assert (await client.post("/api/v1/auth/register", json=payload)).status_code == 201
        async with auth_context.database.session_factory() as session:
            assert (
                await session.get(RegistrationChallenge, PAYLOAD["email"])
            ).consumed_at is not None
        assert (await client.post("/api/v1/auth/register", json=payload)).status_code == 409
        assert (
            await client.post("/api/v1/auth/register/email-code", json={"email": PAYLOAD["email"]})
        ).status_code == 409


@pytest.mark.asyncio
async def test_wrong_attempts_lock_code_without_creating_account(auth_context):
    async with client_for(auth_context) as client:
        payload = await verified_payload(auth_context, client)
        wrong = "000000" if payload["verification_code"] != "000000" else "111111"
        for _ in range(5):
            response = await client.post(
                "/api/v1/auth/register", json={**payload, "verification_code": wrong}
            )
            assert response.json()["code"] == "EMAIL_CODE_INVALID"
        locked = await client.post("/api/v1/auth/register", json=payload)
        assert locked.json()["code"] == "EMAIL_CODE_LOCKED"
    async with auth_context.database.session_factory() as session:
        assert (await session.get(RegistrationChallenge, PAYLOAD["email"])).attempts == 5
        assert await session.scalar(select(func.count(User.id))) == 0


@pytest.mark.asyncio
async def test_resend_cooldown_replaces_old_code_and_enforces_hourly_limit(
    auth_context, monkeypatch
):
    instant = registration.utcnow()
    monkeypatch.setattr(registration, "utcnow", lambda: instant)
    codes = iter([111111, 222222, 333333, 444444, 555555, 666666])
    monkeypatch.setattr(registration.secrets, "randbelow", lambda _: next(codes))
    async with client_for(auth_context) as client:
        payload = await verified_payload(auth_context, client)
        send = lambda: client.post(
            "/api/v1/auth/register/email-code", json={"email": PAYLOAD["email"]}
        )
        cooldown = await send()
        assert cooldown.status_code == 429 and cooldown.headers["retry-after"] == "60"
        instant += timedelta(seconds=61)
        assert (await send()).status_code == 202
        assert (await client.post("/api/v1/auth/register", json=payload)).json()[
            "code"
        ] == "EMAIL_CODE_INVALID"
        for _ in range(3):
            instant += timedelta(seconds=61)
            assert (await send()).status_code == 202
        instant += timedelta(seconds=61)
        assert (await send()).json()["code"] == "CODE_SEND_LIMIT"
        instant += timedelta(hours=1)
        assert (await send()).status_code == 202
        valid = {
            **payload,
            "verification_code": auth_context.app.state.registration_mailer.codes[PAYLOAD["email"]],
        }
        assert (await client.post("/api/v1/auth/register", json=valid)).status_code == 201


@pytest.mark.asyncio
async def test_expiry_and_delivery_failure_preserve_prior_challenge(auth_context, monkeypatch):
    instant = registration.utcnow()
    monkeypatch.setattr(registration, "utcnow", lambda: instant)
    async with client_for(auth_context) as client:
        payload = await verified_payload(auth_context, client)
        instant += timedelta(seconds=61)
        auth_context.app.state.registration_mailer = SimpleNamespace(
            send=AsyncMock(side_effect=ApiError(503, "EMAIL_DELIVERY_FAILED", "发送失败"))
        )
        failed = await client.post(
            "/api/v1/auth/register/email-code", json={"email": PAYLOAD["email"]}
        )
        assert failed.status_code == 503
        async with auth_context.database.session_factory() as session:
            challenge = await session.get(RegistrationChallenge, PAYLOAD["email"])
            assert challenge.send_count == 1
            assert challenge.code_hash == auth_context.service.token_hash(
                f"signup:{PAYLOAD['email']}:{challenge.nonce}:{payload['verification_code']}"
            )
        instant += timedelta(seconds=540)
        assert (await client.post("/api/v1/auth/register", json=payload)).json()[
            "code"
        ] == "EMAIL_CODE_EXPIRED"


@pytest.mark.asyncio
async def test_missing_mail_config_does_not_issue_code_or_user(auth_context):
    async with client_for(auth_context) as client:
        response = await client.post(
            "/api/v1/auth/register/email-code", json={"email": PAYLOAD["email"]}
        )
        assert response.status_code == 503
        assert response.json()["code"] == "EMAIL_NOT_CONFIGURED"
    async with auth_context.database.session_factory() as session:
        assert await session.scalar(select(func.count(RegistrationChallenge.email))) == 0
        assert await session.scalar(select(func.count(User.id))) == 0


@pytest.mark.asyncio
async def test_code_send_rate_limit_applies_across_emails(auth_context):
    auth_context.app.state.registration_mailer = Inbox()
    auth_context.app.state.security_service = SecurityService(auth_context.app.state.settings)
    async with client_for(auth_context) as client:
        for i in range(10):
            assert (
                await client.post(
                    "/api/v1/auth/register/email-code", json={"email": f"user{i}@example.test"}
                )
            ).status_code == 202
        response = await client.post(
            "/api/v1/auth/register/email-code", json={"email": "blocked@example.test"}
        )
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) > 0


def email_config(**values):
    return SimpleNamespace(
        group="email",
        values={
            "enabled": True,
            "provider": "smtp",
            "host": "smtp.example.test",
            "port": 465,
            "use_tls": True,
            "username": "test-sender",
            "from_email": "sender@example.test",
            "api_base_url": "https://mail.example.test/emails",
            **values,
        },
        secrets={"password": "dummy-password", "api_key": "dummy-api-key"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 405, 401])
async def test_api_connection_check_distinguishes_reachability_from_delivery(monkeypatch, status):
    real_client = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status)

    monkeypatch.setattr(
        registration.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    outcome = await ConfigConnectionTester(Settings(_env_file=None)).test(
        email_config(provider="api")
    )
    assert outcome.succeeded == (status != 401)
    if status != 401:
        assert outcome.code == "EMAIL_API_REACHABLE"
        assert "未发送邮件" in outcome.message
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("port,tls", [(465, True), (587, True), (2525, False)])
async def test_smtp_sender_and_connection_tester_use_same_tls_mode(monkeypatch, port, tls):
    smtp, smtp_ssl = MagicMock(), MagicMock()
    for constructor in (smtp, smtp_ssl):
        constructor.return_value.__enter__.return_value.send_message.return_value = {}
    monkeypatch.setattr(registration.smtplib, "SMTP", smtp)
    monkeypatch.setattr(registration.smtplib, "SMTP_SSL", smtp_ssl)
    config = email_config(port=port, use_tls=tls)
    runtime = SimpleNamespace(config_cache=SimpleNamespace(get=AsyncMock(return_value=config)))
    await RegistrationMailer().send(runtime, email="new@example.test", code="123456")
    await ConfigConnectionTester(Settings(_env_file=None))._test_email(config)
    selected, unused = (smtp_ssl, smtp) if port == 465 else (smtp, smtp_ssl)
    assert selected.call_count == 2
    unused.assert_not_called()
    client = selected.return_value.__enter__.return_value
    assert client.starttls.call_count == (2 if port != 465 and tls else 0)
    client.login.assert_called_with("test-sender", "dummy-password")
    assert client.send_message.call_count == 1  # Connection tests never email anyone.
    message = client.send_message.call_args.args[0]
    assert message["To"] == "new@example.test"
    assert "123456" in message.get_content()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [202, 401, 500])
async def test_api_sender_contract_and_sanitized_delivery_errors(monkeypatch, status):
    config = email_config(provider="api")
    runtime = SimpleNamespace(config_cache=SimpleNamespace(get=AsyncMock(return_value=config)))
    real_client = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, text="secret provider detail and dummy-api-key")

    monkeypatch.setattr(
        registration.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    if status == 202:
        await RegistrationMailer().send(runtime, email="new@example.test", code="123456")
    else:
        with pytest.raises(ApiError) as caught:
            await RegistrationMailer().send(runtime, email="new@example.test", code="123456")
        assert caught.value.code == "EMAIL_DELIVERY_FAILED"
        assert "dummy-api-key" not in caught.value.message
        assert "123456" not in caught.value.message
    import json

    request = requests[0]
    assert request.method == "POST" and str(request.url) == config.values["api_base_url"]
    assert request.headers["Authorization"] == "Bearer dummy-api-key"
    body = json.loads(request.content)
    assert body["from"] == "sender@example.test" and body["to"] == ["new@example.test"]
    assert "123456" in body["text"]
