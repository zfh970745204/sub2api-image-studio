from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import asc, desc, or_, select
from sqlalchemy.exc import IntegrityError

from app.api.auth import (
    USERNAME_PATTERN,
    request_metadata,
    role_codes,
    user_payload,
    validate_email,
)
from app.api.dependencies import Principal, require_permission
from app.api.errors import ApiError
from app.domain.ids import uuid7
from app.repositories.models import User
from app.services.auth import AuthService
from app.services.points import PointService
from app.services.rbac import active_super_admin_ids

router = APIRouter(prefix="/api/v1/admin/users", tags=["admin-users"])
point_service = PointService()
UsersReader = Annotated[Principal, Depends(require_permission("users.read"))]
UsersManager = Annotated[Principal, Depends(require_permission("users.manage"))]


class CreateUserRequest(BaseModel):
    email: str
    username: str | None = Field(default=None, max_length=64)
    display_name: str | None = Field(default=None, max_length=120)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return validate_email(value)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().casefold()
        if not USERNAME_PATTERN.fullmatch(normalized):
            raise ValueError("用户名只能包含字母、数字、点、下划线和连字符，长度为 3-64")
        return normalized


class UpdateUserRequest(BaseModel):
    email: str | None = None
    username: str | None = Field(default=None, max_length=64)
    display_name: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        return validate_email(value) if value is not None else None

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().casefold()
        if not USERNAME_PATTERN.fullmatch(normalized):
            raise ValueError("用户名只能包含字母、数字、点、下划线和连字符，长度为 3-64")
        return normalized


def auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


async def get_user_or_404(session, user_id: uuid.UUID, *, for_update: bool = False) -> User:
    statement = select(User).where(User.id == user_id, User.deleted_at.is_(None))
    if for_update:
        statement = statement.with_for_update()
    user = (await session.execute(statement)).scalar_one_or_none()
    if user is None:
        raise ApiError(404, "USER_NOT_FOUND", "用户不存在")
    return user


@router.get("")
async def list_users(
    request: Request,
    _principal: UsersReader,
    query: str | None = Query(default=None, max_length=320),
    status_filter: str | None = Query(default=None, alias="status"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    order: Literal["asc", "desc"] = "desc",
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        statement = select(User).where(User.deleted_at.is_(None))
        if query:
            normalized = query.strip().casefold()
            statement = statement.where(
                or_(
                    User.email.contains(normalized),
                    User.username.contains(normalized),
                    User.display_name.contains(query.strip()),
                )
            )
        if status_filter:
            if status_filter not in {"pending", "active", "disabled", "locked"}:
                raise ApiError(422, "VALIDATION_ERROR", "无效的用户状态")
            statement = statement.where(User.status == status_filter)
        direction = desc if order == "desc" else asc
        users = list(
            (
                await session.scalars(
                    statement.order_by(direction(User.created_at), direction(User.id))
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        )
    return {"items": [user_payload(user) for user in users], "offset": offset, "limit": limit}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: CreateUserRequest,
    request: Request,
    principal: UsersManager,
) -> dict[str, Any]:
    service = auth_service(request)
    _, _, request_id = request_metadata(request, service)
    display_name = (
        payload.display_name or payload.username or payload.email.split("@", 1)[0]
    ).strip()
    if not display_name:
        raise ApiError(422, "VALIDATION_ERROR", "显示名称不能为空")
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        user = User(
            id=uuid7(),
            email=payload.email,
            username=payload.username,
            password_hash=None,
            display_name=display_name,
            status="pending",
            session_version=1,
            permission_version=1,
        )
        session.add(user)
        try:
            await session.flush()
            await point_service.ensure_account(session, user.id)
        except IntegrityError as exc:
            await session.rollback()
            raise ApiError(409, "USER_ALREADY_EXISTS", "邮箱或用户名已被使用") from exc
        role = await service.ensure_role(
            session,
            code="user",
            name="普通用户",
            description="已激活账号的基础工作台角色",
        )
        await service.assign_role(
            session, user_id=user.id, role=role, assigned_by=principal.user_id
        )
        raw_token = service.create_reset_token(session, user)
        service.record_audit(
            session,
            action="user.invited",
            aggregate_type="user",
            aggregate_id=user.id,
            actor_user_id=principal.user_id,
            subject_user_id=user.id,
            request_id=request_id,
        )
        await session.commit()
        await session.refresh(user)
    base_url = request.app.state.settings.public_app_url.rstrip("/")
    return {
        "user": user_payload(user),
        "setup_token": raw_token,
        "setup_url": f"{base_url}/reset-password?token={quote(raw_token)}",
    }


@router.get("/{user_id}")
async def get_user(user_id: uuid.UUID, request: Request, _principal: UsersReader) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        user = await get_user_or_404(session, user_id)
        roles = await role_codes(session, user.id)
    return {"user": user_payload(user, roles=roles)}


@router.patch("/{user_id}")
async def update_user(
    user_id: uuid.UUID,
    payload: UpdateUserRequest,
    request: Request,
    principal: UsersManager,
) -> dict[str, Any]:
    service = auth_service(request)
    _, _, request_id = request_metadata(request, service)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        user = await get_user_or_404(session, user_id, for_update=True)
        email_changed = payload.email is not None and payload.email != user.email
        if email_changed:
            user.email = payload.email or user.email
            user.email_verified_at = None
            user.session_version += 1
            await service.revoke_user_sessions(session, user=user, reason="email_changed")
        if "username" in payload.model_fields_set:
            user.username = payload.username
        if payload.display_name is not None:
            user.display_name = payload.display_name.strip()
        service.record_audit(
            session,
            action="user.updated",
            aggregate_type="user",
            aggregate_id=user.id,
            actor_user_id=principal.user_id,
            subject_user_id=user.id,
            request_id=request_id,
            details={"email_changed": email_changed},
        )
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ApiError(409, "USER_ALREADY_EXISTS", "邮箱或用户名已被使用") from exc
        await session.refresh(user)
    return {"user": user_payload(user)}


@router.post("/{user_id}/disable", status_code=status.HTTP_204_NO_CONTENT)
async def disable_user(
    user_id: uuid.UUID,
    request: Request,
    principal: UsersManager,
) -> None:
    if user_id == principal.user_id:
        raise ApiError(409, "CANNOT_DISABLE_SELF", "不能禁用当前登录账号")
    service = auth_service(request)
    _, _, request_id = request_metadata(request, service)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        existing = await get_user_or_404(session, user_id)
        active_super_admins = await active_super_admin_ids(session, lock=True)
        if existing.id in active_super_admins and len(active_super_admins) == 1:
            raise ApiError(409, "LAST_SUPER_ADMIN", "不能禁用最后一个有效超级管理员")
        user = await get_user_or_404(session, user_id, for_update=True)
        if user.status != "disabled":
            user.status = "disabled"
            user.locked_until = None
            user.session_version += 1
            await service.revoke_user_sessions(session, user=user, reason="user_disabled")
        service.record_audit(
            session,
            action="user.disabled",
            aggregate_type="user",
            aggregate_id=user.id,
            actor_user_id=principal.user_id,
            subject_user_id=user.id,
            request_id=request_id,
        )
        await session.commit()


@router.post("/{user_id}/enable")
async def enable_user(
    user_id: uuid.UUID,
    request: Request,
    principal: UsersManager,
) -> dict[str, Any]:
    service = auth_service(request)
    _, _, request_id = request_metadata(request, service)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        user = await get_user_or_404(session, user_id, for_update=True)
        user.status = "active" if user.password_hash else "pending"
        user.locked_until = None
        service.record_audit(
            session,
            action="user.enabled",
            aggregate_type="user",
            aggregate_id=user.id,
            actor_user_id=principal.user_id,
            subject_user_id=user.id,
            request_id=request_id,
            details={"resulting_status": user.status},
        )
        await session.commit()
        await session.refresh(user)
    return {"user": user_payload(user)}


@router.post("/{user_id}/revoke-sessions", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_user_sessions(
    user_id: uuid.UUID,
    request: Request,
    principal: UsersManager,
) -> None:
    service = auth_service(request)
    _, _, request_id = request_metadata(request, service)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        user = await get_user_or_404(session, user_id, for_update=True)
        user.session_version += 1
        await service.revoke_user_sessions(session, user=user, reason="admin_revoked")
        service.record_audit(
            session,
            action="sessions.admin_revoked",
            aggregate_type="user",
            aggregate_id=user.id,
            actor_user_id=principal.user_id,
            subject_user_id=user.id,
            request_id=request_id,
        )
        await session.commit()
