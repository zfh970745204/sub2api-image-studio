from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

from app.domain.ids import uuid7

JSON_VALUE = JSON().with_variant(JSONB, "postgresql")
EMAIL_VALUE = String(320).with_variant(CITEXT(), "postgresql")
USERNAME_VALUE = String(64).with_variant(CITEXT(), "postgresql")


class Base(DeclarativeBase):
    pass


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ServiceInstance(Base):
    __tablename__ = "service_instances"
    __table_args__ = (
        CheckConstraint(
            "service_type IN ('web', 'worker', 'scheduler')",
            name="ck_service_instances_service_type",
        ),
        UniqueConstraint("service_type", "instance_name", name="uq_service_instances_type_name"),
        Index("ix_service_instances_last_heartbeat", "last_heartbeat_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    service_type: Mapped[str] = mapped_column(String(32), nullable=False)
    instance_name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    instance_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON_VALUE, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'published', 'failed')",
            name="ck_outbox_events_status",
        ),
        Index("ix_outbox_events_dispatch", "status", "available_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    topic: Mapped[str] = mapped_column(String(200), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(
            "result IN ('success', 'denied', 'failed')",
            name="ck_audit_logs_result",
        ),
        Index("ix_audit_logs_occurred", "occurred_at", "id"),
        Index("ix_audit_logs_actor_occurred", "actor_user_id", "occurred_at"),
        Index("ix_audit_logs_target", "target_type", "target_id", "occurred_at"),
        Index("ix_audit_logs_action_occurred", "action", "occurred_at"),
        Index("ix_audit_logs_request_id", "request_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID | None]
    actor_role_snapshot: Mapped[list[str]] = mapped_column(JSON_VALUE, default=list, nullable=False)
    action: Mapped[str] = mapped_column(String(200), nullable=False)
    target_type: Mapped[str] = mapped_column(String(100), nullable=False)
    target_id: Mapped[uuid.UUID | None]
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(500))
    reason: Mapped[str | None] = mapped_column(String(500))
    changes_redacted: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, default=dict, nullable=False
    )
    audit_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON_VALUE, default=dict, nullable=False
    )


class SecurityEvent(Base):
    __tablename__ = "security_events"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name="ck_security_events_severity",
        ),
        CheckConstraint(
            "status IN ('open', 'investigating', 'resolved', 'ignored')",
            name="ck_security_events_status",
        ),
        Index("ix_security_events_status_created", "status", "created_at", "id"),
        Index("ix_security_events_type_created", "event_type", "created_at"),
        Index("ix_security_events_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    user_id: Mapped[uuid.UUID | None]
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    details_redacted: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, default=dict, nullable=False
    )
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AccessBlock(Base):
    __tablename__ = "access_blocks"
    __table_args__ = (
        CheckConstraint(
            "subject_type IN ('user', 'ip_fingerprint')",
            name="ck_access_blocks_subject_type",
        ),
        Index("ix_access_blocks_subject_active", "subject_type", "subject_hash", "ends_at"),
        Index("ix_access_blocks_created", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RateLimitPolicy(Base):
    __tablename__ = "rate_limit_policies"
    __table_args__ = (
        CheckConstraint("request_limit > 0", name="ck_rate_limit_policies_limit"),
        CheckConstraint("window_seconds > 0", name="ck_rate_limit_policies_window"),
    )

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    request_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'active', 'disabled', 'locked')",
            name="ck_users_status",
        ),
        Index("ix_users_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    email: Mapped[str] = mapped_column(EMAIL_VALUE, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(USERNAME_VALUE, unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    avatar_asset_id: Mapped[uuid.UUID | None]
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    session_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    permission_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserPreference(Base):
    __tablename__ = "user_preferences"
    __table_args__ = (
        CheckConstraint(
            "theme IN ('light', 'dark', 'system')",
            name="ck_user_preferences_theme",
        ),
        CheckConstraint(
            "locale IN ('zh-CN', 'en-US')",
            name="ck_user_preferences_locale",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    theme: Mapped[str] = mapped_column(String(16), default="light", nullable=False)
    locale: Mapped[str] = mapped_column(String(16), default="zh-CN", nullable=False)
    studio_layout: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict, nullable=False)
    notification_preferences: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, default=dict, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class UserNotification(Base):
    __tablename__ = "user_notifications"
    __table_args__ = (
        Index("ix_user_notifications_user_created", "user_id", "created_at", "id"),
        Index("ix_user_notifications_user_unread", "user_id", "read_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(String(1000), nullable=False)
    target_url: Mapped[str | None] = mapped_column(String(500))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ConfigGroup(Base):
    __tablename__ = "config_groups"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    active_version: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ConfigVersion(Base):
    __tablename__ = "config_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_config_versions_version"),
        CheckConstraint(
            "status IN ('draft', 'active', 'superseded', 'failed')",
            name="ck_config_versions_status",
        ),
        UniqueConstraint("group_id", "version", name="uq_config_versions_group_version"),
        Index("ix_config_versions_group_created", "group_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("config_groups.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    values: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    change_reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EncryptedSecret(Base):
    __tablename__ = "encrypted_secrets"
    __table_args__ = (
        CheckConstraint("key_version > 0", name="ck_encrypted_secrets_key_version"),
        UniqueConstraint("config_version_id", "key_name", name="uq_encrypted_secrets_version_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    config_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("config_versions.id", ondelete="CASCADE"), nullable=False
    )
    key_name: Mapped[str] = mapped_column(String(100), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(nullable=False)
    nonce: Mapped[bytes] = mapped_column(nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    value_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    last_four: Mapped[str | None] = mapped_column(String(4))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ConfigTestRun(Base):
    __tablename__ = "config_test_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_config_test_runs_status",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0", name="ck_config_test_runs_latency"
        ),
        Index("ix_config_test_runs_version_created", "config_version_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("config_groups.id", ondelete="RESTRICT"), nullable=False
    )
    config_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("config_versions.id", ondelete="SET NULL")
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    result_code: Mapped[str] = mapped_column(String(100), nullable=False)
    result_message_redacted: Mapped[str] = mapped_column(String(1000), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminActionRequest(Base):
    __tablename__ = "admin_action_requests"
    __table_args__ = (
        CheckConstraint(
            "risk_level IN ('normal', 'high', 'critical')",
            name="ck_admin_action_requests_risk_level",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'executed', 'failed')",
            name="ck_admin_action_requests_status",
        ),
        Index("ix_admin_action_requests_status_created", "status", "created_at", "id"),
        Index("ix_admin_action_requests_target", "target_type", "target_id", "created_at"),
        Index("ix_admin_action_requests_requester", "requested_by", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AdminSavedView(Base):
    __tablename__ = "admin_saved_views"
    __table_args__ = (
        UniqueConstraint("owner_id", "module", "name", name="uq_admin_saved_views_owner_name"),
        Index("ix_admin_saved_views_owner_module", "owner_id", "module", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    module: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict, nullable=False)
    columns: Mapped[list[str]] = mapped_column(JSON_VALUE, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        Index("ix_auth_sessions_expires_at", "expires_at"),
        Index("ix_auth_sessions_user_active", "user_id", "revoked_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_agent: Mapped[str] = mapped_column(String(500), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    __table_args__ = (Index("ix_password_reset_tokens_expires_at", "expires_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    __table_args__ = (
        Index("ix_login_attempts_account_created", "account_fingerprint", "created_at"),
        Index("ix_login_attempts_ip_created", "ip_hash", "created_at"),
        Index("ix_login_attempts_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    account_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (Index("ix_user_roles_role_id", "role_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    assigned_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    module: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )


class MembershipPlan(Base):
    __tablename__ = "membership_plans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'inactive')",
            name="ck_membership_plans_status",
        ),
        CheckConstraint(
            "billing_period IN ('none', 'month', 'year')",
            name="ck_membership_plans_billing_period",
        ),
        CheckConstraint(
            "periodic_points >= 0 AND operation_discount_bps BETWEEN 0 AND 10000",
            name="ck_membership_plans_points_discount",
        ),
        CheckConstraint(
            "max_concurrent_jobs > 0 AND max_upload_mb > 0 "
            "AND max_image_megapixels > 0 AND asset_retention_days > 0",
            name="ck_membership_plans_positive_limits",
        ),
        Index("ix_membership_plans_display", "status", "display_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    level: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    billing_period: Mapped[str] = mapped_column(String(16), nullable=False)
    periodic_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    operation_discount_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer, nullable=False)
    max_upload_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    max_image_megapixels: Mapped[int] = mapped_column(Integer, nullable=False)
    asset_retention_days: Mapped[int] = mapped_column(Integer, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PlanEntitlement(Base):
    __tablename__ = "plan_entitlements"
    __table_args__ = (
        UniqueConstraint("plan_id", "entitlement_code", name="uq_plan_entitlements_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("membership_plans.id", ondelete="CASCADE"), nullable=False
    )
    entitlement_code: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[Any] = mapped_column(JSON_VALUE, nullable=False)


class UserMembership(Base):
    __tablename__ = "user_memberships"
    __table_args__ = (
        CheckConstraint(
            "status IN ('scheduled', 'active', 'expired', 'cancelled')",
            name="ck_user_memberships_status",
        ),
        CheckConstraint(
            "source IN ('admin', 'payment', 'promotion', 'migration', 'system')",
            name="ck_user_memberships_source",
        ),
        CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at",
            name="ck_user_memberships_time_range",
        ),
        Index(
            "uq_user_memberships_one_active",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        Index("ix_user_memberships_due", "status", "starts_at", "ends_at"),
        Index("ix_user_memberships_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("membership_plans.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reason: Mapped[str | None] = mapped_column(String(500))
    entitlement_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MembershipEvent(Base):
    __tablename__ = "membership_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('created', 'activated', 'renewed', 'upgraded', "
            "'downgraded', 'expired', 'cancelled')",
            name="ck_membership_events_type",
        ),
        UniqueConstraint(
            "actor_user_id",
            "idempotency_key",
            name="uq_membership_events_actor_idempotency",
        ),
        Index("ix_membership_events_membership_created", "membership_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    membership_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_memberships.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    old_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("membership_plans.id", ondelete="RESTRICT")
    )
    new_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("membership_plans.id", ondelete="RESTRICT")
    )
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reason: Mapped[str | None] = mapped_column(String(500))
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PointAccount(Base):
    __tablename__ = "point_accounts"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="ck_point_accounts_balance"),
        CheckConstraint(
            "lifetime_earned >= 0 AND lifetime_spent >= 0",
            name="ck_point_accounts_lifetime",
        ),
        CheckConstraint(
            "status IN ('active', 'frozen')",
            name="ck_point_accounts_status",
        ),
        Index("ix_point_accounts_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    balance: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lifetime_earned: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lifetime_spent: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PointTransaction(Base):
    __tablename__ = "point_transactions"
    __table_args__ = (
        CheckConstraint(
            "entry_type IN ('grant', 'consume', 'refund', 'adjust', "
            "'renewal', 'promotion', 'reversal')",
            name="ck_point_transactions_entry_type",
        ),
        CheckConstraint(
            "balance_before >= 0 AND balance_after >= 0",
            name="ck_point_transactions_balances",
        ),
        UniqueConstraint("user_id", "idempotency_key", name="uq_point_transactions_idempotency"),
        UniqueConstraint(
            "reference_type",
            "reference_id",
            "entry_type",
            name="uq_point_transactions_business_entry",
        ),
        Index("ix_point_transactions_user_created", "user_id", "created_at", "id"),
        Index("ix_point_transactions_account_created", "account_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("point_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False)
    delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_before: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reference_type: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    transaction_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON_VALUE, default=dict, nullable=False
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PointAdjustmentRequest(Base):
    __tablename__ = "point_adjustment_requests"
    __table_args__ = (
        CheckConstraint("amount <> 0", name="ck_point_adjustments_nonzero"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'applied')",
            name="ck_point_adjustments_status",
        ),
        UniqueConstraint(
            "requested_by",
            "idempotency_key",
            name="uq_point_adjustments_request_idempotency",
        ),
        UniqueConstraint("review_idempotency_key", name="uq_point_adjustments_review_idempotency"),
        Index("ix_point_adjustments_status_created", "status", "created_at"),
        Index("ix_point_adjustments_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("point_transactions.id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    review_idempotency_key: Mapped[str | None] = mapped_column(String(255))
    review_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OperationCatalog(Base):
    __tablename__ = "operation_catalog"
    __table_args__ = (
        CheckConstraint(
            "engine_type IN ('sub2api', 'local_model', 'local_code')",
            name="ck_operation_catalog_engine_type",
        ),
        CheckConstraint(
            "timeout_seconds > 0 AND max_attempts > 0",
            name="ck_operation_catalog_execution_limits",
        ),
        Index("ix_operation_catalog_enabled_code", "enabled", "code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    engine_type: Mapped[str] = mapped_column(String(32), nullable=False)
    queue_name: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class OperationPrice(Base):
    __tablename__ = "operation_prices"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_operation_prices_version"),
        CheckConstraint("base_points >= 0", name="ck_operation_prices_base_points"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_operation_prices_time_range",
        ),
        UniqueConstraint("operation_id", "version", name="uq_operation_prices_version"),
        Index(
            "ix_operation_prices_effective",
            "operation_id",
            "effective_from",
            "effective_to",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operation_catalog.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    base_points: Mapped[int] = mapped_column(Integer, nullable=False)
    parameter_rules: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, default=dict, nullable=False
    )
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class JobQuote(Base):
    __tablename__ = "job_quotes"
    __table_args__ = (
        CheckConstraint(
            "base_points >= 0 AND discount_points >= 0 AND surcharge_points >= 0 "
            "AND final_points >= 0",
            name="ck_job_quotes_points",
        ),
        Index("ix_job_quotes_user_created", "user_id", "created_at", "id"),
        Index("ix_job_quotes_expires", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    operation_code: Mapped[str] = mapped_column(
        ForeignKey("operation_catalog.code", ondelete="RESTRICT"), nullable=False
    )
    source_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    parameters_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    membership_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    pricing_version: Mapped[int] = mapped_column(Integer, nullable=False)
    base_points: Mapped[int] = mapped_column(Integer, nullable=False)
    discount_points: Mapped[int] = mapped_column(Integer, nullable=False)
    surcharge_points: Mapped[int] = mapped_column(Integer, nullable=False)
    final_points: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ImageJob(Base):
    __tablename__ = "image_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed', "
            "'cancelled', 'timed_out')",
            name="ck_image_jobs_status",
        ),
        CheckConstraint(
            "refund_status IN ('none', 'refunded')",
            name="ck_image_jobs_refund_status",
        ),
        CheckConstraint(
            "charged_points >= 0 AND attempt_count >= 0 AND progress BETWEEN 0 AND 100",
            name="ck_image_jobs_counters",
        ),
        UniqueConstraint("user_id", "idempotency_key", name="uq_image_jobs_idempotency"),
        UniqueConstraint("quote_id", name="uq_image_jobs_quote"),
        Index("ix_image_jobs_user_created", "user_id", "created_at", "id"),
        Index("ix_image_jobs_dispatch", "status", "next_attempt_at", "queued_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    operation_code: Mapped[str] = mapped_column(
        ForeignKey("operation_catalog.code", ondelete="RESTRICT"), nullable=False
    )
    source_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    output_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    quote_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("job_quotes.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    refund_status: Mapped[str] = mapped_column(String(16), default="none", nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    pricing_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    charged_points: Mapped[int] = mapped_column(Integer, nullable=False)
    charge_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("point_transactions.id", ondelete="RESTRICT")
    )
    refund_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("point_transactions.id", ondelete="RESTRICT")
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(255))
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'timed_out')",
            name="ck_job_attempts_status",
        ),
        CheckConstraint("attempt_no > 0", name="ck_job_attempts_number"),
        UniqueConstraint("job_id", "attempt_no", name="uq_job_attempts_number"),
        Index("ix_job_attempts_job_started", "job_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("image_jobs.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail_redacted: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict, nullable=False)


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('original', 'result', 'mask', 'thumbnail', 'vector')",
            name="ck_assets_kind",
        ),
        CheckConstraint(
            "status IN ('uploading', 'ready', 'quarantined', 'deleted')",
            name="ck_assets_status",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_assets_size_bytes"),
        CheckConstraint(
            "(width IS NULL AND height IS NULL) OR (width > 0 AND height > 0)",
            name="ck_assets_dimensions",
        ),
        Index("ix_assets_owner_created", "owner_id", "created_at", "id"),
        Index("ix_assets_root_created", "root_asset_id", "created_at", "id"),
        Index("ix_assets_status_retention", "status", "retention_until"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    root_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False
    )
    parent_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    source_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("image_jobs.id", ondelete="RESTRICT")
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    operation_code: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_provider: Mapped[str] = mapped_column(String(16), default="r2", nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    extension: Mapped[str] = mapped_column(String(16), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    has_alpha: Mapped[bool | None] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(16), default="uploading", nullable=False)
    asset_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON_VALUE, default=dict, nullable=False
    )
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AssetAccessLog(Base):
    __tablename__ = "asset_access_logs"
    __table_args__ = (
        CheckConstraint(
            "action IN ('preview', 'download', 'admin_preview')",
            name="ck_asset_access_logs_action",
        ),
        Index("ix_asset_access_logs_asset_created", "asset_id", "created_at"),
        Index("ix_asset_access_logs_actor_created", "actor_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ObjectDeletionQueue(Base):
    __tablename__ = "object_deletion_queue"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'completed', 'failed', 'cancelled')",
            name="ck_object_deletion_queue_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_object_deletion_queue_attempts"),
        UniqueConstraint("asset_id", name="uq_object_deletion_queue_asset"),
        Index("ix_object_deletion_queue_due", "status", "execute_after"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    execute_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


_SENSITIVE_AUDIT_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "ciphertext",
        "cookie",
        "image",
        "password",
        "prompt",
        "secret",
        "secret_access_key",
        "token",
    }
)


def _redact_audit_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if str(key).casefold() in _SENSITIVE_AUDIT_KEYS
                else _redact_audit_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_audit_value(item) for item in value]
    if isinstance(value, bytes):
        return "[BINARY REDACTED]"
    return value


def _audit_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _actor_roles(connection, actor_user_id: uuid.UUID | None) -> list[str]:
    if actor_user_id is None:
        return []
    statement = (
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(
            UserRole.user_id == actor_user_id,
            or_(UserRole.expires_at.is_(None), UserRole.expires_at > datetime.now(UTC)),
        )
        .order_by(Role.code)
    )
    return list(connection.execute(statement).scalars())


@event.listens_for(OutboxEvent, "after_insert")
def _project_audit_event(_mapper, connection, target: OutboxEvent) -> None:
    if not (target.topic.endswith(".audit") or target.topic.startswith("identity.")):
        return
    from app.services.logging import ip_hash_context, request_id_context, user_agent_context

    payload = target.payload if isinstance(target.payload, dict) else {}
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    action = str(payload.get("action") or target.topic)[:200]
    actor_user_id = _audit_uuid(payload.get("actor_user_id") or details.get("actor_user_id"))
    result = str(payload.get("result") or "")
    if result not in {"success", "denied", "failed"}:
        result = "denied" if "denied" in action else "failed" if "failed" in action else "success"
    ip_hash = str(details.get("ip_hash") or ip_hash_context.get() or "")
    if len(ip_hash) != 64:
        ip_hash = hashlib.sha256((ip_hash or "unknown").encode()).hexdigest()
    occurred_at = target.created_at or datetime.now(UTC)
    request_id = str(payload.get("request_id") or request_id_context.get() or "system")[:128]
    connection.execute(
        AuditLog.__table__.insert().values(
            id=uuid7(),
            occurred_at=occurred_at,
            actor_user_id=actor_user_id,
            actor_role_snapshot=_actor_roles(connection, actor_user_id),
            action=action,
            target_type=target.aggregate_type[:100],
            target_id=target.aggregate_id,
            result=result,
            request_id=request_id,
            ip_hash=ip_hash,
            user_agent=(user_agent_context.get() or None),
            reason=str(details.get("reason"))[:500] if details.get("reason") else None,
            changes_redacted=_redact_audit_value(details),
            metadata=_redact_audit_value(
                {
                    "topic": target.topic,
                    "subject_user_id": payload.get("subject_user_id") or payload.get("user_id"),
                }
            ),
        )
    )
    if action == "login.failed" and details.get("failure_code") == "rate_limited":
        connection.execute(
            SecurityEvent.__table__.insert().values(
                id=uuid7(),
                event_type="login_rate_limited",
                severity="high",
                user_id=_audit_uuid(payload.get("subject_user_id")),
                ip_hash=ip_hash,
                request_id=request_id,
                status="open",
                details_redacted={"failure_code": "rate_limited"},
                created_at=occurred_at,
            )
        )


@event.listens_for(AuditLog, "before_update")
@event.listens_for(AuditLog, "before_delete")
def _prevent_audit_mutation(_mapper, _connection, _target: AuditLog) -> None:
    raise ValueError("audit_logs is append-only")
