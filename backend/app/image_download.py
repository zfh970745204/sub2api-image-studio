"""Bounded image decoding and SSRF-safe downloads of structured provider results."""

import asyncio
import base64
import binascii
import ipaddress
import socket
from io import BytesIO

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from .image_provider_types import ImageServiceError, UnsupportedImageOperation

MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 48 * 1024 * 1024


def invalid_image() -> ImageServiceError:
    return ImageServiceError("Image service returned an invalid image response.", retryable=False)


def decode_base64(value: str) -> bytes:
    if value.startswith("data:"):
        prefix, _, value = value.partition(",")
        if prefix not in {
            "data:image/png;base64",
            "data:image/jpeg;base64",
            "data:image/webp;base64",
        }:
            raise invalid_image()
    if len(value) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
        raise invalid_image()
    try:
        result = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise invalid_image() from exc
    if not result or len(result) > MAX_IMAGE_BYTES:
        raise invalid_image()
    return result


def normalize_image(raw: bytes, output_format: str) -> bytes:
    if not raw or len(raw) > MAX_IMAGE_BYTES or output_format not in {"png", "jpeg", "webp"}:
        raise invalid_image()
    try:
        with Image.open(BytesIO(raw)) as probe:
            if (
                probe.format not in {"PNG", "JPEG", "WEBP"}
                or probe.width * probe.height > 40_000_000
            ):
                raise invalid_image()
            actual_format = probe.format.lower()
            probe.verify()
        with Image.open(BytesIO(raw)) as source:
            source.load()
            if actual_format == output_format:
                return raw
            image = source.convert("RGBA")
            if output_format == "jpeg":
                background = Image.new("RGBA", image.size, "white")
                image = Image.alpha_composite(background, image).convert("RGB")
            target = BytesIO()
            image.save(target, format=output_format.upper())
            if target.tell() > MAX_IMAGE_BYTES:
                raise invalid_image()
            return target.getvalue()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise invalid_image() from exc


async def public_address(host: str, port: int) -> str:
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
        if not ips or any(not ip.is_global or (ip.version == 6 and ip.ipv4_mapped) for ip in ips):
            raise ValueError("non-public host")
        return str(min(ips, key=lambda ip: ip.version))
    except (OSError, ValueError) as exc:
        raise ImageServiceError("图片服务返回了不可访问的图片地址", retryable=False) from exc


def inpainting_mask(raw: bytes) -> bytes:
    """Our mask marks editable pixels with alpha=0; Stability expects white."""
    with Image.open(BytesIO(raw)) as source:
        if "A" not in source.getbands():
            raise invalid_image()
        output = BytesIO()
        ImageOps.invert(source.getchannel("A")).save(output, format="PNG")
        return output.getvalue()


def opaque_reference(raw: bytes) -> bytes:
    """Wan disallows alpha channels. Never silently invent a background color."""
    with Image.open(BytesIO(raw)) as source:
        if "A" not in source.getbands() and "transparency" not in source.info:
            return raw
        if any(source.convert("RGBA").getchannel("A").histogram()[:255]):
            raise UnsupportedImageOperation("万相不支持透明参考图，请使用支持透明图片的编辑线路")
        output = BytesIO()
        source.convert("RGB").save(output, format="PNG")
        return output.getvalue()


async def download_image(
    url: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30
) -> bytes:
    # Pin the validated IP for the connection, while preserving Host and TLS SNI.
    # Merely checking DNS then asking HTTPX to resolve again permits DNS rebinding.
    async with httpx.AsyncClient(
        timeout=timeout, transport=transport, follow_redirects=False, trust_env=False
    ) as client:
        for _ in range(4):
            try:
                target = httpx.URL(url)
                if (
                    target.scheme not in {"https", "http"}
                    or not target.host
                    or target.userinfo
                    or target.port not in {None, 80, 443}
                    or target.fragment
                ):
                    raise ValueError("invalid image URL")
                address = await public_address(
                    target.host, target.port or (443 if target.scheme == "https" else 80)
                )
                async with client.stream(
                    "GET",
                    target.copy_with(host=address),
                    headers={"Host": target.netloc.decode("ascii"), "Accept": "image/*"},
                    extensions={"sni_hostname": target.host},
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise invalid_image()
                        url = str(target.join(location))
                        continue
                    response.raise_for_status()
                    length = response.headers.get("content-length")
                    if length and int(length) > MAX_IMAGE_BYTES:
                        raise invalid_image()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_IMAGE_BYTES:
                            raise invalid_image()
                    return bytes(data)
            except (httpx.HTTPError, ValueError) as exc:
                raise ImageServiceError("图片结果下载失败，请稍后重试", retryable=False) from exc
    raise ImageServiceError("图片结果重定向次数过多", retryable=False)
