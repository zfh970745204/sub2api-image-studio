"""Create versioned system configuration and encrypted secrets."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260908_0008"
down_revision: str | None = "20260908_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CONFIG_GROUPS = (
    ("01a11d3e-7300-7001-8000-000000000001", "sub2api", "Sub2API"),
    ("01a11d3e-7300-7002-8000-000000000002", "r2", "Cloudflare R2"),
    ("01a11d3e-7300-7003-8000-000000000003", "email", "邮件服务"),
    ("01a11d3e-7300-7004-8000-000000000004", "general", "通用业务配置"),
)


def upgrade() -> None:
    op.create_table(
        "config_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("active_version", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_table(
        "config_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("values", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("published_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("change_reason", sa.String(length=500), nullable=False),
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
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version > 0", name="ck_config_versions_version"),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'superseded', 'failed')",
            name="ck_config_versions_status",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["group_id"], ["config_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "version", name="uq_config_versions_group_version"),
    )
    op.create_index(
        "ix_config_versions_group_created",
        "config_versions",
        ["group_id", "created_at"],
    )
    op.create_table(
        "encrypted_secrets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key_name", sa.String(length=100), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("value_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("last_four", sa.String(length=4), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("key_version > 0", name="ck_encrypted_secrets_key_version"),
        sa.ForeignKeyConstraint(["config_version_id"], ["config_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "config_version_id", "key_name", name="uq_encrypted_secrets_version_key"
        ),
    )
    op.create_table(
        "config_test_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result_code", sa.String(length=100), nullable=False),
        sa.Column("result_message_redacted", sa.String(length=1000), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_config_test_runs_status",
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0", name="ck_config_test_runs_latency"
        ),
        sa.ForeignKeyConstraint(["config_version_id"], ["config_versions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["group_id"], ["config_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_config_test_runs_version_created",
        "config_test_runs",
        ["config_version_id", "created_at"],
    )
    group_table = sa.table(
        "config_groups",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
        sa.column("active_version", sa.Integer()),
    )
    op.bulk_insert(
        group_table,
        [
            {"id": row_id, "code": code, "name": name, "active_version": None}
            for row_id, code, name in CONFIG_GROUPS
        ],
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
    op.drop_index("ix_config_test_runs_version_created", table_name="config_test_runs")
    op.drop_table("config_test_runs")
    op.drop_table("encrypted_secrets")
    op.drop_index("ix_config_versions_group_created", table_name="config_versions")
    op.drop_table("config_versions")
    op.drop_table("config_groups")
