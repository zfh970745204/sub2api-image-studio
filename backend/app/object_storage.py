from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.config import Settings


class ObjectStorageError(RuntimeError):
    pass


class ObjectStorageNotConfigured(ObjectStorageError):
    pass


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size: int
    last_modified: datetime | None = None
    etag: str | None = None
    metadata: dict[str, str] | None = None


class ObjectStorage(Protocol):
    bucket: str

    async def ping(self) -> None: ...

    async def put_object(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        metadata: dict[str, str],
    ) -> None: ...

    async def get_object(self, key: str) -> bytes: ...

    async def head_object(self, key: str) -> StoredObject | None: ...

    async def delete_object(self, key: str) -> None: ...

    async def presign_get(
        self,
        key: str,
        *,
        expires_seconds: int,
        download_filename: str | None = None,
    ) -> str: ...

    async def list_objects(self, prefix: str = "") -> list[StoredObject]: ...


class DisabledObjectStorage:
    bucket = ""

    @staticmethod
    def _raise() -> None:
        raise ObjectStorageNotConfigured("R2 object storage is not configured")

    async def ping(self) -> None:
        self._raise()

    async def put_object(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        metadata: dict[str, str],
    ) -> None:
        self._raise()

    async def get_object(self, key: str) -> bytes:
        self._raise()

    async def head_object(self, key: str) -> StoredObject | None:
        self._raise()

    async def delete_object(self, key: str) -> None:
        self._raise()

    async def presign_get(
        self,
        key: str,
        *,
        expires_seconds: int,
        download_filename: str | None = None,
    ) -> str:
        self._raise()

    async def list_objects(self, prefix: str = "") -> list[StoredObject]:
        self._raise()


class R2ObjectStorage:
    def __init__(self, settings: Settings) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise ObjectStorageError("boto3 is required for R2 object storage") from exc

        self.bucket = settings.r2_bucket.strip()
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint_url.strip().rstrip("/"),
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name=settings.r2_region,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 4, "mode": "standard"},
                connect_timeout=settings.dependency_timeout_seconds,
                read_timeout=max(5, settings.dependency_timeout_seconds),
            ),
        )

    async def ping(self) -> None:
        await self._call(self._client.head_bucket, Bucket=self.bucket)

    async def put_object(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        metadata: dict[str, str],
    ) -> None:
        await self._call(
            self._client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            Metadata=metadata,
        )

    async def get_object(self, key: str) -> bytes:
        response = await self._call(self._client.get_object, Bucket=self.bucket, Key=key)
        return await asyncio.to_thread(response["Body"].read)

    async def head_object(self, key: str) -> StoredObject | None:
        try:
            response = await self._call(self._client.head_object, Bucket=self.bucket, Key=key)
        except Exception as exc:
            if self._error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise ObjectStorageError(f"Could not inspect object {key}") from exc
        return StoredObject(
            key=key,
            size=int(response.get("ContentLength", 0)),
            last_modified=response.get("LastModified"),
            etag=str(response.get("ETag", "")).strip('"') or None,
            metadata=dict(response.get("Metadata") or {}),
        )

    async def delete_object(self, key: str) -> None:
        await self._call(self._client.delete_object, Bucket=self.bucket, Key=key)

    async def presign_get(
        self,
        key: str,
        *,
        expires_seconds: int,
        download_filename: str | None = None,
    ) -> str:
        params = {"Bucket": self.bucket, "Key": key}
        if download_filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{download_filename}"'
        try:
            return await asyncio.to_thread(
                self._client.generate_presigned_url,
                "get_object",
                Params=params,
                ExpiresIn=expires_seconds,
            )
        except Exception as exc:
            raise ObjectStorageError(f"Could not sign object {key}") from exc

    async def list_objects(self, prefix: str = "") -> list[StoredObject]:
        def collect() -> list[StoredObject]:
            rows: list[StoredObject] = []
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                rows.extend(
                    StoredObject(
                        key=str(item["Key"]),
                        size=int(item.get("Size", 0)),
                        last_modified=item.get("LastModified"),
                        etag=str(item.get("ETag", "")).strip('"') or None,
                    )
                    for item in page.get("Contents", [])
                )
            return rows

        try:
            return await asyncio.to_thread(collect)
        except Exception as exc:
            raise ObjectStorageError("Could not list R2 objects") from exc

    @staticmethod
    def _error_code(exc: Exception) -> str | None:
        current: BaseException | None = exc
        while current is not None and not hasattr(current, "response"):
            current = current.__cause__
        response = getattr(current, "response", None)
        if not isinstance(response, dict):
            return None
        error = response.get("Error", {})
        return str(error.get("Code")) if isinstance(error, dict) else None

    @staticmethod
    async def _call(function, **kwargs):
        try:
            return await asyncio.to_thread(function, **kwargs)
        except ObjectStorageError:
            raise
        except Exception as exc:
            raise ObjectStorageError(f"R2 request failed: {exc.__class__.__name__}") from exc


def build_object_storage(settings: Settings) -> ObjectStorage:
    if not settings.r2_configured:
        return DisabledObjectStorage()
    return R2ObjectStorage(settings)
