import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from local_store import (
    DEFAULT_MIGRATIONS,
    SCHEMA_VERSION,
    LocalStore,
    LocalStoreError,
    SchemaMigrationError,
    SnapshotImportError,
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

        def fail_v2(connection):
            connection.execute("CREATE TABLE should_not_exist (id INTEGER)")
            connection.execute("UPDATE preserved SET value = 'changed'")
            raise RuntimeError("simulated failure")

        with self.assertRaises(SchemaMigrationError) as raised:
            self.store.migrate(
                target_version=2,
                migrations={**DEFAULT_MIGRATIONS, 2: fail_v2},
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


if __name__ == "__main__":
    unittest.main()
