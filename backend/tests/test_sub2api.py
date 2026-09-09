import base64
from io import BytesIO

import httpx
import pytest
from PIL import Image

from app.config import Settings
from app.sub2api import Sub2APIClient, Sub2APIError


def make_png() -> bytes:
    image = Image.new("RGB", (4, 4), (230, 70, 30))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def settings() -> Settings:
    return Settings(
        sub2api_base_url="https://sub2api.test/v1",
        sub2api_api_key="test-only",
    )


@pytest.mark.asyncio
async def test_generate_decodes_base64_image() -> None:
    raw = make_png()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/images/generations"
        assert request.headers["authorization"] == "Bearer test-only"
        return httpx.Response(
            200,
            json={
                "data": [{"b64_json": base64.b64encode(raw).decode(), "revised_prompt": "ok"}],
                "output_format": "png",
            },
        )

    client = Sub2APIClient(settings(), transport=httpx.MockTransport(handler))
    result = await client.generate(
        prompt="test",
        size="1024x1024",
        quality="low",
        output_format="png",
    )
    assert result.data == raw
    assert result.revised_prompt == "ok"


@pytest.mark.asyncio
async def test_edit_uploads_png_with_explicit_mime() -> None:
    raw = make_png()

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        assert request.url.path == "/v1/images/edits"
        assert b'name="image"; filename="image.png"' in body
        assert b"Content-Type: image/png" in body
        assert b'name="mask"; filename="mask.png"' in body
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(raw).decode()}]},
        )

    client = Sub2APIClient(settings(), transport=httpx.MockTransport(handler))
    result = await client.edit(
        image_png=raw,
        prompt="turn it blue",
        size="1024x1024",
        quality="low",
        output_format="png",
        mask_png=raw,
    )
    assert result.data == raw


@pytest.mark.asyncio
async def test_upstream_error_message_is_preserved() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad input"}})

    client = Sub2APIClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(Sub2APIError, match="bad input"):
        await client.generate(
            prompt="test",
            size="1024x1024",
            quality="low",
            output_format="png",
        )


@pytest.mark.asyncio
async def test_model_contract_filters_invalid_entries_and_sorts_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-image-2"}, {}, "invalid", {"id": "alpha-image"}]},
        )

    client = Sub2APIClient(settings(), transport=httpx.MockTransport(handler))
    assert await client.list_models() == ["alpha-image", "gpt-image-2"]


@pytest.mark.asyncio
async def test_network_failure_is_mapped_without_leaking_the_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("upstream-secret-host", request=request)

    client = Sub2APIClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(Sub2APIError, match="ConnectTimeout") as caught:
        await client.list_models()
    assert "upstream-secret-host" not in str(caught.value)
    assert caught.value.status_code == 502


@pytest.mark.asyncio
async def test_non_json_http_error_uses_bounded_status_contract() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="proxy failure with internal details")

    client = Sub2APIClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(Sub2APIError, match="HTTP 503") as caught:
        await client.list_models()
    assert caught.value.status_code == 503
    assert "internal details" not in str(caught.value)


@pytest.mark.asyncio
async def test_invalid_image_and_format_fallback_are_rejected_or_normalized() -> None:
    responses = iter(
        (
            httpx.Response(200, json={"data": [{"b64_json": "not-base64"}]}),
            httpx.Response(
                200,
                json={
                    "data": [{"b64_json": base64.b64encode(make_png()).decode()}],
                    "output_format": "executable",
                },
            ),
        )
    )

    client = Sub2APIClient(settings(), transport=httpx.MockTransport(lambda _: next(responses)))
    with pytest.raises(Sub2APIError, match="invalid image response"):
        await client.generate(
            prompt="test",
            size="1024x1024",
            quality="low",
            output_format="png",
        )
    result = await client.generate(
        prompt="test",
        size="1024x1024",
        quality="low",
        output_format="png",
    )
    assert result.output_format == "png"
