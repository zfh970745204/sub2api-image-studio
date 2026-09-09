from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from .catalog import StudioCatalog
from .config import get_settings
from .image_ops import (
    ImageInputError,
    apply_color_effect,
    build_preflight_report,
    extract_print_artwork,
    has_chroma_key_background,
    inspect_image,
    normalize_image,
    remove_background,
    remove_solid_background,
    restore_print_artwork,
    upscale,
    validate_edit_mask,
    vectorize_artwork,
)
from .schemas import GenerateRequest, ImageResult, JobResult, PreflightReport, PreflightRequest
from .storage import ResultStore
from .sub2api import Sub2APIClient, Sub2APIError

router = APIRouter(prefix="/api")
settings = get_settings()
store = ResultStore(settings.result_dir, settings.result_ttl_hours)
catalog = StudioCatalog(settings.database_path)
sub2api = Sub2APIClient(settings)
processing_slots = asyncio.Semaphore(2)

AI_TRANSFORM_PROMPTS = {
    "faithful-redraw": (
        "Recreate only the artwork visible in the reference as a clean, flat, front-facing "
        "high-resolution source image. Preserve exact text, spelling, line breaks, composition, "
        "proportions, colors, outlines, characters, objects, and small details. Remove fabric, "
        "folds, perspective, lighting, shadows, and the photographed product. Never redesign, "
        "simplify, crop, add, or remove artwork. Place the complete artwork on a perfectly uniform "
        "fully saturated green #00FF00 background, or magenta #FF00FF only if green occurs in the "
        "artwork. Keep white ink white and black ink black."
    ),
    "photo-enhance": (
        "Faithfully restore this image at high resolution. Recover natural edges, faces, hair, "
        "textures, and fine detail while preserving the exact subjects, identity, colors, framing, "
        "background, text, and composition. Remove blur, compression artifacts, noise, and jagged "
        "edges. Do not add, remove, restyle, or rearrange anything."
    ),
    "illustration-enhance": (
        "Faithfully redraw this illustration at high resolution with clean antialiased edges and "
        "crisp intentional detail. Preserve exact composition, text, character identity, line work, "
        "colors, shapes, and background. Remove pixelation, blur, compression artifacts, and broken "
        "lines without redesigning the artwork."
    ),
    "logo-cleanup": (
        "Reconstruct this logo or flat graphic as clean high-resolution artwork. Preserve exact "
        "wording, letter shapes, spacing, geometry, symmetry, proportions, colors, and negative "
        "space. Use flat solid colors and crisp antialiased edges. Do not reinterpret the logo."
    ),
    "line-art": (
        "Reconstruct this as crisp high-resolution line art. Preserve every intentional contour, "
        "shape, word, and interior detail. Repair broken lines, remove pixelation and noise, and keep "
        "line weight visually consistent. Use clean black and white unless the reference clearly "
        "requires another ink color."
    ),
    "text-fix": (
        "Correct the wording in the selected area using exactly the user-provided text. Match the "
        "existing lettering style, weight, perspective, spacing, color, and layout. Preserve every "
        "unselected pixel and all other visual content."
    ),
    "local-repair": (
        "Edit only the selected transparent-mask area according to the user instruction. Preserve "
        "the composition, style, colors, dimensions, and every unselected part of the image. Blend "
        "the repaired boundary cleanly with the surrounding artwork."
    ),
    "recolor": (
        "Change only the requested artwork colors according to the user instruction. Preserve exact "
        "text, shapes, line work, layout, transparency, lighting, and all other content."
    ),
    "variant": (
        "Create a closely related visual variant using the user instruction while preserving the "
        "main subject, overall composition, legibility, and complete uncropped artwork."
    ),
}


@router.post("/assets", response_model=ImageResult)
async def upload_asset(image: Annotated[UploadFile, File()]) -> ImageResult:
    normalized = await _read_and_normalize(image)
    return _save_asset(
        normalized,
        "png",
        "upload",
        metadata={"original_filename": image.filename or "upload"},
    )


@router.get("/assets", response_model=list[ImageResult])
async def list_assets(limit: int = Query(default=30, ge=1, le=200)) -> list[ImageResult]:
    return [_asset_to_result(asset) for asset in catalog.list_assets(limit)]


@router.get("/assets/{asset_id}", response_model=ImageResult)
async def get_asset(asset_id: str) -> ImageResult:
    return _asset_to_result(_get_asset(asset_id))


@router.get("/assets/{asset_id}/lineage", response_model=list[ImageResult])
async def get_lineage(asset_id: str) -> list[ImageResult]:
    try:
        return [_asset_to_result(asset) for asset in catalog.lineage(asset_id)]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="素材不存在。") from exc


@router.get("/jobs", response_model=list[JobResult])
async def list_jobs(limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
    return catalog.list_jobs(limit)


@router.get("/jobs/{job_id}", response_model=JobResult)
async def get_job(job_id: str) -> dict[str, Any]:
    try:
        return catalog.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在。") from exc


@router.post("/chain/generate", response_model=ImageResult)
async def chain_generate(request: GenerateRequest) -> ImageResult:
    if not settings.sub2api_configured:
        raise HTTPException(status_code=503, detail="后端尚未配置 Sub2API。")
    parameters = request.model_dump()
    job = catalog.create_job("generate", None, parameters)
    try:
        async with processing_slots:
            upstream = await sub2api.generate(
                prompt=request.prompt,
                size=request.size,
                quality=request.quality,
                output_format=request.output_format,
            )
        result = _save_asset(
            upstream.data,
            upstream.output_format,
            "generate",
            job_id=job["id"],
            requested_size=request.size,
            revised_prompt=upstream.revised_prompt,
        )
        catalog.complete_job(job["id"], result.id)
        return result
    except Sub2APIError as exc:
        catalog.fail_job(job["id"], str(exc))
        status = exc.status_code if exc.status_code in {400, 401, 403, 413, 422, 429} else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.post("/restore", response_model=ImageResult)
async def restore_image(
    source_asset_id: Annotated[str, Form()],
    mode: Annotated[str, Form()] = "faithful",
    scale: Annotated[int, Form()] = 4,
    denoise: Annotated[int, Form()] = 35,
    deblur: Annotated[int, Form()] = 35,
) -> ImageResult:
    source = _read_asset(source_asset_id)
    parameters = {"mode": mode, "scale": scale, "denoise": denoise, "deblur": deblur}
    job = catalog.create_job("restore", source_asset_id, parameters)
    try:
        async with processing_slots:
            data, metadata = await asyncio.to_thread(
                restore_print_artwork,
                source,
                mode=mode,
                scale=scale,
                denoise=denoise,
                deblur=deblur,
            )
        warning = (
            "神经网络已重建缺失纹理；文字、Logo 和关键轮廓必须与来源图核对。"
            if metadata["generative_detail_reconstruction"]
            else "已按色块与轮廓重建；请检查渐变和细小文字。"
        )
        result = _save_asset(
            data,
            "png",
            "restore",
            parent_id=source_asset_id,
            job_id=job["id"],
            warning=warning,
            metadata=metadata,
        )
        catalog.complete_job(job["id"], result.id)
        return result
    except (ImageInputError, RuntimeError) as exc:
        catalog.fail_job(job["id"], str(exc))
        raise HTTPException(
            status_code=422 if isinstance(exc, ImageInputError) else 503,
            detail=str(exc),
        ) from exc


@router.post("/extract-print", response_model=ImageResult)
async def extract_print(
    source_asset_id: Annotated[str, Form()],
    crop_x: Annotated[float, Form()] = 0,
    crop_y: Annotated[float, Form()] = 0,
    crop_width: Annotated[float, Form()] = 1,
    crop_height: Annotated[float, Form()] = 1,
    background_tolerance: Annotated[int, Form()] = 35,
    texture_reduction: Annotated[int, Form()] = 45,
    shadow_reduction: Annotated[int, Form()] = 55,
    edge_cleanup: Annotated[int, Form()] = 35,
) -> ImageResult:
    source = _read_asset(source_asset_id)
    parameters = {
        "crop": [crop_x, crop_y, crop_width, crop_height],
        "background_tolerance": background_tolerance,
        "texture_reduction": texture_reduction,
        "shadow_reduction": shadow_reduction,
        "edge_cleanup": edge_cleanup,
    }
    job = catalog.create_job("extract-print", source_asset_id, parameters)
    try:
        async with processing_slots:
            data, metadata = await asyncio.to_thread(
                extract_print_artwork,
                source,
                crop=(crop_x, crop_y, crop_width, crop_height),
                background_tolerance=background_tolerance,
                texture_reduction=texture_reduction,
                shadow_reduction=shadow_reduction,
                edge_cleanup=edge_cleanup,
            )
        result = _save_asset(
            data,
            "png",
            "extract-print",
            parent_id=source_asset_id,
            job_id=job["id"],
            warning="自动分离以选区边缘估算衣服底色；相近色印花请放大检查并调整容差。",
            metadata=metadata,
        )
        catalog.complete_job(job["id"], result.id)
        return result
    except ImageInputError as exc:
        catalog.fail_job(job["id"], str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/chain/remove-background", response_model=ImageResult)
async def chain_remove_background(
    source_asset_id: Annotated[str, Form()],
) -> ImageResult:
    source = _read_asset(source_asset_id)
    job = catalog.create_job("remove-background", source_asset_id, {})
    try:
        async with processing_slots:
            data = await asyncio.to_thread(remove_background, source, settings.background_model)
        result = _save_asset(
            data,
            "png",
            "remove-background",
            parent_id=source_asset_id,
            job_id=job["id"],
        )
        catalog.complete_job(job["id"], result.id)
        return result
    except RuntimeError as exc:
        catalog.fail_job(job["id"], str(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/chain/upscale", response_model=ImageResult)
async def chain_upscale(
    source_asset_id: Annotated[str, Form()],
    scale: Annotated[int, Form()] = 2,
    sharpen: Annotated[bool, Form()] = True,
) -> ImageResult:
    source = _read_asset(source_asset_id)
    parameters = {"scale": scale, "sharpen": sharpen}
    job = catalog.create_job("upscale", source_asset_id, parameters)
    try:
        async with processing_slots:
            data = await asyncio.to_thread(upscale, source, scale, sharpen)
        result = _save_asset(
            data,
            "png",
            "upscale",
            parent_id=source_asset_id,
            job_id=job["id"],
            warning="已完成保真插值放大；该操作不会生成原图中不存在的新细节。",
            metadata={
                "engine": "lanczos-unsharp",
                "scale": scale,
                "sharpen": sharpen,
            },
        )
        catalog.complete_job(job["id"], result.id)
        return result
    except (ImageInputError, OSError) as exc:
        catalog.fail_job(job["id"], str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/chain/remove-solid-background", response_model=ImageResult)
async def chain_remove_solid_background(
    source_asset_id: Annotated[str, Form()],
) -> ImageResult:
    source = _read_asset(source_asset_id)
    job = catalog.create_job("remove-background", source_asset_id, {"method": "solid"})
    try:
        async with processing_slots:
            data, metadata = await asyncio.to_thread(remove_solid_background, source)
        result = _save_asset(
            data,
            "png",
            "remove-background",
            parent_id=source_asset_id,
            job_id=job["id"],
            metadata=metadata,
        )
        catalog.complete_job(job["id"], result.id)
        return result
    except (ImageInputError, OSError) as exc:
        catalog.fail_job(job["id"], str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/chain/smart-cutout", response_model=ImageResult)
async def chain_smart_cutout(
    source_asset_id: Annotated[str, Form()],
) -> ImageResult:
    source = _read_asset(source_asset_id)
    use_chroma_key = await asyncio.to_thread(has_chroma_key_background, source)
    parameters = {"method": "chroma-key" if use_chroma_key else "subject"}
    job = catalog.create_job("remove-background", source_asset_id, parameters)
    try:
        async with processing_slots:
            if use_chroma_key:
                data, metadata = await asyncio.to_thread(remove_solid_background, source)
            else:
                data = await asyncio.to_thread(remove_background, source, settings.background_model)
                metadata = {"method": "subject-segmentation"}
        result = _save_asset(
            data,
            "png",
            "remove-background",
            parent_id=source_asset_id,
            job_id=job["id"],
            metadata=metadata,
        )
        catalog.complete_job(job["id"], result.id)
        return result
    except (ImageInputError, OSError, RuntimeError) as exc:
        catalog.fail_job(job["id"], str(exc))
        raise HTTPException(
            status_code=422 if isinstance(exc, (ImageInputError, OSError)) else 503,
            detail=str(exc),
        ) from exc


@router.post("/chain/ai-reconstruct", response_model=ImageResult)
async def chain_ai_reconstruct(
    source_asset_id: Annotated[str, Form()],
    prompt: Annotated[str, Form(min_length=1, max_length=5000)],
    size: Annotated[str, Form()] = "auto",
    quality: Annotated[str, Form()] = "high",
) -> ImageResult:
    if not settings.sub2api_configured:
        raise HTTPException(status_code=503, detail="后端尚未配置 Sub2API。")
    if size not in {"auto", "1024x1024", "1024x1536", "1536x1024"}:
        raise HTTPException(status_code=422, detail="不支持的图片尺寸。")
    if quality not in {"auto", "low", "medium", "high"}:
        raise HTTPException(status_code=422, detail="不支持的质量设置。")
    source = _read_asset(source_asset_id)
    parameters = {"prompt": prompt, "size": size, "quality": quality}
    job = catalog.create_job("edit", source_asset_id, parameters)
    try:
        async with processing_slots:
            upstream = await sub2api.edit(
                image_png=source,
                prompt=prompt,
                size=size,
                quality=quality,
                output_format="png",
            )
        result = _save_asset(
            upstream.data,
            "png",
            "edit",
            parent_id=source_asset_id,
            job_id=job["id"],
            requested_size=size,
            revised_prompt=upstream.revised_prompt,
            warning="AI 重建可能改变文字、Logo、人物与细节，请与来源图放大对照。",
            metadata={"ai_reconstruction": True},
        )
        catalog.complete_job(job["id"], result.id)
        return result
    except Sub2APIError as exc:
        catalog.fail_job(job["id"], str(exc))
        status = exc.status_code if exc.status_code in {400, 401, 403, 413, 422, 429} else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.post("/chain/ai-transform", response_model=ImageResult)
async def chain_ai_transform(
    source_asset_id: Annotated[str, Form()],
    mode: Annotated[str, Form()],
    instruction: Annotated[str, Form(max_length=2000)] = "",
    mask: Annotated[UploadFile | None, File()] = None,
) -> ImageResult:
    if not settings.sub2api_configured:
        raise HTTPException(status_code=503, detail="后端尚未配置 Sub2API。")
    if mode not in AI_TRANSFORM_PROMPTS:
        raise HTTPException(status_code=422, detail="不支持的 AI 图片处理方式。")
    instruction = instruction.strip()
    if mode in {"text-fix", "local-repair", "recolor", "variant"} and not instruction:
        raise HTTPException(status_code=422, detail="请填写处理要求。")

    source = _read_asset(source_asset_id)
    normalized_mask = await _read_and_normalize(mask) if mask else None
    mask_metadata: dict[str, Any] = {}
    if normalized_mask is not None:
        try:
            mask_metadata = await asyncio.to_thread(validate_edit_mask, source, normalized_mask)
        except ImageInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if mode in {"text-fix", "local-repair"} and normalized_mask is None:
        raise HTTPException(status_code=422, detail="请先涂抹需要修改的区域。")

    prompt = AI_TRANSFORM_PROMPTS[mode]
    if instruction:
        prompt = f"{prompt}\nUser instruction: {instruction}"
    parameters = {"mode": mode, "instruction": instruction, "masked": mask is not None}
    job = catalog.create_job("edit", source_asset_id, parameters)
    try:
        async with processing_slots:
            upstream = await sub2api.edit(
                image_png=source,
                mask_png=normalized_mask,
                prompt=prompt,
                size="auto",
                quality="high",
                output_format="png",
            )
        metadata = {"ai_mode": mode, **mask_metadata}
        result = _save_asset(
            upstream.data,
            "png",
            "edit",
            parent_id=source_asset_id,
            job_id=job["id"],
            requested_size="auto",
            revised_prompt=upstream.revised_prompt,
            warning="AI 图片编辑可能改变文字、Logo 或精细结构，请与上一版本对照。",
            metadata=metadata,
        )
        catalog.complete_job(job["id"], result.id)
        return result
    except Sub2APIError as exc:
        catalog.fail_job(job["id"], str(exc))
        status = exc.status_code if exc.status_code in {400, 401, 403, 413, 422, 429} else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.post("/chain/color-effect", response_model=ImageResult)
async def chain_color_effect(
    source_asset_id: Annotated[str, Form()],
    mode: Annotated[str, Form()],
    color: Annotated[str, Form()] = "#111111",
) -> ImageResult:
    source = _read_asset(source_asset_id)
    parameters = {"mode": mode, "color": color}
    job = catalog.create_job("color-effect", source_asset_id, parameters)
    try:
        async with processing_slots:
            data, metadata = await asyncio.to_thread(
                apply_color_effect, source, mode=mode, color=color
            )
        result = _save_asset(
            data,
            "png",
            "color-effect",
            parent_id=source_asset_id,
            job_id=job["id"],
            metadata=metadata,
        )
        catalog.complete_job(job["id"], result.id)
        return result
    except (ImageInputError, OSError) as exc:
        catalog.fail_job(job["id"], str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/chain/vectorize", response_model=ImageResult)
async def chain_vectorize(
    source_asset_id: Annotated[str, Form()],
) -> ImageResult:
    source = _read_asset(source_asset_id)
    job = catalog.create_job("vectorize", source_asset_id, {"max_colors": 6})
    try:
        async with processing_slots:
            svg, metadata = await asyncio.to_thread(vectorize_artwork, source, max_colors=6)
        svg_filename, _ = store.save(svg, "svg")
        metadata["svg_download_url"] = f"/api/results/{svg_filename}?download=true"
        result = _save_asset(
            source,
            "png",
            "vectorize",
            parent_id=source_asset_id,
            job_id=job["id"],
            metadata=metadata,
        )
        catalog.complete_job(job["id"], result.id)
        return result
    except (ImageInputError, OSError) as exc:
        catalog.fail_job(job["id"], str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/preflight", response_model=PreflightReport)
async def preflight(request: PreflightRequest) -> dict[str, Any]:
    source = _read_asset(request.asset_id)
    return await asyncio.to_thread(
        build_preflight_report,
        source,
        asset_id=request.asset_id,
        target_width_cm=request.target_width_cm,
        target_dpi=request.target_dpi,
    )


async def _read_and_normalize(upload: UploadFile) -> bytes:
    limit = settings.max_upload_mb * 1024 * 1024
    raw = await upload.read(limit + 1)
    if len(raw) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"图片超过 {settings.max_upload_mb} MB 上传限制。",
        )
    try:
        return await asyncio.to_thread(
            normalize_image,
            raw,
            max_megapixels=settings.max_image_megapixels,
        )
    except ImageInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _read_asset(asset_id: str) -> bytes:
    asset = _get_asset(asset_id)
    path = store.resolve(asset["filename"])
    if path is None:
        raise HTTPException(status_code=404, detail="素材文件已丢失。")
    return path.read_bytes()


def _get_asset(asset_id: str) -> dict[str, Any]:
    try:
        return catalog.get_asset(asset_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="素材不存在。") from exc


def _save_asset(
    data: bytes,
    output_format: str,
    operation: str,
    *,
    parent_id: str | None = None,
    job_id: str | None = None,
    requested_size: str | None = None,
    revised_prompt: str | None = None,
    warning: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ImageResult:
    try:
        width, height, _ = inspect_image(data)
    except OSError as exc:
        raise HTTPException(status_code=502, detail="无法解码处理结果。") from exc
    filename, path = store.save(data, output_format)
    if requested_size not in {None, "auto"} and requested_size != f"{width}x{height}":
        mismatch = f"请求尺寸为 {requested_size}，上游实际返回 {width}×{height}。"
        warning = f"{warning} {mismatch}".strip() if warning else mismatch
    complete_metadata = dict(metadata or {})
    complete_metadata.update(
        {
            "requested_size": requested_size,
            "revised_prompt": revised_prompt,
            "warning": warning,
        }
    )
    asset = catalog.add_asset(
        filename=filename,
        operation=operation,
        parent_id=parent_id,
        job_id=job_id,
        mime_type=_mime_type(output_format),
        width=width,
        height=height,
        size_bytes=path.stat().st_size,
        data=data,
        metadata=complete_metadata,
    )
    return _asset_to_result(asset)


def _asset_to_result(asset: dict[str, Any]) -> ImageResult:
    metadata = asset["metadata"]
    return ImageResult(
        id=asset["id"],
        operation=asset["operation"],
        url=f"/api/results/{asset['filename']}",
        download_url=f"/api/results/{asset['filename']}?download=true",
        mime_type=asset["mime_type"],
        width=asset["width"],
        height=asset["height"],
        size_bytes=asset["size_bytes"],
        requested_size=metadata.get("requested_size"),
        revised_prompt=metadata.get("revised_prompt"),
        warning=metadata.get("warning"),
        parent_id=asset["parent_id"],
        root_id=asset["root_id"],
        job_id=asset["job_id"],
        created_at=asset["created_at"],
        metadata=metadata,
    )


def _mime_type(output_format: str) -> str:
    return {
        "png": "image/png",
        "jpeg": "image/jpeg",
        "jpg": "image/jpeg",
        "webp": "image/webp",
        "svg": "image/svg+xml",
    }.get(output_format, "application/octet-stream")
