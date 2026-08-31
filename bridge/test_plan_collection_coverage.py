from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch


BRIDGE_DIR = str(Path(__file__).resolve().parent)
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

import http_receiver
from plan_collection_coverage import (
    build_plan_collection_receipt,
    build_scoped_plan_collection_receipts,
    snapshot_plan_coverage_evidence,
)


def _row(plan_id: str, mode: str = "standard") -> dict:
    return {"plan_id": plan_id, "promotion_mode": mode}


class PlanCollectionCoverageTests(unittest.TestCase):
    def test_receipt_reports_partial_collection_and_pagination_truncation(self) -> None:
        snapshot = {
            "source": "qianchuan",
            "page_type": "campaigns",
            "data": {
                "quality": {
                    "plan_collection": {"platform_total": 3},
                    "pages_scanned": 5,
                    "pagination_truncated": True,
                },
                "promotion_context": {"promotion_mode": "standard"},
            },
        }

        receipt = build_plan_collection_receipt([_row("plan-1"), _row("plan-2")], [snapshot])

        self.assertEqual(3, receipt["platform_total"])
        self.assertEqual(2, receipt["collected_rows"])
        self.assertEqual(67, receipt["platform_coverage"])
        self.assertEqual(2, receipt["stable_id_rows"])
        self.assertEqual(100, receipt["stable_id_coverage"])
        self.assertTrue(receipt["pagination_truncated"])
        self.assertFalse(receipt["coverage_complete"])
        self.assertEqual("partial", receipt["status"])
        self.assertIn("PAGINATION_TRUNCATED", {item["code"] for item in receipt["coverage_warnings"]})
        self.assertIn("COLLECTED_ROWS_BELOW_PLATFORM_TOTAL", {item["code"] for item in receipt["coverage_warnings"]})

    def test_visible_plan_total_can_confirm_complete_read_only_receipt(self) -> None:
        snapshot = {
            "source": "qianchuan",
            "page_type": "campaigns",
            "data": {
                "quality": {"pagination_truncated": False},
                "promotion_context": {"promotion_mode": "full_domain"},
                "tables": [{"headers": ["计划"], "rows": [["共 2 条计划"]]}],
            },
        }

        receipt = build_plan_collection_receipt(
            [_row("plan-1", "full_domain"), _row("plan-2", "full_domain")],
            [snapshot],
        )

        self.assertTrue(receipt["read_only"])
        self.assertTrue(receipt["coverage_complete"])
        self.assertTrue(receipt["safe_to_claim_complete"])
        self.assertEqual("complete", receipt["status"])
        self.assertEqual("平台显示 2 条计划", receipt["labels"]["platform_total"])
        self.assertEqual([], receipt["coverage_warnings"])

    def test_conflicting_total_scopes_fail_closed(self) -> None:
        snapshots = [
            {
                "source": "qianchuan",
                "page_type": "campaigns",
                "data": {"platform_total": 2, "promotion_context": {"promotion_mode": "standard"}},
            },
            {
                "source": "qianchuan",
                "page_type": "qianchuan_live",
                "data": {"platform_total": 3, "promotion_context": {"promotion_mode": "chengfang"}},
            },
        ]

        receipt = build_plan_collection_receipt([_row("plan-1"), _row("plan-2")], snapshots)

        self.assertIsNone(receipt["platform_total"])
        self.assertEqual([2, 3], receipt["observed_platform_totals"])
        self.assertTrue(receipt["platform_total_conflict"])
        self.assertFalse(receipt["safe_to_claim_complete"])
        self.assertEqual("inconsistent", receipt["status"])
        codes = {item["code"] for item in receipt["coverage_warnings"]}
        self.assertIn("PLATFORM_TOTAL_CONFLICT", codes)
        self.assertIn("MULTIPLE_TOTAL_SCOPES", codes)

    def test_missing_plan_id_and_mode_have_separate_coverage(self) -> None:
        snapshot = {"data": {"platform_total": 2}}

        receipt = build_plan_collection_receipt([_row("plan-1"), _row("", "unknown")], [snapshot])

        self.assertEqual(1, receipt["stable_id_rows"])
        self.assertEqual(50, receipt["stable_id_coverage"])
        self.assertEqual(1, receipt["mode_confirmed_rows"])
        self.assertEqual(50, receipt["mode_confirmed_coverage"])
        self.assertFalse(receipt["coverage_complete"])
        codes = {item["code"] for item in receipt["coverage_warnings"]}
        self.assertIn("STABLE_PLAN_ID_INCOMPLETE", codes)
        self.assertIn("PROMOTION_MODE_INCOMPLETE", codes)

    def test_duplicate_plan_ids_cannot_satisfy_platform_total_or_supervised_gate(self) -> None:
        rows = [
            {"plan_id": "plan-duplicate", "account_key": "acct-a", "promotion_mode": "standard", "plan_type": "product"},
            {"plan_id": "plan-duplicate", "account_key": "acct-a", "promotion_mode": "standard", "plan_type": "product"},
        ]
        snapshots = [{
            "source": "qianchuan",
            "page_type": "campaigns",
            "data": {
                "platform_total": 2,
                "account": {"key": "acct-a"},
                "promotion_context": {"promotion_mode": "standard"},
            },
        }]

        receipt = build_plan_collection_receipt(rows, snapshots)
        self.assertEqual(2, receipt["stable_id_rows"])
        self.assertEqual(1, receipt["unique_stable_id_rows"])
        self.assertEqual(1, receipt["duplicate_plan_id_rows"])
        self.assertFalse(receipt["coverage_complete"])
        self.assertEqual("inconsistent", receipt["status"])
        self.assertIn("DUPLICATE_PLAN_IDENTITIES", {item["code"] for item in receipt["coverage_warnings"]})

        scoped = build_scoped_plan_collection_receipts(rows, snapshots)[0]
        self.assertTrue(scoped["scope_identity_verified"])
        self.assertFalse(scoped["diagnosis_ready"])
        self.assertFalse(scoped["supervised_draft_ready"])

    def test_explicit_zero_total_confirms_empty_without_inventing_rows(self) -> None:
        snapshot = {"data": {"signals": ["当前筛选条件下共 0 条计划"]}}

        receipt = build_plan_collection_receipt([], [snapshot])

        self.assertEqual(0, receipt["platform_total"])
        self.assertEqual(100, receipt["platform_coverage"])
        self.assertEqual(100, receipt["stable_id_coverage"])
        self.assertEqual(100, receipt["mode_confirmed_coverage"])
        self.assertTrue(receipt["coverage_complete"])
        self.assertEqual("confirmed_empty", receipt["status"])

    def test_total_extraction_ignores_unrelated_business_numbers(self) -> None:
        evidence = snapshot_plan_coverage_evidence({
            "data": {
                "safe_metrics": {"成交订单": "88", "消耗": "1000"},
                "tables": [{"headers": ["计划名称"], "rows": [["夏季计划 2026"]]}],
            },
        })

        self.assertEqual([], evidence["platform_totals"])
        receipt = build_plan_collection_receipt([_row("plan-safe")], [{"data": {"page_text": "匿名计划名称"}}])
        self.assertEqual("平台计划总数未确认", receipt["labels"]["platform_total"])
        self.assertNotIn("匿名计划名称", str(receipt["labels"]))

    def test_scoped_receipts_do_not_compare_product_and_live_totals(self) -> None:
        rows = [
            {"plan_id": "product-1", "account_key": "acct-a", "promotion_mode": "standard", "plan_type": "product"},
            {"plan_id": "live-1", "account_key": "acct-a", "promotion_mode": "chengfang", "plan_type": "live"},
            {"plan_id": "live-2", "account_key": "acct-a", "promotion_mode": "chengfang", "plan_type": "live"},
        ]
        snapshots = [
            {
                "source": "qianchuan",
                "page_type": "campaigns",
                "data": {
                    "platform_total": 1,
                    "account": {"key": "acct-a"},
                    "promotion_context": {"promotion_mode": "standard"},
                },
            },
            {
                "source": "qianchuan",
                "page_type": "qianchuan_live",
                "data": {
                    "platform_total": 2,
                    "account": {"key": "acct-a"},
                    "promotion_context": {"promotion_mode": "chengfang"},
                },
            },
        ]

        receipts = build_scoped_plan_collection_receipts(rows, snapshots)

        self.assertEqual(2, len(receipts))
        self.assertEqual({"acct-a|standard|product", "acct-a|chengfang|live"}, {item["scope_key"] for item in receipts})
        self.assertTrue(all(item["safe_to_claim_complete"] for item in receipts))
        self.assertTrue(all(item["scope_identity_verified"] for item in receipts))
        self.assertTrue(all(item["diagnosis_ready"] for item in receipts))
        self.assertTrue(all(item["supervised_draft_ready"] for item in receipts))
        self.assertEqual({1, 2}, {item["platform_total"] for item in receipts})

    def test_scoped_receipt_keeps_unknown_scope_separate(self) -> None:
        receipts = build_scoped_plan_collection_receipts(
            [{"plan_id": "known-1", "account_key": "acct-a", "promotion_mode": "standard", "plan_type": "product"}],
            [{"source": "qianchuan", "page_type": "report", "data": {"platform_total": 9}}],
        )

        by_key = {item["scope_key"]: item for item in receipts}
        self.assertIn("acct-a|standard|product", by_key)
        self.assertIn("unknown-account|unknown|unknown", by_key)
        self.assertFalse(by_key["acct-a|standard|product"]["safe_to_claim_complete"])
        self.assertFalse(by_key["unknown-account|unknown|unknown"]["safe_to_claim_complete"])
        self.assertFalse(by_key["unknown-account|unknown|unknown"]["scope_identity_verified"])
        self.assertFalse(by_key["unknown-account|unknown|unknown"]["diagnosis_ready"])
        self.assertFalse(by_key["unknown-account|unknown|unknown"]["supervised_draft_ready"])


class PlanConsoleCoverageIntegrationTests(unittest.TestCase):
    def test_plan_console_adds_coverage_receipt_without_enabling_writes(self) -> None:
        now_ms = int(time.time() * 1000)
        record = {
            "source": "qianchuan",
            "page_type": "campaigns",
            "quality_score": 100,
            "captured_at_ms": now_ms,
            "account_key": "acct_coverage1234",
            "account_label": "覆盖测试账户",
            "promotion_context": {
                "promotion_mode": "standard",
                "account_scope": {"account_id": "acct_coverage1234"},
            },
            "record": {
                "计划ID": "plan-coverage-1",
                "计划名称": "覆盖测试计划",
                "投放状态": "投放中",
            },
        }
        snapshots = [{
            "source": "qianchuan",
            "page_type": "campaigns",
            "snapshot": {"data": {
                "platform_total": 1,
                "account": {"key": "acct_coverage1234"},
                "promotion_context": {"promotion_mode": "standard"},
            }},
        }]

        def table_records(source, _page_types):
            return [record] if source == "qianchuan" else []

        with patch.object(http_receiver, "_table_records", side_effect=table_records), patch.object(
            http_receiver, "_plan_collection_snapshots", return_value=snapshots
        ), patch.object(http_receiver, "build_plan_recommendations", return_value=[]):
            console = http_receiver.build_qianchuan_plan_console()

        receipt = console["collection_receipt"]
        self.assertEqual(1, receipt["platform_total"])
        self.assertEqual(1, receipt["collected_rows"])
        self.assertTrue(receipt["coverage_complete"])
        self.assertTrue(receipt["read_only"])
        self.assertEqual(1, len(console["collection_receipts"]))
        self.assertEqual("acct_coverage1234|standard|product", console["collection_receipts"][0]["scope_key"])
        self.assertTrue(console["collection_receipts"][0]["safe_to_claim_complete"])
        self.assertTrue(console["rows"][0]["read_only_diagnosis_ready"])
        self.assertTrue(console["rows"][0]["supervised_draft_ready"])
        self.assertEqual("review_supervised_draft", console["rows"][0]["automation_next_action"]["code"])
        self.assertEqual("review_supervised_draft", console["execution_preparation"]["scope_next_actions"][0]["code"])
        self.assertFalse(console["platform_write_enabled"])
        self.assertFalse(console["automatic_batch_submit"])

    def test_scoped_receipt_respects_plan_console_response_cap(self) -> None:
        now_ms = int(time.time() * 1000)
        records = [{
            "source": "qianchuan",
            "page_type": "campaigns",
            "quality_score": 100,
            "captured_at_ms": now_ms,
            "account_key": "acct_cap1234",
            "account_label": "上限测试账户",
            "promotion_context": {
                "promotion_mode": "standard",
                "account_scope": {"account_id": "acct_cap1234"},
            },
            "record": {
                "计划ID": f"plan-cap-{index:04d}",
                "计划名称": f"上限计划 {index:04d}",
                "投放状态": "投放中",
            },
        } for index in range(501)]
        snapshots = [{
            "source": "qianchuan",
            "page_type": "campaigns",
            "snapshot": {"data": {
                "platform_total": 501,
                "account": {"key": "acct_cap1234"},
                "promotion_context": {"promotion_mode": "standard"},
            }},
        }]

        def table_records(source, _page_types):
            return records if source == "qianchuan" else []

        with patch.object(http_receiver, "_table_records", side_effect=table_records), patch.object(
            http_receiver, "_plan_collection_snapshots", return_value=snapshots
        ), patch.object(http_receiver, "build_plan_recommendations", return_value=[]):
            console = http_receiver.build_qianchuan_plan_console()

        self.assertEqual(500, len(console["rows"]))
        self.assertTrue(console["collection_receipt"]["local_list_truncated"])
        scoped = console["collection_receipts"][0]
        self.assertEqual(500, scoped["collected_rows"])
        self.assertEqual(501, scoped["platform_total"])
        self.assertFalse(scoped["safe_to_claim_complete"])
        self.assertTrue(all(not row["supervised_draft_ready"] for row in console["rows"]))
        self.assertEqual(
            {"continue_scoped_collection"},
            {row["automation_next_action"]["code"] for row in console["rows"]},
        )

    def test_complete_but_stale_scope_has_refresh_next_step_not_collection_step(self) -> None:
        old_ms = int(time.time() * 1000) - (http_receiver.PLAN_CONSOLE_STALE_SECONDS + 60) * 1000
        record = {
            "source": "qianchuan",
            "page_type": "campaigns",
            "quality_score": 100,
            "captured_at_ms": old_ms,
            "account_key": "acct-stale-scope",
            "account_label": "过期范围账户",
            "promotion_context": {
                "promotion_mode": "standard",
                "account_scope": {"account_id": "acct-stale-scope"},
            },
            "record": {"计划ID": "plan-stale-scope", "计划名称": "过期范围计划", "投放状态": "投放中"},
        }
        snapshots = [{
            "source": "qianchuan",
            "page_type": "campaigns",
            "snapshot": {"data": {
                "platform_total": 1,
                "account": {"key": "acct-stale-scope"},
                "promotion_context": {"promotion_mode": "standard"},
            }},
        }]

        def table_records(source, _page_types):
            return [record] if source == "qianchuan" else []

        with patch.object(http_receiver, "_table_records", side_effect=table_records), patch.object(
            http_receiver, "_plan_collection_snapshots", return_value=snapshots
        ), patch.object(http_receiver, "build_plan_recommendations", return_value=[]):
            console = http_receiver.build_qianchuan_plan_console()

        row = console["rows"][0]
        self.assertTrue(console["collection_receipts"][0]["safe_to_claim_complete"])
        self.assertEqual("complete_but_stale", row["collection_gate_state"])
        self.assertEqual("refresh_verified_scope", row["automation_next_action"]["code"])
        self.assertEqual("refresh_verified_scope", console["execution_preparation"]["scope_next_actions"][0]["code"])
        self.assertFalse(row["read_only_diagnosis_ready"])
        self.assertFalse(row["supervised_draft_ready"])

    def test_supervised_gate_requires_one_exact_plan_identity_in_signed_scope(self) -> None:
        scope_key = "acct-exact|standard|product"
        action = {
            "target_ref": {"account_key": "acct-exact", "id": "plan-exact"},
            "promotion_context": {"promotion_mode": "standard", "account_scope": {"account_id": "acct-exact"}},
            "evidence_ref": {
                "collection_scope_required": True,
                "collection_scope_key": scope_key,
                "plan_type": "product",
            },
        }
        row = {
            "account_key": "acct-exact",
            "plan_id": "plan-exact",
            "promotion_mode": "standard",
            "plan_type": "product",
            "collection_scope_key": scope_key,
            "collection_gate_state": "ready",
            "supervised_draft_ready": True,
            "automation_blockers": [],
            "automation_next_action": {"code": "review_supervised_draft"},
        }

        ready = http_receiver._supervised_draft_collection_gate(action, {"rows": [row]})
        self.assertTrue(ready["ready"])
        self.assertEqual("ready", ready["state"])

        ambiguous = http_receiver._supervised_draft_collection_gate(action, {"rows": [row, dict(row)]})
        self.assertFalse(ambiguous["ready"])
        self.assertEqual("identity_mismatch", ambiguous["state"])
        self.assertEqual("refresh_exact_plan_identity", ambiguous["next_action"]["code"])


if __name__ == "__main__":
    unittest.main()
