import tempfile
import unittest
from pathlib import Path

from operator_memory import archive_operator_memory, list_operator_memory, upsert_operator_memory


class OperatorMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_scope_is_safe(self):
        result = list_operator_memory(self.data_dir, "shop_a", "account_1")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["scope"], {"store_key": "shop_a", "account_key": "account_1"})

    def test_upsert_updates_same_memory_and_isolates_scopes(self):
        first = upsert_operator_memory(self.data_dir, {
            "store_key": "shop_a",
            "account_key": "account_1",
            "type": "strategy",
            "title": "冷启动预算",
            "value": "先用小预算观察两小时",
            "evidence": "近7天冷启动 ROI 波动较大",
        })
        second = upsert_operator_memory(self.data_dir, {
            "store_key": "shop_a",
            "account_key": "account_1",
            "type": "strategy",
            "title": "冷启动预算",
            "value": "先用小预算观察三小时",
        })
        self.assertEqual(first["entry"]["id"], second["entry"]["id"])
        current = list_operator_memory(self.data_dir, "shop_a", "account_1")
        self.assertEqual(current["count"], 1)
        self.assertEqual(current["entries"][0]["value"], "先用小预算观察三小时")
        other = list_operator_memory(self.data_dir, "shop_b", "account_1")
        self.assertEqual(other["count"], 0)

    def test_archive_removes_entry_from_active_view(self):
        saved = upsert_operator_memory(self.data_dir, {
            "store_key": "shop_a",
            "account_key": "account_1",
            "type": "fact",
            "title": "库存红线",
            "value": "库存低于3件不继续放量",
        })
        archived = archive_operator_memory(self.data_dir, saved["entry"]["id"], "shop_a", "account_1")
        self.assertEqual(archived["entry"]["status"], "archived")
        self.assertEqual(list_operator_memory(self.data_dir, "shop_a", "account_1")["count"], 0)

    def test_rejects_raw_or_invalid_scope(self):
        with self.assertRaises(ValueError):
            upsert_operator_memory(self.data_dir, {
                "store_key": "shop/a",
                "title": "不应保存",
                "value": "测试",
            })
        with self.assertRaises(ValueError):
            upsert_operator_memory(self.data_dir, {
                "store_key": "shop_a",
                "title": "",
                "value": "测试",
            })


if __name__ == "__main__":
    unittest.main()
