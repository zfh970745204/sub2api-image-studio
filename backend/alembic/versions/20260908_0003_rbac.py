"""Create and seed the RBAC permission catalog."""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260908_0003"
down_revision: str | None = "20260908_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = (
    ("studio.use", "studio", "使用图片工作台"),
    ("assets.read_own", "assets", "查看本人素材"),
    ("assets.write_own", "assets", "创建和修改本人素材"),
    ("assets.delete_own", "assets", "删除符合约束的本人素材"),
    ("tasks.read_own", "tasks", "查看本人任务"),
    ("tasks.create", "tasks", "创建图片任务"),
    ("tasks.cancel_own", "tasks", "取消本人排队任务"),
    ("points.read_own", "points", "查看本人积分"),
    ("membership.read_own", "memberships", "查看本人会员权益"),
    ("admin.dashboard.read", "admin", "查看管理后台概览"),
    ("users.read", "users", "查看用户"),
    ("users.manage", "users", "管理用户"),
    ("roles.read", "roles", "查看角色和权限"),
    ("roles.manage", "roles", "管理角色和用户角色"),
    ("memberships.read", "memberships", "查看会员状态"),
    ("memberships.manage", "memberships", "管理会员状态"),
    ("points.read", "points", "查看积分账户"),
    ("points.adjust", "points", "调整积分"),
    ("pricing.read", "pricing", "查看计价配置"),
    ("pricing.manage", "pricing", "管理计价配置"),
    ("tasks.read", "tasks", "查看所有任务"),
    ("tasks.manage", "tasks", "管理所有任务"),
    ("assets.read", "assets", "查看所有素材"),
    ("assets.manage", "assets", "管理所有素材"),
    ("config.read", "config", "查看配置状态和掩码"),
    ("config.manage", "config", "覆盖和停用配置"),
    ("config.test", "config", "测试外部配置"),
    ("audit.read", "audit", "查看审计日志"),
    ("audit.export", "audit", "导出审计日志"),
    ("security.events.read", "security", "查看安全事件"),
    ("security.policies.manage", "security", "管理安全策略"),
    ("system.health.read", "system", "查看系统健康与服务状态"),
    ("system.diagnostics.read", "system", "查看系统诊断详情"),
    ("system.maintenance.execute", "system", "执行系统维护任务"),
)

USER_PERMISSIONS = (
    "studio.use",
    "assets.read_own",
    "assets.write_own",
    "assets.delete_own",
    "tasks.read_own",
    "tasks.create",
    "tasks.cancel_own",
    "points.read_own",
    "membership.read_own",
)
OPERATOR_PERMISSIONS = (
    "admin.dashboard.read",
    "users.read",
    "users.manage",
    "memberships.read",
    "points.read",
    "pricing.read",
    "tasks.read",
    "tasks.manage",
    "assets.read",
    "assets.manage",
    "system.health.read",
)
FINANCE_PERMISSIONS = (
    "admin.dashboard.read",
    "users.read",
    "memberships.read",
    "memberships.manage",
    "points.read",
    "points.adjust",
    "pricing.read",
    "pricing.manage",
    "tasks.read",
)
AUDITOR_PERMISSIONS = (
    "admin.dashboard.read",
    "users.read",
    "roles.read",
    "memberships.read",
    "points.read",
    "pricing.read",
    "tasks.read",
    "assets.read",
    "config.read",
    "audit.read",
    "security.events.read",
    "system.health.read",
)
ALL_PERMISSION_CODES = tuple(code for code, _, _ in PERMISSIONS)
SYSTEM_ROLES = (
    ("user", "普通用户", "所有已激活账号的基础工作台角色", USER_PERMISSIONS),
    ("operator", "运营", "用户、任务和素材运营处理", OPERATOR_PERMISSIONS),
    ("finance", "财务", "会员、积分和计价管理", FINANCE_PERMISSIONS),
    ("auditor", "审计员", "后台、配置状态和审计只读", AUDITOR_PERMISSIONS),
    ("super_admin", "超级管理员", "拥有全部已定义权限的系统角色", ALL_PERMISSION_CODES),
)


def _seed_id(kind: str, code: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"sub2image:{kind}:{code}")


def upgrade() -> None:
    op.create_index("ix_user_roles_role_id", "user_roles", ["role_id"])
    op.create_table(
        "permissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("module", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["permission_id"], ["permissions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("role_id", "permission_id"),
    )

    for code, module, description in PERMISSIONS:
        op.execute(
            sa.text(
                "INSERT INTO permissions (id, code, module, description) "
                "VALUES (:id, :code, :module, :description)"
            ).bindparams(
                id=_seed_id("permission", code),
                code=code,
                module=module,
                description=description,
            )
        )

    for code, name, description, permission_codes in SYSTEM_ROLES:
        op.execute(
            sa.text(
                "INSERT INTO roles (id, code, name, description, is_system) "
                "VALUES (:id, :code, :name, :description, true) "
                "ON CONFLICT (code) DO UPDATE SET "
                "name = EXCLUDED.name, description = EXCLUDED.description, is_system = true"
            ).bindparams(
                id=_seed_id("role", code),
                code=code,
                name=name,
                description=description,
            )
        )
        for permission_code in permission_codes:
            op.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "SELECT roles.id, permissions.id FROM roles, permissions "
                    "WHERE roles.code = :role_code AND permissions.code = :permission_code "
                    "ON CONFLICT DO NOTHING"
                ).bindparams(role_code=code, permission_code=permission_code)
            )

    op.execute(
        sa.text(
            "INSERT INTO user_roles (user_id, role_id, assigned_by) "
            "SELECT users.id, roles.id, users.id FROM users, roles "
            "WHERE users.deleted_at IS NULL AND roles.code = 'user' "
            "ON CONFLICT DO NOTHING"
        )
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
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_index("ix_user_roles_role_id", table_name="user_roles")
