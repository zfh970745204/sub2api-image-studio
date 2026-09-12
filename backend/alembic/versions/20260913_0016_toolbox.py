"""Register deterministic image tools on the durable job queue."""

import sqlalchemy as sa

from alembic import op

revision = "20260913_0016"
down_revision = "20260910_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO operation_catalog (id, code, name, engine_type, queue_name, enabled, timeout_seconds, max_attempts)
        VALUES ('f0000000-0000-4000-8000-000000000017', 'image.toolbox', '基础图片处理', 'local_code', 'image-jobs', true, 180, 2)
        ON CONFLICT (code) DO NOTHING
    """)
    op.execute("""
        INSERT INTO operation_prices (id, operation_id, version, base_points, parameter_rules, effective_from, reason)
        SELECT 'f0000000-0000-4000-8000-000000000018', id, 1, 0, '{}'::jsonb, now(), 'Toolbox initial free price'
        FROM operation_catalog o WHERE code = 'image.toolbox'
            AND NOT EXISTS (SELECT 1 FROM operation_prices WHERE operation_id = o.id)
    """)
    op.execute(
        sa.text("INSERT INTO schema_migrations (version) VALUES (:version)").bindparams(
            version=revision
        )
    )


def downgrade() -> None:
    op.execute("UPDATE operation_catalog SET enabled = false WHERE code = 'image.toolbox'")
    op.execute(
        sa.text("DELETE FROM schema_migrations WHERE version = :version").bindparams(
            version=revision
        )
    )
