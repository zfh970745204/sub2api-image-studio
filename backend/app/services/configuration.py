from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import os
import smtplib
import ssl
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import ApiError
from app.config import Settings
from app.domain.ids import uuid7
from app.object_storage import ObjectStorage, build_object_storage
from app.repositories.models import (
    ConfigGroup,
    ConfigTestRun,
    ConfigVersion,
    EncryptedSecret,
    MembershipPlan,
    OutboxEvent,
)
from app.sub2api import Sub2APIClient

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _http_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("必须是有效的 HTTP 或 HTTPS 地址")
    if parsed.username or parsed.password:
        raise ValueError("地址中不能包含用户名或密码")
    return normalized


class StrictValues(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Sub2APIProfile(StrictValues):
    id: str = Field(default="primary", min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    name: str = Field(default="主线路", min_length=1, max_length=80)
    enabled: bool = True
    priority: int = Field(default=1, ge=1, le=10000)
    base_url: str = Field(default="", max_length=2048)
    image_model: str = Field(default="gpt-image-2", min_length=1, max_length=200)
    timeout_seconds: float = Field(default=180.0, gt=0, le=600)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("线路名称不能为空")
        return normalized

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return _http_url(value)

    @field_validator("image_model")
    @classmethod
    def strip_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("图片模型不能为空")
        return normalized

    @model_validator(mode="after")
    def require_url_when_enabled(self) -> Sub2APIProfile:
        if self.enabled and not self.base_url:
            raise ValueError("启用 Sub2API 线路前必须填写接口地址")
        return self


class Sub2APIValues(StrictValues):
    enabled: bool = False
    base_url: str = Field(default="", max_length=2048)
    image_model: str = Field(default="gpt-image-2", min_length=1, max_length=200)
    timeout_seconds: float = Field(default=180.0, gt=0, le=600)
    profiles: list[Sub2APIProfile] = Field(default_factory=list, max_length=20)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return _http_url(value)

    @field_validator("image_model")
    @classmethod
    def strip_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("图片模型不能为空")
        return normalized

    @model_validator(mode="after")
    def require_url_when_enabled(self) -> Sub2APIValues:
        if self.enabled and not self.base_url and not any(
            profile.enabled and profile.base_url for profile in self.profiles
        ):
            raise ValueError("启用 Sub2API 前必须填写至少一条接口地址")
        identifiers = [profile.id for profile in self.profiles]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Sub2API 线路 ID 不能重复")
        return self


class R2Values(StrictValues):
    enabled: bool = False
    endpoint_url: str = Field(default="", max_length=2048)
    account_id: str = Field(default="", max_length=100)
    bucket: str = Field(default="", max_length=255)
    region: str = Field(default="auto", min_length=1, max_length=64)

    @field_validator("endpoint_url")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        return _http_url(value)

    @field_validator("account_id", "bucket", "region")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def require_storage_when_enabled(self) -> R2Values:
        if self.enabled and not (self.endpoint_url and self.account_id and self.bucket):
            raise ValueError("启用 R2 前必须填写 Endpoint、Account ID 和 Bucket")
        if self.enabled and not self.endpoint_url.startswith("https://"):
            raise ValueError("R2 Endpoint 必须使用 HTTPS")
        return self


class EmailValues(StrictValues):
    enabled: bool = False
    provider: Literal["smtp", "api"] = "smtp"
    host: str = Field(default="", max_length=255)
    port: int = Field(default=587, ge=1, le=65535)
    username: str = Field(default="", max_length=320)
    api_base_url: str = Field(default="", max_length=2048)
    from_email: str = Field(default="", max_length=320)
    use_tls: bool = True

    @field_validator("host", "username", "from_email")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("api_base_url")
    @classmethod
    def validate_api_url(cls, value: str) -> str:
        return _http_url(value)

    @model_validator(mode="after")
    def require_provider_fields(self) -> EmailValues:
        if not self.enabled:
            return self
        if not self.from_email or parseaddr(self.from_email)[1] != self.from_email:
            raise ValueError("启用邮件服务前必须填写有效的发件人地址")
        if self.provider == "smtp" and not self.host:
            raise ValueError("SMTP 服务必须填写主机")
        if self.provider == "api" and not self.api_base_url:
            raise ValueError("邮件 API 服务必须填写接口地址")
        return self


class GeneralValues(StrictValues):
    registration_enabled: bool = True
    password_reset_enabled: bool = False
    default_membership_code: str = Field(
        default="free", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$"
    )
    default_points: int = Field(default=20, ge=0, le=1_000_000)
    max_upload_mb: int = Field(default=20, ge=1, le=100)
    max_image_megapixels: int = Field(default=40, ge=1, le=200)
    signed_url_ttl_seconds: int = Field(default=600, ge=300, le=900)
    task_concurrency: int = Field(default=2, ge=1, le=64)

    @field_validator("default_membership_code")
    @classmethod
    def strip_membership(cls, value: str) -> str:
        return value.strip()


class BrandingValues(StrictValues):
    site_name: str = Field(default="Sub2Image", min_length=1, max_length=60)
    logo_url: str = "/brand-symbol.svg"
    login_image_url: str = "/brand/login-art.webp"
    register_image_url: str = "/brand/register-art.webp"
    home_image_url: str = "/brand/home-art.webp"

    @field_validator("site_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("网站名称不能为空")
        return value.strip()

    @field_validator("logo_url", "login_image_url", "register_image_url", "home_image_url")
    @classmethod
    def public_image_url(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 2048 or any(c in value for c in ("\\", "%", "..", "?", "#")):
            raise ValueError("图片地址无效，请上传图片或填写 HTTPS 图片地址")
        if value == "/brand-symbol.svg" or value.startswith(("/brand/", "/api/v1/site/media/")):
            return value
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("请上传图片或填写 HTTPS 图片地址")
        return value


@dataclass(frozen=True, slots=True)
class GroupDefinition:
    name: str
    model: type[StrictValues]
    secret_keys: frozenset[str]


GROUP_DEFINITIONS: dict[str, GroupDefinition] = {
    "branding": GroupDefinition("网站名称与品牌配图", BrandingValues, frozenset()),
    "sub2api": GroupDefinition("Sub2API", Sub2APIValues, frozenset({"api_key"})),
    "r2": GroupDefinition(
        "Cloudflare R2", R2Values, frozenset({"access_key_id", "secret_access_key"})
    ),
    "email": GroupDefinition("邮件服务", EmailValues, frozenset({"password", "api_key"})),
    "general": GroupDefinition("通用业务配置", GeneralValues, frozenset()),
}
SENSITIVE_GROUPS = frozenset({"sub2api", "r2", "email"})


class ConfigLoadError(RuntimeError):
    pass


class ConfigCipher:
    def __init__(self, key: bytes, *, key_version: int = 1) -> None:
        if len(key) != 32:
            raise ValueError("APP_CONFIG_MASTER_KEY must decode to exactly 32 bytes")
        self._key = key
        self._cipher = AESGCM(key)
        self.key_version = key_version

    @classmethod
    def from_settings(cls, settings: Settings) -> ConfigCipher:
        return cls(
            decode_master_key(settings.app_config_master_key, production=settings.production)
        )

    def encrypt(self, group: str, key_name: str, value: str) -> tuple[bytes, bytes, str, str]:
        nonce = os.urandom(12)
        plaintext = value.encode("utf-8")
        ciphertext = self._cipher.encrypt(nonce, plaintext, self._aad(group, key_name))
        fingerprint = hmac.new(self._key, plaintext, hashlib.sha256).hexdigest()
        return ciphertext, nonce, fingerprint, value[-4:]

    def decrypt(self, group: str, secret: EncryptedSecret) -> str:
        if secret.key_version != self.key_version:
            raise ConfigLoadError("配置密钥版本不受支持")
        try:
            plaintext = self._cipher.decrypt(
                bytes(secret.nonce),
                bytes(secret.ciphertext),
                self._aad(group, secret.key_name),
            )
        except InvalidTag as exc:
            raise ConfigLoadError("配置密钥完整性校验失败") from exc
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigLoadError("配置密钥编码无效") from exc

    @staticmethod
    def _aad(group: str, key_name: str) -> bytes:
        return f"config:{group}:{key_name}".encode()


def decode_master_key(value: str, *, production: bool) -> bytes:
    raw = value.strip()
    if not raw:
        if production:
            raise ValueError("APP_CONFIG_MASTER_KEY is required in production")
        return hashlib.sha256(b"sub2image-development-only-config-master-key").digest()
    raw = raw.removeprefix("base64:")
    if len(raw) == 64:
        try:
            decoded = bytes.fromhex(raw)
        except ValueError:
            decoded = b""
        if len(decoded) == 32:
            return decoded
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, binascii.Error):
        decoded = b""
    if len(decoded) == 32:
        return decoded
    if not production and len(raw.encode("utf-8")) == 32:
        return raw.encode("utf-8")
    raise ValueError("APP_CONFIG_MASTER_KEY must be a base64 or hex encoded 256-bit key")


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    group: str
    version: int
    values: dict[str, Any]
    secrets: dict[str, str]


@dataclass(frozen=True, slots=True)
class TestOutcome:
    succeeded: bool
    code: str
    message: str
    latency_ms: int


class ConfigService:
    def __init__(self, cipher: ConfigCipher) -> None:
        self.cipher = cipher

    async def ensure_groups(self, session: AsyncSession) -> None:
        existing = set((await session.scalars(select(ConfigGroup.code))).all())
        for code, definition in GROUP_DEFINITIONS.items():
            if code not in existing:
                session.add(ConfigGroup(id=uuid7(), code=code, name=definition.name))
        await session.flush()

    async def group(self, session: AsyncSession, code: str, *, lock: bool = False) -> ConfigGroup:
        self.definition(code)
        statement = select(ConfigGroup).where(ConfigGroup.code == code)
        if lock:
            statement = statement.with_for_update()
        group = (await session.scalars(statement)).one_or_none()
        if group is None:
            await self.ensure_groups(session)
            group = (await session.scalars(statement)).one()
        return group

    @staticmethod
    def definition(code: str) -> GroupDefinition:
        definition = GROUP_DEFINITIONS.get(code)
        if definition is None:
            raise ApiError(404, "CONFIG_GROUP_NOT_FOUND", "配置组不存在")
        return definition

    def validated_values(self, code: str, values: dict[str, Any]) -> dict[str, Any]:
        definition = self.definition(code)
        try:
            parsed = definition.model.model_validate(values)
        except ValidationError as exc:
            raise ApiError(
                422,
                "INVALID_CONFIG_VALUES",
                "配置字段校验失败",
                [
                    {
                        "field": ".".join(str(part) for part in error["loc"]),
                        "message": error["msg"],
                    }
                    for error in exc.errors()
                ],
            ) from exc
        return parsed.model_dump(mode="json")

    def validate_secret_update(
        self, code: str, secrets: dict[str, str], *, confirmed: bool
    ) -> dict[str, str]:
        definition = self.definition(code)
        unknown = {
            key
            for key in secrets
            if key not in definition.secret_keys
            and not (code == "sub2api" and key.startswith("api_key_") and key[8:])
        }
        if unknown:
            raise ApiError(422, "UNKNOWN_SECRET_KEY", "包含不支持的密钥字段")
        updates = {key: value.strip() for key, value in secrets.items() if value.strip()}
        if any(
            len(value) > 4096 or any(ord(char) < 32 for char in value) for value in updates.values()
        ):
            raise ApiError(422, "INVALID_SECRET_VALUE", "密钥格式或长度无效")
        if updates and code in SENSITIVE_GROUPS and not confirmed:
            raise ApiError(409, "SENSITIVE_CHANGE_CONFIRMATION_REQUIRED", "修改密钥需要二次确认")
        return updates

    async def create_draft(
        self,
        session: AsyncSession,
        *,
        code: str,
        actor_user_id: uuid.UUID,
        values: dict[str, Any],
        secrets: dict[str, str],
        change_reason: str,
        confirmed: bool,
        request_id: str,
    ) -> ConfigVersion:
        group = await self.group(session, code, lock=True)
        base = await self._active_version(session, group)
        merged = dict(base.values) if base is not None else {}
        merged.update(values)
        normalized = self.validated_values(code, merged)
        secret_updates = self.validate_secret_update(code, secrets, confirmed=confirmed)
        next_version = (
            int(
                (
                    await session.scalar(
                        select(func.max(ConfigVersion.version)).where(
                            ConfigVersion.group_id == group.id
                        )
                    )
                )
                or 0
            )
            + 1
        )
        version = ConfigVersion(
            id=uuid7(),
            group_id=group.id,
            version=next_version,
            status="draft",
            values=normalized,
            created_by=actor_user_id,
            published_by=None,
            change_reason=change_reason,
            published_at=None,
        )
        session.add(version)
        await session.flush()
        if base is not None:
            await self._copy_secrets(session, code, base.id, version.id)
        await self._apply_secret_updates(session, code, version.id, secret_updates)
        self._audit(
            session,
            action="draft_created",
            group=group,
            version=version,
            actor_user_id=actor_user_id,
            request_id=request_id,
            details={"changed_secret_keys": sorted(secret_updates)},
        )
        return version

    async def update_draft(
        self,
        session: AsyncSession,
        *,
        code: str,
        version_number: int,
        actor_user_id: uuid.UUID,
        values: dict[str, Any] | None,
        secrets: dict[str, str] | None,
        change_reason: str | None,
        confirmed: bool,
        request_id: str,
    ) -> ConfigVersion:
        group = await self.group(session, code)
        version = await self._version(session, group.id, version_number, lock=True)
        if version.status != "draft":
            raise ApiError(409, "CONFIG_VERSION_NOT_DRAFT", "仅草稿版本可修改")
        merged = dict(version.values)
        merged.update(values or {})
        version.values = self.validated_values(code, merged)
        secret_updates = self.validate_secret_update(code, secrets or {}, confirmed=confirmed)
        await self._apply_secret_updates(session, code, version.id, secret_updates)
        if change_reason is not None:
            version.change_reason = change_reason
        version.updated_at = utcnow()
        await session.execute(
            update(ConfigTestRun)
            .where(
                ConfigTestRun.config_version_id == version.id,
                ConfigTestRun.status == "succeeded",
            )
            .values(
                status="failed",
                result_code="CONFIG_CHANGED_AFTER_TEST",
                result_message_redacted="配置在测试后发生变更，请重新测试",
                completed_at=utcnow(),
            )
        )
        self._audit(
            session,
            action="draft_updated",
            group=group,
            version=version,
            actor_user_id=actor_user_id,
            request_id=request_id,
            details={"changed_secret_keys": sorted(secret_updates)},
        )
        return version

    async def resolved(
        self, session: AsyncSession, code: str, version_number: int | None = None
    ) -> ResolvedConfig:
        group = await self.group(session, code)
        number = group.active_version if version_number is None else version_number
        if number is None:
            raise ConfigLoadError(f"{code} 配置尚未发布")
        version = await self._version(session, group.id, number)
        if version_number is None and version.status != "active":
            raise ConfigLoadError(f"{code} 激活版本状态无效")
        rows = list(
            (
                await session.scalars(
                    select(EncryptedSecret).where(EncryptedSecret.config_version_id == version.id)
                )
            ).all()
        )
        secrets = {item.key_name: self.cipher.decrypt(code, item) for item in rows}
        return ResolvedConfig(code, version.version, dict(version.values), secrets)

    async def masked_version(
        self, session: AsyncSession, code: str, version: ConfigVersion
    ) -> dict[str, Any]:
        rows = list(
            (
                await session.scalars(
                    select(EncryptedSecret).where(EncryptedSecret.config_version_id == version.id)
                )
            ).all()
        )
        by_key = {row.key_name: row for row in rows}
        latest_test = (
            await session.scalars(
                select(ConfigTestRun)
                .where(ConfigTestRun.config_version_id == version.id)
                .order_by(ConfigTestRun.created_at.desc())
                .limit(1)
            )
        ).one_or_none()
        definition = self.definition(code)
        secret_names = set(definition.secret_keys)
        if code == "sub2api":
            secret_names.update(
                row.key_name
                for row in by_key.values()
                if row.key_name.startswith("api_key_")
            )
        return {
            "id": version.id,
            "version": version.version,
            "status": version.status,
            "values": dict(version.values),
            "secrets": {
                key: {
                    "has_value": key in by_key,
                    "last_four": by_key[key].last_four if key in by_key else None,
                    "updated_at": by_key[key].created_at if key in by_key else None,
                }
                for key in sorted(secret_names)
            },
            "created_by": version.created_by,
            "published_by": version.published_by,
            "change_reason": version.change_reason,
            "created_at": version.created_at,
            "updated_at": version.updated_at,
            "published_at": version.published_at,
            "latest_test": (
                {
                    "id": latest_test.id,
                    "status": latest_test.status,
                    "result_code": latest_test.result_code,
                    "result_message_redacted": latest_test.result_message_redacted,
                    "latency_ms": latest_test.latency_ms,
                    "created_at": latest_test.created_at,
                    "completed_at": latest_test.completed_at,
                }
                if latest_test is not None
                else None
            ),
        }

    async def publish(
        self,
        session: AsyncSession,
        *,
        code: str,
        version_number: int,
        actor_user_id: uuid.UUID,
        request_id: str,
        require_connection_test: bool = True,
    ) -> ConfigVersion:
        group = await self.group(session, code, lock=True)
        version = await self._version(session, group.id, version_number, lock=True)
        if version.status != "draft":
            raise ApiError(409, "CONFIG_VERSION_NOT_DRAFT", "仅草稿版本可发布")
        resolved = await self.resolved(session, code, version_number)
        self._require_complete(code, resolved)
        if code == "general":
            plan_exists = await session.scalar(
                select(func.count(MembershipPlan.id)).where(
                    MembershipPlan.code == resolved.values["default_membership_code"],
                    MembershipPlan.status == "active",
                )
            )
            if not plan_exists:
                raise ApiError(409, "DEFAULT_MEMBERSHIP_NOT_FOUND", "默认会员等级不存在或未启用")
        if require_connection_test and code in SENSITIVE_GROUPS and resolved.values.get("enabled"):
            succeeded = await session.scalar(
                select(func.count(ConfigTestRun.id)).where(
                    ConfigTestRun.config_version_id == version.id,
                    ConfigTestRun.status == "succeeded",
                )
            )
            if not succeeded:
                raise ApiError(409, "CONFIG_TEST_REQUIRED", "启用外部服务前必须通过连接测试")
        current_time = utcnow()
        await session.execute(
            update(ConfigVersion)
            .where(
                ConfigVersion.group_id == group.id,
                ConfigVersion.status == "active",
                ConfigVersion.id != version.id,
            )
            .values(status="superseded")
        )
        version.status = "active"
        version.published_by = actor_user_id
        version.published_at = current_time
        version.updated_at = current_time
        group.active_version = version.version
        group.updated_at = current_time
        self._audit(
            session,
            action="published",
            group=group,
            version=version,
            actor_user_id=actor_user_id,
            request_id=request_id,
        )
        return version

    async def rollback(
        self,
        session: AsyncSession,
        *,
        code: str,
        target_version: int,
        actor_user_id: uuid.UUID,
        reason: str,
        confirmed: bool,
        request_id: str,
    ) -> ConfigVersion:
        if code in SENSITIVE_GROUPS and not confirmed:
            raise ApiError(409, "SENSITIVE_CHANGE_CONFIRMATION_REQUIRED", "回滚密钥需要二次确认")
        group = await self.group(session, code, lock=True)
        target = await self._version(session, group.id, target_version)
        if target.status not in {"active", "superseded"}:
            raise ApiError(409, "CONFIG_ROLLBACK_TARGET_INVALID", "只能回滚到已发布版本")
        next_version = (
            int(
                (
                    await session.scalar(
                        select(func.max(ConfigVersion.version)).where(
                            ConfigVersion.group_id == group.id
                        )
                    )
                )
                or 0
            )
            + 1
        )
        now = utcnow()
        restored = ConfigVersion(
            id=uuid7(),
            group_id=group.id,
            version=next_version,
            status="active",
            values=dict(target.values),
            created_by=actor_user_id,
            published_by=actor_user_id,
            change_reason=reason,
            created_at=now,
            updated_at=now,
            published_at=now,
        )
        session.add(restored)
        await session.flush()
        await self._copy_secrets(session, code, target.id, restored.id)
        resolved = await self.resolved(session, code, restored.version)
        self._require_complete(code, resolved)
        await session.execute(
            update(ConfigVersion)
            .where(
                ConfigVersion.group_id == group.id,
                ConfigVersion.status == "active",
                ConfigVersion.id != restored.id,
            )
            .values(status="superseded")
        )
        group.active_version = restored.version
        group.updated_at = now
        self._audit(
            session,
            action="rolled_back",
            group=group,
            version=restored,
            actor_user_id=actor_user_id,
            request_id=request_id,
            details={"restored_from_version": target_version},
        )
        return restored

    async def clear_secret(
        self,
        session: AsyncSession,
        *,
        code: str,
        key_name: str,
        actor_user_id: uuid.UUID,
        reason: str,
        confirmed: bool,
        request_id: str,
    ) -> ConfigVersion:
        definition = self.definition(code)
        if key_name not in definition.secret_keys:
            raise ApiError(404, "CONFIG_SECRET_NOT_FOUND", "密钥字段不存在")
        if not confirmed:
            raise ApiError(409, "SENSITIVE_CHANGE_CONFIRMATION_REQUIRED", "清除密钥需要二次确认")
        group = await self.group(session, code, lock=True)
        active = await self._active_version(session, group)
        if active is None:
            raise ApiError(409, "CONFIG_NOT_PUBLISHED", "配置尚未发布")
        next_version = (
            int(
                (
                    await session.scalar(
                        select(func.max(ConfigVersion.version)).where(
                            ConfigVersion.group_id == group.id
                        )
                    )
                )
                or 0
            )
            + 1
        )
        values = dict(active.values)
        if "enabled" in values:
            values["enabled"] = False
        now = utcnow()
        cleared = ConfigVersion(
            id=uuid7(),
            group_id=group.id,
            version=next_version,
            status="active",
            values=values,
            created_by=actor_user_id,
            published_by=actor_user_id,
            change_reason=reason,
            created_at=now,
            updated_at=now,
            published_at=now,
        )
        session.add(cleared)
        await session.flush()
        await self._copy_secrets(session, code, active.id, cleared.id, exclude={key_name})
        active.status = "superseded"
        group.active_version = cleared.version
        group.updated_at = now
        self._audit(
            session,
            action="secret_cleared",
            group=group,
            version=cleared,
            actor_user_id=actor_user_id,
            request_id=request_id,
            details={"key_name": key_name},
        )
        return cleared

    async def versions(self, session: AsyncSession, group_id: uuid.UUID) -> list[ConfigVersion]:
        return list(
            (
                await session.scalars(
                    select(ConfigVersion)
                    .where(ConfigVersion.group_id == group_id)
                    .order_by(ConfigVersion.version.desc())
                )
            ).all()
        )

    async def start_test(
        self,
        session: AsyncSession,
        *,
        code: str,
        version_number: int,
        actor_user_id: uuid.UUID,
    ) -> tuple[ConfigGroup, ConfigVersion, ConfigTestRun, ResolvedConfig]:
        group = await self.group(session, code)
        version = await self._version(session, group.id, version_number)
        if version.status != "draft":
            raise ApiError(409, "CONFIG_VERSION_NOT_DRAFT", "仅草稿版本可测试")
        resolved = await self.resolved(session, code, version_number)
        self._require_complete(code, resolved)
        run = ConfigTestRun(
            id=uuid7(),
            group_id=group.id,
            config_version_id=version.id,
            requested_by=actor_user_id,
            status="running",
            result_code="RUNNING",
            result_message_redacted="连接测试执行中",
            latency_ms=None,
            created_at=utcnow(),
            completed_at=None,
        )
        session.add(run)
        await session.flush()
        return group, version, run, resolved

    def finish_test(
        self,
        session: AsyncSession,
        *,
        group: ConfigGroup,
        version: ConfigVersion,
        run: ConfigTestRun,
        outcome: TestOutcome,
        actor_user_id: uuid.UUID,
        request_id: str,
    ) -> None:
        if outcome.succeeded and as_utc(version.updated_at) > as_utc(run.created_at):
            outcome = TestOutcome(
                False,
                "CONFIG_CHANGED_DURING_TEST",
                "配置在连接测试期间发生变更，请重新测试",
                outcome.latency_ms,
            )
        run.status = "succeeded" if outcome.succeeded else "failed"
        run.result_code = outcome.code
        run.result_message_redacted = outcome.message[:1000]
        run.latency_ms = max(0, outcome.latency_ms)
        run.completed_at = utcnow()
        self._audit(
            session,
            action="connection_tested",
            group=group,
            version=version,
            actor_user_id=actor_user_id,
            request_id=request_id,
            details={"status": run.status, "result_code": run.result_code},
        )

    async def _active_version(
        self, session: AsyncSession, group: ConfigGroup
    ) -> ConfigVersion | None:
        if group.active_version is None:
            return None
        return (
            await session.scalars(
                select(ConfigVersion).where(
                    ConfigVersion.group_id == group.id,
                    ConfigVersion.version == group.active_version,
                )
            )
        ).one_or_none()

    @staticmethod
    async def _version(
        session: AsyncSession, group_id: uuid.UUID, number: int, *, lock: bool = False
    ) -> ConfigVersion:
        statement = select(ConfigVersion).where(
            ConfigVersion.group_id == group_id, ConfigVersion.version == number
        )
        if lock:
            statement = statement.with_for_update()
        version = (await session.scalars(statement)).one_or_none()
        if version is None:
            raise ApiError(404, "CONFIG_VERSION_NOT_FOUND", "配置版本不存在")
        return version

    async def _copy_secrets(
        self,
        session: AsyncSession,
        code: str,
        source_version_id: uuid.UUID,
        target_version_id: uuid.UUID,
        *,
        exclude: set[str] | None = None,
    ) -> None:
        excluded = exclude or set()
        source = list(
            (
                await session.scalars(
                    select(EncryptedSecret).where(
                        EncryptedSecret.config_version_id == source_version_id
                    )
                )
            ).all()
        )
        for item in source:
            if item.key_name in excluded:
                continue
            self._add_secret(
                session,
                code,
                target_version_id,
                item.key_name,
                self.cipher.decrypt(code, item),
            )

    async def _apply_secret_updates(
        self,
        session: AsyncSession,
        code: str,
        version_id: uuid.UUID,
        updates: dict[str, str],
    ) -> None:
        if not updates:
            return
        await session.execute(
            delete(EncryptedSecret).where(
                EncryptedSecret.config_version_id == version_id,
                EncryptedSecret.key_name.in_(updates),
            )
        )
        for key_name, value in updates.items():
            self._add_secret(session, code, version_id, key_name, value)
        await session.flush()

    def _add_secret(
        self,
        session: AsyncSession,
        code: str,
        version_id: uuid.UUID,
        key_name: str,
        value: str,
    ) -> None:
        ciphertext, nonce, fingerprint, last_four = self.cipher.encrypt(code, key_name, value)
        session.add(
            EncryptedSecret(
                id=uuid7(),
                config_version_id=version_id,
                key_name=key_name,
                ciphertext=ciphertext,
                nonce=nonce,
                key_version=self.cipher.key_version,
                value_fingerprint=fingerprint,
                last_four=last_four,
            )
        )

    @staticmethod
    def _require_complete(code: str, resolved: ResolvedConfig) -> None:
        if not bool(resolved.values.get("enabled")):
            return
        required: set[str] = set()
        if code == "sub2api":
            raw_profiles = resolved.values.get("profiles") or []
            if raw_profiles:
                enabled_profiles = [
                    item
                    for item in raw_profiles
                    if isinstance(item, dict) and item.get("enabled", True)
                ]
                if not enabled_profiles:
                    raise ApiError(
                        409,
                        "SUB2API_PROFILE_REQUIRED",
                        "启用 Sub2API 时至少要启用一条线路",
                    )
                for profile in enabled_profiles:
                    profile_id = str(profile.get("id", "primary"))
                    dynamic_key = f"api_key_{profile_id}"
                    has_legacy_primary = profile_id == "primary" and bool(
                        resolved.secrets.get("api_key")
                    )
                    if not resolved.secrets.get(dynamic_key) and not has_legacy_primary:
                        required.add(dynamic_key)
            elif not sub2api_profile_settings(resolved):
                required = {"api_key"}
        elif code == "r2":
            required = {"access_key_id", "secret_access_key"}
        elif code == "email" and resolved.values.get("provider") == "api":
            required = {"api_key"}
        elif code == "email" and resolved.values.get("username"):
            required = {"password"}
        missing = sorted(required - set(resolved.secrets))
        if missing:
            raise ApiError(
                409,
                "CONFIG_SECRET_REQUIRED",
                "启用配置前必须设置所需密钥",
                {"keys": missing},
            )

    @staticmethod
    def _audit(
        session: AsyncSession,
        *,
        action: str,
        group: ConfigGroup,
        version: ConfigVersion,
        actor_user_id: uuid.UUID,
        request_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            OutboxEvent(
                id=uuid7(),
                topic="config.audit",
                aggregate_type="config_group",
                aggregate_id=group.id,
                payload={
                    "action": action,
                    "actor_user_id": str(actor_user_id),
                    "request_id": request_id,
                    "group": group.code,
                    "version": version.version,
                    "details": details or {},
                },
                status="pending",
                attempts=0,
                available_at=utcnow(),
                version=1,
            )
        )


class ConfigConnectionTester:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def test(self, config: ResolvedConfig) -> TestOutcome:
        started = time.perf_counter()
        try:
            if not bool(config.values.get("enabled")):
                return self._outcome(started, True, "DISABLED", "配置已禁用，字段校验通过")
            if config.group == "sub2api":
                profiles = sub2api_profile_settings(config)
                if not profiles:
                    return self._outcome(started, False, "SUB2API_NOT_CONFIGURED", "没有可用的 Sub2API 线路")
                for profile in profiles:
                    await Sub2APIClient(profile).list_models()
            elif config.group == "r2":
                await self._test_r2(config)
            elif config.group == "email":
                await self._test_email(config)
                if config.values["provider"] == "api":
                    return self._outcome(
                        started,
                        True,
                        "EMAIL_API_REACHABLE",
                        "邮件接口可达；此检查未发送邮件，发信授权和投递结果请通过注册验证码确认",
                    )
            else:
                return self._outcome(started, True, "VALID", "业务配置校验通过")
        except Exception as exc:  # noqa: BLE001
            return self._outcome(
                started,
                False,
                f"{config.group.upper()}_CONNECTION_FAILED",
                f"连接测试失败（{exc.__class__.__name__}）",
            )
        return self._outcome(started, True, "CONNECTED", "连接测试通过")

    async def test_profile(self, config: ResolvedConfig, profile_id: str) -> TestOutcome:
        started = time.perf_counter()
        if config.group != "sub2api":
            return self._outcome(started, False, "PROFILE_NOT_SUPPORTED", "该配置组不支持线路测试")
        try:
            profile = next(
                (item for item in sub2api_profile_settings(config) if item.profile_id == profile_id),
                None,
            )
            if profile is None:
                return self._outcome(started, False, "SUB2API_PROFILE_NOT_CONFIGURED", "线路未启用或密钥尚未设置")
            await Sub2APIClient(profile).list_models()
        except Exception as exc:  # noqa: BLE001
            return self._outcome(
                started,
                False,
                "SUB2API_PROFILE_CONNECTION_FAILED",
                f"连接测试失败（{exc.__class__.__name__}）",
            )
        return self._outcome(started, True, "CONNECTED", "连接测试通过")

    @staticmethod
    def queue_failed_outcome() -> TestOutcome:
        return TestOutcome(False, "TEST_QUEUE_UNAVAILABLE", "连接测试队列暂时不可用", 0)

    async def _test_r2(self, config: ResolvedConfig) -> None:
        storage = build_object_storage(r2_settings(config, self.settings))
        key = f"_health/{uuid.uuid4().hex}.txt"
        payload = os.urandom(32)
        try:
            await storage.put_object(
                key,
                payload,
                content_type="application/octet-stream",
                metadata={"purpose": "config-health-check"},
            )
            if await storage.get_object(key) != payload:
                raise RuntimeError("R2 health object did not round-trip")
        finally:
            try:
                await storage.delete_object(key)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "R2 config health object cleanup failed",
                    extra={"operation": "config_test", "status": "cleanup_failed"},
                )

    async def _test_email(self, config: ResolvedConfig) -> None:
        if config.values["provider"] == "api":
            async with httpx.AsyncClient(
                timeout=self.settings.dependency_timeout_seconds, follow_redirects=False
            ) as client:
                response = await client.get(
                    config.values["api_base_url"],
                    headers={"Authorization": f"Bearer {config.secrets['api_key']}"},
                )
                # Send-only endpoints often reject GET. A 405 proves reachability,
                # not send authorization; never send unsolicited test mail here.
                if response.status_code != 405:
                    response.raise_for_status()
            return

        def connect() -> None:
            port = int(config.values["port"])
            context = ssl.create_default_context()
            arguments = {"timeout": self.settings.dependency_timeout_seconds}
            connection = (
                smtplib.SMTP_SSL(config.values["host"], port, context=context, **arguments)
                if port == 465
                else smtplib.SMTP(config.values["host"], port, **arguments)
            )
            with connection as client:
                client.ehlo()
                if port != 465 and config.values["use_tls"]:
                    client.starttls(context=context)
                    client.ehlo()
                if config.values["username"]:
                    client.login(config.values["username"], config.secrets.get("password", ""))

        await asyncio.to_thread(connect)

    @staticmethod
    def _outcome(started: float, succeeded: bool, code: str, message: str) -> TestOutcome:
        return TestOutcome(
            succeeded=succeeded,
            code=code,
            message=message,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
        )


def sub2api_settings(config: ResolvedConfig) -> SimpleNamespace:
    profiles = sub2api_profile_settings(config)
    if profiles:
        return profiles[0]
    return SimpleNamespace(
        profile_id="primary",
        sub2api_base_url=config.values.get("base_url", ""),
        sub2api_api_key=config.secrets.get("api_key", ""),
        sub2api_image_model=config.values.get("image_model", "gpt-image-2"),
        sub2api_timeout_seconds=config.values.get("timeout_seconds", 180.0),
        normalized_base_url=str(config.values.get("base_url", "")).rstrip("/"),
        sub2api_configured=False,
    )


def sub2api_profile_settings(config: ResolvedConfig) -> list[SimpleNamespace]:
    """Resolve enabled, complete profiles while retaining the legacy single-profile format."""
    raw_profiles = config.values.get("profiles") or []
    if not raw_profiles:
        raw_profiles = [
            {
                "id": "primary",
                "name": "主线路",
                "enabled": True,
                "priority": 1,
                "base_url": config.values.get("base_url", ""),
                "image_model": config.values.get("image_model", "gpt-image-2"),
                "timeout_seconds": config.values.get("timeout_seconds", 180.0),
            }
        ]
    resolved: list[SimpleNamespace] = []
    for item in raw_profiles:
        profile = item if isinstance(item, dict) else item.model_dump(mode="json")
        profile_id = str(profile.get("id", "primary"))
        api_key = config.secrets.get(f"api_key_{profile_id}", "")
        if not api_key and profile_id == "primary":
            api_key = config.secrets.get("api_key", "")
        if not (
            config.values.get("enabled")
            and profile.get("enabled", True)
            and profile.get("base_url")
            and api_key
        ):
            continue
        resolved.append(
            SimpleNamespace(
                profile_id=profile_id,
                profile_name=str(profile.get("name", profile_id)),
                priority=int(profile.get("priority", 1)),
                sub2api_base_url=str(profile["base_url"]),
                sub2api_api_key=api_key,
                sub2api_image_model=str(profile.get("image_model", "gpt-image-2")),
                sub2api_timeout_seconds=float(profile.get("timeout_seconds", 180.0)),
                normalized_base_url=str(profile["base_url"]).rstrip("/"),
                sub2api_configured=True,
            )
        )
    return sorted(resolved, key=lambda item: (item.priority, item.profile_id))


def sub2api_configured(config: ResolvedConfig) -> bool:
    return bool(sub2api_profile_settings(config))


def r2_settings(config: ResolvedConfig, settings: Settings) -> SimpleNamespace:
    return SimpleNamespace(
        r2_endpoint_url=config.values["endpoint_url"],
        r2_access_key_id=config.secrets.get("access_key_id", ""),
        r2_secret_access_key=config.secrets.get("secret_access_key", ""),
        r2_bucket=config.values["bucket"],
        r2_region=config.values["region"],
        dependency_timeout_seconds=settings.dependency_timeout_seconds,
        r2_configured=bool(
            config.values.get("enabled")
            and config.values.get("endpoint_url")
            and config.values.get("bucket")
            and config.secrets.get("access_key_id")
            and config.secrets.get("secret_access_key")
        ),
    )


@dataclass(slots=True)
class _CacheEntry:
    config: ResolvedConfig
    expires_at: float


class RuntimeConfigCache:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        service: ConfigService,
        *,
        ttl_seconds: int = 45,
        load_timeout_seconds: float = 5.0,
    ) -> None:
        self.session_factory = session_factory
        self.service = service
        self.ttl_seconds = ttl_seconds
        self.load_timeout_seconds = load_timeout_seconds
        self._cache: dict[tuple[str, int | None], _CacheEntry] = {}
        self._last_good: dict[str, ResolvedConfig] = {}
        self._load_errors: dict[str, str] = {}
        self._locks: dict[tuple[str, int | None], asyncio.Lock] = {}

    async def get(self, group: str, version: int | None = None) -> ResolvedConfig:
        key = (group, version)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and cached.expires_at > now:
            return cached.config
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)
            if cached is not None and cached.expires_at > time.monotonic():
                return cached.config
            try:
                async with asyncio.timeout(self.load_timeout_seconds):
                    async with self.session_factory() as session:
                        loaded = await self.service.resolved(session, group, version)
            except Exception as exc:
                if version is None:
                    self._load_errors[group] = exc.__class__.__name__
                if version is None and group in self._last_good:
                    return self._last_good[group]
                raise
            self._cache[key] = _CacheEntry(loaded, time.monotonic() + self.ttl_seconds)
            if version is None:
                self._last_good[group] = loaded
                self._load_errors.pop(group, None)
            return loaded

    def invalidate(self, group: str | None = None) -> None:
        for key in list(self._cache):
            if group is None or key[0] == group:
                self._cache.pop(key, None)

    def status(self) -> dict[str, Any]:
        return {
            "versions": {group: config.version for group, config in self._last_good.items()},
            "load_errors": dict(self._load_errors),
        }


class DynamicObjectStorage:
    def __init__(
        self,
        settings: Settings,
        config_cache: RuntimeConfigCache,
        *,
        fallback: ObjectStorage,
    ) -> None:
        self.settings = settings
        self.config_cache = config_cache
        self._storage = fallback
        self._version: int | None = None

    @property
    def bucket(self) -> str:
        return self._storage.bucket

    async def _current(self) -> ObjectStorage:
        try:
            config = await self.config_cache.get("r2")
        except Exception:  # noqa: BLE001
            logger.warning(
                "active R2 config unavailable; retaining previous storage client",
                extra={"operation": "config_load", "status": "degraded"},
            )
            return self._storage
        if config.version == self._version:
            return self._storage
        try:
            candidate = build_object_storage(r2_settings(config, self.settings))
        except Exception:  # noqa: BLE001
            logger.warning(
                "active R2 client could not be built; retaining previous storage client",
                extra={"operation": "config_load", "status": "degraded"},
            )
            return self._storage
        self._storage = candidate
        self._version = config.version
        return candidate

    async def refresh(self) -> None:
        await self._current()

    async def ping(self) -> None:
        await (await self._current()).ping()

    async def put_object(
        self, key: str, data: bytes, *, content_type: str, metadata: dict[str, str]
    ) -> None:
        await (await self._current()).put_object(
            key, data, content_type=content_type, metadata=metadata
        )

    async def get_object(self, key: str) -> bytes:
        return await (await self._current()).get_object(key)

    async def head_object(self, key: str):
        return await (await self._current()).head_object(key)

    async def delete_object(self, key: str) -> None:
        await (await self._current()).delete_object(key)

    async def presign_get(
        self, key: str, *, expires_seconds: int, download_filename: str | None = None
    ) -> str:
        return await (await self._current()).presign_get(
            key, expires_seconds=expires_seconds, download_filename=download_filename
        )

    async def list_objects(self, prefix: str = ""):
        return await (await self._current()).list_objects(prefix)


async def runtime_config_value(runtime: Any, group: str, key: str, fallback: Any) -> Any:
    cache = getattr(runtime, "config_cache", None)
    if cache is None:
        return fallback
    try:
        config = await cache.get(group)
    except Exception:  # noqa: BLE001
        return fallback
    return config.values.get(key, fallback)
