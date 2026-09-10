"""Verify email challenge row locks against the migrated disposable PostgreSQL DB."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.errors import ApiError
from app.config import Settings
from app.repositories.models import RegistrationChallenge
from app.services.auth import AuthService
from app.services.registration import RegistrationService

from .test_external_services import required_test_setting

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_postgres_code_attempts_are_not_lost_and_consumption_is_once():
    database_url = required_test_setting("TEST_DATABASE_URL")
    assert (make_url(database_url).database or "").endswith("_test")
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    email = f"registration-{uuid.uuid4().hex}@example.test"
    auth = AuthService(Settings(_env_file=None, APP_ENV="test"))
    service = RegistrationService()
    delivered = []

    async def capture(_runtime, *, email, code):
        delivered.append(code)

    async def verify(code):
        async with factory() as session:
            try:
                await service.verify(session, service=auth, email=email, code=code)
                await session.commit()
                return "accepted"
            except ApiError as exc:
                return exc.code

    try:
        async with factory() as session:
            await service.send_code(
                session,
                service=auth,
                mailer=SimpleNamespace(send=capture),
                runtime=None,
                email=email,
            )
        code = delivered[0]
        wrong = "000000" if code != "000000" else "111111"
        attempts = await asyncio.wait_for(asyncio.gather(verify(wrong), verify(wrong)), 10)
        assert attempts == ["EMAIL_CODE_INVALID", "EMAIL_CODE_INVALID"]
        async with factory() as session:
            assert (await session.get(RegistrationChallenge, email)).attempts == 2
        outcomes = await asyncio.wait_for(asyncio.gather(verify(code), verify(code)), 10)
        assert sorted(outcomes) == ["EMAIL_CODE_EXPIRED", "accepted"]
    finally:
        async with factory() as session:
            await session.execute(
                delete(RegistrationChallenge).where(RegistrationChallenge.email == email)
            )
            await session.commit()
        await engine.dispose()
