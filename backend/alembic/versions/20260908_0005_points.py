"""Create point accounts, immutable transactions, and adjustment approvals."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260908_0005"
down_revision: str | None = "20260908_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "point_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("balance", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("lifetime_earned", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("lifetime_spent", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
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
        sa.CheckConstraint("balance >= 0", name="ck_point_accounts_balance"),
        sa.CheckConstraint(
            "lifetime_earned >= 0 AND lifetime_spent >= 0",
            name="ck_point_accounts_lifetime",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'frozen')",
            name="ck_point_accounts_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_index(
        "ix_point_accounts_status_created",
        "point_accounts",
        ["status", "created_at"],
    )
    op.create_table(
        "point_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entry_type", sa.String(length=16), nullable=False),
        sa.Column("delta", sa.BigInteger(), nullable=False),
        sa.Column("balance_before", sa.BigInteger(), nullable=False),
        sa.Column("balance_after", sa.BigInteger(), nullable=False),
        sa.Column("reference_type", sa.String(length=64), nullable=False),
        sa.Column("reference_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "entry_type IN ('grant', 'consume', 'refund', 'adjust', "
            "'renewal', 'promotion', 'reversal')",
            name="ck_point_transactions_entry_type",
        ),
        sa.CheckConstraint(
            "balance_before >= 0 AND balance_after >= 0",
            name="ck_point_transactions_balances",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["point_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reference_type",
            "reference_id",
            "entry_type",
            name="uq_point_transactions_business_entry",
        ),
        sa.UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_point_transactions_idempotency",
        ),
    )
    op.create_index(
        "ix_point_transactions_user_created",
        "point_transactions",
        ["user_id", "created_at", "id"],
    )
    op.create_index(
        "ix_point_transactions_account_created",
        "point_transactions",
        ["account_id", "created_at"],
    )
    op.create_table(
        "point_adjustment_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("review_idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("review_fingerprint", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount <> 0", name="ck_point_adjustments_nonzero"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'applied')",
            name="ck_point_adjustments_status",
        ),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["transaction_id"], ["point_transactions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "requested_by",
            "idempotency_key",
            name="uq_point_adjustments_request_idempotency",
        ),
        sa.UniqueConstraint(
            "review_idempotency_key",
            name="uq_point_adjustments_review_idempotency",
        ),
    )
    op.create_index(
        "ix_point_adjustments_status_created",
        "point_adjustment_requests",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_point_adjustments_user_created",
        "point_adjustment_requests",
        ["user_id", "created_at"],
    )

    op.execute(
        """
        CREATE FUNCTION sub2image_reject_point_transaction_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'point_transactions is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER point_transactions_append_only
        BEFORE UPDATE OR DELETE ON point_transactions
        FOR EACH ROW EXECUTE FUNCTION sub2image_reject_point_transaction_mutation()
        """
    )

    op.execute(
        """
        INSERT INTO point_accounts
            (id, user_id, balance, lifetime_earned, lifetime_spent, version, status)
        SELECT
            users.id,
            users.id,
            CASE WHEN users.password_hash IS NOT NULL THEN 20 ELSE 0 END,
            CASE WHEN users.password_hash IS NOT NULL THEN 20 ELSE 0 END,
            0,
            CASE WHEN users.password_hash IS NOT NULL THEN 2 ELSE 1 END,
            'active'
        FROM users
        WHERE users.deleted_at IS NULL
        """
    )
    op.execute(
        """
        INSERT INTO point_transactions
            (id, account_id, user_id, entry_type, delta, balance_before, balance_after,
             reference_type, reference_id, idempotency_key, request_fingerprint,
             description, metadata, actor_user_id)
        SELECT
            users.id,
            point_accounts.id,
            users.id,
            'grant',
            20,
            0,
            20,
            'onboarding',
            users.id,
            'onboarding:' || users.id::text,
            NULL,
            '首次激活赠送',
            jsonb_build_object('source', 'migration'),
            NULL
        FROM users
        JOIN point_accounts ON point_accounts.user_id = users.id
        WHERE users.deleted_at IS NULL AND users.password_hash IS NOT NULL
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
    op.drop_index("ix_point_adjustments_user_created", table_name="point_adjustment_requests")
    op.drop_index("ix_point_adjustments_status_created", table_name="point_adjustment_requests")
    op.drop_table("point_adjustment_requests")
    op.execute("DROP TRIGGER point_transactions_append_only ON point_transactions")
    op.execute("DROP FUNCTION sub2image_reject_point_transaction_mutation")
    op.drop_index("ix_point_transactions_account_created", table_name="point_transactions")
    op.drop_index("ix_point_transactions_user_created", table_name="point_transactions")
    op.drop_table("point_transactions")
    op.drop_index("ix_point_accounts_status_created", table_name="point_accounts")
    op.drop_table("point_accounts")
