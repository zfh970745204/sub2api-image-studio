"""Shared image service configuration; persisted legacy group names remain stable."""

from dataclasses import dataclass
from typing import Literal

ImageProvider = Literal[
    "openai", "openlux", "gemini", "seedream", "siliconflow", "dashscope", "bfl", "stability"
]
ImageAuth = Literal["auto", "bearer", "x-goog-api-key", "x-key"]


class ImageServiceError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int = 502,
        *,
        retryable: bool | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.provider_request_id = provider_request_id


class UnsupportedImageOperation(ImageServiceError):
    """Raised before submitting anything; another compatible profile may be selected."""

    def __init__(self, message: str) -> None:
        super().__init__(message, 422, retryable=False)


@dataclass(slots=True)
class UpstreamImage:
    data: bytes
    output_format: str
    revised_prompt: str | None
    provider_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class ImageCapabilities:
    generation: bool = True
    max_images: int = 0
    mask: bool = False


def image_capabilities(provider: str, model: str) -> ImageCapabilities:
    name = model.lower()
    if provider in {"openai", "openlux"}:
        if name == "dall-e-3":
            return ImageCapabilities()
        return ImageCapabilities(max_images=1 if name == "dall-e-2" else 16, mask=True)
    if provider == "gemini":
        return ImageCapabilities(max_images=3 if "2.5" in name else 14)
    if provider == "seedream":
        return ImageCapabilities(max_images=1 if "seededit" in name else 14)
    if provider == "siliconflow":
        if "qwen-image-edit" in name:
            return ImageCapabilities(generation=False, max_images=3 if "2509" in name else 1)
        return ImageCapabilities()
    if provider == "dashscope":
        if "qwen-image-edit" in name:
            return ImageCapabilities(
                generation=False, max_images=1 if name == "qwen-image-edit" else 3
            )
        if name.startswith("wan2.7-image"):
            return ImageCapabilities(max_images=9)
        if name.startswith(("qwen-image-2", "qwen-image-3", "wan2.6-image")):
            return ImageCapabilities(max_images=3)
        return ImageCapabilities()
    if provider == "stability":
        if name == "core":
            return ImageCapabilities()
        if name == "ultra" or name.startswith("sd3"):
            return ImageCapabilities(max_images=1, mask=True)
        raise UnsupportedImageOperation("Stability 模型请填写 core、ultra 或 sd3.5 系列模型名")
    if provider == "bfl":
        if name.startswith("flux-2-"):
            return ImageCapabilities(max_images=8)
        if name.startswith("flux-kontext-"):
            return ImageCapabilities(max_images=1)
        return ImageCapabilities()
    raise UnsupportedImageOperation("未识别的图片接口协议，请检查线路配置")
