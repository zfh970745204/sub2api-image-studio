from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import request_metadata
from app.api.dependencies import Principal, require_permission
from app.api.errors import ApiError
from app.domain.ids import uuid7
from app.repositories.models import Permission, Role, RolePermission, User, UserRole
from app.services.auth import AuthService
from app.services.rbac import active_super_admin_ids, permission_codes_for_user

router = APIRouter(prefix="/api/v1/admin", tags=["rbac"])
RolesReader = Annotated[Principal, Depends(require_permission("roles.read"))]
RolesManager = Annotated[Principal, Depends(require_permission("roles.manage"))]
ROLE_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


class CreateRoleRequest(BaseModel):
    code: str = Field(min_length=3, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    permission_codes: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not ROLE_CODE_PATTERN.fullmatch(normalized):
            raise ValueError("角色代码必须以字母开头，只能包含小写字母、数字和下划线")
        return normalized

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("角色名称不能为空")
        return normalized

    @field_validator("permission_codes")
    @classmethod
    def validate_permissions(cls, value: list[str]) -> list[str]:
        return validate_permission_codes(value)


class UpdateRoleRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("角色名称不能为空")
        return normalized


class ReplaceRolePermissionsRequest(BaseModel):
    permission_codes: list[str] = Field(max_length=100)

    @field_validator("permission_codes")
    @classmethod
    def validate_permissions(cls, value: list[str]) -> list[str]:
        return validate_permission_codes(value)


class UserRoleAssignment(BaseModel):
    role_id: uuid.UUID
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def require_future_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("角色过期时间必须包含时区")
        normalized = value.astimezone(UTC)
        if normalized <= datetime.now(UTC):
            raise ValueError("角色过期时间必须晚于当前时间")
        return normalized


class ReplaceUserRolesRequest(BaseModel):
    roles: list[UserRoleAssignment] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_roles(self) -> ReplaceUserRolesRequest:
        role_ids = [assignment.role_id for assignment in self.roles]
        if len(role_ids) != len(set(role_ids)):
            raise ValueError("角色不能重复")
        return self


def auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


def validate_permission_codes(values: list[str]) -> list[str]:
    normalized = [value.strip() for value in values]
    if any(not value or len(value) > 100 for value in normalized):
        raise ValueError("权限代码长度必须为 1-100 个字符")
    return normalized


async def get_role_or_404(
    session: AsyncSession, role_id: uuid.UUID, *, for_update: bool = False
) -> Role:
    statement = select(Role).where(Role.id == role_id)
    if for_update:
        statement = statement.with_for_update()
    role = (await session.execute(statement)).scalar_one_or_none()
    if role is None:
        raise ApiError(404, "ROLE_NOT_FOUND", "角色不存在")
    return role


async def get_user_or_404(
    session: AsyncSession, user_id: uuid.UUID, *, for_update: bool = False
) -> User:
    statement = select(User).where(User.id == user_id, User.deleted_at.is_(None))
    if for_update:
        statement = statement.with_for_update()
    user = (await session.execute(statement)).scalar_one_or_none()
    if user is None:
        raise ApiError(404, "USER_NOT_FOUND", "用户不存在")
    return user


async def resolve_permissions(
    session: AsyncSession, permission_codes: list[str]
) -> list[Permission]:
    requested = set(permission_codes)
    if len(requested) != len(permission_codes):
        raise ApiError(422, "DUPLICATE_PERMISSION", "权限点不能重复")
    if not requested:
        return []
    permissions = list(
        (await session.scalars(select(Permission).where(Permission.code.in_(requested)))).all()
    )
    found = {permission.code for permission in permissions}
    if found != requested:
        raise ApiError(
            422,
            "UNKNOWN_PERMISSION",
            "包含未定义的权限点",
            details={"codes": sorted(requested - found)},
        )
    return permissions


async def permission_map(
    session: AsyncSession, role_ids: set[uuid.UUID] | None = None
) -> dict[uuid.UUID, list[str]]:
    statement = (
        select(RolePermission.role_id, Permission.code)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .order_by(Permission.code)
    )
    if role_ids is not None:
        statement = statement.where(RolePermission.role_id.in_(role_ids))
    result: dict[uuid.UUID, list[str]] = {}
    for role_id, permission_code in (await session.execute(statement)).all():
        result.setdefault(role_id, []).append(permission_code)
    return result


def role_payload(role: Role, permissions: list[str]) -> dict[str, Any]:
    return {
        "id": str(role.id),
        "code": role.code,
        "name": role.name,
        "description": role.description,
        "is_system": role.is_system,
        "permissions": permissions,
        "created_at": role.created_at,
        "updated_at": role.updated_at,
    }


async def bump_role_users(session: AsyncSession, role_id: uuid.UUID) -> None:
    assigned_users = select(UserRole.user_id).where(UserRole.role_id == role_id)
    await session.execute(
        update(User)
        .where(User.id.in_(assigned_users))
        .values(permission_version=User.permission_version + 1)
    )


@router.get("/permissions")
async def list_permissions(request: Request, _principal: RolesReader) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        permissions = list(
            (
                await session.scalars(
                    select(Permission).order_by(Permission.module, Permission.code)
                )
            ).all()
        )
    return {
        "items": [
            {
                "id": str(permission.id),
                "code": permission.code,
                "module": permission.module,
                "description": permission.description,
            }
            for permission in permissions
        ]
    }


@router.get("/roles")
async def list_roles(request: Request, _principal: RolesReader) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        roles = list(
            (await session.scalars(select(Role).order_by(Role.is_system.desc(), Role.code))).all()
        )
        permissions = await permission_map(session)
    return {"items": [role_payload(role, permissions.get(role.id, [])) for role in roles]}


@router.post("/roles", status_code=status.HTTP_201_CREATED)
async def create_role(
    payload: CreateRoleRequest,
    request: Request,
    principal: RolesManager,
) -> dict[str, Any]:
    service = auth_service(request)
    _, _, request_id = request_metadata(request, service)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        permissions = await resolve_permissions(session, payload.permission_codes)
        role = Role(
            id=uuid7(),
            code=payload.code,
            name=payload.name,
            description=payload.description.strip(),
            is_system=False,
        )
        session.add(role)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise ApiError(409, "ROLE_ALREADY_EXISTS", "角色代码已存在") from exc
        session.add_all(
            RolePermission(role_id=role.id, permission_id=permission.id)
            for permission in permissions
        )
        service.record_audit(
            session,
            action="role.created",
            aggregate_type="role",
            aggregate_id=role.id,
            actor_user_id=principal.user_id,
            subject_user_id=None,
            request_id=request_id,
            details={"code": role.code, "permission_codes": sorted(payload.permission_codes)},
        )
        await session.commit()
        await session.refresh(role)
    return {"role": role_payload(role, sorted(payload.permission_codes))}


@router.patch("/roles/{role_id}")
async def update_role(
    role_id: uuid.UUID,
    payload: UpdateRoleRequest,
    request: Request,
    principal: RolesManager,
) -> dict[str, Any]:
    service = auth_service(request)
    _, _, request_id = request_metadata(request, service)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        role = await get_role_or_404(session, role_id, for_update=True)
        if role.is_system:
            raise ApiError(409, "SYSTEM_ROLE_IMMUTABLE", "系统内置角色只能通过代码迁移更新")
        if payload.name is not None:
            role.name = payload.name
        if payload.description is not None:
            role.description = payload.description.strip()
        service.record_audit(
            session,
            action="role.updated",
            aggregate_type="role",
            aggregate_id=role.id,
            actor_user_id=principal.user_id,
            subject_user_id=None,
            request_id=request_id,
        )
        await session.commit()
        await session.refresh(role)
        permissions = await permission_map(session, {role.id})
    return {"role": role_payload(role, permissions.get(role.id, []))}


@router.put("/roles/{role_id}/permissions")
async def replace_role_permissions(
    role_id: uuid.UUID,
    payload: ReplaceRolePermissionsRequest,
    request: Request,
    principal: RolesManager,
) -> dict[str, Any]:
    service = auth_service(request)
    _, _, request_id = request_metadata(request, service)
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        role = await get_role_or_404(session, role_id, for_update=True)
        if role.is_system:
            raise ApiError(409, "SYSTEM_ROLE_IMMUTABLE", "系统内置角色只能通过代码迁移更新")
        permissions = await resolve_permissions(session, payload.permission_codes)
        current_codes = set((await permission_map(session, {role.id})).get(role.id, []))
        requested_codes = set(payload.permission_codes)
        if current_codes != requested_codes:
            await session.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
            session.add_all(
                RolePermission(role_id=role.id, permission_id=permission.id)
                for permission in permissions
            )
            await bump_role_users(session, role.id)
        service.record_audit(
            session,
            action="role.permissions_replaced",
            aggregate_type="role",
            aggregate_id=role.id,
            actor_user_id=principal.user_id,
            subject_user_id=None,
            request_id=request_id,
            details={"permission_codes": sorted(requested_codes)},
        )
        await session.commit()
        await session.refresh(role)
    return {"role": role_payload(role, sorted(requested_codes))}


@router.get("/users/{user_id}/roles")
async def get_user_roles(
    user_id: uuid.UUID,
    request: Request,
    _principal: RolesReader,
) -> dict[str, Any]:
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        user = await get_user_or_404(session, user_id)
        statement = (
            select(Role, UserRole)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user.id)
            .order_by(Role.code)
        )
        assignments = (await session.execute(statement)).all()
        effective_permissions = await permission_codes_for_user(session, user.id)
    return {
        "user_id": str(user.id),
        "permission_version": user.permission_version,
        "roles": [
            {
                "id": str(role.id),
                "code": role.code,
                "name": role.name,
                "is_system": role.is_system,
                "expires_at": assignment.expires_at,
            }
            for role, assignment in assignments
        ],
        "effective_permissions": sorted(effective_permissions),
    }


@router.put("/users/{user_id}/roles")
async def replace_user_roles(
    user_id: uuid.UUID,
    payload: ReplaceUserRolesRequest,
    request: Request,
    principal: RolesManager,
) -> dict[str, Any]:
    service = auth_service(request)
    _, _, request_id = request_metadata(request, service)
    database = request.app.state.runtime_services.database
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        user = await get_user_or_404(session, user_id)
        requested_ids = {assignment.role_id for assignment in payload.roles}
        roles = list((await session.scalars(select(Role).where(Role.id.in_(requested_ids)))).all())
        role_by_id = {role.id: role for role in roles}
        if set(role_by_id) != requested_ids:
            raise ApiError(422, "UNKNOWN_ROLE", "包含不存在的角色")

        base_role = next((role for role in roles if role.code == "user"), None)
        base_assignment = next(
            (
                assignment
                for assignment in payload.roles
                if assignment.role_id == getattr(base_role, "id", None)
            ),
            None,
        )
        if base_role is None or base_assignment is None or base_assignment.expires_at is not None:
            raise ApiError(422, "BASE_ROLE_REQUIRED", "必须保留永不过期的 user 基础角色")

        desired_super_admin = any(
            role_by_id[assignment.role_id].code == "super_admin" for assignment in payload.roles
        )
        active_super_admins = await active_super_admin_ids(session, now=now, lock=True)
        if (
            user.id in active_super_admins
            and not desired_super_admin
            and len(active_super_admins) == 1
        ):
            raise ApiError(409, "LAST_SUPER_ADMIN", "不能移除最后一个有效超级管理员")
        await session.execute(select(User.id).where(User.id == user.id).with_for_update())

        await session.execute(delete(UserRole).where(UserRole.user_id == user.id))
        session.add_all(
            UserRole(
                user_id=user.id,
                role_id=assignment.role_id,
                assigned_by=principal.user_id,
                expires_at=assignment.expires_at,
            )
            for assignment in payload.roles
        )
        user.permission_version += 1
        role_details = [
            {
                "code": role_by_id[assignment.role_id].code,
                "expires_at": assignment.expires_at.isoformat()
                if assignment.expires_at is not None
                else None,
            }
            for assignment in payload.roles
        ]
        service.record_audit(
            session,
            action="user.roles_replaced",
            aggregate_type="user",
            aggregate_id=user.id,
            actor_user_id=principal.user_id,
            subject_user_id=user.id,
            request_id=request_id,
            details={"roles": sorted(role_details, key=lambda item: item["code"])},
        )
        await session.commit()

    return await get_user_roles(user_id, request, principal)
