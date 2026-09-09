"""Create administrator workflow and saved-view tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260908_0009"
down_revision: str | None = "20260908_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_action_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_type", sa.String(length=100), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "risk_level IN ('normal', 'high', 'critical')",
            name="ck_admin_action_requests_risk_level",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'executed', 'failed')",
            name="ck_admin_action_requests_status",
        ),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_action_requests_status_created",
        "admin_action_requests",
        ["status", "created_at", "id"],
    )
    op.create_index(
        "ix_admin_action_requests_target",
        "admin_action_requests",
        ["target_type", "target_id", "created_at"],
    )
    op.create_index(
        "ix_admin_action_requests_requester",
        "admin_action_requests",
        ["requested_by", "created_at"],
    )
    op.create_table(
        "admin_saved_views",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("module", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("columns", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "module", "name", name="uq_admin_saved_views_owner_name"),
    )
    op.create_index(
        "ix_admin_saved_views_owner_module",
        "admin_saved_views",
        ["owner_id", "module", "updated_at"],
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
    op.drop_index("ix_admin_saved_views_owner_module", table_name="admin_saved_views")
    op.drop_table("admin_saved_views")
    op.drop_index("ix_admin_action_requests_requester", table_name="admin_action_requests")
    op.drop_index("ix_admin_action_requests_target", table_name="admin_action_requests")
    op.drop_index("ix_admin_action_requests_status_created", table_name="admin_action_requests")
    op.drop_table("admin_action_requests")
