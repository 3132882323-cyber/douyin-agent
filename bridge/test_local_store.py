import json
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from local_store import (
    DEFAULT_MIGRATIONS,
    SCHEMA_VERSION,
    LocalStore,
    LocalStoreError,
    SchemaMigrationError,
    SnapshotImportError,
    SnapshotLifecyclePolicy,
)


class LocalStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "DianAgent"
        self.store = LocalStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def write_json(self, name, payload):
        path = Path(self.tmp.name) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_initialize_creates_separated_layout_and_schema(self):
        database = self.store.initialize()

        self.assertEqual(database, self.root / "data" / "shop.db")
        for name in ("data", "knowledge", "config", "backup", "logs", "app"):
            self.assertTrue((self.root / name).is_dir())
        self.assertEqual(self.store.get_schema_version(), SCHEMA_VERSION)

        with closing(self.store.connect()) as connection:
            meta = connection.execute(
                "SELECT schema_version FROM schema_meta WHERE singleton = 1"
            ).fetchone()
        self.assertEqual(meta["schema_version"], SCHEMA_VERSION)

    def test_import_preserves_existing_snapshot_shapes_and_utf8(self):
        object_file = self.write_json(
            "qianchuan/overview.json",
            {
                "source": "qianchuan",
                "page_type": "overview",
                "saved_at": "2026-08-02 10:00:00",
                "data": {"items": [{"标题": "夏装"}]},
            },
        )
        array_file = self.write_json("orders.json", [{"id": 1}, {"id": 2}])

        results = self.store.import_json_snapshots([object_file, array_file])
        rows = list(self.store.iter_snapshots())

        self.assertEqual(len(results), 2)
        self.assertEqual({row["snapshot_type"] for row in rows}, {"overview", "orders"})
        self.assertIn([{"id": 1}, {"id": 2}], [row["payload"] for row in rows])
        self.assertIn(
            {
                "source": "qianchuan",
                "page_type": "overview",
                "saved_at": "2026-08-02 10:00:00",
                "data": {"items": [{"标题": "夏装"}]},
            },
            [row["payload"] for row in rows],
        )

    def test_directory_import_and_unchanged_reimport_are_idempotent(self):
        directory = Path(self.tmp.name) / "legacy"
        snapshot = self.write_json("legacy/shop.json", {"shop": 123})
        self.write_json("legacy/nested/orders.json", [{"id": 1}])

        first = self.store.import_json_snapshots([directory])
        second = self.store.import_json_snapshot(snapshot)

        self.assertEqual(len(first), 2)
        self.assertTrue(second.duplicate)
        imported_shop = next(item for item in first if item.source_path == str(snapshot.resolve()))
        self.assertEqual(imported_shop.id, second.id)
        self.assertEqual(len(list(self.store.iter_snapshots())), 2)

    def test_existing_database_is_backed_up_before_migration(self):
        self.store.paths.create_directories()
        with closing(sqlite3.connect(self.store.paths.database)) as connection:
            connection.execute("CREATE TABLE legacy (value TEXT)")
            connection.execute("INSERT INTO legacy VALUES ('keep me')")
            connection.commit()

        backup = self.store.migrate()

        self.assertIsNotNone(backup)
        self.assertTrue(backup.is_file())
        with closing(sqlite3.connect(backup)) as connection:
            value = connection.execute("SELECT value FROM legacy").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(value, "keep me")
        self.assertEqual(version, 0)

    def test_failed_migration_rolls_back_schema_data_and_version(self):
        self.store.initialize()
        with closing(self.store.connect()) as connection:
            connection.execute("CREATE TABLE preserved (value TEXT)")
            connection.execute("INSERT INTO preserved VALUES ('original')")
            connection.commit()

        def fail_next(connection):
            connection.execute("CREATE TABLE should_not_exist (id INTEGER)")
            connection.execute("UPDATE preserved SET value = 'changed'")
            raise RuntimeError("simulated failure")

        with self.assertRaises(SchemaMigrationError) as raised:
            self.store.migrate(
                target_version=SCHEMA_VERSION + 1,
                migrations={**DEFAULT_MIGRATIONS, SCHEMA_VERSION + 1: fail_next},
            )

        self.assertIsNotNone(raised.exception.backup_path)
        with closing(self.store.connect()) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            value = connection.execute("SELECT value FROM preserved").fetchone()[0]
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'should_not_exist'"
            ).fetchone()
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertEqual(value, "original")
        self.assertIsNone(table)

    def test_historical_v1_migration_still_contains_snapshot_schema(self):
        self.store.migrate(target_version=1)

        with closing(self.store.connect()) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
            version = connection.execute("PRAGMA user_version").fetchone()[0]

        self.assertEqual(1, version)
        self.assertIn("snapshots", tables)
        self.assertIn("idx_snapshots_type_captured", indexes)

    def test_real_v1_snapshot_survives_v2_upgrade_with_backup(self):
        self.store.migrate(target_version=1)
        payload = json.dumps({"source": "doudian", "page_type": "products", "value": "保留"}, ensure_ascii=False)
        digest = "a" * 64
        with closing(self.store.connect()) as connection:
            connection.execute(
                """
                INSERT INTO snapshots (
                    source_path, snapshot_type, captured_at, imported_at,
                    content_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("memory://v1", "products", "2026-08-22T00:00:00Z", "2026-08-22T00:00:01Z", digest, payload),
            )
            connection.commit()

        backup = self.store.migrate(target_version=2)

        self.assertIsNotNone(backup)
        self.assertTrue(backup.is_file())
        with closing(self.store.connect()) as connection:
            stored = connection.execute("SELECT content_sha256, payload_json FROM snapshots").fetchone()
            commerce_tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'commerce_%'")}
        self.assertEqual(digest, stored["content_sha256"])
        self.assertEqual(payload, stored["payload_json"])
        self.assertIn("commerce_entities", commerce_tables)
        with closing(sqlite3.connect(backup)) as connection:
            self.assertEqual(1, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(digest, connection.execute("SELECT content_sha256 FROM snapshots").fetchone()[0])

    def test_real_v2_commerce_data_survives_current_upgrade(self):
        self.store.migrate(target_version=2)
        product_key = "product_v1_" + "0" * 26
        with closing(self.store.connect()) as connection:
            snapshot_id = connection.execute(
                """
                INSERT INTO snapshots (
                    source_path, snapshot_type, captured_at, imported_at,
                    content_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "memory://v2-commerce", "inventory", "2026-08-22T00:00:00Z",
                    "2026-08-22T00:00:01Z", "b" * 64, "{}",
                ),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO commerce_entities (
                    store_key, entity_key, entity_type, first_seen_at_ms,
                    last_seen_at_ms, last_source, last_page_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "store_a", product_key, "douyin_product_id", 1_800_000_000_000,
                    1_800_000_000_000, "doudian", "inventory",
                ),
            )
            observation_id = connection.execute(
                """
                INSERT INTO metric_observations (
                    snapshot_id, store_key, account_key, entity_key, metric,
                    metric_contract, value, unit, channel, window_key,
                    context_key, captured_at_ms, source, page_type, fingerprint
                ) VALUES (?, ?, '', ?, 'stock', 'stock:total_stock', 12, 'count',
                          'inventory', 'point_in_time', ?, ?, 'doudian', 'inventory', ?)
                """,
                (
                    snapshot_id, "store_a", product_key, "c" * 24,
                    1_800_000_000_000, "d" * 64,
                ),
            ).lastrowid
            connection.commit()

        backup = self.store.migrate()

        self.assertIsNotNone(backup)
        with closing(self.store.connect()) as connection:
            self.assertEqual(SCHEMA_VERSION, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(
                12.0,
                connection.execute(
                    "SELECT value FROM metric_observations WHERE id = ?", (observation_id,)
                ).fetchone()[0],
            )
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            index_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'idx_commerce_task_one_open_run'"
            ).fetchone()[0]
        self.assertIn("commerce_task_runs", tables)
        self.assertIn("commerce_task_run_observations", tables)
        self.assertIn("WHERE status IN", index_sql)
        with closing(sqlite3.connect(backup)) as connection:
            self.assertEqual(2, connection.execute("PRAGMA user_version").fetchone()[0])

    def test_v3_upgrade_adds_single_source_metadata_without_losing_snapshots(self):
        self.store.migrate(target_version=3)
        with closing(self.store.connect()) as connection:
            preserved_id = connection.execute(
                """
                INSERT INTO snapshots (
                    source_path, snapshot_type, captured_at, imported_at,
                    content_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "memory://v3-overview", "overview", "2026-08-22T00:00:00Z",
                    "2026-08-22T00:00:01Z", "f" * 64, "{}",
                ),
            ).lastrowid
            connection.commit()

        backup = self.store.migrate()

        self.assertIsNotNone(backup)
        with closing(self.store.connect()) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
            ids = [row[0] for row in connection.execute("SELECT id FROM snapshots")]
        self.assertIn("local_store_meta", tables)
        self.assertIn("idx_snapshots_source_latest", indexes)
        self.assertEqual([preserved_id], ids)
        with closing(sqlite3.connect(backup)) as connection:
            self.assertEqual(3, connection.execute("PRAGMA user_version").fetchone()[0])

    def test_bad_file_leaves_no_partial_import(self):
        valid = self.write_json("valid.json", {"ok": True})
        invalid = Path(self.tmp.name) / "invalid.json"
        invalid.write_text("{not-json", encoding="utf-8")

        with self.assertRaises(SnapshotImportError):
            self.store.import_json_snapshots([valid, invalid])

        self.assertEqual(list(self.store.iter_snapshots()), [])

    def test_insert_failure_rolls_back_earlier_rows_in_same_batch(self):
        first = self.write_json("first.json", {"id": 1})
        second = self.write_json("second.json", {"id": 2})
        real_insert = LocalStore._insert_snapshot
        call_count = 0

        def fail_on_second(connection, **values):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise sqlite3.OperationalError("simulated disk failure")
            return real_insert(connection, **values)

        with patch.object(LocalStore, "_insert_snapshot", side_effect=fail_on_second):
            with self.assertRaises(SnapshotImportError):
                self.store.import_json_snapshots([first, second])

        self.assertEqual(list(self.store.iter_snapshots()), [])

    def test_legacy_json_migration_runs_once_and_json_cannot_take_read_precedence(self):
        legacy = self.write_json(
            "legacy-once/overview.json",
            {"source": "doudian", "page_type": "overview", "value": "first"},
        )

        first = self.store.migrate_legacy_json_snapshots([legacy])
        legacy.write_text(
            json.dumps(
                {"source": "doudian", "page_type": "overview", "value": "changed-json"}
            ),
            encoding="utf-8",
        )
        second = self.store.migrate_legacy_json_snapshots([legacy])
        latest = self.store.latest_snapshot(source_path=legacy)
        status = self.store.legacy_json_migration_status()

        self.assertEqual(1, len(first))
        self.assertEqual([], second)
        self.assertEqual("first", latest["payload"]["value"])
        self.assertEqual("sqlite", latest["authoritative_source"])
        self.assertEqual("complete", status["status"])
        self.assertEqual("non_authoritative_mirror", status["json_compatibility"])
        self.assertEqual(1, status["discovered_files"])
        self.assertEqual(1, status["imported_files"])

    def test_legacy_migration_preserves_existing_sqlite_path(self):
        legacy = self.write_json(
            "legacy-existing/products.json",
            {"source": "doudian", "page_type": "products", "value": "stale-json"},
        )
        existing = self.store.persist_snapshot(
            {"source": "doudian", "page_type": "products", "value": "sqlite-current"},
            legacy,
        )

        imported = self.store.migrate_legacy_json_snapshots([legacy])
        latest = self.store.latest_snapshot(source_path=legacy)
        status = self.store.legacy_json_migration_status()

        self.assertEqual([], imported)
        self.assertEqual(existing.id, latest["id"])
        self.assertEqual("sqlite-current", latest["payload"]["value"])
        self.assertEqual(1, status["preserved_sqlite_paths"])

    def test_failed_legacy_migration_rolls_back_rows_and_completion_marker(self):
        valid = self.write_json("legacy-atomic/valid.json", {"value": "valid"})
        invalid = Path(self.tmp.name) / "legacy-atomic" / "invalid.json"
        invalid.write_text("{not-json", encoding="utf-8")

        with self.assertRaises(SnapshotImportError):
            self.store.migrate_legacy_json_snapshots([valid, invalid])

        self.assertEqual([], list(self.store.iter_snapshots()))
        self.assertEqual("pending", self.store.legacy_json_migration_status()["status"])

    def test_legacy_insert_failure_rolls_back_marker_and_entire_batch(self):
        first = self.write_json("legacy-insert/first.json", {"id": 1})
        second = self.write_json("legacy-insert/second.json", {"id": 2})
        real_insert = LocalStore._insert_snapshot
        call_count = 0

        def fail_on_second(connection, **values):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise sqlite3.OperationalError("simulated migration disk failure")
            return real_insert(connection, **values)

        with patch.object(LocalStore, "_insert_snapshot", side_effect=fail_on_second):
            with self.assertRaises(SnapshotImportError):
                self.store.migrate_legacy_json_snapshots([first, second])

        self.assertEqual([], list(self.store.iter_snapshots()))
        self.assertEqual("pending", self.store.legacy_json_migration_status()["status"])

    def test_authoritative_read_fails_closed_on_sqlite_payload_corruption(self):
        mirror = self.write_json(
            "integrity/overview.json",
            {"source": "doudian", "page_type": "overview", "value": "json-fallback"},
        )
        snapshot = self.store.persist_snapshot(
            {"source": "doudian", "page_type": "overview", "value": "sqlite"},
            mirror,
        )
        with closing(self.store.connect()) as connection:
            connection.execute(
                "UPDATE snapshots SET payload_json = ? WHERE id = ?",
                ('{"value":"tampered"}', snapshot.id),
            )
            connection.commit()

        with self.assertRaisesRegex(LocalStoreError, "refusing legacy JSON fallback"):
            self.store.latest_snapshot(source_path=mirror)

    def test_directory_integrity_failure_is_isolated_from_other_accounts(self):
        selected = self.root / "data" / "qianchuan_accounts" / "acct_selected" / "campaigns.json"
        unrelated = self.root / "data" / "qianchuan_accounts" / "acct_other" / "campaigns.json"
        good = self.store.persist_snapshot(
            {"source": "qianchuan", "page_type": "campaigns", "value": "selected"},
            selected,
        )
        bad = self.store.persist_snapshot(
            {"source": "qianchuan", "page_type": "campaigns", "value": "other"},
            unrelated,
        )
        with closing(self.store.connect()) as connection:
            connection.execute(
                "UPDATE snapshots SET payload_json = ? WHERE id = ?",
                ('{"value":"tampered"}', bad.id),
            )
            connection.commit()

        rows = list(self.store.iter_latest_snapshots(source_directory=selected.parent))

        self.assertEqual([good.id], [row["id"] for row in rows])
        self.assertEqual("selected", rows[0]["payload"]["value"])

    def test_directory_reads_latest_sqlite_rows_without_json_files(self):
        directory = self.root / "data" / "qianchuan_accounts" / "acct_scope"
        first_path = directory / "campaigns.json"
        second_path = directory / "overview.json"
        self.store.persist_snapshot(
            {"source": "qianchuan", "page_type": "campaigns", "value": 1},
            first_path,
        )
        self.store.persist_snapshot(
            {"source": "qianchuan", "page_type": "overview", "value": 2},
            second_path,
        )

        rows = list(self.store.iter_latest_snapshots(source_directory=directory))
        latest = self.store.latest_snapshot(source_directory=directory)

        self.assertEqual(["overview", "campaigns"], [row["snapshot_type"] for row in rows])
        self.assertEqual(2, latest["payload"]["value"])
        self.assertFalse(first_path.exists())
        self.assertFalse(second_path.exists())

    def test_latest_reads_decode_only_one_row_per_path_with_large_history(self):
        directory = self.root / "data" / "qianchuan_accounts" / "mixed_scope"
        first_path = directory / "campaigns.json"
        second_path = directory / "overview.json"
        self.store.initialize()
        values = []
        for index in range(5_000):
            source_path = first_path if index % 2 == 0 else second_path
            page_type = "campaigns" if index % 2 == 0 else "overview"
            account_key = "acct_a" if index % 2 == 0 else "acct_b"
            payload_json = json.dumps(
                {
                    "source": "qianchuan",
                    "page_type": page_type,
                    "sequence": index,
                    "data": {"account": {"key": account_key}},
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            values.append(
                (
                    str(source_path),
                    page_type,
                    str(index),
                    "2026-08-23T00:00:00+00:00",
                    hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                    payload_json,
                )
            )
        with closing(self.store.connect()) as connection:
            connection.executemany(
                """
                INSERT INTO snapshots (
                    source_path, snapshot_type, captured_at, imported_at,
                    content_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            connection.commit()

        with patch.object(
            self.store,
            "_decode_authoritative_snapshot",
            wraps=self.store._decode_authoritative_snapshot,
        ) as decode:
            latest_path = self.store.latest_snapshot(source_path=first_path)
            self.assertEqual(4_998, latest_path["payload"]["sequence"])
            self.assertLessEqual(decode.call_count, 2)

            decode.reset_mock()
            latest_directory = self.store.latest_snapshot(source_directory=directory)
            self.assertEqual(4_999, latest_directory["payload"]["sequence"])
            self.assertLessEqual(decode.call_count, 2)

            decode.reset_mock()
            rows = list(self.store.iter_latest_snapshots(source_directory=directory))
            self.assertEqual({4_998, 4_999}, {row["payload"]["sequence"] for row in rows})
            self.assertEqual(2, decode.call_count)

            decode.reset_mock()
            account_row = self.store.latest_snapshot(
                source_name="qianchuan",
                account_key="acct_a",
            )
            self.assertEqual(4_998, account_row["payload"]["sequence"])
            self.assertLessEqual(decode.call_count, 2)

    def test_backup_restore_rolls_back_authoritative_data_and_migration_marker(self):
        legacy = self.write_json(
            "rollback/overview.json",
            {"source": "doudian", "page_type": "overview", "value": "known-good"},
        )
        self.store.migrate_legacy_json_snapshots([legacy])
        backup = self.store.create_backup(label="single-source-known-good")
        self.store.persist_snapshot(
            {"source": "doudian", "page_type": "overview", "value": "later"},
            legacy,
        )

        self.store.restore_backup(backup)

        latest = self.store.latest_snapshot(source_path=legacy)
        self.assertEqual("known-good", latest["payload"]["value"])
        self.assertEqual("complete", self.store.legacy_json_migration_status()["status"])

    def test_persist_snapshot_accepts_in_memory_payload(self):
        result = self.store.persist_snapshot(
            {"source": "doudian", "page_type": "shelf", "data": {"gmv": 88}},
            "memory://doudian/shelf",
        )

        self.assertFalse(result.duplicate)
        self.assertEqual(result.snapshot_type, "shelf")
        self.assertEqual(list(self.store.iter_snapshots())[0]["payload"]["data"]["gmv"], 88)

    def test_restore_backup_validates_then_atomically_replaces_database(self):
        self.store.initialize()
        original = self.store.persist_snapshot({"value": "before"}, "memory://one")
        backup = self.store.create_backup(label="known-good")
        self.store.persist_snapshot({"value": "after"}, "memory://two")

        safety_backup = self.store.restore_backup(backup)

        self.assertIsNotNone(safety_backup)
        rows = list(self.store.iter_snapshots())
        self.assertEqual([row["id"] for row in rows], [original.id])
        status = self.store.status()
        self.assertEqual(status["schema"], SCHEMA_VERSION)
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["db_path"], str(self.store.paths.database))
        self.assertGreaterEqual(status["backup_count"], 2)

    def test_invalid_backup_does_not_replace_live_database(self):
        self.store.initialize()
        self.store.persist_snapshot({"value": "preserved"}, "memory://one")
        invalid = Path(self.tmp.name) / "broken.db"
        invalid.write_text("not sqlite", encoding="utf-8")

        with self.assertRaises(LocalStoreError):
            self.store.restore_backup(invalid)

        rows = list(self.store.iter_snapshots())
        self.assertEqual(rows[0]["payload"], {"value": "preserved"})

    def test_lifecycle_preview_is_read_only_and_distinguishes_capture_from_business_state(self):
        source = "memory://qianchuan/campaigns.json"
        for captured_at in (1000, 2000, 3000):
            self.store.persist_snapshot(
                {
                    "source": "qianchuan",
                    "page_type": "campaigns",
                    "saved_at": f"2026-08-20 00:00:{captured_at // 1000:02d}",
                    "timestamp": captured_at / 1000,
                    "data": {
                        "captured_at": captured_at,
                        "metrics": {"roi": 2.5},
                        "promotion_context": {
                            "evidence": {"captured_at_ms": captured_at},
                            "promotion_mode_evidence": {"captured_at_ms": captured_at},
                        },
                    },
                },
                source,
            )
        self.store.persist_snapshot(
            {
                "source": "qianchuan",
                "page_type": "campaigns",
                "saved_at": "2026-08-20 00:00:04",
                "timestamp": 4.0,
                "data": {
                    "captured_at": 4000,
                    "metrics": {"roi": 2.8},
                    "promotion_context": {
                        "evidence": {"captured_at_ms": 4000},
                        "promotion_mode_evidence": {"captured_at_ms": 4000},
                    },
                },
            },
            source,
        )
        before_rows = len(list(self.store.iter_snapshots()))
        before_backups = list(self.store.paths.backup.glob("shop-*.db"))
        policy = SnapshotLifecyclePolicy(
            full_fidelity_days=1,
            keep_latest_per_source=1,
            review_after_total_rows=100,
            review_after_source_rows=100,
            review_after_database_bytes=1024 * 1024,
            semantic_sample_rows_per_source=10,
        )

        preview = self.store.snapshot_lifecycle_preview(
            policy=policy,
            now=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )

        self.assertFalse(preview["mutates_data"])
        self.assertFalse(preview["automatic_cleanup_enabled"])
        self.assertFalse(preview["apply_contract"]["implemented"])
        self.assertEqual(4, preview["snapshot_rows"])
        self.assertEqual(4, preview["semantic_analysis"]["observation_count_preserved"])
        self.assertEqual(3, preview["archive_candidate_rows"])
        hotspot = preview["hotspots"][0]
        self.assertEqual("campaigns.json", hotspot["source_name"])
        self.assertEqual(2, hotspot["semantic_state_rows"])
        self.assertEqual(2, hotspot["semantic_repeat_rows"])
        self.assertEqual(2, hotspot["consecutive_semantic_repeat_rows"])
        self.assertTrue(hotspot["semantic_analysis_complete"])
        self.assertNotIn(str(self.root), json.dumps(preview))
        self.assertEqual(before_rows, len(list(self.store.iter_snapshots())))
        self.assertEqual(before_backups, list(self.store.paths.backup.glob("shop-*.db")))

    def test_lifecycle_preview_protects_metric_evidence_and_changes_fingerprint(self):
        source = "memory://qianchuan/protected.json"
        snapshots = [
            self.store.persist_snapshot({"sequence": value}, source)
            for value in range(4)
        ]
        product_key = "product_v1_" + "e" * 26
        self.store.persist_commerce_graph(
            store_key="store_a",
            account_key="account_a",
            entities=[{"entity_key": product_key, "entity_type": "douyin_product_id"}],
            observations=[{
                "entity_key": product_key,
                "metric": "roi",
                "metric_contract": "roi:pay_roi",
                "value": 2.4,
                "unit": "ratio",
                "channel": "ads",
                "window_key": "today",
                "captured_at_ms": 1_800_000_000_000,
            }],
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=1_800_000_000_000,
            snapshot_id=snapshots[0].id,
            identity_key_fingerprint="f" * 16,
        )
        with closing(self.store.connect()) as connection:
            observation_id = int(
                connection.execute(
                    "SELECT id FROM metric_observations WHERE snapshot_id = ?",
                    (snapshots[0].id,),
                ).fetchone()[0]
            )
        run = self.store.start_commerce_task_run(
            run_id="1" * 32,
            store_key="store_a",
            account_key="account_a",
            entity_key=product_key,
            task_key="2" * 24,
            subject_kind="douyin_product",
            rule_id="ops.product.roi",
            contract_fingerprint="3" * 32,
            business_date="2026-08-23",
            started_at_ms=1_800_000_001_000,
            due_at_ms=1_800_000_002_000,
        )
        self.store.attach_commerce_task_observations(
            run["run_id"],
            "baseline",
            [observation_id],
            store_key="store_a",
            account_key="account_a",
        )
        policy = SnapshotLifecyclePolicy(
            full_fidelity_days=1,
            keep_latest_per_source=1,
            semantic_sample_rows_per_source=10,
        )
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)

        preview = self.store.snapshot_lifecycle_preview(policy=policy, now=now)

        self.assertEqual(1, preview["metric_observation_protected_rows"])
        self.assertEqual(1, preview["task_evidence_protected_rows"])
        self.assertEqual(2, preview["archive_candidate_rows"])
        first_preview_id = preview["preview_id"]
        same_day = self.store.snapshot_lifecycle_preview(
            policy=policy,
            now=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(first_preview_id, same_day["preview_id"])
        self.store.persist_snapshot({"sequence": 5}, source)
        changed = self.store.snapshot_lifecycle_preview(policy=policy, now=now)
        self.assertNotEqual(first_preview_id, changed["preview_id"])

    def test_lifecycle_preview_does_not_create_a_missing_database(self):
        self.assertFalse(self.store.paths.database.exists())

        preview = self.store.snapshot_lifecycle_preview()

        self.assertEqual("missing", preview["status"])
        self.assertFalse(self.store.paths.database.exists())

    def test_lifecycle_preview_surfaces_integrity_errors_without_rewriting_them(self):
        snapshot = self.store.persist_snapshot(
            {"source": "qianchuan", "page_type": "campaigns", "value": 1},
            "memory://qianchuan/integrity.json",
        )
        with closing(self.store.connect()) as connection:
            connection.execute(
                "UPDATE snapshots SET payload_json = ? WHERE id = ?",
                ('{"value":2}', snapshot.id),
            )
            connection.commit()

        preview = self.store.snapshot_lifecycle_preview()

        self.assertEqual(1, preview["semantic_analysis"]["integrity_error_rows"])
        self.assertIn("snapshot_integrity_error", preview["review_reason_codes"])
        self.assertFalse(preview["hotspots"][0]["semantic_analysis_complete"])
        with closing(self.store.connect()) as connection:
            stored = connection.execute(
                "SELECT payload_json, content_sha256 FROM snapshots WHERE id = ?",
                (snapshot.id,),
            ).fetchone()
        self.assertEqual('{"value":2}', stored["payload_json"])
        self.assertEqual(snapshot.content_sha256, stored["content_sha256"])

    def test_lifecycle_policy_rejects_unbounded_or_ambiguous_values(self):
        with self.assertRaises(ValueError):
            SnapshotLifecyclePolicy(keep_latest_per_source=0)
        with self.assertRaises(ValueError):
            SnapshotLifecyclePolicy(semantic_sample_rows_per_source=True)

    def test_commerce_memory_is_idempotent_store_scoped_and_source_backed(self):
        snapshot = self.store.persist_snapshot(
            {"source": "doudian", "page_type": "products", "data": {"captured_at": 1_800_000_000_000}},
            "memory://doudian/products",
        )
        product_key = "product_v1_" + "a" * 26
        plan_key = "plan_v1_" + "b" * 26
        entities = [
            {"entity_key": product_key, "entity_type": "douyin_product_id", "display_name": "防晒衣", "confidence": "exact"},
            {"entity_key": plan_key, "entity_type": "qianchuan_plan_id", "display_name": "商品计划", "confidence": "exact"},
        ]
        relations = [{
            "relation_key": "c" * 24,
            "from_key": plan_key,
            "to_key": product_key,
            "relation": "promotes",
            "confidence": "exact_row",
        }]
        observations = [{
            "entity_key": product_key,
            "metric": "stock",
            "metric_contract": "stock:total_stock",
            "value": 88,
            "unit": "count",
            "channel": "inventory",
            "window_key": "point_in_time",
            "captured_at_ms": 1_800_000_000_000,
            "quality_score": 92,
            "evidence": {"table_index": 0, "row_index": 0},
        }]
        first = self.store.persist_commerce_graph(
            store_key="store_a",
            entities=entities,
            relations=relations,
            observations=observations,
            source="doudian",
            page_type="products",
            captured_at_ms=1_800_000_000_000,
            snapshot_id=snapshot.id,
            identity_key_fingerprint="d" * 16,
        )
        second = self.store.persist_commerce_graph(
            store_key="store_a",
            entities=entities,
            relations=relations,
            observations=observations,
            source="doudian",
            page_type="products",
            captured_at_ms=1_800_000_000_000,
            snapshot_id=snapshot.id,
            identity_key_fingerprint="d" * 16,
        )

        self.assertEqual(1, first["observations"])
        self.assertEqual(0, second["observations"])
        memory = self.store.commerce_memory(store_key="store_a", entity_keys=[product_key])
        self.assertEqual(2, memory["entity_count"])
        self.assertEqual(1, memory["relation_count"])
        self.assertEqual(1, memory["observation_count"])
        self.assertEqual(1, memory["entities"][product_key]["history_days"])
        self.assertEqual(0, self.store.commerce_memory(store_key="store_b")["observation_count"])
        self.assertFalse(memory["safe_for_automatic_comparison"])

    def test_commerce_memory_rejects_raw_ids_and_identity_secret_changes(self):
        snapshot = self.store.persist_snapshot({"value": "source"}, "memory://source")
        with self.assertRaises(LocalStoreError):
            self.store.persist_commerce_graph(
                store_key="store_a",
                entities=[{"entity_key": "987654321", "entity_type": "douyin_product_id"}],
                source="doudian",
                page_type="products",
                captured_at_ms=1_800_000_000_000,
                snapshot_id=snapshot.id,
                identity_key_fingerprint="e" * 16,
            )

        product_key = "product_v1_" + "f" * 26
        self.store.persist_commerce_graph(
            store_key="store_a",
            entities=[{"entity_key": product_key, "entity_type": "douyin_product_id"}],
            source="doudian",
            page_type="products",
            captured_at_ms=1_800_000_000_000,
            snapshot_id=snapshot.id,
            identity_key_fingerprint="e" * 16,
        )
        with self.assertRaises(LocalStoreError):
            self.store.persist_commerce_graph(
                store_key="store_a",
                entities=[{"entity_key": product_key, "entity_type": "douyin_product_id"}],
                source="doudian",
                page_type="products",
                captured_at_ms=1_800_000_000_001,
                snapshot_id=snapshot.id,
                identity_key_fingerprint="0" * 16,
            )

    def test_snapshot_bundle_rolls_back_snapshot_and_graph_together(self):
        product_key = "product_v1_" + "1" * 26
        missing_plan_key = "plan_v1_" + "2" * 26
        commerce = {
            "store_key": "store_a",
            "entities": [
                {"entity_key": product_key, "entity_type": "douyin_product_id"},
            ],
            "relations": [{
                "relation_key": "3" * 24,
                "from_key": missing_plan_key,
                "to_key": product_key,
                "relation": "promotes",
            }],
            "source": "doudian",
            "page_type": "products",
            "captured_at_ms": 1_800_000_000_000,
            "identity_key_fingerprint": "4" * 16,
        }

        with self.assertRaises(LocalStoreError):
            self.store.persist_snapshot_bundle(
                {"source": "doudian", "page_type": "products"},
                "memory://atomic-bundle",
                commerce=commerce,
            )

        connection = self.store.connect()
        try:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM commerce_entities").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM commerce_meta").fetchone()[0])
        finally:
            connection.close()

    def test_snapshot_bundle_replay_is_idempotent(self):
        product_key = "product_v1_" + "5" * 26
        payload = {"source": "doudian", "page_type": "inventory", "data": {"captured_at": 1_800_000_000_000}}
        commerce = {
            "store_key": "store_a",
            "entities": [{"entity_key": product_key, "entity_type": "douyin_product_id"}],
            "observations": [{
                "entity_key": product_key,
                "metric": "stock",
                "metric_contract": "stock:total_stock",
                "value": 12,
                "unit": "count",
                "channel": "inventory",
                "window_key": "point_in_time",
                "captured_at_ms": 1_800_000_000_000,
            }],
            "source": "doudian",
            "page_type": "inventory",
            "captured_at_ms": 1_800_000_000_000,
            "identity_key_fingerprint": "6" * 16,
        }

        first = self.store.persist_snapshot_bundle(payload, "memory://bundle", commerce=commerce)
        second = self.store.persist_snapshot_bundle(payload, "memory://bundle", commerce=commerce)

        self.assertFalse(first["snapshot"].duplicate)
        self.assertTrue(second["snapshot"].duplicate)
        self.assertEqual(1, first["commerce"]["observations"])
        self.assertEqual(0, second["commerce"]["observations"])
        self.assertEqual(1, len(list(self.store.iter_snapshots())))

    def test_task_run_start_and_transition_are_idempotent_with_one_open_run(self):
        product_key = "product_v1_" + "9" * 26
        snapshot = self.store.persist_snapshot({"kind": "product"}, "memory://task-product")
        self.store.persist_commerce_graph(
            store_key="store_a",
            entities=[{"entity_key": product_key, "entity_type": "douyin_product_id"}],
            source="doudian",
            page_type="products",
            captured_at_ms=1_800_000_000_000,
            snapshot_id=snapshot.id,
            identity_key_fingerprint="a" * 16,
        )
        arguments = {
            "store_key": "store_a",
            "account_key": "account_a",
            "entity_key": product_key,
            "task_key": "b" * 24,
            "subject_kind": "douyin_product",
            "rule_id": "ops.product.review",
            "contract_fingerprint": "c" * 32,
            "business_date": "2026-08-22",
            "started_at_ms": 1_800_000_001_000,
            "due_at_ms": 1_800_000_002_000,
            "contract": {"contract_version": 2, "contract_fingerprint": "c" * 32},
        }

        first = self.store.start_commerce_task_run(run_id="d" * 32, **arguments)
        replay = self.store.start_commerce_task_run(run_id="d" * 32, **arguments)

        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(first["run_id"], replay["run_id"])
        self.assertIsNone(
            self.store.get_commerce_task_run(
                first["run_id"], store_key="store_a", account_key="account_b"
            )
        )
        with self.assertRaises(LocalStoreError):
            self.store.start_commerce_task_run(run_id="e" * 32, **arguments)

        completed = self.store.transition_commerce_task_run(
            first["run_id"], "completed",
            store_key="store_a", account_key="account_a",
            completed_at_ms=1_800_000_003_000,
            verdict="effective", result={"accepted": True},
        )
        replayed_transition = self.store.transition_commerce_task_run(
            first["run_id"], "completed",
            store_key="store_a", account_key="account_a",
        )
        self.assertTrue(completed["transitioned"])
        self.assertFalse(replayed_transition["transitioned"])
        self.assertEqual({"accepted": True}, replayed_transition["result"])
        replacement = self.store.start_commerce_task_run(run_id="e" * 32, **arguments)
        self.assertTrue(replacement["created"])

    def test_task_observations_are_idempotent_and_never_cross_accounts(self):
        product_key = "product_v1_" + "a" * 26
        fingerprint = "f" * 16
        entity = [{"entity_key": product_key, "entity_type": "douyin_product_id"}]
        for account, value, captured_at in (
            ("", 25, 1_800_000_000_000),
            ("account_a", 2.4, 1_800_000_000_100),
            ("account_b", 0.8, 1_800_000_000_200),
            ("account_a", 2.8, 1_800_000_004_000),
        ):
            snapshot = self.store.persist_snapshot(
                {"account": account, "captured_at": captured_at},
                f"memory://task-observation/{account or 'store'}/{captured_at}",
            )
            is_store = not account
            self.store.persist_commerce_graph(
                store_key="store_a",
                account_key=account,
                entities=entity,
                observations=[{
                    "entity_key": product_key,
                    "metric": "stock" if is_store else "roi",
                    "metric_contract": "stock:total_stock" if is_store else "roi:pay_roi",
                    "value": value,
                    "unit": "count" if is_store else "ratio",
                    "channel": "inventory" if is_store else "ads",
                    "window_key": "point_in_time" if is_store else "today",
                    "captured_at_ms": captured_at,
                }],
                source="doudian" if is_store else "qianchuan",
                page_type="inventory" if is_store else "campaigns",
                captured_at_ms=captured_at,
                snapshot_id=snapshot.id,
                identity_key_fingerprint=fingerprint,
            )
        with closing(self.store.connect()) as connection:
            ids = {
                (str(row["account_key"]), int(row["captured_at_ms"])): int(row["id"])
                for row in connection.execute(
                    "SELECT id, account_key, captured_at_ms FROM metric_observations"
                )
            }
        run = self.store.start_commerce_task_run(
            run_id="1" * 32,
            store_key="store_a",
            account_key="account_a",
            entity_key=product_key,
            task_key="2" * 24,
            subject_kind="douyin_product",
            rule_id="ops.product.roi",
            contract_fingerprint="3" * 32,
            business_date="2026-08-22",
            started_at_ms=1_800_000_001_000,
            due_at_ms=1_800_000_003_000,
        )
        baseline_ids = [
            ids[("", 1_800_000_000_000)],
            ids[("account_a", 1_800_000_000_100)],
        ]
        first = self.store.attach_commerce_task_observations(
            run["run_id"], "baseline", baseline_ids,
            store_key="store_a", account_key="account_a",
        )
        replay = self.store.attach_commerce_task_observations(
            run["run_id"], "baseline", baseline_ids,
            store_key="store_a", account_key="account_a",
        )
        self.assertEqual({"attached": 2, "total": 2}, first)
        self.assertEqual({"attached": 0, "total": 2}, replay)
        with self.assertRaises(LocalStoreError):
            self.store.attach_commerce_task_observations(
                run["run_id"], "baseline", [ids[("account_b", 1_800_000_000_200)]],
                store_key="store_a", account_key="account_a",
            )
        readback = self.store.attach_commerce_task_observations(
            run["run_id"], "readback", [ids[("account_a", 1_800_000_004_000)]],
            store_key="store_a", account_key="account_a",
        )
        loaded = self.store.get_commerce_task_run(
            run["run_id"], store_key="store_a", account_key="account_a"
        )
        self.assertEqual({"attached": 1, "total": 1}, readback)
        self.assertEqual({"baseline": 2, "readback": 1}, loaded["observation_counts"])
        self.assertEqual(
            {"", "account_a"},
            {
                item["account_key"]
                for phase in loaded["observations"].values()
                for item in phase
            },
        )

    def test_product_history_never_mixes_qianchuan_accounts(self):
        product_key = "product_v1_" + "7" * 26
        entity = [{"entity_key": product_key, "entity_type": "douyin_product_id"}]
        fingerprint = "8" * 16
        stock_snapshot = self.store.persist_snapshot({"kind": "stock"}, "memory://stock")
        self.store.persist_commerce_graph(
            store_key="store_a",
            entities=entity,
            observations=[{
                "entity_key": product_key,
                "metric": "stock",
                "metric_contract": "stock:total_stock",
                "value": 25,
                "unit": "count",
                "channel": "inventory",
                "window_key": "point_in_time",
                "captured_at_ms": 1_800_000_000_000,
            }],
            source="doudian",
            page_type="inventory",
            captured_at_ms=1_800_000_000_000,
            snapshot_id=stock_snapshot.id,
            identity_key_fingerprint=fingerprint,
        )
        for account, value, offset in (("account_a", 2.4, 1), ("account_b", 0.8, 2)):
            snapshot = self.store.persist_snapshot({"kind": account}, f"memory://{account}")
            self.store.persist_commerce_graph(
                store_key="store_a",
                account_key=account,
                entities=entity,
                observations=[{
                    "entity_key": product_key,
                    "metric": "roi",
                    "metric_contract": "roi:pay_roi",
                    "value": value,
                    "unit": "ratio",
                    "channel": "ads",
                    "window_key": "today",
                    "captured_at_ms": 1_800_000_000_000 + offset,
                }],
                source="qianchuan",
                page_type="campaigns",
                captured_at_ms=1_800_000_000_000 + offset,
                snapshot_id=snapshot.id,
                identity_key_fingerprint=fingerprint,
            )
        second_contract_snapshot = self.store.persist_snapshot(
            {"kind": "account_a_click_roi"}, "memory://account_a_click_roi"
        )
        self.store.persist_commerce_graph(
            store_key="store_a",
            account_key="account_a",
            entities=entity,
            observations=[{
                "entity_key": product_key,
                "metric": "roi",
                "metric_contract": "roi:click_roi",
                "value": 1.7,
                "unit": "ratio",
                "channel": "ads",
                "window_key": "today",
                "captured_at_ms": 1_800_000_000_003,
            }],
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=1_800_000_000_003,
            snapshot_id=second_contract_snapshot.id,
            identity_key_fingerprint=fingerprint,
        )

        store_only = self.store.query_product_history(store_key="store_a", entity_key=product_key)
        account_a = self.store.query_product_history(
            store_key="store_a", entity_key=product_key, account_key="account_a"
        )
        store_memory = self.store.commerce_memory(
            store_key="store_a", entity_keys=[product_key]
        )
        memory_a = self.store.commerce_memory(
            store_key="store_a", account_key="account_a", entity_keys=[product_key]
        )
        memory_b = self.store.commerce_memory(
            store_key="store_a", account_key="account_b", entity_keys=[product_key]
        )

        self.assertEqual(["stock"], [item["metric"] for item in store_only])
        self.assertEqual({"stock", "roi"}, {item["metric"] for item in account_a})
        self.assertEqual(
            {1.7, 2.4}, {item["value"] for item in account_a if item["metric"] == "roi"}
        )
        self.assertEqual(1, store_memory["observation_count"])
        self.assertEqual(3, memory_a["observation_count"])
        self.assertEqual(2, memory_b["observation_count"])
        latest_a = memory_a["entities"][product_key]["latest_metric_series"]
        latest_b = memory_b["entities"][product_key]["latest_metric_series"]
        self.assertEqual(3, len(latest_a))
        self.assertEqual(2, len(latest_b))
        self.assertEqual(
            {1.7, 2.4},
            {item["value"] for item in latest_a.values() if item["metric"] == "roi"},
        )
        self.assertEqual(
            {0.8},
            {item["value"] for item in latest_b.values() if item["metric"] == "roi"},
        )
        self.assertEqual(
            1.7,
            memory_a["entities"][product_key]["latest_metrics"]["roi"]["value"],
        )


if __name__ == "__main__":
    unittest.main()
