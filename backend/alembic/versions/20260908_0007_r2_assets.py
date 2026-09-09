"""Create private object assets, access logs, and deletion queue."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260908_0007"
down_revision: str | None = "20260908_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("operation_code", sa.String(length=64), nullable=False),
        sa.Column("storage_provider", sa.String(length=16), server_default="r2", nullable=False),
        sa.Column("bucket", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=True),
        sa.Column("mime_type", sa.String(length=100), nullable=False),
        sa.Column("extension", sa.String(length=16), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("has_alpha", sa.Boolean(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
            "kind IN ('original', 'result', 'mask', 'thumbnail', 'vector')",
            name="ck_assets_kind",
        ),
        sa.CheckConstraint(
            "status IN ('uploading', 'ready', 'quarantined', 'deleted')",
            name="ck_assets_status",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_assets_size_bytes"),
        sa.CheckConstraint(
            "(width IS NULL AND height IS NULL) OR (width > 0 AND height > 0)",
            name="ck_assets_dimensions",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parent_asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["root_asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_job_id"], ["image_jobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_index("ix_assets_owner_created", "assets", ["owner_id", "created_at", "id"])
    op.create_index("ix_assets_root_created", "assets", ["root_asset_id", "created_at", "id"])
    op.create_index("ix_assets_status_retention", "assets", ["status", "retention_until"])
    op.create_table(
        "asset_access_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("ip_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('preview', 'download', 'admin_preview')",
            name="ck_asset_access_logs_action",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_asset_access_logs_asset_created",
        "asset_access_logs",
        ["asset_id", "created_at"],
    )
    op.create_index(
        "ix_asset_access_logs_actor_created",
        "asset_access_logs",
        ["actor_user_id", "created_at"],
    )
    op.create_table(
        "object_deletion_queue",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("execute_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_object_deletion_queue_attempts"),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'failed', 'cancelled')",
            name="ck_object_deletion_queue_status",
        ),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id", name="uq_object_deletion_queue_asset"),
    )
    op.create_index(
        "ix_object_deletion_queue_due",
        "object_deletion_queue",
        ["status", "execute_after"],
    )
    # Phase 06 accepted opaque UUIDs before a PostgreSQL asset source existed.
    op.execute(
        "UPDATE job_quotes SET membership_snapshot = "
        "jsonb_set(membership_snapshot, '{migration}', "
        "COALESCE(membership_snapshot->'migration', '{}'::jsonb) || "
        "jsonb_build_object('legacy_source_asset_id', source_asset_id::text), true), "
        "source_asset_id = NULL WHERE source_asset_id IS NOT NULL"
    )
    op.execute(
        "UPDATE image_jobs SET pricing_snapshot = "
        "jsonb_set(pricing_snapshot, '{migration}', "
        "COALESCE(pricing_snapshot->'migration', '{}'::jsonb) || "
        "jsonb_build_object('legacy_source_asset_id', source_asset_id::text), true), "
        "source_asset_id = NULL WHERE source_asset_id IS NOT NULL"
    )
    op.execute(
        "UPDATE image_jobs SET pricing_snapshot = "
        "jsonb_set(pricing_snapshot, '{migration}', "
        "COALESCE(pricing_snapshot->'migration', '{}'::jsonb) || "
        "jsonb_build_object('legacy_output_asset_id', output_asset_id::text), true), "
        "output_asset_id = NULL WHERE output_asset_id IS NOT NULL"
    )
    op.create_foreign_key(
        "fk_job_quotes_source_asset_id_assets",
        "job_quotes",
        "assets",
        ["source_asset_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_image_jobs_source_asset_id_assets",
        "image_jobs",
        "assets",
        ["source_asset_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_image_jobs_output_asset_id_assets",
        "image_jobs",
        "assets",
        ["output_asset_id"],
        ["id"],
        ondelete="RESTRICT",
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
    op.drop_constraint("fk_image_jobs_output_asset_id_assets", "image_jobs", type_="foreignkey")
    op.drop_constraint("fk_image_jobs_source_asset_id_assets", "image_jobs", type_="foreignkey")
    op.drop_constraint("fk_job_quotes_source_asset_id_assets", "job_quotes", type_="foreignkey")
    op.drop_index("ix_object_deletion_queue_due", table_name="object_deletion_queue")
    op.drop_table("object_deletion_queue")
    op.drop_index("ix_asset_access_logs_actor_created", table_name="asset_access_logs")
    op.drop_index("ix_asset_access_logs_asset_created", table_name="asset_access_logs")
    op.drop_table("asset_access_logs")
    op.drop_index("ix_assets_status_retention", table_name="assets")
    op.drop_index("ix_assets_root_created", table_name="assets")
    op.drop_index("ix_assets_owner_created", table_name="assets")
    op.drop_table("assets")
