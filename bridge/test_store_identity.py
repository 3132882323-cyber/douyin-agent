from __future__ import annotations

import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import http_receiver


def claim(kind: str, raw_id: str, source: str = "url_parameter") -> dict[str, str]:
    return {"kind": kind, "raw_id": raw_id, "evidence_source": source, "confidence": "high"}


class StoreIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_dir = http_receiver.DATA_DIR
        self.temp = tempfile.TemporaryDirectory()
        http_receiver.DATA_DIR = Path(self.temp.name) / "data"
        http_receiver.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with http_receiver._current_page_store_grant_lock:
            http_receiver._current_page_store_grants.clear()

    def tearDown(self) -> None:
        http_receiver.DATA_DIR = self.original_dir
        self.temp.cleanup()

    @contextmanager
    def commercial_scope_guard(self, audit_guard):
        """Exercise scope-lock ordering without requiring the private runtime."""
        controller = SimpleNamespace(guard_scope_change=audit_guard)
        with patch.object(http_receiver, "COMMERCIAL_RUNTIME_LOADED", True), patch.object(
            http_receiver,
            "ChengfangProductionController",
            controller,
        ):
            yield

    def save_doudian(self, shop_id: str, page_type: str, value: str = "1", quality_score: int = 80) -> dict:
        return http_receiver.save_data("doudian", {
            "page_type": page_type,
            "identity_claims": [claim("douyin_shop_id", shop_id)],
            "identity_status": "resolved_by_bridge",
            "quality": {"score": quality_score, "row_count": 1},
            "safe_metrics": {"曝光人数": value},
            "signals": ["商品主图存在不良暗示，请优化"] if page_type == "shelf" else [],
        })

    def current_overview_payload(
        self,
        shop_id: str,
        *,
        captured_at: int | None = None,
        identity_status: str = "resolved_by_bridge",
        claims: list[dict[str, str]] | None = None,
    ) -> dict:
        return {
            "page_type": "overview",
            "captured_at": captured_at if captured_at is not None else int(time.time() * 1000),
            "reason": "manual-current-page-simple-start",
            "identity_claims": claims if claims is not None else [claim("douyin_shop_id", shop_id)],
            "identity_status": identity_status,
            "quality": {"score": 80, "row_count": 1},
            "safe_metrics": {"orders": "3"},
        }

    def push_with_current_page_grant(self, source: str, payload: dict) -> dict:
        grant = http_receiver.issue_current_page_store_grant()
        return http_receiver.save_scan_page_once(
            source,
            payload,
            current_page_context={"current_page_token": grant["current_page_token"]},
        )

    def test_fresh_current_overview_auto_confirms_exact_store(self) -> None:
        result = self.push_with_current_page_grant(
            "doudian",
            self.current_overview_payload("shop-auto-1001"),
        )
        store_key = result["store"]["key"]
        self.assertTrue(result["accepted"])
        self.assertTrue(result["store_auto_confirmed"])
        self.assertEqual(store_key, result["selected_store_key"])
        self.assertEqual("", result["selected_account_key"])

        catalog = http_receiver.build_store_catalog()
        self.assertEqual(store_key, catalog["selected_store_key"])
        onboarding = http_receiver._load_onboarding_state()
        self.assertTrue(onboarding["scopes"][store_key]["store_confirmed_at"])

    def test_auto_confirmation_takes_audit_before_binding_without_nested_audit(self) -> None:
        store_key = self.save_doudian("shop-auto-lock-order", "overview")["data"]["store"]["key"]
        events: list[str] = []
        audit_depth = 0
        max_audit_depth = 0
        real_binding_lease = http_receiver._binding_execution_lease

        @contextmanager
        def audit_guard(*args, **kwargs):
            nonlocal audit_depth, max_audit_depth
            events.append("audit_enter")
            audit_depth += 1
            max_audit_depth = max(max_audit_depth, audit_depth)
            try:
                yield
            finally:
                audit_depth -= 1
                events.append("audit_exit")

        @contextmanager
        def binding_lease(*, hold_state_lock, validator=None):
            events.append("binding_enter")
            with real_binding_lease(
                hold_state_lock=hold_state_lock,
                validator=validator,
            ):
                yield
            events.append("binding_exit")

        with self.commercial_scope_guard(audit_guard), patch.object(
            http_receiver, "_binding_execution_lease", new=binding_lease
        ):
            selected = http_receiver._select_auto_confirmed_store_context(store_key)

        self.assertEqual(store_key, selected["selected_store_key"])
        self.assertEqual(["audit_enter", "binding_enter"], events[:2])
        self.assertEqual(1, max_audit_depth)
        self.assertEqual(1, events.count("audit_enter"))

    def test_relinking_an_active_pair_does_not_reenter_audit_file_lock(self) -> None:
        store_key = self.save_doudian("shop-idempotent-link", "overview")["data"]["store"]["key"]
        account_key = http_receiver.save_data("qianchuan", {
            "page_type": "overview",
            "identity_claims": [claim("qianchuan_advertiser_id", "advertiser-idempotent-link")],
            "identity_status": "resolved_by_bridge",
            "quality": {"score": 90},
        })["data"]["account"]["key"]
        http_receiver.link_store_account(store_key, account_key)
        depth = 0
        max_depth = 0
        calls = 0

        @contextmanager
        def audit_guard(*args, **kwargs):
            nonlocal depth, max_depth, calls
            calls += 1
            depth += 1
            max_depth = max(max_depth, depth)
            if depth > 1:
                raise AssertionError("production audit lock was re-entered")
            try:
                yield
            finally:
                depth -= 1

        with self.commercial_scope_guard(audit_guard):
            selected = http_receiver.link_store_account(store_key, account_key)

        self.assertEqual(store_key, selected["selected_store_key"])
        self.assertEqual(account_key, selected["selected_account_key"])
        self.assertEqual(1, calls)
        self.assertEqual(1, max_depth)

    def test_scope_setting_change_uses_audit_but_same_scope_update_does_not(self) -> None:
        store_key = self.save_doudian("shop-settings-scope", "overview")["data"]["store"]["key"]
        http_receiver.save_agent_settings({"store_key": store_key})
        reasons: list[str] = []

        @contextmanager
        def audit_guard(*args, **kwargs):
            reasons.append(str(kwargs.get("reason") or ""))
            yield

        with self.commercial_scope_guard(audit_guard):
            http_receiver.save_agent_settings({"store_key": store_key, "roi_target": 3.4})
            self.assertEqual([], reasons)
            http_receiver.save_agent_settings({"store_key": "", "qianchuan_account_key": ""})

        self.assertEqual(["settings_scope_changed"], reasons)
        self.assertEqual("", http_receiver.load_agent_settings()["store_key"])

    def test_historical_single_store_without_new_current_proof_is_not_selected(self) -> None:
        historical = self.save_doudian("shop-history-only", "overview")
        historical_key = historical["data"]["store"]["key"]
        self.assertEqual(1, http_receiver.build_store_catalog()["store_count"])
        self.assertEqual("", http_receiver.build_store_catalog()["selected_store_key"])

        result = self.push_with_current_page_grant("doudian", {
            **self.current_overview_payload("unused", claims=[]),
            "identity_status": "unresolved",
        })
        self.assertTrue(result["accepted"])
        self.assertNotIn("store_auto_confirmed", result)
        catalog = http_receiver.build_store_catalog()
        self.assertEqual("", catalog["selected_store_key"])
        self.assertEqual(historical_key, catalog["stores"][0]["key"])

    def test_conflicted_current_overview_is_quarantined_without_selection(self) -> None:
        result = self.push_with_current_page_grant("doudian", self.current_overview_payload(
            "unused",
            identity_status="conflict",
            claims=[
                claim("douyin_shop_id", "shop-conflict-a"),
                claim("douyin_shop_id", "shop-conflict-b"),
            ],
        ))
        self.assertFalse(result["accepted"])
        self.assertTrue(result["quarantined"])
        self.assertNotIn("store_auto_confirmed", result)
        self.assertEqual("", http_receiver.build_store_catalog()["selected_store_key"])

    def test_non_overview_qianchuan_and_stale_captures_never_auto_confirm(self) -> None:
        non_overview = self.push_with_current_page_grant("doudian", {
            **self.current_overview_payload("shop-orders-current"),
            "page_type": "orders",
        })
        background_overview = self.push_with_current_page_grant("doudian", {
            **self.current_overview_payload("shop-background-tab"),
            "reason": "manual",
        })
        qianchuan = self.push_with_current_page_grant("qianchuan", {
            "page_type": "overview",
            "captured_at": int(time.time() * 1000),
            "reason": "manual-current-page-simple-start",
            "identity_claims": [claim("qianchuan_advertiser_id", "advertiser-current")],
            "identity_status": "resolved_by_bridge",
            "quality": {"score": 90},
        })
        stale = self.push_with_current_page_grant("doudian", self.current_overview_payload(
            "shop-stale-current",
            captured_at=int(time.time() * 1000) - http_receiver.STALE_SECONDS * 1000,
        ))
        for result in (non_overview, background_overview, qianchuan, stale):
            self.assertTrue(result["accepted"])
            self.assertNotIn("store_auto_confirmed", result)
        self.assertEqual("", http_receiver.build_store_catalog()["selected_store_key"])

    def test_auto_store_switch_uses_scope_boundary_and_clears_old_account(self) -> None:
        store_a = self.save_doudian("shop-switch-a", "overview")["data"]["store"]["key"]
        account_a = http_receiver.save_data("qianchuan", {
            "page_type": "overview",
            "identity_claims": [claim("qianchuan_advertiser_id", "advertiser-switch-a")],
            "identity_status": "resolved_by_bridge",
            "quality": {"score": 90},
        })["data"]["account"]["key"]
        http_receiver.link_store_account(store_a, account_a)
        self.assertEqual(account_a, http_receiver.build_store_catalog()["selected_account_key"])

        with patch.object(
            http_receiver,
            "_prepare_execution_scope_change",
            wraps=http_receiver._prepare_execution_scope_change,
        ) as prepare_scope_change:
            result = self.push_with_current_page_grant(
                "doudian",
                self.current_overview_payload("shop-switch-b"),
            )

        store_b = result["store"]["key"]
        self.assertNotEqual(store_a, store_b)
        self.assertEqual(store_b, result["selected_store_key"])
        self.assertEqual("", result["selected_account_key"])
        self.assertEqual("", http_receiver.build_store_catalog()["selected_account_key"])
        self.assertIn(
            "selected_store_or_account_changed",
            [str(call.args[0]) for call in prepare_scope_change.call_args_list],
        )
        onboarding = http_receiver._load_onboarding_state()
        self.assertTrue(onboarding["scopes"][store_b]["store_confirmed_at"])

    def test_failed_snapshot_persistence_does_not_auto_confirm(self) -> None:
        historical = self.save_doudian("shop-persistence-failure", "overview")
        store_key = historical["data"]["store"]["key"]
        with patch.object(
            http_receiver.LocalStore,
            "persist_snapshot_bundle",
            side_effect=http_receiver.LocalStoreError("simulated persistence failure"),
        ):
            with self.assertRaises(http_receiver.LocalStoreError):
                self.push_with_current_page_grant(
                    "doudian",
                    self.current_overview_payload("shop-persistence-failure"),
                )
        catalog = http_receiver.build_store_catalog()
        self.assertEqual("", catalog["selected_store_key"])
        self.assertEqual(store_key, catalog["stores"][0]["key"])

    def test_grant_is_one_time_and_missing_or_future_capture_time_cannot_confirm(self) -> None:
        grant = http_receiver.issue_current_page_store_grant()
        token_context = {"current_page_token": grant["current_page_token"]}
        first = http_receiver.save_scan_page_once(
            "doudian",
            {**self.current_overview_payload("shop-one-time"), "page_type": "orders"},
            current_page_context=token_context,
        )
        replay = http_receiver.save_scan_page_once(
            "doudian",
            self.current_overview_payload("shop-one-time"),
            current_page_context=token_context,
        )
        missing_time = self.current_overview_payload("shop-missing-time")
        missing_time.pop("captured_at")
        missing = self.push_with_current_page_grant("doudian", missing_time)
        future = self.push_with_current_page_grant(
            "doudian",
            self.current_overview_payload(
                "shop-future-time",
                captured_at=int(time.time() * 1000) + 60_000,
            ),
        )
        for result in (first, replay, missing, future):
            self.assertTrue(result["accepted"])
            self.assertNotIn("store_auto_confirmed", result)
        self.assertEqual("", http_receiver.build_store_catalog()["selected_store_key"])

    def test_onboarding_write_failure_rolls_back_store_and_account_selection(self) -> None:
        store_a = self.save_doudian("shop-atomic-a", "overview")["data"]["store"]["key"]
        account_a = http_receiver.save_data("qianchuan", {
            "page_type": "overview",
            "identity_claims": [claim("qianchuan_advertiser_id", "advertiser-atomic-a")],
            "identity_status": "resolved_by_bridge",
            "quality": {"score": 90},
        })["data"]["account"]["key"]
        http_receiver.link_store_account(store_a, account_a)
        store_b = http_receiver._local_identity_key("douyin_shop_id", "shop-atomic-b")

        with patch.object(
            http_receiver,
            "_confirm_onboarding_store",
            side_effect=OSError("simulated onboarding persistence failure"),
        ):
            with self.assertRaises(OSError):
                self.push_with_current_page_grant(
                    "doudian",
                    self.current_overview_payload("shop-atomic-b"),
                )

        catalog = http_receiver.build_store_catalog()
        self.assertEqual(store_a, catalog["selected_store_key"])
        self.assertEqual(account_a, catalog["selected_account_key"])
        onboarding = http_receiver._load_onboarding_state()
        self.assertNotIn(store_b, onboarding.get("scopes", {}))

    def test_prepared_auto_store_transaction_recovers_previous_scope_after_restart(self) -> None:
        store_a = self.save_doudian("shop-recovery-a", "overview")["data"]["store"]["key"]
        store_b = self.save_doudian("shop-recovery-b", "overview")["data"]["store"]["key"]
        http_receiver.select_store_context(store_a)
        previous_settings = http_receiver.load_agent_settings()
        previous_onboarding = http_receiver._load_onboarding_state()
        journal = {
            "schema_version": 1,
            "phase": "prepared",
            "prepared_at_ms": int(time.time() * 1000),
            "store_key": store_b,
            "previous": {
                "settings": previous_settings,
                "onboarding": previous_onboarding,
                "settings_existed": True,
                "onboarding_existed": True,
            },
        }
        http_receiver._atomic_json_write(http_receiver._auto_store_context_transaction_path(), journal)
        http_receiver._atomic_json_write(
            http_receiver._settings_path(),
            {**previous_settings, "store_key": store_b, "qianchuan_account_key": ""},
        )
        http_receiver._atomic_json_write(
            http_receiver._onboarding_state_path(),
            {
                **previous_onboarding,
                "scopes": {
                    **previous_onboarding.get("scopes", {}),
                    store_b: {"store_confirmed_at": "2026-08-28 17:00:00"},
                },
            },
        )

        recovered = http_receiver.build_store_catalog()
        self.assertEqual(store_a, recovered["selected_store_key"])
        self.assertFalse(http_receiver._auto_store_context_transaction_path().exists())
        self.assertNotIn(store_b, http_receiver._load_onboarding_state().get("scopes", {}))

    def test_pure_doudian_identity_and_first_value_do_not_require_qianchuan(self) -> None:
        overview = self.save_doudian("shop-1001", "overview", "10")
        shelf = self.save_doudian("shop-1001", "shelf", "20")
        store_key = overview["data"]["store"]["key"]
        self.assertEqual(store_key, shelf["data"]["store"]["key"])
        self.assertTrue(store_key.startswith("store_v1_"))
        self.assertNotIn("shop-1001", json.dumps(overview, ensure_ascii=False))
        self.assertFalse(any("shop-1001" in path.read_text(encoding="utf-8") for path in http_receiver.DATA_DIR.rglob("*.json")))
        database = http_receiver._local_store().paths.database
        self.assertNotIn(b"shop-1001", database.read_bytes())

        catalog = http_receiver.select_store_context(store_key)
        self.assertEqual(catalog["selected_store_key"], store_key)
        self.assertEqual(catalog["selected_account_key"], "")
        self.assertEqual(catalog["stores"][0]["state"], "doudian_ready")

        first_task = {"id": "f" * 16, "status": "todo", "title": "优化商品主图", "action": "替换主图", "evidence": "货架页存在风险提示", "acceptance": "风险提示消失"}
        with patch.object(http_receiver, "build_ops_manager", return_value={"all_tasks": [first_task]}), patch.object(
            http_receiver, "build_scan_receipt", return_value={"summary": {"success": 2}, "first_value_ready": True}
        ):
            discovered = http_receiver.build_onboarding_status()
            self.assertFalse(next(item for item in discovered["steps"] if item["id"] == "sync")["complete"])
            self.assertTrue(discovered["discovered"]["out_of_order"])

            for page_type in ("overview", "orders", "products", "shelf"):
                self.save_doudian("shop-1001", page_type, "30")
            state = http_receiver._load_onboarding_state()
            state["scopes"][store_key]["first_task_viewed_at"] = "2026-08-04 10:00:00"
            http_receiver._atomic_json_write(http_receiver._onboarding_state_path(), state)
            status = http_receiver.build_onboarding_status(state=state)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["missing_data"], [])
        self.assertEqual(status["optional_enhancements"][0]["id"], "qianchuan_overview")
        with patch.object(http_receiver, "build_ops_manager", return_value={"all_tasks": [first_task]}), patch.object(
            http_receiver, "build_automation_readiness", return_value={"items": [], "summary": {}}
        ):
            guide = http_receiver.build_connection_guide()
        self.assertEqual(guide["level"], "L2")
        self.assertEqual(guide["automation"]["mode"], "off")
        self.assertTrue(guide["next_upgrade"]["optional"])

        context = http_receiver.build_operation_context(
            catalog=http_receiver.build_store_catalog(),
            receipt={"store_key": store_key, "finished_at": 0, "analysis_ready": False, "summary": {"coverage_rate": 40}, "warnings": []},
        )
        self.assertTrue(context["analysis_allowed"])
        self.assertFalse(context["execution_review_allowed"])

    def test_unlinked_qianchuan_account_requires_explicit_manual_link(self) -> None:
        store_key = self.save_doudian("shop-2001", "overview")["data"]["store"]["key"]
        account_snapshot = http_receiver.save_data("qianchuan", {
            "page_type": "overview",
            "identity_claims": [claim("qianchuan_advertiser_id", "adv-9001")],
            "identity_status": "resolved_by_bridge",
            "quality": {"score": 90},
        })
        account_key = account_snapshot["data"]["account"]["key"]
        http_receiver.select_store_context(store_key)
        before = http_receiver.build_store_catalog()
        self.assertTrue(before["link_required"])
        self.assertEqual(before["stores"][0]["account_keys"], [])
        self.assertEqual(before["unlinked_accounts"][0]["key"], account_key)

        linked = http_receiver.link_store_account(store_key, account_key)
        self.assertEqual(linked["selected_account_key"], account_key)
        self.assertEqual(linked["stores"][0]["account_keys"], [account_key])
        reread = http_receiver.save_data("qianchuan", {
            "page_type": "campaigns",
            "identity_claims": [claim("qianchuan_advertiser_id", "adv-9001")],
            "identity_status": "resolved_by_bridge",
            "quality": {"score": 90},
        }, trusted_origin="official_api_oauth_client")
        self.assertEqual(reread["data"]["store"]["key"], store_key)

    def test_connection_guide_separates_connected_level_from_current_data_readiness(self) -> None:
        catalog = {
            "store_count": 1,
            "selected_store_key": "store_v1_test",
            "selected_account_key": "adacct_v1_test",
            "stores": [{
                "key": "store_v1_test",
                "label": "店铺 TEST",
                "qianchuan_page_count": 2,
                "updated_at": 1,
            }],
        }
        onboarding = {
            "status": "completed",
            "store_confirmed": True,
            "steps": [{"id": "sync", "complete": True}],
            "current_step": {},
        }
        operation_context = {
            "execution_review_allowed": False,
            "core_data": {
                "status": "stale",
                "stale_types": ["orders", "overview"],
                "missing_types": ["shelf"],
            },
            "freshness": {"fresh": False},
        }
        with patch.object(http_receiver, "build_store_catalog", return_value=catalog), patch.object(
            http_receiver, "build_onboarding_status", return_value=onboarding
        ), patch.object(
            http_receiver, "build_operation_context", return_value=operation_context
        ), patch.object(
            http_receiver, "build_automation_readiness", return_value={"items": [], "summary": {}}
        ):
            guide = http_receiver.build_connection_guide()

        self.assertEqual("L3", guide["level"])
        self.assertEqual("投放已连接", guide["level_label"])
        self.assertEqual("refresh_core_data", guide["next_upgrade"]["id"])
        self.assertEqual(["orders", "overview", "shelf"], guide["next_upgrade"]["page_ids"])
        self.assertEqual("refresh_required", guide["operational"]["state"])
        self.assertFalse(guide["operational"]["core_data_fresh"])
        self.assertFalse(guide["operational"]["execution_review_ready"])

    def test_low_quality_page_is_discovered_but_does_not_unlock_l2(self) -> None:
        store_key = self.save_doudian("shop-quality", "overview")["data"]["store"]["key"]
        http_receiver.select_store_context(store_key)
        for page_type in ("overview", "orders", "products"):
            self.save_doudian("shop-quality", page_type)
        self.save_doudian("shop-quality", "shelf", quality_score=40)
        with patch.object(http_receiver, "build_ops_manager", return_value={"all_tasks": []}):
            status = http_receiver.build_onboarding_status()
        self.assertFalse(next(item for item in status["steps"] if item["id"] == "sync")["complete"])
        self.assertEqual(status["discovered"]["formal_snapshot_count"], 4)
        self.assertEqual(status["discovered"]["usable_snapshot_count"], 3)
        self.assertIn("doudian_shelf", [item["id"] for item in status["missing_data"]])

    def test_cross_store_snapshots_tasks_and_reports_are_isolated(self) -> None:
        store_a = self.save_doudian("shop-A001", "shelf", "10")["data"]["store"]["key"]
        store_b = self.save_doudian("shop-B002", "shelf", "99")["data"]["store"]["key"]
        http_receiver.select_store_context(store_a)
        self.assertEqual(http_receiver.load_data("doudian", "shelf")["data"]["safe_metrics"]["曝光人数"], "10")
        http_receiver.update_task_state("a" * 16, "doing", store_key=store_a)
        report_a = http_receiver.generate_daily_report("2026-08-04")["path"]

        http_receiver.select_store_context(store_b)
        self.assertEqual(http_receiver.load_data("doudian", "shelf")["data"]["safe_metrics"]["曝光人数"], "99")
        self.assertEqual(http_receiver.load_task_states(), {})
        http_receiver.update_task_state("b" * 16, "doing", store_key=store_b)
        report_b = http_receiver.generate_daily_report("2026-08-04")["path"]
        self.assertNotEqual(report_a, report_b)
        self.assertIn(store_a, report_a)
        self.assertIn(store_b, report_b)

    def test_no_identity_data_remains_unscoped_and_does_not_accumulate_value(self) -> None:
        saved = http_receiver.save_data("doudian", {"page_type": "overview", "quality": {"score": 80}, "safe_metrics": {"订单量": "3"}})
        self.assertEqual(saved["data"]["identity_resolution"], "unresolved")
        self.assertEqual(http_receiver.build_store_catalog()["store_count"], 0)
        onboarding = http_receiver.build_onboarding_status()
        self.assertEqual(onboarding["current_step"]["id"], "store")
        ledger = http_receiver.build_value_ledger()
        self.assertFalse(ledger["trusted_scope"])
        with self.assertRaisesRegex(ValueError, "尚未识别当前店铺"):
            http_receiver.update_task_state("c" * 16, "doing")

    def test_hmac_identity_is_stable_and_namespaces_do_not_collide(self) -> None:
        first = http_receiver._local_identity_key("douyin_shop_id", "same-1001")
        second = http_receiver._local_identity_key("qianchuan_shop_id", "same-1001")
        account = http_receiver._local_identity_key("qianchuan_account_id", "same-1001")
        advertiser = http_receiver._local_identity_key("qianchuan_advertiser_id", "same-1001")
        product = http_receiver._local_identity_key("douyin_product_id", "same-1001")
        qianchuan_product = http_receiver._local_identity_key("qianchuan_product_id", "same-1001")
        sku = http_receiver._local_identity_key("douyin_sku_id", "same-1001")
        self.assertEqual(first, second)
        self.assertEqual(account, advertiser)
        self.assertEqual(product, qianchuan_product)
        self.assertNotEqual(first, account)
        self.assertNotEqual(product, sku)

    def test_product_ids_are_hmac_resolved_before_json_and_sqlite_writes(self) -> None:
        raw_product_id = "raw-product-9988"
        saved = http_receiver.save_data("doudian", {
            "schema_version": 3,
            "page_type": "products",
            "identity_claims": [claim("douyin_shop_id", "shop-commerce-1")],
            "identity_status": "resolved_by_bridge",
            "quality": {"score": 90, "row_count": 1},
            "privacy": {"masked": True, "commerce_identity_contract": "bridge_hmac_v1"},
            "tables": [{
                "headers": ["商品ID", "商品名称", "可售库存"],
                "rows": [[raw_product_id, "夏季防晒衣", "88"]],
            }],
        })
        local_id = saved["data"]["tables"][0]["rows"][0][0]
        self.assertTrue(local_id.startswith("product_v1_"))
        self.assertEqual(local_id, saved["data"]["commerce_entities"][0]["entity_key"])
        self.assertFalse(saved["data"]["privacy"]["raw_product_ids_persisted"])
        self.assertNotIn(raw_product_id, json.dumps(saved, ensure_ascii=False))
        self.assertFalse(any(raw_product_id in path.read_text(encoding="utf-8") for path in http_receiver.DATA_DIR.rglob("*.json")))
        store = http_receiver._local_store()
        self.assertNotIn(raw_product_id.encode("utf-8"), store.paths.database.read_bytes())
        connection = store.connect()
        try:
            entity_count = connection.execute("SELECT COUNT(*) FROM commerce_entities").fetchone()[0]
            observation = connection.execute(
                "SELECT metric, value, snapshot_id FROM metric_observations"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(1, entity_count)
        self.assertEqual("stock", observation["metric"])
        self.assertEqual(88.0, observation["value"])
        self.assertGreater(observation["snapshot_id"], 0)

    def test_exact_same_row_entities_emit_relations_without_name_join(self) -> None:
        saved = http_receiver.save_data("qianchuan", {
            "schema_version": 3,
            "page_type": "campaigns",
            "quality": {"score": 90, "row_count": 1},
            "tables": [{
                "headers": ["商品ID", "商品名称", "素材ID", "素材名称", "计划ID", "计划名称"],
                "rows": [["product-raw-1001", "防晒衣", "material-raw-1001", "前三秒卖点", "plan-action-1001", "防晒衣计划"]],
            }],
        })
        data = saved["data"]
        self.assertGreaterEqual(len(data["commerce_entities"]), 3)
        self.assertGreaterEqual(len(data["commerce_relations"]), 2)
        self.assertNotIn("product-raw-1001", json.dumps(data, ensure_ascii=False))
        self.assertNotIn("material-raw-1001", json.dumps(data, ensure_ascii=False))
        self.assertEqual("plan-action-1001", data["tables"][0]["rows"][0][4])
        entities = {item["entity_type"]: item["entity_key"] for item in data["commerce_entities"]}
        promotes = next(item for item in data["commerce_relations"] if item["relation"] == "promotes" and item["from_key"] == entities["qianchuan_plan_id"])
        uses_material = next(item for item in data["commerce_relations"] if item["relation"] == "uses_material")
        self.assertEqual(entities["qianchuan_plan_id"], promotes["from_key"])
        self.assertEqual(entities["douyin_product_id"], promotes["to_key"])
        self.assertEqual(entities["qianchuan_plan_id"], uses_material["from_key"])
        self.assertEqual(entities["qianchuan_material_id"], uses_material["to_key"])


if __name__ == "__main__":
    unittest.main()
