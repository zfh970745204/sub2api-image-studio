import socket
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(PROJECT_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Sub2Image Studio"
    app_version: str = "0.13.0"
    app_environment: Literal["development", "test", "production"] = Field(
        default="development", validation_alias="APP_ENV"
    )
    log_level: str = "INFO"
    service_instance_name: str = Field(default_factory=socket.gethostname)

    database_url: str = Field(
        default="postgresql+asyncpg://sub2image:sub2image@127.0.0.1:5432/sub2image",
        repr=False,
    )
    redis_url: str = Field(default="redis://127.0.0.1:6379/0", repr=False)
    app_config_master_key: str = Field(default="", repr=False)
    config_cache_ttl_seconds: int = Field(default=45, ge=30, le=60)
    dependency_checks_enabled: bool = False
    dependency_timeout_seconds: float = Field(default=1.5, gt=0, le=10)
    legacy_sync_api_enabled: bool = True

    auth_token_pepper: str = Field(default="development-only-auth-token-pepper", repr=False)
    auth_hash_salt: str = Field(default="development-only-auth-hash-salt", repr=False)
    session_cookie_name: str = "sub2image_session"
    session_cookie_secure: bool = False
    auth_session_ttl_days: int = Field(default=30, ge=1, le=365)
    auth_session_last_seen_seconds: int = Field(default=300, ge=60, le=3600)
    login_attempt_window_minutes: int = Field(default=15, ge=1, le=1440)
    login_account_failure_limit: int = Field(default=5, ge=2, le=100)
    login_ip_failure_limit: int = Field(default=25, ge=2, le=1000)
    login_lock_minutes: int = Field(default=15, ge=1, le=1440)
    password_reset_ttl_minutes: int = Field(default=30, ge=5, le=1440)
    password_reset_enabled: bool = False
    public_app_url: str = "http://127.0.0.1:8000"
    onboarding_points: int = Field(default=20, ge=0, le=1_000_000)
    point_adjustment_approval_threshold: int = Field(default=1000, ge=1, le=1_000_000_000)
    job_quote_ttl_seconds: int = Field(default=300, ge=60, le=3600)
    admin_export_ttl_seconds: int = Field(default=300, ge=60, le=900)

    r2_endpoint_url: str = ""
    r2_access_key_id: str = Field(default="", repr=False)
    r2_secret_access_key: str = Field(default="", repr=False)
    r2_bucket: str = ""
    r2_region: str = "auto"
    asset_download_url_ttl_seconds: int = Field(default=600, ge=300, le=900)
    asset_delete_grace_days: int = Field(default=7, ge=1, le=30)
    asset_orphan_grace_hours: int = Field(default=24, ge=0, le=168)

    worker_queue_name: str = "image-jobs"
    scheduler_queue_name: str = "scheduler"
    worker_max_jobs: int = Field(default=2, ge=1, le=8)
    worker_job_timeout_seconds: int = Field(default=600, ge=30, le=3600)
    service_heartbeat_seconds: int = Field(default=30, ge=10, le=300)

    sub2api_base_url: str = ""
    sub2api_api_key: str = Field(default="", repr=False)
    sub2api_image_model: str = "gpt-image-2"
    sub2api_timeout_seconds: float = 180.0

    max_upload_mb: int = Field(default=20, ge=1, le=100)
    max_image_megapixels: int = Field(default=40, ge=1, le=200)
    result_ttl_hours: int = Field(default=24, ge=1, le=720)
    background_model: str = "u2net"
    result_dir: Path = PROJECT_ROOT / "backend" / "data" / "results"
    database_path: Path = PROJECT_ROOT / "backend" / "data" / "studio.db"
    background_model_dir: Path = PROJECT_ROOT / "backend" / "data" / "models"

    @property
    def sub2api_configured(self) -> bool:
        return bool(self.sub2api_base_url.strip() and self.sub2api_api_key.strip())

    @property
    def r2_configured(self) -> bool:
        return all(
            value.strip()
            for value in (
                self.r2_endpoint_url,
                self.r2_access_key_id,
                self.r2_secret_access_key,
                self.r2_bucket,
            )
        )

    @property
    def normalized_base_url(self) -> str:
        return self.sub2api_base_url.strip().rstrip("/")

    @property
    def production(self) -> bool:
        return self.app_environment == "production"

    @model_validator(mode="after")
    def validate_production_guards(self) -> "Settings":
        r2_values = (
            self.r2_endpoint_url,
            self.r2_access_key_id,
            self.r2_secret_access_key,
            self.r2_bucket,
        )
        if any(value.strip() for value in r2_values) and not all(
            value.strip() for value in r2_values
        ):
            raise ValueError(
                "R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, and R2_BUCKET "
                "must be configured together"
            )
        if self.production and self.legacy_sync_api_enabled:
            raise ValueError("LEGACY_SYNC_API_ENABLED must be false in production")
        if self.production and not self.dependency_checks_enabled:
            raise ValueError("DEPENDENCY_CHECKS_ENABLED must be true in production")
        if self.production and not self.session_cookie_secure:
            raise ValueError("SESSION_COOKIE_SECURE must be true in production")
        if self.production:
            from app.services.configuration import decode_master_key

            decode_master_key(self.app_config_master_key, production=True)
            for field_name, value in (
                ("AUTH_TOKEN_PEPPER", self.auth_token_pepper),
                ("AUTH_HASH_SALT", self.auth_hash_salt),
            ):
                if (
                    len(value) < 32
                    or value.startswith("development-only-")
                    or "replace-with" in value
                ):
                    raise ValueError(
                        f"{field_name} must be a unique secret of at least 32 characters"
                    )
            if not self.public_app_url.startswith("https://"):
                raise ValueError("PUBLIC_APP_URL must use HTTPS in production")
            if self.r2_configured or self.sub2api_configured:
                raise ValueError(
                    "R2 and Sub2API business credentials must be configured through the admin API"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
