from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ids import uuid7
from app.domain.rbac import PERMISSIONS, SYSTEM_ROLES
from app.repositories.models import Permission, Role, RolePermission, User, UserRole


async def permission_codes_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> frozenset[str]:
    current_time = now or datetime.now(UTC)
    statement = (
        select(Permission.code)
        .select_from(Permission)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(
            UserRole.user_id == user_id,
            or_(UserRole.expires_at.is_(None), UserRole.expires_at > current_time),
        )
        .distinct()
    )
    return frozenset((await session.scalars(statement)).all())


async def active_super_admin_ids(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    lock: bool = False,
) -> set[uuid.UUID]:
    current_time = now or datetime.now(UTC)
    role_statement = select(Role).where(Role.code == "super_admin")
    if lock:
        role_statement = role_statement.with_for_update()
    super_admin_role = (await session.execute(role_statement)).scalar_one_or_none()
    if super_admin_role is None:
        return set()
    statement = (
        select(User.id)
        .join(UserRole, UserRole.user_id == User.id)
        .where(
            UserRole.role_id == super_admin_role.id,
            User.status == "active",
            User.deleted_at.is_(None),
            or_(UserRole.expires_at.is_(None), UserRole.expires_at > current_time),
        )
    )
    return set((await session.scalars(statement)).all())


async def sync_builtin_rbac(session: AsyncSession) -> None:
    existing_permissions = {
        permission.code: permission
        for permission in (await session.scalars(select(Permission))).all()
    }
    for permission_seed in PERMISSIONS:
        permission = existing_permissions.get(permission_seed.code)
        if permission is None:
            permission = Permission(
                id=uuid7(),
                code=permission_seed.code,
                module=permission_seed.module,
                description=permission_seed.description,
            )
            session.add(permission)
            existing_permissions[permission_seed.code] = permission
        else:
            permission.module = permission_seed.module
            permission.description = permission_seed.description

    existing_roles = {role.code: role for role in (await session.scalars(select(Role))).all()}
    for role_seed in SYSTEM_ROLES:
        role = existing_roles.get(role_seed.code)
        if role is None:
            role = Role(
                id=uuid7(),
                code=role_seed.code,
                name=role_seed.name,
                description=role_seed.description,
                is_system=True,
            )
            session.add(role)
            existing_roles[role_seed.code] = role
        else:
            role.name = role_seed.name
            role.description = role_seed.description
            role.is_system = True

    await session.flush()
    for role_seed in SYSTEM_ROLES:
        role = existing_roles[role_seed.code]
        await session.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
        session.add_all(
            RolePermission(
                role_id=role.id,
                permission_id=existing_permissions[permission_code].id,
            )
            for permission_code in sorted(role_seed.permissions)
        )
