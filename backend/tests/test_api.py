from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from app import main
from app.storage import ResultStore
from app.sub2api import Sub2APIError


def make_png() -> bytes:
    image = Image.new("RGB", (8, 6), (20, 90, 180))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_health_and_capabilities() -> None:
    client = TestClient(main.app)
    assert client.get("/api/health").json()["status"] == "ok"
    capabilities = client.get("/api/capabilities").json()
    assert capabilities["local_upscale"] is True
    assert capabilities["transparent_upstream_output"] is False


def test_upscale_route_returns_retrievable_result(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "store", ResultStore(tmp_path, ttl_hours=1))
    client = TestClient(main.app)
    response = client.post(
        "/api/upscale",
        files={"image": ("sample.png", make_png(), "image/png")},
        data={"scale": "2", "sharpen": "true"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert (payload["width"], payload["height"]) == (16, 12)
    assert client.get(payload["url"]).headers["content-type"] == "image/png"


def test_generate_requires_backend_credentials(monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "sub2api_base_url", "")
    monkeypatch.setattr(main.settings, "sub2api_api_key", "")
    client = TestClient(main.app)
    response = client.post(
        "/api/generate",
        json={"prompt": "test", "size": "1024x1024", "quality": "low"},
    )
    assert response.status_code == 503


def test_anonymous_runtime_settings_are_removed() -> None:
    client = TestClient(main.app)
    assert client.get("/api/settings").status_code == 404
    assert client.post("/api/settings", json={}).status_code == 405


def test_user_spa_routes_support_direct_navigation() -> None:
    client = TestClient(main.app)
    for path in ("/login", "/forgot-password", "/app", "/app/studio", "/app/assets"):
        response = client.get(path)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]


def test_model_connection_error_is_public(monkeypatch) -> None:
    class BrokenSub2API:
        async def list_models(self) -> list[str]:
            raise Sub2APIError("Invalid API key.", status_code=401)

    monkeypatch.setattr(main.settings, "sub2api_base_url", "https://sub2api.test/v1")
    monkeypatch.setattr(main.settings, "sub2api_api_key", "test")
    monkeypatch.setattr(main, "sub2api", BrokenSub2API())
    response = TestClient(main.app).get("/api/models")
    assert response.status_code == 401
    assert response.json()["message"] == "Invalid API key."
