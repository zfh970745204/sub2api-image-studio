from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.catalog import StudioCatalog
from app.image_ops import inspect_image
from app.services.asset_files import PreparedAsset, prepare_asset
from app.services.assets import AssetService


@dataclass(slots=True)
class LocalMigrationReport:
    discovered: int = 0
    migrated: int = 0
    verified: int = 0
    skipped: int = 0
    failures: list[str] = field(default_factory=list)
    id_map: dict[str, uuid.UUID] = field(default_factory=dict)


async def migrate_local_results(
    database,
    storage,
    *,
    owner_id: uuid.UUID,
    catalog_path: Path,
    result_dir: Path,
    retention_days: int,
    dry_run: bool = False,
) -> LocalMigrationReport:
    if not catalog_path.is_file():
        raise FileNotFoundError(f"Legacy catalog not found: {catalog_path}")
    if not result_dir.is_dir():
        raise FileNotFoundError(f"Legacy result directory not found: {result_dir}")
    rows = StudioCatalog(catalog_path).list_assets(1_000_000)
    report = LocalMigrationReport(discovered=len(rows))
    pending = {str(row["id"]): row for row in rows}
    asset_service = AssetService()

    while pending:
        progressed = False
        for legacy_id, row in list(pending.items()):
            parent_id = str(row["parent_id"]) if row.get("parent_id") else None
            if parent_id is not None and parent_id in pending:
                continue
            progressed = True
            pending.pop(legacy_id)
            path = (result_dir / str(row["filename"])).resolve()
            try:
                path.relative_to(result_dir.resolve())
                raw = path.read_bytes()
                local_sha = hashlib.sha256(raw).hexdigest()
                if local_sha != str(row["sha256"]):
                    raise ValueError("local SHA-256 differs from the legacy catalog")
                prepared = _legacy_prepared(raw, path.suffix)
                if dry_run:
                    report.skipped += 1
                    report.id_map[legacy_id] = uuid.uuid4()
                    continue
                if parent_id is not None and parent_id not in report.id_map:
                    raise ValueError("parent asset was not migrated successfully")
                parent_asset_id = report.id_map.get(parent_id) if parent_id else None
                asset = await asset_service.store(
                    database,
                    storage,
                    owner_id=owner_id,
                    prepared=prepared,
                    kind=_legacy_kind(str(row["operation"]), prepared.extension),
                    operation_code=f"legacy.{row['operation']!s}"[:64],
                    retention_days=retention_days,
                    original_filename=str(row["filename"]),
                    parent_asset_id=parent_asset_id,
                    metadata={
                        "legacy_asset_id": legacy_id,
                        "legacy_job_id": row.get("job_id"),
                        "legacy_created_at": row.get("created_at"),
                        "migration_source": "backend/data/results",
                    },
                )
                report.migrated += 1
                uploaded = await storage.get_object(asset.object_key)
                if hashlib.sha256(uploaded).hexdigest() != local_sha:
                    await _discard_unverified(database, storage, asset.id)
                    raise ValueError("remote SHA-256 verification failed")
                report.verified += 1
                report.id_map[legacy_id] = asset.id
            except Exception as exc:  # noqa: BLE001
                report.failures.append(f"{legacy_id}: {exc}")
        if not progressed:
            for legacy_id in pending:
                report.failures.append(f"{legacy_id}: unresolved lineage cycle")
            break
    return report


def _legacy_prepared(raw: bytes, suffix: str) -> PreparedAsset:
    extension = suffix.lower().lstrip(".")
    sha256 = hashlib.sha256(raw).hexdigest()
    if extension == "svg":
        inspected = prepare_asset(raw, kind="vector", max_megapixels=200)
        return PreparedAsset(
            data=raw,
            mime_type="image/svg+xml",
            extension="svg",
            width=inspected.width,
            height=inspected.height,
            has_alpha=None,
            sha256=sha256,
        )
    width, height, mode = inspect_image(raw)
    mime_type = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
    }.get(extension)
    if mime_type is None:
        raise ValueError(f"unsupported legacy extension: {extension}")
    return PreparedAsset(
        data=raw,
        mime_type=mime_type,
        extension="jpg" if extension == "jpeg" else extension,
        width=width,
        height=height,
        has_alpha=mode == "RGBA",
        sha256=sha256,
    )


def _legacy_kind(operation: str, extension: str) -> str:
    if extension == "svg":
        return "vector"
    return "original" if operation == "upload" else "result"


async def _discard_unverified(database, storage, asset_id: uuid.UUID) -> None:
    service = AssetService()
    async with database.session_factory() as session:
        await service.soft_delete(
            session,
            asset_id=asset_id,
            owner_id=None,
            grace_days=1,
            immediate=True,
        )
        await session.commit()
    await service.process_deletion_queue(database, storage, limit=1)
