from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from sqlalchemy import update

from app.api.errors import ApiError
from app.config import Settings
from app.image_ops import (
    ImageInputError,
    apply_color_effect,
    finalize_print_extraction,
    has_chroma_key_background,
    print_extraction_key_color,
    remove_background,
    remove_solid_background,
    upscale,
    validate_edit_mask,
    vectorize_artwork,
)
from app.object_storage import ObjectStorage, ObjectStorageError
from app.repositories.models import ImageJob
from app.services.asset_files import AssetInputError, prepare_asset
from app.services.assets import AssetService
from app.services.configuration import (
    RuntimeConfigCache,
    sub2api_profile_settings,
)
from app.services.jobs import ClaimedJob, PermanentJobError, RetryableJobError
from app.services.security import SecurityService
from app.sub2api import Sub2APIClient, Sub2APIError

AI_EDIT_PROMPTS = {
    # The detailed extraction prompt is composed with a source-safe key color in _execute.
    "ai.extract_print": "Extract the print faithfully; source-safe instructions are applied at execution.",
    "ai.redraw": (
        "Faithfully restore the complete source image at high resolution. Recover natural edges, "
        "textures and fine detail, removing blur, compression artifacts, noise and jagged edges. "
        "Preserve exact text, layout, colors, geometry, subjects, background, framing, perspective "
        "and all intentional details. Do not extract artwork from a product, remove backgrounds, "
        "flatten perspective, redesign, add or remove content."
    ),
    "ai.repair": (
        "Repair only the area selected by the mask according to the user instruction. Preserve every "
        "unselected pixel and blend the repaired boundary cleanly."
    ),
    "ai.text_fix": (
        "Correct only the selected text using the exact requested wording. Match the existing style, "
        "spacing, color, perspective, and layout. Preserve all unselected content."
    ),
    "ai.variant": (
        "Create a closely related visual variant while preserving the main subject, legibility, "
        "overall composition, and complete uncropped artwork."
    ),
}


class UpstreamCircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_seconds: int = 60) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.consecutive_failures = 0
        self.opened_until = 0.0

    def before_call(self) -> None:
        if self.opened_until > time.monotonic():
            raise RetryableJobError("SUB2API_CIRCUIT_OPEN", "上游图片服务熔断保护中")
        if self.opened_until:
            self.opened_until = 0.0
            self.consecutive_failures = 0

    def succeeded(self) -> None:
        self.consecutive_failures = 0
        self.opened_until = 0.0

    def failed(self) -> bool:
        self.consecutive_failures += 1
        if self.consecutive_failures < self.failure_threshold:
            return False
        self.opened_until = time.monotonic() + self.recovery_seconds
        return True

    def snapshot(self) -> dict[str, int | bool]:
        remaining = max(0, round(self.opened_until - time.monotonic()))
        return {
            "open": remaining > 0,
            "consecutive_failures": self.consecutive_failures,
            "retry_after_seconds": remaining,
        }


class ImageJobExecutor:
    def __init__(
        self,
        settings: Settings,
        database,
        storage: ObjectStorage,
        *,
        sub2api: Sub2APIClient | None = None,
        config_cache: RuntimeConfigCache | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.storage = storage
        self.assets = AssetService()
        self.sub2api = sub2api or Sub2APIClient(settings)
        self.config_cache = config_cache
        self.circuit_breaker = UpstreamCircuitBreaker()
        self.profile_breakers: dict[str, UpstreamCircuitBreaker] = {"default": self.circuit_breaker}

    async def __call__(self, claim: ClaimedJob) -> dict[str, Any]:
        started = time.perf_counter()
        await self._progress(claim, 8)
        source_data = await self._source_data(claim)
        staged = []
        finished = False
        provider_request_id = None
        try:
            await self._progress(claim, 20)
            count = (
                int(claim.parameters.get("image_count", 1))
                if claim.operation_code == "ai.ecommerce"
                else 1
            )
            async for output, extension, provider_request_id, metadata in self._outputs(
                claim, source_data
            ):
                prepared = await asyncio.to_thread(
                    prepare_asset,
                    output,
                    kind="vector" if extension == "svg" else "result",
                    max_megapixels=200,
                )
                asset = await self.assets.store(
                    self.database,
                    self.storage,
                    owner_id=claim.user_id,
                    prepared=prepared,
                    kind="vector" if prepared.extension == "svg" else "result",
                    operation_code=claim.operation_code,
                    retention_days=claim.retention_days,
                    parent_asset_id=claim.source_asset_id
                    or (
                        uuid.UUID(claim.parameters["reference_asset_ids"][0])
                        if claim.parameters.get("reference_asset_ids")
                        else None
                    ),
                    source_job_id=claim.job_id,
                    metadata=metadata,
                    publish=False,
                    request_id=f"job:{claim.job_id}",
                )
                staged.append(asset)
                await self._progress(claim, 20 + round(72 * len(staged) / count))
            finished = True
        except RetryableJobError as exc:
            if exc.code == "SUB2API_CIRCUIT_OPEN":
                async with self.database.session_factory() as session:
                    SecurityService.record_event(
                        session,
                        event_type="sub2api_circuit_open",
                        severity="critical",
                        user_id=claim.user_id,
                        ip_hash=None,
                        request_id=f"job:{claim.job_id}",
                        details=self.circuit_breaker.snapshot(),
                    )
                    await session.commit()
            raise
        except ApiError as exc:
            raise PermanentJobError("ASSET_STATE_CHANGED", exc.message) from exc
        except (AssetInputError, ImageInputError, OSError, ValueError) as exc:
            raise PermanentJobError("INVALID_IMAGE_OPERATION", str(exc)) from exc
        except ObjectStorageError as exc:
            raise RetryableJobError("OBJECT_STORAGE_UNAVAILABLE", "对象存储暂时不可用") from exc
        except RuntimeError as exc:
            raise PermanentJobError("IMAGE_ENGINE_UNAVAILABLE", str(exc)) from exc
        finally:
            if not finished:
                # Also runs for cancellation/timeouts; never publish or charge for a partial set.
                for item in staged:
                    await self.discard_output(item.id)
        await self._progress(claim, 96)
        return {
            "output_asset_id": staged[0].id,
            "output_asset_ids": [item.id for item in staged],
            "provider_request_id": provider_request_id,
            "metrics": {
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "output_size_bytes": sum(item.size_bytes for item in staged),
                "output_count": len(staged),
            },
        }

    async def _progress(self, claim: ClaimedJob, progress: int) -> None:
        async with self.database.session_factory() as session:
            await session.execute(
                update(ImageJob)
                .where(
                    ImageJob.id == claim.job_id,
                    ImageJob.status == "running",
                    ImageJob.attempt_count == claim.attempt_no,
                    ImageJob.progress < progress,
                )
                .values(progress=progress)
            )
            await session.commit()

    async def publish_output(self, asset_id: uuid.UUID, job_id: uuid.UUID) -> bool:
        return await self.assets.publish_job_output(self.database, asset_id=asset_id, job_id=job_id)

    async def discard_output(self, asset_id: uuid.UUID) -> None:
        async with self.database.session_factory() as session:
            try:
                await self.assets.soft_delete(
                    session,
                    asset_id=asset_id,
                    owner_id=None,
                    grace_days=1,
                    immediate=True,
                )
                await session.commit()
            except ApiError:
                await session.rollback()
                return
        await self.assets.process_deletion_queue(self.database, self.storage, limit=1)

    async def _source_data(self, claim: ClaimedJob) -> bytes | None:
        if claim.source_asset_id is None:
            return None
        async with self.database.session_factory() as session:
            try:
                asset = await self.assets.require_usable(
                    session, claim.source_asset_id, owner_id=claim.user_id
                )
            except ApiError as exc:
                raise PermanentJobError("SOURCE_ASSET_NOT_FOUND", "来源素材不可用") from exc
        try:
            data = await self.storage.get_object(asset.object_key)
        except ObjectStorageError as exc:
            raise RetryableJobError("OBJECT_STORAGE_UNAVAILABLE", "对象存储暂时不可用") from exc
        if hashlib.sha256(data).hexdigest() != asset.sha256:
            raise PermanentJobError("SOURCE_ASSET_INTEGRITY_ERROR", "来源素材完整性校验失败")
        return data

    async def _execute(
        self, claim: ClaimedJob, source: bytes | None
    ) -> tuple[bytes, str, str | None, dict[str, Any]]:
        operation = claim.operation_code
        parameters = claim.parameters
        if operation == "ai.generate":
            clients = await self._sub2api_clients(claim)
            refs = await self._references(claim, source)
            prompt = (
                str(parameters.get("prompt", "")).strip()
                or "Create a polished image using the supplied references. Preserve the subject's identity and design."
            )
            size = self._choice(
                parameters, "size", "1024x1024", {"auto", "1024x1024", "1024x1536", "1536x1024"}
            )
            quality = self._choice(
                parameters, "quality", "medium", {"auto", "low", "medium", "high"}
            )
            output_format = self._choice(
                parameters, "output_format", "png", {"png", "jpeg", "webp"}
            )
            upstream = await self._call_with_failover(
                clients,
                lambda client: (
                    client.edit(
                        image_png=refs[0],
                        reference_images=refs[1:],
                        prompt=prompt,
                        size=size,
                        quality=quality,
                        output_format=output_format,
                    )
                    if refs
                    else client.generate(
                        prompt=prompt, size=size, quality=quality, output_format=output_format
                    )
                ),
            )
            return (
                upstream.data,
                upstream.output_format,
                None,
                {"requested_size": size, "revised_prompt": upstream.revised_prompt},
            )
        if operation in AI_EDIT_PROMPTS:
            self._require_source(source)
            clients = await self._sub2api_clients(claim)
            instruction = str(
                parameters.get("instruction") or parameters.get("prompt") or ""
            ).strip()
            prompt = AI_EDIT_PROMPTS[operation]
            key_color = None
            if operation == "ai.extract_print":
                key_color = await asyncio.to_thread(print_extraction_key_color, source)
                prompt = (
                    "Extract and flatten only the complete printed artwork from this product photo. "
                    "Preserve the exact text, eye colors, ink hues, saturation, brightness, composition "
                    "and fine detail. Remove the photographed product, folds, texture, lighting and "
                    "perspective without redesigning or recoloring the artwork. Do not add contrast, "
                    "orange warmth, cyan eyes or darken green ink. Keep white and black ink intact. "
                    f"Use ONLY a perfectly uniform {key_color} background with a small clear margin. "
                    "This color is a removable background, never a replacement for design colors. "
                    "Keep foreground edge colors free of background reflection or colored outlines; "
                    "retain fine hair, thin strokes and translucent details with natural antialiasing. "
                    "No mockup, checkerboard, ground plane, shadow or added elements."
                )
            if instruction:
                prompt = f"{prompt}\nUser instruction: {instruction}"
            mask = await self._mask_data(claim)
            if operation in {"ai.repair", "ai.text_fix"} and mask is None:
                raise PermanentJobError("MASK_REQUIRED", "该操作必须提供 mask_asset_id")
            if mask is not None:
                await asyncio.to_thread(validate_edit_mask, source, mask)
            upstream = await self._call_with_failover(
                clients,
                lambda client: client.edit(
                    image_png=source,
                    mask_png=mask,
                    prompt=prompt,
                    size=self._choice(
                        parameters,
                        "size",
                        "auto",
                        {"auto", "1024x1024", "1024x1536", "1536x1024"},
                    ),
                    quality=self._choice(
                        parameters, "quality", "high", {"auto", "low", "medium", "high"}
                    ),
                    output_format="png",
                ),
            )
            await self._progress(claim, 65)
            if operation == "ai.extract_print":
                output, metadata = await asyncio.to_thread(
                    finalize_print_extraction, upstream.data, key_color=key_color
                )
                return (
                    output,
                    "png",
                    None,
                    {
                        **metadata,
                        "revised_prompt": upstream.revised_prompt,
                        "workflow": "faithful-product-print-extraction",
                        "extraction_key_color": key_color,
                    },
                )
            return (
                upstream.data,
                upstream.output_format,
                None,
                {"revised_prompt": upstream.revised_prompt},
            )
        if operation == "cutout.smart":
            self._require_source(source)
            if await asyncio.to_thread(has_chroma_key_background, source):
                output, metadata = await asyncio.to_thread(remove_solid_background, source)
            else:
                output = await asyncio.to_thread(
                    remove_background, source, self.settings.background_model
                )
                metadata = {"method": "subject-segmentation"}
            return output, "png", None, metadata
        if operation in {"upscale.2x", "upscale.4x"}:
            self._require_source(source)
            scale = 2 if operation.endswith("2x") else 4
            output = await asyncio.to_thread(
                upscale, source, scale, bool(parameters.get("sharpen", True))
            )
            return output, "png", None, {"scale": scale}
        if operation == "color.effect":
            self._require_source(source)
            output, metadata = await asyncio.to_thread(
                apply_color_effect,
                source,
                mode=str(parameters.get("mode", "grayscale")),
                color=str(parameters.get("color", "#111111")),
            )
            return output, "png", None, metadata
        if operation == "vectorize.svg":
            self._require_source(source)
            max_colors = int(parameters.get("max_colors", 6))
            output, metadata = await asyncio.to_thread(
                vectorize_artwork, source, max_colors=max_colors
            )
            return output, "svg", None, metadata
        raise PermanentJobError("OPERATION_EXECUTOR_MISSING", "图片操作没有可用执行器")

    async def _references(self, claim: ClaimedJob, source: bytes | None) -> list[bytes]:
        ids = list(
            dict.fromkeys(
                ([str(claim.source_asset_id)] if claim.source_asset_id else [])
                + list(claim.parameters.get("reference_asset_ids", []))
            )
        )
        images = []
        for asset_id in ids:
            data = (
                source
                if str(claim.source_asset_id) == asset_id
                else await self._source_data(replace(claim, source_asset_id=uuid.UUID(asset_id)))
            )
            if data is not None:
                # Stored user images are normalized PNGs by AssetService.
                images.append(data)
        return images

    async def _outputs(self, claim: ClaimedJob, source: bytes | None):
        if claim.operation_code != "ai.ecommerce":
            yield await self._execute(claim, source)
            return
        refs = await self._references(claim, source)
        clients = await self._sub2api_clients(claim)
        parameters = claim.parameters
        count = int(parameters.get("image_count", 1))
        platform = parameters.get("platform", "amazon")
        briefs = {
            "amazon": "Amazon style. First image: pure white RGB255 background, product only, centered and uncropped, no props or text. Subsequent images: clean detail or use-context photography, no invented claims.",
            "etsy": "Etsy style: tactile materials, warm natural daylight, considered handmade-product styling. Keep the actual item prominent.",
            "shopify": "Shopify brand storefront: refined editorial photography, restrained palette, consistent studio lighting.",
            "taobao": "Taobao/Tmall product listing: clean commercial photography, immediately legible product silhouette, deliberate negative space.",
            "jd": "JD product listing: precise materials, uncluttered studio photography, realistic proportions, clean background.",
            "douyin": "Douyin product listing: natural contemporary lifestyle scene, strong product visibility, clean mobile-friendly composition.",
        }
        shots = [
            "hero product view",
            "closer material and print detail",
            "natural use-context view",
            "alternate crop of the same visible side",
            "studio composition",
            "close texture detail",
            "minimal lifestyle composition",
            "final catalog view",
        ]
        anchor = None
        for index in range(count):
            prompt = (
                f"Create ONE separate ecommerce product photograph, image {index + 1} of {count}: {shots[index]}. "
                f"{briefs[platform]} User brief: {parameters.get('prompt', '')}. "
                "All input photos describe the SAME product. Preserve exact design, print, lettering, color, proportions, materials and distinctive details. "
                "When the final input is a generated hero image, treat it as the visual identity anchor for this collection. "
                "Change only composition, background and lighting as appropriate. Do not invent unseen product features, accessories, labels or specifications. "
                "No collage, contact sheet, frame, watermark, marketing text or badges. Produce a single image filling the canvas."
            )
            images = [*refs, *([anchor] if anchor is not None else [])]
            options = {
                "prompt": prompt,
                "size": str(parameters.get("size", "1024x1024")),
                "quality": str(parameters.get("quality", "high")),
                "output_format": "png",
            }
            if images:
                primary, extra_images = images[0], images[1:]
                upstream = await self._call_with_failover(
                    clients,
                    lambda client, primary=primary, extra_images=extra_images, options=options: (
                        client.edit(image_png=primary, reference_images=extra_images, **options)
                    ),
                )
            else:
                upstream = await self._call_with_failover(
                    clients,
                    lambda client, options=options: client.generate(**options),
                )
            if anchor is None:
                anchor = upstream.data
            yield (
                upstream.data,
                "png",
                None,
                {
                    "platform": platform,
                    "image_index": index + 1,
                    "image_count": count,
                    "reference_asset_ids": parameters.get("reference_asset_ids", []),
                    "identity_anchor": "first-output",
                    "revised_prompt": upstream.revised_prompt,
                },
            )

    async def _mask_data(self, claim: ClaimedJob) -> bytes | None:
        raw_id = claim.parameters.get("mask_asset_id")
        if not raw_id:
            return None
        try:
            mask_id = uuid.UUID(str(raw_id))
        except ValueError as exc:
            raise PermanentJobError("INVALID_MASK_ASSET", "mask_asset_id 无效") from exc
        async with self.database.session_factory() as session:
            try:
                mask = await self.assets.require_usable(session, mask_id, owner_id=claim.user_id)
            except ApiError as exc:
                raise PermanentJobError("MASK_ASSET_NOT_FOUND", "遮罩素材不可用") from exc
            if mask.kind != "mask":
                raise PermanentJobError("INVALID_MASK_ASSET", "指定素材不是遮罩")
        try:
            data = await self.storage.get_object(mask.object_key)
        except ObjectStorageError as exc:
            raise RetryableJobError("OBJECT_STORAGE_UNAVAILABLE", "对象存储暂时不可用") from exc
        if hashlib.sha256(data).hexdigest() != mask.sha256:
            raise PermanentJobError("MASK_ASSET_INTEGRITY_ERROR", "遮罩素材完整性校验失败")
        return data

    def _breaker(self, profile_id: str) -> UpstreamCircuitBreaker:
        if profile_id not in self.profile_breakers:
            self.profile_breakers[profile_id] = UpstreamCircuitBreaker()
        return self.profile_breakers[profile_id]

    async def _call_with_failover(
        self,
        clients: list[tuple[str, Sub2APIClient]],
        call: Callable[[Sub2APIClient], Awaitable[Any]],
    ) -> Any:
        last_error: RetryableJobError | None = None
        for profile_id, client in clients:
            try:
                return await self._upstream(call(client), profile_id=profile_id)
            except RetryableJobError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise PermanentJobError("SUB2API_NOT_CONFIGURED", "Sub2API 尚未配置")

    async def _upstream(self, awaitable, *, profile_id: str = "default"):
        breaker = self._breaker(profile_id)
        try:
            breaker.before_call()
        except RetryableJobError:
            close = getattr(awaitable, "close", None)
            if close is not None:
                close()
            raise
        try:
            result = await awaitable
        except Sub2APIError as exc:
            message = str(exc).lower()
            transient = (
                exc.status_code in {402, 408, 425, 429}
                or exc.status_code >= 500
                or any(
                    word in message
                    for word in (
                        "quota",
                        "rate limit",
                        "ratelimit",
                        "credit",
                        "balance",
                        "insufficient",
                        "limit reached",
                    )
                )
            )
            if transient:
                if breaker.failed():
                    raise RetryableJobError(
                        "SUB2API_CIRCUIT_OPEN", "上游图片服务熔断保护中"
                    ) from exc
                raise RetryableJobError("SUB2API_UNAVAILABLE", "上游图片服务暂时不可用") from exc
            raise PermanentJobError("SUB2API_REQUEST_REJECTED", "上游图片请求被拒绝") from exc
        breaker.succeeded()
        return result

    async def _sub2api_clients(self, claim: ClaimedJob) -> list[tuple[str, Sub2APIClient]]:
        if self.config_cache is not None and claim.sub2api_config_version is not None:
            try:
                config = await self.config_cache.get("sub2api", claim.sub2api_config_version)
            except Exception as exc:
                raise PermanentJobError(
                    "SUB2API_CONFIG_UNAVAILABLE", "任务绑定的 Sub2API 配置版本不可用"
                ) from exc
            adapters = sub2api_profile_settings(config)
            if not adapters:
                raise PermanentJobError("SUB2API_NOT_CONFIGURED", "Sub2API 尚未配置")
            return [(adapter.profile_id, Sub2APIClient(adapter)) for adapter in adapters]
        if not self.settings.sub2api_configured:
            raise PermanentJobError("SUB2API_NOT_CONFIGURED", "Sub2API 尚未配置")
        return [("default", self.sub2api)]

    async def _sub2api_client(self, claim: ClaimedJob) -> Sub2APIClient:
        """Compatibility helper for integrations that inspect the primary client."""
        clients = await self._sub2api_clients(claim)
        return clients[0][1]

    @staticmethod
    def _require_source(source: bytes | None) -> None:
        if source is None:
            raise PermanentJobError("SOURCE_ASSET_REQUIRED", "该操作必须指定来源素材")

    @staticmethod
    def _required_text(parameters: dict[str, Any], name: str) -> str:
        value = str(parameters.get(name, "")).strip()
        if not value:
            raise PermanentJobError("INVALID_OPERATION_PARAMETERS", f"参数 {name} 不能为空")
        return value

    @staticmethod
    def _choice(parameters: dict[str, Any], name: str, default: str, allowed: set[str]) -> str:
        value = str(parameters.get(name, default))
        if value not in allowed:
            raise PermanentJobError("INVALID_OPERATION_PARAMETERS", f"参数 {name} 无效")
        return value
