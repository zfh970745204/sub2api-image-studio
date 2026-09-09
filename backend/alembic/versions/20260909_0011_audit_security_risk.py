"""Create append-only audit, security events, blocks, and rate limits."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260909_0011"
down_revision: str | None = "20260909_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "actor_role_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=200), nullable=False),
        sa.Column("target_type", sa.String(length=100), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("ip_hash", sa.String(length=64), nullable=False),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column(
            "changes_redacted",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "result IN ('success', 'denied', 'failed')", name="ck_audit_logs_result"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_occurred", "audit_logs", ["occurred_at", "id"])
    op.create_index("ix_audit_logs_actor_occurred", "audit_logs", ["actor_user_id", "occurred_at"])
    op.create_index(
        "ix_audit_logs_target", "audit_logs", ["target_type", "target_id", "occurred_at"]
    )
    op.create_index("ix_audit_logs_action_occurred", "audit_logs", ["action", "occurred_at"])
    op.create_index("ix_audit_logs_request_id", "audit_logs", ["request_id"])

    op.create_table(
        "security_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        sa.Column(
            "details_redacted",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("resolved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')", name="ck_security_events_severity"
        ),
        sa.CheckConstraint(
            "status IN ('open', 'investigating', 'resolved', 'ignored')",
            name="ck_security_events_status",
        ),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_security_events_status_created", "security_events", ["status", "created_at", "id"]
    )
    op.create_index(
        "ix_security_events_type_created", "security_events", ["event_type", "created_at"]
    )
    op.create_index("ix_security_events_user_created", "security_events", ["user_id", "created_at"])

    op.create_table(
        "access_blocks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_hash", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "subject_type IN ('user', 'ip_fingerprint')", name="ck_access_blocks_subject_type"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_access_blocks_subject_active",
        "access_blocks",
        ["subject_type", "subject_hash", "ends_at"],
    )
    op.create_index("ix_access_blocks_created", "access_blocks", ["created_at", "id"])

    op.create_table(
        "rate_limit_policies",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("request_limit", sa.Integer(), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("request_limit > 0", name="ck_rate_limit_policies_limit"),
        sa.CheckConstraint("window_seconds > 0", name="ck_rate_limit_policies_window"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("code"),
    )
    policies = sa.table(
        "rate_limit_policies",
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
        sa.column("scope", sa.String()),
        sa.column("request_limit", sa.Integer()),
        sa.column("window_seconds", sa.Integer()),
        sa.column("enabled", sa.Boolean()),
    )
    op.bulk_insert(
        policies,
        [
            {
                "code": "login",
                "name": "登录",
                "scope": "ip",
                "request_limit": 25,
                "window_seconds": 900,
                "enabled": True,
            },
            {
                "code": "upload",
                "name": "素材上传",
                "scope": "both",
                "request_limit": 20,
                "window_seconds": 3600,
                "enabled": True,
            },
            {
                "code": "job_create",
                "name": "任务创建",
                "scope": "both",
                "request_limit": 30,
                "window_seconds": 60,
                "enabled": True,
            },
            {
                "code": "quote",
                "name": "任务报价",
                "scope": "both",
                "request_limit": 60,
                "window_seconds": 60,
                "enabled": True,
            },
            {
                "code": "download",
                "name": "素材下载",
                "scope": "both",
                "request_limit": 120,
                "window_seconds": 60,
                "enabled": True,
            },
        ],
    )

    op.execute(
        """
        CREATE FUNCTION reject_audit_log_mutation() RETURNS trigger AS $$
        BEGIN
            IF current_setting('sub2image.audit_archive', true) IS DISTINCT FROM 'on' THEN
                RAISE EXCEPTION 'audit_logs is append-only';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_logs_append_only
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION reject_audit_log_mutation();
        """
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
    op.execute("DROP TRIGGER IF EXISTS audit_logs_append_only ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS reject_audit_log_mutation()")
    op.drop_table("rate_limit_policies")
    op.drop_index("ix_access_blocks_created", table_name="access_blocks")
    op.drop_index("ix_access_blocks_subject_active", table_name="access_blocks")
    op.drop_table("access_blocks")
    op.drop_index("ix_security_events_user_created", table_name="security_events")
    op.drop_index("ix_security_events_type_created", table_name="security_events")
    op.drop_index("ix_security_events_status_created", table_name="security_events")
    op.drop_table("security_events")
    op.drop_index("ix_audit_logs_request_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action_occurred", table_name="audit_logs")
    op.drop_index("ix_audit_logs_target", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_occurred", table_name="audit_logs")
    op.drop_index("ix_audit_logs_occurred", table_name="audit_logs")
    op.drop_table("audit_logs")
