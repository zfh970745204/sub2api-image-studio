"""Create operation pricing, quotes, and durable image jobs."""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260908_0006"
down_revision: str | None = "20260908_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OPERATION_ROWS = (
    ("01a080c3-9e00-74f6-bb5d-1144fa11d329", "ai.generate", "AI 生成图案", "sub2api", 20, 240, 3),
    ("01a080c3-9e00-74aa-ada6-2e9afb894359", "ai.redraw", "高清重绘", "sub2api", 18, 240, 3),
    ("01a080c3-9e00-771c-99c8-9be4aefe2a81", "ai.repair", "局部修复", "sub2api", 15, 240, 3),
    ("01a080c3-9e00-7a3d-951a-2d177d580f6b", "ai.text_fix", "文字修正", "sub2api", 15, 240, 3),
    ("01a080c3-9e00-7204-b11d-374b1e3e7e7c", "ai.variant", "生成变体", "sub2api", 18, 240, 3),
    ("01a080c3-9e00-7149-90c9-fffc8d65d8ca", "cutout.smart", "智能抠图", "local_model", 2, 180, 2),
    ("01a080c3-9e00-7fb9-a4ec-f90f5d279716", "upscale.2x", "2x 放大", "local_code", 2, 120, 2),
    ("01a080c3-9e00-785f-b2f7-506b6c0910f3", "upscale.4x", "4x 放大", "local_code", 6, 240, 2),
    ("01a080c3-9e00-7922-be83-f44728d3fa6a", "color.effect", "颜色效果", "local_code", 1, 60, 1),
    (
        "01a080c3-9e00-77e4-b9a5-7c84ab780095",
        "vectorize.svg",
        "SVG 矢量化",
        "local_code",
        3,
        120,
        1,
    ),
)

PRICE_IDS = (
    "01a080c3-9e00-70c9-973a-3b4ee6618956",
    "01a080c3-9e00-7e88-a3de-07cbaddaef39",
    "01a080c3-9e00-7b40-a672-a6dc2f0626ca",
    "01a080c3-9e00-75a5-8076-755122376d42",
    "01a080c3-9e00-7eb9-9cdc-f0511849c42a",
    "01a080c3-9e00-71f1-a465-05c418dc810e",
    "01a080c3-9e00-79b3-be4d-e5f31b4aee97",
    "01a080c3-9e00-7570-819e-8499bbbe0e30",
    "01a080c3-9e00-7735-a8d1-6e0602007458",
    "01a080c3-9e00-7910-85f1-f65720cd7011",
)


def upgrade() -> None:
    op.create_table(
        "operation_catalog",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("engine_type", sa.String(length=32), nullable=False),
        sa.Column("queue_name", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
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
            "engine_type IN ('sub2api', 'local_model', 'local_code')",
            name="ck_operation_catalog_engine_type",
        ),
        sa.CheckConstraint(
            "timeout_seconds > 0 AND max_attempts > 0",
            name="ck_operation_catalog_execution_limits",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_operation_catalog_enabled_code", "operation_catalog", ["enabled", "code"])
    op.create_table(
        "operation_prices",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("base_points", sa.Integer(), nullable=False),
        sa.Column("parameter_rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name="ck_operation_prices_version"),
        sa.CheckConstraint("base_points >= 0", name="ck_operation_prices_base_points"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_operation_prices_time_range",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["operation_id"], ["operation_catalog.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id", "version", name="uq_operation_prices_version"),
    )
    op.create_index(
        "ix_operation_prices_effective",
        "operation_prices",
        ["operation_id", "effective_from", "effective_to"],
    )
    op.create_table(
        "job_quotes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_code", sa.String(length=64), nullable=False),
        sa.Column("source_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("parameters_hash", sa.String(length=64), nullable=False),
        sa.Column("membership_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("pricing_version", sa.Integer(), nullable=False),
        sa.Column("base_points", sa.Integer(), nullable=False),
        sa.Column("discount_points", sa.Integer(), nullable=False),
        sa.Column("surcharge_points", sa.Integer(), nullable=False),
        sa.Column("final_points", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "base_points >= 0 AND discount_points >= 0 AND surcharge_points >= 0 "
            "AND final_points >= 0",
            name="ck_job_quotes_points",
        ),
        sa.ForeignKeyConstraint(
            ["operation_code"], ["operation_catalog.code"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_quotes_user_created", "job_quotes", ["user_id", "created_at", "id"])
    op.create_index("ix_job_quotes_expires", "job_quotes", ["expires_at"])
    op.create_table(
        "image_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_code", sa.String(length=64), nullable=False),
        sa.Column("source_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("output_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("quote_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("refund_status", sa.String(length=16), server_default="none", nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("pricing_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("charged_points", sa.Integer(), nullable=False),
        sa.Column("charge_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("refund_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("worker_id", sa.String(length=255), nullable=True),
        sa.Column("progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed', "
            "'cancelled', 'timed_out')",
            name="ck_image_jobs_status",
        ),
        sa.CheckConstraint(
            "refund_status IN ('none', 'refunded')", name="ck_image_jobs_refund_status"
        ),
        sa.CheckConstraint(
            "charged_points >= 0 AND attempt_count >= 0 AND progress BETWEEN 0 AND 100",
            name="ck_image_jobs_counters",
        ),
        sa.ForeignKeyConstraint(
            ["charge_transaction_id"], ["point_transactions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["operation_code"], ["operation_catalog.code"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["quote_id"], ["job_quotes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["refund_transaction_id"], ["point_transactions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("quote_id", name="uq_image_jobs_quote"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_image_jobs_idempotency"),
    )
    op.create_index("ix_image_jobs_user_created", "image_jobs", ["user_id", "created_at", "id"])
    op.create_index(
        "ix_image_jobs_dispatch", "image_jobs", ["status", "next_attempt_at", "queued_at"]
    )
    op.create_table(
        "job_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_detail_redacted", sa.Text(), nullable=True),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("attempt_no > 0", name="ck_job_attempts_number"),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'timed_out')",
            name="ck_job_attempts_status",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["image_jobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "attempt_no", name="uq_job_attempts_number"),
    )
    op.create_index("ix_job_attempts_job_started", "job_attempts", ["job_id", "started_at"])

    operation_table = sa.table(
        "operation_catalog",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
        sa.column("engine_type", sa.String()),
        sa.column("queue_name", sa.String()),
        sa.column("enabled", sa.Boolean()),
        sa.column("timeout_seconds", sa.Integer()),
        sa.column("max_attempts", sa.Integer()),
    )
    op.bulk_insert(
        operation_table,
        [
            {
                "id": uuid.UUID(row[0]),
                "code": row[1],
                "name": row[2],
                "engine_type": row[3],
                "queue_name": "image-jobs",
                "enabled": True,
                "timeout_seconds": row[5],
                "max_attempts": row[6],
            }
            for row in OPERATION_ROWS
        ],
    )
    for index, row in enumerate(OPERATION_ROWS):
        op.execute(
            "INSERT INTO operation_prices "
            "(id, operation_id, version, base_points, parameter_rules, effective_from, "
            "created_by, reason) VALUES "
            f"('{PRICE_IDS[index]}'::uuid, '{row[0]}'::uuid, 1, {row[4]}, "
            "'{}'::jsonb, now(), NULL, 'P0-v1 initial price')"
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
    op.drop_index("ix_job_attempts_job_started", table_name="job_attempts")
    op.drop_table("job_attempts")
    op.drop_index("ix_image_jobs_dispatch", table_name="image_jobs")
    op.drop_index("ix_image_jobs_user_created", table_name="image_jobs")
    op.drop_table("image_jobs")
    op.drop_index("ix_job_quotes_expires", table_name="job_quotes")
    op.drop_index("ix_job_quotes_user_created", table_name="job_quotes")
    op.drop_table("job_quotes")
    op.drop_index("ix_operation_prices_effective", table_name="operation_prices")
    op.drop_table("operation_prices")
    op.drop_index("ix_operation_catalog_enabled_code", table_name="operation_catalog")
    op.drop_table("operation_catalog")
