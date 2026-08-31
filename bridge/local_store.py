"""Versioned local SQLite storage for shop data and legacy JSON snapshots.

SQLite is the Agent's authoritative store.  JSON files remain a compatibility
mirror for older clients and may be imported once during an upgrade, but they
must never silently take read precedence over committed SQLite state.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from platform_paths import default_install_root


SCHEMA_VERSION = 4
Migration = Callable[[sqlite3.Connection], None]

_LEGACY_JSON_MIGRATION_KEY = "legacy_json_bootstrap_v1"

_COMMERCE_ENTITY_KEY = re.compile(
    r"^(?:product_v1|sku_v1|merchant_v1|plan_v1|material_v1|content_v1|live_v1|session_v1)_[a-f0-9]{26}$"
)
_COMMERCE_SCOPE_KEY = re.compile(r"^[a-z0-9_-]{4,48}$")
_COMMERCE_ENTITY_TYPES = {
    "douyin_product_id",
    "qianchuan_product_id",
    "douyin_sku_id",
    "merchant_product_code",
    "qianchuan_plan_id",
    "qianchuan_material_id",
    "douyin_content_id",
    "douyin_live_room_id",
    "douyin_live_session_id",
}
_COMMERCE_TASK_OPEN_STATUSES = {
    "running",
    "observing",
    "awaiting_readback",
    "blocked",
}
_COMMERCE_TASK_TERMINAL_STATUSES = {"completed", "cancelled"}
_COMMERCE_TASK_TRANSITIONS = {
    "running": {"observing", "awaiting_readback", "blocked", "completed", "cancelled"},
    "observing": {"awaiting_readback", "blocked", "completed", "cancelled"},
    "awaiting_readback": {"blocked", "completed", "cancelled"},
    "blocked": {"running", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


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


@dataclass(frozen=True)
class SnapshotLifecyclePolicy:
    """Read-only planning policy for bounded hot snapshot history.

    The policy deliberately describes *archive candidates*, not rows that may
    be deleted.  Applying a plan is intentionally outside this release: an
    apply path must first create and validate a backup and a queryable archive,
    then revalidate the preview fingerprint under a write transaction.
    """

    policy_version: int = 1
    full_fidelity_days: int = 30
    keep_latest_per_source: int = 720
    review_after_total_rows: int = 5_000
    review_after_source_rows: int = 2_000
    review_after_database_bytes: int = 256 * 1024 * 1024
    hotspot_limit: int = 10
    semantic_sample_rows_per_source: int = 5_000
    semantic_sample_total_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        limits = {
            "policy_version": (self.policy_version, 1, 1),
            "full_fidelity_days": (self.full_fidelity_days, 1, 3650),
            "keep_latest_per_source": (self.keep_latest_per_source, 1, 100_000),
            "review_after_total_rows": (self.review_after_total_rows, 1, 100_000_000),
            "review_after_source_rows": (self.review_after_source_rows, 1, 10_000_000),
            "review_after_database_bytes": (
                self.review_after_database_bytes,
                1024 * 1024,
                10 * 1024 * 1024 * 1024,
            ),
            "hotspot_limit": (self.hotspot_limit, 1, 50),
            "semantic_sample_rows_per_source": (
                self.semantic_sample_rows_per_source,
                10,
                100_000,
            ),
            "semantic_sample_total_bytes": (
                self.semantic_sample_total_bytes,
                1024 * 1024,
                1024 * 1024 * 1024,
            ),
        }
        for name, (value, minimum, maximum) in limits.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            if value < minimum or value > maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")

    def as_dict(self) -> dict[str, int]:
        return {
            "policy_version": self.policy_version,
            "full_fidelity_days": self.full_fidelity_days,
            "keep_latest_per_source": self.keep_latest_per_source,
            "review_after_total_rows": self.review_after_total_rows,
            "review_after_source_rows": self.review_after_source_rows,
            "review_after_database_bytes": self.review_after_database_bytes,
            "hotspot_limit": self.hotspot_limit,
            "semantic_sample_rows_per_source": self.semantic_sample_rows_per_source,
            "semantic_sample_total_bytes": self.semantic_sample_total_bytes,
        }


# These fields record when a page was observed, not a change in the business
# state of that page.  They are removed only for lifecycle diagnostics.  The
# authoritative payload and its SHA-256 are never rewritten.
_SNAPSHOT_SEMANTIC_VOLATILE_PATHS = frozenset(
    {
        ("timestamp",),
        ("saved_at",),
        ("data", "captured_at"),
        ("data", "promotion_context", "evidence", "captured_at_ms"),
        (
            "data",
            "promotion_context",
            "promotion_mode_evidence",
            "captured_at_ms",
        ),
    }
)


def default_store_root() -> Path:
    """Return the platform-native per-user store location."""

    return default_install_root()


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


def _migration_2(connection: sqlite3.Connection) -> None:
    """Add the anonymous, store-scoped Douyin commerce memory graph."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS commerce_meta (
            meta_key TEXT PRIMARY KEY,
            meta_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS commerce_entities (
            store_key TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            attrs_json TEXT NOT NULL DEFAULT '{}',
            first_seen_at_ms INTEGER NOT NULL,
            last_seen_at_ms INTEGER NOT NULL,
            last_source TEXT NOT NULL DEFAULT '',
            last_page_type TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (store_key, entity_key)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_commerce_entities_type_seen
        ON commerce_entities (store_key, entity_type, last_seen_at_ms DESC)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS commerce_relations (
            store_key TEXT NOT NULL,
            relation_key TEXT NOT NULL,
            from_key TEXT NOT NULL,
            to_key TEXT NOT NULL,
            relation TEXT NOT NULL,
            confidence TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            first_seen_at_ms INTEGER NOT NULL,
            last_seen_at_ms INTEGER NOT NULL,
            PRIMARY KEY (store_key, relation_key),
            UNIQUE (store_key, from_key, relation, to_key),
            FOREIGN KEY (store_key, from_key)
                REFERENCES commerce_entities (store_key, entity_key) ON DELETE CASCADE,
            FOREIGN KEY (store_key, to_key)
                REFERENCES commerce_entities (store_key, entity_key) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_commerce_relations_from
        ON commerce_relations (store_key, from_key, relation, last_seen_at_ms DESC)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_commerce_relations_to
        ON commerce_relations (store_key, to_key, relation, last_seen_at_ms DESC)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS metric_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            store_key TEXT NOT NULL,
            account_key TEXT NOT NULL DEFAULT '',
            entity_key TEXT NOT NULL,
            metric TEXT NOT NULL,
            metric_contract TEXT NOT NULL,
            value REAL NOT NULL,
            unit TEXT NOT NULL DEFAULT 'number',
            channel TEXT NOT NULL DEFAULT 'other',
            window_key TEXT NOT NULL DEFAULT 'unknown',
            context_key TEXT NOT NULL,
            captured_at_ms INTEGER NOT NULL,
            source TEXT NOT NULL,
            page_type TEXT NOT NULL,
            quality_score INTEGER NOT NULL DEFAULT 0,
            attribution_scope TEXT NOT NULL DEFAULT 'exact_row',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            fingerprint TEXT NOT NULL,
            UNIQUE (store_key, fingerprint),
            FOREIGN KEY (snapshot_id) REFERENCES snapshots (id) ON DELETE CASCADE,
            FOREIGN KEY (store_key, entity_key)
                REFERENCES commerce_entities (store_key, entity_key) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_metric_observations_entity_metric_time
        ON metric_observations (
            store_key, account_key, entity_key, metric, context_key,
            captured_at_ms DESC, id DESC
        )
        """
    )


def _migration_3(connection: sqlite3.Connection) -> None:
    """Add durable, account-scoped task runs and their evidence links."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS commerce_task_runs (
            run_id TEXT PRIMARY KEY,
            task_key TEXT NOT NULL,
            store_key TEXT NOT NULL,
            account_key TEXT NOT NULL DEFAULT '',
            entity_key TEXT NOT NULL,
            subject_kind TEXT NOT NULL,
            rule_id TEXT NOT NULL,
            contract_fingerprint TEXT NOT NULL,
            contract_json TEXT NOT NULL DEFAULT '{}',
            business_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'running', 'observing', 'awaiting_readback', 'blocked',
                    'completed', 'cancelled'
                )
            ),
            started_at_ms INTEGER NOT NULL,
            due_at_ms INTEGER NOT NULL,
            completed_at_ms INTEGER,
            verdict TEXT NOT NULL DEFAULT 'pending',
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (store_key, entity_key)
                REFERENCES commerce_entities (store_key, entity_key) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_commerce_task_one_open_run
        ON commerce_task_runs (store_key, task_key)
        WHERE status IN ('running', 'observing', 'awaiting_readback', 'blocked')
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_commerce_task_runs_subject_time
        ON commerce_task_runs (
            store_key, account_key, entity_key, started_at_ms DESC, run_id
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS commerce_task_run_observations (
            run_id TEXT NOT NULL,
            phase TEXT NOT NULL CHECK (phase IN ('baseline', 'readback')),
            observation_id INTEGER NOT NULL,
            linked_at TEXT NOT NULL,
            PRIMARY KEY (run_id, phase, observation_id),
            FOREIGN KEY (run_id)
                REFERENCES commerce_task_runs (run_id) ON DELETE CASCADE,
            FOREIGN KEY (observation_id)
                REFERENCES metric_observations (id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_commerce_task_observations_observation
        ON commerce_task_run_observations (observation_id, run_id)
        """
    )


def _migration_4(connection: sqlite3.Connection) -> None:
    """Record one-time compatibility migrations inside the authoritative DB."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS local_store_meta (
            meta_key TEXT PRIMARY KEY,
            meta_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_snapshots_source_latest
        ON snapshots (source_path, id DESC)
        """
    )


DEFAULT_MIGRATIONS: Mapping[int, Migration] = {
    1: _migration_1,
    2: _migration_2,
    3: _migration_3,
    4: _migration_4,
}


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

    def connect_read_only(self) -> sqlite3.Connection:
        """Open the existing database without creating or mutating it."""

        if not self.paths.database.is_file():
            raise LocalStoreError("The local database does not exist")
        try:
            connection = sqlite3.connect(
                self.paths.database.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=10.0,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            return connection
        except sqlite3.Error as exc:
            raise LocalStoreError("Cannot open the local database read-only") from exc

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
                "authoritative_source": "sqlite",
                "json_compatibility": "migration_pending",
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
        json_compatibility = "migration_pending"
        if schema == SCHEMA_VERSION:
            try:
                if self._legacy_json_migration_record() is not None:
                    json_compatibility = "non_authoritative_mirror"
            except LocalStoreError:
                state = "error"
                json_compatibility = "migration_state_error"
        return {
            "schema": schema,
            "status": state,
            "db_path": str(self.paths.database),
            "backup_count": backup_count,
            "authoritative_source": "sqlite",
            "json_compatibility": json_compatibility,
        }

    @staticmethod
    def _semantic_snapshot_value(value: Any, path: tuple[str, ...] = ()) -> Any:
        """Project a payload to business state without changing stored data."""

        if isinstance(value, Mapping):
            projected: dict[str, Any] = {}
            for raw_key, child in value.items():
                key = str(raw_key)
                child_path = (*path, key)
                if child_path in _SNAPSHOT_SEMANTIC_VOLATILE_PATHS:
                    continue
                projected[key] = LocalStore._semantic_snapshot_value(child, child_path)
            return projected
        if isinstance(value, list):
            return [LocalStore._semantic_snapshot_value(child, path) for child in value]
        return value

    @classmethod
    def _semantic_snapshot_digest(cls, payload_json: str) -> str:
        try:
            payload = json.loads(payload_json)
            projected = cls._semantic_snapshot_value(payload)
            canonical = json.dumps(
                projected,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise LocalStoreError("Cannot calculate semantic snapshot identity") from exc
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _snapshot_source_name(source_path: str) -> str:
        """Return a non-identifying display hint instead of a local/account path."""

        parts = [part for part in re.split(r"[\\/]", source_path) if part]
        value = parts[-1] if parts else "snapshot"
        if "://" in value:
            value = value.rsplit("://", 1)[-1]
        return value[:80] or "snapshot"

    def snapshot_lifecycle_preview(
        self,
        *,
        policy: SnapshotLifecyclePolicy | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Build a bounded, read-only snapshot growth and archive preview.

        This method never creates a database, backup, archive or journal and
        never runs DELETE/VACUUM.  Candidate rows remain estimates until a
        future explicit apply flow recomputes the exact set under ``BEGIN
        IMMEDIATE`` after producing a verified backup and queryable archive.
        """

        effective = policy or SnapshotLifecyclePolicy()
        generated_at = now or datetime.now(timezone.utc)
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=timezone.utc)
        else:
            generated_at = generated_at.astimezone(timezone.utc)
        # A day-stable boundary keeps preview_id stable while a user reviews
        # the plan, instead of invalidating it every microsecond.
        cutoff = (generated_at - timedelta(days=effective.full_fidelity_days)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        database_exists = self.paths.database.is_file()
        database_bytes = self.paths.database.stat().st_size if database_exists else 0
        wal_path = Path(f"{self.paths.database}-wal")
        wal_bytes = wal_path.stat().st_size if wal_path.is_file() else 0
        base: dict[str, Any] = {
            "contract_version": 1,
            "generated_at": generated_at.isoformat(),
            "status": "missing" if not database_exists else "unavailable",
            "mode": "preview_only",
            "mutates_data": False,
            "automatic_cleanup_enabled": False,
            "database_bytes": database_bytes,
            "wal_bytes": wal_bytes,
            "policy": effective.as_dict(),
            "archive_cutoff": cutoff.isoformat(),
            "snapshot_rows": 0,
            "logical_sources": 0,
            "payload_bytes": 0,
            "archive_candidate_rows": 0,
            "archive_candidate_payload_bytes": 0,
            "metric_observation_protected_rows": 0,
            "task_evidence_protected_rows": 0,
            "hotspots": [],
            "review_reason_codes": [],
            "preview_id": "",
            "apply_contract": {
                "implemented": False,
                "action": "archive_then_compact",
                "requires_explicit_confirmation": True,
                "requires_same_preview_id": True,
                "requires_verified_backup": True,
                "requires_queryable_archive": True,
                "requires_single_write_transaction": True,
                "requires_post_write_integrity_check": True,
                "automatic_vacuum": False,
            },
            "semantic_compaction": {
                "in_place_supported": False,
                "reason": "capture freshness and immutable task evidence must remain independently auditable",
                "future_schema": {
                    "immutable_business_states": True,
                    "append_only_capture_observations": True,
                    "task_evidence_foreign_keys_preserved": True,
                    "latest_freshness_from_capture_observation": True,
                },
                "volatile_paths": [".".join(path) for path in sorted(_SNAPSHOT_SEMANTIC_VOLATILE_PATHS)],
            },
        }
        if not database_exists:
            return base

        connection = self.connect_read_only()
        try:
            schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
            base["schema"] = schema
            if schema != SCHEMA_VERSION:
                base["status"] = "schema_upgrade_required" if schema < SCHEMA_VERSION else "unsupported_newer"
                return base

            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
            base["allocated_bytes"] = page_count * page_size
            base["currently_reusable_bytes"] = freelist_count * page_size

            grouped_rows = connection.execute(
                """
                WITH metric_protected AS (
                    SELECT DISTINCT snapshot_id
                    FROM metric_observations
                ), task_protected AS (
                    SELECT DISTINCT observations.snapshot_id
                    FROM metric_observations AS observations
                    INNER JOIN commerce_task_run_observations AS links
                        ON links.observation_id = observations.id
                ), ranked AS (
                    SELECT
                        snapshots.id,
                        snapshots.source_path,
                        snapshots.snapshot_type,
                        snapshots.imported_at,
                        LENGTH(CAST(snapshots.payload_json AS BLOB)) AS payload_bytes,
                        ROW_NUMBER() OVER (
                            PARTITION BY snapshots.source_path
                            ORDER BY snapshots.id DESC
                        ) AS source_rank,
                        CASE WHEN metric_protected.snapshot_id IS NULL THEN 0 ELSE 1 END
                            AS metric_protected,
                        CASE WHEN task_protected.snapshot_id IS NULL THEN 0 ELSE 1 END
                            AS task_protected
                    FROM snapshots
                    LEFT JOIN metric_protected
                        ON metric_protected.snapshot_id = snapshots.id
                    LEFT JOIN task_protected
                        ON task_protected.snapshot_id = snapshots.id
                )
                SELECT
                    source_path,
                    MAX(CASE WHEN source_rank = 1 THEN snapshot_type ELSE '' END) AS snapshot_type,
                    COUNT(*) AS history_rows,
                    COALESCE(SUM(payload_bytes), 0) AS payload_bytes,
                    MIN(imported_at) AS oldest_imported_at,
                    MAX(imported_at) AS newest_imported_at,
                    COALESCE(SUM(metric_protected), 0) AS metric_protected_rows,
                    COALESCE(SUM(task_protected), 0) AS task_protected_rows,
                    COALESCE(SUM(
                        CASE
                            WHEN source_rank > ?
                             AND julianday(imported_at) < julianday(?)
                             AND metric_protected = 0
                            THEN 1 ELSE 0
                        END
                    ), 0) AS archive_candidate_rows,
                    COALESCE(SUM(
                        CASE
                            WHEN source_rank > ?
                             AND julianday(imported_at) < julianday(?)
                             AND metric_protected = 0
                            THEN payload_bytes ELSE 0
                        END
                    ), 0) AS archive_candidate_payload_bytes
                FROM ranked
                GROUP BY source_path
                ORDER BY history_rows DESC, source_path
                """,
                (
                    effective.keep_latest_per_source,
                    cutoff.isoformat(),
                    effective.keep_latest_per_source,
                    cutoff.isoformat(),
                ),
            ).fetchall()

            meta = connection.execute(
                """
                SELECT
                    COALESCE(MAX(id), 0) AS max_snapshot_id,
                    COALESCE(MAX(imported_at), '') AS newest_imported_at
                FROM snapshots
                """
            ).fetchone()
            base["snapshot_rows"] = sum(int(row["history_rows"]) for row in grouped_rows)
            base["logical_sources"] = len(grouped_rows)
            base["payload_bytes"] = sum(int(row["payload_bytes"]) for row in grouped_rows)
            base["archive_candidate_rows"] = sum(
                int(row["archive_candidate_rows"]) for row in grouped_rows
            )
            base["archive_candidate_payload_bytes"] = sum(
                int(row["archive_candidate_payload_bytes"]) for row in grouped_rows
            )
            base["metric_observation_protected_rows"] = sum(
                int(row["metric_protected_rows"]) for row in grouped_rows
            )
            base["task_evidence_protected_rows"] = sum(
                int(row["task_protected_rows"]) for row in grouped_rows
            )

            semantic_totals = {
                "sampled_capture_rows": 0,
                "sampled_payload_bytes": 0,
                "semantic_state_rows": 0,
                "semantic_repeat_rows": 0,
                "consecutive_semantic_repeat_rows": 0,
                "integrity_error_rows": 0,
            }
            semantic_byte_limit_reached = False
            hotspots: list[dict[str, Any]] = []
            for row in grouped_rows[: effective.hotspot_limit]:
                source_path = str(row["source_path"])
                samples = connection.execute(
                    """
                    SELECT id, content_sha256, payload_json
                    FROM snapshots
                    WHERE source_path = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (source_path, effective.semantic_sample_rows_per_source),
                )
                seen_semantic: set[str] = set()
                previous_semantic = ""
                valid_rows = 0
                sampled_rows = 0
                sampled_payload_bytes = 0
                consecutive_repeats = 0
                integrity_errors = 0
                for sample in samples:
                    payload_json = str(sample["payload_json"])
                    payload_bytes = len(payload_json.encode("utf-8"))
                    if (
                        semantic_totals["sampled_payload_bytes"] + payload_bytes
                        > effective.semantic_sample_total_bytes
                    ):
                        semantic_byte_limit_reached = True
                        break
                    sampled_rows += 1
                    sampled_payload_bytes += payload_bytes
                    semantic_totals["sampled_payload_bytes"] += payload_bytes
                    raw_digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
                    if raw_digest != str(sample["content_sha256"]):
                        integrity_errors += 1
                        previous_semantic = ""
                        continue
                    semantic_digest = self._semantic_snapshot_digest(payload_json)
                    valid_rows += 1
                    if previous_semantic and semantic_digest == previous_semantic:
                        consecutive_repeats += 1
                    previous_semantic = semantic_digest
                    seen_semantic.add(semantic_digest)
                semantic_states = len(seen_semantic)
                semantic_repeats = max(0, valid_rows - semantic_states)
                history_rows = int(row["history_rows"])
                semantic_totals["sampled_capture_rows"] += valid_rows
                semantic_totals["semantic_state_rows"] += semantic_states
                semantic_totals["semantic_repeat_rows"] += semantic_repeats
                semantic_totals["consecutive_semantic_repeat_rows"] += consecutive_repeats
                semantic_totals["integrity_error_rows"] += integrity_errors
                hotspots.append(
                    {
                        "source_key": hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:16],
                        "source_name": self._snapshot_source_name(source_path),
                        "snapshot_type": str(row["snapshot_type"] or "snapshot")[:80],
                        "history_rows": history_rows,
                        "payload_bytes": int(row["payload_bytes"]),
                        "oldest_imported_at": row["oldest_imported_at"],
                        "newest_imported_at": row["newest_imported_at"],
                        "metric_observation_protected_rows": int(row["metric_protected_rows"]),
                        "task_evidence_protected_rows": int(row["task_protected_rows"]),
                        "archive_candidate_rows": int(row["archive_candidate_rows"]),
                        "archive_candidate_payload_bytes": int(
                            row["archive_candidate_payload_bytes"]
                        ),
                        "semantic_sample_rows": valid_rows,
                        "semantic_sample_payload_bytes": sampled_payload_bytes,
                        "semantic_analysis_complete": (
                            sampled_rows >= history_rows and integrity_errors == 0
                        ),
                        "semantic_state_rows": semantic_states,
                        "semantic_repeat_rows": semantic_repeats,
                        "semantic_repeat_ratio": (
                            round(semantic_repeats / valid_rows, 4) if valid_rows else 0.0
                        ),
                        "consecutive_semantic_repeat_rows": consecutive_repeats,
                        "integrity_error_rows": integrity_errors,
                    }
                )
            base["hotspots"] = hotspots
            sampled_rows = semantic_totals["sampled_capture_rows"]
            base["semantic_analysis"] = {
                **semantic_totals,
                "sample_scope": "recent_rows_of_top_sources",
                "sample_limit_per_source": effective.semantic_sample_rows_per_source,
                "sample_total_byte_limit": effective.semantic_sample_total_bytes,
                "sample_byte_limit_reached": semantic_byte_limit_reached,
                "semantic_repeat_ratio": (
                    round(semantic_totals["semantic_repeat_rows"] / sampled_rows, 4)
                    if sampled_rows
                    else 0.0
                ),
                "observation_count_preserved": base["snapshot_rows"],
            }

            reasons: list[str] = []
            if base["snapshot_rows"] >= effective.review_after_total_rows:
                reasons.append("total_history_rows_high")
            if any(
                int(row["history_rows"]) >= effective.review_after_source_rows
                for row in grouped_rows
            ):
                reasons.append("source_history_rows_high")
            if database_bytes + wal_bytes >= effective.review_after_database_bytes:
                reasons.append("database_size_high")
            if base["archive_candidate_rows"]:
                reasons.append("archive_candidates_available")
            if sampled_rows >= 50 and base["semantic_analysis"]["semantic_repeat_ratio"] >= 0.5:
                reasons.append("semantic_repeat_rate_high")
            if semantic_totals["integrity_error_rows"]:
                reasons.append("snapshot_integrity_error")
            base["review_reason_codes"] = reasons
            base["status"] = "review_recommended" if reasons else "healthy"

            watermark = {
                "schema": schema,
                "max_snapshot_id": int(meta["max_snapshot_id"]),
                "newest_imported_at": str(meta["newest_imported_at"] or ""),
                "snapshot_rows": base["snapshot_rows"],
                "logical_sources": base["logical_sources"],
                "payload_bytes": base["payload_bytes"],
                "archive_candidate_rows": base["archive_candidate_rows"],
                "archive_candidate_payload_bytes": base["archive_candidate_payload_bytes"],
            }
            base["watermark"] = watermark
            fingerprint_payload = {
                "contract_version": base["contract_version"],
                "policy": base["policy"],
                "archive_cutoff": base["archive_cutoff"],
                "watermark": watermark,
            }
            base["preview_id"] = hashlib.sha256(
                json.dumps(
                    fingerprint_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            return base
        except sqlite3.Error as exc:
            raise LocalStoreError("Cannot build snapshot lifecycle preview") from exc
        finally:
            connection.close()

    def _legacy_json_migration_record(
        self,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        """Read the durable one-time migration marker without mutating state."""

        owns_connection = connection is None
        active_connection = connection or self.connect()
        try:
            row = active_connection.execute(
                "SELECT meta_value FROM local_store_meta WHERE meta_key = ?",
                (_LEGACY_JSON_MIGRATION_KEY,),
            ).fetchone()
            if row is None:
                return None
            try:
                value = json.loads(str(row["meta_value"]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise LocalStoreError(
                    "Legacy JSON migration marker is corrupt; refusing ambiguous recovery"
                ) from exc
            if not isinstance(value, dict) or value.get("status") != "complete":
                raise LocalStoreError(
                    "Legacy JSON migration marker is invalid; refusing ambiguous recovery"
                )
            return value
        except sqlite3.Error as exc:
            raise LocalStoreError("Cannot read local store migration state") from exc
        finally:
            if owns_connection:
                active_connection.close()

    def legacy_json_migration_status(self) -> dict[str, Any]:
        """Return whether legacy JSON has been adopted exactly once."""

        self.initialize()
        record = self._legacy_json_migration_record()
        if record is None:
            return {
                "status": "pending",
                "authoritative_source": "sqlite",
                "json_compatibility": "migration_source_only",
            }
        return {
            **record,
            "authoritative_source": "sqlite",
            "json_compatibility": "non_authoritative_mirror",
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

    def migrate_legacy_json_snapshots(
        self,
        sources: Iterable[str | os.PathLike[str]],
    ) -> list[ImportedSnapshot]:
        """Adopt legacy JSON once without allowing it to become a second source.

        Existing SQLite paths always win.  The input batch and completion
        marker share one transaction, so a malformed file or disk failure
        leaves both the previous database and the migration status unchanged.
        Once complete, later JSON edits are treated only as compatibility
        mirror changes and are never re-imported automatically.
        """

        self.initialize()
        if self._legacy_json_migration_record() is not None:
            return []

        files = self._expand_json_sources(sources)
        prepared: list[tuple[Path, str, str | None, str, str]] = []
        try:
            for path in files:
                with path.open("r", encoding="utf-8-sig") as source_file:
                    payload = json.load(source_file)
                canonical_json = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                prepared.append(
                    (
                        path.resolve(),
                        self._infer_snapshot_type(path, payload),
                        self._infer_captured_at(path, payload),
                        hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
                        canonical_json,
                    )
                )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SnapshotImportError(f"Cannot migrate legacy JSON snapshot: {exc}") from exc

        imported: list[ImportedSnapshot] = []
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            # A second Agent process may have completed the migration while
            # this process prepared its files. Recheck under the write lock.
            if self._legacy_json_migration_record(connection) is not None:
                connection.rollback()
                return []
            for path, item_type, captured_at, digest, canonical_json in prepared:
                canonical_path = str(path)
                exists = connection.execute(
                    "SELECT 1 FROM snapshots WHERE source_path = ? LIMIT 1",
                    (canonical_path,),
                ).fetchone()
                if exists is not None:
                    continue
                imported.append(
                    self._insert_snapshot(
                        connection,
                        source_path=canonical_path,
                        snapshot_type=item_type,
                        captured_at=captured_at,
                        content_sha256=digest,
                        payload_json=canonical_json,
                    )
                )
            completed_at = datetime.now(timezone.utc).isoformat()
            record = {
                "status": "complete",
                "policy": "sqlite_authoritative_json_mirror",
                "completed_at": completed_at,
                "discovered_files": len(prepared),
                "imported_files": len(imported),
                "preserved_sqlite_paths": len(prepared) - len(imported),
            }
            connection.execute(
                """
                INSERT INTO local_store_meta (meta_key, meta_value, updated_at)
                VALUES (?, ?, ?)
                """,
                (
                    _LEGACY_JSON_MIGRATION_KEY,
                    json.dumps(record, sort_keys=True, separators=(",", ":")),
                    completed_at,
                ),
            )
            connection.commit()
            return imported
        except Exception as exc:
            connection.rollback()
            if isinstance(exc, LocalStoreError):
                raise
            raise SnapshotImportError("Legacy JSON migration was rolled back") from exc
        finally:
            connection.close()

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

    def persist_snapshot_bundle(
        self,
        payload: Any,
        source_path: str | os.PathLike[str],
        *,
        snapshot_type: str | None = None,
        commerce: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a source snapshot and its commerce memory in one transaction.

        JSON compatibility files are managed by the receiver, but SQLite must
        never expose a source snapshot without the entity/metric rows derived
        from that same source.  Replaying the same bundle remains idempotent.
        """

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
            commerce_result = {"entities": 0, "relations": 0, "observations": 0}
            if isinstance(commerce, Mapping):
                commerce_result = self.persist_commerce_graph(
                    store_key=str(commerce.get("store_key") or ""),
                    account_key=str(commerce.get("account_key") or ""),
                    entities=commerce.get("entities") or (),
                    relations=commerce.get("relations") or (),
                    observations=commerce.get("observations") or (),
                    source=str(commerce.get("source") or ""),
                    page_type=str(commerce.get("page_type") or ""),
                    captured_at_ms=int(commerce.get("captured_at_ms") or 0),
                    snapshot_id=imported.id,
                    identity_key_fingerprint=str(commerce.get("identity_key_fingerprint") or ""),
                    _connection=connection,
                )
            connection.commit()
            return {"snapshot": imported, "commerce": commerce_result}
        except Exception as exc:
            connection.rollback()
            if isinstance(exc, LocalStoreError):
                raise
            raise SnapshotImportError("Snapshot bundle persistence was rolled back") from exc
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
    def _canonical_filesystem_path(value: str | os.PathLike[str]) -> str:
        text = os.fspath(value)
        if "://" in text:
            return text
        return os.path.normcase(str(Path(text).expanduser().resolve()))

    @staticmethod
    def _decode_authoritative_snapshot(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        payload_json = str(item.pop("payload_json"))
        actual_digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if actual_digest != str(item.get("content_sha256") or ""):
            raise LocalStoreError(
                "SQLite snapshot integrity check failed; refusing legacy JSON fallback"
            )
        try:
            item["payload"] = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise LocalStoreError(
                "SQLite snapshot payload is corrupt; refusing legacy JSON fallback"
            ) from exc
        item["authoritative_source"] = "sqlite"
        return item

    @staticmethod
    def _payload_source(payload: Any) -> str:
        if not isinstance(payload, Mapping):
            return ""
        data = payload.get("data")
        nested_source = data.get("source") if isinstance(data, Mapping) else ""
        return str(payload.get("source") or nested_source or "").strip().lower()

    @staticmethod
    def _payload_account_key(payload: Any) -> str:
        if not isinstance(payload, Mapping):
            return ""
        data = payload.get("data")
        if not isinstance(data, Mapping):
            data = payload
        account = data.get("account")
        if isinstance(account, Mapping) and account.get("key"):
            return str(account.get("key") or "").strip().lower()
        context = data.get("promotion_context")
        scope = context.get("account_scope") if isinstance(context, Mapping) else None
        return str(scope.get("account_id") or "").strip().lower() if isinstance(scope, Mapping) else ""

    def latest_snapshot(
        self,
        *,
        source_path: str | os.PathLike[str] | None = None,
        source_directory: str | os.PathLike[str] | None = None,
        source_name: str | None = None,
        snapshot_type: str | None = None,
        account_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Read the latest committed SQLite snapshot for one logical source.

        Exactly one path selector is required.  No JSON fallback is attempted:
        a damaged or unavailable database therefore fails closed instead of
        reviving a potentially stale compatibility file.
        """

        selector_count = sum(
            selector is not None for selector in (source_path, source_directory, source_name)
        )
        if selector_count != 1:
            raise ValueError("exactly one snapshot source selector is required")
        source_name_value = str(source_name or "").strip().lower()
        if source_name is not None and not re.fullmatch(r"[a-z0-9_-]{1,32}", source_name_value):
            raise ValueError("invalid snapshot source name")
        account_key_value = str(account_key or "").strip().lower()
        if account_key is not None and not re.fullmatch(r"[a-z0-9_-]{1,48}", account_key_value):
            raise ValueError("invalid snapshot account key")
        rows = self._authoritative_snapshot_rows(snapshot_type=snapshot_type)
        target_path = (
            self._canonical_filesystem_path(source_path)
            if source_path is not None
            else None
        )
        target_directory = (
            self._canonical_filesystem_path(source_directory)
            if source_directory is not None
            else None
        )
        for row in rows:
            stored_path = str(row["source_path"])
            if "://" in stored_path and target_directory is not None:
                continue
            canonical_path = self._canonical_filesystem_path(stored_path)
            if target_path is not None and canonical_path != target_path:
                continue
            if target_directory is not None:
                candidate = Path(canonical_path)
                if os.path.normcase(str(candidate.parent)) != target_directory:
                    continue
                if candidate.suffix.lower() != ".json":
                    continue
            decoded = self._decode_authoritative_snapshot(row)
            if source_name is not None:
                payload = decoded.get("payload")
                if self._payload_source(payload) != source_name_value:
                    continue
            if account_key is not None and self._payload_account_key(decoded.get("payload")) != account_key_value:
                continue
            return decoded
        return None

    def iter_latest_snapshots(
        self,
        *,
        source_directory: str | os.PathLike[str] | None = None,
        source_name: str | None = None,
        snapshot_type: str | None = None,
        account_key: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield one latest SQLite row per direct compatibility-file path."""

        if (source_directory is None) == (source_name is None):
            raise ValueError("exactly one snapshot source selector is required")
        source_name_value = str(source_name or "").strip().lower()
        if source_name is not None and not re.fullmatch(r"[a-z0-9_-]{1,32}", source_name_value):
            raise ValueError("invalid snapshot source name")
        account_key_value = str(account_key or "").strip().lower()
        if account_key is not None and not re.fullmatch(r"[a-z0-9_-]{1,48}", account_key_value):
            raise ValueError("invalid snapshot account key")
        target_directory = (
            self._canonical_filesystem_path(source_directory)
            if source_directory is not None
            else None
        )
        seen: set[str] = set()
        for row in self._authoritative_snapshot_rows(snapshot_type=snapshot_type):
            stored_path = str(row["source_path"])
            if "://" in stored_path and target_directory is not None:
                continue
            canonical_path = self._canonical_filesystem_path(stored_path)
            if target_directory is not None:
                candidate = Path(canonical_path)
                if os.path.normcase(str(candidate.parent)) != target_directory:
                    continue
                if candidate.suffix.lower() != ".json" or canonical_path in seen:
                    continue
                logical_key = canonical_path
                # Integrity is scoped to the requested directory. A damaged
                # snapshot from another account must not make this account's
                # catalog unavailable, while every row we do return is still
                # verified before it becomes visible.
                decoded = self._decode_authoritative_snapshot(row)
            else:
                decoded = self._decode_authoritative_snapshot(row)
                payload = decoded.get("payload")
                if self._payload_source(payload) != source_name_value:
                    continue
                logical_key = str(row["snapshot_type"])
                if logical_key in seen:
                    continue
            if account_key is not None and self._payload_account_key(decoded.get("payload")) != account_key_value:
                continue
            seen.add(logical_key)
            yield decoded

    def _authoritative_snapshot_rows(
        self,
        *,
        snapshot_type: str | None = None,
    ) -> list[sqlite3.Row]:
        """Return only the newest committed row for each compatibility path.

        ``snapshots`` intentionally retains history, so reading the whole table
        here makes every latest/catalog lookup grow with the lifetime of an
        installation.  Keep the integrity check in Python, but let SQLite use
        ``idx_snapshots_source_latest`` to collapse history before payloads are
        decoded.
        """

        self.initialize()
        latest_sql = "SELECT source_path, MAX(id) AS latest_id FROM snapshots"
        parameters: tuple[Any, ...] = ()
        if snapshot_type is not None:
            latest_sql += " WHERE snapshot_type = ?"
            parameters = (snapshot_type,)
        latest_sql += " GROUP BY source_path"
        sql = f"""
            SELECT snapshots.*
            FROM snapshots
            INNER JOIN ({latest_sql}) AS latest
                ON latest.latest_id = snapshots.id
            ORDER BY snapshots.id DESC
        """
        connection = self.connect()
        try:
            return list(connection.execute(sql, parameters).fetchall())
        except sqlite3.Error as exc:
            raise LocalStoreError("Cannot read authoritative SQLite snapshots") from exc
        finally:
            connection.close()

    def persist_commerce_graph(
        self,
        *,
        store_key: str,
        entities: Iterable[Mapping[str, Any]],
        relations: Iterable[Mapping[str, Any]] = (),
        observations: Iterable[Mapping[str, Any]] = (),
        source: str,
        page_type: str,
        captured_at_ms: int,
        snapshot_id: int,
        account_key: str = "",
        identity_key_fingerprint: str,
        _connection: sqlite3.Connection | None = None,
    ) -> dict[str, int]:
        """Persist one anonymous, exact-ID commerce graph update atomically.

        The caller must provide installation-local HMAC entity keys. Raw
        platform IDs and name-only joins are rejected by the key contract.
        Every record is scoped to one confirmed store so identical IDs from
        different stores can never share history.
        """

        scope = str(store_key or "").strip().lower()
        if not _COMMERCE_SCOPE_KEY.fullmatch(scope):
            raise LocalStoreError("A confirmed store key is required for commerce memory")
        account_scope = str(account_key or "").strip().lower()
        if account_scope and not _COMMERCE_SCOPE_KEY.fullmatch(account_scope):
            raise LocalStoreError("Invalid commerce account key")
        fingerprint_value = str(identity_key_fingerprint or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{16,64}", fingerprint_value):
            raise LocalStoreError("Identity key fingerprint is required for commerce memory")
        source_value = str(source or "").strip().lower()[:32]
        page_value = str(page_type or "").strip().lower()[:48]
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", source_value):
            raise LocalStoreError("Invalid commerce source")
        if not re.fullmatch(r"[a-z0-9_-]{1,48}", page_value):
            raise LocalStoreError("Invalid commerce page type")
        try:
            default_captured = int(captured_at_ms)
        except (TypeError, ValueError) as exc:
            raise LocalStoreError("Invalid commerce capture time") from exc
        if default_captured <= 0:
            raise LocalStoreError("Commerce capture time must be positive")
        try:
            source_snapshot_id = int(snapshot_id)
        except (TypeError, ValueError) as exc:
            raise LocalStoreError("Commerce memory requires a source snapshot") from exc
        if source_snapshot_id <= 0:
            raise LocalStoreError("Commerce memory requires a source snapshot")
        if source_value == "qianchuan" and not account_scope:
            raise LocalStoreError("Qianchuan commerce memory requires a confirmed account")

        entity_rows: dict[str, dict[str, Any]] = {}
        for item in list(entities)[:2000]:
            if not isinstance(item, Mapping):
                continue
            entity_key = str(item.get("entity_key") or "").strip().lower()
            entity_type = str(item.get("entity_type") or "").strip()
            if not _COMMERCE_ENTITY_KEY.fullmatch(entity_key):
                raise LocalStoreError("Commerce memory accepts local HMAC entity keys only")
            if entity_type not in _COMMERCE_ENTITY_TYPES:
                raise LocalStoreError("Unsupported commerce entity type")
            entity_rows[entity_key] = {
                "entity_key": entity_key,
                "entity_type": entity_type,
                "display_name": re.sub(r"\s+", " ", str(item.get("display_name") or "")).strip()[:120],
                "attrs_json": json.dumps(
                    {
                        "confidence": str(item.get("confidence") or "exact")[:24],
                        "evidence": item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {},
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }

        relation_rows: list[dict[str, Any]] = []
        for item in list(relations)[:4000]:
            if not isinstance(item, Mapping):
                continue
            relation_key = str(item.get("relation_key") or "").strip().lower()
            from_key = str(item.get("from_key") or "").strip().lower()
            to_key = str(item.get("to_key") or "").strip().lower()
            relation = str(item.get("relation") or "").strip().lower()
            if not re.fullmatch(r"[a-f0-9]{24}", relation_key):
                raise LocalStoreError("Invalid commerce relation key")
            if not _COMMERCE_ENTITY_KEY.fullmatch(from_key) or not _COMMERCE_ENTITY_KEY.fullmatch(to_key):
                raise LocalStoreError("Commerce relation contains an invalid entity key")
            if not re.fullmatch(r"[a-z][a-z0-9_]{2,39}", relation):
                raise LocalStoreError("Invalid commerce relation")
            relation_rows.append({
                "relation_key": relation_key,
                "from_key": from_key,
                "to_key": to_key,
                "relation": relation,
                "confidence": str(item.get("confidence") or "exact_row")[:24],
                "evidence_json": json.dumps(
                    item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            })

        observation_rows: list[dict[str, Any]] = []
        for item in list(observations)[:10000]:
            if not isinstance(item, Mapping):
                continue
            entity_key = str(item.get("entity_key") or "").strip().lower()
            metric = str(item.get("metric") or "").strip().lower()
            if not _COMMERCE_ENTITY_KEY.fullmatch(entity_key):
                raise LocalStoreError("Commerce observation contains an invalid entity key")
            if not re.fullmatch(r"[a-z][a-z0-9_]{1,47}", metric):
                raise LocalStoreError("Invalid commerce metric")
            try:
                value = float(item.get("value"))
                observed_at = int(item.get("captured_at_ms") or default_captured)
                quality_score = max(0, min(100, int(item.get("quality_score") or 0)))
            except (TypeError, ValueError) as exc:
                raise LocalStoreError("Invalid commerce observation value") from exc
            if not math.isfinite(value) or observed_at <= 0:
                raise LocalStoreError("Commerce observations require finite values and a positive time")
            unit = str(item.get("unit") or "number").strip().lower()
            channel = str(item.get("channel") or "other").strip().lower()
            window_key = str(item.get("window_key") or "unknown").strip().lower()
            metric_contract = str(item.get("metric_contract") or metric).strip().lower()
            if not re.fullmatch(r"[a-z][a-z0-9_]{1,23}", unit):
                raise LocalStoreError("Invalid commerce metric unit")
            if not re.fullmatch(r"[a-z][a-z0-9_]{1,23}", channel):
                raise LocalStoreError("Invalid commerce metric channel")
            if not re.fullmatch(r"[a-z0-9_:-]{1,64}", window_key):
                raise LocalStoreError("Invalid commerce metric window")
            if not re.fullmatch(r"[a-z0-9_:-]{2,96}", metric_contract):
                raise LocalStoreError("Invalid commerce metric contract")
            context_key = hashlib.sha256(
                f"{account_scope}|{metric_contract}|{unit}|{channel}|{window_key}".encode("utf-8")
            ).hexdigest()[:24]
            evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
            fingerprint_source = {
                "entity_key": entity_key,
                "metric": metric,
                "value": value,
                "captured_at_ms": observed_at,
                "source": source_value,
                "page_type": page_value,
                "unit": unit,
                "channel": channel,
                "window_key": window_key,
                "metric_contract": metric_contract,
                "context_key": context_key,
                "table_index": evidence.get("table_index"),
                "row_index": evidence.get("row_index"),
            }
            fingerprint = hashlib.sha256(
                json.dumps(fingerprint_source, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            observation_rows.append({
                "entity_key": entity_key,
                "metric": metric,
                "value": value,
                "captured_at_ms": observed_at,
                "quality_score": quality_score,
                "unit": unit,
                "channel": channel,
                "window_key": window_key,
                "metric_contract": metric_contract,
                "context_key": context_key,
                "attribution_scope": str(item.get("attribution_scope") or "exact_row")[:32],
                "evidence_json": json.dumps(
                    evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "fingerprint": fingerprint,
            })

        owns_connection = _connection is None
        if owns_connection:
            self.initialize()
        connection = _connection or self.connect()
        inserted_observations = 0
        try:
            if owns_connection:
                connection.execute("BEGIN IMMEDIATE")
            existing_fingerprint = connection.execute(
                "SELECT meta_value FROM commerce_meta WHERE meta_key = 'identity_key_fingerprint'"
            ).fetchone()
            if existing_fingerprint and str(existing_fingerprint["meta_value"]) != fingerprint_value:
                raise LocalStoreError("Commerce identity key changed; restore the matching identity secret before writing")
            connection.execute(
                """
                INSERT INTO commerce_meta (meta_key, meta_value, updated_at)
                VALUES ('identity_key_fingerprint', ?, ?)
                ON CONFLICT(meta_key) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (fingerprint_value, datetime.now(timezone.utc).isoformat()),
            )
            snapshot_exists = connection.execute(
                "SELECT 1 FROM snapshots WHERE id = ?",
                (source_snapshot_id,),
            ).fetchone()
            if not snapshot_exists:
                raise LocalStoreError("Commerce memory source snapshot does not exist")
            for item in entity_rows.values():
                connection.execute(
                    """
                    INSERT INTO commerce_entities (
                        store_key, entity_key, entity_type, display_name, attrs_json,
                        first_seen_at_ms, last_seen_at_ms, last_source, last_page_type
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(store_key, entity_key) DO UPDATE SET
                        entity_type = excluded.entity_type,
                        display_name = CASE WHEN excluded.display_name <> ''
                            THEN excluded.display_name ELSE commerce_entities.display_name END,
                        attrs_json = excluded.attrs_json,
                        first_seen_at_ms = MIN(commerce_entities.first_seen_at_ms, excluded.first_seen_at_ms),
                        last_seen_at_ms = MAX(commerce_entities.last_seen_at_ms, excluded.last_seen_at_ms),
                        last_source = CASE WHEN excluded.last_seen_at_ms >= commerce_entities.last_seen_at_ms
                            THEN excluded.last_source ELSE commerce_entities.last_source END,
                        last_page_type = CASE WHEN excluded.last_seen_at_ms >= commerce_entities.last_seen_at_ms
                            THEN excluded.last_page_type ELSE commerce_entities.last_page_type END
                    """,
                    (
                        scope, item["entity_key"], item["entity_type"], item["display_name"],
                        item["attrs_json"], default_captured, default_captured, source_value, page_value,
                    ),
                )

            known_keys = {
                str(row["entity_key"])
                for row in connection.execute(
                    "SELECT entity_key FROM commerce_entities WHERE store_key = ?",
                    (scope,),
                ).fetchall()
            }
            for item in relation_rows:
                if item["from_key"] not in known_keys or item["to_key"] not in known_keys:
                    raise LocalStoreError("Commerce relation endpoint is outside the current store scope")
                connection.execute(
                    """
                    INSERT INTO commerce_relations (
                        store_key, relation_key, from_key, to_key, relation, confidence,
                        evidence_json, first_seen_at_ms, last_seen_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(store_key, relation_key) DO UPDATE SET
                        confidence = excluded.confidence,
                        evidence_json = excluded.evidence_json,
                        first_seen_at_ms = MIN(commerce_relations.first_seen_at_ms, excluded.first_seen_at_ms),
                        last_seen_at_ms = MAX(commerce_relations.last_seen_at_ms, excluded.last_seen_at_ms)
                    """,
                    (
                        scope, item["relation_key"], item["from_key"], item["to_key"],
                        item["relation"], item["confidence"], item["evidence_json"],
                        default_captured, default_captured,
                    ),
                )
            for item in observation_rows:
                if item["entity_key"] not in known_keys:
                    raise LocalStoreError("Commerce observation entity is outside the current store scope")
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO metric_observations (
                        snapshot_id, store_key, account_key, entity_key, metric, metric_contract,
                        value, unit, channel, window_key, context_key, captured_at_ms,
                        source, page_type, quality_score,
                        attribution_scope, evidence_json, fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_snapshot_id, scope, account_scope, item["entity_key"], item["metric"],
                        item["metric_contract"], item["value"], item["unit"], item["channel"],
                        item["window_key"], item["context_key"], item["captured_at_ms"],
                        source_value, page_value, item["quality_score"],
                        item["attribution_scope"], item["evidence_json"], item["fingerprint"],
                    ),
                )
                inserted_observations += max(0, int(cursor.rowcount))
            if owns_connection:
                connection.commit()
        except Exception as exc:
            if owns_connection:
                connection.rollback()
            if isinstance(exc, LocalStoreError):
                raise
            raise LocalStoreError("Commerce memory update was rolled back") from exc
        finally:
            if owns_connection:
                connection.close()
        return {
            "entities": len(entity_rows),
            "relations": len(relation_rows),
            "observations": inserted_observations,
        }

    @staticmethod
    def _task_json(value: Mapping[str, Any] | None, *, label: str) -> str:
        document: Mapping[str, Any] = value if isinstance(value, Mapping) else {}
        try:
            encoded = json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise LocalStoreError(f"Commerce task {label} must be JSON serializable") from exc
        if len(encoded.encode("utf-8")) > 32 * 1024:
            raise LocalStoreError(f"Commerce task {label} is too large")
        return encoded

    @staticmethod
    def _task_scope_values(
        store_key: str,
        account_key: str,
        entity_key: str,
    ) -> tuple[str, str, str]:
        scope = str(store_key or "").strip().lower()
        account_scope = str(account_key or "").strip().lower()
        entity = str(entity_key or "").strip().lower()
        if not _COMMERCE_SCOPE_KEY.fullmatch(scope):
            raise LocalStoreError("A confirmed store key is required for a commerce task run")
        if account_scope and not _COMMERCE_SCOPE_KEY.fullmatch(account_scope):
            raise LocalStoreError("Invalid commerce task account key")
        if not _COMMERCE_ENTITY_KEY.fullmatch(entity):
            raise LocalStoreError("Commerce task runs require a local HMAC entity key")
        return scope, account_scope, entity

    @staticmethod
    def _decode_task_run(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_observations: bool,
    ) -> dict[str, Any]:
        result = dict(row)
        for source_key, target_key in (
            ("contract_json", "contract"),
            ("result_json", "result"),
        ):
            try:
                result[target_key] = json.loads(str(result.pop(source_key)))
            except (TypeError, json.JSONDecodeError):
                result[target_key] = {}
                result.pop(source_key, None)
        count_rows = connection.execute(
            """
            SELECT phase, COUNT(*) AS linked_count
            FROM commerce_task_run_observations
            WHERE run_id = ? GROUP BY phase
            """,
            (str(row["run_id"]),),
        ).fetchall()
        result["observation_counts"] = {"baseline": 0, "readback": 0}
        for count_row in count_rows:
            result["observation_counts"][str(count_row["phase"])] = int(
                count_row["linked_count"] or 0
            )
        if not include_observations:
            return result
        result["observations"] = {"baseline": [], "readback": []}
        observation_rows = connection.execute(
            """
            SELECT link.phase, observation.id AS observation_id,
                   observation.snapshot_id, observation.account_key,
                   observation.entity_key, observation.metric,
                   observation.metric_contract, observation.value,
                   observation.unit, observation.channel,
                   observation.window_key, observation.context_key,
                   observation.captured_at_ms, observation.source,
                   observation.page_type, observation.quality_score,
                   observation.attribution_scope, observation.evidence_json,
                   link.linked_at
            FROM commerce_task_run_observations AS link
            JOIN metric_observations AS observation
              ON observation.id = link.observation_id
            WHERE link.run_id = ?
            ORDER BY link.phase, observation.captured_at_ms, observation.id
            """,
            (str(row["run_id"]),),
        ).fetchall()
        for observation_row in observation_rows:
            item = dict(observation_row)
            phase = str(item.pop("phase"))
            try:
                item["evidence"] = json.loads(str(item.pop("evidence_json")))
            except (TypeError, json.JSONDecodeError):
                item["evidence"] = {}
                item.pop("evidence_json", None)
            result["observations"][phase].append(item)
        return result

    def start_commerce_task_run(
        self,
        *,
        store_key: str,
        account_key: str = "",
        entity_key: str,
        task_key: str,
        subject_kind: str,
        rule_id: str,
        contract_fingerprint: str,
        business_date: str,
        started_at_ms: int,
        due_at_ms: int,
        contract: Mapping[str, Any] | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Start one durable task run, or return the same run on an exact retry."""

        scope, account_scope, entity = self._task_scope_values(
            store_key, account_key, entity_key
        )
        task = str(task_key or "").strip().lower()
        subject = str(subject_kind or "").strip().lower()
        rule = str(rule_id or "").strip().lower()
        fingerprint = str(contract_fingerprint or "").strip().lower()
        day = str(business_date or "").strip()
        if not re.fullmatch(r"[a-f0-9]{24,64}", task):
            raise LocalStoreError("Invalid commerce task key")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", subject):
            raise LocalStoreError("Invalid commerce task subject kind")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{1,119}", rule):
            raise LocalStoreError("Invalid commerce task rule id")
        if not re.fullmatch(r"[a-f0-9]{16,64}", fingerprint):
            raise LocalStoreError("Invalid commerce task contract fingerprint")
        try:
            datetime.strptime(day, "%Y-%m-%d")
            started = int(started_at_ms)
            due = int(due_at_ms)
        except (TypeError, ValueError) as exc:
            raise LocalStoreError("Invalid commerce task timing") from exc
        if started <= 0 or due < started:
            raise LocalStoreError("Commerce task due time cannot precede its start")
        contract_json = self._task_json(contract, label="contract")
        contract_document = json.loads(contract_json)
        embedded_fingerprint = str(contract_document.get("contract_fingerprint") or "").lower()
        if embedded_fingerprint and embedded_fingerprint != fingerprint:
            raise LocalStoreError("Commerce task contract fingerprint does not match its payload")
        selected_run_id = str(run_id or os.urandom(16).hex()).strip().lower()
        if not re.fullmatch(r"[a-f0-9]{16,64}", selected_run_id):
            raise LocalStoreError("Invalid commerce task run id")

        immutable = {
            "task_key": task,
            "store_key": scope,
            "account_key": account_scope,
            "entity_key": entity,
            "subject_kind": subject,
            "rule_id": rule,
            "contract_fingerprint": fingerprint,
            "contract_json": contract_json,
            "business_date": day,
            "started_at_ms": started,
            "due_at_ms": due,
        }
        self.initialize()
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM commerce_task_runs WHERE run_id = ?",
                (selected_run_id,),
            ).fetchone()
            if existing is not None:
                if any(existing[key] != value for key, value in immutable.items()):
                    raise LocalStoreError("Commerce task run id is already bound to another contract")
                connection.commit()
                decoded = self._decode_task_run(
                    connection, existing, include_observations=True
                )
                decoded["created"] = False
                return decoded
            now = datetime.now(timezone.utc).isoformat()
            try:
                connection.execute(
                    """
                    INSERT INTO commerce_task_runs (
                        run_id, task_key, store_key, account_key, entity_key,
                        subject_kind, rule_id, contract_fingerprint, contract_json,
                        business_date, status, started_at_ms, due_at_ms,
                        completed_at_ms, verdict, result_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, NULL,
                              'pending', '{}', ?, ?)
                    """,
                    (
                        selected_run_id, task, scope, account_scope, entity,
                        subject, rule, fingerprint, contract_json, day,
                        started, due, now, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                open_run = connection.execute(
                    """
                    SELECT run_id FROM commerce_task_runs
                    WHERE store_key = ? AND task_key = ?
                      AND status IN ('running', 'observing', 'awaiting_readback', 'blocked')
                    """,
                    (scope, task),
                ).fetchone()
                if open_run is not None:
                    raise LocalStoreError(
                        "An open commerce task run already exists for this task"
                    ) from exc
                raise LocalStoreError(
                    "Commerce task run requires an entity in the same store"
                ) from exc
            created_row = connection.execute(
                "SELECT * FROM commerce_task_runs WHERE run_id = ?",
                (selected_run_id,),
            ).fetchone()
            connection.commit()
            decoded = self._decode_task_run(
                connection, created_row, include_observations=True
            )
            decoded["created"] = True
            return decoded
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_commerce_task_run(
        self,
        run_id: str,
        *,
        store_key: str,
        account_key: str = "",
        include_observations: bool = True,
    ) -> dict[str, Any] | None:
        """Read a task run only through its exact store and account scope."""

        selected_run_id = str(run_id or "").strip().lower()
        scope = str(store_key or "").strip().lower()
        account_scope = str(account_key or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{16,64}", selected_run_id):
            return None
        if not _COMMERCE_SCOPE_KEY.fullmatch(scope):
            return None
        if account_scope and not _COMMERCE_SCOPE_KEY.fullmatch(account_scope):
            return None
        self.initialize()
        connection = self.connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM commerce_task_runs
                WHERE run_id = ? AND store_key = ? AND account_key = ?
                """,
                (selected_run_id, scope, account_scope),
            ).fetchone()
            if row is None:
                return None
            return self._decode_task_run(
                connection, row, include_observations=bool(include_observations)
            )
        finally:
            connection.close()

    def transition_commerce_task_run(
        self,
        run_id: str,
        status: str,
        *,
        store_key: str,
        account_key: str = "",
        completed_at_ms: int | None = None,
        verdict: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply a validated task state transition within an exact scope."""

        selected_run_id = str(run_id or "").strip().lower()
        scope = str(store_key or "").strip().lower()
        account_scope = str(account_key or "").strip().lower()
        next_status = str(status or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{16,64}", selected_run_id):
            raise LocalStoreError("Invalid commerce task run id")
        if not _COMMERCE_SCOPE_KEY.fullmatch(scope):
            raise LocalStoreError("Invalid commerce task store key")
        if account_scope and not _COMMERCE_SCOPE_KEY.fullmatch(account_scope):
            raise LocalStoreError("Invalid commerce task account key")
        if next_status not in _COMMERCE_TASK_TRANSITIONS:
            raise LocalStoreError("Invalid commerce task status")
        verdict_value = str(verdict or "pending").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", verdict_value):
            raise LocalStoreError("Invalid commerce task verdict")
        result_json = self._task_json(result, label="result") if result is not None else None

        self.initialize()
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM commerce_task_runs
                WHERE run_id = ? AND store_key = ? AND account_key = ?
                """,
                (selected_run_id, scope, account_scope),
            ).fetchone()
            if row is None:
                raise LocalStoreError("Commerce task run does not exist in this scope")
            current_status = str(row["status"])
            if current_status == next_status:
                connection.commit()
                decoded = self._decode_task_run(
                    connection, row, include_observations=True
                )
                decoded["transitioned"] = False
                return decoded
            if next_status not in _COMMERCE_TASK_TRANSITIONS[current_status]:
                raise LocalStoreError(
                    f"Commerce task cannot transition from {current_status} to {next_status}"
                )
            completed: int | None = None
            if next_status in _COMMERCE_TASK_TERMINAL_STATUSES:
                try:
                    completed = (
                        int(completed_at_ms)
                        if completed_at_ms is not None
                        else int(datetime.now(timezone.utc).timestamp() * 1000)
                    )
                except (TypeError, ValueError) as exc:
                    raise LocalStoreError("Invalid commerce task completion time") from exc
                if completed < int(row["started_at_ms"]):
                    raise LocalStoreError("Commerce task completion cannot precede its start")
            elif completed_at_ms is not None:
                raise LocalStoreError("Only a terminal commerce task may have a completion time")
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                UPDATE commerce_task_runs
                SET status = ?, completed_at_ms = ?, verdict = ?,
                    result_json = COALESCE(?, result_json), updated_at = ?
                WHERE run_id = ? AND store_key = ? AND account_key = ?
                """,
                (
                    next_status, completed, verdict_value, result_json, now,
                    selected_run_id, scope, account_scope,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM commerce_task_runs WHERE run_id = ?",
                (selected_run_id,),
            ).fetchone()
            connection.commit()
            decoded = self._decode_task_run(
                connection, updated, include_observations=True
            )
            decoded["transitioned"] = True
            return decoded
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def attach_commerce_task_observations(
        self,
        run_id: str,
        phase: str,
        observation_ids: Iterable[int],
        *,
        store_key: str,
        account_key: str = "",
    ) -> dict[str, int]:
        """Attach source observations after enforcing entity, account and time scope."""

        selected_run_id = str(run_id or "").strip().lower()
        scope = str(store_key or "").strip().lower()
        account_scope = str(account_key or "").strip().lower()
        selected_phase = str(phase or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{16,64}", selected_run_id):
            raise LocalStoreError("Invalid commerce task run id")
        if not _COMMERCE_SCOPE_KEY.fullmatch(scope):
            raise LocalStoreError("Invalid commerce task store key")
        if account_scope and not _COMMERCE_SCOPE_KEY.fullmatch(account_scope):
            raise LocalStoreError("Invalid commerce task account key")
        if selected_phase not in {"baseline", "readback"}:
            raise LocalStoreError("Invalid commerce task observation phase")
        ids: list[int] = []
        for value in observation_ids:
            try:
                observation_id = int(value)
            except (TypeError, ValueError) as exc:
                raise LocalStoreError("Invalid commerce task observation id") from exc
            if observation_id <= 0:
                raise LocalStoreError("Invalid commerce task observation id")
            if observation_id not in ids:
                ids.append(observation_id)
            if len(ids) > 500:
                raise LocalStoreError("Too many commerce task observations")

        self.initialize()
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                """
                SELECT * FROM commerce_task_runs
                WHERE run_id = ? AND store_key = ? AND account_key = ?
                """,
                (selected_run_id, scope, account_scope),
            ).fetchone()
            if run is None:
                raise LocalStoreError("Commerce task run does not exist in this scope")
            existing_ids = {
                int(row["observation_id"])
                for row in connection.execute(
                    """
                    SELECT observation_id FROM commerce_task_run_observations
                    WHERE run_id = ? AND phase = ?
                    """,
                    (selected_run_id, selected_phase),
                ).fetchall()
            }
            new_ids = [value for value in ids if value not in existing_ids]
            if not new_ids:
                connection.commit()
                return {"attached": 0, "total": len(existing_ids)}
            if str(run["status"]) in _COMMERCE_TASK_TERMINAL_STATUSES:
                raise LocalStoreError("A terminal commerce task run cannot accept new observations")
            placeholders = ",".join("?" for _ in new_ids)
            rows = connection.execute(
                f"""
                SELECT id, store_key, account_key, entity_key, captured_at_ms
                FROM metric_observations WHERE id IN ({placeholders})
                """,
                tuple(new_ids),
            ).fetchall()
            if len(rows) != len(new_ids):
                raise LocalStoreError("One or more commerce task observations do not exist")
            allowed_accounts = {"", account_scope} if account_scope else {""}
            for row in rows:
                if (
                    str(row["store_key"]) != scope
                    or str(row["entity_key"]) != str(run["entity_key"])
                    or str(row["account_key"]) not in allowed_accounts
                ):
                    raise LocalStoreError(
                        "Commerce task observations must match the run store, account and entity"
                    )
                captured_at = int(row["captured_at_ms"])
                if selected_phase == "baseline" and captured_at > int(run["started_at_ms"]):
                    raise LocalStoreError("Commerce task baseline must not be newer than its start")
                if selected_phase == "readback" and captured_at < int(run["due_at_ms"]):
                    raise LocalStoreError("Commerce task readback is earlier than its observation window")
            linked_at = datetime.now(timezone.utc).isoformat()
            attached = 0
            for observation_id in new_ids:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO commerce_task_run_observations (
                        run_id, phase, observation_id, linked_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (selected_run_id, selected_phase, observation_id, linked_at),
                )
                attached += max(0, int(cursor.rowcount))
            connection.commit()
            return {"attached": attached, "total": len(existing_ids) + attached}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def commerce_memory(
        self,
        *,
        store_key: str,
        account_key: str | None = None,
        entity_keys: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Return UI-safe metrics without ever aggregating unrelated ad accounts.

        Omitting ``account_key`` returns store-level observations only.  A
        supplied account includes store-level facts and that exact account.
        """

        scope = str(store_key or "").strip().lower()
        if not _COMMERCE_SCOPE_KEY.fullmatch(scope):
            return {"status": "unscoped", "history_days": 0, "observation_count": 0, "entities": {}}
        account_scope = str(account_key or "").strip().lower()
        if account_scope and not _COMMERCE_SCOPE_KEY.fullmatch(account_scope):
            return {"status": "unscoped", "history_days": 0, "observation_count": 0, "entities": {}}
        if account_key is not None and account_scope:
            account_clause = "account_key IN ('', ?)"
            account_parameters: tuple[Any, ...] = (account_scope,)
        else:
            account_clause = "account_key = ''"
            account_parameters = ()
        keys = list(dict.fromkeys(
            str(value or "").strip().lower()
            for value in entity_keys
            if _COMMERCE_ENTITY_KEY.fullmatch(str(value or "").strip().lower())
        ))[:100]
        self.initialize()
        connection = self.connect()
        try:
            overall = connection.execute(
                f"""
                SELECT COUNT(*) AS observation_count,
                       COUNT(DISTINCT entity_key) AS observed_entities,
                       COUNT(DISTINCT date(captured_at_ms / 1000 + 28800, 'unixepoch')) AS history_days,
                       MIN(captured_at_ms) AS first_seen_at_ms,
                       MAX(captured_at_ms) AS last_seen_at_ms
                FROM metric_observations
                WHERE store_key = ? AND {account_clause}
                """,
                (scope, *account_parameters),
            ).fetchone()
            entity_count = int(connection.execute(
                "SELECT COUNT(*) FROM commerce_entities WHERE store_key = ?",
                (scope,),
            ).fetchone()[0])
            relation_count = int(connection.execute(
                "SELECT COUNT(*) FROM commerce_relations WHERE store_key = ?",
                (scope,),
            ).fetchone()[0])
            entity_memory: dict[str, dict[str, Any]] = {}
            if keys:
                placeholders = ",".join("?" for _ in keys)
                parameters: tuple[Any, ...] = (scope, *keys, *account_parameters)
                summaries = connection.execute(
                    f"""
                    SELECT entity_key, COUNT(*) AS observation_count,
                           COUNT(DISTINCT metric_contract || '|' || context_key) AS metric_count,
                           COUNT(DISTINCT date(captured_at_ms / 1000 + 28800, 'unixepoch')) AS history_days,
                           MIN(captured_at_ms) AS first_seen_at_ms,
                           MAX(captured_at_ms) AS last_seen_at_ms
                    FROM metric_observations
                    WHERE store_key = ? AND entity_key IN ({placeholders})
                      AND {account_clause}
                    GROUP BY entity_key
                    """,
                    parameters,
                ).fetchall()
                for row in summaries:
                    entity_memory[str(row["entity_key"])] = {
                        "observation_count": int(row["observation_count"] or 0),
                        "metric_count": int(row["metric_count"] or 0),
                        "history_days": int(row["history_days"] or 0),
                        "first_seen_at_ms": int(row["first_seen_at_ms"] or 0),
                        "last_seen_at_ms": int(row["last_seen_at_ms"] or 0),
                        "latest_metrics": {},
                        "latest_metric_series": {},
                    }
                latest_rows = connection.execute(
                    f"""
                    SELECT entity_key, metric, metric_contract, account_key, context_key,
                           value, unit, channel, window_key, captured_at_ms,
                           source, page_type, quality_score
                    FROM metric_observations
                    WHERE store_key = ? AND entity_key IN ({placeholders})
                      AND {account_clause}
                    ORDER BY captured_at_ms DESC, id DESC
                    """,
                    parameters,
                ).fetchall()
                for row in latest_rows:
                    memory = entity_memory.get(str(row["entity_key"]))
                    series_key = f'{row["metric_contract"]}|{row["context_key"]}'
                    if memory is None or series_key in memory["latest_metric_series"]:
                        continue
                    point = {
                        "metric": str(row["metric"]),
                        "series_key": series_key,
                        "value": float(row["value"]),
                        "unit": str(row["unit"]),
                        "channel": str(row["channel"]),
                        "window_key": str(row["window_key"]),
                        "metric_contract": str(row["metric_contract"]),
                        "account_key": str(row["account_key"]),
                        "context_key": str(row["context_key"]),
                        "captured_at_ms": int(row["captured_at_ms"]),
                        "source": str(row["source"]),
                        "page_type": str(row["page_type"]),
                        "quality_score": int(row["quality_score"] or 0),
                    }
                    memory["latest_metric_series"][series_key] = point
                    memory["latest_metrics"].setdefault(
                        str(row["metric"]), point
                    )
        finally:
            connection.close()
        return {
            "status": "ready",
            "entity_count": entity_count,
            "relation_count": relation_count,
            "observation_count": int(overall["observation_count"] or 0),
            "observed_entities": int(overall["observed_entities"] or 0),
            "history_days": int(overall["history_days"] or 0),
            "first_seen_at_ms": int(overall["first_seen_at_ms"] or 0),
            "last_seen_at_ms": int(overall["last_seen_at_ms"] or 0),
            "entities": entity_memory,
            "account_key": account_scope if account_key is not None else None,
            "includes_store_level": True,
            "privacy": {
                "raw_platform_ids_persisted": False,
                "store_scoped": True,
                "name_only_join_allowed": False,
            },
            "safe_for_automatic_comparison": False,
        }

    def query_product_history(
        self,
        *,
        store_key: str,
        entity_key: str,
        account_key: str | None = None,
        metrics: Iterable[str] = (),
        start_ms: int = 0,
        end_ms: int | None = None,
        context_key: str = "",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return exact-entity observations without mixing ad accounts.

        ``account_key=None`` intentionally returns store-level observations
        only.  Supplying an account includes both store-level facts (for
        example inventory) and observations from that one confirmed account.
        """

        scope = str(store_key or "").strip().lower()
        entity = str(entity_key or "").strip().lower()
        if not _COMMERCE_SCOPE_KEY.fullmatch(scope) or not _COMMERCE_ENTITY_KEY.fullmatch(entity):
            return []
        account_scope = str(account_key or "").strip().lower()
        if account_scope and not _COMMERCE_SCOPE_KEY.fullmatch(account_scope):
            return []
        metric_values = list(dict.fromkeys(
            str(value or "").strip().lower()
            for value in metrics
            if re.fullmatch(r"[a-z][a-z0-9_]{1,47}", str(value or "").strip().lower())
        ))[:32]
        try:
            start_value = max(0, int(start_ms or 0))
            end_value = int(end_ms) if end_ms is not None else 9_223_372_036_854_775_807
            limit_value = max(1, min(2000, int(limit or 500)))
        except (TypeError, ValueError):
            return []
        if end_value <= start_value:
            return []
        context = str(context_key or "").strip().lower()
        if context and not re.fullmatch(r"[a-f0-9]{24}", context):
            return []

        clauses = [
            "store_key = ?",
            "entity_key = ?",
            "captured_at_ms >= ?",
            "captured_at_ms < ?",
        ]
        parameters: list[Any] = [scope, entity, start_value, end_value]
        if account_scope:
            clauses.append("account_key IN ('', ?)")
            parameters.append(account_scope)
        else:
            clauses.append("account_key = ''")
        if metric_values:
            placeholders = ",".join("?" for _ in metric_values)
            clauses.append(f"metric IN ({placeholders})")
            parameters.extend(metric_values)
        if context:
            clauses.append("context_key = ?")
            parameters.append(context)
        parameters.append(limit_value)

        self.initialize()
        connection = self.connect()
        try:
            rows = connection.execute(
                f"""
                SELECT id, snapshot_id, account_key, entity_key, metric, metric_contract,
                       value, unit, channel, window_key, context_key, captured_at_ms,
                       source, page_type, quality_score, attribution_scope, evidence_json
                FROM metric_observations
                WHERE {' AND '.join(clauses)}
                ORDER BY captured_at_ms DESC, id DESC
                LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
        finally:
            connection.close()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["evidence"] = json.loads(item.pop("evidence_json"))
            except (TypeError, json.JSONDecodeError):
                item["evidence"] = {}
                item.pop("evidence_json", None)
            result.append(item)
        return result

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
    "SnapshotLifecyclePolicy",
    "StorePaths",
    "default_store_root",
]
