"""Persist complete image batches and add per-image ecommerce pricing."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260910_0015"
down_revision = "20260910_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("image_jobs", sa.Column("output_asset_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.execute("UPDATE image_jobs SET output_asset_ids = jsonb_build_array(output_asset_id::text) WHERE output_asset_id IS NOT NULL")
    op.execute("""
        INSERT INTO operation_catalog (id, code, name, engine_type, queue_name, enabled, timeout_seconds, max_attempts)
        VALUES ('f0000000-0000-4000-8000-000000000015', 'ai.ecommerce', '电商主图', 'sub2api', 'image-jobs', true, 2400, 2)
        ON CONFLICT (code) DO NOTHING
    """)
    op.execute("""
        INSERT INTO operation_prices (id, operation_id, version, base_points, parameter_rules, effective_from, reason)
        SELECT 'f0000000-0000-4000-8000-000000000016', id, 1, 20,
            '{"rules":[{"parameter":"quality","type":"choice","points":{"low":0,"medium":0,"high":10,"auto":10}}]}'::jsonb,
            now(), 'Ecommerce per-image price; multiplied by image_count'
        FROM operation_catalog o WHERE code = 'ai.ecommerce'
            AND NOT EXISTS (SELECT 1 FROM operation_prices WHERE operation_id = o.id)
    """)
    # Existing installations had the earlier placeholder art in the active brand version.
    # Replace only those untouched defaults; custom URLs remain unchanged.
    op.execute("""
        UPDATE config_versions
        SET "values" = jsonb_set(
            "values", '{login_image_url}', '"/brand/login-studio-v3.webp"'::jsonb
        )
        WHERE "values"->>'login_image_url' = '/brand/login-art.webp'
    """)
    op.execute("""
        UPDATE config_versions
        SET "values" = jsonb_set(
            "values", '{register_image_url}', '"/brand/register-studio-v3.webp"'::jsonb
        )
        WHERE "values"->>'register_image_url' = '/brand/register-art.webp'
    """)
    op.execute("""
        UPDATE config_versions
        SET "values" = jsonb_set(
            "values", '{home_image_url}', '"/brand/home-studio-v3.webp"'::jsonb
        )
        WHERE "values"->>'home_image_url' = '/brand/home-art.webp'
    """)
    op.execute(sa.text("INSERT INTO schema_migrations (version) VALUES (:version)").bindparams(version=revision))


def downgrade() -> None:
    # Keep output IDs and financial history: rolling back disables creation without losing batches.
    op.execute("UPDATE operation_catalog SET enabled = false WHERE code = 'ai.ecommerce'")
    op.execute(sa.text("DELETE FROM schema_migrations WHERE version = :version").bindparams(version=revision))
