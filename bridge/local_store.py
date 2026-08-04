"""Versioned local SQLite storage for shop data and legacy JSON snapshots.

This module does not replace the current JSON read/write path yet.  It provides
the isolated storage foundation and a lossless, idempotent import path so the
HTTP receiver can be migrated separately.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence


SCHEMA_VERSION = 1
Migration = Callable[[sqlite3.Connection], None]


class LocalStoreError(RuntimeError):
    """Base error raised by the local store."""


class SchemaMigrationError(LocalStoreError):
    """A schema migration failed and its transaction was rolled back."""

    def __init__(self, message: str, *, backup_path: Path | None = None) -> None:
        super().__init__(message)
        self.backup_path = backup_path


class SnapshotImportError(LocalStoreError):
    """One or more JSON snapshots could not be imported."""


@dataclass(frozen=True)
class StorePaths:
    """Filesystem layout whose independently managed areas never overlap."""

    root: Path
    data: Path
    database: Path
    knowledge: Path
    config: Path
    backup: Path
    logs: Path
    app: Path

    @classmethod
    def under(cls, root: str | os.PathLike[str]) -> "StorePaths":
        base = Path(root).expanduser().resolve()
        data = base / "data"
        return cls(
            root=base,
            data=data,
            database=data / "shop.db",
            knowledge=base / "knowledge",
            config=base / "config",
            backup=base / "backup",
            logs=base / "logs",
            app=base / "app",
        )

    def create_directories(self) -> None:
        for path in (
            self.data,
            self.knowledge,
            self.config,
            self.backup,
            self.logs,
            self.app,
        ):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class ImportedSnapshot:
    id: int
    source_path: str
    snapshot_type: str
    captured_at: str | None
    content_sha256: str
    duplicate: bool


def default_store_root() -> Path:
    """Return the per-user store location without depending on ``HOME``."""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "DianAgent"
    return Path.cwd() / ".dianagent"


def _migration_1(connection: sqlite3.Connection) -> None:
    # Keep each statement inside the caller's explicit transaction.  Python's
    # sqlite3.executescript() may commit an existing transaction implicitly.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            snapshot_type TEXT NOT NULL,
            captured_at TEXT,
            imported_at TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE (source_path, content_sha256)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_snapshots_type_captured
        ON snapshots (snapshot_type, captured_at DESC, id DESC)
        """
    )


DEFAULT_MIGRATIONS: Mapping[int, Migration] = {1: _migration_1}


class LocalStore:
    """Own the private shop database and its migration/import lifecycle."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.paths = StorePaths.under(root or default_store_root())

    def initialize(self) -> Path:
        """Create the directory layout and migrate to the current schema."""

        self.paths.create_directories()
        self.migrate()
        return self.paths.database

    def connect(self) -> sqlite3.Connection:
        """Open a configured connection. Initialize before ordinary data access."""

        connection = sqlite3.connect(self.paths.database, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def get_schema_version(self) -> int:
        if not self.paths.database.exists():
            return 0
        connection = self.connect()
        try:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()

    def status(self) -> dict[str, Any]:
        """Return a small, UI-safe storage health summary."""

        backup_count = (
            sum(1 for path in self.paths.backup.glob("shop-*.db") if path.is_file())
            if self.paths.backup.exists()
            else 0
        )
        if not self.paths.database.exists():
            return {
                "schema": 0,
                "status": "missing",
                "db_path": str(self.paths.database),
                "backup_count": backup_count,
            }
        try:
            schema = self.get_schema_version()
            if schema == SCHEMA_VERSION:
                state = "ready"
            elif schema < SCHEMA_VERSION:
                state = "upgrade_required"
            else:
                state = "unsupported_newer"
        except sqlite3.Error:
            schema = None
            state = "error"
        return {
            "schema": schema,
            "status": state,
            "db_path": str(self.paths.database),
            "backup_count": backup_count,
        }

    def create_backup(self, *, label: str = "manual") -> Path:
        """Create a transactionally consistent SQLite backup."""

        if not self.paths.database.exists():
            raise LocalStoreError("Cannot back up a database that does not exist")
        self.paths.backup.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = self.paths.backup / f"shop-{label}-{timestamp}.db"
        source_connection = self.connect()
        target_connection = sqlite3.connect(target)
        try:
            source_connection.backup(target_connection)
        except Exception:
            target_connection.close()
            target.unlink(missing_ok=True)
            raise
        else:
            target_connection.close()
            return target
        finally:
            source_connection.close()

    def restore_backup(self, backup_path: str | os.PathLike[str]) -> Path | None:
        """Validate and atomically restore a backup.

        The current database is backed up first, when present.  Validation and
        staging happen before ``os.replace`` so a corrupt or unsupported backup
        can never partially overwrite the live database.
        """

        source_path = Path(backup_path).resolve()
        if not source_path.is_file():
            raise LocalStoreError(f"Backup does not exist: {source_path}")
        if source_path == self.paths.database.resolve():
            raise LocalStoreError("The live database cannot be restored as its own backup")

        self._validate_database(source_path)

        self.paths.create_directories()
        safety_backup = (
            self.create_backup(label="pre-restore") if self.paths.database.exists() else None
        )
        handle, staging_name = tempfile.mkstemp(
            prefix=".shop-restore-", suffix=".db", dir=self.paths.data
        )
        os.close(handle)
        staging_path = Path(staging_name)
        staging_path.unlink(missing_ok=True)
        source_connection = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
        staging_connection = sqlite3.connect(staging_path)
        try:
            source_connection.backup(staging_connection)
            staging_connection.close()
            staging_connection = None
            source_connection.close()
            source_connection = None
            # Validate the staged copy too, closing a check/copy time-of-check
            # gap if another process modified the source backup meanwhile.
            self._validate_database(staging_path)
            os.replace(staging_path, self.paths.database)
        except Exception:
            staging_path.unlink(missing_ok=True)
            raise
        finally:
            if staging_connection is not None:
                staging_connection.close()
            if source_connection is not None:
                source_connection.close()
        return safety_backup

    @staticmethod
    def _validate_database(path: Path) -> int:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            check = connection.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                raise LocalStoreError("Backup failed SQLite integrity validation")
            schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if schema > SCHEMA_VERSION:
                raise LocalStoreError(
                    f"Backup schema {schema} is newer than supported {SCHEMA_VERSION}"
                )
            return schema
        except sqlite3.Error as exc:
            raise LocalStoreError("Backup is not a valid SQLite database") from exc
        finally:
            if connection is not None:
                connection.close()

    def migrate(
        self,
        *,
        target_version: int = SCHEMA_VERSION,
        migrations: Mapping[int, Migration] | None = None,
    ) -> Path | None:
        """Migrate atomically and back up every pre-existing database first.

        The migration mapping is keyed by the version being entered.  Exposing
        it lets later releases compose migrations and makes rollback testable.
        """

        if target_version < 0:
            raise ValueError("target_version cannot be negative")
        migration_set = dict(DEFAULT_MIGRATIONS if migrations is None else migrations)
        self.paths.create_directories()

        existed = self.paths.database.exists()
        current_version = self.get_schema_version() if existed else 0
        if current_version > target_version:
            raise SchemaMigrationError(
                f"Database schema {current_version} is newer than supported {target_version}"
            )
        if current_version == target_version:
            return None

        missing = [
            version
            for version in range(current_version + 1, target_version + 1)
            if version not in migration_set
        ]
        if missing:
            raise SchemaMigrationError(f"Missing schema migration(s): {missing}")

        backup_path = self.create_backup(label=f"pre-v{target_version}") if existed else None
        connection: sqlite3.Connection | None = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for version in range(current_version + 1, target_version + 1):
                migration_set[version](connection)
                now = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    """
                    INSERT INTO schema_meta (singleton, schema_version, updated_at)
                    VALUES (1, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        schema_version = excluded.schema_version,
                        updated_at = excluded.updated_at
                    """,
                    (version, now),
                )
                connection.execute(f"PRAGMA user_version = {version:d}")
            connection.commit()
        except Exception as exc:
            connection.rollback()
            if not existed:
                connection.close()
                connection = None
                self.paths.database.unlink(missing_ok=True)
            raise SchemaMigrationError(
                f"Migration from schema {current_version} to {target_version} failed",
                backup_path=backup_path,
            ) from exc
        finally:
            if connection is not None:
                connection.close()
        return backup_path

    def import_json_snapshot(
        self,
        source: str | os.PathLike[str],
        *,
        snapshot_type: str | None = None,
    ) -> ImportedSnapshot:
        """Import one legacy JSON file while preserving its complete value."""

        return self.import_json_snapshots([source], snapshot_type=snapshot_type)[0]

    def persist_snapshot(
        self,
        payload: Any,
        source_path: str | os.PathLike[str],
        *,
        snapshot_type: str | None = None,
    ) -> ImportedSnapshot:
        """Persist an in-memory snapshot without writing and rereading JSON."""

        self.initialize()
        path = Path(source_path)
        try:
            canonical_json = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise SnapshotImportError("Snapshot payload is not JSON serializable") from exc
        digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        item_type = snapshot_type or self._infer_snapshot_type(path, payload)
        captured_at = self._infer_captured_at(path, payload)
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            imported = self._insert_snapshot(
                connection,
                source_path=str(source_path),
                snapshot_type=item_type,
                captured_at=captured_at,
                content_sha256=digest,
                payload_json=canonical_json,
            )
            connection.commit()
            return imported
        except Exception as exc:
            connection.rollback()
            raise SnapshotImportError("Snapshot persistence was rolled back") from exc
        finally:
            connection.close()

    def import_json_snapshots(
        self,
        sources: Iterable[str | os.PathLike[str]],
        *,
        snapshot_type: str | None = None,
    ) -> list[ImportedSnapshot]:
        """Import files or directories as one all-or-nothing transaction."""

        self.initialize()
        files = self._expand_json_sources(sources)
        prepared: list[tuple[Path, str, str | None, str, str]] = []
        try:
            for path in files:
                with path.open("r", encoding="utf-8-sig") as source_file:
                    payload = json.load(source_file)
                canonical_json = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
                item_type = snapshot_type or self._infer_snapshot_type(path, payload)
                captured_at = self._infer_captured_at(path, payload)
                prepared.append((path, item_type, captured_at, digest, canonical_json))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SnapshotImportError(f"Cannot read JSON snapshot: {exc}") from exc

        imported: list[ImportedSnapshot] = []
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for path, item_type, captured_at, digest, canonical_json in prepared:
                imported.append(
                    self._insert_snapshot(
                        connection,
                        source_path=str(path.resolve()),
                        snapshot_type=item_type,
                        captured_at=captured_at,
                        content_sha256=digest,
                        payload_json=canonical_json,
                    )
                )
            connection.commit()
        except Exception as exc:
            connection.rollback()
            raise SnapshotImportError("JSON snapshot batch was rolled back") from exc
        finally:
            connection.close()
        return imported

    def iter_snapshots(self, *, snapshot_type: str | None = None) -> Iterator[dict[str, Any]]:
        """Yield imported snapshots newest first with decoded payloads."""

        self.initialize()
        sql = "SELECT * FROM snapshots"
        parameters: Sequence[Any] = ()
        if snapshot_type is not None:
            sql += " WHERE snapshot_type = ?"
            parameters = (snapshot_type,)
        sql += " ORDER BY COALESCE(captured_at, imported_at) DESC, id DESC"
        connection = self.connect()
        try:
            rows = connection.execute(sql, parameters).fetchall()
        finally:
            connection.close()
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            yield item

    @staticmethod
    def _expand_json_sources(
        sources: Iterable[str | os.PathLike[str]],
    ) -> list[Path]:
        files: list[Path] = []
        for source in sources:
            path = Path(source)
            if path.is_dir():
                files.extend(sorted(item for item in path.rglob("*.json") if item.is_file()))
            elif path.is_file():
                files.append(path)
            else:
                raise SnapshotImportError(f"Snapshot source does not exist: {path}")
        return files

    @staticmethod
    def _infer_snapshot_type(path: Path, payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("page_type", "snapshot_type", "type", "kind", "source"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            data = payload.get("data")
            if isinstance(data, dict):
                value = data.get("page_type")
                if isinstance(value, str) and value.strip():
                    return value.strip()
        stem = path.stem.lower()
        for suffix in ("_snapshot", "-snapshot", "_data", "-data"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        return stem or "legacy"

    @staticmethod
    def _infer_captured_at(path: Path, payload: Any) -> str | None:
        if isinstance(payload, dict):
            for container in (payload, payload.get("data"), payload.get("metadata")):
                if not isinstance(container, dict):
                    continue
                for key in (
                    "captured_at",
                    "timestamp",
                    "saved_at",
                    "fetched_at",
                    "collected_at",
                    "created_at",
                ):
                    value = container.get(key)
                    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                        return str(value)
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        except OSError:
            return None

    @staticmethod
    def _insert_snapshot(
        connection: sqlite3.Connection,
        *,
        source_path: str,
        snapshot_type: str,
        captured_at: str | None,
        content_sha256: str,
        payload_json: str,
    ) -> ImportedSnapshot:
        imported_at = datetime.now(timezone.utc).isoformat()
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO snapshots (
                source_path, snapshot_type, captured_at, imported_at,
                content_sha256, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source_path,
                snapshot_type,
                captured_at,
                imported_at,
                content_sha256,
                payload_json,
            ),
        )
        duplicate = cursor.rowcount == 0
        if duplicate:
            row = connection.execute(
                """
                SELECT id, snapshot_type, captured_at FROM snapshots
                WHERE source_path = ? AND content_sha256 = ?
                """,
                (source_path, content_sha256),
            ).fetchone()
            if row is None:
                raise SnapshotImportError("Duplicate snapshot lookup failed")
            snapshot_id = int(row["id"])
            stored_type = str(row["snapshot_type"])
            stored_captured_at = row["captured_at"]
        else:
            snapshot_id = int(cursor.lastrowid)
            stored_type = snapshot_type
            stored_captured_at = captured_at
        return ImportedSnapshot(
            id=snapshot_id,
            source_path=source_path,
            snapshot_type=stored_type,
            captured_at=stored_captured_at,
            content_sha256=content_sha256,
            duplicate=duplicate,
        )


__all__ = [
    "DEFAULT_MIGRATIONS",
    "ImportedSnapshot",
    "LocalStore",
    "LocalStoreError",
    "SCHEMA_VERSION",
    "SchemaMigrationError",
    "SnapshotImportError",
    "StorePaths",
    "default_store_root",
]
