"""Require an email code before creating a publicly registered account."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260910_0013"
down_revision: str | None = "20260910_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "registration_challenges",
        sa.Column("email", postgresql.CITEXT(), primary_key=True),
        sa.Column("nonce", sa.String(32), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("send_count", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
    )
    op.execute("""
        INSERT INTO rate_limit_policies (code, name, scope, request_limit, window_seconds, enabled)
        VALUES ('register_email', '注册验证码发送', 'ip', 10, 3600, true)
        ON CONFLICT (code) DO NOTHING
    """)
    op.execute(
        sa.text("INSERT INTO schema_migrations (version) VALUES (:version)").bindparams(
            version=revision
        )
    )


def downgrade() -> None:
    op.drop_table("registration_challenges")
    op.execute("DELETE FROM rate_limit_policies WHERE code = 'register_email'")
    op.execute(
        sa.text("DELETE FROM schema_migrations WHERE version = :version").bindparams(
            version=revision
        )
    )
