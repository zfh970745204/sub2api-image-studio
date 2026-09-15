"""Protocol contracts use tiny synthetic images and never call a paid API."""

import base64
import gzip
import json
import socket
from email import policy
from email.parser import BytesParser
from io import BytesIO
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from app.config import Settings
from app.image_download import download_image, opaque_reference, public_address
from app.image_gateway import ImageServiceClient
from app.image_provider_types import ImageServiceError, UnsupportedImageOperation
from app.services.configuration import ResolvedConfig, Sub2APIValues, sub2api_profile_settings
from app.services.image_executor import ImageJobExecutor
from app.services.jobs import PermanentJobError


def png() -> bytes:
    output = BytesIO()
    Image.new("RGBA", (8, 8), (180, 30, 10, 120)).save(output, "PNG")
    return output.getvalue()


def image_response() -> dict:
    return {"data": [{"b64_json": base64.b64encode(png()).decode()}]}


def test_wan_reference_keeps_opaque_pixels_but_rejects_transparency():
    with pytest.raises(UnsupportedImageOperation, match="透明参考图"):
        opaque_reference(png())
    source = Image.new("RGBA", (64, 64), (10, 20, 30, 255))
    output = BytesIO()
    source.save(output, "PNG")
    normalized = Image.open(BytesIO(opaque_reference(output.getvalue())))
    assert normalized.mode == "RGB" and normalized.getpixel((0, 0)) == (10, 20, 30)


def client(provider="openai", model="gpt-image-2", handler=None, **options):
    settings = Settings(
        _env_file=None,
        sub2api_provider=provider,
        sub2api_image_model=model,
        sub2api_api_key="test-secret",
        sub2api_base_url="https://api.example.test",
    ).model_copy(update=options)
    return ImageServiceClient(
        settings,
        httpx.MockTransport(handler or (lambda _: httpx.Response(200, json=image_response()))),
    )


ARGS = {
    "prompt": "preserve this product",
    "size": "1024x1536",
    "quality": "high",
    "output_format": "png",
}


@pytest.mark.asyncio
async def test_openlux_generation_format_and_edit_multipart():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=image_response())

    service = client("openlux", handler=handler)
    await service.generate(**ARGS)
    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/images/generations"
    assert body["format"] == "png" and "output_format" not in body
    await service.edit(image_png=png(), reference_images=[png()], mask_png=png(), **ARGS)
    request = requests[1]
    assert request.url.path == "/v1/images/edits"
    assert request.content.count(b'name="image[]"') == 2
    assert b'name="mask"' in request.content
    assert b'name="format"' not in request.content


@pytest.mark.asyncio
@pytest.mark.parametrize("inline", ["inline_data", "inlineData"])
async def test_gemini_preserves_references_and_native_auth(inline):
    def handler(request):
        assert request.url.path == "/v1beta/models/gemini-3-pro-image-preview:generateContent"
        assert request.headers["x-goog-api-key"] == "test-secret"
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        assert len(body["contents"][0]["parts"]) == 3
        assert body["generationConfig"]["imageConfig"] == {"aspectRatio": "2:3", "imageSize": "2K"}
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "done"},
                                {
                                    inline: {
                                        "data": base64.b64encode(png()).decode(),
                                        "mimeType": "image/png",
                                    }
                                },
                            ]
                        }
                    }
                ]
            },
        )

    result = await client("gemini", "gemini-3-pro-image-preview", handler).edit(
        image_png=png(), reference_images=[png()], **ARGS
    )
    assert result.data == png()


@pytest.mark.asyncio
async def test_gemini_relay_bearer_and_v1beta_prefix():
    def handler(request):
        assert request.url.path == "/v1beta/models/gemini-2.5-flash-image:generateContent"
        assert request.headers["authorization"] == "Bearer test-secret"
        assert "imageSize" not in json.loads(request.content)["generationConfig"]["imageConfig"]
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [{"inlineData": {"data": base64.b64encode(png()).decode()}}]
                        }
                    }
                ]
            },
        )

    await client(
        "gemini",
        "gemini-2.5-flash-image",
        handler,
        sub2api_base_url="https://api.openlux.ai/v1beta",
        sub2api_auth_mode="bearer",
    ).generate(**ARGS)


@pytest.mark.asyncio
async def test_seedream_edits_use_generation_json_and_at_least_2k():
    def handler(request):
        assert request.url.path == "/api/v3/images/generations"
        body = json.loads(request.content)
        assert len(body["image"]) == 2
        assert all(value.startswith("data:image/png;base64,") for value in body["image"])
        width, height = map(int, body["size"].split("x"))
        assert width * height >= 4_190_000 and abs(width / height - 2 / 3) < 0.001
        assert body["sequential_image_generation"] == "disabled"
        assert "quality" not in body
        return httpx.Response(200, json=image_response())

    await client("seedream", "doubao-seedream-4-5-251128", handler).edit(
        image_png=png(), reference_images=[png()], **ARGS
    )


@pytest.mark.asyncio
async def test_siliconflow_edit_uses_numbered_inputs_without_unsupported_size():
    def handler(request):
        body = json.loads(request.content)
        assert all(
            body[key].startswith("data:image/png;base64,") for key in ("image", "image2", "image3")
        )
        assert "image_size" not in body and "size" not in body
        return httpx.Response(
            200,
            json={"images": [{"url": "data:image/png;base64," + base64.b64encode(png()).decode()}]},
        )

    result = await client("siliconflow", "Qwen/Qwen-Image-Edit-2509", handler).edit(
        image_png=png(), reference_images=[png(), png()], **ARGS
    )
    assert result.data == png()


@pytest.mark.asyncio
async def test_dashscope_uses_multimodal_messages_and_converts_output():
    def handler(request):
        assert request.url.path == "/api/v1/services/aigc/multimodal-generation/generation"
        body = json.loads(request.content)
        assert body["input"]["messages"][0]["content"][1] == {"text": ARGS["prompt"]}
        assert body["parameters"]["size"] == "1024*1536"
        return httpx.Response(
            200,
            json={
                "output": {
                    "choices": [
                        {
                            "message": {
                                "content": [
                                    {
                                        "image": "data:image/png;base64,"
                                        + base64.b64encode(png()).decode()
                                    }
                                ]
                            }
                        }
                    ]
                },
                "request_id": "trace-1",
            },
        )

    result = await client("dashscope", "qwen-image-2.0-pro", handler).edit(
        image_png=png(), **{**ARGS, "output_format": "jpeg"}
    )
    assert Image.open(BytesIO(result.data)).format == "JPEG"
    assert result.output_format == "jpeg" and result.provider_request_id == "trace-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,model,refs,mask",
    [
        ("openai", "dall-e-3", 0, False),
        ("gemini", "gemini-3-pro-image-preview", 0, True),
        ("seedream", "doubao-seedream-4-5-251128", 0, True),
        ("siliconflow", "Qwen/Qwen-Image-Edit-2509", 3, False),
        ("bfl", "flux-kontext-pro", 1, False),
    ],
)
async def test_unsupported_edit_never_submits(provider, model, refs, mask):
    def handler(request):
        pytest.fail("An unsupported edit must not consume upstream credits")

    with pytest.raises(UnsupportedImageOperation):
        await client(provider, model, handler).edit(
            image_png=png(),
            reference_images=[png()] * refs,
            mask_png=png() if mask else None,
            **ARGS,
        )


@pytest.mark.asyncio
async def test_incompatible_primary_is_skipped_before_masked_request():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=image_response())

    executor = ImageJobExecutor.__new__(ImageJobExecutor)
    executor.profile_breakers = {}
    result = await executor._call_with_failover(
        [
            ("gemini", client("gemini", "gemini-3-pro-image-preview", handler)),
            ("openai", client(handler=handler)),
        ],
        lambda service: service.edit(image_png=png(), mask_png=png(), **ARGS),
    )
    assert result.data == png() and len(requests) == 1
    assert requests[0].url.path == "/v1/images/edits"
    assert executor.profile_breakers["gemini"].consecutive_failures == 0


@pytest.mark.asyncio
async def test_accepted_invalid_result_does_not_switch_or_retry():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": "bm90LWFuLWltYWdl"}]})

    executor = ImageJobExecutor.__new__(ImageJobExecutor)
    executor.profile_breakers = {}
    with pytest.raises(PermanentJobError):
        await executor._call_with_failover(
            [("one", client(handler=handler)), ("two", client(handler=handler))],
            lambda service: service.generate(**ARGS),
        )
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_bfl_submit_once_poll_through_transient_and_preserve_task_id(monkeypatch):
    monkeypatch.setattr("app.image_gateway.asyncio.sleep", AsyncMock())
    requests = []
    poll_results = iter(
        [
            httpx.Response(503),
            httpx.Response(200, json={"status": "Pending"}),
            httpx.Response(
                200,
                json={
                    "status": "Ready",
                    "result": {
                        "sample": "data:image/png;base64," + base64.b64encode(png()).decode()
                    },
                },
            ),
        ]
    )

    def handler(request):
        requests.append(request)
        assert request.headers["x-key"] == "test-secret"
        if request.method == "POST":
            body = json.loads(request.content)
            assert "input_image_2" in body and "model" not in body
            return httpx.Response(
                200,
                json={
                    "id": "task-1",
                    "polling_url": "https://api.eu.bfl.ai/v1/get_result?id=task-1",
                },
            )
        return next(poll_results)

    result = await client(
        "bfl", "flux-2-pro", handler, sub2api_base_url="https://api.bfl.ai/v1"
    ).edit(image_png=png(), reference_images=[png()], **ARGS)
    assert result.provider_request_id == "task-1"
    assert sum(request.method == "POST" for request in requests) == 1
    assert len(requests) == 4


@pytest.mark.asyncio
async def test_bfl_deadline_preserves_id_and_never_resubmits():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "pending-task",
                "polling_url": "https://api.example.test/v1/get_result?id=pending-task",
            },
        )

    with pytest.raises(ImageServiceError) as error:
        await client("bfl", "flux-2-pro", handler, sub2api_timeout_seconds=0.03).generate(**ARGS)
    assert error.value.provider_request_id == "pending-task"
    assert error.value.retryable is False and len(requests) == 1


@pytest.mark.asyncio
async def test_bfl_rejects_foreign_poll_host_before_sending_credentials():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json={"id": "task-1", "polling_url": "https://attacker.test/steal"}
        )

    with pytest.raises(ImageServiceError, match="不可信"):
        await client("bfl", "flux-2-pro", handler).generate(**ARGS)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "100.64.0.1"]
)
async def test_download_blocks_private_dns_results(monkeypatch, ip):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))],
    )
    with pytest.raises(ImageServiceError):
        await public_address("image.example", 443)


@pytest.mark.asyncio
async def test_url_download_pins_ip_without_auth_and_rechecks_redirect(monkeypatch):
    resolve = AsyncMock(return_value="93.184.215.14")
    monkeypatch.setattr("app.image_download.public_address", resolve)
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200, json={"data": [{"url": "https://cdn.example/image?signature=123"}]}
            )
        assert request.url.host == "93.184.215.14"
        assert "authorization" not in request.headers and "x-key" not in request.headers
        assert request.extensions["sni_hostname"] == request.headers["host"]
        if request.headers["host"] == "cdn.example":
            return httpx.Response(302, headers={"location": "https://cdn2.example/final.png"})
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

    result = await client(handler=handler).generate(**ARGS)
    assert result.data == png()
    assert resolve.await_count == 2
    assert requests[1].url.query == b"signature=123"


@pytest.mark.asyncio
async def test_gzip_response_and_plaintext_errors_do_not_leak_keys():
    body = gzip.compress(json.dumps(image_response()).encode())
    result = await client(
        handler=lambda _: httpx.Response(200, content=body, headers={"content-encoding": "gzip"})
    ).generate(**ARGS)
    assert result.data == png()
    with pytest.raises(ImageServiceError) as error:
        await client(
            handler=lambda _: httpx.Response(
                401,
                json={
                    "error": {
                        "message": "invalid test-secret at https://secret-host/?key=test-secret"
                    }
                },
            )
        ).generate(**ARGS)
    assert "test-secret" not in str(error.value) and "secret-host" not in str(error.value)


@pytest.mark.asyncio
async def test_connection_probe_does_not_generate_or_claim_auth_verified():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(405)

    message = await client("seedream", "doubao-seedream-4-5-251128", handler).check_connection()
    assert all(request.method == "GET" for request in requests)
    assert "未验证密钥" in message


@pytest.mark.asyncio
async def test_stability_text_generation_is_multipart_and_decodes_binary():
    def handler(request):
        assert request.url.path == "/v2beta/stable-image/generate/sd3"
        assert request.headers["accept"] == "image/*"
        assert request.headers["content-type"].startswith("multipart/form-data;")
        assert b'name="mode"\r\n\r\ntext-to-image' in request.content
        assert b'name="model"\r\n\r\nsd3.5-large' in request.content
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

    result = await client("stability", "sd3.5-large", handler).generate(**ARGS)
    assert result.data == png()


@pytest.mark.asyncio
async def test_stability_mask_uses_alpha_not_rgb_and_never_grows_selection():
    mask = Image.new("RGBA", (64, 64), (240, 80, 60, 255))
    mask.putpixel((4, 4), (0, 0, 0, 0))
    raw_mask = BytesIO()
    mask.save(raw_mask, "PNG")

    def handler(request):
        assert request.url.path == "/v2beta/stable-image/edit/inpaint"
        message = BytesParser(policy=policy.default).parsebytes(
            ("Content-Type: " + request.headers["content-type"] + "\r\n\r\n").encode()
            + request.content
        )
        parts = {
            part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
            for part in message.iter_parts()
        }
        sent_mask = Image.open(BytesIO(parts["mask"]))
        assert sent_mask.getpixel((4, 4)) == 255
        assert sent_mask.getpixel((0, 0)) == 0
        assert parts["grow_mask"] == b"0"
        assert "model" not in parts and "strength" not in parts
        return httpx.Response(
            200, json={"image": base64.b64encode(png()).decode(), "finish_reason": "SUCCESS"}
        )

    result = await client("stability", "sd3.5-large", handler).edit(
        image_png=png(), mask_png=raw_mask.getvalue(), **ARGS
    )
    assert result.data == png()


@pytest.mark.asyncio
async def test_download_rejects_redirect_to_metadata_host(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *args, **kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("169.254.169.254" if host == "169.254.169.254" else "93.184.215.14", 443),
            )
        ],
    )
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    with pytest.raises(ImageServiceError):
        await download_image("https://public.example/image", transport=httpx.MockTransport(handler))
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_download_byte_limit_applies_even_without_content_length(monkeypatch):
    monkeypatch.setattr(
        "app.image_download.public_address", AsyncMock(return_value="93.184.215.14")
    )
    monkeypatch.setattr("app.image_download.MAX_IMAGE_BYTES", 100)

    class ChunkedImage(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"a" * 60
            yield b"b" * 60

    with pytest.raises(ImageServiceError):
        await download_image(
            "https://public.example/image",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=ChunkedImage())),
        )


@pytest.mark.parametrize(
    "values",
    [
        {"provider": "unknown"},
        {"auth_mode": "arbitrary-header"},
        {"base_url": "https://api.example/v1?key=secret"},
        {"base_url": "https://api.example/v1/audio/speech"},
    ],
)
def test_invalid_provider_settings_fail_before_publish(values):
    with pytest.raises(ValidationError):
        Sub2APIValues.model_validate(values)


def test_legacy_and_version_pinned_profiles_resolve_protocols():
    legacy = Sub2APIValues.model_validate({"enabled": True, "base_url": "https://old.test/v1"})
    config = ResolvedConfig(
        group="sub2api", version=7, values=legacy.model_dump(), secrets={"api_key": "old-key"}
    )
    profile = sub2api_profile_settings(config)[0]
    assert profile.sub2api_provider == "openai" and profile.sub2api_api_key == "old-key"
    native = Sub2APIValues.model_validate(
        {
            "enabled": True,
            "profiles": [
                {
                    "id": "gemini",
                    "provider": "gemini",
                    "auth_mode": "bearer",
                    "base_url": "https://api.openlux.ai/v1beta",
                    "image_model": "gemini-3-pro-image-preview",
                }
            ],
        }
    )
    config = ResolvedConfig(
        group="sub2api",
        version=8,
        values=native.model_dump(),
        secrets={"api_key_gemini": "relay-key"},
    )
    profile = sub2api_profile_settings(config)[0]
    assert profile.sub2api_provider == "gemini" and profile.sub2api_auth_mode == "bearer"
    assert profile.sub2api_api_key == "relay-key"
