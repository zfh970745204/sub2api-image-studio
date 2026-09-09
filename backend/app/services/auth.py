from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError
from app.config import Settings
from app.domain.ids import uuid7
from app.repositories.models import (
    AuthSession,
    LoginAttempt,
    OutboxEvent,
    PasswordResetToken,
    Role,
    User,
    UserRole,
)
from app.services.rbac import permission_codes_for_user

LOGIN_FAILED_MESSAGE = "登录失败，请检查凭据或稍后重试"


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class AuthService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.password_hasher = PasswordHasher(
            time_cost=3,
            memory_cost=65536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._dummy_password_hash = self.password_hasher.hash(secrets.token_urlsafe(32))

    @staticmethod
    def normalize_identifier(value: str) -> str:
        return value.strip().casefold()

    def hash_password(self, password: str) -> str:
        return self.password_hasher.hash(password)

    def verify_password(self, password_hash: str | None, password: str) -> bool:
        candidate = password_hash or self._dummy_password_hash
        try:
            return (
                bool(self.password_hasher.verify(candidate, password)) and password_hash is not None
            )
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False

    def token_hash(self, token: str) -> str:
        return hmac.new(
            self.settings.auth_token_pepper.encode(), token.encode(), hashlib.sha256
        ).hexdigest()

    def fingerprint(self, value: str) -> str:
        return hmac.new(
            self.settings.auth_hash_salt.encode(), value.encode(), hashlib.sha256
        ).hexdigest()

    @staticmethod
    def new_token() -> str:
        return secrets.token_urlsafe(32)

    async def login(
        self,
        session: AsyncSession,
        *,
        identifier: str,
        password: str,
        ip_address: str,
        user_agent: str,
        request_id: str,
    ) -> tuple[User, AuthSession, str]:
        now = utcnow()
        normalized = self.normalize_identifier(identifier)
        account_fingerprint = self.fingerprint(normalized)
        ip_hash = self.fingerprint(ip_address)
        user = await self.find_user(session, normalized, for_update=True)

        account_failures = await self._consecutive_failures(
            session, LoginAttempt.account_fingerprint, account_fingerprint, now
        )
        ip_failures = await self._consecutive_failures(
            session, LoginAttempt.ip_hash, ip_hash, now, reset_on_success=False
        )
        if (
            account_failures >= self.settings.login_account_failure_limit
            or ip_failures >= self.settings.login_ip_failure_limit
        ):
            if (
                user is not None
                and user.status in {"active", "locked"}
                and (
                    user.status == "active"
                    or user.locked_until is None
                    or as_utc(user.locked_until) <= now
                )
            ):
                user.status = "locked"
                user.locked_until = now + timedelta(minutes=self.settings.login_lock_minutes)
            self._record_attempt(
                session,
                account_fingerprint=account_fingerprint,
                ip_hash=ip_hash,
                success=False,
                failure_code="rate_limited",
                user=user,
                request_id=request_id,
            )
            await session.commit()
            raise ApiError(429, "LOGIN_RATE_LIMITED", LOGIN_FAILED_MESSAGE)

        if (
            user is not None
            and user.status == "locked"
            and user.locked_until is not None
            and as_utc(user.locked_until) <= now
        ):
            user.status = "active"
            user.locked_until = None

        active_user = user is not None and user.status == "active" and user.deleted_at is None
        password_valid = self.verify_password(
            user.password_hash if active_user and user is not None else None,
            password,
        )
        if not active_user or not password_valid:
            failure_code = "invalid_credentials"
            self._record_attempt(
                session,
                account_fingerprint=account_fingerprint,
                ip_hash=ip_hash,
                success=False,
                failure_code=failure_code,
                user=user,
                request_id=request_id,
            )
            if (
                user is not None
                and user.status == "active"
                and account_failures + 1 >= self.settings.login_account_failure_limit
            ):
                user.status = "locked"
                user.locked_until = now + timedelta(minutes=self.settings.login_lock_minutes)
            await session.commit()
            raise ApiError(401, "INVALID_CREDENTIALS", LOGIN_FAILED_MESSAGE)

        assert user is not None
        if user.password_hash and self.password_hasher.check_needs_rehash(user.password_hash):
            user.password_hash = self.hash_password(password)
        user.last_login_at = now
        auth_token = self.new_token()
        auth_session = AuthSession(
            id=uuid7(),
            user_id=user.id,
            token_hash=self.token_hash(auth_token),
            session_version=user.session_version,
            ip_hash=ip_hash,
            user_agent=user_agent[:500],
            expires_at=now + timedelta(days=self.settings.auth_session_ttl_days),
            last_seen_at=now,
            created_at=now,
        )
        session.add(auth_session)
        attempt = LoginAttempt(
            id=uuid7(),
            account_fingerprint=account_fingerprint,
            ip_hash=ip_hash,
            success=True,
            created_at=now,
        )
        session.add(attempt)
        self.record_audit(
            session,
            action="login.succeeded",
            aggregate_type="user",
            aggregate_id=user.id,
            actor_user_id=user.id,
            subject_user_id=user.id,
            request_id=request_id,
            details={"session_id": str(auth_session.id), "ip_hash": ip_hash},
        )
        await session.commit()
        return user, auth_session, auth_token

    async def authenticate_session(
        self, session: AsyncSession, raw_token: str
    ) -> tuple[User, AuthSession, frozenset[str]] | None:
        now = utcnow()
        statement = (
            select(User, AuthSession)
            .join(AuthSession, AuthSession.user_id == User.id)
            .where(
                AuthSession.token_hash == self.token_hash(raw_token),
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
                User.deleted_at.is_(None),
            )
        )
        row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        user, auth_session = row
        if user.status == "locked" and user.locked_until is not None:
            if as_utc(user.locked_until) <= now:
                user.status = "active"
                user.locked_until = None
            else:
                return None
        if user.status != "active" or auth_session.session_version != user.session_version:
            auth_session.revoked_at = now
            auth_session.revoke_reason = "account_state_changed"
            await session.commit()
            return None
        if (now - as_utc(auth_session.last_seen_at)).total_seconds() >= (
            self.settings.auth_session_last_seen_seconds
        ):
            auth_session.last_seen_at = now
        permissions = await self.permissions_for_user(session, user.id, now=now)
        await session.commit()
        return user, auth_session, permissions

    async def find_user(
        self, session: AsyncSession, identifier: str, *, for_update: bool = False
    ) -> User | None:
        normalized = self.normalize_identifier(identifier)
        statement = select(User).where(
            User.deleted_at.is_(None),
            or_(User.email == normalized, User.username == normalized),
        )
        if for_update:
            statement = statement.with_for_update()
        return (await session.execute(statement)).scalar_one_or_none()

    async def permissions_for_user(
        self, session: AsyncSession, user_id: uuid.UUID, *, now: datetime | None = None
    ) -> frozenset[str]:
        return await permission_codes_for_user(session, user_id, now=now)

    async def ensure_role(
        self,
        session: AsyncSession,
        *,
        code: str,
        name: str,
        description: str,
    ) -> Role:
        role = (await session.execute(select(Role).where(Role.code == code))).scalar_one_or_none()
        if role is None:
            role = Role(
                id=uuid7(),
                code=code,
                name=name,
                description=description,
                is_system=True,
            )
            session.add(role)
            await session.flush()
        return role

    async def assign_role(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        role: Role,
        assigned_by: uuid.UUID,
    ) -> None:
        existing = await session.get(UserRole, (user_id, role.id))
        if existing is None:
            session.add(UserRole(user_id=user_id, role_id=role.id, assigned_by=assigned_by))

    def create_reset_token(self, session: AsyncSession, user: User) -> str:
        now = utcnow()
        raw_token = self.new_token()
        session.add(
            PasswordResetToken(
                id=uuid7(),
                user_id=user.id,
                token_hash=self.token_hash(raw_token),
                expires_at=now + timedelta(minutes=self.settings.password_reset_ttl_minutes),
                created_at=now,
            )
        )
        return raw_token

    async def revoke_user_sessions(
        self,
        session: AsyncSession,
        *,
        user: User,
        reason: str,
        except_session_id: uuid.UUID | None = None,
    ) -> None:
        statement = update(AuthSession).where(
            AuthSession.user_id == user.id,
            AuthSession.revoked_at.is_(None),
        )
        if except_session_id is not None:
            statement = statement.where(AuthSession.id != except_session_id)
        await session.execute(statement.values(revoked_at=utcnow(), revoke_reason=reason))

    async def _consecutive_failures(
        self,
        session: AsyncSession,
        dimension,
        fingerprint: str,
        now: datetime,
        *,
        reset_on_success: bool = True,
    ) -> int:
        window_start = now - timedelta(minutes=self.settings.login_attempt_window_minutes)
        if reset_on_success:
            last_success = await session.scalar(
                select(func.max(LoginAttempt.created_at)).where(
                    dimension == fingerprint,
                    LoginAttempt.success.is_(True),
                    LoginAttempt.created_at >= window_start,
                )
            )
            if last_success is not None:
                window_start = max(window_start, as_utc(last_success))
        count = await session.scalar(
            select(func.count(LoginAttempt.id)).where(
                dimension == fingerprint,
                LoginAttempt.success.is_(False),
                LoginAttempt.created_at >= window_start,
            )
        )
        return int(count or 0)

    def _record_attempt(
        self,
        session: AsyncSession,
        *,
        account_fingerprint: str,
        ip_hash: str,
        success: bool,
        failure_code: str | None,
        user: User | None,
        request_id: str,
    ) -> None:
        attempt_id = uuid7()
        session.add(
            LoginAttempt(
                id=attempt_id,
                account_fingerprint=account_fingerprint,
                ip_hash=ip_hash,
                success=success,
                failure_code=failure_code,
                created_at=utcnow(),
            )
        )
        self.record_audit(
            session,
            action="login.failed",
            aggregate_type="user" if user is not None else "login_attempt",
            aggregate_id=user.id if user is not None else attempt_id,
            actor_user_id=None,
            subject_user_id=user.id if user is not None else None,
            request_id=request_id,
            details={
                "account_fingerprint": account_fingerprint,
                "ip_hash": ip_hash,
                "failure_code": failure_code,
            },
        )

    @staticmethod
    def record_audit(
        session: AsyncSession,
        *,
        action: str,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
        subject_user_id: uuid.UUID | None,
        request_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "action": action,
            "actor_user_id": str(actor_user_id) if actor_user_id else None,
            "subject_user_id": str(subject_user_id) if subject_user_id else None,
            "request_id": request_id,
            "details": details or {},
        }
        session.add(
            OutboxEvent(
                id=uuid7(),
                topic=f"identity.{action}",
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                payload=payload,
                status="pending",
                attempts=0,
                available_at=utcnow(),
                version=1,
            )
        )
