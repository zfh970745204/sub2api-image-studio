from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.admin import router as admin_router_v1
from .api.admin_users import router as admin_users_router
from .api.app import router as user_app_router
from .api.assets import router as assets_router
from .api.auth import router as auth_router
from .api.configuration import router as configuration_router
from .api.errors import install_exception_handlers
from .api.jobs import router as jobs_router
from .api.memberships import router as memberships_router
from .api.middleware import RequestContextMiddleware
from .api.points import router as points_router
from .api.rbac import router as rbac_router
from .api.security import router as security_router
from .api.site import router as site_router
from .api.system import admin_router, public_router, v1_router
from .config import get_settings
from .image_ops import (
    ImageInputError,
    inspect_image,
    normalize_image,
    real_esrgan_available,
    remove_background,
    upscale,
)
from .schemas import Capabilities, GenerateRequest, ImageResult
from .services.auth import AuthService
from .services.logging import configure_logging
from .services.runtime import RuntimeServices
from .services.security import SecurityService
from .storage import ResultStore
from .sub2api import Sub2APIClient, Sub2APIError

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)
if settings.legacy_sync_api_enabled:
    settings.background_model_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("U2NET_HOME", str(settings.background_model_dir))
store: ResultStore | None = (
    ResultStore(settings.result_dir, settings.result_ttl_hours)
    if settings.legacy_sync_api_enabled
    else None
)
sub2api = Sub2APIClient(settings)
processing_slots = asyncio.Semaphore(2)
runtime_services = RuntimeServices(settings)
auth_service = AuthService(settings)
security_service = SecurityService(settings, runtime_services)


@asynccontextmanager
async def lifespan(application: FastAPI):
    stop = asyncio.Event()
    heartbeat_task: asyncio.Task[None] | None = None
    config_listener_task: asyncio.Task[None] | None = None
    if settings.dependency_checks_enabled or not settings.legacy_sync_api_enabled:
        await runtime_services.preload_configuration()
    if settings.dependency_checks_enabled:
        heartbeat_task = asyncio.create_task(runtime_services.heartbeat_loop("web", stop))
        config_listener_task = asyncio.create_task(runtime_services.config_invalidation_loop(stop))
    try:
        yield
    finally:
        stop.set()
        if heartbeat_task is not None:
            await heartbeat_task
        if config_listener_task is not None:
            await config_listener_task
        await runtime_services.close()


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
app.state.settings = settings
app.state.runtime_services = runtime_services
app.state.auth_service = auth_service
app.state.security_service = security_service
app.state.object_storage = runtime_services.object_storage
install_exception_handlers(app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Idempotency-Key", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)
app.add_middleware(RequestContextMiddleware)
app.include_router(public_router)
app.include_router(v1_router)
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(user_app_router)
app.include_router(site_router)
app.include_router(admin_users_router)
app.include_router(admin_router_v1)
app.include_router(rbac_router)
app.include_router(memberships_router)
app.include_router(points_router)
app.include_router(jobs_router)
app.include_router(assets_router)
app.include_router(configuration_router)
app.include_router(security_router)


@app.get("/api/capabilities", response_model=Capabilities)
async def capabilities() -> Capabilities:
    configured = settings.sub2api_configured
    model = settings.sub2api_image_model
    password_reset_enabled = settings.password_reset_enabled
    if not settings.legacy_sync_api_enabled:
        try:
            config = await runtime_services.config_cache.get("sub2api")
            configured = bool(
                config.values.get("enabled")
                and config.values.get("base_url")
                and config.secrets.get("api_key")
            )
            model = str(config.values.get("image_model") or model)
        except Exception:  # noqa: BLE001
            configured = settings.sub2api_configured
        try:
            general = await runtime_services.config_cache.get("general")
            password_reset_enabled = bool(
                general.values.get("password_reset_enabled", password_reset_enabled)
            )
        except Exception:  # noqa: BLE001
            password_reset_enabled = settings.password_reset_enabled
    return Capabilities(
        sub2api_configured=configured,
        sub2api_model=model,
        generation=configured,
        editing=configured,
        local_background_removal=True,
        background_model=settings.background_model,
        ai_upscale_available=real_esrgan_available(),
        password_reset=password_reset_enabled,
    )


@app.get("/api/models")
async def models() -> dict[str, list[str]]:
    _require_legacy_sync_api()
    _require_sub2api()
    try:
        return {"models": await sub2api.list_models()}
    except Sub2APIError as exc:
        raise HTTPException(status_code=_public_status(exc.status_code), detail=str(exc)) from exc


@app.post("/api/generate", response_model=ImageResult)
async def generate(request: GenerateRequest) -> ImageResult:
    _require_legacy_sync_api()
    _require_sub2api()
    try:
        async with processing_slots:
            result = await sub2api.generate(
                prompt=request.prompt,
                size=request.size,
                quality=request.quality,
                output_format=request.output_format,
            )
        return _save_result(
            result.data,
            result.output_format,
            "generate",
            requested_size=request.size,
            revised_prompt=result.revised_prompt,
        )
    except Sub2APIError as exc:
        raise HTTPException(status_code=_public_status(exc.status_code), detail=str(exc)) from exc


@app.post("/api/edit", response_model=ImageResult)
async def edit(
    image: Annotated[UploadFile, File()],
    prompt: Annotated[str, Form(min_length=1, max_length=5000)],
    size: Annotated[str, Form()] = "1024x1024",
    quality: Annotated[str, Form()] = "medium",
    output_format: Annotated[str, Form()] = "png",
    mask: Annotated[UploadFile | None, File()] = None,
) -> ImageResult:
    _require_legacy_sync_api()
    _require_sub2api()
    _validate_options(size, quality, output_format)
    normalized = await _read_and_normalize(image)
    normalized_mask = await _read_and_normalize(mask) if mask else None
    try:
        async with processing_slots:
            result = await sub2api.edit(
                image_png=normalized,
                mask_png=normalized_mask,
                prompt=prompt,
                size=size,
                quality=quality,
                output_format=output_format,
            )
        return _save_result(
            result.data,
            result.output_format,
            "edit",
            requested_size=size,
            revised_prompt=result.revised_prompt,
        )
    except Sub2APIError as exc:
        raise HTTPException(status_code=_public_status(exc.status_code), detail=str(exc)) from exc


@app.post("/api/remove-background", response_model=ImageResult)
async def remove_image_background(
    image: Annotated[UploadFile, File()],
) -> ImageResult:
    _require_legacy_sync_api()
    normalized = await _read_and_normalize(image)
    try:
        async with processing_slots:
            result = await asyncio.to_thread(
                remove_background, normalized, settings.background_model
            )
        return _save_result(result, "png", "remove-background")
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/upscale", response_model=ImageResult)
async def upscale_image(
    image: Annotated[UploadFile, File()],
    scale: Annotated[int, Form()] = 2,
    sharpen: Annotated[bool, Form()] = True,
) -> ImageResult:
    _require_legacy_sync_api()
    normalized = await _read_and_normalize(image)
    try:
        async with processing_slots:
            result = await asyncio.to_thread(upscale, normalized, scale, sharpen)
        return _save_result(
            result,
            "png",
            "upscale",
            warning="采用 Lanczos 保真缩放与适度锐化，不会重绘或改变原图内容。",
        )
    except ImageInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/results/{filename}")
async def get_result(filename: str, download: bool = Query(False)) -> FileResponse:
    active_store = _legacy_store()
    path = active_store.resolve(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Result not found or expired.")
    media_type = _mime_type(path.suffix.lstrip("."))
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name if download else None,
        content_disposition_type="attachment" if download else "inline",
    )


async def _read_and_normalize(upload: UploadFile) -> bytes:
    limit = settings.max_upload_mb * 1024 * 1024
    raw = await upload.read(limit + 1)
    if len(raw) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"Image exceeds the {settings.max_upload_mb} MB upload limit.",
        )
    try:
        return await asyncio.to_thread(
            normalize_image,
            raw,
            max_megapixels=settings.max_image_megapixels,
        )
    except ImageInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _save_result(
    data: bytes,
    output_format: str,
    operation: str,
    *,
    requested_size: str | None = None,
    revised_prompt: str | None = None,
    warning: str | None = None,
) -> ImageResult:
    try:
        width, height, _ = inspect_image(data)
    except OSError as exc:
        raise HTTPException(status_code=502, detail="Image output could not be decoded.") from exc
    active_store = _legacy_store()
    filename, path = active_store.save(data, output_format)
    size_warning = warning
    if requested_size not in (None, "auto") and requested_size != f"{width}x{height}":
        mismatch = f"请求尺寸为 {requested_size}，上游实际返回 {width}×{height}。"
        size_warning = f"{size_warning} {mismatch}".strip() if size_warning else mismatch
    return ImageResult(
        id=filename.rsplit(".", 1)[0],
        operation=operation,
        url=f"/api/results/{filename}",
        download_url=f"/api/results/{filename}?download=true",
        mime_type=_mime_type(output_format),
        width=width,
        height=height,
        size_bytes=path.stat().st_size,
        requested_size=requested_size,
        revised_prompt=revised_prompt,
        warning=size_warning,
    )


def _require_sub2api() -> None:
    if not settings.sub2api_configured:
        raise HTTPException(
            status_code=503,
            detail="Configure SUB2API_BASE_URL and SUB2API_API_KEY on the backend first.",
        )


def _require_legacy_sync_api() -> None:
    if not settings.legacy_sync_api_enabled:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "LEGACY_SYNC_API_DISABLED",
                "message": "同步图片接口已关闭，请使用异步任务接口。",
                "details": None,
            },
        )


def _legacy_store() -> ResultStore:
    _require_legacy_sync_api()
    if store is None:
        raise RuntimeError("Legacy result store is unavailable")
    return store


def _validate_options(size: str, quality: str, output_format: str) -> None:
    if size not in {"auto", "1024x1024", "1024x1536", "1536x1024"}:
        raise HTTPException(status_code=422, detail="Unsupported image size.")
    if quality not in {"auto", "low", "medium", "high"}:
        raise HTTPException(status_code=422, detail="Unsupported quality setting.")
    if output_format not in {"png", "jpeg", "webp"}:
        raise HTTPException(status_code=422, detail="Unsupported output format.")


def _mime_type(output_format: str) -> str:
    return {
        "png": "image/png",
        "jpeg": "image/jpeg",
        "jpg": "image/jpeg",
        "webp": "image/webp",
        "svg": "image/svg+xml",
    }.get(output_format, "application/octet-stream")


def _public_status(status_code: int) -> int:
    if status_code in {400, 401, 403, 404, 409, 413, 422, 429}:
        return status_code
    return 502


if settings.legacy_sync_api_enabled:
    from .workflow import router as workflow_router

    app.include_router(workflow_router)


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.is_dir():

    @app.get("/login", include_in_schema=False)
    @app.get("/forgot-password", include_in_schema=False)
    @app.get("/app", include_in_schema=False)
    @app.get("/app/{path:path}", include_in_schema=False)
    async def user_frontend(path: str = "") -> FileResponse:
        return FileResponse(frontend_dist / "index.html")

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/{path:path}", include_in_schema=False)
    async def admin_frontend(path: str = "") -> FileResponse:
        return FileResponse(frontend_dist / "index.html")

    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
