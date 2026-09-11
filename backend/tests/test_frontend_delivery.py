from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.testclient import TestClient

from app.frontend import FrontendStaticFiles


def test_static_delivery_compresses_text_and_revalidates_only_mutable_files(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app-abcdefgh.js").write_text(
        "const message = 'hello';\n" * 100, newline=""
    )
    (tmp_path / "index.html").write_text("<html>current release</html>")
    (tmp_path / "brand.png").write_bytes(b"image-payload" * 1000)
    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    app.mount("/", FrontendStaticFiles(directory=tmp_path, html=True))
    client = TestClient(app)

    script = client.get("/assets/app-abcdefgh.js", headers={"Accept-Encoding": "gzip"})
    assert script.headers["content-encoding"] == "gzip"
    assert "immutable" in script.headers["cache-control"]
    assert "Accept-Encoding" in script.headers["vary"]
    assert script.text == "const message = 'hello';\n" * 100
    assert (
        client.get("/", headers={"Accept-Encoding": "identity"}).headers["cache-control"]
        == "no-cache"
    )

    picture = client.get("/brand.png", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in picture.headers
    assert picture.headers["cache-control"] == "no-cache"
    cached = client.get("/brand.png", headers={"If-None-Match": picture.headers["etag"]})
    assert cached.status_code == 304
    assert not cached.content
    missing = client.get("/assets/missing-abcdefgh.js")
    assert missing.status_code == 404
    assert "immutable" not in missing.headers.get("cache-control", "")
