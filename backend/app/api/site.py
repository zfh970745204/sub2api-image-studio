from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageOps, UnidentifiedImageError

from app.api.dependencies import Principal, require_permission
from app.api.errors import ApiError
from app.repositories.models import SiteMedia
from app.services.configuration import BrandingValues

router = APIRouter(tags=["site"])


@router.get("/api/v1/site")
async def site_branding(request: Request) -> dict[str, str]:
    cache = getattr(request.app.state.runtime_services, "config_cache", None)
    values = {}
    if cache is not None:
        try:
            values = (await cache.get("branding")).values
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).warning("Brand configuration unavailable; using defaults")
    # Explicit allowlist: never expose other configuration groups or secrets.
    defaults = BrandingValues().model_dump()
    return {key: str(values.get(key, default)) for key, default in defaults.items()}


def prepare_site_image(raw: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if (
                image.format not in {"PNG", "JPEG", "WEBP"}
                or image.width * image.height > 40_000_000
            ):
                raise ValueError("请上传不超过 4000 万像素的 PNG、JPEG 或 WebP 图片")
            image = ImageOps.exif_transpose(image).convert("RGBA")
            image.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, "WEBP", quality=85, method=4)
            return output.getvalue()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ValueError("无法读取图片") from exc


@router.post("/api/v1/admin/site-media", status_code=201)
async def upload_site_media(
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("config.manage"))],
    image: Annotated[UploadFile, File()],
) -> dict[str, str]:
    raw = await image.read(12 * 1024 * 1024 + 1)
    if len(raw) > 12 * 1024 * 1024:
        raise ApiError(413, "SITE_IMAGE_TOO_LARGE", "品牌图片不能超过 12 MB")
    try:
        data = await asyncio.to_thread(prepare_site_image, raw)
    except ValueError as exc:
        raise ApiError(422, "INVALID_SITE_IMAGE", str(exc)) from exc
    if len(data) > 2 * 1024 * 1024:
        raise ApiError(413, "SITE_IMAGE_TOO_LARGE", "图片细节过多，请缩小尺寸后上传")
    async with request.app.state.runtime_services.database.session_factory() as session:
        media = SiteMedia(
            data=data, sha256=hashlib.sha256(data).hexdigest(), created_by=principal.user_id
        )
        session.add(media)
        await session.commit()
        media_id = media.id
    return {"url": f"/api/v1/site/media/{media_id}"}


@router.get("/api/v1/site/media/{media_id}")
async def get_site_media(media_id: uuid.UUID, request: Request) -> Response:
    async with request.app.state.runtime_services.database.session_factory() as session:
        media = await session.get(SiteMedia, media_id)
        if media is None:
            raise ApiError(404, "SITE_IMAGE_NOT_FOUND", "品牌图片不存在")
        headers = {
            "ETag": f'"{media.sha256}"',
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        }
        if request.headers.get("if-none-match") == headers["ETag"]:
            return Response(status_code=304, headers=headers)
        return Response(media.data, media_type="image/webp", headers=headers)
