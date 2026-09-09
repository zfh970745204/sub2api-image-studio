"""Create user preferences and notifications."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260909_0010"
down_revision: str | None = "20260908_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_preferences",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("theme", sa.String(length=16), server_default="light", nullable=False),
        sa.Column("locale", sa.String(length=16), server_default="zh-CN", nullable=False),
        sa.Column(
            "studio_layout",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "notification_preferences",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "theme IN ('light', 'dark', 'system')",
            name="ck_user_preferences_theme",
        ),
        sa.CheckConstraint(
            "locale IN ('zh-CN', 'en-US')",
            name="ck_user_preferences_locale",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "user_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.String(length=1000), nullable=False),
        sa.Column("target_url", sa.String(length=500), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_notifications_user_created",
        "user_notifications",
        ["user_id", "created_at", "id"],
    )
    op.create_index(
        "ix_user_notifications_user_unread",
        "user_notifications",
        ["user_id", "read_at"],
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
    op.drop_index("ix_user_notifications_user_unread", table_name="user_notifications")
    op.drop_index("ix_user_notifications_user_created", table_name="user_notifications")
    op.drop_table("user_notifications")
    op.drop_table("user_preferences")
