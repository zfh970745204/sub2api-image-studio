from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from typing import Any

from app.api.errors import ApiError
from app.config import Settings
from app.image_ops import (
    ImageInputError,
    apply_color_effect,
    has_chroma_key_background,
    remove_background,
    remove_solid_background,
    upscale,
    validate_edit_mask,
    vectorize_artwork,
)
from app.object_storage import ObjectStorage, ObjectStorageError
from app.services.asset_files import AssetInputError, prepare_asset
from app.services.assets import AssetService
from app.services.configuration import RuntimeConfigCache, sub2api_settings
from app.services.jobs import ClaimedJob, PermanentJobError, RetryableJobError
from app.services.security import SecurityService
from app.sub2api import Sub2APIClient, Sub2APIError

AI_EDIT_PROMPTS = {
    "ai.redraw": (
        "Faithfully redraw the complete source artwork at high resolution. Preserve exact text, "
        "layout, colors, geometry, subjects, and all intentional details. Do not add or remove content."
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

    async def __call__(self, claim: ClaimedJob) -> dict[str, Any]:
        started = time.perf_counter()
        source_data = await self._source_data(claim)
        try:
            output, extension, provider_request_id, metadata = await self._execute(
                claim, source_data
            )
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
                parent_asset_id=claim.source_asset_id,
                source_job_id=claim.job_id,
                metadata=metadata,
                publish=False,
                request_id=f"job:{claim.job_id}",
            )
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
        return {
            "output_asset_id": asset.id,
            "provider_request_id": provider_request_id,
            "metrics": {
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "output_size_bytes": asset.size_bytes,
            },
        }

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
            sub2api = await self._sub2api_client(claim)
            prompt = self._required_text(parameters, "prompt")
            size = self._choice(
                parameters, "size", "1024x1024", {"auto", "1024x1024", "1024x1536", "1536x1024"}
            )
            quality = self._choice(
                parameters, "quality", "medium", {"auto", "low", "medium", "high"}
            )
            output_format = self._choice(
                parameters, "output_format", "png", {"png", "jpeg", "webp"}
            )
            upstream = await self._upstream(
                sub2api.generate(
                    prompt=prompt,
                    size=size,
                    quality=quality,
                    output_format=output_format,
                )
            )
            return (
                upstream.data,
                upstream.output_format,
                None,
                {"requested_size": size, "revised_prompt": upstream.revised_prompt},
            )
        if operation in AI_EDIT_PROMPTS:
            self._require_source(source)
            sub2api = await self._sub2api_client(claim)
            instruction = str(
                parameters.get("instruction") or parameters.get("prompt") or ""
            ).strip()
            prompt = AI_EDIT_PROMPTS[operation]
            if instruction:
                prompt = f"{prompt}\nUser instruction: {instruction}"
            mask = await self._mask_data(claim)
            if operation in {"ai.repair", "ai.text_fix"} and mask is None:
                raise PermanentJobError("MASK_REQUIRED", "该操作必须提供 mask_asset_id")
            if mask is not None:
                await asyncio.to_thread(validate_edit_mask, source, mask)
            upstream = await self._upstream(
                sub2api.edit(
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
                )
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

    async def _upstream(self, awaitable):
        try:
            self.circuit_breaker.before_call()
        except RetryableJobError:
            close = getattr(awaitable, "close", None)
            if close is not None:
                close()
            raise
        try:
            result = await awaitable
        except Sub2APIError as exc:
            if exc.status_code == 429 or exc.status_code >= 500:
                if self.circuit_breaker.failed():
                    raise RetryableJobError(
                        "SUB2API_CIRCUIT_OPEN", "上游图片服务熔断保护中"
                    ) from exc
                raise RetryableJobError("SUB2API_UNAVAILABLE", "上游图片服务暂时不可用") from exc
            raise PermanentJobError("SUB2API_REQUEST_REJECTED", "上游图片请求被拒绝") from exc
        self.circuit_breaker.succeeded()
        return result

    async def _sub2api_client(self, claim: ClaimedJob) -> Sub2APIClient:
        if self.config_cache is not None and claim.sub2api_config_version is not None:
            try:
                config = await self.config_cache.get("sub2api", claim.sub2api_config_version)
            except Exception as exc:
                raise PermanentJobError(
                    "SUB2API_CONFIG_UNAVAILABLE", "任务绑定的 Sub2API 配置版本不可用"
                ) from exc
            adapter = sub2api_settings(config)
            if not adapter.sub2api_configured:
                raise PermanentJobError("SUB2API_NOT_CONFIGURED", "Sub2API 尚未配置")
            return Sub2APIClient(adapter)
        if not self.settings.sub2api_configured:
            raise PermanentJobError("SUB2API_NOT_CONFIGURED", "Sub2API 尚未配置")
        return self.sub2api

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
