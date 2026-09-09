from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PermissionSeed:
    code: str
    module: str
    description: str


@dataclass(frozen=True, slots=True)
class RoleSeed:
    code: str
    name: str
    description: str
    permissions: frozenset[str]


PERMISSIONS = (
    PermissionSeed("studio.use", "studio", "使用图片工作台"),
    PermissionSeed("assets.read_own", "assets", "查看本人素材"),
    PermissionSeed("assets.write_own", "assets", "创建和修改本人素材"),
    PermissionSeed("assets.delete_own", "assets", "删除符合约束的本人素材"),
    PermissionSeed("tasks.read_own", "tasks", "查看本人任务"),
    PermissionSeed("tasks.create", "tasks", "创建图片任务"),
    PermissionSeed("tasks.cancel_own", "tasks", "取消本人排队任务"),
    PermissionSeed("points.read_own", "points", "查看本人积分"),
    PermissionSeed("membership.read_own", "memberships", "查看本人会员权益"),
    PermissionSeed("admin.dashboard.read", "admin", "查看管理后台概览"),
    PermissionSeed("users.read", "users", "查看用户"),
    PermissionSeed("users.manage", "users", "管理用户"),
    PermissionSeed("roles.read", "roles", "查看角色和权限"),
    PermissionSeed("roles.manage", "roles", "管理角色和用户角色"),
    PermissionSeed("memberships.read", "memberships", "查看会员状态"),
    PermissionSeed("memberships.manage", "memberships", "管理会员状态"),
    PermissionSeed("points.read", "points", "查看积分账户"),
    PermissionSeed("points.adjust", "points", "调整积分"),
    PermissionSeed("pricing.read", "pricing", "查看计价配置"),
    PermissionSeed("pricing.manage", "pricing", "管理计价配置"),
    PermissionSeed("tasks.read", "tasks", "查看所有任务"),
    PermissionSeed("tasks.manage", "tasks", "管理所有任务"),
    PermissionSeed("assets.read", "assets", "查看所有素材"),
    PermissionSeed("assets.manage", "assets", "管理所有素材"),
    PermissionSeed("config.read", "config", "查看配置状态和掩码"),
    PermissionSeed("config.manage", "config", "覆盖和停用配置"),
    PermissionSeed("config.test", "config", "测试外部配置"),
    PermissionSeed("audit.read", "audit", "查看审计日志"),
    PermissionSeed("audit.export", "audit", "导出审计日志"),
    PermissionSeed("security.events.read", "security", "查看安全事件"),
    PermissionSeed("security.policies.manage", "security", "管理安全策略"),
    PermissionSeed("system.health.read", "system", "查看系统健康与服务状态"),
    PermissionSeed("system.diagnostics.read", "system", "查看系统诊断详情"),
    PermissionSeed("system.maintenance.execute", "system", "执行系统维护任务"),
)

USER_PERMISSIONS = frozenset(
    {
        "studio.use",
        "assets.read_own",
        "assets.write_own",
        "assets.delete_own",
        "tasks.read_own",
        "tasks.create",
        "tasks.cancel_own",
        "points.read_own",
        "membership.read_own",
    }
)

OPERATOR_PERMISSIONS = frozenset(
    {
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
    }
)

FINANCE_PERMISSIONS = frozenset(
    {
        "admin.dashboard.read",
        "users.read",
        "memberships.read",
        "memberships.manage",
        "points.read",
        "points.adjust",
        "pricing.read",
        "pricing.manage",
        "tasks.read",
    }
)

AUDITOR_PERMISSIONS = frozenset(
    {
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
    }
)

PERMISSION_CODES = frozenset(permission.code for permission in PERMISSIONS)

SYSTEM_ROLES = (
    RoleSeed("user", "普通用户", "所有已激活账号的基础工作台角色", USER_PERMISSIONS),
    RoleSeed("operator", "运营", "用户、任务和素材运营处理", OPERATOR_PERMISSIONS),
    RoleSeed("finance", "财务", "会员、积分和计价管理", FINANCE_PERMISSIONS),
    RoleSeed("auditor", "审计员", "后台、配置状态和审计只读", AUDITOR_PERMISSIONS),
    RoleSeed("super_admin", "超级管理员", "拥有全部已定义权限的系统角色", PERMISSION_CODES),
)

SYSTEM_ROLE_BY_CODE = {role.code: role for role in SYSTEM_ROLES}
