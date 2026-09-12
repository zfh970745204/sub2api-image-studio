from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OperationSeed:
    code: str
    name: str
    engine_type: str
    queue_name: str
    base_points: int
    timeout_seconds: int
    max_attempts: int


OPERATION_SEEDS = (
    OperationSeed("image.toolbox", "基础图片处理", "local_code", "image-jobs", 0, 180, 2),
    OperationSeed("ai.generate", "AI 生成图案", "sub2api", "image-jobs", 20, 240, 3),
    OperationSeed("ai.ecommerce", "电商主图", "sub2api", "image-jobs", 20, 2400, 2),
    OperationSeed("ai.redraw", "高清重绘", "sub2api", "image-jobs", 18, 240, 3),
    OperationSeed("ai.extract_print", "印花提取", "sub2api", "image-jobs", 18, 240, 3),
    OperationSeed("ai.repair", "局部修复", "sub2api", "image-jobs", 15, 240, 3),
    OperationSeed("ai.text_fix", "文字修正", "sub2api", "image-jobs", 15, 240, 3),
    OperationSeed("ai.variant", "生成变体", "sub2api", "image-jobs", 18, 240, 3),
    OperationSeed("cutout.smart", "智能抠图", "local_model", "image-jobs", 2, 180, 2),
    OperationSeed("upscale.2x", "2x 放大", "local_code", "image-jobs", 2, 120, 2),
    OperationSeed("upscale.4x", "4x 放大", "local_code", "image-jobs", 6, 240, 2),
    OperationSeed("color.effect", "颜色效果", "local_code", "image-jobs", 1, 60, 1),
    OperationSeed("vectorize.svg", "SVG 矢量化", "local_code", "image-jobs", 3, 120, 1),
)

OPERATION_CODES = frozenset(seed.code for seed in OPERATION_SEEDS)
ECOMMERCE_PLATFORMS = frozenset({"amazon", "etsy", "shopify", "taobao", "jd", "douyin"})
OPERATION_ENGINE_TYPES = frozenset({"sub2api", "local_model", "local_code"})
JOB_STATUSES = frozenset(
    {"queued", "running", "retry_wait", "succeeded", "failed", "cancelled", "timed_out"}
)
ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "retry_wait"})
FINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
REFUNDABLE_JOB_STATUSES = frozenset({"failed", "cancelled", "timed_out"})
JOB_ATTEMPT_STATUSES = frozenset({"running", "succeeded", "failed", "timed_out"})
JOB_REFUND_STATUSES = frozenset({"none", "refunded"})
