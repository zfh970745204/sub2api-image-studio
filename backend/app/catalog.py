from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class StudioCatalog:
    """Persistent asset and job lineage backed by SQLite."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    source_asset_id TEXT,
                    status TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    output_asset_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL UNIQUE,
                    operation TEXT NOT NULL,
                    parent_id TEXT,
                    root_id TEXT NOT NULL,
                    job_id TEXT,
                    mime_type TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(parent_id) REFERENCES assets(id),
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                CREATE INDEX IF NOT EXISTS assets_created_at_idx
                    ON assets(created_at DESC);
                CREATE INDEX IF NOT EXISTS assets_parent_id_idx
                    ON assets(parent_id);
                CREATE INDEX IF NOT EXISTS jobs_created_at_idx
                    ON jobs(created_at DESC);
                """
            )

    def create_job(
        self,
        operation: str,
        source_asset_id: str | None,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        created_at = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, operation, source_asset_id, status, parameters_json, created_at
                ) VALUES (?, ?, ?, 'running', ?, ?)
                """,
                (
                    job_id,
                    operation,
                    source_asset_id,
                    json.dumps(parameters or {}, ensure_ascii=False, separators=(",", ":")),
                    created_at,
                ),
            )
        return self.get_job(job_id)

    def complete_job(self, job_id: str, output_asset_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'completed', output_asset_id = ?, completed_at = ?
                WHERE id = ?
                """,
                (output_asset_id, _now(), job_id),
            )
        return self.get_job(job_id)

    def fail_job(self, job_id: str, error: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', error = ?, completed_at = ?
                WHERE id = ?
                """,
                (error[:1000], _now(), job_id),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job_dict(row)

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._job_dict(row) for row in rows]

    def add_asset(
        self,
        *,
        filename: str,
        operation: str,
        mime_type: str,
        width: int,
        height: int,
        size_bytes: int,
        data: bytes,
        parent_id: str | None = None,
        job_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        asset_id = uuid.uuid4().hex
        if parent_id:
            parent = self.get_asset(parent_id)
            root_id = parent["root_id"]
        else:
            root_id = asset_id
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO assets (
                    id, filename, operation, parent_id, root_id, job_id, mime_type,
                    width, height, size_bytes, sha256, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    filename,
                    operation,
                    parent_id,
                    root_id,
                    job_id,
                    mime_type,
                    width,
                    height,
                    size_bytes,
                    hashlib.sha256(data).hexdigest(),
                    json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":")),
                    _now(),
                ),
            )
        return self.get_asset(asset_id)

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None:
            raise KeyError(asset_id)
        return self._asset_dict(row)

    def list_assets(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM assets ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._asset_dict(row) for row in rows]

    def lineage(self, asset_id: str) -> list[dict[str, Any]]:
        lineage: list[dict[str, Any]] = []
        seen: set[str] = set()
        current_id: str | None = asset_id
        while current_id:
            if current_id in seen:
                break
            seen.add(current_id)
            asset = self.get_asset(current_id)
            lineage.append(asset)
            current_id = asset["parent_id"]
        lineage.reverse()
        return lineage

    @staticmethod
    def _asset_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        return item

    @staticmethod
    def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["parameters"] = json.loads(item.pop("parameters_json"))
        return item
