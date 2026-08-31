import unittest

from douyin_commerce_graph import build_product_operating_graph, extract_commerce_observations


def row(source, page_type, record, *, captured=1_000, quality=90, account="acct_test"):
    return {
        "source": source,
        "page_type": page_type,
        "record": record,
        "captured_at_ms": captured,
        "quality_score": quality,
        "account_key": account,
        "table_index": 0,
        "row_index": 0,
    }


class DouyinCommerceGraphTests(unittest.TestCase):
    def test_exact_product_id_links_inventory_shelf_live_and_ads(self):
        records = [
            row("doudian", "products", {"商品ID": "product_v1_1234567890", "商品名称": "防晒衣", "可售库存": "80"}),
            row("doudian", "shelf", {"商品ID": "product_v1_1234567890", "商品名称": "防晒衣", "曝光人数": "1000", "点击人数": "100"}, captured=2_000),
            row("doudian", "live", {"商品ID": "product_v1_1234567890", "商品名称": "防晒衣", "商品点击人数": "30", "成交订单数": "6"}, captured=3_000),
            row("qianchuan", "campaigns", {"商品ID": "product_v1_1234567890", "商品名称": "防晒衣", "消耗": "¥300", "支付ROI": "2.1"}, captured=4_000),
        ]
        graph = build_product_operating_graph(records, store_key="store_a")
        self.assertEqual(1, graph["summary"]["products"])
        self.assertEqual(1, graph["summary"]["cross_channel_products"])
        product = graph["products"][0]
        self.assertEqual(["shelf", "inventory", "live", "ads"], product["channels"])
        self.assertEqual(80.0, product["metrics"]["inventory"]["stock"])
        self.assertEqual(300.0, product["metrics"]["ads"]["spend"])
        self.assertEqual("high", product["identity_confidence"])

    def test_same_name_with_different_ids_is_never_merged(self):
        graph = build_product_operating_graph([
            row("doudian", "products", {"商品ID": "product_v1_alpha1234", "商品名称": "同款 T 恤", "库存": "10"}),
            row("doudian", "products", {"商品ID": "product_v1_beta56789", "商品名称": "同款 T 恤", "库存": "20"}),
        ], store_key="store_a")
        self.assertEqual(2, graph["summary"]["products"])
        self.assertEqual(1, graph["summary"]["same_name_conflicts"])
        self.assertEqual("SAME_NAME_CONFLICT", graph["blockers"][-1]["code"])

    def test_name_only_row_is_reported_but_not_joined(self):
        graph = build_product_operating_graph([
            row("doudian", "shelf", {"商品名称": "没有 ID 的商品", "曝光人数": "900", "点击人数": "90"}),
        ], store_key="store_a")
        self.assertEqual(0, graph["summary"]["products"])
        self.assertEqual(1, graph["summary"]["unresolved_rows"])
        self.assertEqual("missing", graph["status"])
        self.assertFalse(graph["governance"]["name_only_join_allowed"])

    def test_zero_stock_blocks_scaling_even_when_roi_is_high(self):
        graph = build_product_operating_graph([
            row("doudian", "inventory", {"商品ID": "product_v1_stockzero", "商品名称": "爆款", "可售库存": "0"}),
            row("qianchuan", "campaigns", {"商品ID": "product_v1_stockzero", "商品名称": "爆款", "消耗": "500", "支付ROI": "5.0"}),
        ], store_key="store_a")
        product = graph["products"][0]
        self.assertEqual("inventory", product["decision"]["stage"])
        self.assertFalse(product["promotion_gate"]["scale_allowed"])
        self.assertIn("库存不足", product["promotion_gate"]["reasons"])

    def test_missing_metrics_remain_missing_instead_of_zero(self):
        graph = build_product_operating_graph([
            row("doudian", "products", {"商品ID": "product_v1_missing1", "商品名称": "新品"}),
            row("qianchuan", "campaigns", {"商品ID": "product_v1_missing1", "商品名称": "新品", "支付ROI": "--"}),
        ], store_key="store_a")
        product = graph["products"][0]
        self.assertNotIn("roi", product["metrics"].get("ads", {}))
        self.assertFalse(graph["governance"]["missing_values_default_to_zero"])

    def test_merchant_code_is_medium_confidence_and_cannot_scale(self):
        graph = build_product_operating_graph([
            row("doudian", "inventory", {"商家编码": "merchant_7788", "商品名称": "自编码商品", "库存": "99"}),
            row("qianchuan", "campaigns", {"商家编码": "merchant_7788", "商品名称": "自编码商品", "消耗": "200", "支付ROI": "3"}),
        ], store_key="store_a")
        product = graph["products"][0]
        self.assertEqual("medium", product["identity_confidence"])
        self.assertFalse(product["promotion_gate"]["scale_allowed"])
        self.assertIn("缺少平台商品 ID", product["promotion_gate"]["reasons"])

    def test_sku_inventory_joins_exact_same_row_product_identity(self):
        graph = build_product_operating_graph([
            row("doudian", "products", {"商品ID": "product_v1_productjoin", "SKU ID": "sku_v1_skujoin12345", "商品名称": "联名款"}),
            row("doudian", "inventory", {"SKU ID": "sku_v1_skujoin12345", "商品名称": "联名款", "总库存": "66"}, captured=2_000),
        ], store_key="store_a")

        self.assertEqual(1, graph["summary"]["products"])
        product = graph["products"][0]
        self.assertEqual("product_v1_productjoin", product["product_id"])
        self.assertEqual(66.0, product["metrics"]["inventory"]["stock"])
        self.assertEqual({"product_id", "sku_id"}, {item["kind"] for item in product["identity_aliases"]})

    def test_scale_requires_profit_refund_samples_and_exact_plan_mapping(self):
        graph = build_product_operating_graph([
            row("doudian", "inventory", {"商品ID": "product_v1_scalecheck", "商品名称": "安全款", "可售库存": "100", "保本ROI": "1.8"}, captured=3_000),
            row("doudian", "refunds", {"商品ID": "product_v1_scalecheck", "商品名称": "安全款", "退款率": "5", "成交订单数": "8"}, captured=3_000),
            row("qianchuan", "campaigns", {"商品ID": "product_v1_scalecheck", "计划ID": "plan_v1_scaleplan123", "商品名称": "安全款", "消耗": "300", "支付ROI": "2.4", "成交订单数": "8"}, captured=3_000),
        ], store_key="store_a")

        product = graph["products"][0]
        self.assertTrue(product["promotion_gate"]["scale_allowed"])
        self.assertEqual("scale", product["decision"]["stage"])

        incomplete = build_product_operating_graph([
            row("doudian", "inventory", {"商品ID": "product_v1_incomplete", "可售库存": "100"}),
            row("qianchuan", "campaigns", {"商品ID": "product_v1_incomplete", "消耗": "300", "支付ROI": "3", "成交订单数": "8"}),
        ], store_key="store_a")["products"][0]
        self.assertFalse(incomplete["promotion_gate"]["scale_allowed"])
        self.assertIn("缺少退款率", incomplete["promotion_gate"]["reasons"])
        self.assertIn("缺少保本 ROI", incomplete["promotion_gate"]["reasons"])
        self.assertIn("缺少精确计划 ID—商品映射", incomplete["promotion_gate"]["reasons"])

    def test_prefixed_plan_id_headers_preserve_exact_product_plan_mapping(self):
        for plan_header in ("推广计划 ID", "广告计划编号", "商品计划ID", "直播计划 ID"):
            with self.subTest(plan_header=plan_header):
                graph = build_product_operating_graph([
                    row("doudian", "inventory", {
                        "商品ID": "product_v1_prefixedplan", "可售库存": "100", "保本ROI": "1.8",
                    }, captured=3_000),
                    row("doudian", "refunds", {
                        "商品ID": "product_v1_prefixedplan", "退款率": "5", "成交订单数": "8",
                    }, captured=3_000),
                    row("qianchuan", "campaigns", {
                        "商品ID": "product_v1_prefixedplan", plan_header: "plan_v1_prefixed1001",
                        "消耗": "300", "支付ROI": "2.4", "成交订单数": "8",
                    }, captured=3_000),
                ], store_key="store_a")
                product = graph["products"][0]
                self.assertTrue(product["promotion_gate"]["scale_allowed"])
                self.assertEqual("scale", product["decision"]["stage"])

    def test_extracts_source_backed_observations_for_one_primary_entity(self):
        product_key = "product_v1_" + "a" * 26
        plan_key = "plan_v1_" + "b" * 26
        observations = extract_commerce_observations({
            "source": "qianchuan",
            "page_type": "campaigns",
            "captured_at": 1_800_000_000_000,
            "quality": {"score": 88},
            "tables": [{
                "headers": ["商品ID", "计划ID", "消耗", "支付ROI"],
                "rows": [[product_key, "pid_12345678", "¥200", "2.2"]],
                "entity_rows": [{"row_index": 0, "entities": [
                    {"entity_key": product_key, "entity_type": "douyin_product_id"},
                    {"entity_key": plan_key, "entity_type": "qianchuan_plan_id"},
                ]}],
            }],
        })

        self.assertEqual({"spend", "roi"}, {item["metric"] for item in observations})
        self.assertTrue(all(item["entity_key"] == product_key for item in observations))
        self.assertTrue(all(item["channel"] == "ads" for item in observations))
        self.assertTrue(all(item["window_key"] == "unknown" for item in observations))


if __name__ == "__main__":
    unittest.main()
