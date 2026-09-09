from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import Principal, get_current_principal
from app.api.errors import ApiError
from app.repositories.models import (
    AuthSession,
    LoginAttempt,
    PasswordResetToken,
    Role,
    User,
    UserRole,
)
from app.services.auth import AuthService, as_utc, utcnow
from app.services.configuration import runtime_config_value
from app.services.memberships import EntitlementService
from app.services.points import PointService

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])
membership_service = EntitlementService()
point_service = PointService()
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=320)
    password: SecretStr

    @field_validator("password")
    @classmethod
    def limit_password(cls, value: SecretStr) -> SecretStr:
        if not 1 <= len(value.get_secret_value()) <= 128:
            raise ValueError("密码长度必须为 1-128 个字符")
        return value


class ProfileUpdateRequest(BaseModel):
    username: str | None = Field(default=None, max_length=64)
    display_name: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().casefold()
        if not USERNAME_PATTERN.fullmatch(normalized):
            raise ValueError("用户名只能包含字母、数字、点、下划线和连字符，长度为 3-64")
        return normalized


class PasswordChangeRequest(BaseModel):
    current_password: SecretStr
    new_password: SecretStr

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: SecretStr) -> SecretStr:
        validate_password(value.get_secret_value())
        return value


class ForgotPasswordRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=320)


class ResetPasswordRequest(BaseModel):
    token: SecretStr
    new_password: SecretStr

    @field_validator("token")
    @classmethod
    def limit_token(cls, value: SecretStr) -> SecretStr:
        if not 20 <= len(value.get_secret_value()) <= 512:
            raise ValueError("重置链接无效")
        return value

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: SecretStr) -> SecretStr:
        validate_password(value.get_secret_value())
        return value


def validate_email(value: str) -> str:
    normalized = value.strip().casefold()
    if len(normalized) > 320 or not EMAIL_PATTERN.fullmatch(normalized):
        raise ValueError("请输入有效的邮箱地址")
    return normalized


def validate_password(value: str) -> None:
    if len(value) < 12 or len(value) > 128:
        raise ValueError("密码长度必须为 12-128 个字符")
    if value.isspace():
        raise ValueError("密码不能只包含空白字符")


def auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


def request_metadata(request: Request, service: AuthService) -> tuple[str, str, str]:
    ip_address = request.client.host if request.client else "unknown"
    ip_hash = service.fingerprint(ip_address)
    request_id = getattr(request.state, "request_id", "unknown")
    return ip_address, ip_hash, request_id


def user_payload(user: User, *, roles: list[str] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(user.id),
        "email": user.email,
        "username": user.username,
        "display_name": user.display_name,
        "avatar_asset_id": str(user.avatar_asset_id) if user.avatar_asset_id else None,
        "status": user.status,
        "email_verified_at": user.email_verified_at,
        "last_login_at": user.last_login_at,
        "created_at": user.created_at,
    }
    if roles is not None:
        payload["roles"] = roles
    return payload


async def role_codes(session, user_id: uuid.UUID) -> list[str]:
    now = utcnow()
    statement = (
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(
            UserRole.user_id == user_id,
            (UserRole.expires_at.is_(None) | (UserRole.expires_at > now)),
        )
        .order_by(Role.code)
    )
    return list((await session.scalars(statement)).all())


def set_session_cookie(response: Response, request: Request, raw_token: str) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        key=settings.session_cookie_name,
        value=raw_token,
        max_age=settings.auth_session_ttl_days * 86400,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    settings = request.app.state.settings
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


@router.post("/login")
async def login(payload: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    service = auth_service(request)
    security_service = getattr(request.app.state, "security_service", None)
    if security_service is not None:
        await security_service.enforce_login(request)
    ip_address, _, request_id = request_metadata(request, service)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        user, _, raw_token = await service.login(
            session,
            identifier=payload.identifier,
            password=payload.password.get_secret_value(),
            ip_address=ip_address,
            user_agent=request.headers.get("user-agent", "unknown"),
            request_id=request_id,
        )
    set_session_cookie(response, request, raw_token)
    return {"user": user_payload(user)}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Response:
    service = auth_service(request)
    _, ip_hash, request_id = request_metadata(request, service)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        auth_session = await session.get(AuthSession, principal.session_id)
        if auth_session is not None and auth_session.revoked_at is None:
            auth_session.revoked_at = utcnow()
            auth_session.revoke_reason = "logout"
            service.record_audit(
                session,
                action="logout",
                aggregate_type="auth_session",
                aggregate_id=auth_session.id,
                actor_user_id=principal.user_id,
                subject_user_id=principal.user_id,
                request_id=request_id,
                details={"ip_hash": ip_hash},
            )
            await session.commit()
    clear_session_cookie(response, request)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Response:
    service = auth_service(request)
    _, _, request_id = request_metadata(request, service)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        user = await session.get(User, principal.user_id, with_for_update=True)
        if user is None:
            raise ApiError(401, "AUTHENTICATION_REQUIRED", "登录状态已失效，请重新登录")
        user.session_version += 1
        await service.revoke_user_sessions(session, user=user, reason="logout_all")
        service.record_audit(
            session,
            action="sessions.revoked_all",
            aggregate_type="user",
            aggregate_id=user.id,
            actor_user_id=user.id,
            subject_user_id=user.id,
            request_id=request_id,
        )
        await session.commit()
    clear_session_cookie(response, request)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me")
async def me(
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        user = await session.get(User, principal.user_id)
        if user is None:
            raise ApiError(401, "AUTHENTICATION_REQUIRED", "登录状态已失效，请重新登录")
        roles = await role_codes(session, user.id)
    return {
        "user": user_payload(user, roles=roles),
        "permissions": sorted(principal.permissions),
    }


@router.patch("/me")
async def update_me(
    payload: ProfileUpdateRequest,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict[str, Any]:
    service = auth_service(request)
    _, _, request_id = request_metadata(request, service)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        user = await session.get(User, principal.user_id, with_for_update=True)
        if user is None:
            raise ApiError(401, "AUTHENTICATION_REQUIRED", "登录状态已失效，请重新登录")
        if "username" in payload.model_fields_set:
            user.username = payload.username
        if payload.display_name is not None:
            user.display_name = payload.display_name.strip()
        service.record_audit(
            session,
            action="profile.updated",
            aggregate_type="user",
            aggregate_id=user.id,
            actor_user_id=user.id,
            subject_user_id=user.id,
            request_id=request_id,
        )
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ApiError(409, "USERNAME_ALREADY_EXISTS", "用户名已被使用") from exc
        await session.refresh(user)
    return {"user": user_payload(user)}


@router.post("/password/change", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Response:
    service = auth_service(request)
    _, _, request_id = request_metadata(request, service)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        user = await session.get(User, principal.user_id, with_for_update=True)
        current_session = await session.get(AuthSession, principal.session_id, with_for_update=True)
        if user is None or current_session is None:
            raise ApiError(401, "AUTHENTICATION_REQUIRED", "登录状态已失效，请重新登录")
        current_password = payload.current_password.get_secret_value()
        new_password = payload.new_password.get_secret_value()
        if not service.verify_password(user.password_hash, current_password):
            raise ApiError(400, "INVALID_CURRENT_PASSWORD", "当前密码不正确")
        if service.verify_password(user.password_hash, new_password):
            raise ApiError(400, "PASSWORD_UNCHANGED", "新密码不能与当前密码相同")
        user.password_hash = service.hash_password(new_password)
        user.session_version += 1
        current_session.session_version = user.session_version
        await service.revoke_user_sessions(
            session,
            user=user,
            reason="password_changed",
            except_session_id=current_session.id,
        )
        service.record_audit(
            session,
            action="password.changed",
            aggregate_type="user",
            aggregate_id=user.id,
            actor_user_id=user.id,
            subject_user_id=user.id,
            request_id=request_id,
            details={"retained_session_id": str(current_session.id)},
        )
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/password/forgot", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(payload: ForgotPasswordRequest, request: Request) -> dict[str, str]:
    enabled = await runtime_config_value(
        request.app.state.runtime_services,
        "general",
        "password_reset_enabled",
        request.app.state.settings.password_reset_enabled,
    )
    _ = (payload.identifier, enabled)
    return {"status": "accepted"}


@router.post("/password/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(payload: ResetPasswordRequest, request: Request) -> Response:
    service = auth_service(request)
    _, ip_hash, request_id = request_metadata(request, service)
    now = utcnow()
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        statement = (
            select(PasswordResetToken)
            .where(
                PasswordResetToken.token_hash
                == service.token_hash(payload.token.get_secret_value())
            )
            .with_for_update()
        )
        reset_token = (await session.execute(statement)).scalar_one_or_none()
        if (
            reset_token is None
            or reset_token.consumed_at is not None
            or as_utc(reset_token.expires_at) <= now
        ):
            raise ApiError(400, "INVALID_RESET_TOKEN", "重置链接无效或已过期")
        user = await session.get(User, reset_token.user_id, with_for_update=True)
        if user is None or user.status == "disabled" or user.deleted_at is not None:
            raise ApiError(400, "INVALID_RESET_TOKEN", "重置链接无效或已过期")
        user.password_hash = service.hash_password(payload.new_password.get_secret_value())
        user.status = "active"
        user.locked_until = None
        user.session_version += 1
        await membership_service.ensure_default_membership(
            session,
            user.id,
            now=now,
            request_id=request_id,
        )
        onboarding_points = int(
            await runtime_config_value(
                request.app.state.runtime_services,
                "general",
                "default_points",
                request.app.state.settings.onboarding_points,
            )
        )
        await point_service.ensure_onboarding_grant(
            session,
            user.id,
            points=onboarding_points,
            request_id=request_id,
        )
        await session.execute(
            update(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )
        await service.revoke_user_sessions(session, user=user, reason="password_reset")
        service.record_audit(
            session,
            action="password.reset",
            aggregate_type="user",
            aggregate_id=user.id,
            actor_user_id=None,
            subject_user_id=user.id,
            request_id=request_id,
            details={"ip_hash": ip_hash, "reset_token_id": str(reset_token.id)},
        )
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions")
async def list_sessions(
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict[str, list[dict[str, Any]]]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        user = await session.get(User, principal.user_id)
        if user is None:
            raise ApiError(401, "AUTHENTICATION_REQUIRED", "登录状态已失效，请重新登录")
        statement = (
            select(AuthSession)
            .where(
                AuthSession.user_id == principal.user_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > datetime.now(UTC),
            )
            .order_by(AuthSession.last_seen_at.desc())
        )
        sessions = list((await session.scalars(statement)).all())
        service = auth_service(request)
        identifiers = {user.email.casefold()}
        if user.username:
            identifiers.add(user.username.casefold())
        fingerprints = [service.fingerprint(value) for value in identifiers]
        attempts_statement = (
            select(LoginAttempt)
            .where(
                LoginAttempt.account_fingerprint.in_(fingerprints),
                LoginAttempt.success.is_(False),
            )
            .order_by(LoginAttempt.created_at.desc())
            .limit(20)
        )
        failed_attempts = list((await session.scalars(attempts_statement)).all())
    return {
        "items": [
            {
                "id": str(item.id),
                "user_agent": item.user_agent,
                "last_seen_at": item.last_seen_at,
                "created_at": item.created_at,
                "expires_at": item.expires_at,
                "current": item.id == principal.session_id,
            }
            for item in sessions
        ],
        "recent_failed_logins": [
            {"failure_code": item.failure_code, "created_at": item.created_at}
            for item in failed_attempts
        ],
    }


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Response:
    service = auth_service(request)
    _, _, request_id = request_metadata(request, service)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        target = await session.get(AuthSession, session_id, with_for_update=True)
        if target is None or target.user_id != principal.user_id or target.revoked_at is not None:
            raise ApiError(404, "SESSION_NOT_FOUND", "会话不存在")
        target.revoked_at = utcnow()
        target.revoke_reason = "user_revoked"
        service.record_audit(
            session,
            action="session.revoked",
            aggregate_type="auth_session",
            aggregate_id=target.id,
            actor_user_id=principal.user_id,
            subject_user_id=principal.user_id,
            request_id=request_id,
        )
        await session.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    if target.id == principal.session_id:
        clear_session_cookie(response, request)
    return response
