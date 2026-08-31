import unittest
from unittest.mock import patch

import http_receiver


class ProductTaskReadbackTests(unittest.TestCase):
    def test_product_task_collects_only_the_bound_product_metrics(self) -> None:
        graph = {
            "products": [
                {
                    "product_key": "bound-product-key",
                    "product_id": "product_v1_bound",
                    "sku_id": None,
                    "merchant_code": None,
                    "entity_ref": "product_id:product_v1_bound",
                    "metrics": {"ads": {"roi": 1.2, "spend": 200}, "inventory": {"stock": 50}},
                    "evidence": [
                        {"channel": "ads", "source": "qianchuan", "page_type": "campaigns", "captured_at_ms": 2_000},
                        {"channel": "inventory", "source": "doudian", "page_type": "inventory", "captured_at_ms": 1_500},
                    ],
                },
                {
                    "product_key": "unrelated-product-key",
                    "product_id": "product_v1_other",
                    "metrics": {"ads": {"roi": 9.9, "spend": 999}},
                    "evidence": [
                        {"channel": "ads", "source": "qianchuan", "page_type": "campaigns", "captured_at_ms": 3_000},
                    ],
                },
            ],
        }
        with patch.object(http_receiver, "load_agent_settings", return_value={"store_key": "store-a"}), \
             patch.object(http_receiver, "build_douyin_product_graph", return_value=graph):
            metrics, watermarks = http_receiver._collect_suggestion_metrics("store-a", {
                "kind": "douyin_product", "id": "bound-product-key", "id_source": "platform",
            })
        self.assertEqual(3, len(metrics))
        self.assertIn("qianchuan/campaigns/entity/bound-product-key/ROI", metrics)
        self.assertEqual(1.2, metrics["qianchuan/campaigns/entity/bound-product-key/ROI"])
        self.assertIn("doudian/inventory/entity/bound-product-key/库存", metrics)
        self.assertFalse(any("unrelated-product-key" in key for key in metrics))
        self.assertEqual({"qianchuan/campaigns": 2_000, "doudian/inventory": 1_500}, watermarks)

    def test_derived_product_name_hash_never_falls_back_to_page_level_metrics(self) -> None:
        with patch.object(http_receiver, "load_agent_settings", return_value={"store_key": "store-a"}), \
             patch.object(http_receiver, "build_douyin_product_graph") as graph_builder:
            metrics, watermarks = http_receiver._collect_suggestion_metrics("store-a", {
                "kind": "product", "id": "derived-name-hash", "id_source": "derived",
            })
        self.assertEqual({}, metrics)
        self.assertEqual({}, watermarks)
        graph_builder.assert_not_called()

    def test_task_contract_scope_keeps_subject_identity(self) -> None:
        scoped = http_receiver._suggestion_contract_scope({
            "task_contract": {
                "contract_version": 2,
                "contract_fingerprint": "f" * 32,
                "task_key": "k" * 24,
                "rule_id": "ops.product.review",
                "scope": {"store_key": "store-a", "account_key": "acct-a"},
                "subject": {"kind": "douyin_product", "id": "bound-product-key", "id_source": "platform", "name": "防晒衣"},
                "source_refs": [{"max_age_seconds": 1800}],
                "completion_contract": {
                    "required_source_keys": ["qianchuan/campaigns"],
                    "metric_keywords": ["ROI"],
                    "kind": "metric_rule",
                },
            },
        })
        self.assertEqual("bound-product-key", scoped["subject"]["id"])
        self.assertEqual("douyin_product", scoped["subject"]["kind"])
        self.assertEqual(["qianchuan/campaigns"], scoped["required_source_keys"])

    def test_same_name_products_get_different_stable_task_keys(self) -> None:
        def item(product_id, title="同名商品 · 库存不足"):
            return {
                "title": title,
                "owner": "商品运营",
                "action_params": {
                    "operation_type": "product_operating_review",
                    "target_ref": {"kind": "douyin_product", "id": product_id, "name": "同名商品"},
                },
            }

        with patch.object(http_receiver, "_task_result", return_value={"status": "pending"}):
            first = http_receiver._build_task_contract(
                item("product-key-a"), [], store_key="store-a", account_key="acct-a",
                business_date="2026-08-22", now_ms=1_800_000_000_000,
            )
            second = http_receiver._build_task_contract(
                item("product-key-b"), [], store_key="store-a", account_key="acct-a",
                business_date="2026-08-22", now_ms=1_800_000_000_000,
            )
            renamed = http_receiver._build_task_contract(
                item("product-key-a", "商品改名后 · 库存不足"), [], store_key="store-a", account_key="acct-a",
                business_date="2026-08-23", now_ms=1_800_000_000_000,
            )

        self.assertNotEqual(first["task_key"], second["task_key"])
        self.assertEqual(first["task_key"], renamed["task_key"])
        self.assertEqual(24, len(first["task_key"]))


if __name__ == "__main__":
    unittest.main()
