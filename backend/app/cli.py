from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.api.auth import validate_email, validate_password
from app.config import get_settings
from app.domain.ids import uuid7
from app.migrations.local_results import migrate_local_results
from app.object_storage import build_object_storage
from app.repositories.database import Database
from app.repositories.models import LoginAttempt, Role, User, UserRole
from app.services.auth import AuthService
from app.services.memberships import EntitlementService, sync_builtin_membership_plans
from app.services.points import PointService
from app.services.rbac import sync_builtin_rbac


async def create_admin() -> int:
    settings = get_settings()
    service = AuthService(settings)
    membership_service = EntitlementService()
    point_service = PointService()
    database = Database(
        settings.database_url,
        timeout_seconds=settings.dependency_timeout_seconds,
    )
    try:
        email = validate_email(input("管理员邮箱: "))
        password = getpass.getpass("密码（12-128 个字符）: ")
        confirmation = getpass.getpass("再次输入密码: ")
        if password != confirmation:
            print("两次输入的密码不一致。", file=sys.stderr)
            return 2
        validate_password(password)

        async with database.session_factory() as session:
            await sync_builtin_rbac(session)
            await sync_builtin_membership_plans(session)
            await session.execute(
                select(Role.id).where(Role.code == "super_admin").with_for_update()
            )
            existing_admins = await session.scalar(
                select(func.count(UserRole.user_id))
                .join(Role, Role.id == UserRole.role_id)
                .where(Role.code == "super_admin")
            )
            if existing_admins:
                print("超级管理员已存在；后续角色分配必须通过管理后台完成。", file=sys.stderr)
                return 3

            user = User(
                id=uuid7(),
                email=email,
                username=None,
                password_hash=service.hash_password(password),
                display_name=email.split("@", 1)[0],
                status="active",
                email_verified_at=datetime.now(UTC),
                session_version=1,
                permission_version=1,
            )
            session.add(user)
            try:
                await session.flush()
                user_role = await service.ensure_role(
                    session,
                    code="user",
                    name="普通用户",
                    description="已激活账号的基础工作台角色",
                )
                super_admin_role = await service.ensure_role(
                    session,
                    code="super_admin",
                    name="超级管理员",
                    description="拥有全部后台权限的系统角色",
                )
                await service.assign_role(
                    session, user_id=user.id, role=user_role, assigned_by=user.id
                )
                await service.assign_role(
                    session, user_id=user.id, role=super_admin_role, assigned_by=user.id
                )
                await membership_service.ensure_default_membership(
                    session,
                    user.id,
                    assigned_by=user.id,
                    request_id=f"cli-{uuid7().hex}",
                )
                await point_service.ensure_onboarding_grant(
                    session,
                    user.id,
                    points=settings.onboarding_points,
                    request_id=f"cli-{uuid7().hex}",
                )
                service.record_audit(
                    session,
                    action="super_admin.created",
                    aggregate_type="user",
                    aggregate_id=user.id,
                    actor_user_id=user.id,
                    subject_user_id=user.id,
                    request_id=f"cli-{uuid7().hex}",
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                print("该邮箱已存在，未创建管理员。", file=sys.stderr)
                return 4
        print(f"已创建首个超级管理员：{email}")
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        await database.dispose()


async def reset_admin_password() -> int:
    settings = get_settings()
    service = AuthService(settings)
    database = Database(
        settings.database_url,
        timeout_seconds=settings.dependency_timeout_seconds,
    )
    try:
        email = validate_email(input("管理员邮箱: "))
        password = getpass.getpass("新密码（12-128 个字符）: ")
        confirmation = getpass.getpass("再次输入新密码: ")
        if password != confirmation:
            print("两次输入的密码不一致。", file=sys.stderr)
            return 2
        validate_password(password)

        async with database.session_factory() as session:
            statement = (
                select(User)
                .join(UserRole, UserRole.user_id == User.id)
                .join(Role, Role.id == UserRole.role_id)
                .where(
                    User.email == email,
                    User.deleted_at.is_(None),
                    Role.code == "super_admin",
                )
                .with_for_update()
            )
            user = (await session.execute(statement)).scalar_one_or_none()
            if user is None:
                print("未找到有效的超级管理员账户。", file=sys.stderr)
                return 3
            if user.status not in {"active", "locked"}:
                print("超级管理员账户已被停用，不能通过该命令重新启用。", file=sys.stderr)
                return 4

            user.password_hash = service.hash_password(password)
            user.status = "active"
            user.locked_until = None
            user.session_version += 1
            await service.revoke_user_sessions(
                session,
                user=user,
                reason="password_reset_by_cli",
            )
            account_fingerprint = service.fingerprint(service.normalize_identifier(email))
            await session.execute(
                delete(LoginAttempt).where(LoginAttempt.account_fingerprint == account_fingerprint)
            )
            service.record_audit(
                session,
                action="super_admin.password_reset_by_cli",
                aggregate_type="user",
                aggregate_id=user.id,
                actor_user_id=None,
                subject_user_id=user.id,
                request_id=f"cli-{uuid7().hex}",
            )
            await session.commit()
        print(f"已重设超级管理员密码并解除锁定：{email}")
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        await database.dispose()


async def migrate_results(
    *,
    owner_id: uuid.UUID,
    catalog_path: Path,
    result_dir: Path,
    dry_run: bool,
) -> int:
    settings = get_settings()
    database = Database(
        settings.database_url,
        timeout_seconds=settings.dependency_timeout_seconds,
    )
    storage = build_object_storage(settings)
    if not dry_run and not storage.bucket:
        print("R2 尚未配置，不能执行迁移。", file=sys.stderr)
        return 2
    try:
        async with database.session_factory() as session:
            user = await session.get(User, owner_id)
            if user is None:
                print("迁移目标用户不存在。", file=sys.stderr)
                return 2
            entitlement = await EntitlementService().current_snapshot(
                session, owner_id, request_id="cli-local-results-migration"
            )
            await session.commit()
        report = await migrate_local_results(
            database,
            storage,
            owner_id=owner_id,
            catalog_path=catalog_path,
            result_dir=result_dir,
            retention_days=entitlement.retention_days,
            dry_run=dry_run,
        )
        print(
            f"发现 {report.discovered}，迁移 {report.migrated}，"
            f"校验 {report.verified}，仅检查 {report.skipped}，失败 {len(report.failures)}。"
        )
        for failure in report.failures:
            print(f"- {failure}", file=sys.stderr)
        return 1 if report.failures else 0
    finally:
        await database.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("create-admin", help="交互式创建首个超级管理员")
    subparsers.add_parser("reset-admin-password", help="交互式重设超级管理员密码并解除登录锁定")
    migration = subparsers.add_parser(
        "migrate-local-results", help="迁移旧 SQLite 素材及本地结果到 R2"
    )
    migration.add_argument("--owner-id", type=uuid.UUID, required=True)
    migration.add_argument("--catalog-path", type=Path, default=Path("backend/data/studio.db"))
    migration.add_argument("--result-dir", type=Path, default=Path("backend/data/results"))
    migration.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "create-admin":
        return asyncio.run(create_admin())
    if args.command == "reset-admin-password":
        return asyncio.run(reset_admin_password())
    if args.command == "migrate-local-results":
        return asyncio.run(
            migrate_results(
                owner_id=args.owner_id,
                catalog_path=args.catalog_path,
                result_dir=args.result_dir,
                dry_run=args.dry_run,
            )
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
