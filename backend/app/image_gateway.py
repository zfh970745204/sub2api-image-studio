"""Image API dialects behind one generate/edit contract; never retry a submission here."""

from __future__ import annotations

import asyncio
import base64
import math
import re
from typing import Any
from urllib.parse import quote

import httpx

from .image_download import (
    MAX_RESPONSE_BYTES,
    decode_base64,
    download_image,
    inpainting_mask,
    invalid_image,
    normalize_image,
    opaque_reference,
)
from .image_provider_types import (
    ImageCapabilities,
    ImageServiceError,
    UnsupportedImageOperation,
    UpstreamImage,
    image_capabilities,
)


def dimensions(size: str) -> tuple[int, int]:
    if size == "auto":
        return 1024, 1024
    if not re.fullmatch(r"\d{2,5}x\d{2,5}", size):
        raise UnsupportedImageOperation("图片尺寸格式无效")
    width, height = map(int, size.split("x"))
    if not 64 <= width <= 8192 or not 64 <= height <= 8192:
        raise UnsupportedImageOperation("图片尺寸超出接口支持范围")
    return width, height


def aspect_ratio(size: str) -> str:
    width, height = dimensions(size)
    ratios = ("1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "21:9")
    return min(
        ratios,
        key=lambda ratio: abs(width / height - int(ratio.split(":")[0]) / int(ratio.split(":")[1])),
    )


def data_uri(image: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(image).decode("ascii")


class ImageServiceClient:
    def __init__(self, settings: Any, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport
        self.provider = getattr(settings, "sub2api_provider", "openai")
        self.model = settings.sub2api_image_model

    @property
    def capabilities(self) -> ImageCapabilities:
        return image_capabilities(self.provider, self.model)

    @property
    def headers(self) -> dict[str, str]:
        auth = getattr(self.settings, "sub2api_auth_mode", "auto")
        if auth == "auto":
            auth = {"gemini": "x-goog-api-key", "bfl": "x-key"}.get(self.provider, "bearer")
        key = self.settings.sub2api_api_key
        return {"Authorization": f"Bearer {key}"} if auth == "bearer" else {auth: key}

    @property
    def base_url(self) -> str:
        base = self.settings.normalized_base_url.rstrip("/")
        if not httpx.URL(base).path.strip("/"):
            base += {
                "gemini": "/v1beta",
                "seedream": "/api/v3",
                "dashscope": "/api/v1",
                "stability": "/v2beta",
            }.get(self.provider, "/v1")
        return base

    async def generate(
        self, *, prompt: str, size: str, quality: str, output_format: str
    ) -> UpstreamImage:
        return await self._image_call(prompt, size, quality, output_format, [], None)

    async def edit(
        self,
        *,
        image_png: bytes,
        prompt: str,
        size: str,
        quality: str,
        output_format: str,
        mask_png: bytes | None = None,
        reference_images: list[bytes] | None = None,
    ) -> UpstreamImage:
        return await self._image_call(
            prompt, size, quality, output_format, [image_png, *(reference_images or [])], mask_png
        )

    async def _image_call(
        self,
        prompt: str,
        size: str,
        quality: str,
        output_format: str,
        images: list[bytes],
        mask: bytes | None,
    ) -> UpstreamImage:
        capability = self.capabilities
        if not images and not capability.generation:
            raise UnsupportedImageOperation("该模型仅支持图片编辑，请配置支持文生图的线路")
        if images and not capability.max_images:
            raise UnsupportedImageOperation("该模型不支持参考图编辑，请配置图片编辑线路")
        if len(images) > capability.max_images:
            raise UnsupportedImageOperation(
                f"该模型最多支持 {capability.max_images} 张参考图（包含原图）"
            )
        if mask is not None and not capability.mask:
            raise UnsupportedImageOperation(
                "该协议不支持遮罩局部重绘，请配置 OpenAI 或 Stability 图片编辑线路"
            )
        dimensions(size)
        if output_format not in {"png", "jpeg", "webp"}:
            raise UnsupportedImageOperation("不支持的输出图片格式")
        request_id = None
        try:
            # One deadline includes submission, polling and result retrieval.
            async with asyncio.timeout(self.settings.sub2api_timeout_seconds):
                if self.provider == "dashscope" and self.model.lower().startswith("wan"):
                    images = [await asyncio.to_thread(opaque_reference, image) for image in images]
                if self.provider == "stability":
                    response = await self._stability(prompt, size, output_format, images, mask)
                elif self.provider in {"openai", "openlux"}:
                    response = await self._openai(
                        prompt, size, quality, output_format, images, mask
                    )
                else:
                    path, payload = self._json_request(prompt, size, output_format, images)
                    response = await self._request("POST", path, json=payload)
                try:
                    if self.provider == "bfl":
                        task_id = self._json(response).get("id")
                        request_id = task_id if isinstance(task_id, str) else None
                        response, request_id = await self._poll_bfl(response)
                    result = await self._read_image(response, output_format)
                    result.provider_request_id = request_id or result.provider_request_id
                    return result
                except ImageServiceError as exc:
                    # A successful POST may already have consumed provider credits.
                    exc.retryable = False
                    exc.provider_request_id = exc.provider_request_id or request_id
                    raise
        except TimeoutError as exc:
            raise ImageServiceError(
                "图片服务等待超时；未自动重复提交生图",
                504,
                retryable=False,
                provider_request_id=request_id,
            ) from exc

    async def _openai(self, prompt, size, quality, output_format, images, mask) -> httpx.Response:
        payload: dict[str, Any] = {"model": self.model, "prompt": prompt, "n": 1}
        if self.model.lower().startswith("gpt-image"):
            payload.update(size=size, quality=quality)
            if self.provider == "openai":
                payload["output_format"] = output_format
            elif not images:
                # OpenLux GPT Image hangs on response_format even though its schema lists it.
                # Its documented request uses format and returns b64_json by default.
                payload["format"] = output_format
        else:
            payload["response_format"] = "b64_json"
            if size != "auto":
                if self.model == "dall-e-3":
                    width, height = dimensions(size)
                    payload["size"] = (
                        "1792x1024"
                        if width > height
                        else "1024x1792"
                        if height > width
                        else "1024x1024"
                    )
                    payload["quality"] = "hd" if quality == "high" else "standard"
                elif self.model == "dall-e-2":
                    if dimensions(size)[0] != dimensions(size)[1]:
                        raise UnsupportedImageOperation("DALL·E 2 仅支持方形图片")
                    payload["size"] = "1024x1024"
                else:
                    payload["size"] = size
        if not images:
            return await self._request("POST", "/images/generations", json=payload)
        field = "image[]" if len(images) > 1 else "image"
        files = [
            (field, ("image.png" if len(images) == 1 else f"image-{index}.png", value, "image/png"))
            for index, value in enumerate(images)
        ]
        if mask is not None:
            files.append(("mask", ("mask.png", mask, "image/png")))
        return await self._request(
            "POST",
            "/images/edits",
            data={key: str(value) for key, value in payload.items()},
            files=files,
        )

    async def _stability(
        self, prompt: str, size: str, output_format: str, images: list[bytes], mask: bytes | None
    ) -> httpx.Response:
        fields = {"prompt": prompt, "output_format": output_format}
        model = self.model.lower()
        path = f"/stable-image/generate/{model if model in {'core', 'ultra'} else 'sd3'}"
        if mask is not None:
            path = "/stable-image/edit/inpaint"
            fields["grow_mask"] = "0"
        elif model not in {"core", "ultra"}:
            fields.update(model=self.model, mode="image-to-image" if images else "text-to-image")
        if mask is None:
            if images:
                fields["strength"] = "0.65"
            elif size != "auto":
                ratio = aspect_ratio(size)
                fields["aspect_ratio"] = {"4:3": "5:4", "3:4": "4:5"}.get(ratio, ratio)
        # All fields are multipart, including text-only generations.
        files: list[tuple[str, Any]] = [(key, (None, value)) for key, value in fields.items()]
        if images:
            files.append(("image", ("image.png", images[0], "image/png")))
        if mask is not None:
            converted = await asyncio.to_thread(inpainting_mask, mask)
            files.append(("mask", ("mask.png", converted, "image/png")))
        if sum(map(len, images)) + (len(mask) if mask else 0) > 9 * 1024 * 1024:
            raise UnsupportedImageOperation("Stability 输入图片和遮罩总量需小于 9 MB")
        return await self._request("POST", path, files=files, extra_headers={"Accept": "image/*"})

    def _json_request(
        self, prompt: str, size: str, output_format: str, images: list[bytes]
    ) -> tuple[str, dict[str, Any]]:
        width, height = dimensions(size)
        payload: dict[str, Any] = {"model": self.model, "prompt": prompt}
        name = self.model.lower()
        if self.provider == "gemini":
            config: dict[str, Any] = {"responseModalities": ["TEXT", "IMAGE"]}
            image_config: dict[str, str] = {}
            if size != "auto":
                image_config["aspectRatio"] = aspect_ratio(size)
                if "2.5" not in name:
                    image_config["imageSize"] = (
                        "4K"
                        if width * height > 2048 * 2048
                        else "2K"
                        if width * height > 1024 * 1024
                        else "1K"
                    )
            if image_config:
                config["imageConfig"] = image_config
            parts = [
                {"text": prompt},
                *[
                    {
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": base64.b64encode(image).decode("ascii"),
                        }
                    }
                    for image in images
                ],
            ]
            return (
                f"/models/{quote(self.model.removeprefix('models/'), safe='')}:generateContent",
                {"contents": [{"role": "user", "parts": parts}], "generationConfig": config},
            )
        if self.provider == "seedream":
            if "seededit" in name:
                if not images:
                    raise UnsupportedImageOperation("SeedEdit 需要原图")
                payload["size"] = "adaptive"
            else:
                # Seedream 4+ requires at least roughly 2K pixels. Keep ratio.
                factor = max(1.0, math.sqrt(2048 * 2048 / (width * height)))
                payload["size"] = f"{round(width * factor)}x{round(height * factor)}"
                payload["sequential_image_generation"] = "disabled"
            payload["response_format"] = "b64_json"
            if images:
                payload["image"] = (
                    data_uri(images[0])
                    if len(images) == 1
                    else [data_uri(image) for image in images]
                )
            return "/images/generations", payload
        if self.provider == "siliconflow":
            if not images:
                payload["image_size"] = f"{width}x{height}"
            for index, image in enumerate(images):
                payload["image" if index == 0 else f"image{index + 1}"] = data_uri(image)
            return "/images/generations", payload
        if self.provider == "dashscope":
            content = [{"image": data_uri(image)} for image in images] + [{"text": prompt}]
            parameters: dict[str, Any] = {"n": 1}
            if size != "auto" and name != "qwen-image-edit":
                factor = min(1.0, 2048 / max(width, height))
                parameters["size"] = (
                    f"{round(width * factor / 16) * 16}*{round(height * factor / 16) * 16}"
                )
            return "/services/aigc/multimodal-generation/generation", {
                "model": self.model,
                "input": {"messages": [{"role": "user", "content": content}]},
                "parameters": parameters,
            }
        if self.provider == "bfl":
            payload = {
                "prompt": prompt,
                "output_format": "jpeg" if output_format == "jpeg" else "png",
            }
            if name.startswith("flux-kontext-") or name == "flux-pro-1.1-ultra":
                if size != "auto":
                    payload["aspect_ratio"] = aspect_ratio(size)
            elif size != "auto":
                maximum = 2048 if name.startswith("flux-2-") else 1440
                factor = min(1.0, maximum / max(width, height))
                payload.update(
                    width=round(width * factor / 32) * 32, height=round(height * factor / 32) * 32
                )
            for index, image in enumerate(images):
                payload["input_image" if index == 0 else f"input_image_{index + 1}"] = (
                    base64.b64encode(image).decode("ascii")
                )
            return f"/{quote(self.model, safe='')}", payload
        raise UnsupportedImageOperation("不支持的图片协议")

    async def list_models(self) -> list[str]:
        if self.provider not in {"openai", "openlux", "gemini", "siliconflow"}:
            return [self.model]
        payload = self._json(await self._request("GET", "/models"))
        field, key = ("models", "name") if self.provider == "gemini" else ("data", "id")
        items = payload.get(field)
        if not isinstance(items, list):
            raise ImageServiceError("模型列表返回格式无效", retryable=False)
        return sorted(
            {
                str(item[key]).removeprefix("models/")
                for item in items
                if isinstance(item, dict) and item.get(key)
            }
        )

    async def check_connection(self) -> str:
        if self.provider == "stability":
            await self._request(
                "GET", "/user/balance", api_base=self.base_url.removesuffix("/v2beta") + "/v1"
            )
            return "账户认证接口可用；具体模型权限和生图效果需实际任务验证"
        if self.provider in {"openai", "openlux", "gemini", "siliconflow"}:
            models = await self.list_models()
            if self.model not in models:
                raise ImageServiceError(
                    f"认证成功，但配置模型 {self.model} 不在当前密钥可见的模型列表中",
                    retryable=False,
                )
            return (
                f"认证成功，配置模型 {self.model} 可见；"
                "未执行收费生图或编辑，实际调用能力仍需任务验证"
            )
        path = (
            "/services/aigc/multimodal-generation/generation"
            if self.provider == "dashscope"
            else f"/{quote(self.model, safe='')}"
            if self.provider == "bfl"
            else "/images/generations"
        )
        await self._request("GET", path, allowed_statuses={400, 405, 422})
        return "服务可达；该协议无统一模型列表，此检查未验证密钥、模型权限或发起付费生图"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        allowed_statuses: set[int] | None = None,
        extra_headers: dict[str, str] | None = None,
        api_base: str | None = None,
        **kwargs,
    ) -> httpx.Response:
        url = path if path.startswith("https://") else f"{api_base or self.base_url}{path}"
        try:
            async with (
                httpx.AsyncClient(
                    timeout=self.settings.sub2api_timeout_seconds,
                    follow_redirects=False,
                    transport=self.transport,
                ) as client,
                client.stream(
                    method, url, headers={**self.headers, **(extra_headers or {})}, **kwargs
                ) as response,
            ):
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise ImageServiceError("图片服务响应过大", retryable=False)
                result = httpx.Response(
                    response.status_code,
                    headers={
                        key: value
                        for key, value in response.headers.items()
                        if key not in {"content-encoding", "content-length"}
                    },
                    content=bytes(content),
                )
        except httpx.RequestError as exc:
            safe = method == "GET" or isinstance(
                exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
            )
            message = f"Could not reach image service: {exc.__class__.__name__}."
            if method != "GET" and isinstance(
                exc, (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError)
            ):
                message = "图片服务在生成完成前超时或断开连接；为避免重复扣费未自动重试"
            raise ImageServiceError(
                message,
                retryable=safe,
            ) from exc
        if (result.is_error or result.is_redirect) and result.status_code not in (
            allowed_statuses or set()
        ):
            message = f"Image service returned HTTP {result.status_code}."
            try:
                payload = result.json()
                error = payload.get("error", payload)
                if isinstance(error, dict):
                    message = str(error.get("message") or error.get("detail") or message)
            except (ValueError, AttributeError):
                pass
            raise ImageServiceError(self._safe_message(message), status_code=result.status_code)
        return result

    def _safe_message(self, message: str) -> str:
        key = self.settings.sub2api_api_key
        if key:
            message = message.replace(key, "[redacted]")
        return re.sub(r"https?://\S+", "[upstream URL]", message)[:600]

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise invalid_image() from exc
        if not isinstance(payload, dict):
            raise invalid_image()
        if payload.get("error") or payload.get("code") not in (None, "", 0, "0", "200", 200):
            error = payload.get("error") or payload
            message = (
                error.get("message", "图片服务拒绝了请求")
                if isinstance(error, dict)
                else "图片服务拒绝了请求"
            )
            raise ImageServiceError(self._safe_message(str(message)), 400, retryable=False)
        return payload

    async def _read_image(self, response: httpx.Response, output_format: str) -> UpstreamImage:
        if response.headers.get("content-type", "").startswith("image/"):
            raw = response.content
            revised = request_id = None
        else:
            payload = self._json(response)
            revised = None
            request_id = payload.get("request_id") or payload.get("id")
            encoded = url = None
            try:
                if self.provider == "gemini":
                    parts = payload["candidates"][0]["content"]["parts"]
                    for part in parts:
                        inline = part.get("inlineData") or part.get("inline_data")
                        if inline:
                            encoded = inline["data"]
                            break
                elif self.provider == "dashscope":
                    parts = payload["output"]["choices"][0]["message"]["content"]
                    url = next(part["image"] for part in parts if part.get("image"))
                elif self.provider == "bfl":
                    url = payload["result"]["sample"]
                elif self.provider == "stability":
                    if payload.get("finish_reason", "SUCCESS") != "SUCCESS":
                        raise ImageServiceError(
                            "图片服务未生成图片或内容审核未通过", 400, retryable=False
                        )
                    encoded = payload.get("image")
                else:
                    items = payload.get("data") or payload.get("images")
                    if not isinstance(items, list) or not items:
                        raise invalid_image()
                    item = items[0]
                    encoded, url = item.get("b64_json"), item.get("url")
                    revised = item.get("revised_prompt")
            except (KeyError, IndexError, TypeError, AttributeError, StopIteration) as exc:
                raise invalid_image() from exc
            if isinstance(encoded, str) and encoded:
                raw = decode_base64(encoded)
            elif isinstance(url, str) and url:
                raw = (
                    decode_base64(url)
                    if url.startswith("data:")
                    else await download_image(url, transport=self.transport)
                )
            else:
                raise invalid_image()
        raw = await asyncio.to_thread(normalize_image, raw, output_format)
        return UpstreamImage(
            raw,
            output_format,
            revised if isinstance(revised, str) else None,
            str(request_id)[:200] if request_id else None,
        )

    async def _poll_bfl(self, response: httpx.Response) -> tuple[httpx.Response, str]:
        payload = self._json(response)
        task_id, polling_url = payload.get("id"), payload.get("polling_url")
        if not isinstance(task_id, str) or not isinstance(polling_url, str):
            raise invalid_image()
        target, base = httpx.URL(polling_url), httpx.URL(self.base_url)
        same_origin = (target.scheme, target.host, target.port) == (
            base.scheme,
            base.host,
            base.port,
        )
        bfl_host = base.host in {
            "api.bfl.ai",
            "api.eu.bfl.ai",
            "api.us.bfl.ai",
        } and target.host.endswith(".bfl.ai")
        if (
            target.scheme != "https"
            or target.userinfo
            or target.port not in {None, 443}
            or not (same_origin or bfl_host)
        ):
            raise ImageServiceError(
                "图片服务返回了不可信的任务查询地址", retryable=False, provider_request_id=task_id
            )
        try:
            while True:
                await asyncio.sleep(2)
                try:
                    result = await self._request("GET", polling_url)
                except ImageServiceError as exc:
                    if exc.status_code in {408, 429} or exc.status_code >= 500:
                        continue
                    raise
                status = str(self._json(result).get("status", "")).lower()
                if status == "ready":
                    return result, task_id
                if status not in {"pending", "processing"}:
                    raise ImageServiceError(
                        "图片服务任务失败或被内容审核拒绝", 400, retryable=False
                    )
        except ImageServiceError as exc:
            exc.retryable = False
            exc.provider_request_id = task_id
            raise
