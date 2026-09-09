"""Create membership plans, assignments, entitlements, and event history."""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260908_0004"
down_revision: str | None = "20260908_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PLAN_SEEDS = (
    ("free", "Free", "默认长期会员", 0, "none", 0, 10000, 1, 20, 40, 30, 10),
    ("basic", "Basic", "基础会员", 10, "month", 0, 9500, 2, 30, 60, 60, 20),
    ("pro", "Pro", "专业会员", 20, "month", 0, 8500, 3, 40, 80, 90, 30),
)


def _seed_id(kind: str, code: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"sub2image:{kind}:{code}")


def upgrade() -> None:
    op.create_table(
        "membership_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="draft", nullable=False),
        sa.Column("billing_period", sa.String(length=16), nullable=False),
        sa.Column("periodic_points", sa.Integer(), server_default="0", nullable=False),
        sa.Column("operation_discount_bps", sa.Integer(), nullable=False),
        sa.Column("max_concurrent_jobs", sa.Integer(), nullable=False),
        sa.Column("max_upload_mb", sa.Integer(), nullable=False),
        sa.Column("max_image_megapixels", sa.Integer(), nullable=False),
        sa.Column("asset_retention_days", sa.Integer(), nullable=False),
        sa.Column("display_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'inactive')",
            name="ck_membership_plans_status",
        ),
        sa.CheckConstraint(
            "billing_period IN ('none', 'month', 'year')",
            name="ck_membership_plans_billing_period",
        ),
        sa.CheckConstraint(
            "periodic_points >= 0 AND operation_discount_bps BETWEEN 0 AND 10000",
            name="ck_membership_plans_points_discount",
        ),
        sa.CheckConstraint(
            "max_concurrent_jobs > 0 AND max_upload_mb > 0 "
            "AND max_image_megapixels > 0 AND asset_retention_days > 0",
            name="ck_membership_plans_positive_limits",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.UniqueConstraint("level"),
    )
    op.create_index("ix_membership_plans_display", "membership_plans", ["status", "display_order"])
    op.create_table(
        "plan_entitlements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entitlement_code", sa.String(length=100), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["membership_plans.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "entitlement_code", name="uq_plan_entitlements_code"),
    )
    op.create_table(
        "user_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_renew", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("assigned_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("entitlement_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'active', 'expired', 'cancelled')",
            name="ck_user_memberships_status",
        ),
        sa.CheckConstraint(
            "source IN ('admin', 'payment', 'promotion', 'migration', 'system')",
            name="ck_user_memberships_source",
        ),
        sa.CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at",
            name="ck_user_memberships_time_range",
        ),
        sa.ForeignKeyConstraint(["assigned_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["plan_id"], ["membership_plans.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_user_memberships_one_active",
        "user_memberships",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_user_memberships_due",
        "user_memberships",
        ["status", "starts_at", "ends_at"],
    )
    op.create_index(
        "ix_user_memberships_user_created",
        "user_memberships",
        ["user_id", "created_at"],
    )
    op.create_table(
        "membership_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("membership_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("old_plan_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("new_plan_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('created', 'activated', 'renewed', 'upgraded', "
            "'downgraded', 'expired', 'cancelled')",
            name="ck_membership_events_type",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["membership_id"], ["user_memberships.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["new_plan_id"], ["membership_plans.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["old_plan_id"], ["membership_plans.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_user_id",
            "idempotency_key",
            name="uq_membership_events_actor_idempotency",
        ),
    )
    op.create_index(
        "ix_membership_events_membership_created",
        "membership_events",
        ["membership_id", "created_at"],
    )

    for seed in PLAN_SEEDS:
        (
            code,
            name,
            description,
            level,
            billing_period,
            periodic_points,
            operation_discount_bps,
            max_concurrent_jobs,
            max_upload_mb,
            max_image_megapixels,
            asset_retention_days,
            display_order,
        ) = seed
        op.execute(
            sa.text(
                "INSERT INTO membership_plans "
                "(id, code, name, description, level, status, billing_period, periodic_points, "
                "operation_discount_bps, max_concurrent_jobs, max_upload_mb, "
                "max_image_megapixels, asset_retention_days, display_order) "
                "VALUES (:id, :code, :name, :description, :level, 'active', :billing_period, "
                ":periodic_points, :operation_discount_bps, :max_concurrent_jobs, "
                ":max_upload_mb, :max_image_megapixels, :asset_retention_days, :display_order)"
            ).bindparams(
                id=_seed_id("membership-plan", code),
                code=code,
                name=name,
                description=description,
                level=level,
                billing_period=billing_period,
                periodic_points=periodic_points,
                operation_discount_bps=operation_discount_bps,
                max_concurrent_jobs=max_concurrent_jobs,
                max_upload_mb=max_upload_mb,
                max_image_megapixels=max_image_megapixels,
                asset_retention_days=asset_retention_days,
                display_order=display_order,
            )
        )

    op.execute(
        sa.text(
            "INSERT INTO user_memberships "
            "(id, user_id, plan_id, status, starts_at, ends_at, auto_renew, source, reason, "
            "entitlement_snapshot) "
            "SELECT users.id, users.id, :free_plan_id, 'active', now(), NULL, false, "
            "'migration', 'P0-v1 default membership backfill', "
            "jsonb_build_object('plan_code', 'free', 'discount_bps', 10000, "
            "'max_concurrent_jobs', 1, 'max_upload_mb', 20, 'max_image_megapixels', 40, "
            "'retention_days', 30, 'periodic_points', 0, 'entitlements', '{}'::jsonb) "
            "FROM users WHERE users.status = 'active' AND users.deleted_at IS NULL"
        ).bindparams(free_plan_id=_seed_id("membership-plan", "free"))
    )
    op.execute(
        sa.text(
            "INSERT INTO membership_events "
            "(id, membership_id, event_type, old_plan_id, new_plan_id, effective_at, reason) "
            "SELECT id, id, 'activated', NULL, plan_id, starts_at, "
            "'P0-v1 default membership backfill' FROM user_memberships"
        )
    )
    op.execute(
        sa.text("INSERT INTO schema_migrations (version) VALUES (:version)").bindparams(
            version=revision
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM schema_migrations WHERE version = :version").bindparams(
            version=revision
        )
    )
    op.drop_index("ix_membership_events_membership_created", table_name="membership_events")
    op.drop_table("membership_events")
    op.drop_index("ix_user_memberships_user_created", table_name="user_memberships")
    op.drop_index("ix_user_memberships_due", table_name="user_memberships")
    op.drop_index("uq_user_memberships_one_active", table_name="user_memberships")
    op.drop_table("user_memberships")
    op.drop_table("plan_entitlements")
    op.drop_index("ix_membership_plans_display", table_name="membership_plans")
    op.drop_table("membership_plans")
