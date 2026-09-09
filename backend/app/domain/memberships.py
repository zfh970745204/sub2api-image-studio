from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MembershipPlanSeed:
    code: str
    name: str
    description: str
    level: int
    billing_period: str
    periodic_points: int
    operation_discount_bps: int
    max_concurrent_jobs: int
    max_upload_mb: int
    max_image_megapixels: int
    asset_retention_days: int
    display_order: int


MEMBERSHIP_PLAN_SEEDS = (
    MembershipPlanSeed(
        code="free",
        name="Free",
        description="默认长期会员",
        level=0,
        billing_period="none",
        periodic_points=0,
        operation_discount_bps=10000,
        max_concurrent_jobs=1,
        max_upload_mb=20,
        max_image_megapixels=40,
        asset_retention_days=30,
        display_order=10,
    ),
    MembershipPlanSeed(
        code="basic",
        name="Basic",
        description="基础会员",
        level=10,
        billing_period="month",
        periodic_points=0,
        operation_discount_bps=9500,
        max_concurrent_jobs=2,
        max_upload_mb=30,
        max_image_megapixels=60,
        asset_retention_days=60,
        display_order=20,
    ),
    MembershipPlanSeed(
        code="pro",
        name="Pro",
        description="专业会员",
        level=20,
        billing_period="month",
        periodic_points=0,
        operation_discount_bps=8500,
        max_concurrent_jobs=3,
        max_upload_mb=40,
        max_image_megapixels=80,
        asset_retention_days=90,
        display_order=30,
    ),
)

MEMBERSHIP_PLAN_CODES = frozenset(seed.code for seed in MEMBERSHIP_PLAN_SEEDS)
MEMBERSHIP_PLAN_STATUSES = frozenset({"draft", "active", "inactive"})
MEMBERSHIP_BILLING_PERIODS = frozenset({"none", "month", "year"})
MEMBERSHIP_STATUSES = frozenset({"scheduled", "active", "expired", "cancelled"})
MEMBERSHIP_EVENT_TYPES = frozenset(
    {"created", "activated", "renewed", "upgraded", "downgraded", "expired", "cancelled"}
)
