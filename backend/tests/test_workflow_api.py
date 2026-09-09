from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from app import main, workflow
from app.catalog import StudioCatalog
from app.storage import ResultStore
from app.sub2api import UpstreamImage


def make_logo() -> bytes:
    image = Image.new("RGB", (32, 24), (235, 235, 235))
    for x in range(8, 24):
        for y in range(6, 18):
            image.putpixel((x, y), (20, 40, 170))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def make_chroma_logo() -> bytes:
    image = Image.new("RGB", (32, 24), (0, 255, 0))
    for x in range(8, 24):
        for y in range(6, 18):
            image.putpixel((x, y), (20, 40, 170))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def make_mask() -> bytes:
    image = Image.new("RGBA", (32, 24), (255, 255, 255, 255))
    for x in range(10, 20):
        for y in range(8, 16):
            image.putpixel((x, y), (0, 0, 0, 0))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def workflow_client(tmp_path, monkeypatch) -> TestClient:
    result_store = ResultStore(tmp_path / "results", ttl_hours=1)
    catalog = StudioCatalog(tmp_path / "studio.db")
    monkeypatch.setattr(workflow, "store", result_store)
    monkeypatch.setattr(workflow, "catalog", catalog)
    monkeypatch.setattr(main, "store", result_store)
    return TestClient(main.app)


def upload(client: TestClient, data: bytes = b"") -> dict:
    response = client.post(
        "/api/assets",
        files={"image": ("artwork.png", data or make_logo(), "image/png")},
    )
    assert response.status_code == 200
    return response.json()


def test_upload_restore_and_lineage(tmp_path, monkeypatch) -> None:
    client = workflow_client(tmp_path, monkeypatch)
    source = upload(client)

    restoration = client.post(
        "/api/restore",
        data={
            "source_asset_id": source["id"],
            "mode": "logo",
            "scale": "2",
            "denoise": "10",
            "deblur": "30",
        },
    )
    assert restoration.status_code == 200
    result = restoration.json()
    assert result["parent_id"] == source["id"]
    assert (result["width"], result["height"]) == (64, 48)

    lineage = client.get(f"/api/assets/{result['id']}/lineage")
    assert lineage.status_code == 200
    assert [item["id"] for item in lineage.json()] == [source["id"], result["id"]]

    preflight = client.post(
        "/api/preflight",
        json={"asset_id": result["id"], "target_width_cm": 30, "target_dpi": 300},
    )
    assert preflight.status_code == 200
    assert preflight.json()["status"] == "insufficient"


def test_ai_transform_passes_validated_mask_to_sub2api(tmp_path, monkeypatch) -> None:
    class FakeSub2API:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def edit(self, **kwargs) -> UpstreamImage:
            self.calls.append(kwargs)
            return UpstreamImage(kwargs["image_png"], "png", "test prompt")

    fake = FakeSub2API()
    monkeypatch.setattr(workflow.settings, "sub2api_base_url", "https://test.invalid/v1")
    monkeypatch.setattr(workflow.settings, "sub2api_api_key", "test-only")
    monkeypatch.setattr(workflow, "sub2api", fake)
    client = workflow_client(tmp_path, monkeypatch)
    source = upload(client)

    response = client.post(
        "/api/chain/ai-transform",
        data={
            "source_asset_id": source["id"],
            "mode": "local-repair",
            "instruction": "repair the selected edge",
        },
        files={"mask": ("mask.png", make_mask(), "image/png")},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["parent_id"] == source["id"]
    assert result["metadata"]["ai_mode"] == "local-repair"
    assert result["metadata"]["mask_selected_pixels"] == 80
    assert fake.calls[0]["mask_png"] is not None
    assert "repair the selected edge" in fake.calls[0]["prompt"]


def test_smart_cutout_detects_chroma_background(tmp_path, monkeypatch) -> None:
    client = workflow_client(tmp_path, monkeypatch)
    source = upload(client, make_chroma_logo())

    response = client.post(
        "/api/chain/smart-cutout",
        data={"source_asset_id": source["id"]},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["parent_id"] == source["id"]
    assert result["metadata"]["key_mode"] == "green"
    with Image.open(BytesIO(client.get(result["url"]).content)) as image:
        assert image.getchannel("A").getextrema()[0] == 0


def test_color_effect_creates_chainable_result(tmp_path, monkeypatch) -> None:
    client = workflow_client(tmp_path, monkeypatch)
    source = upload(client)

    response = client.post(
        "/api/chain/color-effect",
        data={
            "source_asset_id": source["id"],
            "mode": "monochrome",
            "color": "#d82c3f",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["operation"] == "color-effect"
    assert result["parent_id"] == source["id"]
    assert result["metadata"]["target_color"] == "#d82c3f"


def test_upscale_creates_chainable_result(tmp_path, monkeypatch) -> None:
    client = workflow_client(tmp_path, monkeypatch)
    source = upload(client)

    response = client.post(
        "/api/chain/upscale",
        data={"source_asset_id": source["id"], "scale": "2", "sharpen": "true"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["operation"] == "upscale"
    assert result["parent_id"] == source["id"]
    assert (result["width"], result["height"]) == (64, 48)
    assert result["metadata"]["scale"] == 2
    assert result["metadata"]["engine"] == "lanczos-unsharp"


def test_vectorize_exposes_downloadable_svg(tmp_path, monkeypatch) -> None:
    client = workflow_client(tmp_path, monkeypatch)
    source = upload(client)

    response = client.post(
        "/api/chain/vectorize",
        data={"source_asset_id": source["id"]},
    )

    assert response.status_code == 200
    result = response.json()
    svg_url = result["metadata"]["svg_download_url"]
    svg = client.get(svg_url)
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in svg.content
