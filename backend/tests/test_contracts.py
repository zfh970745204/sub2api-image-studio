from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main

PROTECTED_READ_ROUTES = (
    "/api/v1/auth/me",
    "/api/v1/app/bootstrap",
    "/api/v1/membership/me",
    "/api/v1/points/balance",
    "/api/v1/jobs",
    "/api/v1/assets",
    "/api/v1/admin/dashboard/summary",
    "/api/v1/admin/users",
    "/api/v1/admin/membership-plans",
    "/api/v1/admin/points/accounts",
    "/api/v1/admin/operations",
    "/api/v1/admin/jobs",
    "/api/v1/admin/assets",
    "/api/v1/admin/config",
    "/api/v1/admin/permissions",
    "/api/v1/admin/audit-logs",
    "/api/v1/admin/security/events",
)


@pytest.mark.contract
@pytest.mark.parametrize("path", PROTECTED_READ_ROUTES)
def test_protected_read_contract_is_consistently_closed(path: str) -> None:
    response = TestClient(main.app).get(path)

    assert response.status_code == 401
    assert response.json() == {
        "code": "AUTHENTICATION_REQUIRED",
        "message": "请先登录",
        "request_id": response.headers["X-Request-ID"],
        "details": None,
    }


@pytest.mark.contract
def test_production_application_contains_no_internal_test_routes() -> None:
    def route_paths(routes: list[object]) -> set[str]:
        paths: set[str] = set()
        for route in routes:
            path = getattr(route, "path", None)
            if isinstance(path, str):
                paths.add(path)
            nested = getattr(route, "routes", None)
            if isinstance(nested, list):
                paths.update(route_paths(nested))
        return paths

    paths = route_paths(main.app.routes)

    assert not any(path.startswith("/internal/test") for path in paths)
