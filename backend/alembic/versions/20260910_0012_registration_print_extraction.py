"""Add standalone print extraction and registration rate limiting."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260910_0012"
down_revision: str | None = "20260909_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Preserve prices/switches if an operator already seeded this operation.
    op.execute("""
        INSERT INTO operation_catalog
        (id, code, name, engine_type, queue_name, enabled, timeout_seconds, max_attempts)
        VALUES ('01991210-0012-7000-8000-000000000001'::uuid, 'ai.extract_print',
        '印花提取', 'sub2api', 'image-jobs', true, 240, 3)
        ON CONFLICT (code) DO NOTHING
    """)
    op.execute("""
        INSERT INTO operation_prices
        (id, operation_id, version, base_points, parameter_rules, effective_from, reason)
        SELECT '01991210-0012-7000-8000-000000000002'::uuid, id, 1, 18, '{}'::jsonb,
        now(), 'Standalone print extraction initial price'
        FROM operation_catalog o WHERE o.code = 'ai.extract_print'
        AND NOT EXISTS (SELECT 1 FROM operation_prices p WHERE p.operation_id = o.id)
    """)
    op.execute("""
        INSERT INTO rate_limit_policies
        (code, name, scope, request_limit, window_seconds, enabled)
        VALUES ('register', '公开注册', 'ip', 5, 3600, true)
        ON CONFLICT (code) DO NOTHING
    """)
    op.execute(
        sa.text("INSERT INTO schema_migrations (version) VALUES (:version)").bindparams(
            version=revision
        )
    )


def downgrade() -> None:
    # Existing jobs and assets reference these rows; keep their history recoverable.
    op.execute("UPDATE operation_catalog SET enabled = false WHERE code = 'ai.extract_print'")
    op.execute(
        sa.text("DELETE FROM schema_migrations WHERE version = :version").bindparams(
            version=revision
        )
    )
