from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

import httpx

from .config import Settings


class Sub2APIError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class UpstreamImage:
    data: bytes
    output_format: str
    revised_prompt: str | None


class Sub2APIClient:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.transport = transport

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.sub2api_api_key}"}

    async def generate(
        self,
        *,
        prompt: str,
        size: str,
        quality: str,
        output_format: str,
    ) -> UpstreamImage:
        payload = {
            "model": self.settings.sub2api_image_model,
            "prompt": prompt,
            "size": size,
            "quality": quality,
            "output_format": output_format,
            "n": 1,
        }
        response = await self._request("POST", "/images/generations", json=payload)
        return self._decode_image(response, output_format)

    async def edit(
        self,
        *,
        image_png: bytes,
        prompt: str,
        size: str,
        quality: str,
        output_format: str,
        mask_png: bytes | None = None,
    ) -> UpstreamImage:
        data = {
            "model": self.settings.sub2api_image_model,
            "prompt": prompt,
            "size": size,
            "quality": quality,
            "output_format": output_format,
            "n": "1",
        }
        files: dict[str, tuple[str, bytes, str]] = {"image": ("image.png", image_png, "image/png")}
        if mask_png is not None:
            files["mask"] = ("mask.png", mask_png, "image/png")
        response = await self._request("POST", "/images/edits", data=data, files=files)
        return self._decode_image(response, output_format)

    async def list_models(self) -> list[str]:
        response = await self._request("GET", "/models")
        payload = response.json()
        return sorted(
            str(item["id"])
            for item in payload.get("data", [])
            if isinstance(item, dict) and item.get("id")
        )

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.sub2api_timeout_seconds,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = await client.request(
                    method,
                    f"{self.settings.normalized_base_url}{path}",
                    headers=self.headers,
                    **kwargs,
                )
        except httpx.RequestError as exc:
            raise Sub2APIError(f"Could not reach Sub2API: {exc.__class__.__name__}.") from exc

        if response.is_error:
            message = f"Sub2API returned HTTP {response.status_code}."
            try:
                error = response.json().get("error", {})
                message = str(error.get("message") or message)
            except (ValueError, AttributeError):
                pass
            raise Sub2APIError(message[:600], status_code=response.status_code)
        return response

    @staticmethod
    def _decode_image(response: httpx.Response, fallback_format: str) -> UpstreamImage:
        try:
            payload = response.json()
            item = payload["data"][0]
            encoded = item["b64_json"]
            image = base64.b64decode(encoded, validate=True)
        except (ValueError, KeyError, IndexError, TypeError, binascii.Error) as exc:
            raise Sub2APIError("Sub2API returned an invalid image response.") from exc

        output_format = str(payload.get("output_format") or fallback_format).lower()
        if output_format not in {"png", "jpeg", "webp"}:
            output_format = fallback_format
        return UpstreamImage(
            data=image,
            output_format=output_format,
            revised_prompt=item.get("revised_prompt"),
        )
