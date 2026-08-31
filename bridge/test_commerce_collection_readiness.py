import time
import unittest
from unittest.mock import patch

import http_receiver


class CommerceCollectionReadinessTests(unittest.TestCase):
    def snapshot(self, *, schema=3, entities=None, relations=None, headers=None, captured_at=None):
        rows = [["商品 A", "10"]]
        return {
            "data": {
                "schema_version": schema,
                "commerce_contract_version": 1 if entities else 0,
                "captured_at": captured_at or int(time.time() * 1000),
                "quality": {"score": 90, "row_count": 1},
                "commerce_entities": entities or [],
                "commerce_relations": relations or [],
                "tables": [{"headers": headers or ["商品名称", "总库存"], "rows": rows}],
            }
        }

    def test_old_extractor_is_reported_as_recapture_not_missing_product(self):
        product = {"entity_key": "product_v1_" + "a" * 26, "entity_type": "douyin_product_id"}
        with patch.object(http_receiver, "load_data", return_value=self.snapshot(schema=2, entities=[product])):
            state = http_receiver._commerce_snapshot_contract_state(
                "doudian", ("products",), http_receiver.INVENTORY_ANALYSIS_STALE_SECONDS
            )

        self.assertEqual("recapture_required", state["status"])
        self.assertEqual(1, state["stable_entity_count"])

    def test_expired_snapshot_is_distinct_from_identity_missing(self):
        product = {"entity_key": "product_v1_" + "a" * 26, "entity_type": "douyin_product_id"}
        with patch.object(http_receiver, "load_data", return_value=self.snapshot(entities=[product], captured_at=1_000)):
            state = http_receiver._commerce_snapshot_contract_state(
                "doudian", ("products",), http_receiver.INVENTORY_ANALYSIS_STALE_SECONDS
            )

        self.assertEqual("stale", state["status"])

    def test_material_page_cannot_satisfy_qianchuan_product_plan_contract(self):
        material = {"entity_key": "material_v1_" + "b" * 26, "entity_type": "qianchuan_material_id"}
        with patch.object(http_receiver, "load_data", return_value=self.snapshot(entities=[material], headers=["素材ID", "视频消耗"])):
            state = http_receiver._commerce_snapshot_contract_state(
                "qianchuan", ("campaigns",), http_receiver.PLAN_CONSOLE_STALE_SECONDS
            )

        self.assertEqual("identity_missing", state["status"])
        self.assertEqual(0, state["plan_entity_count"])
        self.assertEqual(0, state["mapping_count"])

    def test_readiness_uses_three_page_targeted_scan_instead_of_full_scan(self):
        states = [
            {"status": "identity_missing", "captured_at_ms": 1, "stable_entity_count": 0},
            {"status": "missing", "captured_at_ms": 0, "has_inventory_metric": False},
        ]
        with patch.object(http_receiver, "load_agent_settings", return_value={"store_key": "store_a", "qianchuan_account_key": ""}), patch.object(
            http_receiver, "_commerce_snapshot_contract_state", side_effect=states
        ):
            readiness = http_receiver._commerce_collection_readiness({})

        self.assertEqual("product_data_required", readiness["status"])
        self.assertEqual(["products", "inventory", "shelf"], readiness["next_action"]["page_ids"])
        self.assertIn("1 分钟", readiness["next_action"]["label"])
        self.assertEqual("optional", readiness["steps"][-1]["status"])


if __name__ == "__main__":
    unittest.main()
