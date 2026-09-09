from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Request

from app.api.errors import ApiError


@dataclass(slots=True)
class Principal:
    user_id: uuid.UUID
    session_id: uuid.UUID
    permissions: frozenset[str] = field(default_factory=frozenset)


async def get_current_principal(request: Request) -> Principal:
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    settings = request.app.state.settings
    raw_token = request.cookies.get(settings.session_cookie_name)
    if not raw_token:
        raise ApiError(401, "AUTHENTICATION_REQUIRED", "请先登录")
    database = request.app.state.runtime_services.database
    async with database.session_factory() as session:
        authenticated = await request.app.state.auth_service.authenticate_session(
            session, raw_token
        )
        if authenticated is None:
            raise ApiError(401, "AUTHENTICATION_REQUIRED", "登录状态已失效，请重新登录")
        user, auth_session, permissions = authenticated
        security_service = getattr(request.app.state, "security_service", None)
        if security_service is not None:
            try:
                await security_service.enforce_authenticated(request, session, user.id)
            except ApiError:
                await session.commit()
                raise
            await session.commit()
        principal = Principal(
            user_id=user.id,
            session_id=auth_session.id,
            permissions=permissions,
        )
    request.state.principal = principal
    return principal


def require_permission(permission: str):
    async def dependency(
        request: Request,
        principal: Annotated[Principal, Depends(get_current_principal)],
    ) -> Principal:
        if permission not in principal.permissions:
            security_service = getattr(request.app.state, "security_service", None)
            if security_service is not None:
                database = request.app.state.runtime_services.database
                async with database.session_factory() as session:
                    security_service.record_audit(
                        session,
                        action="permission.denied",
                        target_type="permission",
                        target_id=None,
                        actor_user_id=principal.user_id,
                        request_id=request.state.request_id,
                        result="denied",
                        details={"permission": permission, "path": request.url.path},
                    )
                    await session.commit()
            raise ApiError(403, "PERMISSION_DENIED", "没有执行此操作的权限")
        return principal

    return dependency
