"""Persistent public brand media and versioned AI quality prices."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260910_0014"
down_revision: str | None = "20260910_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_media",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # Preserve historic quotes and existing custom quality rules. Add a new price version.
    op.execute("""
        WITH candidates AS (
            SELECT p.*, GREATEST(now(), p.effective_from + interval '1 microsecond') AS starts
            FROM operation_prices p JOIN operation_catalog o ON o.id = p.operation_id
            WHERE o.code LIKE 'ai.%' AND p.effective_to IS NULL
            AND NOT EXISTS (
                SELECT 1 FROM jsonb_array_elements(COALESCE(p.parameter_rules->'rules', '[]'::jsonb)) r
                WHERE r->>'parameter' = 'quality'
            )
        ), closed AS (
            UPDATE operation_prices p SET effective_to = c.starts
            FROM candidates c WHERE p.id = c.id RETURNING p.id
        )
        INSERT INTO operation_prices (id, operation_id, version, base_points, parameter_rules,
            effective_from, effective_to, created_by, reason)
        -- Avoid depending on pgcrypto/uuid-ossp being preinstalled in a managed database.
        SELECT md5(random()::text || clock_timestamp()::text || c.operation_id::text)::uuid,
            c.operation_id, c.version + 1, c.base_points,
            jsonb_build_object('rules', COALESCE(c.parameter_rules->'rules', '[]'::jsonb) ||
                jsonb_build_array(jsonb_build_object('parameter', 'quality', 'type', 'choice',
                    'points', jsonb_build_object('low', 0, 'medium', 0,
                        'high', GREATEST(1, CEIL(c.base_points / 2.0)),
                        'auto', GREATEST(1, CEIL(c.base_points / 2.0)))))),
            c.starts, NULL, NULL, 'Add standard and fine quality pricing'
        FROM candidates c JOIN closed d ON d.id = c.id
    """)
    op.execute(
        sa.text("INSERT INTO schema_migrations (version) VALUES (:version)").bindparams(
            version=revision
        )
    )


def downgrade() -> None:
    # Price history is financial data: retain published versions on rollback.
    op.drop_table("site_media")
    op.execute(
        sa.text("DELETE FROM schema_migrations WHERE version = :version").bindparams(
            version=revision
        )
    )
