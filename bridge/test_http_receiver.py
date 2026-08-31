from __future__ import annotations

import gc
import hashlib
import tempfile
import threading
import time
import unittest
import warnings
import json
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from unittest.mock import MagicMock, Mock, patch

BRIDGE_DIR = str(Path(__file__).resolve().parent)
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)
import http_receiver
from action_protocol import action_integrity_hash
from local_api_auth import AUTH_HEADER, issue_internal_session_token


def complete_chengfang_context():
    return {
        "promotion_mode": "chengfang",
        "account_scope": {"store_id": "shop-1", "account_id": "adv-1", "binding_status": "verified"},
        "promotion_mode_evidence": {"source": "official_api", "confidence": "high"},
        "strategy_id": "strategy-1",
        "metric_contract": {"definition": "net_revenue_roi", "version": "contract-v1"},
        "data_quality": {"confidence": "high", "freshness_seconds": 30, "completeness": 0.95},
    }


def complete_chengfang_profile():
    return {
        "goal": "profit",
        "inputs": {
            "price": 100, "product_cost": 35, "commission_rate": 10,
            "platform_fee_rate": 5, "merchant_discount": 5,
            "fulfillment_cost": 5, "refund_rate": 10, "advertising_cost": 30,
        },
        "evidence": {
            "inventory_days": 12, "fulfillment_rate": 98, "creative_count": 5,
            "data_completeness": 95, "data_freshness_minutes": 5,
            "current_total_budget": 1000, "actual_roi": 1.2,
            "today_spend": 500, "attributed_orders": 8, "daily_realized_loss": 50,
        },
        "boundaries": {
            "minimum_contribution_margin": 5, "daily_budget_cap": 1500,
            "daily_loss_cap": 300, "refund_rate_ceiling": 25,
            "inventory_days_floor": 7, "single_adjustment_cap": 10,
            "daily_adjustment_cap": 20, "daily_action_cap": 2,
            "cooldown_minutes": 30, "authorization_ttl_seconds": 60,
        },
    }


def execution_promotion_context(
    store_key: str,
    account_key: str,
    strategy_id: str,
    *,
    promotion_mode: str = "standard",
) -> dict:
    """Return the minimum explicit promotion contract used by write/readback tests."""

    metric_definition = "net_revenue_roi" if promotion_mode == "chengfang" else "pay_roi"
    return {
        "promotion_mode": promotion_mode,
        "account_scope": {
            "store_id": store_key,
            "account_id": account_key,
            "binding_status": "verified",
        },
        "promotion_mode_evidence": {
            "source": "browser_visible_label",
            "confidence": "high",
        },
        "strategy_id": strategy_id,
        "metric_contract": {
            "definition": metric_definition,
            "version": "v1",
            "period": "test-attribution-window-v1",
        },
        "data_quality": {
            "confidence": "high",
            "freshness_seconds": 0,
            "completeness": 1.0,
        },
    }


def complete_health_scan(
    run_id: str,
    *,
    store_key: str = "store_health",
    account_key: str = "",
    captured_at: int | None = None,
    overrides: dict[str, dict] | None = None,
):
    captured_at = captured_at or int(time.time() * 1000)
    page_types = {
        "qianchuan_video_library": "video_library",
        "qianchuan_overview": "overview",
        "qianchuan_campaigns": "campaigns",
        "qianchuan_live": "qianchuan_live",
        "qianchuan_live_dashboard": "live_dashboard",
    }
    page_ids = list(http_receiver.SCAN_FULL_DOUDIAN_PAGE_IDS)
    if account_key:
        page_ids.extend(http_receiver.SCAN_ADS_OPTIONAL_PAGE_IDS)
    overrides = overrides or {}
    results = []
    for page_id in page_ids:
        source = "qianchuan" if page_id.startswith("qianchuan_") else "doudian"
        result = {
            "id": page_id,
            "source": source,
            "page_type": page_types.get(page_id, page_id),
            "ok": True,
            "collection_complete": True,
            "captured_at": captured_at,
            "store_key": store_key,
            "account_key": account_key if source == "qianchuan" else "",
            "quality": {"score": 90, "row_count": 100, "metric_count": 10},
        }
        patch_value = overrides.get(page_id, {})
        result.update({key: value for key, value in patch_value.items() if key != "quality"})
        if isinstance(patch_value.get("quality"), dict):
            result["quality"] = {**result["quality"], **patch_value["quality"]}
        results.append(result)
    return {
        "status": "completed",
        "scope": "full",
        "coverage_complete": True,
        "store_key": store_key,
        "account_key": account_key,
        "run_id": run_id,
        "finished_at": captured_at,
        "planned_page_ids": page_ids,
        "results": results,
    }


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_dir = http_receiver.DATA_DIR
        self._temp = tempfile.TemporaryDirectory()
        http_receiver.DATA_DIR = Path(self._temp.name) / "data"
        http_receiver.DATA_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        http_receiver.DATA_DIR = self._original_dir
        self._temp.cleanup()

    def test_local_http_server_uses_an_explicit_finite_backlog_and_http_1_0(self) -> None:
        server_class = http_receiver.LocalAgentHTTPServer
        self.assertTrue(issubclass(server_class, ThreadingHTTPServer))
        self.assertIn(
            "request_queue_size",
            server_class.__dict__,
            "the local service must explicitly bound its listen backlog instead of inheriting the platform default",
        )
        request_queue_size = server_class.__dict__["request_queue_size"]
        self.assertIs(type(request_queue_size), int, "the listen backlog must be a finite integer")
        self.assertGreaterEqual(request_queue_size, 16, "a dashboard read burst must not overflow the local listen backlog")
        self.assertEqual(
            http_receiver.Handler.protocol_version,
            "HTTP/1.0",
            "the local Agent must continue closing each response rather than retaining unbounded keep-alive sockets",
        )

    @contextmanager
    def _isolated_data_dir(self):
        """Give looped crash/recovery cases independent durable ledgers."""

        previous_dir = http_receiver.DATA_DIR
        with tempfile.TemporaryDirectory() as temporary_dir:
            http_receiver.DATA_DIR = Path(temporary_dir) / "data"
            http_receiver.DATA_DIR.mkdir(parents=True, exist_ok=True)
            try:
                yield
            finally:
                http_receiver.DATA_DIR = previous_dir

    def _internal_http_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Dian-Agent": "2",
            AUTH_HEADER: issue_internal_session_token(http_receiver.DATA_DIR.parent)["access_token"],
        }

    def _internal_http_get(self, url: str):
        return urllib.request.urlopen(
            urllib.request.Request(url, headers=self._internal_http_headers())
        )

    def _scheduled_report_fixture(
        self,
        report_date: str,
        content: str,
        *,
        now_seconds: int,
        store_scope: str = "store-report1",
        content_kind: str = "operating_report",
        safe: bool = True,
    ) -> dict:
        guard = {
            "schema_version": http_receiver.REPORT_GUARD_SCHEMA_VERSION,
            "store_scope": store_scope,
            "safe_for_business_conclusions": safe,
            "delivery_kind": content_kind,
            "checked_at_ms": (now_seconds - 1) * 1000,
            "valid_until_ms": (now_seconds + 300) * 1000,
            "data_cutoff_at_ms": (now_seconds - 10) * 1000,
            "required_pages": list(http_receiver.SCAN_CORE_DOUDIAN_PAGE_IDS),
            "missing": [],
            "stale": [],
            "low_quality": [],
            "timestamp_conflicts": [],
        }
        return {
            "date": report_date,
            "path": str(http_receiver.DATA_DIR / "reports" / store_scope / f"{report_date}.md"),
            "store_scope": store_scope,
            "content_kind": content_kind,
            "delivery_guard": guard,
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "content": content,
        }

    def _bind_execution_scope(self, store_key: str, account_key: str) -> int:
        """Create the bidirectional catalog witness required by supervised writes."""

        http_receiver._remember_qianchuan_account({
            "key": account_key,
            "store_key": store_key,
            "label": f"测试账户 {account_key}",
            "confidence": "high",
        })
        http_receiver._remember_store_identity(
            {"key": store_key, "confidence": "high", "identity_source": "test"},
            account_key,
        )
        return http_receiver._binding_generation(store_key, account_key)

    def _save_bound_qianchuan(
        self,
        payload: dict,
        *,
        store_key: str = "store-test1",
        account_key: str = "acct-test1",
        trusted_origin: str = "",
    ) -> dict:
        """Persist a qianchuan fixture under the production store/account gate."""

        document = json.loads(json.dumps(payload))
        store = document.get("store") if isinstance(document.get("store"), dict) else {}
        account = document.get("account") if isinstance(document.get("account"), dict) else {}
        document["store"] = {
            **store,
            "key": store_key,
            "confidence": "high",
            "identity_source": "test",
        }
        document["account"] = {
            **account,
            "key": account_key,
            "store_key": store_key,
            "label": str(account.get("label") or f"测试账户 {account_key}"),
            "confidence": "high",
        }
        self._bind_execution_scope(store_key, account_key)
        http_receiver.save_agent_settings({
            "store_key": store_key,
            "qianchuan_account_key": account_key,
        })
        kwargs = {"trusted_origin": trusted_origin} if trusted_origin else {}
        return http_receiver.save_data("qianchuan", document, **kwargs)

    def _seed_authorized_execution(
        self,
        *,
        store_key: str = "store-1",
        account_key: str = "acct-safe1",
        plan_id: str = "plan-safe1",
        authorization_id: str = "a" * 32,
    ) -> tuple[dict, str, dict]:
        now_ms = int(time.time() * 1000)
        final_readback_token = authorization_id * 2
        authorized_document_instance_id = "doc-transaction-authorized-0000"
        final_document_instance_id = "doc-transaction-baseline-0001"
        http_receiver._remember_qianchuan_account({
            "key": account_key,
            "store_key": store_key,
            "label": "事务安全测试账号",
            "confidence": "high",
        })
        http_receiver._remember_store_identity(
            {"key": store_key, "confidence": "high", "identity_source": "test"},
            account_key,
        )
        http_receiver.save_agent_settings({
            "execution_mode": "supervised",
            "store_key": store_key,
            "qianchuan_account_key": account_key,
        })
        draft = http_receiver.build_action_draft(
            operation_type="adjust_budget",
            operation_label="降低预算 20%",
            target_kind="qianchuan_plan",
            target_id=plan_id,
            promotion_context={
                "promotion_mode": "standard",
                "account_scope": {"store_id": store_key, "account_id": account_key},
                "strategy_id": plan_id,
                "metric_contract": {"definition": "pay_roi", "version": "v1", "period": "test-attribution-window-v1"},
                "data_quality": {"confidence": "high", "freshness_seconds": 0, "completeness": 1.0},
            },
            target_name="事务安全测试计划",
            account_key=account_key,
            account_label="事务安全测试账号",
            field="预算",
            current_value=500.0,
            target_value=400.0,
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=now_ms,
            quality_score=90,
            confidence="high",
            evidence={"spend": 300.0, "roi": 0.6, "orders": 3.0},
        )
        confirmed = http_receiver.confirm_action_draft(draft)
        baseline = {
            "snapshot_ref": "qianchuan/campaigns/transaction-test",
            "captured_at_ms": now_ms,
            "quality_score": 90,
            "store_key": store_key,
            "account_key": account_key,
            "plan_id": plan_id,
            "current_value": 500.0,
            "delivery_status": "投放中",
            "spend": 300.0,
            "roi": 0.6,
            "orders": 3.0,
            "metric_contract": {"definition": "pay_roi", "version": "v1", "period": "test-attribution-window-v1"},
            "document_instance_id": final_document_instance_id,
            "document_url": "https://qianchuan.example/campaigns",
            "navigation_started_at_ms": now_ms - 1_000,
            "readback_token": final_readback_token,
            "readback_purpose": "preconsume_baseline",
        }
        confirmed["execution_baseline"] = baseline
        confirmed["execution_baseline_hash"] = http_receiver._execution_baseline_hash(baseline)
        confirmed["final_preconsume_readback_token"] = final_readback_token
        confirmed["final_preconsume_authorization_id"] = authorization_id
        confirmed["final_preconsume_reread_at_ms"] = now_ms
        http_receiver._atomic_json_write(
            http_receiver._action_audit_path(),
            {"schema_version": 1, "updated_at": http_receiver._now_label(), "execution_enabled": False, "actions": [confirmed]},
        )
        http_receiver._save_execution_preflight({
            "session_id": "b" * 24,
            "action_id": confirmed["action_id"],
            "action_integrity_hash": confirmed["integrity_hash"],
            "action_server_signature": confirmed["server_signature"],
            "store_key": store_key,
            "account_key": account_key,
            "binding_generation": int(confirmed["proposal_scope"]["binding_generation"]),
            "execution_baseline_hash": confirmed["execution_baseline_hash"],
            "state": "authorized",
            "authorization_id": authorization_id,
            "authorized_at_ms": now_ms - 1_000,
            "authorized_baseline_document_instance_id": authorized_document_instance_id,
            "final_preconsume_document_instance_id": final_document_instance_id,
            "final_preconsume_readback_token": final_readback_token,
            "final_preconsume_authorization_id": authorization_id,
            "final_preconsume_reread_at_ms": now_ms,
            "authorization_consumed": False,
            "authorization_expires_at_ms": now_ms + 60_000,
            "expires_at_ms": now_ms + 180_000,
        })
        receipt = {
            "authorization_id": authorization_id,
            "submitted": False,
            "platform_success_observed": False,
            "operation_type": "adjust_budget",
            "store_key": store_key,
            "account_key": account_key,
            "plan_id": plan_id,
            "target_value": 400.0,
            "error": "页面明确未提交",
            "definite_not_submitted": True,
            "submission_phase": "pre_mutation",
            "mutation_started": False,
            "click_invoked": False,
            "recovery_unverified": False,
        }
        return confirmed, authorization_id, receipt

    def _seed_manual_reconcile_execution(
        self,
        *,
        store_key: str = "store-archive",
        account_key: str = "acct-archive",
        plan_id: str = "plan-archive",
        authorization_id: str = "7" * 32,
    ) -> tuple[dict, str, dict]:
        confirmed, authorization_id, _ = self._seed_authorized_execution(
            store_key=store_key,
            account_key=account_key,
            plan_id=plan_id,
            authorization_id=authorization_id,
        )
        http_receiver.save_agent_settings({
            "max_daily_execution_count": 10,
            "max_daily_budget_reduction": 10_000,
            "execution_cooldown_minutes": 0,
        })
        http_receiver.consume_execution_authorization(authorization_id)
        audit = http_receiver.load_action_audit()
        locked = next(item for item in audit["actions"] if item["action_id"] == confirmed["action_id"])
        locked = {
            **locked,
            "manual_reconcile_required": True,
            "manual_reconcile_required_at_ms": int(time.time() * 1000),
            "execution_note": "测试：平台结果仍不确定。",
        }
        http_receiver._atomic_json_write(
            http_receiver._action_audit_path(),
            {
                "schema_version": 1,
                "updated_at": http_receiver._now_label(),
                "execution_enabled": False,
                "actions": [
                    locked if item.get("action_id") == locked["action_id"] else item
                    for item in audit["actions"]
                ],
            },
        )
        report = http_receiver.build_execution_preflight_report()
        self.assertEqual("manual_reconcile_required", report["state"])
        return locked, authorization_id, report

    def _execution_plan_snapshot(
        self,
        *,
        store_key: str,
        account_key: str,
        plan_id: str,
        budget: float,
        captured_at_ms: int,
        document_instance_id: str,
        navigation_started_at_ms: int | None = None,
        targeted: bool = False,
        store_raw_id: str = "",
        account_raw_id: str = "",
    ) -> dict:
        quality = {
            "score": 90,
            "row_count": 1,
            "target_plan_found": True,
        }
        if targeted:
            quality.update({
                "targeted": True,
                "scope": "target_plan",
                "coverage_complete": False,
                "collection_complete": False,
                "target_plan_id": plan_id,
            })
        snapshot = {
            "schema_version": 3,
            "page_type": "campaigns",
            "captured_at": captured_at_ms,
            "document_instance_id": document_instance_id,
            "document_url": "https://qianchuan.example/campaigns",
            "navigation_started_at_ms": (
                captured_at_ms - 100
                if navigation_started_at_ms is None
                else navigation_started_at_ms
            ),
            "store": {
                "key": store_key,
                "confidence": "high",
                "identity_source": "data_attribute",
                "evidence_source": "data_attribute",
            },
            "account": {
                "key": account_key,
                "store_key": store_key,
                "label": f"测试账户 {account_key}",
                "confidence": "high",
                "identity_source": "url_parameter",
                "evidence_source": "url_parameter",
            },
            "promotion_context": execution_promotion_context(
                store_key, account_key, plan_id
            ),
            "quality": quality,
            "tables": [{
                "headers": ["计划ID", "计划名称", "日预算", "消耗", "支付 ROI", "成交订单"],
                "rows": [[plan_id, f"测试计划 {plan_id}", str(budget), "320", "0.65", "3"]],
            }],
        }
        if store_raw_id and account_raw_id:
            snapshot["identity_claims"] = [
                {
                    "kind": "qianchuan_shop_id",
                    "raw_id": store_raw_id,
                    "confidence": "high",
                    "evidence_source": "data_attribute",
                },
                {
                    "kind": "qianchuan_advertiser_id",
                    "raw_id": account_raw_id,
                    "confidence": "high",
                    "evidence_source": "url_parameter",
                },
            ]
        return snapshot

    def _write_execution_snapshot_fixture(
        self,
        *,
        action_id: str,
        authorization_id: str,
        readback_token: str,
        purpose: str,
        data: dict,
    ) -> Path:
        """Persist the isolated snapshot shape produced by a strict execution push."""

        snapshot = json.loads(json.dumps(data))
        execution_context = {
            "purpose": purpose,
            "action_id": action_id,
            "authorization_id": authorization_id,
            "readback_token": readback_token,
        }
        payload = {
            "schema_version": 1,
            "source": "qianchuan",
            "page_type": str(snapshot.get("page_type") or "campaigns"),
            "data": snapshot,
            "timestamp": float(snapshot.get("captured_at") or 0) / 1000.0,
            "saved_at": http_receiver._now_label(),
            "snapshot_ref": f"execution/{action_id}/{readback_token}",
            "execution_context": execution_context,
        }
        path = http_receiver._execution_readback_snapshot_path(
            action_id,
            readback_token,
        )
        http_receiver._atomic_json_write(path, payload)
        return path

    def test_identity_secret_first_initialization_is_thread_safe_and_stable(self) -> None:
        barrier = threading.Barrier(24)
        values: list[bytes] = []
        errors: list[Exception] = []

        def read_secret() -> None:
            try:
                barrier.wait(timeout=3)
                values.append(http_receiver._identity_secret())
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        threads = [threading.Thread(target=read_secret) for _ in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual([], errors)
        self.assertEqual(24, len(values))
        self.assertEqual(1, len(set(values)))
        self.assertEqual(values[0], http_receiver._identity_secret_path().read_bytes())
        self.assertEqual([], list(http_receiver._identity_secret_path().parent.glob(".identity-secret-v1.bin.*.tmp")))

    def test_identity_secret_uses_the_winning_cross_process_record(self) -> None:
        winner = b"w" * 32

        def competing_link(_temporary: str, destination: Path) -> None:
            Path(destination).write_bytes(winner)
            raise FileExistsError

        with patch.object(http_receiver.os, "link", side_effect=competing_link):
            result = http_receiver._identity_secret()

        self.assertEqual(winner, result)
        self.assertEqual(winner, http_receiver._identity_secret_path().read_bytes())
        self.assertEqual([], list(http_receiver._identity_secret_path().parent.glob(".identity-secret-v1.bin.*.tmp")))

    def test_client_error_sanitizer_redacts_urls_and_common_credentials(self) -> None:
        safe = http_receiver._safe_client_error(ValueError(
            "https://example.invalid/hook/token api_key=secret-value "
            "client_secret: another-secret Authorization: Bearer bearer-secret"
        ))

        self.assertNotIn("example.invalid", safe)
        self.assertNotIn("secret-value", safe)
        self.assertNotIn("another-secret", safe)
        self.assertNotIn("bearer-secret", safe)
        self.assertIn("[redacted]", safe)

    def test_memory_scope_rejects_cross_store_and_cross_account_overrides(self) -> None:
        http_receiver.save_agent_settings({
            "store_key": "store_current",
            "qianchuan_account_key": "account_current",
        })
        self.assertEqual(
            ("store_current", "account_current"),
            http_receiver._current_memory_scope({}),
        )
        with self.assertRaisesRegex(ValueError, "store scope"):
            http_receiver._current_memory_scope({"store_key": ["store_other"]})
        with self.assertRaisesRegex(ValueError, "account scope"):
            http_receiver._current_memory_scope({"account_key": ["account_other"]})

    def test_database_failure_does_not_publish_a_new_compatibility_snapshot(self) -> None:
        class BrokenStore:
            def persist_snapshot_bundle(self, *args, **kwargs):
                raise http_receiver.LocalStoreError("disk unavailable")

        with patch.object(http_receiver, "_local_store", return_value=BrokenStore()):
            with self.assertRaisesRegex(http_receiver.LocalStoreError, "本次采集未生效"):
                http_receiver.save_data("doudian", {
                    "page_type": "overview",
                    "captured_at": int(time.time() * 1000),
                    "quality": {"score": 90},
                    "safe_metrics": {"成交金额": 100},
                })
        self.assertFalse(http_receiver._snapshot_path("doudian", "overview").exists())
        self.assertFalse((http_receiver.DATA_DIR / "doudian.json").exists())

    def test_json_mirror_failure_keeps_successful_sqlite_snapshot_readable(self) -> None:
        with patch.object(
            http_receiver,
            "_atomic_json_write",
            side_effect=OSError("compatibility mirror unavailable"),
        ):
            saved = http_receiver.save_data("doudian", {
                "page_type": "overview",
                "captured_at": int(time.time() * 1000),
                "quality": {"score": 90},
                "safe_metrics": {"成交金额": 321},
            })

        self.assertEqual(
            "JSON_COMPATIBILITY_MIRROR_DEGRADED",
            saved["post_commit_warnings"][0]["code"],
        )
        self.assertFalse(http_receiver._snapshot_path("doudian", "overview").exists())
        loaded = http_receiver.load_data("doudian", "overview")
        self.assertEqual(321, loaded["data"]["safe_metrics"]["成交金额"])
        self.assertEqual("sqlite", loaded["storage_provenance"]["authoritative_source"])

    def test_qianchuan_store_and_account_persists_by_account_and_reads_old_store_rows(self) -> None:
        account_key = "acct_dualscope"
        store_key = "store_dualscope"
        http_receiver._remember_store_identity({
            "key": store_key, "confidence": "high", "identity_source": "test",
        })
        http_receiver._remember_qianchuan_account({
            "key": account_key, "confidence": "high", "identity_source": "test",
        })
        http_receiver.link_store_account(store_key, account_key)
        captured_at = http_receiver._active_qianchuan_binding_scope(
            store_key,
            account_key,
        )["linked_at_ms"] + 1
        saved = http_receiver.save_data("qianchuan", {
            "page_type": "campaigns",
            "captured_at": captured_at,
            "store": {"key": store_key, "confidence": "high"},
            "account": {
                "key": account_key,
                "store_key": store_key,
                "confidence": "high",
            },
            "quality": {"score": 90},
        }, trusted_origin="official_api_oauth_client")
        rows = list(http_receiver._local_store().iter_snapshots())
        self.assertEqual(
            str(http_receiver._account_snapshot_path(account_key, "campaigns")),
            rows[0]["source_path"],
        )
        self.assertNotEqual(
            str(http_receiver._store_snapshot_path(store_key, "qianchuan", "campaigns")),
            rows[0]["source_path"],
        )

        http_receiver.save_agent_settings({"store_key": store_key, "qianchuan_account_key": account_key})
        self.assertEqual(captured_at, http_receiver.load_data("qianchuan", "campaigns")["data"]["captured_at"])

        # A pre-v4.1.1 row may have been keyed by store even though its payload
        # carries a confirmed account. Account-filtered reads must migrate it
        # safely without ever opening another account's rows.
        old_path = http_receiver._store_snapshot_path(store_key, "qianchuan", "plans")
        old_payload = {
            **saved,
            "page_type": "plans",
            "data": {**saved["data"], "page_type": "plans", "captured_at": captured_at + 1},
        }
        http_receiver._local_store().persist_snapshot(old_payload, old_path, snapshot_type="plans")
        self.assertEqual(
            captured_at + 1,
            http_receiver.load_data("qianchuan", "plans")["data"]["captured_at"],
        )

    def test_low_confidence_refresh_does_not_erase_existing_legacy_binding(self) -> None:
        store_key = "store-legacy1"
        account_key = "acct-legacy1"
        self._bind_execution_scope(store_key, account_key)
        http_receiver.save_agent_settings({
            "store_key": store_key,
            "qianchuan_account_key": account_key,
        })

        # A real page refresh can retain stable local keys while omitting the
        # confidence metadata needed to create a new binding.  It may refresh
        # the existing exact pair, but must not silently delete that pair.
        http_receiver.save_data("qianchuan", {
            "page_type": "overview",
            "captured_at": int(time.time() * 1000),
            "store": {"key": store_key},
            "account": {"key": account_key, "store_key": store_key},
            "quality": {"score": 90},
        })

        self.assertIsNotNone(http_receiver._active_qianchuan_binding_scope(store_key, account_key))
        self.assertIsNotNone(http_receiver.load_data("qianchuan", "overview"))

    def test_qianchuan_without_selected_account_never_cross_reads_partitions(self) -> None:
        for suffix in ("first", "second"):
            http_receiver.save_data("qianchuan", {
                "page_type": "campaigns",
                "captured_at": int(time.time() * 1000),
                "account": {
                    "key": f"acct_{suffix}",
                    "confidence": "confirmed_session",
                },
                "quality": {"score": 90},
            }, trusted_origin="official_api_oauth_client")

        self.assertIsNone(http_receiver.load_data("qianchuan", "campaigns"))
        self.assertFalse(any(item["source"] == "qianchuan" for item in http_receiver.list_snapshots()))

        http_receiver.save_agent_settings({"qianchuan_account_key": "acct_first"})
        self.assertIsNone(
            http_receiver.load_data("qianchuan", "campaigns"),
            "an account without an exact selected store must not become current data",
        )

    def test_qianchuan_cross_store_rebind_requires_a_new_current_snapshot(self) -> None:
        account_key = "acct_rebind1"
        first_store = "store_alpha1"
        second_store = "store_bravo2"
        for store_key in (first_store, second_store):
            http_receiver._remember_store_identity({
                "key": store_key,
                "confidence": "high",
                "identity_source": "test",
            })
        http_receiver._remember_qianchuan_account({
            "key": account_key,
            "confidence": "high",
            "identity_source": "test",
        })
        http_receiver.link_store_account(first_store, account_key)
        first = http_receiver.save_data("qianchuan", {
            "page_type": "overview",
            "captured_at": int(time.time() * 1000),
            "store": {"key": first_store, "confidence": "high"},
            "account": {"key": account_key, "confidence": "high", "store_key": first_store},
            "quality": {"score": 90},
            "binding_scope": {"binding_generation": 2_000_000_000},
        }, expected_store_key=first_store, expected_account_key=account_key)
        self.assertEqual(1, first["binding_scope"]["binding_generation"])
        self.assertEqual(first_store, http_receiver.load_data("qianchuan", "overview")["data"]["store"]["key"])

        http_receiver.unlink_store_account(first_store, account_key)
        http_receiver.link_store_account(second_store, account_key)
        http_receiver.select_store_context(second_store, account_key)

        self.assertIsNone(http_receiver.load_data("qianchuan", "overview"))
        self.assertFalse(any(item["source"] == "qianchuan" for item in http_receiver.list_snapshots()))
        second_catalog = next(
            item for item in http_receiver.build_store_catalog()["stores"]
            if item["key"] == second_store
        )
        self.assertEqual(0, second_catalog["qianchuan_page_count"])
        archived = http_receiver._local_store().latest_snapshot(
            source_name="qianchuan", snapshot_type="overview", account_key=account_key,
        )
        self.assertEqual(first_store, archived["payload"]["data"]["store"]["key"])

        second = http_receiver.save_data("qianchuan", {
            "page_type": "overview",
            "captured_at": int(time.time() * 1000) + 1,
            "store": {"key": second_store, "confidence": "high"},
            "account": {"key": account_key, "confidence": "high", "store_key": second_store},
            "quality": {"score": 90},
        }, expected_store_key=second_store, expected_account_key=account_key)
        loaded = http_receiver.load_data("qianchuan", "overview")
        self.assertEqual(second_store, loaded["data"]["store"]["key"])
        self.assertEqual(second["binding_scope"], loaded["binding_scope"])

    def test_qianchuan_same_store_relink_invalidates_previous_generation(self) -> None:
        store_key = "store_relink1"
        account_key = "acct_relink1"
        http_receiver._remember_store_identity({
            "key": store_key, "confidence": "high", "identity_source": "test",
        })
        http_receiver._remember_qianchuan_account({
            "key": account_key, "confidence": "high", "identity_source": "test",
        })
        http_receiver.link_store_account(store_key, account_key)
        old = http_receiver.save_data("qianchuan", {
            "page_type": "overview",
            "captured_at": int(time.time() * 1000),
            "store": {"key": store_key, "confidence": "high"},
            "account": {"key": account_key, "confidence": "high", "store_key": store_key},
            "quality": {"score": 90},
        }, expected_store_key=store_key, expected_account_key=account_key)
        self.assertEqual(1, old["binding_scope"]["binding_generation"])

        http_receiver.unlink_store_account(store_key, account_key)
        http_receiver.link_store_account(store_key, account_key)
        self.assertEqual(3, http_receiver._binding_generation(store_key, account_key))
        self.assertIsNone(http_receiver.load_data("qianchuan", "overview"))

        fresh = http_receiver.save_data("qianchuan", {
            "page_type": "overview",
            "captured_at": int(time.time() * 1000) + 1,
            "store": {"key": store_key, "confidence": "high"},
            "account": {"key": account_key, "confidence": "high", "store_key": store_key},
            "quality": {"score": 90},
        }, expected_store_key=store_key, expected_account_key=account_key)
        self.assertEqual(3, fresh["binding_scope"]["binding_generation"])
        self.assertIsNotNone(http_receiver.load_data("qianchuan", "overview"))

    def test_identity_conflict_is_quarantined_without_overwriting_current_snapshot(self) -> None:
        current_path = http_receiver._snapshot_path("doudian", "overview")
        current_path.parent.mkdir(parents=True, exist_ok=True)
        current_path.write_text('{"trusted": true}', encoding="utf-8")

        saved = http_receiver.save_data("doudian", {
            "page_type": "overview",
            "captured_at": int(time.time() * 1000),
            "identity_status": "conflict",
            "identity_claims": [],
            "quality": {"score": 70},
        })

        self.assertTrue(saved["data"]["quarantine"]["active"])
        self.assertEqual(current_path.read_text(encoding="utf-8"), '{"trusted": true}')
        self.assertEqual(len(list((http_receiver.DATA_DIR / "quarantine").rglob("*.json"))), 1)

    def test_identity_conflict_push_is_forensic_only_and_not_a_sync_success(self) -> None:
        result = http_receiver.save_scan_page_once("doudian", {
            "page_type": "overview",
            "captured_at": int(time.time() * 1000),
            "identity_status": "conflict",
            "identity_claims": [],
            "quality": {"score": 70},
        })

        self.assertFalse(result["ok"])
        self.assertFalse(result["accepted"])
        self.assertFalse(result["accepted_for_current_data"])
        self.assertFalse(result["current_snapshot_updated"])
        self.assertTrue(result["forensic_saved"])
        self.assertTrue(result["quarantined"])
        self.assertEqual("STORE_IDENTITY_CONFLICT", result["error_code"])
        self.assertFalse(http_receiver._snapshot_path("doudian", "overview").exists())

    def test_qianchuan_conflict_cannot_shadow_current_snapshot_or_catalog(self) -> None:
        store_key = "store-quarantine1"
        account_key = "acct-quarantine1"
        http_receiver._remember_store_identity({
            "key": store_key, "confidence": "high", "identity_source": "test",
        })
        http_receiver._remember_qianchuan_account({
            "key": account_key, "confidence": "high", "identity_source": "test",
        })
        http_receiver.link_store_account(store_key, account_key)
        http_receiver.save_agent_settings({
            "store_key": store_key,
            "qianchuan_account_key": account_key,
        })
        captured_at = int(time.time() * 1000)
        current = http_receiver.save_data("qianchuan", {
            "page_type": "overview",
            "captured_at": captured_at,
            "store": {"key": store_key, "confidence": "high"},
            "account": {
                "key": account_key,
                "store_key": store_key,
                "confidence": "high",
            },
            "quality": {"score": 90},
            "safe_metrics": {"消耗": 100},
        }, expected_store_key=store_key, expected_account_key=account_key)

        forensic = http_receiver.save_data("qianchuan", {
            "page_type": "overview",
            "captured_at": captured_at + 1,
            "identity_status": "conflict",
            "store": {"key": store_key, "confidence": "high"},
            "account": {
                "key": account_key,
                "store_key": store_key,
                "confidence": "high",
            },
            "quality": {"score": 99},
            "safe_metrics": {"消耗": 999999},
        }, expected_store_key=store_key, expected_account_key=account_key)

        self.assertTrue(forensic["data"]["quarantine"]["active"])
        self.assertNotIn("binding_scope", forensic)
        self.assertFalse(http_receiver._snapshot_matches_qianchuan_binding(
            forensic,
            http_receiver._active_qianchuan_binding_scope(store_key, account_key),
        ))
        self.assertEqual(current, http_receiver.load_data("qianchuan"))
        self.assertEqual(current, http_receiver.load_data("qianchuan", "overview"))
        snapshots = [item for item in http_receiver.list_snapshots() if item["source"] == "qianchuan"]
        self.assertEqual(["overview"], [item["page_type"] for item in snapshots])
        selected = next(
            item for item in http_receiver.build_store_catalog()["stores"]
            if item["key"] == store_key
        )
        self.assertEqual(1, selected["qianchuan_page_count"])

    def test_qianchuan_snapshot_requires_exact_binding_link_time(self) -> None:
        store_key = "store-link-time1"
        account_key = "acct-link-time1"
        http_receiver._remember_store_identity({
            "key": store_key, "confidence": "high", "identity_source": "test",
        })
        http_receiver._remember_qianchuan_account({
            "key": account_key, "confidence": "high", "identity_source": "test",
        })
        http_receiver.link_store_account(store_key, account_key)
        http_receiver.save_agent_settings({
            "store_key": store_key,
            "qianchuan_account_key": account_key,
        })
        captured_at = int(time.time() * 1000)
        current = http_receiver.save_data("qianchuan", {
            "page_type": "overview",
            "captured_at": captured_at,
            "store": {"key": store_key, "confidence": "high"},
            "account": {
                "key": account_key,
                "store_key": store_key,
                "confidence": "high",
            },
            "quality": {"score": 90},
        })
        forged = json.loads(json.dumps(current))
        forged["page_type"] = "report"
        forged["data"]["page_type"] = "report"
        forged["data"]["captured_at"] = captured_at + 1
        forged["timestamp"] = (captured_at + 1) / 1000
        forged["binding_scope"]["linked_at_ms"] += 1
        http_receiver._local_store().persist_snapshot(
            forged,
            http_receiver._account_snapshot_path(account_key, "report"),
            snapshot_type="report",
        )

        self.assertIsNone(http_receiver.load_data("qianchuan", "report"))
        self.assertEqual(current, http_receiver.load_data("qianchuan"))
        snapshots = [item for item in http_receiver.list_snapshots() if item["source"] == "qianchuan"]
        self.assertEqual(["overview"], [item["page_type"] for item in snapshots])
        selected = next(
            item for item in http_receiver.build_store_catalog()["stores"]
            if item["key"] == store_key
        )
        self.assertEqual(1, selected["qianchuan_page_count"])

    def test_late_snapshot_from_previous_binding_epoch_is_forensic_only(self) -> None:
        store_key = "store-late-epoch1"
        account_key = "acct-late-epoch1"
        http_receiver._remember_store_identity({
            "key": store_key, "confidence": "high", "identity_source": "test",
        })
        http_receiver._remember_qianchuan_account({
            "key": account_key, "confidence": "high", "identity_source": "test",
        })
        http_receiver.link_store_account(store_key, account_key)
        previous_scope = http_receiver._active_qianchuan_binding_scope(store_key, account_key)
        http_receiver.unlink_store_account(store_key, account_key)
        http_receiver.link_store_account(store_key, account_key)
        current_scope = http_receiver._active_qianchuan_binding_scope(store_key, account_key)
        http_receiver.save_agent_settings({
            "store_key": store_key,
            "qianchuan_account_key": account_key,
        })
        self.assertGreater(current_scope["binding_generation"], previous_scope["binding_generation"])

        current = http_receiver.save_data("qianchuan", {
            "page_type": "live_dashboard",
            "captured_at": current_scope["linked_at_ms"] + 1,
            "store": {"key": store_key, "confidence": "high"},
            "account": {
                "key": account_key,
                "store_key": store_key,
                "confidence": "high",
            },
            "quality": {"score": 90},
            "safe_metrics": {"消耗": 10, "成交订单": 1, "ROI": 2.0},
        })
        late = http_receiver.save_data("qianchuan", {
            "page_type": "live_dashboard",
            "captured_at": current_scope["linked_at_ms"] - 1,
            "binding_scope": previous_scope,
            "store": {"key": store_key, "confidence": "high"},
            "account": {
                "key": account_key,
                "store_key": store_key,
                "confidence": "high",
            },
            "quality": {"score": 99},
            "safe_metrics": {"消耗": 999999, "成交订单": 0, "ROI": 0},
        })

        self.assertEqual("binding_epoch_stale", late["data"]["identity_resolution"])
        self.assertEqual("binding_epoch_stale", late["data"]["quarantine"]["reason"])
        self.assertNotIn("binding_scope", late)
        self.assertEqual(current, http_receiver.load_data("qianchuan", "live_dashboard"))
        self.assertEqual(current, http_receiver.load_data("qianchuan"))
        points = http_receiver.load_history("qianchuan", "live_dashboard", 1)
        self.assertEqual(1, len(points))
        self.assertEqual(10, points[0]["safe_metrics"]["消耗"])
        snapshots = [item for item in http_receiver.list_snapshots() if item["source"] == "qianchuan"]
        self.assertEqual(["live_dashboard"], [item["page_type"] for item in snapshots])
        selected = next(
            item for item in http_receiver.build_store_catalog()["stores"]
            if item["key"] == store_key
        )
        self.assertEqual(1, selected["qianchuan_page_count"])
        response = http_receiver._snapshot_push_response("qianchuan", late)
        self.assertFalse(response["ok"])
        self.assertEqual("ACCOUNT_BINDING_EPOCH_STALE", response["error_code"])

    def test_full_scan_identity_conflict_is_forensic_even_with_active_lease(self) -> None:
        store_key = "store-lease-conflict1"
        account_key = "acct-lease-conflict1"
        run_id = "scan_identity_conflict1"
        page_id = "qianchuan_overview"
        http_receiver.save_scan_status({
            "status": "running",
            "scope": "full",
            "run_id": run_id,
            "revision": 1,
            "store_key": store_key,
            "account_key": account_key,
            "planned_page_ids": [page_id],
            "execution_page_ids": [page_id],
            "results": [],
        })

        result = http_receiver.save_scan_page_once(
            "qianchuan",
            {
                "page_type": "overview",
                "captured_at": int(time.time() * 1000),
                "identity_status": "conflict",
                "identity_claims": [],
                "quality": {"score": 70},
            },
            {"store_key": store_key, "account_key": account_key},
            {"run_id": run_id, "page_id": page_id},
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["forensic_saved"])
        self.assertTrue(result["quarantined"])
        self.assertFalse(http_receiver._scan_page_receipt_path(run_id, page_id).exists())
        quarantined = list(http_receiver._local_store().iter_snapshots(
            snapshot_type="quarantine_overview"
        ))
        self.assertEqual(1, len(quarantined))
        self.assertEqual(store_key, quarantined[0]["payload"]["data"]["quarantine"]["expected_store_key"])
        self.assertEqual(account_key, quarantined[0]["payload"]["data"]["quarantine"]["expected_account_key"])

    def test_http_push_returns_conflict_for_forensic_only_snapshot(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        body = json.dumps({
            "source": "doudian",
            "data": {
                "page_type": "overview",
                "captured_at": int(time.time() * 1000),
                "identity_status": "conflict",
                "identity_claims": [],
                "quality": {"score": 70},
            },
        }).encode("utf-8")
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/push",
                data=body,
                method="POST",
                headers=self._internal_http_headers(),
            )
            with self.assertRaises(urllib.error.HTTPError) as blocked:
                urllib.request.urlopen(request)
            self.assertEqual(409, blocked.exception.code)
            payload = json.loads(blocked.exception.read().decode("utf-8"))
            self.assertFalse(payload["accepted_for_current_data"])
            self.assertTrue(payload["forensic_saved"])
            self.assertEqual("STORE_IDENTITY_CONFLICT", payload["error_code"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_expected_scan_scope_is_checked_before_snapshot_publish(self) -> None:
        with self.assertRaisesRegex(ValueError, "STORE_MISMATCH"):
            http_receiver.save_data(
                "doudian",
                {
                    "page_type": "orders",
                    "store": {"key": "store_other", "confidence": "confirmed_session"},
                    "quality": {"score": 80},
                },
                expected_store_key="store_selected",
            )
        self.assertFalse(http_receiver._snapshot_path("doudian", "orders").exists())

    def test_promotion_readiness_exposes_fail_closed_chengfang_autopilot(self) -> None:
        result = http_receiver.build_current_promotion_readiness()
        autopilot = result["autopilot"]
        self.assertEqual("L0", autopilot["current_level"])
        self.assertFalse(autopilot["execution_allowed"])
        self.assertEqual("official_api_only", autopilot["adapter_policy"])
        self.assertFalse(autopilot["dom_write_fallback_allowed"])
        self.assertFalse(autopilot["official_api"]["registry_verified"])
        self.assertGreater(len(autopilot["official_api"]["discovered_capabilities"]), 0)

    def test_current_promotion_context_recomputes_live_snapshot_age(self) -> None:
        snapshot = {
            "timestamp": 1_000,
            "data": {
                "promotion_context": {
                    **complete_chengfang_context(),
                    "data_quality": {"confidence": "high", "freshness_seconds": 0, "completeness": 0.95},
                }
            },
        }
        with patch.object(http_receiver, "load_data", return_value=snapshot), patch.object(http_receiver.time, "time", return_value=2_200):
            context = http_receiver._current_promotion_context()
        self.assertEqual(1_200, context["data_quality"]["freshness_seconds"])

    def test_current_promotion_context_uses_capture_time_not_save_time(self) -> None:
        now_seconds = 2_000_000_000
        snapshot = {
            "timestamp": now_seconds,
            "data": {
                "captured_at": (now_seconds - 1_200) * 1000,
                "promotion_context": {
                    **complete_chengfang_context(),
                    "data_quality": {"confidence": "high", "freshness_seconds": 0, "completeness": 0.95},
                },
            },
        }
        with patch.object(http_receiver, "load_data", return_value=snapshot), patch.object(http_receiver.time, "time", return_value=now_seconds):
            context = http_receiver._current_promotion_context()
        self.assertEqual(1_200, context["data_quality"]["freshness_seconds"])

    def test_current_promotion_context_marks_excessive_future_time_stale(self) -> None:
        now_seconds = 2_000_000_000
        snapshot = {
            "timestamp": now_seconds,
            "data": {
                "captured_at": (now_seconds + 1_000) * 1000,
                "promotion_context": {
                    **complete_chengfang_context(),
                    "data_quality": {"confidence": "high", "freshness_seconds": 0, "completeness": 0.95},
                },
            },
        }
        with patch.object(http_receiver, "load_data", return_value=snapshot), patch.object(http_receiver.time, "time", return_value=now_seconds):
            context = http_receiver._current_promotion_context()
        self.assertTrue(context["data_quality"]["timestamp_conflict"])
        self.assertGreater(context["data_quality"]["freshness_seconds"], http_receiver.PLAN_CONSOLE_STALE_SECONDS)

    @unittest.skipUnless(http_receiver.COMMERCIAL_RUNTIME_LOADED, "commercial runtime required")
    def test_current_chengfang_profile_hydrates_from_saved_browser_snapshot(self) -> None:
        captured_at = int(time.time() * 1000)
        saved = self._save_bound_qianchuan({
            "schema_version": 2,
            "page_type": "qianchuan_live",
            "captured_at": captured_at,
            "quality": {"score": 92, "metric_count": 3, "row_count": 1},
            "identity_claims": [
                {"kind": "douyin_shop_id", "raw_id": "shop-hydrate", "confidence": "high", "evidence_source": "test"},
                {"kind": "qianchuan_account_id", "raw_id": "acct-hydrate", "confidence": "high", "evidence_source": "test"},
            ],
            "promotion_context": {
                "promotion_mode": "chengfang",
                "promotion_mode_evidence": {"source": "visible_label", "confidence": "high"},
                "data_quality": {"confidence": "high", "completeness": 0.92, "freshness_seconds": 1},
            },
            "tables": [
                {"headers": ["抖音号", "投放设置", "综合营销ROI", "净成交订单数", "整体消耗(元)"], "rows": []},
                {"headers": [], "rows": [["测试直播间", "每日预算\n1000元", "1.2", "8", "500"]]},
            ],
        }, store_key="shop-hydrate", account_key="acct-hydrate")
        http_receiver.save_agent_settings({
            "store_key": saved["data"]["store"]["key"],
            "qianchuan_account_key": saved["data"]["account"]["key"],
        })
        runtime = http_receiver._current_chengfang_runtime()
        runtime.save_profile(complete_chengfang_profile())
        result = http_receiver.hydrate_current_chengfang_profile(save=True)
        self.assertEqual("hydrated", result["status"])
        self.assertTrue(result["saved"])
        self.assertEqual("browser_snapshot_hydrated", result["profile"]["source"])
        self.assertEqual(1000, result["profile"]["evidence"]["current_total_budget"])
        self.assertEqual(1.2, result["profile"]["evidence"]["actual_roi"])
        self.assertFalse(result["production_identifier_verified"])

    def test_browser_push_cannot_forge_trusted_official_storage_provenance(self) -> None:
        saved = http_receiver.save_data("qianchuan", {
            "schema_version": 2,
            "page_type": "overview",
            "channel": "official_api",
            "quality": {"score": 100},
        })
        self.assertEqual("official_api", saved["data"]["channel"])
        self.assertFalse(saved["storage_provenance"]["trusted_official_api"])
        self.assertEqual("browser_bridge_v1", saved["storage_provenance"]["producer"])

        trusted = http_receiver.save_data(
            "qianchuan",
            {"schema_version": 2, "page_type": "plans", "channel": "official_api", "quality": {"score": 100}},
            trusted_origin="official_api_oauth_client",
        )
        self.assertTrue(trusted["storage_provenance"]["trusted_official_api"])
        self.assertEqual("agent_oauth_client_v1", trusted["storage_provenance"]["producer"])

    def test_snapshot_rejects_capture_time_far_in_the_future(self) -> None:
        future_ms = int(time.time() * 1000) + http_receiver.MAX_CAPTURE_FUTURE_SKEW_MS + 60_000
        with self.assertRaisesRegex(ValueError, "too far in the future"):
            http_receiver.save_data(
                "qianchuan",
                {"page_type": "campaigns", "captured_at": future_ms, "quality": {"score": 90}},
            )
        self.assertFalse((http_receiver.DATA_DIR / "qianchuan" / "campaigns.json").exists())

    def test_snapshot_preserves_collector_metric_and_mode_conflicts(self) -> None:
        saved = http_receiver.save_data(
            "qianchuan",
            {
                "page_type": "campaigns",
                "captured_at": int(time.time() * 1000),
                "quality": {"score": 90},
                "promotion_context": {
                    "promotion_mode": "standard",
                    "data_quality": {"metric_conflict": True, "mode_conflict": True},
                },
            },
            trusted_origin="official_api_oauth_client",
        )
        context = saved["data"]["promotion_context"]
        self.assertTrue(context["data_quality"]["metric_conflict"])
        self.assertTrue(context["data_quality"]["mode_conflict"])
        self.assertEqual("unknown", context["promotion_mode"])

    def test_chengfang_snapshot_loader_pins_selected_account_for_all_pages(self) -> None:
        calls = []

        def fake_load(source, page_type=None, account_key=None, store_key=None):
            calls.append((source, page_type, account_key))
            return None

        with patch.object(http_receiver, "load_data", side_effect=fake_load):
            self.assertEqual([], http_receiver._current_chengfang_snapshots("acct-pinned"))
        self.assertEqual(5, len(calls))
        self.assertTrue(all(item[2] == "acct-pinned" for item in calls))

    def test_snapshots_are_partitioned_by_source_and_page_type(self) -> None:
        http_receiver.save_data(
            "doudian",
            {
                "schema_version": 2,
                "page_type": "orders",
                "captured_at": int(time.time() * 1000),
                "quality": {"score": 80, "metric_count": 2, "row_count": 3},
                "metrics": {"待发货": "3"},
            },
        )
        http_receiver.save_data(
            "doudian",
            {
                "schema_version": 2,
                "page_type": "products",
                "captured_at": int(time.time() * 1000),
                "quality": {"score": 70, "metric_count": 1, "row_count": 8},
                "metrics": {"在售商品": "8"},
            },
        )

        self.assertEqual(http_receiver.load_data("doudian", "orders")["page_type"], "orders")
        self.assertEqual(http_receiver.load_data("doudian", "products")["page_type"], "products")
        self.assertEqual(len(http_receiver.list_snapshots()), 2)

    def test_unsafe_page_type_is_normalized(self) -> None:
        saved = http_receiver.save_data("qianchuan", {"page_type": "../../secret", "quality": {}})
        self.assertEqual(saved["page_type"], "unknown")
        self.assertTrue((http_receiver.DATA_DIR / "qianchuan" / "unknown.json").exists())

    def test_low_roi_creates_evidence_based_alert(self) -> None:
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "report",
                "captured_at": int(time.time() * 1000),
                "quality": {"score": 90, "metric_count": 2, "row_count": 5},
                "metrics": {"支付 ROI": "0.82", "消耗": "¥320"},
            },
        )
        insights = http_receiver.build_insights()
        alert = next(item for item in insights["alerts"] if "ROI" in item["title"])
        self.assertEqual(alert["level"], "high")
        self.assertEqual(alert["evidence"]["value"], "0.82")
        self.assertEqual("今天先处理高优先级投放异常", insights["headline"])

    def test_insights_never_calls_stale_high_alert_a_current_delivery_anomaly(self) -> None:
        stale_snapshot = {
            "source": "qianchuan",
            "page_type": "campaigns",
            "saved_at": "2026-08-24 08:00:00",
            "age_seconds": 24 * 60 * 60,
            "fresh": False,
            "quality_score": 90,
        }
        stale_high_alert = {
            "level": "high",
            "title": "经营数据已过期",
            "detail": "当前高优先级提醒来自历史快照。",
            "action": "先同步",
            "evidence": {"source": "qianchuan", "page_type": "campaigns"},
        }
        with patch.object(http_receiver, "list_snapshots", return_value=[stale_snapshot]), patch.object(
            http_receiver, "_metric_matches", return_value=[]
        ), patch.object(http_receiver, "_evaluate_knowledge_rules", return_value=[stale_high_alert]):
            insights = http_receiver.build_insights()

        self.assertEqual(0, sum(1 for item in insights["coverage"] if item["fresh"]))
        self.assertEqual("经营数据已过期，请先同步", insights["headline"])
        self.assertIn("先同步", insights["summary"])
        self.assertNotIn("投放异常", insights["headline"])

    def test_plan_recommendation_uses_plan_level_evidence(self) -> None:
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "campaigns",
                "quality": {"score": 90, "metric_count": 0, "row_count": 2},
                "tables": [
                    {
                        "headers": ["计划名称", "消耗", "支付 ROI", "成交订单"],
                        "rows": [["共2条计划", "¥800", "1.20", "10"]],
                    },
                    {
                        "headers": [],
                        "rows": [["计划 A", "¥500", "0.60", "2"], ["计划 B", "¥300", "2.20", "8"]],
                    },
                ],
            },
            trusted_origin="official_api_oauth_client",
        )
        recommendations = http_receiver.build_plan_recommendations()
        self.assertFalse(any(item["plan"].startswith("共") for item in recommendations))
        plan_a = next(item for item in recommendations if item["plan"] == "计划 A")
        plan_b = next(item for item in recommendations if item["plan"] == "计划 B")
        self.assertEqual(plan_a["action_type"], "reduce_budget")
        self.assertEqual(plan_a["level"], "high")
        self.assertEqual(plan_a["diagnosis"], "ROI 明显低于目标")
        self.assertIn("观察", plan_a["observation_window"])
        self.assertIn("ROI", plan_a["acceptance"])
        self.assertRegex(plan_a["task_id"], r"^[a-f0-9]{16}$")
        self.assertEqual(plan_b["action_type"], "scale_cautiously")
        self.assertIn("10%–15%", plan_b["adjustment_range"])
        self.assertNotIn("action_params", plan_a)
        self.assertNotIn("action_params", plan_b)
        self.assertFalse(plan_a["actionable"])
        self.assertEqual("", plan_a["plan_key"])
        self.assertNotEqual(plan_a["task_id"], plan_b["task_id"])
        self.assertEqual(
            "local_unverified_plan_row",
            plan_a["task_contract"]["subject"]["id_source"],
        )
        self.assertFalse(plan_a["task_contract"]["eligibility"]["can_start"])
        self.assertIn(
            "PLAN_IDENTITY_UNVERIFIED",
            {
                blocker["code"]
                for blocker in plan_a["task_contract"]["eligibility"]["blockers"]
            },
        )

    def test_stale_plan_snapshot_never_generates_performance_recommendation(self) -> None:
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "campaigns",
                "captured_at": int(time.time() * 1000) - (http_receiver.PLAN_CONSOLE_STALE_SECONDS + 1) * 1000,
                "quality": {"score": 90, "metric_count": 0, "row_count": 1},
                "tables": [
                    {
                        "headers": ["计划ID", "计划名称", "消耗", "支付 ROI", "成交订单"],
                        "rows": [["plan-stale", "过期计划", "500", "0", "0"]],
                    }
                ],
            },
            trusted_origin="official_api_oauth_client",
        )
        self.assertEqual([], http_receiver.build_plan_recommendations())

    def test_material_aggregate_is_not_manufactured_into_numbered_plan(self) -> None:
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "campaigns",
                "captured_at": int(time.time() * 1000),
                "quality": {"score": 100, "metric_count": 0, "row_count": 1},
                "tables": [
                    {
                        "headers": ["视频", "整体消耗(元)", "综合营销ROI", "操作"],
                        "rows": [["共40个视频", "500", "0", "查看素材"]],
                    }
                ],
            },
            trusted_origin="official_api_oauth_client",
        )
        self.assertEqual([], http_receiver.build_plan_recommendations())

    def test_plan_console_keeps_all_plans_and_exposes_binding_readiness(self) -> None:
        captured_at = int(time.time() * 1000)
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "campaigns",
                "captured_at": captured_at,
                "account": {"key": "acct_console1234", "label": "主投放账户", "confidence": "high"},
                "promotion_context": {
                    "promotion_mode": "full_domain",
                    "account_scope": {"store_id": "store-1", "account_id": "acct_console1234"},
                },
                "quality": {"score": 90, "metric_count": 0, "row_count": 2},
                "tables": [
                    {
                        "headers": ["计划ID", "计划名称", "投放状态", "日预算", "消耗", "支付 ROI", "成交订单"],
                        "rows": [
                            ["plan-a", "计划 A", "投放中", "500", "500", "0.60", "2"],
                            ["", "计划 B", "已暂停", "300", "300", "2.20", "8"],
                        ],
                    }
                ],
            },
            store_key="store-1",
            account_key="acct_console1234",
        )
        console = http_receiver.build_qianchuan_plan_console()
        self.assertTrue(console["safe"])
        self.assertFalse(console["platform_write_enabled"])
        self.assertFalse(console["automatic_batch_submit"])
        self.assertEqual(console["summary"]["total"], 1)
        self.assertEqual(console["summary"]["running"], 1)
        self.assertEqual(console["summary"]["paused"], 0)
        self.assertEqual(console["summary"]["stale"], 0)
        self.assertEqual(console["summary"]["weighted_roi"], 0.6)
        self.assertEqual(console["data_as_of_ms"], captured_at)
        self.assertEqual(console["oldest_data_as_of_ms"], captured_at)
        self.assertGreaterEqual(console["data_age_seconds"], 0)
        self.assertLess(console["data_age_seconds"], http_receiver.PLAN_CONSOLE_STALE_SECONDS)
        self.assertEqual(console["freshness_status"], "fresh")
        plan_a = next(item for item in console["rows"] if item["plan_name"] == "计划 A")
        self.assertEqual(plan_a["promotion_mode"], "full_domain")
        self.assertEqual(plan_a["plan_type"], "product")
        self.assertFalse(plan_a["stale"])
        self.assertGreaterEqual(plan_a["data_age_seconds"], 0)
        self.assertTrue(plan_a["eligible_for_local_binding"])
        self.assertEqual(plan_a["learning_phase"], "unavailable")
        self.assertEqual(plan_a["learning_status_label"], "暂不可读")
        self.assertEqual(plan_a["platform_low_efficiency"], "unknown")
        self.assertEqual(plan_a["platform_low_efficiency_label"], "暂不可读")
        self.assertEqual(plan_a["diagnostic_source"], "unavailable")
        stored = http_receiver.load_data("qianchuan", "campaigns")
        self.assertEqual("partial", stored["data"]["plan_collection"]["status"])
        self.assertEqual(1, stored["data"]["plan_collection"]["quarantined_rows"])
        self.assertFalse(stored["data"]["plan_collection"]["automatic_write_allowed"])

    def test_plan_console_extracts_id_and_name_from_composite_plan_cell(self) -> None:
        now_ms = int(time.time() * 1000)
        records = [{
            "source": "qianchuan",
            "page_type": "qianchuan_live",
            "quality_score": 100,
            "captured_at_ms": now_ms,
            "account_key": "acct_composite1234",
            "account_label": "乘方账户",
            "promotion_context": {
                "promotion_mode": "chengfang",
                "account_scope": {"account_id": "acct_composite1234"},
            },
            "record": {
                "计划信息": "8/12策\nID：1234567890123456\n1\n商品",
                "投放状态": "投放中",
                "整体消耗(元)": "88.50",
            },
        }]

        def table_records(source, _page_types):
            return records if source == "qianchuan" else []

        with patch.object(http_receiver, "_table_records", side_effect=table_records), patch.object(
            http_receiver, "build_plan_recommendations", return_value=[]
        ):
            console = http_receiver.build_qianchuan_plan_console()

        self.assertEqual(1, console["summary"]["total"])
        row = console["rows"][0]
        self.assertEqual("1234567890123456", row["plan_id"])
        self.assertEqual("8/12策", row["plan_name"])
        self.assertEqual("chengfang", row["promotion_mode"])
        self.assertEqual("live", row["plan_type"])
        self.assertTrue(row["eligible_for_local_binding"])

    def test_plan_identity_keeps_same_name_and_cross_surface_ids_separate(self) -> None:
        now_ms = int(time.time() * 1000)
        account_key = "acct-plan-identity"
        store_key = "store-plan-identity"
        http_receiver.save_agent_settings({
            "store_key": store_key,
            "qianchuan_account_key": account_key,
        })

        def entry(index: int, plan_id: str, mode: str, page_type: str) -> dict:
            return {
                "source": "qianchuan",
                "page_type": page_type,
                "table_index": 0,
                "row_index": index,
                "quality_score": 90,
                "captured_at_ms": now_ms,
                "account_key": account_key,
                "account_label": "唯一键账户",
                "promotion_context": execution_promotion_context(
                    store_key, account_key, plan_id, promotion_mode=mode
                ),
                "record": {
                    "计划ID": plan_id,
                    "计划名称": "同名计划",
                    "日预算": "500",
                    "消耗": "500",
                    "支付 ROI": "0.5",
                    "成交订单": "2",
                },
            }

        records = [
            entry(0, "plan-a", "standard", "campaigns"),
            entry(1, "plan-b", "standard", "campaigns"),
            entry(2, "plan-a", "standard", "qianchuan_live"),
            entry(3, "plan-a", "full_domain", "campaigns"),
        ]

        def table_records(source, _page_types):
            return records if source == "qianchuan" else []

        with patch.object(http_receiver, "_table_records", side_effect=table_records), patch.object(
            http_receiver, "_plan_collection_snapshots", return_value=[]
        ):
            recommendations = http_receiver.build_plan_recommendations()
            console = http_receiver.build_qianchuan_plan_console()

        self.assertEqual(4, len(recommendations))
        self.assertEqual(4, len({item["plan_key"] for item in recommendations}))
        self.assertEqual(4, len({item["task_id"] for item in recommendations}))
        self.assertTrue(all(item["task_id"] == item["task_key"][:16] for item in recommendations))
        self.assertTrue(all(item["task_contract"]["subject"]["id"] == item["plan_key"] for item in recommendations))
        legacy_aliases = [
            alias
            for item in recommendations
            for alias in item["legacy_task_ids"]
        ]
        self.assertEqual(len(legacy_aliases), len(set(legacy_aliases)))
        self.assertEqual(4, console["summary"]["total"])
        self.assertEqual(4, len({row["plan_key"] for row in console["rows"]}))

    def test_missing_plan_ids_stay_distinct_and_fail_closed_across_accounts(self) -> None:
        now_ms = int(time.time() * 1000)
        selected_store = "store-unverified-plan"
        selected_account = "acct-selected-plan"
        http_receiver.save_agent_settings({
            "store_key": selected_store,
            "qianchuan_account_key": selected_account,
        })

        def entry(
            row_index: int, account_key: str, promotion_mode: str, page_type: str
        ) -> dict:
            return {
                "source": "qianchuan",
                "page_type": page_type,
                "table_index": 0,
                "row_index": row_index,
                "quality_score": 90,
                "captured_at_ms": now_ms,
                "account_key": account_key,
                "account_label": account_key,
                "promotion_context": {
                    "promotion_mode": promotion_mode,
                    "account_scope": {"account_id": account_key},
                },
                "record": {
                    "计划名称": "同名无 ID 计划",
                    "消耗": "500",
                    "支付 ROI": "0.5",
                    "成交订单": "2",
                },
            }

        records = [
            entry(0, "acct-a", "standard", "campaigns"),
            entry(0, "acct-b", "chengfang", "qianchuan_live"),
        ]

        def table_records(source, _page_types):
            return records if source == "qianchuan" else []

        with patch.object(http_receiver, "_table_records", side_effect=table_records), patch.object(
            http_receiver,
            "_task_contract_source_index",
            return_value=[{
                "source": "qianchuan",
                "page_type": "campaigns",
                "captured_at_ms": now_ms,
                "quality_score": 90,
            }],
        ):
            recommendations = http_receiver.build_plan_recommendations()

        self.assertEqual(2, len(recommendations))
        self.assertEqual(2, len({item["task_id"] for item in recommendations}))
        self.assertEqual(2, len({item["contract_fingerprint"] for item in recommendations}))
        for item in recommendations:
            contract = item["task_contract"]
            self.assertEqual(
                "local_unverified_plan_row", contract["subject"]["id_source"]
            )
            self.assertFalse(contract["eligibility"]["can_start"])
            blocker_codes = {
                blocker["code"] for blocker in contract["eligibility"]["blockers"]
            }
            self.assertIn("PLAN_IDENTITY_UNVERIFIED", blocker_codes)
            self.assertIn("ACCOUNT_MISMATCH", blocker_codes)

    def test_plan_workbench_and_ops_manager_share_canonical_task_contract(self) -> None:
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "campaigns",
                "promotion_context": {"promotion_mode": "standard"},
                "quality": {"score": 92, "metric_count": 4, "row_count": 1},
                "tables": [{
                    "headers": ["计划ID", "计划名称", "日预算", "消耗", "支付 ROI", "成交订单"],
                    "rows": [["plan-contract-001", "合同一致性计划", "1000", "500", "0.60", "2"]],
                }],
            },
            store_key="store-plan-contract",
            account_key="acct-plan-contract",
            trusted_origin="official_api_oauth_client",
        )
        recommendation = http_receiver.build_plan_recommendations()[0]

        with patch.object(http_receiver, "build_shelf_analysis", return_value={"recommendations": [], "data_status": "ready"}), \
             patch.object(http_receiver, "build_live_analysis", return_value={"recommendations": [], "data_status": "ready"}), \
             patch.object(http_receiver, "build_plan_recommendations", return_value=[recommendation]), \
             patch.object(http_receiver, "build_inventory_alerts", return_value=[]), \
             patch.object(http_receiver, "_inventory_task_state", return_value={"status": "ready"}), \
             patch.object(http_receiver, "build_qianchuan_creative_analysis", return_value={"recommendations": [], "data_status": "ready"}), \
             patch.object(http_receiver, "build_douyin_product_graph", return_value={"recommendations": [], "status": "ready"}), \
             patch.object(http_receiver, "build_scan_receipt", return_value={"results": []}), \
             patch.object(http_receiver, "build_execution_effectiveness_report", return_value={"items": []}):
            manager = http_receiver.build_ops_manager()

        task = manager["all_tasks"][0]
        self.assertEqual(recommendation["task_id"], task["id"])
        self.assertEqual(recommendation["task_key"], task["task_key"])
        self.assertEqual(recommendation["contract_fingerprint"], task["contract_fingerprint"])
        self.assertEqual(recommendation["plan_key"], task["task_contract"]["subject"]["id"])
        self.assertEqual("local_plan_identity", task["task_contract"]["subject"]["id_source"])
        self.assertNotEqual(task["id"], task["legacy_task_ids"][0])

    def test_legacy_plan_task_state_migrates_to_canonical_id_on_update(self) -> None:
        store_key = "store-plan-migration"
        account_key = "acct-plan-migration"
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "campaigns",
                "promotion_context": {"promotion_mode": "standard"},
                "quality": {"score": 92, "metric_count": 4, "row_count": 1},
                "tables": [{
                    "headers": ["计划ID", "计划名称", "日预算", "消耗", "支付 ROI", "成交订单"],
                    "rows": [["plan-migrate-001", "历史任务迁移计划", "1000", "500", "0.60", "2"]],
                }],
            },
            store_key=store_key,
            account_key=account_key,
            trusted_origin="official_api_oauth_client",
        )
        recommendation = http_receiver.build_plan_recommendations()[0]
        canonical_id = recommendation["task_id"]
        legacy_ids = recommendation["legacy_task_ids"]
        self.assertGreaterEqual(len(legacy_ids), 2)
        scope = http_receiver._task_scope_key(store_key)
        for legacy_id in legacy_ids:
            with self.subTest(legacy_id=legacy_id):
                http_receiver._atomic_json_write(http_receiver._task_states_path(), {
                    "schema_version": 2,
                    "scopes": {scope: {legacy_id: {
                        "status": "observing",
                        "updated_at": "2026-08-30 10:00:00",
                        "owner": "投放运营",
                        "title": recommendation["workbench_title"],
                        "history": [{"from": "doing", "to": "observing"}],
                    }}},
                })

                rebuilt = http_receiver.build_plan_recommendations()[0]
                self.assertEqual("observing", rebuilt["task_status"])
                manager = http_receiver.build_ops_manager()
                self.assertEqual(1, sum(item["id"] == canonical_id for item in manager["all_tasks"]))
                self.assertFalse(any(item["id"] == legacy_id for item in manager["all_tasks"]))
                legacy_snapshot_key = f"{scope}|{legacy_id}"
                http_receiver._atomic_json_write(http_receiver._suggestion_snapshots_path(), {
                    legacy_snapshot_key: {
                        "schema_version": 3,
                        "task_id": legacy_id,
                        "scope": scope,
                        "store_key": store_key,
                        "business_date": scope.rsplit(":", 1)[-1],
                        "created_at": "2026-08-30 09:00:00",
                        "captured_at_ms": 1_788_061_200_000,
                        "observation_window_minutes": 0,
                        "due_at_ms": 1_788_061_200_000,
                        "context": {"title": recommendation["workbench_title"]},
                        "task_contract": {},
                        "required_source_keys": [],
                        "metric_keywords": [],
                        "metrics_snapshot": {},
                        "source_watermarks": {},
                        "metric_watermarks": {},
                        "evaluated": False,
                        "evaluation": None,
                    },
                })

                closed = http_receiver.update_task_state(
                    legacy_id,
                    "done",
                    note="已复核观察窗口，ROI 回升并完成结案。",
                    contract_fingerprint=rebuilt["contract_fingerprint"],
                    store_key=store_key,
                )
                self.assertEqual(canonical_id, closed["task_id"])
                self.assertEqual("done", closed["status"])
                current_states = http_receiver._load_task_state_document()["scopes"][scope]
                self.assertIn(canonical_id, current_states)
                self.assertTrue(current_states[legacy_id]["migration_tombstone"])
                self.assertEqual(canonical_id, current_states[legacy_id]["migrated_to"])
                snapshots = http_receiver.load_suggestion_snapshots()
                canonical_snapshot_key = f"{scope}|{canonical_id}"
                self.assertIn(canonical_snapshot_key, snapshots)
                self.assertNotIn(legacy_snapshot_key, snapshots)
                self.assertEqual(canonical_id, snapshots[canonical_snapshot_key]["task_id"])
                self.assertIsInstance(snapshots[canonical_snapshot_key]["evaluation"], dict)

    def test_canonical_plan_state_wins_when_legacy_alias_also_exists(self) -> None:
        store_key = "store-plan-conflict"
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "campaigns",
                "promotion_context": {"promotion_mode": "standard"},
                "quality": {"score": 92, "metric_count": 4, "row_count": 1},
                "tables": [{
                    "headers": ["计划ID", "计划名称", "日预算", "消耗", "支付 ROI", "成交订单"],
                    "rows": [["plan-conflict-001", "状态冲突计划", "1000", "500", "0.60", "2"]],
                }],
            },
            store_key=store_key,
            account_key="acct-plan-conflict",
            trusted_origin="official_api_oauth_client",
        )
        recommendation = http_receiver.build_plan_recommendations()[0]
        canonical_id = recommendation["task_id"]
        legacy_id = recommendation["legacy_task_ids"][0]
        scope = http_receiver._task_scope_key(store_key)
        http_receiver._atomic_json_write(http_receiver._task_states_path(), {
            "schema_version": 2,
            "scopes": {scope: {
                canonical_id: {"status": "doing", "updated_at": "2026-08-30 11:00:00"},
                legacy_id: {"status": "observing", "updated_at": "2026-08-30 12:00:00"},
            }},
        })

        rebuilt = http_receiver.build_plan_recommendations()[0]
        self.assertEqual("doing", rebuilt["task_status"])
        manager = http_receiver.build_ops_manager()
        canonical_task = next(item for item in manager["all_tasks"] if item["id"] == canonical_id)
        self.assertEqual("doing", canonical_task["status"])
        self.assertFalse(any(item["id"] == legacy_id for item in manager["all_tasks"]))

    def test_stop_loss_execution_start_uses_canonical_action_and_scope_readiness(self) -> None:
        account_key = "acct-stop-loss-ready"
        scope_key = http_receiver._plan_collection_scope_key(account_key, "standard", "product")

        def action(plan_id: str, *, can_confirm: bool = True, blocked: list | None = None) -> dict:
            return {
                "can_confirm": can_confirm,
                "blocked_reasons": blocked or [],
                "target_ref": {"id": plan_id, "account_key": account_key},
                "promotion_context": execution_promotion_context(
                    "store-stop-loss", account_key, plan_id
                ),
                "evidence_ref": {
                    "collection_scope_required": True,
                    "plan_type": "product",
                    "collection_scope_key": scope_key,
                },
            }

        ready_action = action("plan-ready")
        blocked_action = action(
            "plan-blocked",
            can_confirm=False,
            blocked=[{"code": "DATA_QUALITY_LOW", "message": "质量不足"}],
        )
        scope_action = action("plan-scope-blocked")

        def recommendation(plan_id: str, draft: dict) -> dict:
            return {
                "plan": plan_id,
                "action_type": "reduce_budget",
                "confidence": "high",
                "evidence": {"spend": 500.0, "roi": 0.5, "roi_target": 1.5},
                "action_params": draft,
            }

        recommendations = [
            recommendation("plan-ready", ready_action),
            recommendation("plan-blocked", blocked_action),
            recommendation("plan-scope-blocked", scope_action),
            {
                "plan": "missing-id",
                "action_type": "reduce_budget",
                "confidence": "high",
                "evidence": {"spend": 500.0, "roi": 0.5, "roi_target": 1.5},
            },
        ]
        rows = [
            {
                "account_key": account_key,
                "plan_id": "plan-ready",
                "promotion_mode": "standard",
                "plan_type": "product",
                "collection_scope_key": scope_key,
                "supervised_draft_ready": True,
                "collection_gate_state": "ready",
                "automation_blockers": [],
            },
            {
                "account_key": account_key,
                "plan_id": "plan-blocked",
                "promotion_mode": "standard",
                "plan_type": "product",
                "collection_scope_key": scope_key,
                "supervised_draft_ready": True,
                "collection_gate_state": "ready",
                "automation_blockers": [],
            },
            {
                "account_key": account_key,
                "plan_id": "plan-scope-blocked",
                "promotion_mode": "standard",
                "plan_type": "product",
                "collection_scope_key": scope_key,
                "supervised_draft_ready": False,
                "collection_gate_state": "partial_collection",
                "automation_blockers": [{
                    "code": "PLAN_SCOPE_COVERAGE_INCOMPLETE",
                    "message": "范围未完整采集",
                }],
            },
        ]
        settings = {
            "execution_mode": "supervised",
            "min_spend_for_action": 100,
            "roi_target": 1.5,
        }
        with patch.object(http_receiver, "build_plan_recommendations", return_value=recommendations), patch.object(
            http_receiver, "build_qianchuan_plan_console", return_value={"rows": rows}
        ):
            queue = http_receiver.build_stop_loss_queue(settings)

        by_plan = {item["plan"]: item for item in queue["items"]}
        self.assertTrue(by_plan["plan-ready"]["can_start_execution"])
        self.assertFalse(by_plan["plan-blocked"]["can_start_execution"])
        self.assertFalse(by_plan["plan-scope-blocked"]["can_start_execution"])
        self.assertFalse(by_plan["missing-id"]["can_start_execution"])
        self.assertFalse(by_plan["plan-scope-blocked"]["execution_readiness"]["scope_gate"]["ready"])

    def test_plan_console_rejects_hidden_and_invalid_plan_id_placeholders(self) -> None:
        now_ms = int(time.time() * 1000)
        base = {
            "source": "qianchuan",
            "page_type": "campaigns",
            "quality_score": 100,
            "captured_at_ms": now_ms,
            "account_key": "acct_placeholder1234",
            "account_label": "占位符账户",
            "promotion_context": {
                "promotion_mode": "standard",
                "account_scope": {"account_id": "acct_placeholder1234"},
            },
        }
        records = [
            {**base, "record": {"计划ID": "[已隐藏]", "计划名称": "隐藏 ID 计划", "状态": "投放中"}},
            {**base, "record": {"计划ID": "[标识无效]", "计划名称": "无效 ID 计划", "状态": "暂停"}},
        ]

        def table_records(source, _page_types):
            return records if source == "qianchuan" else []

        with patch.object(http_receiver, "_table_records", side_effect=table_records), patch.object(
            http_receiver, "build_plan_recommendations", return_value=[]
        ):
            console = http_receiver.build_qianchuan_plan_console()

        self.assertEqual(2, console["summary"]["total"])
        self.assertEqual({""}, {row["plan_id"] for row in console["rows"]})
        for row in console["rows"]:
            self.assertFalse(row["eligible_for_local_binding"])
            self.assertIn("计划 ID 缺失", row["binding_blockers"])

    def test_plan_console_does_not_manufacture_live_material_rows_into_plans(self) -> None:
        now_ms = int(time.time() * 1000)
        base = {
            "source": "qianchuan",
            "page_type": "qianchuan_live",
            "quality_score": 100,
            "captured_at_ms": now_ms,
            "account_key": "acct_live1234",
            "account_label": "直播账户",
            "promotion_context": {
                "promotion_mode": "chengfang",
                "account_scope": {"account_id": "acct_live1234"},
            },
        }
        records = [
            {
                **base,
                "record": {
                    "计划": "真实直播计划\nID：live-plan-1234",
                    "投放状态": "投放中",
                    "消耗": "120",
                },
            },
            {
                **base,
                "record": {
                    "商品": "兽醒纪男士活力裤",
                    "视频": "素材 8/12",
                    "抖音号": "直播间账号",
                    "消耗": "66",
                },
            },
            {
                **base,
                "record": {
                    "抖音号": "兽醒纪男士活力裤\n设置直播规划\n素材",
                    "投放状态": "投放中",
                    "投放设置": "ROI目标\n3.00",
                    "整体消耗(元)": "120",
                },
            },
        ]

        def table_records(source, _page_types):
            return records if source == "qianchuan" else []

        with patch.object(http_receiver, "_table_records", side_effect=table_records), patch.object(
            http_receiver, "build_plan_recommendations", return_value=[]
        ):
            console = http_receiver.build_qianchuan_plan_console()

        self.assertEqual(2, console["summary"]["total"])
        explicit = next(row for row in console["rows"] if row["plan_name"] == "真实直播计划")
        self.assertEqual("live-plan-1234", explicit["plan_id"])
        fallback = next(row for row in console["rows"] if row["plan_name"] == "兽醒纪男士活力裤")
        self.assertEqual("", fallback["plan_id"])
        self.assertFalse(fallback["eligible_for_local_binding"])
        self.assertIn("计划 ID 缺失", fallback["binding_blockers"])
        self.assertFalse(fallback["supervised_draft_ready"])

    def test_plan_console_prefers_newer_browser_snapshot_over_richer_old_snapshot(self) -> None:
        now_ms = int(time.time() * 1000)
        base = {
            "source": "qianchuan",
            "page_type": "campaigns",
            "quality_score": 100,
            "account_key": "acct_freshness1234",
            "account_label": "新鲜度账户",
            "promotion_context": {
                "promotion_mode": "standard",
                "account_scope": {"account_id": "acct_freshness1234"},
            },
        }
        records = [
            {
                **base,
                "captured_at_ms": now_ms - 60_000,
                "record": {
                    "计划ID": "fresh-plan-1234",
                    "计划名称": "新鲜度计划",
                    "投放状态": "投放中",
                    "预算": "900",
                    "消耗": "500",
                    "支付ROI": "2.50",
                    "成交订单": "20",
                },
            },
            {
                **base,
                "captured_at_ms": now_ms,
                "record": {
                    "计划ID": "fresh-plan-1234",
                    "计划名称": "新鲜度计划",
                    "投放状态": "暂停",
                },
            },
        ]

        def table_records(source, _page_types):
            return records if source == "qianchuan" else []

        with patch.object(http_receiver, "_table_records", side_effect=table_records), patch.object(
            http_receiver, "build_plan_recommendations", return_value=[]
        ):
            console = http_receiver.build_qianchuan_plan_console()

        self.assertEqual(1, console["summary"]["total"])
        row = console["rows"][0]
        self.assertEqual(now_ms, row["captured_at_ms"])
        self.assertEqual("暂停", row["delivery_status"])
        self.assertIsNone(row["budget"])
        self.assertIsNone(row["spend"])
        self.assertIsNone(row["roi"])
        self.assertIsNone(row["orders"])

    def test_plan_snapshot_gate_rejects_split_body_when_plan_ids_were_lost(self) -> None:
        payload = {
            "schema_version": 3,
            "page_type": "campaigns",
            "reason": "manual-plan-sync",
            "identity_claims": [{
                "kind": "qianchuan_advertiser_id",
                "raw_id": "advertiser-gate-1001",
                "evidence_source": "test",
                "confidence": "high",
            }],
            "quality": {"score": 100},
            "tables": [
                {"headers": ["计划", "投放状态"], "rows": [["共1条计划", ""]]},
                {"headers": [], "rows": [["8/12策\nID：[已隐藏]", "投放中"]]},
            ],
        }

        with self.assertRaisesRegex(ValueError, "PLAN_IDENTIFIERS_UNRESOLVED"):
            http_receiver.save_data("qianchuan", payload)

    def test_plan_snapshot_gate_cannot_be_bypassed_by_omitting_reason(self) -> None:
        payload = {
            "schema_version": 3,
            "page_type": "campaigns",
            "quality": {"score": 90},
            "tables": [{
                "headers": ["计划ID", "计划名称", "消耗"],
                "rows": [["", "缺少稳定标识的计划", "100"]],
            }],
        }

        with self.assertRaisesRegex(ValueError, "PLAN_IDENTIFIERS_UNRESOLVED"):
            http_receiver.save_data("qianchuan", payload)

    def test_all_invalid_plan_rows_do_not_overwrite_last_valid_snapshot(self) -> None:
        first = self._save_bound_qianchuan({
            "schema_version": 3,
            "page_type": "campaigns",
            "captured_at": 1_000,
            "quality": {"score": 90},
            "tables": [{
                "headers": ["计划ID", "计划名称"],
                "rows": [["plan-safe-1001", "已验证计划"]],
            }],
        }, store_key="store-invalid1", account_key="acct-invalid1")
        with self.assertRaisesRegex(ValueError, "PLAN_IDENTIFIERS_UNRESOLVED"):
            self._save_bound_qianchuan({
                "schema_version": 3,
                "page_type": "campaigns",
                "captured_at": 2_000,
                "quality": {"score": 90},
                "tables": [{
                    "headers": ["计划ID", "计划名称"],
                    "rows": [["", "缺少标识的候选计划"]],
                }],
            }, store_key="store-invalid1", account_key="acct-invalid1")

        current = http_receiver.load_data("qianchuan", "campaigns")
        self.assertEqual(first["data"]["captured_at"], current["data"]["captured_at"])
        self.assertEqual("plan-safe-1001", current["data"]["tables"][0]["rows"][0][0])

    def test_plan_snapshot_gate_keeps_valid_rows_and_quarantines_only_missing_ids(self) -> None:
        payload = {
            "schema_version": 3,
            "page_type": "campaigns",
            "quality": {"score": 90},
            "plan_collection": {"platform_total": 2},
            "tables": [{
                "headers": ["计划ID", "计划名称", "消耗"],
                "rows": [
                    ["plan-valid-1001", "有效计划", "100"],
                    ["", "缺少稳定标识的计划", "80"],
                ],
            }],
        }

        saved = http_receiver.save_data("qianchuan", payload)

        rows = saved["data"]["tables"][0]["rows"]
        self.assertEqual([["plan-valid-1001", "有效计划", "100"]], rows)
        validation = saved["data"]["quality"]["server_plan_identity_coverage"]
        self.assertEqual(2, validation["eligible_rows"])
        self.assertEqual(1, validation["identified_rows"])
        self.assertEqual(1, validation["quarantined_rows"])
        self.assertEqual("partial", validation["collection_state"])
        self.assertFalse(validation["coverage_complete"])
        self.assertFalse(validation["automatic_write_allowed"])
        quarantine = saved["data"]["plan_row_quarantine"]
        self.assertTrue(quarantine["active"])
        self.assertTrue(quarantine["excluded_from_current_plan_rows"])
        self.assertEqual("缺少稳定标识的计划", quarantine["rows"][0]["plan_name"])
        receipt = http_receiver.build_plan_collection_receipt(
            [{"plan_id": "plan-valid-1001", "promotion_mode": "standard"}],
            [saved],
        )
        self.assertEqual("partial", receipt["status"])
        self.assertTrue(receipt["identity_quarantined"])
        self.assertFalse(receipt["safe_to_claim_complete"])
        self.assertIn("PLAN_ROWS_QUARANTINED", {
            warning["code"] for warning in receipt["coverage_warnings"]
        })

    def test_plan_snapshot_gate_accepts_complete_split_plan_identity(self) -> None:
        payload = {
            "schema_version": 3,
            "page_type": "campaigns",
            "reason": "manual-plan-sync",
            "identity_claims": [{
                "kind": "qianchuan_advertiser_id",
                "raw_id": "advertiser-gate-1002",
                "evidence_source": "test",
                "confidence": "high",
            }],
            "quality": {"score": 100},
            "tables": [
                {"headers": ["计划", "投放状态"], "rows": [["共1条计划", ""]]},
                {
                    "headers": ["计划", "投放状态", "计划ID"],
                    "rows": [["8/12策\nID：pid_deadbeef", "投放中", "pid_deadbeef"]],
                },
            ],
        }

        saved = http_receiver.save_data("qianchuan", payload)

        self.assertEqual(100, saved["data"]["quality"]["server_plan_identity_coverage"]["coverage_rate"])

    def test_plan_snapshot_gate_rejects_header_only_loading_state(self) -> None:
        payload = {
            "schema_version": 3,
            "page_type": "campaigns",
            "reason": "manual-plan-sync",
            "quality": {"score": 25},
            "tables": [{"headers": ["计划信息", "投放状态"], "rows": []}],
        }

        with self.assertRaisesRegex(ValueError, "PLAN_TABLE_LOADING"):
            http_receiver.save_data("qianchuan", payload)

    def test_plan_snapshot_gate_accepts_explicit_empty_plan_state(self) -> None:
        payload = {
            "schema_version": 3,
            "page_type": "campaigns",
            "reason": "manual-plan-sync",
            "quality": {"score": 25},
            "signals": ["当前筛选条件下暂无符合条件的计划"],
            "tables": [{"headers": ["计划信息", "投放状态"], "rows": []}],
        }

        saved = http_receiver.save_data("qianchuan", payload)
        coverage = saved["data"]["quality"]["server_plan_identity_coverage"]
        self.assertTrue(coverage["explicit_empty"])
        self.assertEqual(0, coverage["eligible_rows"])
        self.assertEqual(100, coverage["coverage_rate"])

    def test_plan_identifier_rejects_common_english_placeholders(self) -> None:
        for value in ("unknown", "null", "none", "masked", "missing", "undefined", "n/a", "[masked]"):
            with self.subTest(value=value):
                self.assertEqual("", http_receiver._normalize_plan_identifier(value))

    def test_plan_identifier_never_promotes_english_or_numeric_plan_names(self) -> None:
        for plan_name in ("SUMMER_SALE", "12345678"):
            with self.subTest(plan_name=plan_name):
                self.assertEqual("", http_receiver._plan_identifier({"计划名称": plan_name}))
                self.assertEqual("", http_receiver._plan_identifier({"计划": plan_name}))

    def test_plan_identifier_extracts_only_explicit_id_subline_from_composite_cell(self) -> None:
        self.assertEqual(
            "1234567890123456",
            http_receiver._plan_identifier({"计划信息": "SUMMER_SALE\nID：1234567890123456\n商品"}),
        )
        self.assertEqual(
            "plan_explicit_1001",
            http_receiver._plan_identifier({"计划ID": "plan_explicit_1001", "计划名称": "12345678"}),
        )
        self.assertEqual(
            "",
            http_receiver._plan_identifier({"计划信息": "SUMMER_SALE\n计划编号：1234567890123456"}),
        )

    def test_new_qianchuan_plan_headers_share_the_browser_extractor_contract(self) -> None:
        cases = [
            (
                "campaigns",
                ["商品计划名称", "推广计划 ID", "投放状态"],
                ["夏季商品计划", "product-plan-1001", "投放中"],
            ),
            (
                "qianchuan_live",
                ["直播计划名称", "计划ID", "投放状态"],
                ["直播间拉新计划", "live-plan-1002", "投放中"],
            ),
            (
                "campaigns",
                ["推广计划名称/ID", "计划ID", "投放状态"],
                ["全域成交计划\nID：full-plan-1003", "full-plan-1003", "暂停"],
            ),
        ]
        captured_at = int(time.time() * 1000)
        for index, (page_type, headers, row) in enumerate(cases):
            with self.subTest(headers=headers):
                saved = http_receiver.save_data("qianchuan", {
                    "schema_version": 3,
                    "page_type": page_type,
                    "captured_at": captured_at + index,
                    "quality": {"score": 95},
                    "tables": [{"headers": headers, "rows": [row]}],
                })
                coverage = saved["data"]["quality"]["server_plan_identity_coverage"]
                self.assertEqual(1, coverage["plan_table_count"])
                self.assertEqual(1, coverage["eligible_rows"])
                self.assertEqual(1, coverage["identified_rows"])
                self.assertEqual("complete", coverage["collection_state"])

    def test_prefixed_plan_id_headers_accept_ids_but_never_promote_names(self) -> None:
        for header in ("推广计划 ID", "广告计划编号", "商品计划ID", "直播计划 ID"):
            with self.subTest(header=header):
                self.assertEqual("plan_explicit_2001", http_receiver._plan_identifier({header: "plan_explicit_2001"}))
        for header in ("商品计划名称", "直播计划名称", "推广计划名称"):
            with self.subTest(header=header):
                self.assertEqual("", http_receiver._plan_identifier({header: "SUMMER_SALE"}))
                self.assertEqual("", http_receiver._plan_identifier({header: "12345678"}))

    def test_oauth_sync_selection_never_promotes_parent_subject(self) -> None:
        parent_key = "adacct_v1_parent123456789012345678"
        advertiser_key = "adacct_v1_child1234567890123456789"
        sync_result = {"accounts": [{"account_key": parent_key, "advertiser_count": 2}]}
        with patch.object(
            http_receiver, "load_agent_settings", return_value={"qianchuan_account_key": parent_key}
        ), patch.object(http_receiver, "save_agent_settings") as save_settings:
            finalized = http_receiver._finalize_oceanengine_sync_selection(dict(sync_result))
        self.assertEqual("", finalized["selected_account_key"])
        self.assertTrue(finalized["selection_required"])
        self.assertTrue(finalized["oauth_parent_selection_cleared"])
        save_settings.assert_called_once_with({"qianchuan_account_key": ""})

        with patch.object(
            http_receiver, "load_agent_settings", return_value={"qianchuan_account_key": advertiser_key}
        ), patch.object(http_receiver, "save_agent_settings") as save_settings:
            finalized = http_receiver._finalize_oceanengine_sync_selection(dict(sync_result))
        self.assertEqual(advertiser_key, finalized["selected_account_key"])
        self.assertFalse(finalized["selection_required"])
        self.assertFalse(finalized["oauth_parent_selection_cleared"])
        save_settings.assert_not_called()

    def test_plan_console_blocks_stale_suixintui_plan_until_resync(self) -> None:
        captured_at = int(time.time() * 1000) - (http_receiver.PLAN_CONSOLE_STALE_SECONDS + 5) * 1000
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "campaigns",
                "captured_at": captured_at,
                "account": {"key": "acct_stale1234", "label": "随心推账户", "confidence": "high"},
                "promotion_context": {
                    "promotion_mode": "suixintui",
                    "account_scope": {"store_id": "store-1", "account_id": "acct_stale1234"},
                },
                "quality": {"score": 90, "metric_count": 0, "row_count": 1},
                "tables": [{
                    "headers": ["计划ID", "计划名称", "投放状态", "日预算", "消耗", "支付 ROI", "成交订单"],
                    "rows": [["plan-stale", "过期随心推计划", "投放中", "500", "300", "0.60", "2"]],
                }],
            },
            store_key="store-1",
            account_key="acct_stale1234",
        )

        console = http_receiver.build_qianchuan_plan_console()
        self.assertEqual(console["freshness_status"], "stale")
        self.assertEqual(console["data_as_of_ms"], captured_at)
        self.assertEqual(console["oldest_data_as_of_ms"], captured_at)
        self.assertGreaterEqual(console["data_age_seconds"], http_receiver.PLAN_CONSOLE_STALE_SECONDS)
        self.assertEqual(console["summary"]["stale"], 1)
        self.assertEqual(console["summary"]["binding_ready"], 0)
        row = console["rows"][0]
        self.assertEqual(row["promotion_mode"], "suixintui")
        self.assertEqual(row["promotion_mode_label"], "随心推")
        self.assertTrue(row["stale"])
        self.assertGreaterEqual(row["data_age_seconds"], http_receiver.PLAN_CONSOLE_STALE_SECONDS)
        self.assertFalse(row["eligible_for_local_binding"])
        self.assertIn("计划数据已过期，请重新同步", row["binding_blockers"])

    def test_plan_console_treats_missing_capture_time_as_stale(self) -> None:
        record = {
            "source": "qianchuan",
            "page_type": "campaigns",
            "quality_score": 90,
            "captured_at_ms": 0,
            "account_key": "acct_missingtime",
            "account_label": "时间缺失账户",
            "promotion_context": {
                "promotion_mode": "standard",
                "account_scope": {"store_id": "store-1", "account_id": "acct_missingtime"},
            },
            "record": {
                "计划ID": "plan-missing-time",
                "计划名称": "时间缺失计划",
                "投放状态": "投放中",
                "日预算": "500",
            },
        }

        def table_records(source, _page_types):
            return [record] if source == "qianchuan" else []

        with patch.object(http_receiver, "_table_records", side_effect=table_records), patch.object(http_receiver, "build_plan_recommendations", return_value=[]):
            console = http_receiver.build_qianchuan_plan_console()

        self.assertEqual(console["freshness_status"], "stale")
        self.assertIsNone(console["data_as_of_ms"])
        self.assertIsNone(console["oldest_data_as_of_ms"])
        self.assertIsNone(console["data_age_seconds"])
        self.assertEqual(console["summary"]["stale"], 1)
        row = console["rows"][0]
        self.assertTrue(row["stale"])
        self.assertIsNone(row["data_age_seconds"])
        self.assertFalse(row["eligible_for_local_binding"])
        self.assertIn("计划数据已过期，请重新同步", row["binding_blockers"])

    def test_plan_console_treats_excessive_future_capture_time_as_stale(self) -> None:
        future_ms = int(time.time() * 1000) + http_receiver.MAX_CAPTURE_FUTURE_SKEW_MS + 60_000
        record = {
            "source": "qianchuan",
            "page_type": "campaigns",
            "quality_score": 90,
            "captured_at_ms": future_ms,
            "account_key": "acct_futuretime",
            "account_label": "时间异常账户",
            "promotion_context": {
                "promotion_mode": "standard",
                "account_scope": {"store_id": "store-1", "account_id": "acct_futuretime"},
            },
            "record": {
                "计划ID": "plan-future",
                "计划名称": "未来时间计划",
                "投放状态": "投放中",
                "日预算": "500",
            },
        }

        def table_records(source, _page_types):
            return [record] if source == "qianchuan" else []

        with patch.object(http_receiver, "_table_records", side_effect=table_records), patch.object(http_receiver, "build_plan_recommendations", return_value=[]):
            console = http_receiver.build_qianchuan_plan_console()

        self.assertEqual("stale", console["freshness_status"])
        self.assertIsNone(console["data_as_of_ms"])
        self.assertIsNone(console["data_age_seconds"])
        row = console["rows"][0]
        self.assertTrue(row["future_timestamp"])
        self.assertTrue(row["stale"])
        self.assertIsNone(row["data_age_seconds"])
        self.assertFalse(row["eligible_for_local_binding"])
        self.assertIn("计划采集时间异常晚于本机，请校准时间并重新同步", row["binding_blockers"])

    def test_plan_console_includes_official_api_plans_as_read_only(self) -> None:
        captured_at = int(time.time() * 1000)
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "plans",
                "captured_at": captured_at,
                "channel": "official_api",
                "account": {"key": "acct_official1234", "label": "官方计划账户", "confidence": "high"},
                "quality": {"score": 100, "metric_count": 1, "row_count": 1},
                "platform_write_enabled": False,
                "tables": [{
                    "headers": [
                        "广告主ID", "计划ID", "计划名称", "状态", "学习期状态",
                        "平台低效", "诊断来源", "推广类型", "预算",
                    ],
                    "rows": [[
                        "raw-advertiser-9988", "official-plan-1", "官方随心推计划", "投放中",
                        "学习中", "平台标记低效", "官方 API", "随心推", "600",
                    ]],
                }],
            },
            store_key="store-official1",
            account_key="acct_official1234",
            trusted_origin="official_api_oauth_client",
        )

        console = http_receiver.build_qianchuan_plan_console()
        self.assertEqual(console["summary"]["total"], 1)
        self.assertFalse(console["platform_write_enabled"])
        self.assertFalse(console["automatic_batch_submit"])
        row = console["rows"][0]
        self.assertEqual(row["plan_id"], "official-plan-1")
        self.assertEqual(row["promotion_mode"], "suixintui")
        self.assertEqual(row["promotion_mode_label"], "随心推")
        self.assertEqual(row["account_key"], "acct_official1234")
        self.assertNotEqual(row["account_key"], "raw-advertiser-9988")
        self.assertTrue(row["read_only"])
        self.assertFalse(row["stale"])
        self.assertTrue(row["eligible_for_local_binding"])
        self.assertEqual(row["learning_phase"], "learning")
        self.assertEqual(row["learning_status_label"], "学习中")
        self.assertEqual(row["platform_low_efficiency"], "flagged")
        self.assertEqual(row["platform_low_efficiency_label"], "平台标记低效")
        self.assertEqual(row["diagnostic_source"], "official_api")
        self.assertEqual(row["diagnostic_source_label"], "官方 API")

    def test_plan_console_overlays_official_health_without_erasing_web_metrics(self) -> None:
        now_ms = int(time.time() * 1000)
        account = {"key": "acct_merge1234", "label": "双源账户", "confidence": "high"}
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "campaigns",
                "captured_at": now_ms - 1_000,
                "account": account,
                "quality": {"score": 92, "row_count": 1},
                "tables": [{
                    "headers": ["计划ID", "计划名称", "状态", "推广类型", "日预算", "消耗", "支付 ROI", "成交订单"],
                    "rows": [["merge-plan-1", "双源合并计划", "投放中", "标准推广", "500", "321", "1.23", "8"]],
                }],
            },
            store_key="store-merge1",
            account_key="acct_merge1234",
        )
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "plans",
                "captured_at": now_ms,
                "account": account,
                "quality": {"score": 100, "row_count": 1},
                "tables": [{
                    "headers": ["计划ID", "计划名称", "状态", "推广类型", "预算", "学习期状态", "平台低效", "诊断来源"],
                    "rows": [["merge-plan-1", "双源合并计划", "投放中", "标准推广", "900", "学习中", "平台标记低效", "官方 API"]],
                }],
            },
            store_key="store-merge1",
            account_key="acct_merge1234",
            trusted_origin="official_api_oauth_client",
        )

        console = http_receiver.build_qianchuan_plan_console()
        self.assertEqual(1, console["summary"]["total"])
        row = console["rows"][0]
        self.assertEqual("product", row["plan_type"])
        self.assertEqual(500.0, row["budget"])
        self.assertEqual(321.0, row["spend"])
        self.assertEqual(1.23, row["roi"])
        self.assertEqual(8.0, row["orders"])
        self.assertEqual(now_ms - 1_000, row["captured_at_ms"])
        self.assertEqual("learning", row["learning_phase"])
        self.assertEqual("flagged", row["platform_low_efficiency"])
        self.assertEqual("official_api", row["diagnostic_source"])

    def test_qianchuan_budget_draft_requires_identity_and_supports_local_audit(self) -> None:
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "campaigns",
                "captured_at": int(time.time() * 1000),
                "account": {"key": "acct_safe1234", "label": "主投放账号", "confidence": "high"},
                "quality": {"score": 90, "metric_count": 0, "row_count": 1},
                "tables": [
                    {
                        "headers": ["计划ID", "计划名称", "日预算", "消耗", "支付 ROI", "成交订单"],
                        "rows": [["plan_987654", "夏季直播计划", "500", "300", "0.60", "2"]],
                    }
                ],
            },
            store_key="store-safe1234",
            account_key="acct_safe1234",
        )
        item = http_receiver.build_plan_recommendations()[0]
        draft = item["action_params"]
        self.assertTrue(draft["can_confirm"])
        self.assertFalse(draft["can_execute"])
        self.assertEqual(draft["target_ref"]["id"], "plan_987654")
        self.assertEqual(draft["change"]["target_value"], 400)

        confirmed = http_receiver.confirm_action_draft(draft)
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertFalse(http_receiver.get_action_audit()["execution_enabled"])
        self.assertEqual(http_receiver.get_action_audit()["summary"]["executed"], 0)
        self.assertEqual(http_receiver.confirm_action_draft(draft)["action_id"], confirmed["action_id"])

        http_receiver.save_agent_settings({"execution_mode": "supervised"})
        with self.assertRaisesRegex(ValueError, "计划采集范围尚未通过"):
            http_receiver.start_execution_preflight(confirmed["action_id"])

        cancelled = http_receiver.cancel_confirmed_action(confirmed["action_id"])
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(http_receiver.get_action_audit()["summary"]["cancelled"], 1)

    def test_client_forged_action_with_valid_public_hash_is_rejected(self) -> None:
        http_receiver._remember_store_identity({"key": "store-signed-action", "confidence": "high"})
        http_receiver.save_agent_settings({"store_key": "store-signed-action"})
        draft = http_receiver.build_action_draft(
            operation_type="adjust_budget",
            operation_label="降低预算 20%",
            target_kind="qianchuan_plan",
            target_id="plan_signed01",
            target_name="签名保护计划",
            account_key="acct_signed01",
            account_label="签名保护账号",
            field="预算",
            current_value=500.0,
            target_value=400.0,
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=int(time.time() * 1000),
            quality_score=90,
            confidence="high",
            promotion_context={
                "promotion_mode": "standard",
                "account_scope": {"store_id": "store-signed-action", "account_id": "acct_signed01"},
                "strategy_id": "plan_signed01",
                "metric_contract": {"definition": "pay_roi", "version": "v1"},
                "data_quality": {"confidence": "high", "freshness_seconds": 0, "completeness": 1.0},
            },
        )
        forged = json.loads(json.dumps(draft))
        forged["change"]["target_value"] = 50.0
        forged["target_value"] = 50.0
        forged_hash = action_integrity_hash(forged)
        forged["integrity_hash"] = forged_hash
        forged["action_id"] = forged_hash[:24]
        forged["idempotency_key"] = f"dian-action-{forged_hash[:32]}"
        with self.assertRaisesRegex(ValueError, "当前本地 Agent 签发"):
            http_receiver.confirm_action_draft(forged)

        confirmed = http_receiver.confirm_action_draft(draft)
        self.assertEqual("confirmed", confirmed["state"])

    def test_same_plan_cannot_open_overlapping_actions(self) -> None:
        http_receiver._remember_store_identity({"key": "store-plan-lock", "confidence": "high"})
        http_receiver.save_agent_settings({"store_key": "store-plan-lock"})
        common = {
            "operation_type": "adjust_budget",
            "operation_label": "降低预算",
            "target_kind": "qianchuan_plan",
            "target_id": "plan_lock001",
            "target_name": "观察期锁计划",
            "account_key": "acct_planlock",
            "account_label": "计划锁账号",
            "field": "预算",
            "current_value": 500.0,
            "source": "qianchuan",
            "page_type": "campaigns",
            "captured_at_ms": int(time.time() * 1000),
            "quality_score": 90,
            "confidence": "high",
            "evidence": {"spend": 500.0, "roi": 0.7, "orders": 3.0},
            "promotion_context": {
                "promotion_mode": "standard",
                "account_scope": {"store_id": "store-plan-lock", "account_id": "acct_planlock"},
                "strategy_id": "plan_lock001",
                "metric_contract": {"definition": "pay_roi", "version": "v1"},
                "data_quality": {"confidence": "high", "freshness_seconds": 0, "completeness": 1.0},
            },
        }
        first = http_receiver.build_action_draft(**common, target_value=400.0)
        second = http_receiver.build_action_draft(**common, target_value=425.0)
        http_receiver.confirm_action_draft(first)
        with self.assertRaisesRegex(ValueError, "观察中的动作"):
            http_receiver.confirm_action_draft(second)

    def test_strategy_simulation_compares_policies_without_execution(self) -> None:
        queue_report = {
            "items": [
                {
                    "plan": "高风险计划",
                    "bucket": "must_handle",
                    "risk_score": 82,
                    "estimated_savings_low": 60,
                    "estimated_savings_high": 120,
                    "evidence": {"orders": 2},
                    "action_params": {"change": {"current_value": 500, "target_value": 400}},
                },
                {
                    "plan": "中风险计划",
                    "bucket": "must_handle",
                    "risk_score": 62,
                    "estimated_savings_low": 30,
                    "estimated_savings_high": 60,
                    "evidence": {"orders": 1},
                    "action_params": {"change": {"current_value": 300, "target_value": 240}},
                },
            ]
        }
        report = http_receiver.build_strategy_simulation(queue_report)
        scenarios = {item["key"]: item for item in report["scenarios"]}
        self.assertFalse(report["execution_enabled"])
        self.assertEqual(3, len(scenarios))
        self.assertEqual(2, scenarios["protect_roi"]["selected_plan_count"])
        self.assertEqual(1, scenarios["balanced"]["selected_plan_count"])
        self.assertEqual(80.0, scenarios["balanced"]["estimated_budget_impact"])
        self.assertTrue(all(not item["can_execute"] for item in scenarios.values()))
        decision = http_receiver.save_strategy_decision("balanced")
        self.assertEqual("balanced", decision["policy_key"])
        self.assertFalse(decision["execution_enabled"])
        self.assertEqual(decision["decision_id"], http_receiver.load_strategy_decisions()["current"]["decision_id"])
        with self.assertRaisesRegex(ValueError, "策略类型无效"):
            http_receiver.save_strategy_decision("automatic_batch")

    def test_missing_budget_stays_blocked_and_never_falls_back_to_pause(self) -> None:
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "campaigns",
                "captured_at": int(time.time() * 1000),
                "account": {"key": "acct_safe1234", "label": "主投放账号"},
                "quality": {"score": 90, "row_count": 1},
                "tables": [
                    {
                        "headers": ["计划ID", "计划名称", "消耗", "支付 ROI", "成交订单"],
                        "rows": [["plan_987654", "无预算字段计划", "300", "0", "0"]],
                    }
                ],
            },
            store_key="store-safe1234",
            account_key="acct_safe1234",
        )
        draft = http_receiver.build_plan_recommendations()[0]["action_params"]
        self.assertEqual(draft["operation_type"], "adjust_budget")
        self.assertFalse(draft["can_confirm"])
        self.assertIn("CURRENT_VALUE_MISSING", {item["code"] for item in draft["blocked_reasons"]})

    def test_automation_readiness_builds_candidate_queue_without_execution(self) -> None:
        base = {
            "operation_type": "adjust_budget",
            "operation_label": "降低预算 20%",
            "target_kind": "qianchuan_plan",
            "target_id": "plan-ready-1",
            "target_name": "准备度测试计划",
            "account_key": "account-ready-1",
            "account_label": "准备度测试账号",
            "field": "预算",
            "current_value": 500.0,
            "target_value": 400.0,
            "source": "qianchuan",
            "page_type": "campaigns",
            "captured_at_ms": 1_000_000,
            "quality_score": 90,
            "confidence": "high",
            "now_ms": 1_001_000,
            "promotion_context": {
                "promotion_mode": "standard",
                "account_scope": {
                    "store_id": "store-ready-1",
                    "account_id": "account-ready-1",
                    "binding_status": "confirmed",
                },
                "strategy_id": "plan-ready-1",
                "metric_contract": {"definition": "pay_roi", "version": "qianchuan-visible-pay-roi-v1"},
                "cost_ledger": {"ad_spend": 100.0},
                "result_ledger": {"orders": 2.0},
                "data_quality": {"confidence": "high", "completeness": 1.0, "freshness_seconds": 10},
            },
        }
        confirmable = http_receiver.build_action_draft(**base)
        preflight = http_receiver.transition_action(confirmable, "confirmed")
        pause = http_receiver.build_action_draft(**{
            **base,
            "operation_type": "pause_plan",
            "operation_label": "暂停单计划",
            "field": "投放状态",
            "current_value": "投放中",
            "target_value": "暂停",
            "page_type": "qianchuan_live",
        })
        blocked = http_receiver.build_action_draft(**{**base, "target_id": "", "account_key": ""})
        report = http_receiver.build_automation_readiness(
            [
                {"plan": "待授权计划", "level": "high", "action_params": confirmable},
                {"plan": "已授权计划", "level": "high", "action_params": preflight},
                {"plan": "直播暂停计划", "level": "high", "action_params": pause},
                {"plan": "缺少身份计划", "level": "warning", "action_params": blocked},
                {"plan": "人工建议", "level": "warning"},
            ]
        )
        self.assertFalse(report["execution_enabled"])
        self.assertEqual(5, report["summary"]["total"])
        self.assertEqual(1, report["summary"]["preflight_ready"])
        self.assertEqual(2, report["summary"]["confirmable"])
        self.assertEqual(1, report["summary"]["blocked"])
        self.assertEqual(1, report["summary"]["manual_only"])
        self.assertEqual("preflight_ready", report["items"][0]["status"])

    def test_automation_readiness_blocks_real_shape_without_execution_contract(self) -> None:
        draft = http_receiver.build_action_draft(
            operation_type="adjust_budget",
            operation_label="降低预算 20%",
            target_kind="qianchuan_plan",
            target_id="plan-real-shape",
            target_name="真实采集形状",
            account_key="account-real-shape",
            account_label="测试账号",
            field="预算",
            current_value=500.0,
            target_value=400.0,
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=1_000_000,
            quality_score=90,
            confidence="high",
            now_ms=1_001_000,
            promotion_context={
                "promotion_mode": "standard",
                "account_scope": {"account_id": "account-real-shape"},
                "data_quality": {"confidence": "high", "completeness": 1.0},
            },
        )
        report = http_receiver.build_automation_readiness([
            {"plan": "真实采集形状", "level": "high", "action_params": draft},
        ])
        self.assertEqual(1, report["summary"]["blocked"])
        self.assertEqual(0, report["summary"]["confirmable"])
        self.assertEqual("PROMOTION_SCOPE_UNVERIFIED", report["items"][0]["promotion_guard"]["code"])
        self.assertFalse(report["items"][0]["can_enter_preflight"])

    def test_pause_plan_uses_text_status_and_verifies_paused_readback(self) -> None:
        self._bind_execution_scope("store-pause", "acct_pause1")
        http_receiver.save_agent_settings({
            "store_key": "store-pause",
            "qianchuan_account_key": "acct_pause1",
        })
        captured_at = int(time.time() * 1000)
        draft = http_receiver._action_params_for_plan(
            "暂停验证计划",
            "stop_loss",
            {
                "spend": 300.0,
                "roi": 0.5,
                "orders": 2.0,
                "_record": {
                    "计划ID": "plan_pause1",
                    "计划名称": "暂停验证计划",
                    "日预算": "500",
                    "投放状态": "正常投放中",
                },
            },
            {
                "source": "qianchuan",
                "page_type": "campaigns",
                "captured_at_ms": captured_at,
                "quality_score": 90,
                "account_key": "acct_pause1",
                "account_label": "暂停验证账号",
                "promotion_context": execution_promotion_context(
                    "store-pause", "acct_pause1", "plan_pause1"
                ),
            },
            "high",
        )
        self.assertEqual("pause_plan", draft["operation_type"])
        self.assertEqual("投放中", draft["change"]["current_value"])
        self.assertTrue(draft["can_confirm"])

        confirmed = http_receiver.confirm_action_draft(draft)
        executing = http_receiver.transition_action(confirmed, "executing", allow_execution=True)
        succeeded = http_receiver.transition_action(executing, "succeeded", allow_execution=True)
        authorization_id = "d" * 32
        succeeded["execution_authorization_id"] = authorization_id
        succeeded["execution_reported_at_ms"] = captured_at + 1_000
        succeeded["execution_baseline"] = {
            "captured_at_ms": captured_at,
            "quality_score": 90,
            "current_value": "投放中",
            "spend": 300.0,
            "roi": 0.5,
            "orders": 2.0,
            "metric_contract": {"definition": "pay_roi", "version": "v1", "period": "test-attribution-window-v1"},
            "document_instance_id": "doc-pause-baseline-0001",
            "navigation_started_at_ms": captured_at - 1_000,
        }
        http_receiver._atomic_json_write(
            http_receiver._action_audit_path(),
            {"schema_version": 1, "updated_at": http_receiver._now_label(), "execution_enabled": True, "actions": [succeeded]},
        )
        http_receiver.save_data(
            "qianchuan",
            {
                "schema_version": 2,
                "page_type": "campaigns",
                "captured_at": captured_at + 2_000,
                "document_instance_id": "doc-pause-readback-0002",
                "navigation_started_at_ms": captured_at + 1_500,
                "store": {"key": confirmed["proposal_scope"]["store_key"], "confidence": "high"},
                "account": {"key": "acct_pause1", "store_key": confirmed["proposal_scope"]["store_key"], "label": "暂停验证账号", "confidence": "high"},
                "promotion_context": execution_promotion_context(
                    "store-pause", "acct_pause1", "plan_pause1"
                ),
                "quality": {"score": 90, "row_count": 1},
                "tables": [{
                    "headers": ["计划ID", "计划名称", "日预算", "投放状态", "消耗", "支付 ROI", "成交订单"],
                    "rows": [["plan_pause1", "暂停验证计划", "500", "已暂停", "300", "0.5", "2"]],
                }],
            },
        )
        readback_token = "d" * 64
        canonical_readback = http_receiver.load_data(
            "qianchuan", "campaigns", account_key="acct_pause1"
        )
        self._write_execution_snapshot_fixture(
            action_id=confirmed["action_id"],
            authorization_id=authorization_id,
            readback_token=readback_token,
            purpose="execution_readback",
            data=canonical_readback["data"],
        )
        self.assertEqual([], http_receiver.build_execution_effectiveness_report()["items"])
        verification = http_receiver.verify_execution_result(
            confirmed["action_id"], readback_token
        )
        self.assertTrue(verification["verified"])
        self.assertEqual("暂停", verification["readback"]["delivery_status"])
        snapshot_due_at = succeeded["execution_reported_at_ms"] + 2 * 60 * 60 * 1000 + 1
        waiting_for_due_readback = http_receiver.build_execution_effectiveness_report(now_ms=snapshot_due_at)
        self.assertEqual("needs_reread", waiting_for_due_readback["items"][0]["status"])
        with patch.object(http_receiver.time, "time", return_value=snapshot_due_at / 1000):
            http_receiver.save_data(
                "qianchuan",
                {
                    "schema_version": 2,
                    "page_type": "campaigns",
                    "captured_at": snapshot_due_at,
                    "document_instance_id": "doc-pause-effect-0003",
                    "navigation_started_at_ms": snapshot_due_at - 500,
                    "store": {"key": confirmed["proposal_scope"]["store_key"], "confidence": "high"},
                    "account": {"key": "acct_pause1", "store_key": confirmed["proposal_scope"]["store_key"], "label": "暂停验证账号", "confidence": "high"},
                    "promotion_context": execution_promotion_context(
                        "store-pause", "acct_pause1", "plan_pause1"
                    ),
                    "quality": {"score": 90, "row_count": 1},
                    "tables": [{
                        "headers": ["计划ID", "计划名称", "日预算", "投放状态", "消耗", "支付 ROI", "成交订单"],
                        "rows": [["plan_pause1", "暂停验证计划", "500", "已暂停", "300", "0.5", "2"]],
                    }],
                },
            )
        report = http_receiver.build_execution_effectiveness_report(
            now_ms=snapshot_due_at,
        )
        self.assertEqual("effective", report["items"][0]["status"])

    def test_effectiveness_gate_requires_quality_time_window_and_complete_metric_contract(self) -> None:
        executed_at_ms = 1_000_000
        due_at_ms = executed_at_ms + 30 * 60 * 1000
        now_ms = due_at_ms + 1_000
        complete_contract = {
            "definition": "pay_roi",
            "version": "v1",
            "period": "same-day",
        }
        action = {
            "action_id": "e" * 24,
            "state": "verified",
            "operation_type": "adjust_budget",
            "execution_reported_at_ms": executed_at_ms,
            "target_ref": {"account_key": "acct-effect", "name": "效果门禁计划"},
            "change": {"current_value": 500.0, "target_value": 400.0},
            "execution_baseline": {
                "captured_at_ms": executed_at_ms - 1_000,
                "quality_score": 90,
                "spend": 1_000.0,
                "roi": 1.0,
                "orders": 10.0,
                "current_value": 500.0,
                "metric_contract": complete_contract,
            },
        }
        valid_after = {
            "captured_at_ms": due_at_ms,
            "quality_score": 90,
            "account_identity_matches": True,
            "store_identity_matches": True,
            "spend": 1_200.0,
            "roi": 1.2,
            "orders": 12.0,
            "current_value": 400.0,
            "metric_contract": complete_contract,
        }

        def status_for(after: dict, baseline_contract: dict | None = None) -> str:
            candidate = json.loads(json.dumps(action))
            if baseline_contract is not None:
                candidate["execution_baseline"]["metric_contract"] = baseline_contract
            with patch.object(http_receiver, "load_action_audit", return_value={"actions": [candidate]}), patch.object(
                http_receiver, "_find_plan_readback", return_value=after
            ):
                return http_receiver.build_execution_effectiveness_report(now_ms=now_ms)["items"][0]["status"]

        self.assertEqual("effective", status_for(valid_after))
        self.assertEqual("needs_reread", status_for({**valid_after, "quality_score": 69}))
        self.assertEqual("needs_reread", status_for({
            **valid_after,
            "captured_at_ms": now_ms + http_receiver.MAX_CAPTURE_FUTURE_SKEW_MS + 1,
        }))
        self.assertEqual("needs_reread", status_for({**valid_after, "captured_at_ms": due_at_ms - 1}))
        self.assertEqual("needs_reread", status_for({
            **valid_after,
            "metric_contract": {"definition": "pay_roi", "version": "v1"},
        }))
        self.assertEqual("inconclusive", status_for({
            **valid_after,
            "metric_contract": {**complete_contract, "period": "seven-day"},
        }))
        self.assertEqual("inconclusive", status_for(
            valid_after,
            {"definition": "pay_roi", "version": "v1"},
        ))
        self.assertEqual("inconclusive", status_for(
            {**valid_after, "metric_contract": {}},
            {},
        ))

    def test_budget_rollback_inherits_live_page_plan_type_and_collection_scope(self) -> None:
        now_ms = int(time.time() * 1000)
        account_key = "acct-live-rollback"
        store_key = "store-live-rollback"
        scope_key = http_receiver._plan_collection_scope_key(account_key, "standard", "live")
        http_receiver.save_agent_settings({
            "store_key": store_key,
            "qianchuan_account_key": account_key,
        })
        original = http_receiver.build_action_draft(
            operation_type="adjust_budget",
            operation_label="降低预算 20%",
            target_kind="qianchuan_plan",
            target_id="plan-live-rollback",
            target_name="直播回滚计划",
            account_key=account_key,
            account_label="直播回滚账户",
            field="预算",
            current_value=500.0,
            target_value=400.0,
            source="qianchuan",
            page_type="qianchuan_live",
            captured_at_ms=now_ms - 2_000,
            quality_score=90,
            confidence="high",
            evidence={
                "collection_scope_required": True,
                "plan_type": "live",
                "collection_scope_key": scope_key,
            },
            promotion_context=execution_promotion_context(
                store_key, account_key, "plan-live-rollback"
            ),
        )
        original["state"] = "verified"
        original["execution_reported_at_ms"] = now_ms - 1_000
        readback = {
            "captured_at_ms": now_ms,
            "quality_score": 90,
            "page_type": "qianchuan_live",
            "plan_type": "live",
            "collection_scope_key": scope_key,
            "current_value": 400.0,
            "spend": 500.0,
            "roi": 0.8,
            "orders": 4.0,
        }
        with patch.object(http_receiver, "load_action_audit", return_value={"actions": [original]}), patch.object(
            http_receiver, "_find_plan_readback", return_value=readback
        ):
            rollback = http_receiver.create_budget_rollback_draft(original["action_id"])

        evidence = rollback["evidence_ref"]
        self.assertEqual("qianchuan_live", evidence["page_type"])
        self.assertEqual("live", evidence["plan_type"])
        self.assertEqual(scope_key, evidence["collection_scope_key"])
        self.assertTrue(evidence["collection_scope_required"])

    def test_supervised_preflight_requires_new_readback_and_can_be_stopped(self) -> None:
        self._bind_execution_scope("store-1", "acct_preflight1")
        http_receiver.save_agent_settings({
            "execution_mode": "supervised",
            "store_key": "store-1",
            "qianchuan_account_key": "acct_preflight1",
        })
        captured_at = int(time.time() * 1000)
        snapshot = {
            "schema_version": 2,
            "page_type": "campaigns",
            "captured_at": captured_at,
            "document_instance_id": "doc-preflight-baseline-0001",
            "navigation_started_at_ms": captured_at - 1_000,
            "store": {"key": "store-1", "confidence": "high"},
            "account": {
                "key": "acct_preflight1",
                "store_key": "store-1",
                "label": "止损试运行账号",
                "confidence": "high",
            },
            "promotion_context": execution_promotion_context(
                "store-1", "acct_preflight1", "plan_preflight1"
            ),
            "quality": {"score": 90, "row_count": 1},
            "tables": [
                {
                    "headers": ["计划ID", "计划名称", "日预算", "消耗", "支付 ROI", "成交订单"],
                    "rows": [["plan_preflight1", "止损检查计划", "500", "300", "0.60", "2"]],
                }
            ],
        }
        http_receiver.save_data("qianchuan", snapshot)
        draft = http_receiver.build_action_draft(
            operation_type="adjust_budget",
            operation_label="降低预算 20%",
            target_kind="qianchuan_plan",
            target_id="plan_preflight1",
            promotion_context={"promotion_mode": "standard", "account_scope": {"store_id": "store-1", "account_id": "acct_preflight1"}, "strategy_id": "plan_preflight1", "metric_contract": {"definition": "pay_roi", "version": "v1", "period": "test-attribution-window-v1"}, "data_quality": {"confidence": "high", "freshness_seconds": 0, "completeness": 0.9}},
            target_name="止损检查计划",
            account_key="acct_preflight1",
            account_label="止损试运行账号",
            field="预算",
            current_value=500.0,
            target_value=400.0,
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=captured_at,
            quality_score=90,
            confidence="high",
            # Deliberately stale proposal evidence; the effect baseline must be
            # replaced by the fresh preflight reread below.
            evidence={"spend": 100.0, "roi": 0.20, "orders": 1.0},
        )
        confirmed = http_receiver.confirm_action_draft(draft)
        awaiting = http_receiver.start_execution_preflight(confirmed["action_id"])
        self.assertEqual("awaiting_reread", awaiting["state"])
        self.assertFalse(awaiting["execution_enabled"])

        snapshot["captured_at"] = awaiting["session"]["started_at_ms"] + 1000
        snapshot["document_instance_id"] = "doc-preflight-hard-reread-0002"
        snapshot["navigation_started_at_ms"] = awaiting["session"]["started_at_ms"] + 500
        http_receiver.save_data("qianchuan", snapshot)
        ready = http_receiver.build_execution_preflight_report()
        self.assertEqual("ready_for_final_confirmation", ready["state"])
        self.assertTrue(all(item["passed"] for item in ready["checks"]))
        self.assertFalse(ready["write_enabled"])

        with self.assertRaisesRegex(ValueError, "确认口令不一致"):
            http_receiver.authorize_execution_preflight(ready["session"]["session_id"], "确认")
        authorized = http_receiver.authorize_execution_preflight(
            ready["session"]["session_id"],
            "确认降低预算至400.0",
        )
        self.assertEqual("authorized", authorized["state"])
        self.assertRegex(authorized["session"]["authorization_id"], r"^[a-f0-9]{32}$")
        self.assertFalse(authorized["session"]["authorization_consumed"])
        self.assertFalse(authorized["execution_enabled"])
        receipt = {
            "authorization_id": authorized["session"]["authorization_id"],
            "submitted": True,
            "platform_success_observed": True,
            "operation_type": "adjust_budget",
            "store_key": "store-1",
            "account_key": "acct_preflight1",
            "plan_id": "plan_preflight1",
            "target_value": 400.0,
        }
        with self.assertRaisesRegex(ValueError, "尚未消费"):
            http_receiver.record_execution_result(confirmed["action_id"], receipt)

        preview = http_receiver.preview_execution_authorization(authorized["session"]["authorization_id"])
        self.assertEqual(confirmed["action_id"], preview["action_id"])
        self.assertEqual("supervised_submit", preview["execution_request"]["mode"])
        self.assertFalse(http_receiver.load_execution_preflight()["session"]["authorization_consumed"])

        authorization_id = authorized["session"]["authorization_id"]
        authorized_at_ms = int(authorized["session"]["authorized_at_ms"])
        final_snapshot = json.loads(json.dumps(snapshot))
        final_snapshot["captured_at"] = authorized_at_ms + 2
        final_snapshot["document_instance_id"] = "doc-preconsume-final-reread-0003"
        final_snapshot["navigation_started_at_ms"] = authorized_at_ms + 1
        preconsume_token = "b" * 64
        self._write_execution_snapshot_fixture(
            action_id=confirmed["action_id"],
            authorization_id=authorization_id,
            readback_token=preconsume_token,
            purpose="preconsume_baseline",
            data=final_snapshot,
        )
        consume_now_ms = authorized_at_ms + 10
        with patch.object(http_receiver.time, "time", return_value=consume_now_ms / 1000):
            http_receiver.refresh_authorized_execution_baseline(
                authorization_id, preconsume_token
            )
            consumed = http_receiver.consume_execution_authorization(authorization_id)
        self.assertEqual("authorization_consumed", consumed["state"])
        self.assertTrue(consumed["authorization_consumed"])
        locked = next(item for item in http_receiver.load_action_audit()["actions"] if item["action_id"] == confirmed["action_id"])
        self.assertEqual("executing", locked["state"])
        with self.assertRaisesRegex(ValueError, "只有已确认"):
            http_receiver.start_execution_preflight(confirmed["action_id"])
        self.assertEqual("supervised_submit", consumed["execution_request"]["mode"])
        self.assertEqual("plan_preflight1", consumed["execution_request"]["plan_id"])
        self.assertEqual(400.0, consumed["execution_request"]["target_value"])
        with self.assertRaisesRegex(ValueError, "已使用或已失效"):
            http_receiver.consume_execution_authorization(authorized["session"]["authorization_id"])
        with self.assertRaisesRegex(ValueError, "参数不一致"):
            http_receiver.record_execution_result(
                confirmed["action_id"],
                {**receipt, "plan_id": "plan_other"},
            )
        executed = http_receiver.record_execution_result(
            confirmed["action_id"],
            receipt,
        )
        self.assertEqual("executing", executed["state"])
        self.assertIn("必须先回读", executed["execution_note"])
        duplicate = http_receiver.record_execution_result(confirmed["action_id"], receipt)
        self.assertEqual(executed["execution_receipt"], duplicate["execution_receipt"])
        snapshot["captured_at"] = executed["execution_reported_at_ms"] + 1000
        snapshot["document_instance_id"] = "doc-execution-readback-0002"
        snapshot["navigation_started_at_ms"] = executed["execution_reported_at_ms"] + 500
        snapshot["tables"][0]["rows"][0][2] = "400"
        snapshot["tables"][0]["rows"][0][3] = "360"
        snapshot["tables"][0]["rows"][0][4] = "0.80"
        snapshot["tables"][0]["rows"][0][5] = "3"
        http_receiver.save_data("qianchuan", snapshot)
        execution_readback_token = "c" * 64
        self._write_execution_snapshot_fixture(
            action_id=confirmed["action_id"],
            authorization_id=authorization_id,
            readback_token=execution_readback_token,
            purpose="execution_readback",
            data=snapshot,
        )
        verification = http_receiver.verify_execution_result(
            confirmed["action_id"], execution_readback_token
        )
        self.assertTrue(verification["verified"])
        self.assertEqual("verified", verification["state"])
        self.assertEqual(1, http_receiver.get_action_audit()["summary"]["executed"])
        snapshot["captured_at"] = executed["execution_reported_at_ms"] + 2 * 60 * 60 * 1000 + 1
        with patch.object(http_receiver.time, "time", return_value=snapshot["captured_at"] / 1000):
            http_receiver.save_data("qianchuan", snapshot)
        effectiveness = http_receiver.build_execution_effectiveness_report(
            now_ms=snapshot["captured_at"],
        )
        self.assertEqual("effective", effectiveness["items"][0]["status"])
        self.assertEqual(0.6, effectiveness["items"][0]["before"]["roi"])
        self.assertEqual(300.0, effectiveness["items"][0]["before"]["spend"])
        self.assertEqual(0.8, effectiveness["items"][0]["after"]["roi"])
        with patch.object(http_receiver.time, "time", return_value=snapshot["captured_at"] / 1000):
            rollback = http_receiver.create_budget_rollback_draft(confirmed["action_id"])
            self.assertEqual("restore_budget", rollback["operation_type"])
            self.assertEqual(400.0, rollback["change"]["current_value"])
            self.assertEqual(500.0, rollback["change"]["target_value"])
            self.assertTrue(rollback["can_confirm"])
            rollback_confirmed = http_receiver.confirm_action_draft(rollback)
            rollback_preflight = http_receiver.start_execution_preflight(rollback_confirmed["action_id"])
        self.assertIn(rollback_preflight["state"], {"awaiting_reread", "ready_for_final_confirmation"})
        self.assertTrue(rollback_preflight["session"]["quota"]["recovery_exemption"])

    def test_unknown_execution_is_locked_until_fresh_target_readback(self) -> None:
        http_receiver._remember_qianchuan_account({
            "key": "acct_unknown1", "store_key": "store-1", "label": "回执不确定账号", "confidence": "high",
        })
        http_receiver._remember_store_identity(
            {"key": "store-1", "confidence": "high", "identity_source": "test"},
            "acct_unknown1",
        )
        http_receiver.save_agent_settings({
            "execution_mode": "supervised",
            "store_key": "store-1",
            "qianchuan_account_key": "acct_unknown1",
        })
        now_ms = int(time.time() * 1000)
        action = http_receiver.build_action_draft(
            operation_type="adjust_budget",
            operation_label="降低预算 20%",
            target_kind="qianchuan_plan",
            target_id="plan_unknown01",
            promotion_context={
                "promotion_mode": "standard",
                "account_scope": {"store_id": "store-1", "account_id": "acct_unknown1"},
                "strategy_id": "plan_unknown01",
                "metric_contract": {"definition": "pay_roi", "version": "v1"},
                "data_quality": {"confidence": "high", "freshness_seconds": 0, "completeness": 0.9},
            },
            target_name="回执不确定计划",
            account_key="acct_unknown1",
            account_label="回执不确定账号",
            field="预算",
            current_value=500.0,
            target_value=400.0,
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=now_ms,
            quality_score=90,
            confidence="high",
            evidence={"spend": 300.0, "roi": 0.6, "orders": 3.0},
        )
        confirmed = http_receiver.confirm_action_draft(action)
        authorization_id = "a" * 32
        final_readback_token = "e" * 64
        baseline = {
            "snapshot_ref": "qianchuan/campaigns/manual-test",
            "captured_at_ms": now_ms,
            "quality_score": 90,
            "store_key": confirmed["proposal_scope"]["store_key"],
            "account_key": "acct_unknown1",
            "plan_id": "plan_unknown01",
            "current_value": 500.0,
            "delivery_status": "投放中",
            "spend": 300.0,
            "roi": 0.6,
            "orders": 3.0,
            "metric_contract": {"definition": "pay_roi", "version": "v1"},
            "document_instance_id": "doc-unknown-baseline-0001",
            "navigation_started_at_ms": now_ms - 1_000,
            "readback_token": final_readback_token,
            "readback_purpose": "preconsume_baseline",
        }
        confirmed["execution_baseline"] = baseline
        confirmed["execution_baseline_hash"] = http_receiver._execution_baseline_hash(baseline)
        confirmed["final_preconsume_readback_token"] = final_readback_token
        confirmed["final_preconsume_authorization_id"] = authorization_id
        confirmed["final_preconsume_reread_at_ms"] = now_ms
        http_receiver._atomic_json_write(
            http_receiver._action_audit_path(),
            {"schema_version": 1, "updated_at": http_receiver._now_label(), "execution_enabled": False, "actions": [confirmed]},
        )
        http_receiver._save_execution_preflight({
            "session_id": "b" * 24,
            "action_id": confirmed["action_id"],
            "action_integrity_hash": confirmed["integrity_hash"],
            "action_server_signature": confirmed["server_signature"],
            "store_key": confirmed["proposal_scope"]["store_key"],
            "account_key": confirmed["target_ref"]["account_key"],
            "binding_generation": int(confirmed["proposal_scope"]["binding_generation"]),
            "execution_baseline_hash": confirmed["execution_baseline_hash"],
            "state": "authorized",
            "authorization_id": authorization_id,
            "authorized_at_ms": now_ms - 1_000,
            "authorized_baseline_document_instance_id": "doc-unknown-authorized-0000",
            "final_preconsume_document_instance_id": "doc-unknown-baseline-0001",
            "final_preconsume_readback_token": final_readback_token,
            "final_preconsume_authorization_id": authorization_id,
            "final_preconsume_reread_at_ms": now_ms,
            "authorization_consumed": False,
            "authorization_expires_at_ms": now_ms + 60_000,
            "expires_at_ms": now_ms + 180_000,
        })

        consumed = http_receiver.consume_execution_authorization(authorization_id)
        self.assertEqual("executing", next(item for item in http_receiver.load_action_audit()["actions"] if item["action_id"] == confirmed["action_id"])["state"])
        unknown = http_receiver.record_execution_result(confirmed["action_id"], {
            "authorization_id": authorization_id,
            "submitted": True,
            "platform_success_observed": False,
            "operation_type": "adjust_budget",
            "store_key": "store-1",
            "account_key": "acct_unknown1",
            "plan_id": "plan_unknown01",
            "target_value": 400.0,
        })
        self.assertEqual("executing", unknown["state"])
        with self.assertRaisesRegex(ValueError, "结果尚未确认"):
            http_receiver.confirm_action_draft(action)

        snapshot = {
            "schema_version": 2,
            "page_type": "campaigns",
            "captured_at": int(unknown["execution_started_at_ms"]) + 1_000,
            "document_instance_id": "doc-unknown-readback-0002",
            "navigation_started_at_ms": int(unknown["execution_started_at_ms"]) + 500,
            "store": {"key": "store-1"},
            "account": {"key": "acct_unknown1", "store_key": "store-1", "label": "回执不确定账号"},
            "promotion_context": execution_promotion_context(
                "store-1", "acct_unknown1", "plan_unknown01"
            ),
            "quality": {"score": 90, "row_count": 1},
            "tables": [{
                "headers": ["计划ID", "计划名称", "日预算", "消耗", "支付 ROI", "成交订单"],
                "rows": [["plan_unknown01", "回执不确定计划", "400", "330", "0.70", "4"]],
            }],
        }
        http_receiver.save_data("qianchuan", snapshot)
        readback_token = "f" * 64
        self._write_execution_snapshot_fixture(
            action_id=confirmed["action_id"],
            authorization_id=authorization_id,
            readback_token=readback_token,
            purpose="execution_readback",
            data=snapshot,
        )
        verified = http_receiver.verify_execution_result(
            confirmed["action_id"], readback_token
        )
        self.assertTrue(verified["verified"])
        self.assertEqual("verified", verified["state"])

    def test_original_value_confirmations_require_distinct_documents_and_manual_reconcile_can_resolve(self) -> None:
        self._bind_execution_scope("store-1", "acct_eventual1")
        http_receiver.save_agent_settings({
            "execution_mode": "supervised",
            "store_key": "store-1",
            "qianchuan_account_key": "acct_eventual1",
        })
        now_ms = int(time.time() * 1000)
        action = http_receiver.build_action_draft(
            operation_type="adjust_budget",
            operation_label="降低预算 20%",
            target_kind="qianchuan_plan",
            target_id="plan_eventual01",
            promotion_context={
                "promotion_mode": "standard",
                "account_scope": {"store_id": "store-1", "account_id": "acct_eventual1"},
                "strategy_id": "plan_eventual01",
                "metric_contract": {"definition": "pay_roi", "version": "v1"},
                "data_quality": {"confidence": "high", "freshness_seconds": 0, "completeness": 0.9},
            },
            target_name="最终一致性计划",
            account_key="acct_eventual1",
            account_label="最终一致性账号",
            field="预算",
            current_value=500.0,
            target_value=400.0,
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=now_ms,
            quality_score=90,
            confidence="high",
            evidence={"spend": 300.0, "roi": 0.6, "orders": 3.0},
        )
        confirmed = http_receiver.confirm_action_draft(action)
        authorization_id = "6" * 32
        executing = http_receiver.transition_action(confirmed, "executing", allow_execution=True)
        executing["execution_authorization_id"] = authorization_id
        executing["execution_reported_at_ms"] = now_ms
        executing["execution_baseline"] = {
            "document_instance_id": "doc-eventual-baseline-0001",
            "navigation_started_at_ms": now_ms - 1_000,
        }
        http_receiver._atomic_json_write(
            http_receiver._action_audit_path(),
            {"schema_version": 1, "updated_at": http_receiver._now_label(), "execution_enabled": True, "actions": [executing]},
        )

        def save_plan_value(
            captured_at_ms: int,
            document_instance_id: str,
            budget: str = "500",
        ) -> str:
            with patch.object(http_receiver.time, "time", return_value=captured_at_ms / 1000):
                http_receiver.save_data("qianchuan", {
                    "schema_version": 2,
                    "page_type": "campaigns",
                    "captured_at": captured_at_ms,
                    "document_instance_id": document_instance_id,
                    "navigation_started_at_ms": captured_at_ms - 100,
                    "store": {"key": "store-1", "confidence": "high"},
                    "account": {
                        "key": "acct_eventual1",
                        "store_key": "store-1",
                        "label": "最终一致性账号",
                        "confidence": "high",
                    },
                    "promotion_context": execution_promotion_context(
                        "store-1", "acct_eventual1", "plan_eventual01"
                    ),
                    "quality": {"score": 90, "row_count": 1},
                    "tables": [{
                        "headers": ["计划ID", "计划名称", "日预算", "消耗", "支付 ROI", "成交订单"],
                        "rows": [["plan_eventual01", "最终一致性计划", budget, "320", "0.62", "3"]],
                    }],
                })
            readback_token = f"{captured_at_ms:064x}"
            canonical = http_receiver.load_data(
                "qianchuan", "campaigns", account_key="acct_eventual1"
            )
            self._write_execution_snapshot_fixture(
                action_id=confirmed["action_id"],
                authorization_id=authorization_id,
                readback_token=readback_token,
                purpose="execution_readback",
                data=canonical["data"],
            )
            return readback_token

        first_at = now_ms + 2_000
        first_token = save_plan_value(first_at, "doc-eventual-shared-0001")
        with patch.object(http_receiver.time, "time", return_value=first_at / 1000):
            first = http_receiver.verify_execution_result(
                confirmed["action_id"], first_token
            )
        self.assertEqual("executing", first["state"])
        self.assertTrue(first["execution_unknown"])
        self.assertFalse(first["safe_to_retry"])
        self.assertEqual(1, first["original_value_confirmation_count"])

        repeated_at = now_ms + 8_000
        repeated_token = save_plan_value(repeated_at, "doc-eventual-shared-0001")
        with patch.object(http_receiver.time, "time", return_value=repeated_at / 1000):
            repeated = http_receiver.verify_execution_result(
                confirmed["action_id"], repeated_token
            )
        self.assertEqual(1, repeated["original_value_confirmation_count"])

        second_at = now_ms + 15_000
        second_token = save_plan_value(second_at, "doc-eventual-distinct-0002")
        with patch.object(http_receiver.time, "time", return_value=second_at / 1000):
            second = http_receiver.verify_execution_result(
                confirmed["action_id"], second_token
            )
        self.assertEqual("executing", second["state"])
        self.assertFalse(second["safe_to_retry"])
        self.assertEqual(2, second["original_value_confirmation_count"])

        mature_at = now_ms + 30_000
        mature_token = save_plan_value(mature_at, "doc-eventual-distinct-0003")
        with patch.object(http_receiver.time, "time", return_value=mature_at / 1000):
            mature = http_receiver.verify_execution_result(
                confirmed["action_id"], mature_token
            )
        self.assertEqual("executing", mature["state"])
        self.assertFalse(mature["safe_to_retry"])
        self.assertTrue(mature["execution_unknown"])
        self.assertEqual(3, mature["original_value_confirmation_count"])
        self.assertTrue(next(
            item for item in http_receiver.load_action_audit()["actions"]
            if item["action_id"] == confirmed["action_id"]
        )["manual_reconcile_required"])

        http_receiver._save_execution_preflight({
            "session_id": "c" * 24,
            "action_id": confirmed["action_id"],
            "authorization_id": authorization_id,
            "state": "manual_reconcile_required",
            "authorization_consumed": True,
            "write_enabled": False,
            "execution_enabled": False,
        })
        resolved_at = now_ms + 31_000
        resolved_token = save_plan_value(
            resolved_at, "doc-eventual-target-0004", "400"
        )
        with patch.object(http_receiver.time, "time", return_value=resolved_at / 1000):
            resolved = http_receiver.verify_execution_result(
                confirmed["action_id"], resolved_token
            )
        self.assertTrue(resolved["verified"])
        canonical = next(
            item for item in http_receiver.load_action_audit()["actions"]
            if item["action_id"] == confirmed["action_id"]
        )
        self.assertFalse(canonical["manual_reconcile_required"])
        self.assertNotIn("manual_reconcile_required_at_ms", canonical)
        self.assertEqual("target_verified_by_independent_readback", canonical["manual_reconcile_resolution"])
        self.assertGreater(canonical["manual_reconcile_resolved_at_ms"], 0)
        self.assertEqual("completed", http_receiver.load_execution_preflight()["session"]["state"])

    def test_cancelled_action_invalidates_existing_execution_authorization(self) -> None:
        self._bind_execution_scope("store-1", "acct_cancel001")
        http_receiver.save_agent_settings({
            "execution_mode": "supervised",
            "store_key": "store-1",
            "qianchuan_account_key": "acct_cancel001",
        })
        captured_at = int(time.time() * 1000)
        snapshot = {
            "schema_version": 2,
            "page_type": "campaigns",
            "captured_at": captured_at,
            "document_instance_id": "doc-cancel-baseline-0001",
            "navigation_started_at_ms": captured_at - 1_000,
            "store": {"key": "store-1", "confidence": "high"},
            "account": {"key": "acct_cancel001", "store_key": "store-1", "label": "撤销授权账号", "confidence": "high"},
            "promotion_context": execution_promotion_context(
                "store-1", "acct_cancel001", "plan_cancel001"
            ),
            "quality": {"score": 90, "row_count": 1},
            "tables": [{
                "headers": ["计划ID", "计划名称", "日预算", "消耗", "支付 ROI", "成交订单"],
                "rows": [["plan_cancel001", "撤销授权计划", "500", "300", "0.60", "2"]],
            }],
        }
        http_receiver.save_data("qianchuan", snapshot)
        draft = http_receiver.build_action_draft(
            operation_type="adjust_budget",
            operation_label="降低预算 20%",
            target_kind="qianchuan_plan",
            target_id="plan_cancel001",
            promotion_context={"promotion_mode": "standard", "account_scope": {"store_id": "store-1", "account_id": "acct_cancel001"}, "strategy_id": "plan_cancel001", "metric_contract": {"definition": "pay_roi", "version": "v1"}, "data_quality": {"confidence": "high", "freshness_seconds": 0, "completeness": 0.9}},
            target_name="撤销授权计划",
            account_key="acct_cancel001",
            account_label="撤销授权账号",
            field="预算",
            current_value=500.0,
            target_value=400.0,
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=captured_at,
            quality_score=90,
            confidence="high",
            evidence={"spend": 300.0, "roi": 0.60, "orders": 2.0},
        )
        confirmed = http_receiver.confirm_action_draft(draft)
        awaiting = http_receiver.start_execution_preflight(confirmed["action_id"])
        snapshot["captured_at"] = awaiting["session"]["started_at_ms"] + 1000
        snapshot["document_instance_id"] = "doc-cancel-hard-reread-0002"
        snapshot["navigation_started_at_ms"] = awaiting["session"]["started_at_ms"] + 500
        http_receiver.save_data("qianchuan", snapshot)
        ready = http_receiver.build_execution_preflight_report()
        authorized = http_receiver.authorize_execution_preflight(
            ready["session"]["session_id"],
            "确认降低预算至400.0",
        )
        authorization_id = authorized["session"]["authorization_id"]
        cancelled = http_receiver.cancel_confirmed_action(confirmed["action_id"])
        self.assertEqual("cancelled", cancelled["state"])
        with self.assertRaisesRegex(ValueError, "已撤销、已停止或不再允许执行"):
            http_receiver.consume_execution_authorization(authorization_id)
        invalidated = http_receiver.load_execution_preflight()["session"]
        self.assertEqual("invalidated", invalidated["state"])
        self.assertFalse(invalidated["authorization_consumed"])

    def test_execution_scope_mismatch_is_rejected_before_draft_is_sealed(self) -> None:
        http_receiver.save_agent_settings({
            "store_key": "store-a",
            "qianchuan_account_key": "acct-a",
        })
        with self.assertRaisesRegex(ValueError, "方案店铺与当前选择店铺不一致"):
            http_receiver.build_action_draft(
                operation_type="adjust_budget",
                operation_label="降低预算",
                target_kind="qianchuan_plan",
                target_id="plan-a",
                target_name="错店计划",
                account_key="acct-a",
                account_label="错店账号",
                field="预算",
                current_value=500.0,
                target_value=400.0,
                source="qianchuan",
                page_type="campaigns",
                captured_at_ms=int(time.time() * 1000),
                quality_score=90,
                confidence="high",
                promotion_context={
                    "promotion_mode": "standard",
                    "account_scope": {"store_id": "store-b", "account_id": "acct-a"},
                    "strategy_id": "plan-a",
                    "metric_contract": {"definition": "pay_roi", "version": "v1"},
                    "data_quality": {"confidence": "high", "freshness_seconds": 0, "completeness": 1.0},
                },
            )

    def test_scope_change_keeps_preflight_irreversibly_invalidated(self) -> None:
        confirmed, _, _ = self._seed_authorized_execution()
        old_session_id = http_receiver.load_execution_preflight()["session"]["session_id"]
        http_receiver.save_agent_settings({
            "store_key": "store-2",
            "qianchuan_account_key": "acct-safe2",
        })
        report = http_receiver.build_execution_preflight_report()
        self.assertEqual("invalidated", report["state"])
        self.assertEqual(old_session_id, report["session"]["session_id"])
        with self.assertRaisesRegex(ValueError, "尚未全部通过"):
            http_receiver.authorize_execution_preflight(old_session_id, "确认降低预算至400.0")
        self.assertEqual("confirmed", next(
            item for item in http_receiver.load_action_audit()["actions"]
            if item["action_id"] == confirmed["action_id"]
        )["state"])

    def test_consume_second_write_failure_recovers_authorized_pair(self) -> None:
        confirmed, authorization_id, _ = self._seed_authorized_execution()
        with patch.object(http_receiver, "_save_execution_preflight", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                http_receiver.consume_execution_authorization(authorization_id)
        action = next(
            item for item in http_receiver.load_action_audit()["actions"]
            if item["action_id"] == confirmed["action_id"]
        )
        session = http_receiver.load_execution_preflight()["session"]
        self.assertEqual("confirmed", action["state"])
        self.assertEqual("authorized", session["state"])
        self.assertFalse(session["authorization_consumed"])
        self.assertFalse(http_receiver._execution_consume_transaction_path().exists())

    def test_committed_consume_journal_never_reopens_terminal_action(self) -> None:
        confirmed, authorization_id, receipt = self._seed_authorized_execution()
        with patch.object(http_receiver, "_clear_execution_consume_transaction", return_value=None):
            http_receiver.consume_execution_authorization(authorization_id)
            failed = http_receiver.record_execution_result(confirmed["action_id"], receipt)
        self.assertEqual("failed", failed["state"])
        self.assertTrue(http_receiver._execution_consume_transaction_path().exists())
        recovered = next(
            item for item in http_receiver.load_action_audit()["actions"]
            if item["action_id"] == confirmed["action_id"]
        )
        self.assertEqual("failed", recovered["state"])
        self.assertTrue(recovered["execution_receipt"]["submitted"] is False)
        self.assertFalse(http_receiver._execution_consume_transaction_path().exists())

    def test_negative_receipt_repairs_partial_preflight_write_and_is_idempotent(self) -> None:
        confirmed, authorization_id, receipt = self._seed_authorized_execution()
        http_receiver.consume_execution_authorization(authorization_id)
        with patch.object(http_receiver, "_save_execution_preflight", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                http_receiver.record_execution_result(confirmed["action_id"], receipt)
        repaired = http_receiver.record_execution_result(confirmed["action_id"], receipt)
        self.assertEqual("failed", repaired["state"])
        session = http_receiver.load_execution_preflight()["session"]
        self.assertEqual("execution_failed", session["state"])
        self.assertTrue(session["execution_receipt_recorded"])
        repeated = http_receiver.record_execution_result(confirmed["action_id"], receipt)
        self.assertEqual(repaired["execution_receipt"], repeated["execution_receipt"])

    def test_unknown_submission_receipt_keeps_consumed_action_locked(self) -> None:
        confirmed, authorization_id, receipt = self._seed_authorized_execution()
        http_receiver.consume_execution_authorization(authorization_id)
        with self.assertRaisesRegex(ValueError, "必须明确说明"):
            http_receiver.record_execution_result(
                confirmed["action_id"],
                {**receipt, "submitted": None},
            )
        action = next(
            item for item in http_receiver.load_action_audit()["actions"]
            if item["action_id"] == confirmed["action_id"]
        )
        self.assertEqual("executing", action["state"])
        self.assertFalse(http_receiver.load_execution_preflight()["session"].get("execution_receipt_recorded", False))

    def test_corrupt_action_audit_fails_closed_for_reads_and_execution(self) -> None:
        invalid_documents = (
            "{",
            "[]",
            json.dumps({"schema_version": 2, "actions": []}),
            json.dumps({"schema_version": 1, "actions": {}}),
            json.dumps({"schema_version": 1, "actions": [None]}),
        )
        for raw in invalid_documents:
            http_receiver._action_audit_path().write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw), self.assertRaises(OSError):
                http_receiver.load_action_audit()
        http_receiver._action_audit_path().unlink(missing_ok=True)

        confirmed, authorization_id, _ = self._seed_authorized_execution(
            store_key="store-corrupt-audit",
            account_key="acct-corrupt-audit",
            plan_id="plan-corrupt-audit",
            authorization_id="2" * 32,
        )
        http_receiver._action_audit_path().write_text(
            json.dumps({"schema_version": 1, "actions": "not-a-list"}),
            encoding="utf-8",
        )
        with self.assertRaises(OSError):
            http_receiver.consume_execution_authorization(authorization_id)
        self.assertEqual("confirmed", confirmed["state"])

    def test_corrupt_execution_preflight_fails_closed_for_reads_and_execution(self) -> None:
        invalid_documents = (
            "{",
            "[]",
            json.dumps({"schema_version": 2, "session": None}),
            json.dumps({"schema_version": 1, "session": []}),
            json.dumps({"schema_version": 1, "session": "authorized"}),
        )
        for raw in invalid_documents:
            http_receiver._execution_preflight_path().write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw), self.assertRaises(OSError):
                http_receiver.load_execution_preflight()
        http_receiver._execution_preflight_path().unlink(missing_ok=True)

        _, authorization_id, _ = self._seed_authorized_execution(
            store_key="store-corrupt-preflight",
            account_key="acct-corrupt-preflight",
            plan_id="plan-corrupt-preflight",
            authorization_id="3" * 32,
        )
        http_receiver._execution_preflight_path().write_text(
            json.dumps({"schema_version": 1, "session": 1}),
            encoding="utf-8",
        )
        with self.assertRaises(OSError):
            http_receiver.preview_execution_authorization(authorization_id)

    def test_recover_execution_readback_job_rebuilds_exact_consumed_scope_without_mutation(self) -> None:
        for index, action_state in enumerate(("executing", "succeeded"), start=1):
            with self.subTest(action_state=action_state), self._isolated_data_dir():
                account_key = f"acct-readback-recover-{index}"
                plan_id = f"plan-readback-recover-{index}"
                authorization_id = str(3 + index) * 32
                confirmed, _, _ = self._seed_authorized_execution(
                    store_key="store-readback-recover",
                    account_key=account_key,
                    plan_id=plan_id,
                    authorization_id=authorization_id,
                )
                http_receiver.consume_execution_authorization(authorization_id)
                if action_state == "succeeded":
                    audit = http_receiver.load_action_audit()
                    executing = next(
                        item for item in audit["actions"] if item["action_id"] == confirmed["action_id"]
                    )
                    succeeded = http_receiver.transition_action(
                        executing, "succeeded", allow_execution=True
                    )
                    http_receiver._atomic_json_write(
                        http_receiver._action_audit_path(),
                        {
                            "schema_version": 1,
                            "updated_at": http_receiver._now_label(),
                            "execution_enabled": True,
                            "actions": [succeeded],
                        },
                    )

                action_bytes = http_receiver._action_audit_path().read_bytes()
                preflight_bytes = http_receiver._execution_preflight_path().read_bytes()
                job = http_receiver.recover_execution_readback_job(confirmed["action_id"])
                self.assertEqual(confirmed["action_id"], job["action_id"])
                self.assertEqual(authorization_id, job["authorization_id"])
                self.assertTrue(job["authorization_consumed"])
                self.assertEqual("store-readback-recover", job["store_key"])
                self.assertEqual(account_key, job["account_key"])
                self.assertEqual(plan_id, job["plan_id"])
                self.assertEqual("standard", job["baseline_promotion_mode"])
                self.assertEqual("campaigns", job["baseline_page_type"])
                self.assertEqual(
                    confirmed["execution_baseline_hash"], job["execution_baseline_hash"]
                )
                self.assertFalse(job["write_enabled"])
                self.assertFalse(job["execution_enabled"])
                self.assertNotIn("execution_request", job)
                self.assertNotIn("authorization_expires_at_ms", job)
                self.assertEqual(action_bytes, http_receiver._action_audit_path().read_bytes())
                self.assertEqual(preflight_bytes, http_receiver._execution_preflight_path().read_bytes())

                if action_state == "executing":
                    server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        response = self._internal_http_get(
                            f"http://127.0.0.1:{server.server_port}/actions/execution/readback-job"
                            f"?action_id={confirmed['action_id']}"
                        )
                        payload = json.loads(response.read())
                    finally:
                        server.shutdown()
                        server.server_close()
                        thread.join(timeout=2)
                    self.assertTrue(payload["ok"])
                    for key in (
                        "action_id", "authorization_id", "store_key", "account_key", "plan_id",
                        "baseline_promotion_mode", "baseline_page_type", "execution_baseline_hash",
                        "authorization_consumed", "write_enabled", "execution_enabled",
                    ):
                        self.assertEqual(job[key], payload["job"][key])
                    self.assertNotIn("execution_request", payload["job"])
                    self.assertNotIn("authorization_expires_at_ms", payload["job"])
                    self.assertEqual(action_bytes, http_receiver._action_audit_path().read_bytes())
                    self.assertEqual(preflight_bytes, http_receiver._execution_preflight_path().read_bytes())

    def test_recover_execution_readback_job_rejects_unconsumed_terminal_and_mismatched_actions(self) -> None:
        confirmed, _, _ = self._seed_authorized_execution(
            store_key="store-readback-reject",
            account_key="acct-readback-unconsumed",
            plan_id="plan-readback-unconsumed",
            authorization_id="8" * 32,
        )
        action_bytes = http_receiver._action_audit_path().read_bytes()
        preflight_bytes = http_receiver._execution_preflight_path().read_bytes()
        with self.assertRaises(ValueError):
            http_receiver.recover_execution_readback_job(confirmed["action_id"])
        self.assertEqual(action_bytes, http_receiver._action_audit_path().read_bytes())
        self.assertEqual(preflight_bytes, http_receiver._execution_preflight_path().read_bytes())

        for index, terminal_state in enumerate(("failed", "verified"), start=1):
            authorization_id = ("8" if index == 1 else "9") * 32
            confirmed, _, _ = self._seed_authorized_execution(
                store_key="store-readback-reject",
                account_key=f"acct-readback-terminal-{index}",
                plan_id=f"plan-readback-terminal-{index}",
                authorization_id=authorization_id,
            )
            http_receiver.consume_execution_authorization(authorization_id)
            audit = http_receiver.load_action_audit()
            action = audit["actions"][0]
            if terminal_state == "failed":
                action = http_receiver.transition_action(action, "failed", allow_execution=True)
            else:
                action = http_receiver.transition_action(action, "succeeded", allow_execution=True)
                action = http_receiver.transition_action(action, "verified")
            http_receiver._atomic_json_write(
                http_receiver._action_audit_path(),
                {
                    "schema_version": 1,
                    "updated_at": http_receiver._now_label(),
                    "execution_enabled": True,
                    "actions": [action],
                },
            )
            action_bytes = http_receiver._action_audit_path().read_bytes()
            preflight_bytes = http_receiver._execution_preflight_path().read_bytes()
            with self.subTest(terminal_state=terminal_state), self.assertRaises(ValueError):
                http_receiver.recover_execution_readback_job(confirmed["action_id"])
            self.assertEqual(action_bytes, http_receiver._action_audit_path().read_bytes())
            self.assertEqual(preflight_bytes, http_receiver._execution_preflight_path().read_bytes())

        confirmed, authorization_id, _ = self._seed_authorized_execution(
            store_key="store-readback-reject",
            account_key="acct-readback-mismatch",
            plan_id="plan-readback-mismatch",
            authorization_id="a" * 32,
        )
        http_receiver.consume_execution_authorization(authorization_id)
        session = http_receiver.load_execution_preflight()["session"]
        http_receiver._save_execution_preflight({**session, "action_id": "f" * 24})
        action_bytes = http_receiver._action_audit_path().read_bytes()
        preflight_bytes = http_receiver._execution_preflight_path().read_bytes()
        with self.assertRaises(ValueError):
            http_receiver.recover_execution_readback_job(confirmed["action_id"])
        self.assertEqual(action_bytes, http_receiver._action_audit_path().read_bytes())
        self.assertEqual(preflight_bytes, http_receiver._execution_preflight_path().read_bytes())
        with self.assertRaises(ValueError):
            http_receiver.recover_execution_readback_job("not-an-action-id")
        self.assertEqual(action_bytes, http_receiver._action_audit_path().read_bytes())
        self.assertEqual(preflight_bytes, http_receiver._execution_preflight_path().read_bytes())

        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self._internal_http_get(
                    f"http://127.0.0.1:{server.server_port}/actions/execution/readback-job"
                    f"?action_id={confirmed['action_id']}"
                )
            payload = json.loads(raised.exception.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(400, raised.exception.code)
        self.assertFalse(payload["executed"])
        self.assertFalse(payload["write_enabled"])
        self.assertNotIn("job", payload)
        self.assertEqual(action_bytes, http_receiver._action_audit_path().read_bytes())
        self.assertEqual(preflight_bytes, http_receiver._execution_preflight_path().read_bytes())

    def test_preflight_report_repairs_exact_persisted_receipt_marker_without_extension_retry(self) -> None:
        confirmed, authorization_id, receipt = self._seed_authorized_execution(
            store_key="store-receipt-heal",
            account_key="acct-receipt-heal",
            plan_id="plan-receipt-heal",
            authorization_id="b" * 32,
        )
        http_receiver.consume_execution_authorization(authorization_id)
        canonical_receipt = {
            **receipt,
            "submitted": True,
            "platform_success_observed": False,
            "error": "",
            "definite_not_submitted": False,
            "submission_phase": "click_invoked",
            "mutation_started": True,
            "click_invoked": True,
        }
        audit = http_receiver.load_action_audit()
        action = audit["actions"][0]
        action = {
            **action,
            "execution_receipt": canonical_receipt,
            "execution_reported_at_ms": int(time.time() * 1000),
        }
        http_receiver._atomic_json_write(
            http_receiver._action_audit_path(),
            {
                "schema_version": 1,
                "updated_at": http_receiver._now_label(),
                "execution_enabled": True,
                "actions": [action],
            },
        )
        self.assertFalse(
            http_receiver.load_execution_preflight()["session"].get(
                "execution_receipt_recorded", False
            )
        )

        report = http_receiver.build_execution_preflight_report()
        self.assertEqual("authorization_consumed", report["state"])
        self.assertTrue(report["session"]["execution_receipt_recorded"])
        self.assertEqual("executing", report["session"]["execution_receipt_action_state"])
        session = http_receiver.load_execution_preflight()["session"]
        self.assertTrue(session["execution_receipt_recorded"])
        self.assertEqual(authorization_id, session["authorization_id"])
        persisted_action = http_receiver.load_action_audit()["actions"][0]
        self.assertEqual(canonical_receipt, persisted_action["execution_receipt"])
        self.assertEqual("executing", persisted_action["state"])

    def test_preflight_report_never_repairs_mismatched_persisted_receipt(self) -> None:
        mismatch_cases = (
            {"authorization_id": "d" * 32},
            {"store_key": "store-receipt-other"},
            {"account_key": "acct-receipt-other"},
            {"plan_id": "plan-receipt-other"},
            {"operation_type": "pause_plan"},
            {"target_value": 401.0},
        )
        for index, receipt_override in enumerate(mismatch_cases, start=1):
            with self.subTest(receipt_override=receipt_override), self._isolated_data_dir():
                authorization_id = "c" * 32
                confirmed, _, receipt = self._seed_authorized_execution(
                    store_key="store-receipt-mismatch",
                    account_key=f"acct-receipt-mismatch-{index}",
                    plan_id=f"plan-receipt-mismatch-{index}",
                    authorization_id=authorization_id,
                )
                http_receiver.consume_execution_authorization(authorization_id)
                canonical_receipt = {
                    **receipt,
                    "submitted": True,
                    "platform_success_observed": False,
                    "error": "",
                    "definite_not_submitted": False,
                    "submission_phase": "click_invoked",
                    "mutation_started": True,
                    "click_invoked": True,
                    **receipt_override,
                }
                audit = http_receiver.load_action_audit()
                action = {
                    **audit["actions"][0],
                    "execution_receipt": canonical_receipt,
                    "execution_reported_at_ms": int(time.time() * 1000),
                }
                http_receiver._atomic_json_write(
                    http_receiver._action_audit_path(),
                    {
                        "schema_version": 1,
                        "updated_at": http_receiver._now_label(),
                        "execution_enabled": True,
                        "actions": [action],
                    },
                )
                report = http_receiver.build_execution_preflight_report()
                self.assertFalse(
                    report["session"].get("execution_receipt_recorded", False)
                )
                self.assertEqual("authorization_consumed", report["state"])
                session = http_receiver.load_execution_preflight()["session"]
                self.assertFalse(session.get("execution_receipt_recorded", False))
                self.assertTrue(session["authorization_consumed"])

    def test_execution_readback_push_is_action_scoped_and_never_replaces_canonical_campaigns(self) -> None:
        store_raw_id = "810000001"
        account_raw_id = "710000001"
        store_key = http_receiver._local_identity_key("qianchuan_shop_id", store_raw_id)
        account_key = http_receiver._local_identity_key(
            "qianchuan_advertiser_id", account_raw_id
        )
        plan_id = "plan-targeted-readback"
        confirmed, authorization_id, receipt = self._seed_authorized_execution(
            store_key=store_key,
            account_key=account_key,
            plan_id=plan_id,
            authorization_id="1" * 32,
        )
        canonical_snapshot = self._execution_plan_snapshot(
            store_key=store_key,
            account_key=account_key,
            plan_id=plan_id,
            budget=500.0,
            captured_at_ms=int(time.time() * 1000),
            document_instance_id="doc-canonical-before-readback-0001",
            store_raw_id=store_raw_id,
            account_raw_id=account_raw_id,
        )
        http_receiver.save_data("qianchuan", canonical_snapshot)
        canonical_sqlite = http_receiver.load_data(
            "qianchuan", "campaigns", account_key=account_key
        )
        canonical_paths = (
            http_receiver._snapshot_path("qianchuan", "campaigns"),
            http_receiver._account_snapshot_path(account_key, "campaigns"),
            http_receiver.DATA_DIR / "qianchuan.json",
        )
        canonical_bytes = {path: path.read_bytes() for path in canonical_paths}

        http_receiver.consume_execution_authorization(authorization_id)
        submitted = http_receiver.record_execution_result(confirmed["action_id"], {
            **receipt,
            "submitted": True,
            "platform_success_observed": False,
            "error": "",
            "definite_not_submitted": False,
            "submission_phase": "click_invoked",
            "mutation_started": True,
            "click_invoked": True,
        })
        captured_at_ms = int(submitted["execution_reported_at_ms"]) + 1_000
        targeted_snapshot = self._execution_plan_snapshot(
            store_key=store_key,
            account_key=account_key,
            plan_id=plan_id,
            budget=400.0,
            captured_at_ms=captured_at_ms,
            document_instance_id="doc-targeted-execution-readback-0002",
            navigation_started_at_ms=int(submitted["execution_reported_at_ms"]) + 500,
            targeted=True,
            store_raw_id=store_raw_id,
            account_raw_id=account_raw_id,
        )
        readback_token = "2" * 64
        body = json.dumps({
            "source": "qianchuan",
            "data": targeted_snapshot,
            "expected_scope": {
                "store_key": store_key,
                "account_key": account_key,
            },
            "scan_context": {},
            "execution_context": {
                "purpose": "execution_readback",
                "action_id": confirmed["action_id"],
                "authorization_id": authorization_id,
                "readback_token": readback_token,
            },
        }).encode("utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/push",
                data=body,
                headers=self._internal_http_headers(),
                method="POST",
            ))
            push_result = json.loads(response.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertTrue(push_result["ok"])
        self.assertTrue(push_result["targeted_execution_snapshot"])
        self.assertEqual(
            canonical_sqlite,
            http_receiver.load_data("qianchuan", "campaigns", account_key=account_key),
        )
        for path, value in canonical_bytes.items():
            self.assertEqual(value, path.read_bytes())
        with self.assertRaises(TypeError):
            http_receiver.verify_execution_result(confirmed["action_id"])
        wrong_token = http_receiver.verify_execution_result(
            confirmed["action_id"], "3" * 64
        )
        self.assertFalse(wrong_token["verified"])
        self.assertIsNone(wrong_token["readback"])

        verification = http_receiver.verify_execution_result(
            confirmed["action_id"], readback_token
        )
        self.assertTrue(verification["verified"])
        self.assertEqual("verified", verification["state"])
        self.assertEqual(400.0, verification["readback"]["current_value"])
        self.assertEqual(
            "doc-targeted-execution-readback-0002",
            verification["readback"]["document_instance_id"],
        )

    def test_execution_readback_push_rejects_missing_malformed_and_cross_action_context(self) -> None:
        store_raw_id = "810000002"
        account_raw_id = "710000002"
        store_key = http_receiver._local_identity_key("qianchuan_shop_id", store_raw_id)
        account_key = http_receiver._local_identity_key(
            "qianchuan_advertiser_id", account_raw_id
        )
        plan_id = "plan-readback-token"
        confirmed, authorization_id, receipt = self._seed_authorized_execution(
            store_key=store_key,
            account_key=account_key,
            plan_id=plan_id,
            authorization_id="4" * 32,
        )
        canonical_snapshot = self._execution_plan_snapshot(
            store_key=store_key,
            account_key=account_key,
            plan_id=plan_id,
            budget=500.0,
            captured_at_ms=int(time.time() * 1000),
            document_instance_id="doc-readback-token-canonical-0001",
            store_raw_id=store_raw_id,
            account_raw_id=account_raw_id,
        )
        http_receiver.save_data("qianchuan", canonical_snapshot)
        canonical_sqlite = http_receiver.load_data(
            "qianchuan", "campaigns", account_key=account_key
        )
        http_receiver.consume_execution_authorization(authorization_id)
        submitted = http_receiver.record_execution_result(confirmed["action_id"], {
            **receipt,
            "submitted": True,
            "platform_success_observed": False,
            "error": "",
            "definite_not_submitted": False,
            "submission_phase": "click_invoked",
            "mutation_started": True,
            "click_invoked": True,
        })
        targeted_snapshot = self._execution_plan_snapshot(
            store_key=store_key,
            account_key=account_key,
            plan_id=plan_id,
            budget=400.0,
            captured_at_ms=int(submitted["execution_reported_at_ms"]) + 1_000,
            document_instance_id="doc-readback-token-targeted-0002",
            navigation_started_at_ms=int(submitted["execution_reported_at_ms"]) + 500,
            targeted=True,
            store_raw_id=store_raw_id,
            account_raw_id=account_raw_id,
        )
        base_context = {
            "purpose": "execution_readback",
            "action_id": confirmed["action_id"],
            "authorization_id": authorization_id,
        }
        invalid_contexts = (
            base_context,
            {**base_context, "readback_token": "short"},
            {**base_context, "readback_token": "A" * 64},
            {**base_context, "readback_token": "5" * 64, "action_id": "f" * 24},
            {**base_context, "readback_token": "5" * 64, "authorization_id": "f" * 32},
            {**base_context, "readback_token": "5" * 64, "purpose": "preconsume_baseline"},
            {**base_context, "readback_token": "5" * 64, "unexpected": True},
        )
        for execution_context in invalid_contexts:
            with self.subTest(execution_context=execution_context), self.assertRaises(ValueError):
                http_receiver.save_scan_page_once(
                    "qianchuan",
                    targeted_snapshot,
                    {"store_key": store_key, "account_key": account_key},
                    {},
                    execution_context,
                )
            self.assertEqual(
                canonical_sqlite,
                http_receiver.load_data("qianchuan", "campaigns", account_key=account_key),
            )

        readback_token = "5" * 64
        push_result = http_receiver.save_scan_page_once(
            "qianchuan",
            targeted_snapshot,
            {"store_key": store_key, "account_key": account_key},
            {},
            {**base_context, "readback_token": readback_token},
        )
        self.assertTrue(push_result["targeted_execution_snapshot"])
        wrong_token = http_receiver.verify_execution_result(
            confirmed["action_id"], "6" * 64
        )
        self.assertFalse(wrong_token["verified"])
        self.assertIsNone(wrong_token["readback"])
        self.assertEqual(
            canonical_sqlite,
            http_receiver.load_data("qianchuan", "campaigns", account_key=account_key),
        )
        self.assertEqual("executing", next(
            item for item in http_receiver.load_action_audit()["actions"]
            if item["action_id"] == confirmed["action_id"]
        )["state"])

    def test_concurrent_normal_scan_cannot_impersonate_execution_readback(self) -> None:
        store_raw_id = "810000003"
        account_raw_id = "710000003"
        store_key = http_receiver._local_identity_key("qianchuan_shop_id", store_raw_id)
        account_key = http_receiver._local_identity_key(
            "qianchuan_advertiser_id", account_raw_id
        )
        plan_id = "plan-readback-race"
        confirmed, authorization_id, receipt = self._seed_authorized_execution(
            store_key=store_key,
            account_key=account_key,
            plan_id=plan_id,
            authorization_id="7" * 32,
        )
        http_receiver.consume_execution_authorization(authorization_id)
        submitted = http_receiver.record_execution_result(confirmed["action_id"], {
            **receipt,
            "submitted": True,
            "platform_success_observed": False,
            "error": "",
            "definite_not_submitted": False,
            "submission_phase": "click_invoked",
            "mutation_started": True,
            "click_invoked": True,
        })
        captured_at_ms = int(submitted["execution_reported_at_ms"]) + 1_000
        targeted_original = self._execution_plan_snapshot(
            store_key=store_key,
            account_key=account_key,
            plan_id=plan_id,
            budget=500.0,
            captured_at_ms=captured_at_ms,
            document_instance_id="doc-readback-race-targeted-0002",
            navigation_started_at_ms=int(submitted["execution_reported_at_ms"]) + 500,
            targeted=True,
            store_raw_id=store_raw_id,
            account_raw_id=account_raw_id,
        )
        ordinary_target = self._execution_plan_snapshot(
            store_key=store_key,
            account_key=account_key,
            plan_id=plan_id,
            budget=400.0,
            captured_at_ms=captured_at_ms + 1,
            document_instance_id="doc-readback-race-ordinary-0003",
            navigation_started_at_ms=int(submitted["execution_reported_at_ms"]) + 501,
            store_raw_id=store_raw_id,
            account_raw_id=account_raw_id,
        )
        readback_token = "8" * 64
        barrier = threading.Barrier(3)
        results: list[dict] = []
        errors: list[Exception] = []

        def targeted_push() -> None:
            try:
                barrier.wait(timeout=3)
                results.append(http_receiver.save_scan_page_once(
                    "qianchuan",
                    targeted_original,
                    {"store_key": store_key, "account_key": account_key},
                    {},
                    {
                        "purpose": "execution_readback",
                        "action_id": confirmed["action_id"],
                        "authorization_id": authorization_id,
                        "readback_token": readback_token,
                    },
                ))
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        def ordinary_push() -> None:
            try:
                barrier.wait(timeout=3)
                results.append(http_receiver.save_scan_page_once(
                    "qianchuan",
                    ordinary_target,
                    {"store_key": store_key, "account_key": account_key},
                    {},
                ))
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        threads = [
            threading.Thread(target=targeted_push),
            threading.Thread(target=ordinary_push),
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=3)
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertEqual(1, sum(
            item.get("targeted_execution_snapshot") is True for item in results
        ))
        canonical = http_receiver.load_data(
            "qianchuan", "campaigns", account_key=account_key
        )
        self.assertEqual("400.0", canonical["data"]["tables"][0]["rows"][0][2])
        with self.assertRaises(TypeError):
            http_receiver.verify_execution_result(confirmed["action_id"])
        verification = http_receiver.verify_execution_result(
            confirmed["action_id"], readback_token
        )
        self.assertFalse(verification["verified"])
        self.assertEqual("executing", verification["state"])
        self.assertEqual(500.0, verification["readback"]["current_value"])
        self.assertEqual(
            "doc-readback-race-targeted-0002",
            verification["readback"]["document_instance_id"],
        )

    def test_preconsume_baseline_requires_its_own_action_bound_token(self) -> None:
        store_raw_id = "810000004"
        account_raw_id = "710000004"
        store_key = http_receiver._local_identity_key("qianchuan_shop_id", store_raw_id)
        account_key = http_receiver._local_identity_key(
            "qianchuan_advertiser_id", account_raw_id
        )
        plan_id = "plan-preconsume-token"
        confirmed, authorization_id, _ = self._seed_authorized_execution(
            store_key=store_key,
            account_key=account_key,
            plan_id=plan_id,
            authorization_id="9" * 32,
        )
        authorized = http_receiver.load_execution_preflight()["session"]
        captured_at_ms = int(authorized["authorized_at_ms"]) + 1_000
        canonical_reread = self._execution_plan_snapshot(
            store_key=store_key,
            account_key=account_key,
            plan_id=plan_id,
            budget=500.0,
            captured_at_ms=captured_at_ms,
            document_instance_id="doc-preconsume-canonical-0002",
            navigation_started_at_ms=int(authorized["authorized_at_ms"]) + 500,
            store_raw_id=store_raw_id,
            account_raw_id=account_raw_id,
        )
        http_receiver.save_data("qianchuan", canonical_reread)
        canonical_sqlite = http_receiver.load_data(
            "qianchuan", "campaigns", account_key=account_key
        )
        readback_token = "a" * 64
        with self.assertRaises(TypeError):
            http_receiver.refresh_authorized_execution_baseline(authorization_id)
        with self.assertRaises(ValueError):
            http_receiver.save_scan_page_once(
                "qianchuan",
                canonical_reread,
                {"store_key": store_key, "account_key": account_key},
                {},
                {
                    "purpose": "execution_readback",
                    "action_id": confirmed["action_id"],
                    "authorization_id": authorization_id,
                    "readback_token": readback_token,
                },
            )

        targeted_reread = self._execution_plan_snapshot(
            store_key=store_key,
            account_key=account_key,
            plan_id=plan_id,
            budget=500.0,
            captured_at_ms=captured_at_ms + 1,
            document_instance_id="doc-preconsume-targeted-0003",
            navigation_started_at_ms=int(authorized["authorized_at_ms"]) + 501,
            targeted=True,
            store_raw_id=store_raw_id,
            account_raw_id=account_raw_id,
        )
        pushed = http_receiver.save_scan_page_once(
            "qianchuan",
            targeted_reread,
            {"store_key": store_key, "account_key": account_key},
            {},
            {
                "purpose": "preconsume_baseline",
                "action_id": confirmed["action_id"],
                "authorization_id": authorization_id,
                "readback_token": readback_token,
            },
        )
        self.assertTrue(pushed["targeted_execution_snapshot"])
        self.assertEqual(
            canonical_sqlite,
            http_receiver.load_data("qianchuan", "campaigns", account_key=account_key),
        )
        refreshed = http_receiver.refresh_authorized_execution_baseline(
            authorization_id, readback_token
        )
        self.assertEqual(
            "doc-preconsume-targeted-0003",
            refreshed["execution_baseline"]["document_instance_id"],
        )
        http_receiver.consume_execution_authorization(authorization_id)
        with self.assertRaises(ValueError):
            http_receiver.save_scan_page_once(
                "qianchuan",
                targeted_reread,
                {"store_key": store_key, "account_key": account_key},
                {},
                {
                    "purpose": "preconsume_baseline",
                    "action_id": confirmed["action_id"],
                    "authorization_id": authorization_id,
                    "readback_token": "c" * 64,
                },
            )

    def test_consume_rejects_bypassed_final_preconsume_reread_without_execution_request(self) -> None:
        confirmed, authorization_id, _ = self._seed_authorized_execution(
            store_key="store-final-required",
            account_key="acct-final-required",
            plan_id="plan-final-required",
            authorization_id="7" * 32,
        )
        audit = http_receiver.load_action_audit()
        action = next(
            item for item in audit["actions"]
            if item["action_id"] == confirmed["action_id"]
        )
        baseline = dict(action["execution_baseline"])
        baseline.pop("readback_token", None)
        baseline.pop("readback_purpose", None)
        action["execution_baseline"] = baseline
        action["execution_baseline_hash"] = http_receiver._execution_baseline_hash(
            baseline
        )
        action.pop("final_preconsume_readback_token", None)
        action.pop("final_preconsume_authorization_id", None)
        action.pop("final_preconsume_reread_at_ms", None)
        http_receiver._atomic_json_write(
            http_receiver._action_audit_path(),
            {
                "schema_version": 1,
                "updated_at": http_receiver._now_label(),
                "execution_enabled": False,
                "actions": audit["actions"],
            },
        )
        session = dict(http_receiver.load_execution_preflight()["session"])
        session["execution_baseline_hash"] = action["execution_baseline_hash"]
        session.pop("final_preconsume_document_instance_id", None)
        session.pop("final_preconsume_readback_token", None)
        session.pop("final_preconsume_authorization_id", None)
        session.pop("final_preconsume_reread_at_ms", None)
        http_receiver._save_execution_preflight(session)

        with self.assertRaisesRegex(ValueError, "最终硬刷新复核"):
            http_receiver.consume_execution_authorization(authorization_id)

        invalidated = http_receiver.load_execution_preflight()["session"]
        self.assertEqual("invalidated", invalidated["state"])
        self.assertEqual(
            "final_preconsume_reread_missing_stale_or_unbound",
            invalidated["invalidation_reason"],
        )
        self.assertFalse(invalidated["authorization_consumed"])
        self.assertNotIn("execution_request", invalidated)
        canonical = next(
            item for item in http_receiver.load_action_audit()["actions"]
            if item["action_id"] == confirmed["action_id"]
        )
        self.assertEqual("confirmed", canonical["state"])
        self.assertNotIn("execution_request", canonical)

    def test_execution_identity_probe_binds_exact_server_request_before_and_after_consume(self) -> None:
        store_raw_id = "810000099"
        account_raw_id = "710000099"
        store_key = http_receiver._local_identity_key("qianchuan_shop_id", store_raw_id)
        account_key = http_receiver._local_identity_key(
            "qianchuan_advertiser_id", account_raw_id
        )
        confirmed, authorization_id, _ = self._seed_authorized_execution(
            store_key=store_key,
            account_key=account_key,
            plan_id="plan-request-bound",
            authorization_id="8" * 32,
        )
        page_snapshot = self._execution_plan_snapshot(
            store_key=store_key,
            account_key=account_key,
            plan_id="plan-request-bound",
            budget=500.0,
            captured_at_ms=int(time.time() * 1000),
            document_instance_id="doc-request-binding-0001",
            targeted=True,
            store_raw_id=store_raw_id,
            account_raw_id=account_raw_id,
        )
        authorized_request = {
            **http_receiver._execution_request_for_action(confirmed),
            "action_id": confirmed["action_id"],
            "authorization_id": authorization_id,
            "execution_attempt_id": authorization_id,
        }
        resolved = http_receiver.resolve_execution_identity_snapshot(
            confirmed["action_id"], authorization_id, page_snapshot, authorized_request
        )
        self.assertTrue(resolved["execution_request_bound"])
        self.assertRegex(resolved["execution_request_digest"], r"^[a-f0-9]{64}$")
        for changed in (
            {**authorized_request, "plan_id": "plan-other"},
            {**authorized_request, "target_value": 350.0},
            {**authorized_request, "operation_type": "pause_plan"},
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(
                ValueError, "计划、动作、预算或授权参数已变化"
            ):
                http_receiver.resolve_execution_identity_snapshot(
                    confirmed["action_id"], authorization_id, page_snapshot, changed
                )

        consumed = http_receiver.consume_execution_authorization(authorization_id)
        consumed_request = {
            **consumed["execution_request"],
            "action_id": confirmed["action_id"],
            "authorization_id": authorization_id,
        }
        self.assertTrue(http_receiver.resolve_execution_identity_snapshot(
            confirmed["action_id"], authorization_id, page_snapshot, consumed_request
        )["execution_request_bound"])
        with self.assertRaisesRegex(ValueError, "计划、动作、预算或授权参数已变化"):
            http_receiver.resolve_execution_identity_snapshot(
                confirmed["action_id"],
                authorization_id,
                page_snapshot,
                {**consumed_request, "execution_attempt_id": "7" * 32},
            )
    def test_same_document_optimistic_target_never_verifies_execution(self) -> None:
        confirmed, authorization_id, receipt = self._seed_authorized_execution()
        http_receiver.consume_execution_authorization(authorization_id)
        submitted = http_receiver.record_execution_result(confirmed["action_id"], {
            **receipt,
            "submitted": True,
            "error": "",
            "definite_not_submitted": False,
            "submission_phase": "click_invoked",
            "mutation_started": True,
            "click_invoked": True,
        })
        captured_at_ms = int(submitted["execution_reported_at_ms"]) + 1_000
        http_receiver.save_data("qianchuan", {
            "schema_version": 3,
            "page_type": "campaigns",
            "captured_at": captured_at_ms,
            "document_instance_id": "doc-transaction-baseline-0001",
            "document_url": "https://qianchuan.example/campaigns",
            "navigation_started_at_ms": captured_at_ms - 10_000,
            "store": {"key": "store-1"},
            "account": {"key": "acct-safe1", "store_key": "store-1", "label": "事务安全测试账号"},
            "promotion_context": execution_promotion_context(
                "store-1", "acct-safe1", "plan-safe1"
            ),
            "quality": {"score": 90, "row_count": 1},
            "tables": [{
                "headers": ["计划ID", "计划名称", "日预算", "消耗", "支付 ROI", "成交订单"],
                "rows": [["plan-safe1", "事务安全测试计划", "400", "320", "0.65", "3"]],
            }],
        })
        readback_token = "2" * 64
        canonical = http_receiver.load_data(
            "qianchuan", "campaigns", account_key="acct-safe1"
        )
        self._write_execution_snapshot_fixture(
            action_id=confirmed["action_id"],
            authorization_id=authorization_id,
            readback_token=readback_token,
            purpose="execution_readback",
            data=canonical["data"],
        )
        verification = http_receiver.verify_execution_result(
            confirmed["action_id"], readback_token
        )
        self.assertFalse(verification["verified"])
        self.assertFalse(verification["independent_document"])
        self.assertEqual("executing", verification["state"])

    def test_duplicate_plan_readback_is_order_independent_and_conflicts_fail_closed(self) -> None:
        confirmed, _, _ = self._seed_authorized_execution()
        captured_at_ms = int(time.time() * 1000)
        base_data = {
            "schema_version": 3,
            "page_type": "campaigns",
            "captured_at": captured_at_ms,
            "document_instance_id": "doc-duplicate-readback-0001",
            "navigation_started_at_ms": captured_at_ms - 100,
            "store": {"key": "store-1"},
            "account": {"key": "acct-safe1", "store_key": "store-1", "label": "事务安全测试账号"},
            "promotion_context": execution_promotion_context(
                "store-1", "acct-safe1", "plan-safe1"
            ),
            "quality": {"score": 90, "row_count": 2},
        }
        headers = ["计划ID", "计划名称", "日预算", "投放状态", "消耗", "支付 ROI", "成交订单"]
        target_row = ["plan-safe1", "事务安全测试计划", "400", "投放中", "320", "0.65", "3"]
        conflicting_row = ["plan-safe1", "事务安全测试计划", "500", "投放中", "300", "0.60", "2"]
        for rows in ([target_row, conflicting_row], [conflicting_row, target_row]):
            snapshot = {"data": {**base_data, "tables": [{"headers": headers, "rows": rows}]}}
            with self.subTest(first_budget=rows[0][2]), patch.object(
                http_receiver, "load_data", return_value=snapshot
            ):
                self.assertIsNone(http_receiver._find_plan_readback(confirmed))

        identical_snapshot = {
            "data": {
                **base_data,
                "tables": [{"headers": headers, "rows": [target_row, list(target_row)]}],
            }
        }
        with patch.object(http_receiver, "load_data", return_value=identical_snapshot):
            readback = http_receiver._find_plan_readback(confirmed)
        self.assertIsNotNone(readback)
        self.assertEqual(2, readback["duplicate_match_count"])
        self.assertEqual(400.0, readback["current_value"])

    def test_standard_and_chengfang_readbacks_never_cross_verify_same_plan_id(self) -> None:
        standard_action, _, _ = self._seed_authorized_execution()
        chengfang_action = http_receiver.build_action_draft(
            operation_type="adjust_budget",
            operation_label="测试模式隔离",
            target_kind="qianchuan_plan",
            target_id="plan-safe1",
            target_name="事务安全测试计划",
            account_key="acct-safe1",
            account_label="事务安全测试账号",
            field="预算",
            current_value=500.0,
            target_value=400.0,
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=int(time.time() * 1000),
            quality_score=90,
            confidence="high",
            promotion_context=execution_promotion_context(
                "store-1", "acct-safe1", "plan-safe1", promotion_mode="chengfang"
            ),
        )
        captured_at_ms = int(time.time() * 1000)

        def snapshot_for(mode: str) -> dict:
            return {
                "data": {
                    "schema_version": 3,
                    "page_type": "campaigns",
                    "captured_at": captured_at_ms,
                    "document_instance_id": f"doc-mode-{mode}-readback-0001",
                    "navigation_started_at_ms": captured_at_ms - 100,
                    "store": {"key": "store-1"},
                    "account": {"key": "acct-safe1", "store_key": "store-1", "label": "事务安全测试账号"},
                    "promotion_context": execution_promotion_context(
                        "store-1", "acct-safe1", "plan-safe1", promotion_mode=mode
                    ),
                    "quality": {"score": 90, "row_count": 1},
                    "tables": [{
                        "headers": ["计划ID", "计划名称", "日预算", "消耗", "支付 ROI", "成交订单"],
                        "rows": [["plan-safe1", "事务安全测试计划", "400", "320", "0.65", "3"]],
                    }],
                }
            }

        cases = (
            (standard_action, "chengfang"),
            (chengfang_action, "standard"),
        )
        for action, observed_mode in cases:
            with self.subTest(
                expected=action["promotion_context"]["promotion_mode"], observed=observed_mode
            ), patch.object(http_receiver, "load_data", return_value=snapshot_for(observed_mode)):
                self.assertIsNone(http_receiver._find_plan_readback(action))

        with patch.object(http_receiver, "load_data", return_value=snapshot_for("standard")):
            self.assertEqual("standard", http_receiver._find_plan_readback(standard_action)["promotion_mode"])
        with patch.object(http_receiver, "load_data", return_value=snapshot_for("chengfang")):
            self.assertEqual("chengfang", http_receiver._find_plan_readback(chengfang_action)["promotion_mode"])

    def test_final_hard_reread_invalidates_reused_dom_or_changed_budget(self) -> None:
        scenarios = (
            ("acct-final-old", "plan-final-old", "d" * 32, "doc-transaction-baseline-0001", "400"),
            ("acct-final-new", "plan-final-new", "e" * 32, "doc-final-hard-reread-new-0002", "450"),
        )
        for account_key, plan_id, authorization_id, document_id, budget in scenarios:
            with self.subTest(document_id=document_id, budget=budget):
                confirmed, _, _ = self._seed_authorized_execution(
                    store_key="store-final-reread",
                    account_key=account_key,
                    plan_id=plan_id,
                    authorization_id=authorization_id,
                )
                captured_at_ms = int(time.time() * 1000)
                http_receiver.save_data("qianchuan", {
                    "schema_version": 3,
                    "page_type": "campaigns",
                    "captured_at": captured_at_ms,
                    "document_instance_id": document_id,
                    "document_url": "https://qianchuan.example/campaigns",
                    "navigation_started_at_ms": captured_at_ms - 100,
                    "store": {"key": "store-final-reread", "confidence": "high"},
                    "account": {
                        "key": account_key,
                        "store_key": "store-final-reread",
                        "label": "最终硬刷新账号",
                        "confidence": "high",
                    },
                    "promotion_context": execution_promotion_context(
                        "store-final-reread", account_key, plan_id
                    ),
                    "quality": {"score": 90, "row_count": 1},
                    "tables": [{
                        "headers": ["计划ID", "计划名称", "日预算", "消耗", "支付 ROI", "成交订单"],
                        "rows": [[plan_id, "最终硬刷新计划", budget, "320", "0.65", "3"]],
                    }],
                })
                readback_token = "3" * 64
                canonical_snapshot = http_receiver.load_data(
                    "qianchuan", "campaigns", account_key=account_key
                )
                self._write_execution_snapshot_fixture(
                    action_id=confirmed["action_id"],
                    authorization_id=authorization_id,
                    readback_token=readback_token,
                    purpose="preconsume_baseline",
                    data=canonical_snapshot["data"],
                )
                with self.assertRaisesRegex(ValueError, "最终硬刷新发现"):
                    http_receiver.refresh_authorized_execution_baseline(
                        authorization_id, readback_token
                    )
                session = http_receiver.load_execution_preflight()["session"]
                self.assertEqual("invalidated", session["state"])
                self.assertEqual(
                    "final_hard_reread_changed_or_unverified",
                    session["invalidation_reason"],
                )
                canonical = next(
                    item for item in http_receiver.load_action_audit()["actions"]
                    if item["action_id"] == confirmed["action_id"]
                )
                self.assertEqual("confirmed", canonical["state"])

    def test_final_hard_reread_rejects_snapshot_captured_too_long_before_seal(self) -> None:
        confirmed, authorization_id, _ = self._seed_authorized_execution(
            store_key="store-final-stale",
            account_key="acct-final-stale",
            plan_id="plan-final-stale",
            authorization_id="6" * 32,
        )
        now_ms = int(time.time() * 1000)
        session = http_receiver.load_execution_preflight()["session"]
        session["authorized_at_ms"] = now_ms - 40_000
        session["authorization_expires_at_ms"] = now_ms + 60_000
        http_receiver._save_execution_preflight(session)
        stale_snapshot = self._execution_plan_snapshot(
            store_key="store-final-stale",
            account_key="acct-final-stale",
            plan_id="plan-final-stale",
            budget=500.0,
            captured_at_ms=now_ms - 30_000,
            navigation_started_at_ms=now_ms - 35_000,
            document_instance_id="doc-final-stale-reread-0002",
            targeted=True,
        )
        readback_token = "6" * 64
        self._write_execution_snapshot_fixture(
            action_id=confirmed["action_id"],
            authorization_id=authorization_id,
            readback_token=readback_token,
            purpose="preconsume_baseline",
            data=stale_snapshot,
        )
        with self.assertRaisesRegex(ValueError, "最终硬刷新发现"):
            http_receiver.refresh_authorized_execution_baseline(
                authorization_id, readback_token
            )
        invalidated = http_receiver.load_execution_preflight()["session"]
        self.assertEqual("invalidated", invalidated["state"])
        self.assertFalse(invalidated["authorization_consumed"])

    def test_final_hard_reread_updates_action_and_session_to_one_baseline_hash(self) -> None:
        confirmed, authorization_id, _ = self._seed_authorized_execution(
            store_key="store-final-success",
            account_key="acct-final-success",
            plan_id="plan-final-success",
            authorization_id="f" * 32,
        )
        original_hash = confirmed["execution_baseline_hash"]
        authorized = http_receiver.load_execution_preflight()["session"]
        captured_at_ms = int(authorized["authorized_at_ms"]) + 1_000
        http_receiver.save_data("qianchuan", {
            "schema_version": 3,
            "page_type": "campaigns",
            "captured_at": captured_at_ms,
            "document_instance_id": "doc-final-success-hard-reread-0002",
            "document_url": "https://qianchuan.example/campaigns",
            "navigation_started_at_ms": int(authorized["authorized_at_ms"]) + 500,
            "store": {"key": "store-final-success", "confidence": "high"},
            "account": {
                "key": "acct-final-success",
                "store_key": "store-final-success",
                "label": "最终硬刷新成功账号",
                "confidence": "high",
            },
            "promotion_context": execution_promotion_context(
                "store-final-success", "acct-final-success", "plan-final-success"
            ),
            "quality": {"score": 90, "row_count": 1},
            "tables": [{
                "headers": ["计划ID", "计划名称", "日预算", "消耗", "支付 ROI", "成交订单"],
                "rows": [["plan-final-success", "最终硬刷新成功计划", "500", "320", "0.65", "3"]],
            }],
        })

        readback_token = "4" * 64
        canonical_snapshot = http_receiver.load_data(
            "qianchuan", "campaigns", account_key="acct-final-success"
        )
        self._write_execution_snapshot_fixture(
            action_id=confirmed["action_id"],
            authorization_id=authorization_id,
            readback_token=readback_token,
            purpose="preconsume_baseline",
            data=canonical_snapshot["data"],
        )
        preview = http_receiver.refresh_authorized_execution_baseline(
            authorization_id, readback_token
        )
        canonical = next(
            item for item in http_receiver.load_action_audit()["actions"]
            if item["action_id"] == confirmed["action_id"]
        )
        session = http_receiver.load_execution_preflight()["session"]
        self.assertNotEqual(original_hash, canonical["execution_baseline_hash"])
        self.assertEqual(canonical["execution_baseline_hash"], session["execution_baseline_hash"])
        self.assertEqual(
            canonical["execution_baseline_hash"], preview["execution_baseline"]["hash"]
        )
        self.assertEqual(
            "doc-final-success-hard-reread-0002",
            canonical["execution_baseline"]["document_instance_id"],
        )
        self.assertEqual(
            canonical["execution_baseline"]["document_instance_id"],
            session["final_preconsume_document_instance_id"],
        )
        self.assertFalse(http_receiver._execution_baseline_transaction_path().exists())

    def test_final_hard_reread_second_file_failure_recovers_consistent_pair(self) -> None:
        confirmed, authorization_id, _ = self._seed_authorized_execution(
            store_key="store-final-recover",
            account_key="acct-final-recover",
            plan_id="plan-final-recover",
            authorization_id="1" * 32,
        )
        authorized = http_receiver.load_execution_preflight()["session"]
        captured_at_ms = int(authorized["authorized_at_ms"]) + 1_000
        http_receiver.save_data("qianchuan", {
            "schema_version": 3,
            "page_type": "campaigns",
            "captured_at": captured_at_ms,
            "document_instance_id": "doc-final-recovery-reread-0002",
            "navigation_started_at_ms": int(authorized["authorized_at_ms"]) + 500,
            "store": {"key": "store-final-recover", "confidence": "high"},
            "account": {
                "key": "acct-final-recover",
                "store_key": "store-final-recover",
                "label": "最终硬刷新恢复账号",
                "confidence": "high",
            },
            "promotion_context": execution_promotion_context(
                "store-final-recover", "acct-final-recover", "plan-final-recover"
            ),
            "quality": {"score": 90, "row_count": 1},
            "tables": [{
                "headers": ["计划ID", "计划名称", "日预算", "消耗", "支付 ROI", "成交订单"],
                "rows": [["plan-final-recover", "最终硬刷新恢复计划", "500", "320", "0.65", "3"]],
            }],
        })
        readback_token = "5" * 64
        canonical_snapshot = http_receiver.load_data(
            "qianchuan", "campaigns", account_key="acct-final-recover"
        )
        self._write_execution_snapshot_fixture(
            action_id=confirmed["action_id"],
            authorization_id=authorization_id,
            readback_token=readback_token,
            purpose="preconsume_baseline",
            data=canonical_snapshot["data"],
        )
        original_save = http_receiver._save_execution_preflight
        calls = 0

        def fail_second_file_once(session):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("simulated preflight second-file failure")
            return original_save(session)

        with patch.object(
            http_receiver, "_save_execution_preflight", side_effect=fail_second_file_once
        ):
            with self.assertRaisesRegex(OSError, "simulated preflight second-file failure"):
                http_receiver.refresh_authorized_execution_baseline(
                    authorization_id, readback_token
                )

        canonical = next(
            item for item in http_receiver.load_action_audit()["actions"]
            if item["action_id"] == confirmed["action_id"]
        )
        session = http_receiver.load_execution_preflight()["session"]
        self.assertEqual("authorized", session["state"])
        self.assertEqual(canonical["execution_baseline_hash"], session["execution_baseline_hash"])
        self.assertEqual(
            "doc-final-recovery-reread-0002",
            canonical["execution_baseline"]["document_instance_id"],
        )
        self.assertEqual(
            canonical["execution_baseline"]["document_instance_id"],
            session["final_preconsume_document_instance_id"],
        )
        self.assertFalse(http_receiver._execution_baseline_transaction_path().exists())
        self.assertEqual(
            canonical["execution_baseline_hash"],
            http_receiver.preview_execution_authorization(authorization_id)["execution_baseline"]["hash"],
        )

    def test_manual_reconcile_globally_blocks_new_preflight_and_scope_switch_until_resolved(self) -> None:
        store_key = "store-manual-lock"
        account_a = "acct-manual-lock-a"
        account_b = "acct-manual-lock-b"
        self._bind_execution_scope(store_key, account_a)
        self._bind_execution_scope(store_key, account_b)
        http_receiver.save_agent_settings({
            "execution_mode": "supervised",
            "store_key": store_key,
            "qianchuan_account_key": account_a,
            "max_daily_execution_count": 10,
            "max_daily_budget_reduction": 10_000,
            "execution_cooldown_minutes": 0,
        })

        def confirmed_action(account_key: str, plan_id: str) -> dict:
            return http_receiver.confirm_action_draft(http_receiver.build_action_draft(
                operation_type="adjust_budget",
                operation_label="降低预算 20%",
                target_kind="qianchuan_plan",
                target_id=plan_id,
                target_name=f"计划 {plan_id}",
                account_key=account_key,
                account_label=f"账户 {account_key}",
                field="预算",
                current_value=500.0,
                target_value=400.0,
                source="qianchuan",
                page_type="campaigns",
                captured_at_ms=int(time.time() * 1000),
                quality_score=90,
                confidence="high",
                promotion_context=execution_promotion_context(
                    store_key, account_key, plan_id
                ),
            ))

        locked = confirmed_action(account_a, "plan-manual-pending")
        locked = http_receiver.transition_action(locked, "executing", allow_execution=True)
        locked.update({
            "manual_reconcile_required": True,
            "manual_reconcile_required_at_ms": int(time.time() * 1000),
            "execution_started_at_ms": int(time.time() * 1000),
        })
        http_receiver._atomic_json_write(
            http_receiver._action_audit_path(),
            {
                "schema_version": 1,
                "updated_at": http_receiver._now_label(),
                "execution_enabled": True,
                "actions": [locked],
            },
        )
        http_receiver._save_execution_preflight({
            "session_id": "9" * 24,
            "action_id": locked["action_id"],
            "state": "manual_reconcile_required",
            "authorization_consumed": True,
            "write_enabled": False,
            "execution_enabled": False,
        })
        same_account = confirmed_action(account_a, "plan-manual-same-account")
        with self.assertRaisesRegex(ValueError, "仍有投放动作等待人工核对"):
            http_receiver.start_execution_preflight(same_account["action_id"])

        with self.assertRaisesRegex(ValueError, "暂不能切换或解绑店铺账号"):
            http_receiver.save_agent_settings({"qianchuan_account_key": account_b})

        resolved = http_receiver.transition_action(locked, "succeeded", allow_execution=True)
        resolved = http_receiver.transition_action(resolved, "verified")
        resolved["manual_reconcile_required"] = False
        audit = http_receiver.load_action_audit()
        http_receiver._atomic_json_write(
            http_receiver._action_audit_path(),
            {
                "schema_version": 1,
                "updated_at": http_receiver._now_label(),
                "execution_enabled": True,
                "actions": [
                    resolved if item.get("action_id") == resolved["action_id"] else item
                    for item in audit["actions"]
                ],
            },
        )
        http_receiver._save_execution_preflight({
            "session_id": "9" * 24,
            "action_id": resolved["action_id"],
            "state": "completed",
            "authorization_consumed": True,
            "write_enabled": False,
            "execution_enabled": False,
        })
        http_receiver.save_agent_settings({"qianchuan_account_key": account_b})
        other_account = confirmed_action(account_b, "plan-manual-other-account")
        report = http_receiver.start_execution_preflight(other_account["action_id"])
        self.assertEqual("awaiting_reread", report["state"])
        self.assertEqual(account_b, report["session"]["account_key"])

    def test_manual_reconcile_archive_releases_unrelated_plan_but_permanently_blocks_same_plan(self) -> None:
        store_raw_id = "810000091"
        account_raw_id = "710000091"
        store_key = http_receiver._local_identity_key("qianchuan_shop_id", store_raw_id)
        account_key = http_receiver._local_identity_key("qianchuan_advertiser_id", account_raw_id)
        locked, authorization_id, report = self._seed_manual_reconcile_execution(
            store_key=store_key,
            account_key=account_key,
        )
        phrase = report["action"]["archive_confirmation_text"]
        with self.assertRaisesRegex(ValueError, "归档确认口令不一致"):
            http_receiver.archive_manual_reconcile(locked["action_id"], f"{phrase}错")
        unchanged = next(
            item for item in http_receiver.load_action_audit()["actions"]
            if item["action_id"] == locked["action_id"]
        )
        self.assertNotIn("manual_reconcile_archived", unchanged)

        archived = http_receiver.archive_manual_reconcile(
            locked["action_id"],
            phrase,
            "已在官方后台核对，但平台仍无法确认提交结果。",
        )
        self.assertFalse(archived["archived"]["result_known"])
        self.assertFalse(archived["archived"]["safe_to_retry"])
        self.assertTrue(archived["archived"]["plan_retry_blocked"])
        self.assertEqual("manual_reconcile_archived", archived["preflight"]["state"])
        canonical = next(
            item for item in http_receiver.load_action_audit()["actions"]
            if item["action_id"] == locked["action_id"]
        )
        self.assertEqual("executing", canonical["state"])
        self.assertTrue(canonical["manual_reconcile_required"])
        self.assertTrue(canonical["manual_reconcile_archived"])
        self.assertTrue(canonical["plan_retry_blocked"])
        self.assertEqual(authorization_id, canonical["execution_authorization_id"])

        repeated = http_receiver.archive_manual_reconcile(locked["action_id"], phrase)
        self.assertTrue(repeated["idempotent_replay"])
        other_account_key = http_receiver._local_identity_key(
            "qianchuan_advertiser_id", "710000092"
        )
        self._bind_execution_scope(store_key, other_account_key)
        http_receiver.save_agent_settings({"qianchuan_account_key": other_account_key})
        http_receiver.save_agent_settings({"qianchuan_account_key": account_key})

        def draft_for(plan_id: str, target_value: float) -> dict:
            return http_receiver.build_action_draft(
                operation_type="adjust_budget",
                operation_label="降低预算",
                target_kind="qianchuan_plan",
                target_id=plan_id,
                target_name=f"归档后计划 {plan_id}",
                account_key=account_key,
                account_label="归档测试账号",
                field="预算",
                current_value=500.0,
                target_value=target_value,
                source="qianchuan",
                page_type="campaigns",
                captured_at_ms=int(time.time() * 1000),
                quality_score=90,
                confidence="high",
                promotion_context=execution_promotion_context(
                    store_key, account_key, plan_id
                ),
            )

        with self.assertRaisesRegex(ValueError, "已有正在执行或观察中的动作"):
            http_receiver.confirm_action_draft(draft_for("plan-archive", 450.0))

        unrelated = http_receiver.confirm_action_draft(draft_for("plan-unrelated", 400.0))
        unrelated_report = http_receiver.start_execution_preflight(unrelated["action_id"])
        self.assertEqual("awaiting_reread", unrelated_report["state"])
        self.assertIn(locked["action_id"], unrelated_report["recoverable_readback_action_ids"])
        recovered = http_receiver.recover_execution_readback_job(locked["action_id"])
        self.assertEqual(locked["action_id"], recovered["action_id"])
        self.assertEqual(authorization_id, recovered["authorization_id"])
        self.assertTrue(recovered["manual_reconcile_archived"])
        self.assertTrue(recovered["plan_retry_blocked"])
        self.assertFalse(recovered["write_enabled"])
        self.assertFalse(recovered["execution_enabled"])

        archived_action = next(
            item for item in http_receiver.load_action_audit()["actions"]
            if item["action_id"] == locked["action_id"]
        )
        submitted_at_ms = int(archived_action["execution_started_at_ms"])
        readback_token = "9" * 64
        target_snapshot = self._execution_plan_snapshot(
            store_key=store_key,
            account_key=account_key,
            plan_id="plan-archive",
            budget=400.0,
            captured_at_ms=submitted_at_ms + 2_000,
            navigation_started_at_ms=submitted_at_ms + 1_000,
            document_instance_id="doc-archived-independent-readback-0002",
            targeted=True,
            store_raw_id=store_raw_id,
            account_raw_id=account_raw_id,
        )
        http_receiver.save_targeted_execution_snapshot(
            "qianchuan",
            target_snapshot,
            {"store_key": store_key, "account_key": account_key},
            {
                "purpose": "execution_readback",
                "action_id": locked["action_id"],
                "authorization_id": authorization_id,
                "readback_token": readback_token,
            },
        )
        verified = http_receiver.verify_execution_result(locked["action_id"], readback_token)
        self.assertTrue(verified["verified"])
        resolved_archived = next(
            item for item in http_receiver.load_action_audit()["actions"]
            if item["action_id"] == locked["action_id"]
        )
        self.assertEqual("verified", resolved_archived["state"])
        self.assertFalse(resolved_archived["manual_reconcile_required"])
        self.assertTrue(resolved_archived["manual_reconcile_archived"])
        self.assertTrue(resolved_archived["plan_retry_blocked"])
        with self.assertRaisesRegex(ValueError, "已有正在执行或观察中的动作"):
            http_receiver.confirm_action_draft(draft_for("plan-archive", 425.0))

    def test_recoverable_archived_readbacks_keep_all_actions_in_newest_first_order(self) -> None:
        base_ms = 1_700_000_000_000
        actions = [
            {
                "action_id": f"{index:024x}",
                "execution_authorization_id": f"{index + 1:032x}",
                "state": "executing" if index % 2 == 0 else "succeeded",
                "manual_reconcile_required": True,
                "manual_reconcile_archived": True,
                "plan_retry_blocked": True,
                "manual_reconcile_archived_at_ms": base_ms + index,
            }
            for index in range(60)
        ]
        audit = {"actions": actions}

        recovered_actions = http_receiver._recoverable_archived_readback_actions(audit)
        recovered_ids = http_receiver._recoverable_archived_readback_action_ids(audit)
        expected_ids = [f"{index:024x}" for index in reversed(range(60))]

        self.assertEqual(60, len(recovered_actions))
        self.assertEqual(60, len(recovered_ids))
        self.assertEqual(expected_ids, recovered_ids)
        self.assertEqual(expected_ids, [item["action_id"] for item in recovered_actions])
        self.assertEqual(f"{59:024x}", recovered_ids[0])
        self.assertEqual(f"{0:024x}", recovered_ids[-1])

    def test_manual_reconcile_archive_second_file_failure_rolls_forward_without_unlocking_plan(self) -> None:
        locked, authorization_id, report = self._seed_manual_reconcile_execution(
            plan_id="plan-archive-crash",
            authorization_id="8" * 32,
        )
        phrase = report["action"]["archive_confirmation_text"]
        with patch.object(http_receiver, "_save_execution_preflight", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                http_receiver.archive_manual_reconcile(locked["action_id"], phrase)
        self.assertTrue(http_receiver._manual_reconcile_archive_transaction_path().exists())

        canonical = next(
            item for item in http_receiver.load_action_audit()["actions"]
            if item["action_id"] == locked["action_id"]
        )
        session = http_receiver.load_execution_preflight()["session"]
        self.assertTrue(canonical["manual_reconcile_archived"])
        self.assertTrue(canonical["plan_retry_blocked"])
        self.assertEqual("executing", canonical["state"])
        self.assertEqual("manual_reconcile_archived", session["state"])
        self.assertEqual(authorization_id, session["authorization_id"])
        self.assertFalse(http_receiver._manual_reconcile_archive_transaction_path().exists())

    def test_consumed_action_blocks_stop_and_second_plan_preflight(self) -> None:
        confirmed, authorization_id, _ = self._seed_authorized_execution()
        consumed = http_receiver.consume_execution_authorization(authorization_id)
        with self.assertRaisesRegex(ValueError, "不能把进行中的动作标记为已停止"):
            http_receiver.stop_execution_preflight(consumed["session_id"])
        second = http_receiver.build_action_draft(
            operation_type="adjust_budget",
            operation_label="降低预算 20%",
            target_kind="qianchuan_plan",
            target_id="plan-safe2",
            target_name="第二计划",
            account_key="acct-safe1",
            account_label="事务安全测试账号",
            field="预算",
            current_value=500.0,
            target_value=400.0,
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=int(time.time() * 1000),
            quality_score=90,
            confidence="high",
            promotion_context={
                "promotion_mode": "standard",
                "account_scope": {"store_id": "store-1", "account_id": "acct-safe1"},
                "strategy_id": "plan-safe2",
                "metric_contract": {"definition": "pay_roi", "version": "v1"},
                "data_quality": {"confidence": "high", "freshness_seconds": 0, "completeness": 1.0},
            },
        )
        second = http_receiver.confirm_action_draft(second)
        with patch.object(http_receiver, "_supervised_draft_collection_gate", return_value={"ready": True}):
            with self.assertRaisesRegex(ValueError, "等待页面回执、验收或人工核对"):
                http_receiver.start_execution_preflight(second["action_id"])
        self.assertEqual("executing", next(
            item for item in http_receiver.load_action_audit()["actions"]
            if item["action_id"] == confirmed["action_id"]
        )["state"])

    def test_disabling_supervised_mode_revokes_unconsumed_authorization(self) -> None:
        _, authorization_id, _ = self._seed_authorized_execution()
        http_receiver.save_agent_settings({"execution_mode": "observe"})
        session = http_receiver.load_execution_preflight()["session"]
        self.assertEqual("invalidated", session["state"])
        self.assertFalse(session["authorization_consumed"])
        with self.assertRaisesRegex(ValueError, "已关闭受监督执行"):
            http_receiver.preview_execution_authorization(authorization_id)
        with self.assertRaisesRegex(ValueError, "已关闭受监督执行"):
            http_receiver.consume_execution_authorization(authorization_id)

    def test_execution_requires_current_bidirectional_catalog_binding(self) -> None:
        confirmed, authorization_id, _ = self._seed_authorized_execution()
        http_receiver._atomic_json_write(
            http_receiver._store_catalog_path(),
            {
                "schema_version": 1,
                "stores": [{
                    "key": "store-1",
                    "account_keys": [],
                    "confidence": "high",
                    "identity_source": "test",
                }],
            },
        )
        with self.assertRaisesRegex(ValueError, "双向绑定不完整"):
            http_receiver.consume_execution_authorization(authorization_id)
        action = next(
            item for item in http_receiver.load_action_audit()["actions"]
            if item["action_id"] == confirmed["action_id"]
        )
        self.assertEqual("confirmed", action["state"])
        self.assertFalse(http_receiver.load_execution_preflight()["session"]["authorization_consumed"])

    def test_action_audit_trimming_never_discards_live_execution_lock(self) -> None:
        http_receiver.save_agent_settings({
            "store_key": "store-retain",
            "qianchuan_account_key": "acct-retain",
        })
        now_ms = int(time.time() * 1000)
        locked_action_id = "f" * 24
        locked = {
            "action_id": locked_action_id,
            "state": "executing",
            "state_updated_at_ms": 1,
            "created_at_ms": 1,
            "manual_reconcile_required": True,
            "execution_attempt_id": "e" * 32,
            "execution_started_at_ms": now_ms - 1_000,
            "target_ref": {"account_key": "acct-retain", "id": "plan-locked"},
        }
        terminal = [
            {
                "action_id": f"{index:024x}",
                "state": "cancelled",
                "state_updated_at_ms": now_ms - index,
                "created_at_ms": now_ms - index,
                "target_ref": {"account_key": "acct-old", "id": f"plan-old-{index}"},
            }
            for index in range(1, 511)
        ]
        http_receiver._atomic_json_write(
            http_receiver._action_audit_path(),
            {"schema_version": 1, "updated_at": http_receiver._now_label(), "execution_enabled": False, "actions": [locked, *terminal]},
        )
        draft = http_receiver.build_action_draft(
            operation_type="adjust_budget",
            operation_label="降低预算",
            target_kind="qianchuan_plan",
            target_id="plan-new",
            target_name="新计划",
            account_key="acct-retain",
            account_label="留存测试账号",
            field="预算",
            current_value=500.0,
            target_value=400.0,
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=now_ms,
            quality_score=90,
            confidence="high",
            promotion_context={
                "promotion_mode": "standard",
                "account_scope": {"store_id": "store-retain", "account_id": "acct-retain"},
                "strategy_id": "plan-new",
                "metric_contract": {"definition": "pay_roi", "version": "v1"},
                "data_quality": {"confidence": "high", "freshness_seconds": 0, "completeness": 1.0},
            },
        )
        confirmed = http_receiver.confirm_action_draft(draft)
        retained = http_receiver.load_action_audit()["actions"]
        self.assertIn(locked_action_id, {item.get("action_id") for item in retained})
        self.assertIn(confirmed["action_id"], {item.get("action_id") for item in retained})
        self.assertEqual(502, len(retained))

    def test_supervised_preflight_rejects_budget_increase(self) -> None:
        http_receiver.save_agent_settings({"execution_mode": "supervised"})
        now_ms = int(time.time() * 1000)
        draft = http_receiver.build_action_draft(
            operation_type="adjust_budget",
            operation_label="增加预算 10%",
            target_kind="qianchuan_plan",
            target_id="plan_scale001",
            promotion_context={"promotion_mode": "standard", "account_scope": {"store_id": "store-1", "account_id": "acct_scale001"}, "strategy_id": "plan_scale001", "metric_contract": {"definition": "pay_roi", "version": "v1"}, "data_quality": {"confidence": "high", "freshness_seconds": 0, "completeness": 0.9}},
            target_name="放量计划",
            account_key="acct_scale001",
            account_label="放量账号",
            field="预算",
            current_value=500.0,
            target_value=550.0,
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=now_ms,
            quality_score=90,
            confidence="high",
        )
        confirmed = http_receiver.confirm_action_draft(draft)
        with self.assertRaisesRegex(ValueError, "不能增加预算"):
            http_receiver.start_execution_preflight(confirmed["action_id"])

    def test_execution_quota_blocks_daily_count_budget_impact_and_cooldown(self) -> None:
        now_ms = int(time.time() * 1000)
        http_receiver.save_agent_settings({
            "max_daily_execution_count": 1,
            "max_daily_budget_reduction": 150,
            "execution_cooldown_minutes": 30,
        })
        completed = http_receiver.build_action_draft(
            operation_type="adjust_budget",
            operation_label="降低预算",
            target_kind="qianchuan_plan",
            target_id="plan_done",
            target_name="已执行计划",
            account_key="acct_quota",
            account_label="额度测试账号",
            field="预算",
            current_value=500.0,
            target_value=400.0,
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=now_ms,
            quality_score=90,
            confidence="high",
            now_ms=now_ms,
        )
        completed.update({"state": "verified", "execution_reported_at_ms": now_ms - 60_000})
        http_receiver._atomic_json_write(
            http_receiver._action_audit_path(),
            {"schema_version": 1, "actions": [completed], "execution_enabled": True},
        )
        proposed = http_receiver.build_action_draft(
            operation_type="adjust_budget",
            operation_label="再次降低预算",
            target_kind="qianchuan_plan",
            target_id="plan_next",
            target_name="待执行计划",
            account_key="acct_quota",
            account_label="额度测试账号",
            field="预算",
            current_value=500.0,
            target_value=400.0,
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=now_ms,
            quality_score=90,
            confidence="high",
            now_ms=now_ms,
        )
        quota = http_receiver.assess_execution_quota(proposed, now_ms=now_ms)
        codes = {item["code"] for item in quota["blocked_reasons"]}
        self.assertFalse(quota["allowed"])
        self.assertEqual(1, quota["today_execution_count"])
        self.assertEqual(100.0, quota["today_budget_reduction"])
        self.assertIn("DAILY_EXECUTION_COUNT_LIMIT", codes)
        self.assertIn("DAILY_BUDGET_REDUCTION_LIMIT", codes)
        self.assertIn("EXECUTION_COOLDOWN", codes)

    def test_execution_quota_counts_rolled_back_write_and_restore_as_two_attempts(self) -> None:
        now_ms = int(time.time() * 1000)
        account_key = "acct-quota-rollback"
        http_receiver.save_agent_settings({
            "max_daily_execution_count": 2,
            "max_daily_budget_reduction": 1_000,
            "execution_cooldown_minutes": 30,
        })
        http_receiver._atomic_json_write(
            http_receiver._action_audit_path(),
            {
                "schema_version": 1,
                "execution_enabled": True,
                "actions": [
                    {
                        "action_id": "1" * 24,
                        "state": "rolled_back",
                        "target_ref": {"account_key": account_key},
                        "change": {"current_value": 500.0, "target_value": 400.0},
                        "execution_started_at_ms": now_ms - 120_000,
                    },
                    {
                        "action_id": "2" * 24,
                        "state": "verified",
                        "operation_type": "restore_budget",
                        "target_ref": {"account_key": account_key},
                        "change": {"current_value": 400.0, "target_value": 500.0},
                        "execution_started_at_ms": now_ms - 60_000,
                    },
                ],
            },
        )
        quota = http_receiver.assess_execution_quota(
            {
                "operation_type": "adjust_budget",
                "target_ref": {"account_key": account_key},
                "change": {"current_value": 500.0, "target_value": 450.0},
            },
            now_ms=now_ms,
        )
        codes = {item["code"] for item in quota["blocked_reasons"]}
        self.assertEqual(2, quota["today_execution_count"])
        self.assertIn("DAILY_EXECUTION_COUNT_LIMIT", codes)
        self.assertIn("EXECUTION_COOLDOWN", codes)

    def test_manual_reconcile_attempt_uses_started_time_for_cooldown(self) -> None:
        now_ms = int(time.time() * 1000)
        account_key = "acct-quota-unknown"
        http_receiver.save_agent_settings({"execution_cooldown_minutes": 30})
        http_receiver._atomic_json_write(
            http_receiver._action_audit_path(),
            {
                "schema_version": 1,
                "execution_enabled": True,
                "actions": [{
                    "action_id": "3" * 24,
                    "state": "executing",
                    "manual_reconcile_required": True,
                    "target_ref": {"account_key": account_key},
                    "change": {"current_value": 500.0, "target_value": 400.0},
                    "execution_started_at_ms": now_ms - 60_000,
                }],
            },
        )
        quota = http_receiver.assess_execution_quota(
            {
                "operation_type": "adjust_budget",
                "target_ref": {"account_key": account_key},
                "change": {"current_value": 500.0, "target_value": 450.0},
            },
            now_ms=now_ms,
        )
        self.assertIn("EXECUTION_COOLDOWN", {item["code"] for item in quota["blocked_reasons"]})

    def test_shadow_execution_requires_manual_claim_and_matches_later_readback(self) -> None:
        captured_at = int(time.time() * 1000)
        snapshot = {
            "schema_version": 2,
            "page_type": "campaigns",
            "captured_at": captured_at,
            "store": {"key": "store-shadow", "confidence": "high"},
            "account": {"key": "acct_shadow123", "store_key": "store-shadow", "label": "影子测试账号", "confidence": "high"},
            "promotion_context": execution_promotion_context(
                "store-shadow", "acct_shadow123", "plan_shadow01"
            ),
            "quality": {"score": 90, "row_count": 1},
            "tables": [
                {
                    "headers": ["计划ID", "计划名称", "日预算", "消耗", "支付 ROI", "成交订单"],
                    "rows": [["plan_shadow01", "影子计划", "500", "300", "0.60", "2"]],
                }
            ],
        }
        http_receiver.save_data("qianchuan", snapshot)
        http_receiver.save_agent_settings({"store_key": "store-shadow", "qianchuan_account_key": "acct_shadow123"})
        draft = http_receiver.build_plan_recommendations()[0]["action_params"]
        confirmed = http_receiver.confirm_action_draft(draft)

        before_claim = http_receiver.build_shadow_execution_report()
        self.assertEqual(before_claim["items"][0]["status"], "awaiting_manual_action")
        marker = http_receiver.mark_action_manually_applied(confirmed["action_id"])
        self.assertFalse(marker["execution_enabled"])
        awaiting = http_receiver.build_shadow_execution_report()
        self.assertEqual(awaiting["items"][0]["status"], "awaiting_readback")

        snapshot["captured_at"] = marker["reported_applied_at_ms"] + 1000
        snapshot["tables"][0]["rows"][0][2] = "400"
        http_receiver.save_data("qianchuan", snapshot)
        verified = http_receiver.build_shadow_execution_report()
        self.assertFalse(verified["execution_enabled"])
        self.assertEqual(verified["summary"]["matched"], 1)
        self.assertEqual(verified["items"][0]["status"], "matched")
        self.assertEqual(verified["items"][0]["readback"]["current_value"], 400)

    def test_inventory_alert_uses_days_of_cover(self) -> None:
        http_receiver.save_data(
            "doudian",
            {
                "schema_version": 2,
                "page_type": "inventory",
                "quality": {"score": 85, "metric_count": 0, "row_count": 2},
                "tables": [
                    {
                        "headers": ["商品名称", "可售库存", "近7日销量"],
                        "rows": [["商品 A", "14", "70"], ["商品 B", "0", "3"]],
                    }
                ],
            },
        )
        alerts = http_receiver.build_inventory_alerts()
        product_a = next(item for item in alerts if item["product"] == "商品 A")
        product_b = next(item for item in alerts if item["product"] == "商品 B")
        self.assertEqual(product_a["title"], "预计即将售罄")
        self.assertAlmostEqual(product_a["evidence"]["days_of_cover"], 1.4)
        self.assertEqual(product_b["title"], "已缺货")

    def test_qianchuan_video_library_builds_live_creative_actions(self) -> None:
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "video_library",
                "quality": {"score": 90, "metric_count": 0, "row_count": 3},
                "tables": [
                    {
                        "headers": ["视频", "素材评估", "消耗(元)", "整体支付ROI", "成交订单数", "标签", "时长"],
                        "rows": [
                            ["开场引流 A", "优质", "500", "2.20", "6", "直播引流", "00:18"],
                            ["低效素材 B", "", "300", "0", "0", "直播引流", "00:24"],
                            ["待测素材 C", "", "0", "-", "0", "商品卖点", "00:15"],
                        ],
                    }
                ],
            },
        )
        analysis = http_receiver.build_qianchuan_creative_analysis()
        self.assertEqual(analysis["summary"]["total_videos"], 3)
        self.assertEqual(analysis["summary"]["risky_videos"], 1)
        self.assertEqual(analysis["summary"]["untested_videos"], 1)
        self.assertEqual(analysis["summary"]["high_potential_videos"], 1)
        self.assertEqual(analysis["videos"][0]["name"], "低效素材 B")
        self.assertTrue(any("高消耗低转化" in item["title"] for item in analysis["recommendations"]))
        self.assertEqual(analysis["governance_policy"]["min_spend_for_action"], 100.0)
        self.assertEqual(analysis["governance_policy"]["roi_target"], 1.5)
        self.assertEqual(analysis["governance_policy"]["min_orders_for_scale"], 3)
        self.assertFalse(analysis["governance_policy"]["automatic_delete_enabled"])
        self.assertFalse(analysis["governance_policy"]["platform_write_enabled"])

    def test_creative_analysis_builds_funnel_diagnosis_and_single_variable_tests(self) -> None:
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "video_library",
                "quality": {"score": 90, "row_count": 2},
                "tables": [{
                    "headers": ["视频", "消耗", "展示", "点击数", "成交订单", "支付ROI"],
                    "rows": [
                        ["钩子偏弱", "200", "10000", "50", "1", "0.5"],
                        ["承接偏弱", "200", "5000", "100", "0", "0"],
                    ],
                }],
            },
        )
        analysis = http_receiver.build_qianchuan_creative_analysis()
        self.assertEqual(analysis["summary"]["hook_bottleneck_videos"], 1)
        self.assertEqual(analysis["summary"]["conversion_bottleneck_videos"], 1)
        self.assertEqual(analysis["videos"][0]["evidence"]["ctr"], 2.0)
        self.assertTrue(any(item["stage"] == "hook" for item in analysis["test_matrix"]))
        self.assertTrue(any(item["stage"] == "conversion" for item in analysis["test_matrix"]))

    def test_stale_creative_snapshot_only_generates_resync_task(self) -> None:
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "video_library",
                "captured_at": int(time.time() * 1000) - (http_receiver.CREATIVE_ANALYSIS_STALE_SECONDS + 1) * 1000,
                "quality": {"score": 90, "row_count": 1},
                "tables": [{
                    "headers": ["视频", "消耗", "成交订单", "支付ROI"],
                    "rows": [["过期低效素材", "500", "0", "0"]],
                }],
            },
        )
        analysis = http_receiver.build_qianchuan_creative_analysis()
        self.assertEqual("stale", analysis["data_status"])
        self.assertEqual(0, analysis["summary"]["total_videos"])
        self.assertEqual(1, analysis["summary"]["stale_records_ignored"])
        self.assertEqual("重新同步千川视频库", analysis["recommendations"][0]["title"])
        self.assertEqual(0, analysis["memory"]["observation_count"])

    def test_creative_analysis_ignores_aggregate_video_count_row(self) -> None:
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "video_library",
                "captured_at": int(time.time() * 1000),
                "quality": {"score": 90, "row_count": 2},
                "tables": [{
                    "headers": ["视频", "消耗", "成交订单", "支付ROI"],
                    "rows": [["共40个视频", "0", "0", "0"], ["真实素材 A", "10", "0", "0"]],
                }],
            },
        )
        analysis = http_receiver.build_qianchuan_creative_analysis()
        self.assertEqual(["真实素材 A"], [item["name"] for item in analysis["videos"]])

    def test_value_ledger_is_conservative_and_does_not_claim_revenue(self) -> None:
        original = http_receiver.build_execution_effectiveness_report
        http_receiver.build_execution_effectiveness_report = lambda: {
            "items": [
                {"status": "effective", "account_key": "acct_a", "before": {"spend": 300}, "change": {"from": 500, "to": 400}},
                {"status": "ineffective", "account_key": "acct_a", "before": {"spend": 200}, "change": {"from": 400, "to": 320}},
                {"status": "waiting", "account_key": "acct_a", "before": {"spend": 100}, "change": {"from": 300, "to": 240}},
            ]
        }
        try:
            with patch.object(http_receiver, "build_store_catalog", return_value={"selected_store_key": "store_a", "stores": [{"key": "store_a", "account_keys": ["acct_a"]}]}):
                ledger = http_receiver.build_value_ledger()
        finally:
            http_receiver.build_execution_effectiveness_report = original
        self.assertEqual(ledger["summary"]["effective_rate"], 50.0)
        self.assertEqual(ledger["summary"]["protected_budget_capacity"], 100.0)
        self.assertEqual(ledger["summary"]["reviewed_spend"], 500.0)
        self.assertEqual(ledger["summary"]["readback_promotion_actions"], 3)
        self.assertEqual(ledger["summary"]["evaluated_promotion_actions"], 2)
        self.assertTrue(ledger["first_verified_value_complete"])
        self.assertIn("不等同于实际节省", ledger["note"])

    def test_content_memory_deduplicates_snapshots_and_stays_account_scoped(self) -> None:
        captured_at = int(time.time() * 1000)
        self._save_bound_qianchuan(
            {
                "schema_version": 2,
                "page_type": "video_library",
                "captured_at": captured_at,
                "account": {"key": "acct_memory01", "label": "内容记忆店铺", "confidence": "high"},
                "quality": {"score": 90, "row_count": 3},
                "tables": [{
                    "headers": ["视频", "消耗", "点击率", "成交订单", "支付ROI", "标签", "来源", "时长"],
                    "rows": [
                        ["胜出 A", "300", "2.0", "5", "2.2", "利益点", "自制", "00:18"],
                        ["胜出 B", "280", "1.8", "4", "2.0", "利益点", "自制", "00:22"],
                        ["风险 C", "250", "0.5", "0", "0", "利益点", "自制", "00:20"],
                    ],
                }],
            },
            store_key="store-memory01",
            account_key="acct_memory01",
        )
        first = http_receiver.build_qianchuan_creative_analysis()["memory"]
        second = http_receiver.build_qianchuan_creative_analysis()["memory"]
        self.assertEqual(first["observation_count"], 3)
        self.assertEqual(second["observation_count"], 3)
        tag_pattern = next(item for item in second["patterns"] if item["dimension"] == "标签" and item["value"] == "利益点")
        self.assertEqual(tag_pattern["direction"], "winner")
        self.assertEqual(tag_pattern["confidence"], "medium")
        report = http_receiver.generate_daily_report("2026-08-01")
        self.assertIn("本店内容记忆", report["content"])
        self.assertIn("利益点", report["content"])

        self._bind_execution_scope("store-other01", "acct_other01")
        http_receiver.save_agent_settings({
            "store_key": "store-other01",
            "qianchuan_account_key": "acct_other01",
        })
        other = http_receiver.build_qianchuan_creative_analysis()["memory"]
        self.assertEqual(other["observation_count"], 0)

    def test_qianchuan_accounts_are_partitioned_and_selectable(self) -> None:
        def snapshot(account_key: str, label: str, video: str) -> dict:
            return {
                "page_type": "video_library",
                "account": {"key": account_key, "label": label, "confidence": "high"},
                "quality": {"score": 90, "row_count": 1},
                "tables": [{"headers": ["视频", "素材评估", "消耗(元)"], "rows": [[video, "优质", "100"]]}],
            }

        self._save_bound_qianchuan(
            snapshot("acct_aaaa1111", "千川账号 A", "素材 A"),
            store_key="store-aaaa1111",
            account_key="acct_aaaa1111",
        )
        self._save_bound_qianchuan(
            snapshot("acct_bbbb2222", "千川账号 B", "素材 B"),
            store_key="store-bbbb2222",
            account_key="acct_bbbb2222",
        )
        self.assertEqual(len(http_receiver.list_qianchuan_accounts()), 2)

        http_receiver.save_agent_settings({
            "store_key": "store-aaaa1111",
            "qianchuan_account_key": "acct_aaaa1111",
        })
        account_a = http_receiver.build_qianchuan_creative_analysis()
        self.assertEqual([item["name"] for item in account_a["videos"]], ["素材 A"])

        http_receiver.save_agent_settings({
            "store_key": "store-bbbb2222",
            "qianchuan_account_key": "acct_bbbb2222",
        })
        account_b = http_receiver.build_qianchuan_creative_analysis()
        self.assertEqual([item["name"] for item in account_b["videos"]], ["素材 B"])

    def test_qianchuan_account_catalog_filters_false_accounts_without_merging_same_name_accounts(self) -> None:
        http_receiver._atomic_json_write(
            http_receiver._account_catalog_path(),
            {
                "accounts": [
                    {"key": "acct_real0001", "label": "真实旗舰店", "last_seen": "2026-07-27 10:00:00"},
                    {"key": "acct_duplicate", "label": " 真实旗舰店 ", "last_seen": "2026-07-27 09:00:00"},
                    {"key": "acct_store000", "label": "店铺", "last_seen": "2026-07-27 08:00:00"},
                    {"key": "acct_funds000", "label": "我的资金 账户明细 账户余额 0.00 元 立即充值", "last_seen": "2026-07-27 07:00:00"},
                    {"key": "acct_id000000", "label": "ID：", "last_seen": "2026-07-27 06:00:00"},
                ]
            },
        )
        accounts = http_receiver.list_qianchuan_accounts()
        self.assertEqual(
            [(item["key"], item["label"]) for item in accounts],
            [("acct_real0001", "千川账户 EA0001"), ("acct_duplicate", "千川账户 CCDCAE")],
        )

    def test_same_qianchuan_account_label_reuses_canonical_key_across_pages(self) -> None:
        first = http_receiver.save_data(
            "qianchuan",
            {
                "page_type": "overview",
                "account": {
                    "key": "acct_route001",
                    "label": "跨页面旗舰店",
                    "confidence": "medium",
                    "identity_source": "account_label",
                },
                "quality": {"score": 80},
            },
        )
        second = http_receiver.save_data(
            "qianchuan",
            {
                "page_type": "campaigns",
                "account": {
                    "key": "acct_route002",
                    "label": "跨页面旗舰店",
                    "confidence": "high",
                    "identity_source": "platform_id",
                },
                "quality": {"score": 90},
            },
            trusted_origin="official_api_oauth_client",
        )
        self.assertEqual(first["data"]["account"]["key"], "acct_route001")
        self.assertEqual(second["data"]["account"]["key"], "acct_route002")
        self.assertTrue((http_receiver.DATA_DIR / "qianchuan_accounts" / "acct_route002" / "campaigns.json").exists())
        accounts = http_receiver.list_qianchuan_accounts()
        self.assertEqual(len(accounts), 2)

    def test_same_name_platform_accounts_remain_separate(self) -> None:
        for key in ("acct_stable001", "acct_stable002"):
            saved = http_receiver.save_data(
                "qianchuan",
                {
                    "page_type": "campaigns",
                    "account": {
                        "key": key,
                        "label": "同名旗舰店",
                        "confidence": "high",
                        "identity_source": "platform_id",
                    },
                    "quality": {"score": 90},
                },
                trusted_origin="official_api_oauth_client",
            )
            self.assertEqual(saved["data"]["account"]["key"], key)
        self.assertEqual(
            {item["key"] for item in http_receiver.list_qianchuan_accounts()},
            {"acct_stable001", "acct_stable002"},
        )

    def test_invalid_calendar_report_date_is_rejected_before_writing(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid calendar date"):
            http_receiver.generate_daily_report("2026-99-99")
        self.assertFalse((http_receiver.DATA_DIR / "reports" / "2026-99-99.md").exists())

    def test_invalid_calendar_business_date_is_rejected(self) -> None:
        for value in ("2026-99-99", "2026-02-30"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "invalid business_date"):
                http_receiver._task_scope_key("store_calendar", value)

    def test_settings_and_daily_report_are_local_and_configurable(self) -> None:
        settings = http_receiver.save_agent_settings(
            {"roi_target": 2.0, "low_inventory_threshold": 20, "daily_report_time": "08:30"}
        )
        self.assertEqual(settings["roi_target"], 2.0)
        self.assertEqual(settings["daily_report_time"], "08:30")
        report = http_receiver.generate_daily_report("2026-07-22")
        report_path = Path(report["path"])
        self.assertTrue(report_path.exists())
        self.assertIn("千川计划调整建议", report_path.read_text(encoding="utf-8"))

    def test_daily_report_enabled_rejects_string_boolean(self) -> None:
        with self.assertRaisesRegex(ValueError, "daily_report_enabled must be true or false"):
            http_receiver.save_agent_settings({"daily_report_enabled": "false"})

    def test_numeric_settings_are_bounded_and_never_coerce_boolean_or_fractional_values(self) -> None:
        http_receiver.save_agent_settings({})
        settings_path = http_receiver._settings_path()
        original = settings_path.read_bytes()
        invalid_values = (
            ("roi_target", True),
            ("min_spend_for_action", 0),
            ("low_inventory_threshold", 1.5),
            ("critical_inventory_threshold", False),
            ("inventory_days_warning", float("inf")),
            ("report_retention_days", 0),
            ("history_retention_days", 366),
            ("max_daily_execution_count", 50.5),
            ("max_daily_budget_reduction", 1_000_001),
            ("execution_cooldown_minutes", 1441),
        )
        for field_name, value in invalid_values:
            with self.subTest(field_name=field_name, value=value):
                with self.assertRaisesRegex(ValueError, field_name):
                    http_receiver.save_agent_settings({field_name: value})
                self.assertEqual(original, settings_path.read_bytes())

        with self.assertRaisesRegex(ValueError, "critical_inventory_threshold"):
            http_receiver.save_agent_settings({
                "low_inventory_threshold": 2,
                "critical_inventory_threshold": 3,
            })
        self.assertEqual(original, settings_path.read_bytes())

        saved = http_receiver.save_agent_settings({
            "roi_target": 20,
            "min_spend_for_action": 1,
            "low_inventory_threshold": 1_000_000,
            "critical_inventory_threshold": 1_000_000,
            "inventory_days_warning": 365,
            "report_retention_days": 365,
            "history_retention_days": 1,
            "max_daily_execution_count": 50,
            "max_daily_budget_reduction": 1_000_000,
            "execution_cooldown_minutes": 0,
        })
        self.assertEqual(50, saved["max_daily_execution_count"])
        self.assertEqual(0, saved["execution_cooldown_minutes"])
        self.assertTrue(http_receiver.load_agent_settings()["daily_report_enabled"])

    def test_chengfang_scheduler_survives_one_local_store_failure(self) -> None:
        class StopAfterOne:
            wait_calls = 0

            def is_set(self):
                return self.wait_calls > 0

            def wait(self, _seconds):
                self.wait_calls += 1
                return True

        stop = StopAfterOne()
        with (
            patch.object(
                http_receiver,
                "run_chengfang_autopilot_cycle",
                side_effect=http_receiver.LocalStoreError("database locked"),
            ),
            self.assertLogs(http_receiver.logger, level="WARNING"),
        ):
            http_receiver._chengfang_autopilot_scheduler(stop)
        self.assertEqual(1, stop.wait_calls)

    def test_health_baseline_is_scoped_frozen_and_classifies_stale_or_empty(self) -> None:
        now_ms = int(time.time() * 1000)
        for index in range(5):
            promoted = http_receiver._promote_scan_health_baselines(
                complete_health_scan(f"scan_health_{index}", captured_at=now_ms + index)
            )
            self.assertEqual(len(http_receiver.SCAN_FULL_DOUDIAN_PAGE_IDS), promoted)

        entries = http_receiver.load_health_baselines()
        orders_entry = next(
            entry for entry in entries.values()
            if entry.get("scope", {}).get("filter_contract") == "orders"
        )
        self.assertEqual(90, orders_entry["baseline"]["avg_quality_score"])
        self.assertEqual(100, orders_entry["baseline"]["avg_row_count"])

        anomalous = complete_health_scan(
            "scan_health_bad",
            captured_at=now_ms,
            overrides={"orders": {"quality": {"score": 20, "row_count": 0, "metric_count": 0}}},
        )
        http_receiver._promote_scan_health_baselines(anomalous)
        orders_entry = next(
            entry for entry in http_receiver.load_health_baselines().values()
            if entry.get("scope", {}).get("filter_contract") == "orders"
        )
        self.assertEqual(5, orders_entry["baseline"]["sample_count"])
        self.assertEqual(90, orders_entry["baseline"]["avg_quality_score"])

        http_receiver.save_agent_settings({"store_key": "store_health", "qianchuan_account_key": ""})
        with (
            patch.object(http_receiver, "load_scan_status", return_value=anomalous),
            patch.object(http_receiver, "list_snapshots", return_value=[]),
        ):
            health = http_receiver.check_selector_health()
        order_alerts = [item for item in health["alerts"] if item["page"] == "doudian/orders"]
        self.assertEqual(["high"], [item["level"] for item in order_alerts])

        quick = complete_health_scan("scan_health_quick", captured_at=now_ms)
        quick["scope"] = "quick"
        with patch.object(http_receiver, "load_scan_status", return_value=quick):
            health = http_receiver.check_selector_health()
        order_alerts = [item for item in health["alerts"] if item["page"] == "doudian/orders"]
        self.assertEqual(["stale"], [item["level"] for item in order_alerts])

        missing = complete_health_scan("scan_health_missing", captured_at=now_ms)
        missing["results"] = [item for item in missing["results"] if item["id"] != "orders"]
        with (
            patch.object(http_receiver, "load_scan_status", return_value=missing),
            patch.object(http_receiver, "list_snapshots") as global_snapshots,
        ):
            health = http_receiver.check_selector_health()
        global_snapshots.assert_not_called()
        order_alerts = [item for item in health["alerts"] if item["page"] == "doudian/orders"]
        self.assertEqual(["stale"], [item["level"] for item in order_alerts])

        explicit_empty = complete_health_scan(
            "scan_health_empty",
            captured_at=now_ms,
            overrides={"orders": {"explicit_empty": True, "quality": {"score": 90, "row_count": 0}}},
        )
        with (
            patch.object(http_receiver, "load_scan_status", return_value=explicit_empty),
            patch.object(http_receiver, "list_snapshots", return_value=[]),
        ):
            health = http_receiver.check_selector_health()
        self.assertFalse(any(item["page"] == "doudian/orders" for item in health["alerts"]))

        stale = complete_health_scan(
            "scan_health_stale",
            captured_at=now_ms - (http_receiver.STALE_SECONDS + 1) * 1000,
            overrides={"orders": {"quality": {"score": 20, "row_count": 0}}},
        )
        with (
            patch.object(http_receiver, "load_scan_status", return_value=stale),
            patch.object(http_receiver, "list_snapshots", return_value=[]),
        ):
            health = http_receiver.check_selector_health()
        order_alerts = [item for item in health["alerts"] if item["page"] == "doudian/orders"]
        self.assertEqual(["stale"], [item["level"] for item in order_alerts])

        http_receiver.save_agent_settings({"store_key": "store_other", "qianchuan_account_key": ""})
        with (
            patch.object(
                http_receiver,
                "load_scan_status",
                return_value=complete_health_scan("scan_health_other", store_key="store_other", captured_at=now_ms),
            ),
            patch.object(http_receiver, "list_snapshots", return_value=[]),
        ):
            health = http_receiver.check_selector_health()
        self.assertEqual([], health["alerts"])
        self.assertEqual(0, health["pages_with_baseline"])

    def test_partial_scan_never_updates_health_baseline(self) -> None:
        scan = complete_health_scan("scan_health_partial")
        scan["status"] = "partial"
        scan["coverage_complete"] = False
        self.assertEqual(0, http_receiver._promote_scan_health_baselines(scan))
        self.assertFalse(http_receiver._health_baselines_path().exists())

        unscoped = complete_health_scan("scan_health_unscoped")
        for result in unscoped["results"]:
            result.pop("store_key", None)
        self.assertEqual(0, http_receiver._promote_scan_health_baselines(unscoped))
        self.assertFalse(http_receiver._health_baselines_path().exists())

    def test_scan_status_rejects_duplicate_page_results(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate page id"):
            http_receiver.save_scan_status({
                "status": "partial",
                "results": [{"id": "orders", "ok": False}, {"id": "orders", "ok": True}],
            })

    def test_scan_status_rejects_malformed_result_types_and_scope(self) -> None:
        malformed_results = (
            {"id": "orders", "ok": "false"},
            {"id": "orders", "ok": True, "collection_complete": "false"},
            {"id": "orders", "ok": True, "captured_at": {}},
            {"id": "orders", "ok": True, "source": "qianchuan"},
            {"id": "orders", "ok": True, "quality": {"score": True}},
            {"id": "orders", "ok": True, "quality": {"score": 101}},
            {"id": "orders", "ok": True, "store_key": []},
        )
        for result in malformed_results:
            with self.subTest(result=result), self.assertRaisesRegex(ValueError, "invalid scan"):
                http_receiver.save_scan_status({"status": "partial", "results": [result]})
        for field_name, value in (
            ("coverage_complete", "false"),
            ("success", True),
            ("finished_at", 1.5),
        ):
            with self.subTest(field_name=field_name), self.assertRaisesRegex(ValueError, "invalid scan"):
                http_receiver.save_scan_status({
                    "status": "partial", field_name: value, "results": []
                })

    def test_legacy_scan_without_collection_proof_fails_closed(self) -> None:
        scan = complete_health_scan("scan_legacy_proof")
        for result in scan["results"]:
            result.pop("collection_complete")
        saved = http_receiver.save_scan_status(scan)
        self.assertTrue(all(item["collection_complete"] is False for item in saved["results"]))
        receipt = http_receiver.build_scan_receipt()
        self.assertFalse(receipt["analysis_ready"])
        self.assertEqual("attention", receipt["readiness"])
        self.assertEqual(0, receipt["summary"]["success"])
        self.assertEqual(set(scan["planned_page_ids"]), set(receipt["resume_page_ids"]))

    def test_corrupt_historical_scan_is_safe_for_read_endpoints(self) -> None:
        http_receiver._scan_status_path().write_text(json.dumps({
            "status": "completed",
            "scope": "full",
            "coverage_complete": "false",
            "results": [{"id": "orders", "ok": "false", "captured_at": {}}],
        }), encoding="utf-8")
        status = http_receiver.load_scan_status()
        self.assertEqual("SCAN_STATUS_CORRUPT", status["error_code"])
        self.assertFalse(http_receiver.build_scan_receipt()["analysis_ready"])
        self.assertEqual([], http_receiver.check_selector_health()["alerts"])

        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            for path in ("/scan-status", "/scan-receipt", "/health-monitor"):
                with self.subTest(path=path):
                    response = self._internal_http_get(f"{base_url}{path}")
                    self.assertEqual(200, response.status)
                    self.assertIsInstance(json.loads(response.read()), dict)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_integer_bounds_and_side_effect_boolean_contracts(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"

        def post(path, payload):
            request = urllib.request.Request(
                f"{base_url}{path}",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers=self._internal_http_headers(),
            )
            return json.loads(urllib.request.urlopen(request).read())

        try:
            for path in (
                "/actions/audit?limit=abc",
                "/actions/audit?limit=501",
                "/trends?days=abc",
                "/trends?days=-1",
                "/trends?days=91",
            ):
                with self.subTest(path=path):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        self._internal_http_get(f"{base_url}{path}")
                    self.assertEqual(400, raised.exception.code)
                    payload = json.loads(raised.exception.read())
                    self.assertEqual("INVALID_QUERY_PARAMETER", payload["error"])

            with (
                patch.object(http_receiver.Handler, "_paired_extension_origin", return_value=True),
                patch.object(http_receiver, "get_ai_proposals", return_value={"items": []}) as proposals,
            ):
                for path in (
                    "/ai/proposals?limit=abc",
                    "/ai/proposals?limit=0",
                    "/ai/proposals?limit=101",
                    "/ai/proposals?limit=1.5",
                ):
                    with self.subTest(path=path):
                        with self.assertRaises(urllib.error.HTTPError) as raised:
                            self._internal_http_get(f"{base_url}{path}")
                        self.assertEqual(400, raised.exception.code)
                        payload = json.loads(raised.exception.read())
                        self.assertEqual("INVALID_QUERY_PARAMETER", payload["error"])
                self.assertEqual(
                    {"items": []},
                    json.loads(self._internal_http_get(f"{base_url}/ai/proposals?limit=25").read()),
                )
                proposals.assert_called_once_with(25)

            with patch.object(http_receiver.OceanEngineDataClient, "sync") as sync:
                for path in ("/oauth/oceanengine/sync", "/oauth/oceanengine/account-center/sync"):
                    for days in (False, 0, 31, "7.5"):
                        with self.subTest(path=path, days=days):
                            with self.assertRaises(urllib.error.HTTPError) as raised:
                                post(path, {"days": days, "account_keys": []})
                            self.assertEqual(400, raised.exception.code)
                sync.assert_not_called()

            report = {"date": "2026-08-25", "path": "report.md", "content": "safe"}
            with (
                patch.object(http_receiver, "generate_daily_report", return_value=report) as generate,
                patch.object(http_receiver, "send_report_notifications") as notify,
            ):
                for value in ("false", 1, None):
                    with self.subTest(notify=value):
                        with self.assertRaises(urllib.error.HTTPError) as raised:
                            post("/reports/generate", {"notify": value})
                        self.assertEqual(400, raised.exception.code)
                generate.assert_not_called()
                notify.assert_not_called()

                result = post("/reports/generate", {"notify": False, "date": "2026-08-25"})
                self.assertEqual([], result["deliveries"])
                generate.assert_called_once_with("2026-08-25")
                notify.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_report_templates_support_builtin_and_custom_layouts(self) -> None:
        http_receiver.save_agent_settings({"report_template": "brief"})
        brief = http_receiver.generate_daily_report("2026-07-22")
        self.assertEqual(brief["template"], "brief")
        self.assertIn("老板简报", brief["content"])

        http_receiver.save_agent_settings(
            {
                "report_template": "custom",
                "custom_report_template": "# 店策 Agent 自定义日志 {{date}}\n{{headline}}\n{{scan_status}}",
            }
        )
        custom = http_receiver.generate_daily_report("2026-07-23")
        self.assertEqual(custom["template"], "custom")
        self.assertIn("自定义日志 2026-07-23", custom["content"])
        self.assertNotIn("{{headline}}", custom["content"])

    def test_notification_webhooks_are_local_masked_and_platform_specific(self) -> None:
        protected: dict[str, dict] = {}

        class MemorySecretStore:
            install_id = "a" * 32

            @staticmethod
            def label() -> str:
                return "mock_secure_store"

            def load(self, *, required: bool = False, legacy_revision: str = "") -> dict:
                value = json.loads(json.dumps(protected.get("record", {})))
                if required and not value:
                    raise http_receiver.IntegrationSecretStoreError("secure store unavailable")
                return value

            def store(self, record: dict) -> None:
                protected["record"] = json.loads(json.dumps(record))

        store = MemorySecretStore()
        with patch.object(http_receiver, "_integration_secret_store", return_value=store):
            public = http_receiver.save_integration_settings(
                {
                    "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook-id",
                    "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=test-token",
                    "auto_send_reports": True,
                }
            )
            self.assertTrue(public["feishu"]["configured"])
            self.assertTrue(public["dingtalk"]["configured"])
            public_json = json.dumps(public)
            self.assertNotIn("test-hook-id", public_json)
            self.assertNotIn("test-token", public_json)
            saved = json.loads((http_receiver.DATA_DIR / "integrations.json").read_text(encoding="utf-8"))
            saved_json = json.dumps(saved)
            self.assertEqual(http_receiver.INTEGRATION_METADATA_SCHEMA_VERSION, saved["schema_version"])
            self.assertNotIn("feishu_webhook", saved)
            self.assertNotIn("dingtalk_webhook", saved)
            self.assertNotIn("test-hook-id", saved_json)
            self.assertNotIn("test-token", saved_json)
            self.assertEqual(16, len(saved["platforms"]["feishu"]["fingerprint"]))

            sent: list[tuple[str, dict]] = []
            original_post = http_receiver._post_json
            try:
                def fake_post(url: str, payload: dict, timeout: float = 8.0, *, platform: str) -> dict:
                    sent.append((url, payload))
                    return {"code": 0} if "feishu" in url else {"errcode": 0}

                http_receiver._post_json = fake_post
                self.assertTrue(http_receiver.test_integration("feishu")["ok"])
                self.assertTrue(http_receiver.test_integration("dingtalk")["ok"])
            finally:
                http_receiver._post_json = original_post
        self.assertEqual(sent[0][1]["msg_type"], "text")
        self.assertEqual(sent[1][1]["msgtype"], "text")
        self.assertIn("店策 Agent", sent[0][1]["content"]["text"])

        with patch.object(http_receiver, "_integration_secret_store", return_value=store):
            with self.assertRaises(ValueError):
                http_receiver.save_integration_settings({"feishu_webhook": "https://example.com/hook/token"})

    def test_legacy_plaintext_webhooks_migrate_once_and_restart_from_secure_store(self) -> None:
        feishu = "https://open.feishu.cn/open-apis/bot/v2/hook/legacy-hook-id"
        dingtalk = "https://oapi.dingtalk.com/robot/send?access_token=legacy-token"
        http_receiver._integrations_path().write_text(json.dumps({
            "feishu_webhook": feishu,
            "dingtalk_webhook": dingtalk,
            "auto_send_reports": True,
        }), encoding="utf-8")
        protected: dict[str, dict] = {}

        class RestartableStore:
            install_id = "a" * 32

            @staticmethod
            def label() -> str:
                return "mock_secure_store"

            def load(self, *, required: bool = False, legacy_revision: str = "") -> dict:
                value = json.loads(json.dumps(protected.get("record", {})))
                if required and not value:
                    raise http_receiver.IntegrationSecretStoreError("unavailable")
                return value

            def store(self, record: dict) -> None:
                protected["record"] = json.loads(json.dumps(record))

        with patch.object(http_receiver, "_integration_secret_store", side_effect=RestartableStore):
            first = http_receiver._load_integration_secrets()
            restarted = http_receiver._load_integration_secrets()

        self.assertEqual(feishu, first["feishu_webhook"])
        self.assertEqual(first, restarted)
        metadata = http_receiver._integrations_path().read_text(encoding="utf-8")
        self.assertNotIn("legacy-hook-id", metadata)
        self.assertNotIn("legacy-token", metadata)
        self.assertNotIn("feishu_webhook", json.loads(metadata))
        self.assertEqual(feishu, protected["record"]["feishu_webhook"])

    def test_legacy_migration_fails_closed_without_destroying_the_only_copy(self) -> None:
        secret_url = "https://open.feishu.cn/open-apis/bot/v2/hook/do-not-echo-this"
        http_receiver._integrations_path().write_text(json.dumps({
            "feishu_webhook": secret_url,
            "dingtalk_webhook": "",
            "auto_send_reports": False,
        }), encoding="utf-8")

        class UnavailableStore:
            install_id = "a" * 32

            @staticmethod
            def label() -> str:
                return "mock_secure_store"

            def load(self, *, required: bool = False, legacy_revision: str = "") -> dict:
                return {}

            def store(self, _record: dict) -> None:
                raise http_receiver.IntegrationSecretStoreError(
                    "通知密钥存储不可用；为保护 Webhook，本次保存未生效。"
                )

        with patch.object(http_receiver, "_integration_secret_store", return_value=UnavailableStore()):
            with self.assertRaises(http_receiver.IntegrationSecretStoreError) as blocked:
                http_receiver._load_integration_secrets()

        self.assertNotIn("do-not-echo-this", str(blocked.exception))
        # Native storage never accepted the credential, so the legacy source is
        # retained for a later retry instead of silently losing the connection.
        self.assertIn("do-not-echo-this", http_receiver._integrations_path().read_text(encoding="utf-8"))

    def test_legacy_migration_rejects_non_boolean_auto_send_without_sending(self) -> None:
        secret_url = "https://open.feishu.cn/open-apis/bot/v2/hook/keep-local-only"

        class MemorySecretStore:
            install_id = "a" * 32
            stored = False

            @staticmethod
            def label() -> str:
                return "mock_secure_store"

            def load(self, *, required: bool = False, legacy_revision: str = "") -> dict:
                return {}

            def store(self, _record: dict) -> None:
                self.stored = True

        for invalid in ("false", "0", 1):
            with self.subTest(invalid=invalid):
                http_receiver._integrations_path().write_text(json.dumps({
                    "feishu_webhook": secret_url,
                    "dingtalk_webhook": "",
                    "auto_send_reports": invalid,
                }), encoding="utf-8")
                store = MemorySecretStore()
                with patch.object(http_receiver, "_integration_secret_store", return_value=store):
                    with self.assertRaisesRegex(ValueError, "迁移已停止"):
                        http_receiver._load_integration_secrets()
                self.assertFalse(store.stored)
                retained = http_receiver._integrations_path().read_text(encoding="utf-8")
                self.assertIn("keep-local-only", retained)
                self.assertEqual(invalid, json.loads(retained)["auto_send_reports"])

    def test_secure_record_repairs_interrupted_metadata_commit(self) -> None:
        protected: dict[str, dict] = {}

        class MemorySecretStore:
            install_id = "a" * 32

            @staticmethod
            def label() -> str:
                return "mock_secure_store"

            def load(self, *, required: bool = False, legacy_revision: str = "") -> dict:
                return json.loads(json.dumps(protected.get("record", {})))

            def store(self, record: dict) -> None:
                protected["record"] = json.loads(json.dumps(record))

        store = MemorySecretStore()
        with (
            patch.object(http_receiver, "_integration_secret_store", return_value=store),
            patch.object(http_receiver, "_atomic_json_write", side_effect=OSError("disk interrupted")),
        ):
            with self.assertRaises(OSError):
                http_receiver.save_integration_settings({
                    "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/recoverable",
                })

        self.assertTrue(protected.get("record"))
        with patch.object(http_receiver, "_integration_secret_store", return_value=store):
            recovered = http_receiver._load_integration_secrets()
        self.assertIn("recoverable", recovered["feishu_webhook"])
        metadata = json.loads(http_receiver._integrations_path().read_text(encoding="utf-8"))
        self.assertNotIn("recoverable", json.dumps(metadata))

    def test_clearing_webhook_replaces_protected_record_and_survives_restart(self) -> None:
        protected: dict[str, dict] = {}

        class MemorySecretStore:
            install_id = "a" * 32

            @staticmethod
            def label() -> str:
                return "mock_secure_store"

            def load(self, *, required: bool = False, legacy_revision: str = "") -> dict:
                value = json.loads(json.dumps(protected.get("record", {})))
                if required and not value:
                    raise http_receiver.IntegrationSecretStoreError("unavailable")
                return value

            def store(self, record: dict) -> None:
                protected["record"] = json.loads(json.dumps(record))

        store = MemorySecretStore()
        with patch.object(http_receiver, "_integration_secret_store", return_value=store):
            http_receiver.save_integration_settings({
                "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=clear-me",
                "auto_send_reports": True,
            })
            cleared = http_receiver.save_integration_settings({
                "dingtalk_webhook": "",
                "auto_send_reports": False,
            })
            restarted = http_receiver._load_integration_secrets()

        self.assertFalse(cleared["dingtalk"]["configured"])
        self.assertFalse(restarted["auto_send_reports"])
        self.assertEqual("", restarted["dingtalk_webhook"])
        self.assertNotIn("clear-me", json.dumps(protected))
        self.assertNotIn("clear-me", http_receiver._integrations_path().read_text(encoding="utf-8"))

    def test_integrations_http_get_never_returns_secret_and_store_errors_are_redacted(self) -> None:
        secret_marker = "http-response-must-not-contain-this"
        protected: dict[str, dict] = {}

        class MemorySecretStore:
            install_id = "a" * 32

            @staticmethod
            def label() -> str:
                return "mock_secure_store"

            def load(self, *, required: bool = False, legacy_revision: str = "") -> dict:
                return json.loads(json.dumps(protected.get("record", {})))

            def store(self, record: dict) -> None:
                protected["record"] = json.loads(json.dumps(record))

        class BrokenSecretStore:
            install_id = "a" * 32

            @staticmethod
            def label() -> str:
                return "mock_secure_store"

            def load(self, *, required: bool = False, legacy_revision: str = "") -> dict:
                raise http_receiver.IntegrationSecretStoreError(
                    f"native error https://example.invalid/{secret_marker}"
                )

        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch.object(http_receiver, "_integration_secret_store", return_value=MemorySecretStore()):
                http_receiver.save_integration_settings({
                    "feishu_webhook": (
                        "https://open.feishu.cn/open-apis/bot/v2/hook/" + secret_marker
                    ),
                })
                public = self._internal_http_get(f"{base_url}/integrations").read().decode("utf-8")
            self.assertNotIn(secret_marker, public)
            self.assertNotIn("feishu_webhook", public)
            self.assertFalse(json.loads(public)["secrets_exposed"])

            with patch.object(http_receiver, "_integration_secret_store", return_value=BrokenSecretStore()):
                with self.assertRaises(urllib.error.HTTPError) as blocked:
                    self._internal_http_get(f"{base_url}/integrations")
                self.assertEqual(503, blocked.exception.code)
                error_body = blocked.exception.read().decode("utf-8")
            self.assertNotIn(secret_marker, error_body)
            self.assertNotIn("example.invalid", error_body)
            self.assertEqual("INTEGRATION_SECRET_STORE_UNAVAILABLE", json.loads(error_body)["error"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_integration_metadata_symlink_is_rejected_without_touching_target(self) -> None:
        target = Path(self._temp.name) / "outside-integrations.json"
        target.write_text(json.dumps({
            "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/outside",
            "dingtalk_webhook": "",
            "auto_send_reports": False,
        }), encoding="utf-8")
        try:
            http_receiver._integrations_path().symlink_to(target)
        except OSError as error:  # pragma: no cover - host policy may forbid symlinks
            self.skipTest(f"symlink creation unavailable: {error}")

        with patch.object(http_receiver, "_integration_secret_store") as secret_store:
            with self.assertRaises(OSError):
                http_receiver._load_integration_secrets()
        secret_store.assert_not_called()
        self.assertIn("outside", target.read_text(encoding="utf-8"))

    def test_scheduler_log_does_not_render_suppressed_native_secret_error(self) -> None:
        secret_marker = "scheduler-native-secret"
        try:
            try:
                raise OSError(f"Keychain failed https://example.invalid/{secret_marker}")
            except OSError:
                raise http_receiver.IntegrationSecretStoreError(
                    "通知密钥存储不可用；为保护 Webhook，本次读取已停止。"
                ) from None
        except http_receiver.IntegrationSecretStoreError as safe_error:
            storage_error = safe_error

        class OneIterationStop:
            calls = 0

            def wait(self, _timeout: float) -> bool:
                self.calls += 1
                return self.calls > 1

        report = {
            "date": time.strftime("%Y-%m-%d"),
            "path": str(http_receiver.DATA_DIR / "reports" / "scheduler.md"),
            "content": "safe report",
        }
        with (
            patch.object(http_receiver, "load_agent_settings", return_value={
                **http_receiver.DEFAULT_AGENT_SETTINGS,
                "daily_report_enabled": True,
                "daily_report_time": "00:00",
            }),
            patch.object(http_receiver, "generate_daily_report", return_value=report),
            patch.object(http_receiver, "_load_integration_secrets", side_effect=storage_error),
            self.assertLogs(http_receiver.logger, level="ERROR") as captured,
        ):
            http_receiver._daily_report_scheduler(OneIterationStop())

        rendered = "\n".join(captured.output)
        self.assertNotIn(secret_marker, rendered)
        self.assertNotIn("example.invalid", rendered)
        self.assertIn("通知密钥存储不可用", rendered)

    def test_webhook_post_rejects_redirect_without_contacting_location_target(self) -> None:
        redirected_requests: list[str] = []

        class RedirectTarget(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                redirected_requests.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, _format: str, *args) -> None:
                return

        target = ThreadingHTTPServer(("127.0.0.1", 0), RedirectTarget)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()

        class RedirectSource(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/redirected")
                self.end_headers()

            def log_message(self, _format: str, *args) -> None:
                return

        source = ThreadingHTTPServer(("127.0.0.1", 0), RedirectSource)
        source_thread = threading.Thread(target=source.serve_forever, daemon=True)
        source_thread.start()
        try:
            url = f"http://127.0.0.1:{source.server_port}/robot/send?access_token=secret-token"
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                with patch.object(http_receiver, "_validate_webhook", side_effect=lambda _platform, value: value):
                    with self.assertRaisesRegex(ValueError, "HTTP 302") as blocked:
                        http_receiver._post_json(
                            url,
                            {"msgtype": "text", "text": {"content": "shop report"}},
                            platform="dingtalk",
                        )
                blocked_message = str(blocked.exception)
                del blocked
                gc.collect()
            self.assertNotIn("secret-token", blocked_message)
            self.assertEqual(
                [],
                [warning for warning in caught if issubclass(warning.category, ResourceWarning)],
            )
            self.assertEqual([], redirected_requests)
        finally:
            source.shutdown()
            source.server_close()
            source_thread.join(timeout=2)
            target.shutdown()
            target.server_close()
            target_thread.join(timeout=2)

    def test_webhook_response_is_bounded_and_requires_json_object(self) -> None:
        url = "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook-id"
        for raw, message in (
            (b"[]", "JSON object"),
            (b"x" * (http_receiver.MAX_WEBHOOK_RESPONSE_BYTES + 1), "too large"),
        ):
            with self.subTest(message=message):
                response = MagicMock()
                response.__enter__.return_value = response
                response.geturl.return_value = url
                response.read.return_value = raw
                opener = Mock()
                opener.open.return_value = response
                with patch.object(http_receiver, "build_opener", return_value=opener):
                    with self.assertRaisesRegex(ValueError, message):
                        http_receiver._post_json(url, {"msg_type": "text"}, platform="feishu")
                response.read.assert_called_once_with(http_receiver.MAX_WEBHOOK_RESPONSE_BYTES + 1)
                response.__exit__.assert_called_once()

    def test_webhook_delivery_requires_explicit_integral_success_code(self) -> None:
        secrets = {
            "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook-id",
            "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=test-token",
            "auto_send_reports": False,
        }
        with patch.object(http_receiver, "_load_integration_secrets", return_value=secrets):
            for platform, malformed in (
                ("feishu", {"code": 0.5}),
                ("feishu", {"code": False}),
                ("feishu", {}),
                ("dingtalk", {"errcode": 0.5}),
                ("dingtalk", {"errcode": False}),
                ("dingtalk", {}),
            ):
                with self.subTest(platform=platform, malformed=malformed):
                    with patch.object(http_receiver, "_post_json", return_value=malformed):
                        with self.assertRaisesRegex(ValueError, "状态码"):
                            http_receiver.send_notification(platform, "测试")
            for platform, accepted in (("feishu", {"code": "0"}), ("dingtalk", {"errcode": 0})):
                with self.subTest(platform=platform, accepted=accepted):
                    with patch.object(http_receiver, "_post_json", return_value=accepted):
                        self.assertTrue(http_receiver.send_notification(platform, "测试")["ok"])

    def test_scheduled_report_delivery_retries_failure_and_persists_success_receipt(self) -> None:
        report = self._scheduled_report_fixture(
            "2026-08-13", "店策 Agent 测试日报", now_seconds=100
        )
        with (
            patch.object(http_receiver, "_configured_report_destinations", return_value={"feishu": "destination-a"}),
            patch.object(http_receiver, "build_scheduled_report_guard", return_value=report["delivery_guard"]),
            patch.object(
                http_receiver,
                "send_notification",
                side_effect=[ValueError("temporary failure"), {"ok": True, "message": "sent"}],
            ) as sender,
        ):
            failed = http_receiver.deliver_scheduled_report(report, now=100)
            waiting = http_receiver.deliver_scheduled_report(report, now=120)
            succeeded = http_receiver.deliver_scheduled_report(report, now=160)

        self.assertEqual(failed["status"], "retry_pending")
        self.assertEqual(failed["platforms"]["feishu"]["next_retry_at"], 160)
        self.assertEqual(waiting["attempted"], [])
        self.assertEqual(succeeded["status"], "sent")
        self.assertEqual(succeeded["platforms"]["feishu"]["attempts"], 2)
        self.assertEqual(sender.call_count, 2)
        saved = json.loads(http_receiver._report_delivery_path().read_text(encoding="utf-8"))
        receipt = saved["reports"]["store-report1:2026-08-13:operating_report"]["platforms"]["feishu"]
        self.assertEqual(receipt["status"], "sent")
        self.assertEqual(receipt["destination_fingerprint"], "destination-a")

    def test_scheduled_report_delivery_does_not_repeat_successful_destination(self) -> None:
        report = self._scheduled_report_fixture(
            "2026-08-14", "店策 Agent 测试日报", now_seconds=200
        )
        with (
            patch.object(http_receiver, "_configured_report_destinations", return_value={"dingtalk": "destination-b"}),
            patch.object(http_receiver, "build_scheduled_report_guard", return_value=report["delivery_guard"]),
            patch.object(http_receiver, "send_notification", return_value={"ok": True, "message": "sent"}) as sender,
        ):
            first = http_receiver.deliver_scheduled_report(report, now=200)
            second = http_receiver.deliver_scheduled_report(report, now=250)

        self.assertEqual(first["status"], "sent")
        self.assertEqual(second["status"], "sent")
        self.assertEqual(second["attempted"], [])
        sender.assert_called_once()

    def test_scheduled_report_delivery_fails_closed_when_receipt_state_is_corrupt(self) -> None:
        http_receiver._report_delivery_path().write_text("{broken", encoding="utf-8")
        report = self._scheduled_report_fixture(
            "2026-08-15", "店策 Agent 测试日报", now_seconds=300
        )
        with (
            patch.object(http_receiver, "_configured_report_destinations", return_value={"feishu": "destination-c"}),
            patch.object(http_receiver, "build_scheduled_report_guard", return_value=report["delivery_guard"]),
            patch.object(http_receiver, "send_notification") as sender,
        ):
            with self.assertRaisesRegex(ValueError, "避免重复发送"):
                http_receiver.deliver_scheduled_report(report, now=300)
        sender.assert_not_called()

    def test_scheduled_report_guard_requires_all_fresh_high_quality_core_pages(self) -> None:
        catalog = [
            {
                "source": "doudian",
                "page_type": page_type,
                "fresh": True,
                "age_seconds": 10,
                "quality_score": 80,
                "rule_eligible": True,
                "timestamp_conflict": False,
                "captured_at": 1_000,
            }
            for page_type in http_receiver.SCAN_CORE_DOUDIAN_PAGE_IDS
        ]
        safe = http_receiver.build_scheduled_report_guard(
            catalog, {"store_key": "store-report1"}, now_ms=1_100_000
        )
        self.assertTrue(safe["safe_for_business_conclusions"])
        self.assertEqual("operating_report", safe["delivery_kind"])

        for field, value, reason in (
            ("fresh", False, "stale"),
            ("quality_score", 59, "low_quality"),
            ("timestamp_conflict", True, "timestamp_conflicts"),
        ):
            with self.subTest(field=field):
                unsafe_catalog = json.loads(json.dumps(catalog))
                unsafe_catalog[0][field] = value
                guard = http_receiver.build_scheduled_report_guard(
                    unsafe_catalog, {"store_key": "store-report1"}, now_ms=1_100_000
                )
                self.assertFalse(guard["safe_for_business_conclusions"])
                self.assertIn("overview", guard[reason])

        missing = http_receiver.build_scheduled_report_guard(
            catalog[:-1], {"store_key": "store-report1"}, now_ms=1_100_000
        )
        self.assertFalse(missing["safe_for_business_conclusions"])
        self.assertIn("shelf", missing["missing"])

    def test_scheduled_report_delivery_rejects_missing_tampered_and_expired_guards(self) -> None:
        valid = self._scheduled_report_fixture(
            "2026-08-16", "店策 Agent 测试日报", now_seconds=400
        )
        missing = dict(valid)
        missing.pop("delivery_guard")
        tampered = {**valid, "content": "被篡改的旧结论"}
        expired = json.loads(json.dumps(valid))
        expired["delivery_guard"]["valid_until_ms"] = 399_000
        with (
            patch.object(http_receiver, "_configured_report_destinations", return_value={"feishu": "destination-d"}),
            patch.object(http_receiver, "build_scheduled_report_guard", return_value=valid["delivery_guard"]),
            patch.object(http_receiver, "send_notification") as sender,
        ):
            with self.assertRaisesRegex(ValueError, "freshness guard"):
                http_receiver.deliver_scheduled_report(missing, now=400)
            with self.assertRaisesRegex(ValueError, "integrity"):
                http_receiver.deliver_scheduled_report(tampered, now=400)
            with self.assertRaisesRegex(ValueError, "expired"):
                http_receiver.deliver_scheduled_report(expired, now=400)
        sender.assert_not_called()

    def test_scheduled_operating_report_is_blocked_if_current_evidence_became_unsafe(self) -> None:
        report = self._scheduled_report_fixture(
            "2026-08-17", "店策 Agent 测试日报", now_seconds=500
        )
        current_guard = {**report["delivery_guard"], "safe_for_business_conclusions": False, "delivery_kind": "sync_reminder"}
        with (
            patch.object(http_receiver, "build_scheduled_report_guard", return_value=current_guard),
            patch.object(http_receiver, "_configured_report_destinations", return_value={"feishu": "destination-e"}),
            patch.object(http_receiver, "send_notification") as sender,
        ):
            with self.assertRaisesRegex(ValueError, "no longer has fresh business evidence"):
                http_receiver.deliver_scheduled_report(report, now=500)
        sender.assert_not_called()

    def test_daily_report_scheduler_revalidates_existing_file_and_sends_only_sync_reminder(self) -> None:
        http_receiver.save_agent_settings({"daily_report_time": "00:00", "daily_report_enabled": True})
        report_date = time.strftime("%Y-%m-%d")
        target = http_receiver._reports_dir() / f"{report_date}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("OLD_STALE_BUSINESS_CONCLUSION", encoding="utf-8")
        checked_at_ms = int(time.time() * 1000)
        unsafe_guard = {
            "schema_version": http_receiver.REPORT_GUARD_SCHEMA_VERSION,
            "store_scope": "unselected",
            "safe_for_business_conclusions": False,
            "delivery_kind": "sync_reminder",
            "checked_at_ms": checked_at_ms,
            "valid_until_ms": checked_at_ms + 300_000,
            "data_cutoff_at_ms": 0,
            "required_pages": list(http_receiver.SCAN_CORE_DOUDIAN_PAGE_IDS),
            "missing": ["selected_store"],
            "stale": [],
            "low_quality": [],
            "timestamp_conflicts": [],
        }
        stale_generated = {
            "date": report_date,
            "path": str(target),
            "store_scope": "unselected",
            "content_kind": "operating_report",
            "delivery_guard": unsafe_guard,
            "content": "OLD_STALE_BUSINESS_CONCLUSION",
        }

        class OneIterationStop:
            calls = 0

            def wait(self, _timeout: float) -> bool:
                self.calls += 1
                return self.calls > 1

        with (
            patch.object(http_receiver, "_load_integration_secrets", return_value={"auto_send_reports": True}),
            patch.object(http_receiver, "generate_daily_report", return_value=stale_generated) as generate,
            patch.object(http_receiver, "deliver_scheduled_report", return_value={"status": "sent", "attempted": [{"ok": True}]}) as deliver,
        ):
            http_receiver._daily_report_scheduler(OneIterationStop())

        generate.assert_called_once_with(report_date)
        deliver.assert_called_once()
        delivered = deliver.call_args.args[0]
        self.assertEqual(delivered["content_kind"], "sync_reminder")
        self.assertNotIn("OLD_STALE_BUSINESS_CONCLUSION", delivered["content"])
        self.assertNotIn("OLD_STALE_BUSINESS_CONCLUSION", target.read_text(encoding="utf-8"))

    def test_shelf_live_and_ops_manager_priorities(self) -> None:
        http_receiver.save_data("doudian", {"page_type": "shelf", "quality": {"score": 70}, "safe_metrics": {"曝光人数": "28", "点击人数": "4", "成交人数": "0", "订单量": "0", "用户支付金额": "¥0.00"}, "signals": ["商品主图存在不良暗示，请优化", "猜你喜欢未入选 1"]})
        http_receiver.save_data("doudian", {"page_type": "live", "quality": {"score": 70}, "safe_metrics": {"直播场次": "0", "成交金额": "¥0.00"}, "signals": ["当前待直播计划 0"]})
        shelf = http_receiver.build_shelf_analysis()
        live = http_receiver.build_live_analysis()
        ops = http_receiver.build_ops_manager()
        self.assertAlmostEqual(shelf["funnel"]["click_rate"], 14.2857, places=3)
        self.assertEqual(shelf["recommendations"][0]["level"], "high")
        self.assertIn("基准直播", live["recommendations"][0]["title"])
        self.assertIn("主图合规", ops["today_top_actions"][0]["title"])
        self.assertEqual(len({item["id"] for item in ops["all_tasks"]}), len(ops["all_tasks"]))
        report = http_receiver.generate_daily_report("2026-07-22")
        content = Path(report["path"]).read_text(encoding="utf-8")
        self.assertEqual(ops["roles"], ["货架商品", "直播投放", "内容"])
        self.assertIn("今日重点任务", content)
        self.assertIn("货架商品", content)
        self.assertIn("直播投放", content)
        self.assertIn("## 内容", content)

    def test_stale_shelf_snapshot_only_generates_resync_task(self) -> None:
        now_ms = int(time.time() * 1000)
        http_receiver.save_data("doudian", {
            "page_type": "shelf",
            "captured_at": now_ms - (http_receiver.SHELF_ANALYSIS_STALE_SECONDS + 1) * 1000,
            "quality": {"score": 90},
            "safe_metrics": {"曝光人数": "100", "点击人数": "20", "成交人数": "0"},
            "signals": ["商品主图存在不良暗示，请优化", "猜你喜欢未入选 1"],
        })

        stale = http_receiver.build_shelf_analysis()
        self.assertEqual("stale", stale["data_status"])
        self.assertEqual(["重新同步货架数据"], [item["title"] for item in stale["recommendations"]])
        self.assertFalse(any("合规" in item["title"] or "成交" in item["title"] for item in stale["recommendations"]))

        http_receiver.save_data("doudian", {
            "page_type": "shelf",
            "captured_at": now_ms,
            "quality": {"score": 90},
            "safe_metrics": {"曝光人数": "100", "点击人数": "20", "成交人数": "0"},
            "signals": ["商品主图存在不良暗示，请优化"],
        })
        fresh = http_receiver.build_shelf_analysis()
        self.assertEqual("ready", fresh["data_status"])
        self.assertTrue(any("主图合规" in item["title"] for item in fresh["recommendations"]))

    def test_future_live_snapshot_only_generates_resync_task(self) -> None:
        now_ms = int(time.time() * 1000)
        http_receiver.save_data("doudian", {
            "page_type": "live",
            "captured_at": now_ms + 60_000,
            "quality": {"score": 90},
            "safe_metrics": {"直播场次": "0", "整体消耗(元)": "500", "整体成交订单数": "0"},
            "signals": ["当前待直播计划 0"],
        })

        analysis = http_receiver.build_live_analysis()
        self.assertEqual("stale", analysis["data_status"])
        self.assertEqual(["重新同步直播大屏"], [item["title"] for item in analysis["recommendations"]])
        self.assertFalse(any("基准直播" in item["title"] or "止损" in item["title"] for item in analysis["recommendations"]))

    def test_stale_inventory_is_ignored_and_ops_requests_resync(self) -> None:
        now_ms = int(time.time() * 1000)
        http_receiver.save_data("doudian", {
            "page_type": "inventory",
            "captured_at": now_ms - (http_receiver.INVENTORY_ANALYSIS_STALE_SECONDS + 1) * 1000,
            "quality": {"score": 90},
            "tables": [{"headers": ["商品名称", "可售库存"], "rows": [["过期缺货商品", "0"]]}],
        })

        self.assertEqual([], http_receiver.build_inventory_alerts())
        stale_ops = http_receiver.build_ops_manager()
        self.assertEqual("stale", stale_ops["modules"]["inventory"]["status"])
        self.assertTrue(any(item["title"] == "重新同步商品库存" for item in stale_ops["all_tasks"]))
        self.assertFalse(any("过期缺货商品" in item["title"] for item in stale_ops["all_tasks"]))

        http_receiver.save_data("doudian", {
            "page_type": "inventory",
            "captured_at": now_ms,
            "quality": {"score": 90},
            "tables": [{"headers": ["商品名称", "可售库存"], "rows": [["当前缺货商品", "0"]]}],
        })
        alerts = http_receiver.build_inventory_alerts()
        self.assertEqual("当前缺货商品", alerts[0]["product"])
        self.assertEqual("已缺货", alerts[0]["title"])

    def test_ops_task_priority_does_not_inflate_evidence_confidence(self) -> None:
        urgent = {
            "level": "high",
            "owner": "货架运营",
            "title": "高优先级但证据置信度未声明",
            "action": "人工核对。",
            "acceptance": "完成核对。",
            "evidence": "只有一条待复核信号。",
        }
        with patch.object(http_receiver, "build_shelf_analysis", return_value={"recommendations": [urgent], "data_status": "ready"}), \
             patch.object(http_receiver, "build_live_analysis", return_value={"recommendations": [], "data_status": "ready"}), \
             patch.object(http_receiver, "build_plan_recommendations", return_value=[]), \
             patch.object(http_receiver, "build_inventory_alerts", return_value=[]), \
             patch.object(http_receiver, "_inventory_task_state", return_value={"status": "ready"}), \
             patch.object(http_receiver, "build_qianchuan_creative_analysis", return_value={"recommendations": [], "data_status": "ready"}):
            manager = http_receiver.build_ops_manager()

        task = next(item for item in manager["all_tasks"] if item["title"] == urgent["title"])
        self.assertEqual("high", task["level"])
        self.assertEqual("medium", task["confidence"])

    def test_ops_sync_task_exposes_v2_contract_and_stays_startable_when_evidence_is_stale(self) -> None:
        now_ms = int(time.time() * 1000)
        http_receiver._remember_store_identity({"key": "store-contract", "confidence": "high"})
        http_receiver.save_agent_settings({"store_key": "store-contract"})
        recommendation = {
            "level": "warning", "owner": "货架运营", "title": "重新同步货架数据",
            "action": "打开商城经营概览并重新同步。", "acceptance": "出现 30 分钟内采集的货架数据。",
            "evidence": "现有货架快照已过期。", "confidence": "high",
        }
        with patch.object(http_receiver, "build_shelf_analysis", return_value={"recommendations": [recommendation], "data_status": "stale"}), \
             patch.object(http_receiver, "build_live_analysis", return_value={"recommendations": [], "data_status": "ready"}), \
             patch.object(http_receiver, "build_plan_recommendations", return_value=[]), \
             patch.object(http_receiver, "build_inventory_alerts", return_value=[]), \
             patch.object(http_receiver, "_inventory_task_state", return_value={"status": "ready"}), \
             patch.object(http_receiver, "build_qianchuan_creative_analysis", return_value={"recommendations": [], "data_status": "ready"}), \
             patch.object(http_receiver, "build_scan_receipt", return_value={"results": []}), \
             patch.object(http_receiver, "build_execution_effectiveness_report", return_value={"items": []}), \
             patch.object(http_receiver, "_task_contract_source_index", return_value=[{
                 "source": "doudian", "page_type": "shelf",
                 "captured_at_ms": now_ms - (http_receiver.SHELF_ANALYSIS_STALE_SECONDS + 1) * 1000,
                 "quality_score": 90,
             }]):
            manager = http_receiver.build_ops_manager()
        task = next(item for item in manager["all_tasks"] if item["title"] == "重新同步货架数据")
        contract = task["task_contract"]
        self.assertEqual(2, contract["contract_version"])
        self.assertEqual("system.sync.shelf", contract["rule_id"])
        self.assertEqual("shelf-actions", contract["navigation"]["target_id"])
        self.assertTrue(contract["eligibility"]["sync_task"])
        self.assertTrue(contract["eligibility"]["can_start"])
        self.assertEqual("stale", contract["source_refs"][0]["freshness"])
        self.assertEqual(task["contract_fingerprint"], contract["contract_fingerprint"])

    def test_task_contract_rejects_stale_start_and_changed_fingerprint(self) -> None:
        http_receiver._remember_store_identity({"key": "store-contract-gate", "confidence": "high"})
        http_receiver.save_agent_settings({"store_key": "store-contract-gate"})
        recommendation = {
            "level": "warning", "owner": "货架运营", "title": "核对商品承接",
            "action": "检查商品详情。", "acceptance": "转化率改善。", "evidence": "证据等待重新同步。",
        }
        patches = (
            patch.object(http_receiver, "build_shelf_analysis", return_value={"recommendations": [recommendation], "data_status": "ready"}),
            patch.object(http_receiver, "build_live_analysis", return_value={"recommendations": [], "data_status": "ready"}),
            patch.object(http_receiver, "build_plan_recommendations", return_value=[]),
            patch.object(http_receiver, "build_inventory_alerts", return_value=[]),
            patch.object(http_receiver, "_inventory_task_state", return_value={"status": "ready"}),
            patch.object(http_receiver, "build_qianchuan_creative_analysis", return_value={"recommendations": [], "data_status": "ready"}),
            patch.object(http_receiver, "build_scan_receipt", return_value={"results": []}),
            patch.object(http_receiver, "build_execution_effectiveness_report", return_value={"items": []}),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            task = http_receiver.build_ops_manager()["all_tasks"][0]
            self.assertFalse(task["task_contract"]["eligibility"]["can_start"])
            with self.assertRaisesRegex(ValueError, "重新同步"):
                http_receiver.update_task_state(
                    task["id"], "doing", contract_fingerprint=task["contract_fingerprint"],
                )
            with self.assertRaisesRegex(ValueError, "任务数据已经更新"):
                http_receiver.update_task_state(task["id"], "doing", contract_fingerprint="0" * 32)

    def test_legacy_plan_alias_cannot_bypass_canonical_start_blocker(self) -> None:
        store_key = "store-plan-alias-gate"
        canonical_id = "1" * 16
        legacy_id = "2" * 16
        fingerprint = "3" * 32
        http_receiver._remember_store_identity({"key": store_key, "confidence": "high"})
        http_receiver.save_agent_settings({"store_key": store_key})
        canonical_task = {
            "id": canonical_id,
            "legacy_task_ids": [legacy_id],
            "title": "降低高风险计划预算",
            "owner": "投放运营",
            "action": "建议降低预算 20%",
            "acceptance": "ROI 恢复",
            "evidence": "当前证据已过期",
            "level": "high",
            "task_contract": {
                "contract_version": 2,
                "task_key": canonical_id + "4" * 8,
                "contract_fingerprint": fingerprint,
                "scope": {"store_key": store_key, "account_key": "acct-plan-alias-gate"},
                "eligibility": {
                    "can_start": False,
                    "blockers": [{"code": "EVIDENCE_STALE", "message": "该任务证据已经过期，请重新同步后再开始。"}],
                },
            },
        }
        with patch.object(http_receiver, "_canonical_ops_task", return_value=canonical_task):
            with self.assertRaisesRegex(ValueError, "证据已经过期"):
                http_receiver.update_task_state(
                    legacy_id,
                    "doing",
                    contract_fingerprint=fingerprint,
                    store_key=store_key,
                )
        self.assertFalse(http_receiver._task_states_path().exists())

    def test_ambiguous_legacy_plan_alias_is_rejected_without_fingerprint(self) -> None:
        store_key = "store-plan-alias-ambiguous"
        ambiguous_alias = "4" * 16
        http_receiver._remember_store_identity({"key": store_key, "confidence": "high"})
        http_receiver.save_agent_settings({"store_key": store_key})
        recommendations = [
            {
                "level": "warning",
                "workbench_title": f"冲突计划 {index}",
                "action": "先复核",
                "acceptance": "确认归属",
                "found": "旧编号冲突",
                "adjustment_range": "不调整",
                "observation_window": "立即",
                "task_id": f"{index + 5:016x}",
                "task_status": "todo",
                "legacy_task_ids": [],
                "ambiguous_legacy_task_ids": [ambiguous_alias],
                "task_contract": {
                    "contract_version": 2,
                    "task_key": f"{index + 5:016x}" + "a" * 8,
                    "contract_fingerprint": f"{index + 20:032x}",
                    "scope": {"store_key": store_key, "account_key": "acct-ambiguous"},
                    "eligibility": {"can_start": True, "blockers": []},
                },
            }
            for index in range(2)
        ]
        with patch.object(http_receiver, "build_ops_manager", return_value={"all_tasks": []}), \
             patch.object(http_receiver, "build_plan_recommendations", return_value=recommendations):
            with self.assertRaisesRegex(ValueError, "对应多个千川计划"):
                http_receiver.update_task_state(
                    ambiguous_alias,
                    "doing",
                    store_key=store_key,
                )
        self.assertFalse(http_receiver._task_states_path().exists())

    def test_plan_outside_ops_shortlist_still_resolves_canonical_contract(self) -> None:
        recommendations = []
        for index in range(6):
            canonical_id = f"{index + 1:016x}"
            legacy_id = f"{index + 101:016x}"
            recommendations.append({
                "level": "warning",
                "owner": "投放运营",
                "workbench_title": f"计划 {index + 1} · ROI 待改善",
                "action": "保持预算并复核素材",
                "acceptance": "ROI 恢复",
                "found": "ROI 低于目标",
                "adjustment_range": "预算保持不变",
                "observation_window": "观察 2 小时",
                "action_type": "optimize",
                "task_id": canonical_id,
                "task_status": "todo",
                "legacy_task_ids": [legacy_id],
                "task_contract": {
                    "contract_version": 2,
                    "task_key": canonical_id + "a" * 8,
                    "contract_fingerprint": f"{index + 11:032x}",
                    "scope": {"store_key": "store-shortlist", "account_key": "acct-shortlist"},
                    "eligibility": {"can_start": True, "blockers": []},
                },
            })
        shortlist = [
            {"id": item["task_id"], "legacy_task_ids": item["legacy_task_ids"], "task_contract": item["task_contract"]}
            for item in recommendations[:5]
        ]
        target = recommendations[5]
        with patch.object(http_receiver, "build_ops_manager", return_value={"all_tasks": shortlist}), \
             patch.object(http_receiver, "build_plan_recommendations", return_value=recommendations):
            by_id = http_receiver._canonical_ops_task(target["task_id"])
            by_alias = http_receiver._canonical_ops_task(target["legacy_task_ids"][0])
        self.assertEqual(target["task_id"], by_id["id"])
        self.assertEqual(target["task_id"], by_alias["id"])
        self.assertEqual(target["task_contract"]["contract_fingerprint"], by_alias["contract_fingerprint"])

    def test_task_update_uses_server_authored_content_and_scopes_effect_metrics(self) -> None:
        now_ms = int(time.time() * 1000)
        http_receiver._remember_store_identity({"key": "store-contract-source", "confidence": "high"})
        http_receiver.save_agent_settings({"store_key": "store-contract-source"})
        recommendation = {
            "level": "high", "owner": "货架运营", "title": "检查主图合规",
            "action": "替换风险主图。", "acceptance": "点击率改善。", "evidence": "主图存在风险信号。",
            "confidence": "high",
        }
        with patch.object(http_receiver, "build_shelf_analysis", return_value={"recommendations": [recommendation], "data_status": "ready"}), \
             patch.object(http_receiver, "build_live_analysis", return_value={"recommendations": [], "data_status": "ready"}), \
             patch.object(http_receiver, "build_plan_recommendations", return_value=[]), \
             patch.object(http_receiver, "build_inventory_alerts", return_value=[]), \
             patch.object(http_receiver, "_inventory_task_state", return_value={"status": "ready"}), \
             patch.object(http_receiver, "build_qianchuan_creative_analysis", return_value={"recommendations": [], "data_status": "ready"}), \
             patch.object(http_receiver, "build_scan_receipt", return_value={"results": []}), \
             patch.object(http_receiver, "build_execution_effectiveness_report", return_value={"items": []}), \
             patch.object(http_receiver, "_task_contract_source_index", return_value=[{
                 "source": "doudian", "page_type": "shelf", "captured_at_ms": now_ms, "quality_score": 90,
             }]), \
             patch.object(
                 http_receiver,
                 "_collect_suggestion_metrics",
                 return_value=(
                     {"doudian/shelf/点击率": "10", "qianchuan/campaigns/ROI": "1.0"},
                     {"doudian/shelf": 1000, "qianchuan/campaigns": 1000},
                 ),
             ):
            task = next(item for item in http_receiver.build_ops_manager()["all_tasks"] if "主图合规" in item["title"])
            started = http_receiver.update_task_state(
                task["id"], "doing", title="客户端篡改标题",
                evidence="客户端篡改证据", contract_fingerprint=task["contract_fingerprint"],
            )
        self.assertEqual(task["title"], started["title"])
        self.assertEqual(task["evidence"], started["evidence"])
        snapshot = http_receiver._find_suggestion_snapshot(
            http_receiver.load_suggestion_snapshots(), task["id"], "store-contract-source",
        )[1]
        self.assertEqual({"doudian/shelf/点击率": "10"}, snapshot["metrics_snapshot"])
        self.assertEqual({"doudian/shelf": 1000}, snapshot["source_watermarks"])

    def test_live_pacing_uses_current_session_rates_without_enabling_writes(self) -> None:
        now_ms = 1_800_000_000_000
        points = [
            {
                "captured_at": now_ms - 10 * 60_000,
                "source": "qianchuan",
                "page_type": "live_dashboard",
                "safe_metrics": {"整体消耗(元)": 100, "整体成交金额(元)": 150, "整体成交订单数": 1, "进入直播间人数": 100, "直播间商品点击人数": 10},
            },
            {
                "captured_at": now_ms - 5 * 60_000,
                "source": "qianchuan",
                "page_type": "live_dashboard",
                "safe_metrics": {"整体消耗(元)": 150, "整体成交金额(元)": 210, "整体成交订单数": 2, "进入直播间人数": 150, "直播间商品点击人数": 15},
            },
            {
                "captured_at": now_ms,
                "source": "qianchuan",
                "page_type": "live_dashboard",
                "safe_metrics": {"整体消耗(元)": 230, "整体成交金额(元)": 330, "整体成交订单数": 3, "进入直播间人数": 200, "直播间商品点击人数": 20},
            },
        ]
        result = http_receiver.build_live_pacing_analysis(
            history_points=points,
            settings={"roi_target": 1.4, "min_spend_for_action": 50},
            now_ms=now_ms,
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["pace_state"], "surging")
        self.assertEqual(result["decision"]["code"], "maintain_pace")
        self.assertEqual(result["latest_interval"]["spend_delta"], 80)
        self.assertEqual(result["latest_interval"]["interval_roi"], 1.5)
        self.assertFalse(result["platform_write_enabled"])
        self.assertFalse(result["automatic_status_change_enabled"])

    def test_live_pacing_splits_reset_session_and_flags_zero_order_spend(self) -> None:
        now_ms = 1_800_000_000_000
        points = [
            {
                "captured_at": now_ms - 15 * 60_000,
                "source": "qianchuan",
                "page_type": "live_dashboard",
                "safe_metrics": {"整体消耗(元)": 500, "整体成交金额(元)": 900, "整体成交订单数": 8},
            },
            {
                "captured_at": now_ms - 5 * 60_000,
                "source": "qianchuan",
                "page_type": "live_dashboard",
                "safe_metrics": {"整体消耗(元)": 20, "整体成交金额(元)": 0, "整体成交订单数": 0},
            },
            {
                "captured_at": now_ms,
                "source": "qianchuan",
                "page_type": "live_dashboard",
                "safe_metrics": {"整体消耗(元)": 140, "整体成交金额(元)": 0, "整体成交订单数": 0},
            },
        ]
        result = http_receiver.build_live_pacing_analysis(
            history_points=points,
            settings={"roi_target": 1.5, "min_spend_for_action": 100},
            now_ms=now_ms,
        )
        self.assertTrue(result["session_reset_detected"])
        self.assertEqual(result["session_point_count"], 2)
        self.assertEqual(result["latest_interval"]["spend_delta"], 120)
        self.assertEqual(result["decision"]["code"], "hold_spend_review")
        self.assertEqual(result["decision"]["level"], "high")

    def test_operation_task_status_is_persisted(self) -> None:
        http_receiver._remember_store_identity({"key": "store_test", "confidence": "high"})
        http_receiver.save_agent_settings({"store_key": "store_test"})
        http_receiver.save_data("doudian", {"page_type": "shelf", "quality": {"score": 70}, "safe_metrics": {"曝光人数": "20", "点击人数": "2", "成交人数": "0"}, "signals": ["商品主图存在不良暗示，请优化"]})
        before = http_receiver.build_ops_manager()
        task = before["must_do"][0]
        updated = http_receiver.update_task_state(task["id"], "doing")
        after = http_receiver.build_ops_manager()
        self.assertEqual(updated["status"], "doing")
        self.assertEqual(next(item for item in after["all_tasks"] if item["id"] == task["id"])["status"], "doing")
        http_receiver.update_task_state(task["id"], "observing")
        http_receiver.update_task_state(task["id"], "done", note="已替换主图并同步复核")
        completed = http_receiver.build_ops_manager()
        self.assertEqual(completed["progress"]["done"], 1)
        self.assertFalse(any(item["id"] == task["id"] for item in completed["today_top_actions"]))

    def test_task_close_requires_observation_and_note_and_start_tracks_once(self) -> None:
        task_id = "c" * 16
        http_receiver._remember_store_identity({"key": "store-loop", "confidence": "high"})
        http_receiver.save_agent_settings({"store_key": "store-loop"})
        with patch.object(
            http_receiver,
            "_collect_suggestion_metrics",
            return_value=({"doudian/shelf/成交订单": "1"}, {"doudian/shelf": 1000}),
        ):
            started = http_receiver.update_task_state(
                task_id,
                "doing",
                title="优化商品主图",
                owner="货架运营",
                action="替换主图",
                acceptance="成交订单改善",
                evidence="当前成交订单 1",
            )
            retried = http_receiver.save_suggestion_snapshot(
                task_id,
                {"title": "不应覆盖原始基线"},
                store_key="store-loop",
            )
        self.assertEqual("captured", started["baseline_tracking"]["status"])
        self.assertTrue(retried["baseline_reused"])
        self.assertEqual("优化商品主图", retried["context"]["title"])

        with self.assertRaisesRegex(ValueError, "先进入效果观察"):
            http_receiver.update_task_state(task_id, "done", note="尝试直接结案")
        http_receiver.update_task_state(task_id, "observing")
        with self.assertRaisesRegex(ValueError, "必须填写结果说明"):
            http_receiver.update_task_state(task_id, "done")
        closed = http_receiver.update_task_state(task_id, "done", note="已完成操作，等待后续数据")
        self.assertEqual("done", closed["status"])
        report = http_receiver.get_effectiveness_report()
        self.assertEqual(1, report["awaiting_readback_count"])
        self.assertEqual(0, report["manual_verified_count"])
        self.assertEqual(0, report["comparable_count"])

    def test_active_task_carries_to_next_business_day_for_same_store_only(self) -> None:
        task_id = "d" * 16
        today = time.strftime("%Y-%m-%d")
        yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        http_receiver._remember_store_identity({"key": "store-carry", "confidence": "high"})
        http_receiver._remember_store_identity({"key": "store-other", "confidence": "high"})
        http_receiver.save_agent_settings({"store_key": "store-carry"})
        http_receiver.update_task_state(
            task_id,
            "doing",
            title="跨日优化直播间",
            owner="直播运营",
            store_key="store-carry",
            business_date=yesterday,
        )
        carried = http_receiver.load_task_states("store-carry", today)
        self.assertEqual("doing", carried[task_id]["status"])
        self.assertEqual(f"store-carry:{yesterday}", carried[task_id]["carried_from_scope"])
        self.assertEqual({}, http_receiver.load_task_states("store-other", today))

        observed = http_receiver.update_task_state(
            task_id,
            "observing",
            store_key="store-carry",
            business_date=today,
        )
        self.assertEqual("doing", observed["history"][-1]["from"])
        self.assertEqual("observing", observed["status"])
        self.assertNotIn("carried_from_scope", observed)
        with patch.object(http_receiver, "build_shelf_analysis", return_value={"recommendations": [], "data_status": "ready"}), \
             patch.object(http_receiver, "build_live_analysis", return_value={"recommendations": [], "data_status": "ready"}), \
             patch.object(http_receiver, "build_plan_recommendations", return_value=[]), \
             patch.object(http_receiver, "build_inventory_alerts", return_value=[]), \
             patch.object(http_receiver, "build_qianchuan_creative_analysis", return_value={"recommendations": [], "data_status": "ready"}), \
             patch.object(http_receiver, "build_scan_receipt", return_value={"results": []}), \
             patch.object(http_receiver, "build_execution_effectiveness_report", return_value={"items": []}):
            manager = http_receiver.build_ops_manager()
        persisted = next(item for item in manager["all_tasks"] if item["id"] == task_id)
        self.assertEqual("observing", persisted["status"])
        self.assertTrue(persisted["carried_over"])

    def test_newer_terminal_state_prevents_older_active_task_resurrection(self) -> None:
        task_id = "e" * 16
        store_key = "store-terminal-carry"
        http_receiver._atomic_json_write(http_receiver._task_states_path(), {
            "schema_version": 2,
            "scopes": {
                f"{store_key}:2026-08-28": {
                    task_id: {"status": "observing", "updated_at": "2026-08-28 23:00:00"},
                },
                f"{store_key}:2026-08-29": {
                    task_id: {"status": "done", "updated_at": "2026-08-29 10:00:00"},
                },
            },
        })
        self.assertNotIn(
            task_id,
            http_receiver.load_task_states(store_key, "2026-08-30"),
        )

    def test_effectiveness_statuses_and_cross_day_denominator(self) -> None:
        http_receiver._remember_store_identity({"key": "store-effect", "confidence": "high"})
        http_receiver.save_agent_settings({"store_key": "store-effect"})

        cases = [
            ("1" * 16, {"qianchuan/plans/ROI": "1"}, {"qianchuan/plans/ROI": "1.2"}, 2000, "effective"),
            ("2" * 16, {"qianchuan/plans/ROI": "2"}, {"qianchuan/plans/ROI": "1"}, 2000, "ineffective"),
            ("3" * 16, {"doudian/shelf/曝光人数": "10"}, {"doudian/shelf/曝光人数": "20"}, 2000, "inconclusive"),
            ("4" * 16, {"qianchuan/plans/ROI": "1"}, {"qianchuan/plans/ROI": "1"}, 1000, "awaiting_readback"),
        ]
        for task_id, before, after, after_watermark, expected in cases:
            source_key = "/".join(next(iter(before)).split("/")[:2])
            with patch.object(
                http_receiver,
                "_collect_suggestion_metrics",
                return_value=(before, {source_key: 1000}),
            ):
                http_receiver.save_suggestion_snapshot(
                    task_id,
                    {"title": f"效果样本 {expected}"},
                    store_key="store-effect",
                    business_date="2026-08-17",
                )
            with patch.object(
                http_receiver,
                "_collect_suggestion_metrics",
                return_value=(after, {source_key: after_watermark}),
            ):
                evaluated = http_receiver._evaluate_on_completion(
                    task_id,
                    completion_note="人工结案说明",
                    store_key="store-effect",
                    business_date="2026-08-17",
                )
            self.assertEqual(expected, evaluated["evaluation"]["status"])

        with patch.object(
            http_receiver,
            "_collect_suggestion_metrics",
            return_value=({"doudian/shelf/成交订单": "1"}, {"doudian/shelf": 1000}),
        ):
            http_receiver.save_suggestion_snapshot(
                "5" * 16,
                {"title": "仍在处理中"},
                store_key="store-effect",
            )
        report = http_receiver.get_effectiveness_report()
        self.assertEqual(5, report["total_tracked"])
        self.assertEqual(1, report["pending_count"])
        self.assertEqual(0, report["manual_verified_count"])
        self.assertEqual(1, report["awaiting_readback_count"])
        self.assertEqual(1, report["inconclusive_count"])
        self.assertEqual(1, report["effective_count"])
        self.assertEqual(1, report["ineffective_count"])
        self.assertEqual(2, report["comparable_count"])
        self.assertEqual(50.0, report["effective_rate"])

    def test_legacy_effectiveness_entry_remains_readable(self) -> None:
        http_receiver._remember_store_identity({"key": "store-legacy", "confidence": "high"})
        http_receiver.save_agent_settings({"store_key": "store-legacy"})
        http_receiver._atomic_json_write(
            http_receiver._suggestion_snapshots_path(),
            {
                "legacy-task": {
                    "task_id": "e" * 16,
                    "created_at": "2026-08-01 10:00:00",
                    "metrics_snapshot": {},
                    "evaluated": True,
                    "evaluation": {
                        "effective": True,
                        "changes": [],
                        "evaluated_at": "2026-08-01 12:00:00",
                    },
                }
            },
        )
        report = http_receiver.get_effectiveness_report()
        self.assertEqual(1, report["effective_count"])
        self.assertEqual(1, report["comparable_count"])
        self.assertEqual("effective", report["recent_evaluations"][0]["status"])

    def test_manual_close_upgrades_after_a_new_comparable_snapshot(self) -> None:
        task_id = "f" * 16
        http_receiver._remember_store_identity({"key": "store-late-effect", "confidence": "high"})
        http_receiver.save_agent_settings({"store_key": "store-late-effect"})
        with patch.object(
            http_receiver,
            "_collect_suggestion_metrics",
            return_value=({"qianchuan/plans/ROI": "1"}, {"qianchuan/plans": 1_000}),
        ):
            http_receiver.save_suggestion_snapshot(task_id, {"title": "迟到回读"}, store_key="store-late-effect")
            provisional = http_receiver._evaluate_on_completion(
                task_id,
                completion_note="已完成，等待回读",
                store_key="store-late-effect",
            )
        self.assertEqual("awaiting_readback", provisional["evaluation"]["status"])

        with patch.object(
            http_receiver,
            "_collect_suggestion_metrics",
            return_value=({"qianchuan/plans/ROI": "1.2"}, {"qianchuan/plans": 2_000}),
        ):
            report = http_receiver.get_effectiveness_report()
        self.assertEqual(0, report["awaiting_readback_count"])
        self.assertEqual(1, report["effective_count"])
        self.assertEqual(100.0, report["effective_rate"])

    def test_effect_waits_for_due_time_and_the_same_metric_readback(self) -> None:
        task_id = "9" * 16
        http_receiver._remember_store_identity({"key": "store-observation-gate", "confidence": "high"})
        http_receiver.save_agent_settings({"store_key": "store-observation-gate"})
        with patch.object(http_receiver.time, "time", return_value=1_000), patch.object(
            http_receiver,
            "_collect_suggestion_metrics",
            return_value=({"qianchuan/plans/ROI": "1"}, {"qianchuan/plans": 1_000_000}),
        ):
            baseline = http_receiver.save_suggestion_snapshot(
                task_id,
                {"title": "观察门禁", "observation_window": "调整后观察 2 小时"},
                store_key="store-observation-gate",
            )
        self.assertEqual(120, baseline["observation_window_minutes"])
        self.assertEqual(8_200_000, baseline["due_at_ms"])

        with patch.object(http_receiver.time, "time", return_value=2_000), patch.object(
            http_receiver,
            "_collect_suggestion_metrics",
            return_value=({"qianchuan/plans/ROI": "1.3"}, {"qianchuan/plans": 2_000_000}),
        ):
            early = http_receiver._evaluate_on_completion(task_id, store_key="store-observation-gate")
        self.assertEqual("observing", early["evaluation"]["status"])
        self.assertFalse(early["evaluated"])

        with patch.object(http_receiver.time, "time", return_value=8_201), patch.object(
            http_receiver,
            "_collect_suggestion_metrics",
            return_value=({"qianchuan/plans/消耗": "20"}, {"qianchuan/plans": 8_201_000}),
        ):
            missing = http_receiver._evaluate_on_completion(task_id, store_key="store-observation-gate")
        self.assertEqual("awaiting_readback", missing["evaluation"]["status"])
        self.assertEqual(1, missing["evaluation"]["missing_metric_count"])

        with patch.object(http_receiver.time, "time", return_value=8_202), patch.object(
            http_receiver,
            "_collect_suggestion_metrics",
            return_value=({"qianchuan/plans/ROI": "1.3"}, {"qianchuan/plans": 8_202_000}),
        ):
            final = http_receiver._evaluate_on_completion(task_id, store_key="store-observation-gate")
        self.assertEqual("effective", final["evaluation"]["status"])
        self.assertTrue(final["evaluated"])

    def test_exact_product_task_is_mirrored_to_sqlite_run_ledger(self) -> None:
        captured_at_ms = int(time.time() * 1000)
        http_receiver._remember_store_identity({"key": "store-product-run", "confidence": "high"})
        http_receiver.save_agent_settings({"store_key": "store-product-run"})
        http_receiver.save_data("doudian", {
            "schema_version": 2,
            "page_type": "products",
            "captured_at": captured_at_ms,
            "store": {"key": "store-product-run", "label": "单品任务店", "confidence": "high"},
            "quality": {"score": 90, "row_count": 1},
            "tables": [{
                "headers": ["productid", "productname", "stock", "ctr"],
                "rows": [["product-run-001", "单品任务商品", "20", "2.0"]],
            }],
        })
        entity_key = http_receiver._local_identity_key("douyin_product_id", "product-run-001")
        contract = {
            "contract_version": 2,
            "contract_fingerprint": "f" * 32,
            "task_key": "a" * 24,
            "rule_id": "ops.product_graph.review",
            "store_key": "store-product-run",
            "account_key": "",
            "subject": {
                "kind": "douyin_product", "id": "product-graph-key", "id_source": "platform",
            },
            "required_source_keys": ["doudian/products"],
            "metric_keywords": ["库存", "点击率"],
            "completion_kind": "metric_rule",
        }
        entry = {
            "schema_version": 3,
            "task_id": "a" * 16,
            "scope": "store-product-run:2026-08-22",
            "store_key": "store-product-run",
            "business_date": "2026-08-22",
            "captured_at_ms": captured_at_ms,
            "due_at_ms": captured_at_ms,
            "task_contract": contract,
        }
        product_graph = {"products": [{
            "product_key": "product-graph-key",
            "product_id": entity_key,
            "sku_id": None,
            "merchant_code": None,
            "entity_ref": f"douyin_product_id:{entity_key}",
        }]}
        http_receiver._atomic_json_write(
            http_receiver._suggestion_snapshots_path(),
            {f"{entry['scope']}|{entry['task_id']}": entry},
        )
        with patch.object(http_receiver, "build_douyin_product_graph", return_value=product_graph):
            run = http_receiver._start_product_task_run(entry)
        self.assertIsNotNone(run)
        self.assertGreater(run["observation_counts"]["baseline"], 0)

        http_receiver.save_data("doudian", {
            "schema_version": 2,
            "page_type": "products",
            "captured_at": captured_at_ms + 1_000,
            "store": {"key": "store-product-run", "label": "单品任务店", "confidence": "high"},
            "quality": {"score": 90, "row_count": 1},
            "tables": [{
                "headers": ["productid", "productname", "stock", "ctr"],
                "rows": [["product-run-001", "单品任务商品", "18", "2.3"]],
            }],
        })
        stored_entry = next(iter(http_receiver.load_suggestion_snapshots().values()))
        http_receiver._sync_product_task_run(stored_entry, {"status": "effective", "changes": []})
        completed = http_receiver._local_store().get_commerce_task_run(
            str(run["run_id"]), store_key="store-product-run",
        )
        self.assertEqual("completed", completed["status"])
        self.assertEqual("effective", completed["verdict"])
        self.assertGreater(completed["observation_counts"]["readback"], 0)

    def test_task_state_is_scoped_by_store_and_day_with_audit_history(self) -> None:
        task_id = "a" * 16
        http_receiver._remember_store_identity({"key": "store-a", "confidence": "high"})
        http_receiver._remember_store_identity({"key": "store-b", "confidence": "high"})
        started = http_receiver.update_task_state(
            task_id,
            "doing",
            operator="运营负责人",
            assignee="小王",
            title="修复商品主图",
            owner="货架运营",
            store_key="store-a",
            business_date="2026-08-04",
        )
        self.assertEqual(started["status"], "doing")
        self.assertEqual(started["assignee"], "小王")
        self.assertEqual(http_receiver.load_task_states("store-b", "2026-08-04"), {})

        with self.assertRaisesRegex(ValueError, "必须填写原因"):
            http_receiver.update_task_state(
                task_id, "blocked", store_key="store-a", business_date="2026-08-04"
            )
        blocked = http_receiver.update_task_state(
            task_id,
            "blocked",
            operator="运营负责人",
            note="等待设计重新出图",
            store_key="store-a",
            business_date="2026-08-04",
        )
        self.assertEqual(blocked["blocked_reason"], "等待设计重新出图")
        self.assertEqual(blocked["history"][-1]["from"], "doing")
        self.assertEqual(blocked["history"][-1]["to"], "blocked")

    def test_onboarding_resumes_from_real_store_snapshot_and_task_evidence(self) -> None:
        first_task = {
            "id": "b" * 16,
            "status": "todo",
            "title": "先修复商品主图",
            "action": "替换不合规主图",
            "evidence": "页面存在合规提示",
            "acceptance": "违规提示消失",
        }
        with patch.object(http_receiver, "build_store_catalog", return_value={"selected_store_key": "store-a", "stores": [{"key": "store-a"}]}), \
             patch.object(http_receiver, "list_snapshots", return_value=[
                 {"source": "doudian", "page_type": "overview", "fresh": True, "captured_at": 1785808801, "quality_score": 80},
                 {"source": "doudian", "page_type": "orders", "fresh": True, "captured_at": 1785808801, "quality_score": 80},
                 {"source": "doudian", "page_type": "products", "fresh": True, "captured_at": 1785808801, "quality_score": 80},
                 {"source": "doudian", "page_type": "shelf", "fresh": True, "captured_at": 1785808801, "quality_score": 80},
             ]), \
             patch.object(http_receiver, "build_scan_receipt", return_value={"summary": {"success": 2}}), \
             patch.object(http_receiver, "build_ops_manager", return_value={"all_tasks": [first_task]}):
            initial_state = {"schema_version": 2, "scopes": {"store-a": {"started_at": "2026-08-04 10:00:00", "store_confirmed_at": "2026-08-04 10:00:00"}}}
            http_receiver._atomic_json_write(http_receiver._onboarding_state_path(), initial_state)
            status = http_receiver.build_onboarding_status(state=initial_state)
            self.assertEqual(status["current_step"]["id"], "evidence")
            self.assertTrue(status["resume_supported"])
            self.assertEqual(status["first_task"]["evidence"], "页面存在合规提示")

            completed = http_receiver.update_onboarding_state("first_task_viewed")
            self.assertEqual(completed["status"], "completed")
            self.assertTrue(completed["value_milestones"]["activation_complete"])
            self.assertFalse(completed["value_milestones"]["verified_value_complete"])
            self.assertIn("价值待回读", completed["status_label"])

    def test_onboarding_activation_does_not_regress_when_qualified_snapshots_become_stale(self) -> None:
        first_task = {
            "id": "d" * 16,
            "status": "todo",
            "title": "复核低库存商品",
            "action": "确认补货安排",
            "evidence": "历史正式快照已识别低库存",
            "acceptance": "补货安排已确认",
        }
        confirmed_label = "2026-08-04 10:00:00"
        captured_after_confirmation = http_receiver._label_timestamp(confirmed_label) + 1
        state = {
            "schema_version": 2,
            "scopes": {"store-a": {
                "started_at": confirmed_label,
                "store_confirmed_at": confirmed_label,
                "first_task_viewed_at": "2026-08-04 10:05:00",
            }},
        }
        stale_snapshots = [
            {
                "source": "doudian",
                "page_type": page_type,
                "fresh": False,
                "captured_at": captured_after_confirmation,
                "quality_score": 80,
            }
            for page_type in ("overview", "orders", "products", "shelf")
        ]
        with patch.object(
            http_receiver,
            "build_store_catalog",
            return_value={"selected_store_key": "store-a", "stores": [{"key": "store-a"}]},
        ), patch.object(http_receiver, "list_snapshots", return_value=stale_snapshots), patch.object(
            http_receiver, "build_ops_manager", return_value={"all_tasks": [first_task]}
        ):
            status = http_receiver.build_onboarding_status(state=state)

        self.assertEqual("completed", status["status"])
        self.assertEqual(100, status["progress"]["percent"])
        self.assertTrue(next(item for item in status["steps"] if item["id"] == "sync")["complete"])
        self.assertEqual("stale", status["current_freshness"]["status"])
        self.assertFalse(status["current_freshness"]["fresh"])
        self.assertEqual(
            ["orders", "overview", "products", "shelf"],
            status["current_freshness"]["stale_or_missing_required_page_types"],
        )
        self.assertEqual(4, status["discovered"]["formal_snapshot_count"])
        self.assertEqual(4, status["discovered"]["usable_snapshot_count"])

    def test_unlink_store_account_removes_both_sides_without_deleting_data(self) -> None:
        store_key = "store-unlink-a"
        account_key = "account-unlink-a"
        http_receiver._remember_qianchuan_account({
            "key": account_key,
            "store_key": store_key,
            "confidence": "high",
            "identity_source": "hmac_qianchuan_account_id",
            "evidence_source": "manual_confirmation",
            "aliases": ["account-unlink-alias"],
        })
        http_receiver._remember_store_identity({
            "key": store_key,
            "confidence": "high",
            "identity_source": "hmac_douyin_shop_id",
            "evidence_source": "official_api",
        }, account_key)
        store_marker = http_receiver.DATA_DIR / "stores" / store_key / "doudian" / "overview.json"
        account_marker = http_receiver.DATA_DIR / "qianchuan_accounts" / account_key / "campaigns.json"
        store_marker.parent.mkdir(parents=True, exist_ok=True)
        account_marker.parent.mkdir(parents=True, exist_ok=True)
        store_marker.write_text('{"keep":"store"}', encoding="utf-8")
        account_marker.write_text('{"keep":"account"}', encoding="utf-8")

        result = http_receiver.unlink_store_account(store_key, account_key)

        store = next(item for item in http_receiver.list_store_identities() if item["key"] == store_key)
        account = next(item for item in http_receiver.list_qianchuan_accounts() if item["key"] == account_key)
        self.assertEqual([], store["account_keys"])
        self.assertEqual("", account["store_key"])
        self.assertEqual(["account-unlink-alias"], account["aliases"])
        self.assertEqual("official_api", store["evidence_source"])
        self.assertTrue(store_marker.exists())
        self.assertTrue(account_marker.exists())
        self.assertEqual("store", json.loads(store_marker.read_text(encoding="utf-8"))["keep"])
        self.assertEqual("account", json.loads(account_marker.read_text(encoding="utf-8"))["keep"])
        self.assertIn(account_key, [item["key"] for item in result["unlinked_accounts"]])

    def test_unlink_store_account_rejects_wrong_store_without_changing_relation(self) -> None:
        linked_store = "store-unlink-owner"
        wrong_store = "store-unlink-wrong"
        account_key = "account-unlink-owner"
        http_receiver._remember_qianchuan_account({"key": account_key, "store_key": linked_store})
        http_receiver._remember_store_identity({"key": linked_store}, account_key)
        http_receiver._remember_store_identity({"key": wrong_store})

        with self.assertRaisesRegex(ValueError, "其他店铺"):
            http_receiver.unlink_store_account(wrong_store, account_key)

        linked = next(item for item in http_receiver.list_store_identities() if item["key"] == linked_store)
        account = next(item for item in http_receiver.list_qianchuan_accounts() if item["key"] == account_key)
        self.assertEqual([account_key], linked["account_keys"])
        self.assertEqual(linked_store, account["store_key"])

    def test_http_unlink_clears_selected_account_but_preserves_selected_store(self) -> None:
        store_key = "store-unlink-current"
        account_key = "account-unlink-current"
        http_receiver._remember_qianchuan_account({"key": account_key, "store_key": store_key})
        http_receiver._remember_store_identity({"key": store_key}, account_key)
        http_receiver.select_store_context(store_key, account_key)
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/stores/unlink",
                data=json.dumps({"store_key": store_key, "account_key": account_key}).encode("utf-8"),
                headers=self._internal_http_headers(),
                method="POST",
            ))
            result = json.loads(response.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        settings = http_receiver.load_agent_settings()
        self.assertTrue(result["ok"])
        self.assertEqual(store_key, result["selected_store_key"])
        self.assertEqual("", result["selected_account_key"])
        self.assertEqual(store_key, settings["store_key"])
        self.assertEqual("", settings["qianchuan_account_key"])

    def test_unlink_tombstone_blocks_old_and_new_browser_snapshots_until_explicit_link(self) -> None:
        store_key = "store-revoke-snapshot"
        account_key = "account-revoke-snapshot"
        http_receiver._remember_qianchuan_account({"key": account_key, "store_key": store_key, "confidence": "high"})
        http_receiver._remember_store_identity({"key": store_key, "confidence": "high"}, account_key)
        http_receiver.select_store_context(store_key, account_key)
        captured_before_unlink = int(time.time() * 1000) - 1_000
        http_receiver.unlink_store_account(store_key, account_key)

        for captured_at in (captured_before_unlink, int(time.time() * 1000) + 1):
            payload = http_receiver.save_data("qianchuan", {
                "page_type": "overview",
                "captured_at": captured_at,
                "store": {"key": store_key, "confidence": "high"},
                "account": {"key": account_key, "store_key": store_key, "confidence": "high"},
                "quality": {"score": 90, "row_count": 1},
                "safe_metrics": {"spend": "10"},
            })
            self.assertEqual("binding_revoked", payload["data"]["identity_resolution"])
            account = next(item for item in http_receiver.list_qianchuan_accounts() if item["key"] == account_key)
            store = next(item for item in http_receiver.list_store_identities() if item["key"] == store_key)
            self.assertEqual("", account["store_key"])
            self.assertNotIn(account_key, store["account_keys"])
            self.assertTrue(http_receiver._binding_is_revoked(store_key, account_key))

        linked = http_receiver.link_store_account(store_key, account_key)
        self.assertEqual(account_key, linked["selected_account_key"])
        self.assertGreater(http_receiver._binding_generation(store_key, account_key), 0)
        self.assertFalse(http_receiver._binding_is_revoked(store_key, account_key))

    def test_unlink_and_relink_same_pair_permanently_invalidates_old_action_generation(self) -> None:
        action, _, _ = self._seed_authorized_execution(
            store_key="store-binding-aba",
            account_key="account-binding-aba",
            plan_id="plan-binding-aba",
        )
        http_receiver.unlink_store_account("store-binding-aba", "account-binding-aba")
        http_receiver.link_store_account("store-binding-aba", "account-binding-aba")
        with self.assertRaisesRegex(ValueError, "STORE_ACCOUNT_BINDING_CHANGED"):
            http_receiver.start_execution_preflight(action["action_id"])

    def test_link_transaction_second_catalog_write_failure_recovers_exact_previous_state(self) -> None:
        store_key = "store-link-transaction"
        account_key = "account-link-transaction"
        http_receiver._remember_store_identity({"key": store_key, "confidence": "high"})
        http_receiver._remember_qianchuan_account({"key": account_key, "confidence": "high"})
        original_atomic = http_receiver._atomic_json_write
        calls = 0

        def fail_once(path, payload):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise OSError("simulated account catalog failure")
            return original_atomic(path, payload)

        with patch.object(http_receiver, "_atomic_json_write", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "simulated account catalog failure"):
                http_receiver.link_store_account(store_key, account_key)

        account = next(item for item in http_receiver.list_qianchuan_accounts() if item["key"] == account_key)
        store = next(item for item in http_receiver.list_store_identities() if item["key"] == store_key)
        self.assertEqual("", account["store_key"])
        self.assertNotIn(account_key, store["account_keys"])
        self.assertFalse(http_receiver._binding_transaction_path().exists())

    def test_automatic_pair_capacity_never_creates_one_sided_binding(self) -> None:
        store_key = "store-capacity-pair"
        with patch.object(http_receiver, "MAX_STORE_ACCOUNT_BINDINGS", 2):
            for account_key in ("account-capacity-a", "account-capacity-b"):
                http_receiver._remember_qianchuan_account({"key": account_key, "store_key": store_key})
                http_receiver._remember_store_identity({"key": store_key}, account_key)
            http_receiver._remember_qianchuan_identity_pair(
                {"key": store_key, "confidence": "high"},
                {"key": "account-capacity-c", "store_key": store_key, "confidence": "high"},
            )
        account = next(item for item in http_receiver.list_qianchuan_accounts() if item["key"] == "account-capacity-c")
        store = next(item for item in http_receiver.list_store_identities() if item["key"] == store_key)
        self.assertEqual("", account["store_key"])
        self.assertEqual({"account-capacity-a", "account-capacity-b"}, set(store["account_keys"]))

    def test_initialized_registry_requires_manual_link_for_new_browser_pair(self) -> None:
        existing_store = "store-registry-existing"
        existing_account = "account-registry-existing"
        new_store = "store-registry-new"
        new_account = "account-registry-new"
        http_receiver._remember_store_identity({"key": existing_store, "confidence": "high"})
        http_receiver._remember_qianchuan_account({"key": existing_account, "confidence": "high"})
        http_receiver.link_store_account(existing_store, existing_account)
        http_receiver._remember_qianchuan_identity_pair(
            {"key": new_store, "confidence": "high", "identity_source": "data_attribute"},
            {
                "key": new_account,
                "store_key": new_store,
                "confidence": "high",
                "identity_source": "url_parameter",
            },
        )

        catalog = http_receiver.build_store_catalog()
        mirrored_store = next(item for item in catalog["stores"] if item["key"] == new_store)
        self.assertEqual([], mirrored_store["account_keys"])
        self.assertIn(new_account, [item["key"] for item in catalog["unlinked_accounts"]])
        self.assertIsNone(http_receiver._active_qianchuan_binding_scope(new_store, new_account))
        with self.assertRaisesRegex(ValueError, "尚未.*双向确认"):
            http_receiver.select_store_context(new_store, new_account)

        linked = http_receiver.link_store_account(new_store, new_account)
        scope = http_receiver._active_qianchuan_binding_scope(new_store, new_account)
        self.assertIsNotNone(scope)
        self.assertGreater(scope["binding_generation"], 0)
        self.assertEqual(new_account, linked["selected_account_key"])
        self.assertNotIn(new_account, [item["key"] for item in linked["unlinked_accounts"]])

    def test_manual_link_repairs_catalog_only_pair_after_registry_initialization(self) -> None:
        store_key = "store-registry-ghost"
        account_key = "account-registry-ghost"
        http_receiver.save_agent_settings({"binding_registry_initialized": True})
        http_receiver._save_binding_revocations({})
        http_receiver._remember_qianchuan_account({"key": account_key, "store_key": store_key})
        http_receiver._remember_store_identity({"key": store_key}, account_key)

        before = http_receiver.build_store_catalog()
        self.assertEqual([], before["stores"][0]["account_keys"])
        self.assertIn(account_key, [item["key"] for item in before["unlinked_accounts"]])
        self.assertIsNone(http_receiver._active_qianchuan_binding_scope(store_key, account_key))

        linked = http_receiver.link_store_account(store_key, account_key)
        relation = http_receiver._load_binding_revocations()[f"{store_key}:{account_key}"]
        self.assertTrue(relation["active"])
        self.assertEqual(1, relation["generation"])
        self.assertGreater(relation["linked_at_ms"], 0)
        self.assertEqual(account_key, linked["selected_account_key"])

    def test_legacy_uninitialized_registry_keeps_generation_zero_browser_pair(self) -> None:
        store_key = "store-legacy-auto"
        account_key = "account-legacy-auto"
        http_receiver._remember_qianchuan_identity_pair(
            {"key": store_key, "confidence": "high"},
            {"key": account_key, "store_key": store_key, "confidence": "high"},
        )

        scope = http_receiver._active_qianchuan_binding_scope(store_key, account_key)
        self.assertIsNotNone(scope)
        self.assertEqual(0, scope["binding_generation"])
        self.assertEqual([account_key], http_receiver.build_store_catalog()["stores"][0]["account_keys"])

    def test_build_store_catalog_reads_binding_authority_once_for_large_catalog(self) -> None:
        store_key = "store-large-catalog"
        accounts = [
            {"key": f"account-large-{index:04d}", "store_key": store_key}
            for index in range(http_receiver.MAX_STORE_ACCOUNT_BINDINGS)
        ]
        stores = [{
            "key": store_key,
            "account_keys": [item["key"] for item in accounts],
        }]
        settings = {
            **http_receiver.DEFAULT_AGENT_SETTINGS,
            "store_key": store_key,
            "binding_registry_initialized": False,
        }
        with patch.object(http_receiver, "list_store_identities", return_value=stores) as store_reader, \
             patch.object(http_receiver, "list_qianchuan_accounts", return_value=accounts) as account_reader, \
             patch.object(http_receiver, "_load_binding_revocations", return_value={}) as registry_reader, \
             patch.object(http_receiver, "load_agent_settings", return_value=settings) as settings_reader:
            catalog = http_receiver.build_store_catalog()

        self.assertEqual(http_receiver.MAX_STORE_ACCOUNT_BINDINGS, len(catalog["stores"][0]["account_keys"]))
        self.assertEqual(1, store_reader.call_count)
        self.assertEqual(1, account_reader.call_count)
        self.assertEqual(1, registry_reader.call_count)
        self.assertEqual(1, settings_reader.call_count)

    def test_store_catalog_exposes_doudian_specific_freshness(self) -> None:
        store_key = "store-doudian-freshness"
        http_receiver._remember_store_identity({"key": store_key, "confidence": "high"})
        snapshot_path = http_receiver._store_snapshot_path(store_key, "doudian", "overview")
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text('{"source":"doudian","page_type":"overview"}', encoding="utf-8")
        file_timestamp = snapshot_path.stat().st_mtime

        with patch.object(http_receiver.time, "time", return_value=file_timestamp + 1):
            fresh = http_receiver.build_store_catalog()["stores"][0]
        self.assertEqual(1, fresh["doudian_page_count"])
        self.assertTrue(fresh["doudian_fresh"])
        self.assertEqual(int(file_timestamp), fresh["doudian_updated_at"])

        with patch.object(
            http_receiver.time,
            "time",
            return_value=file_timestamp + http_receiver.STALE_SECONDS + 1,
        ):
            stale = http_receiver.build_store_catalog()["stores"][0]
        self.assertFalse(stale["doudian_fresh"])

    def test_missing_initialized_binding_registry_fails_closed(self) -> None:
        store_key = "store-registry-missing"
        account_key = "account-registry-missing"
        http_receiver._remember_qianchuan_account({"key": account_key, "store_key": store_key})
        http_receiver._remember_store_identity({"key": store_key}, account_key)
        http_receiver.select_store_context(store_key, account_key)
        http_receiver.unlink_store_account(store_key, account_key)
        http_receiver._binding_revocations_path().unlink()
        with self.assertRaisesRegex(OSError, "binding registry is missing"):
            http_receiver._load_binding_revocations()

    def test_binding_registry_rejects_empty_schema_and_invalid_permission_entries(self) -> None:
        valid_entry = {
            "store_key": "store-registry-strict",
            "account_key": "account-registry-strict",
            "generation": 1,
            "active": True,
            "linked_at_ms": 1,
            "revoked_at_ms": 0,
            "reason": "manual_confirmation",
        }
        invalid_payloads = (
            {},
            [],
            {"schema_version": 2, "revocations": []},
            {"schema_version": 1, "revocations": {}},
            {"schema_version": 1, "revocations": [None]},
            {"schema_version": 1, "revocations": [{**valid_entry, "generation": "1"}]},
            {"schema_version": 1, "revocations": [{**valid_entry, "active": 1}]},
            {"schema_version": 1, "revocations": [{**valid_entry, "reason": ""}]},
        )
        for payload in invalid_payloads:
            http_receiver._binding_revocations_path().write_text(
                json.dumps(payload), encoding="utf-8"
            )
            with self.subTest(payload=payload), self.assertRaises(OSError):
                http_receiver._load_binding_revocations()

        http_receiver._binding_revocations_path().write_text(
            json.dumps({"schema_version": 1, "revocations": [{**valid_entry, "linked_at_ms": True}]}),
            encoding="utf-8",
        )
        with self.assertRaises(OSError):
            http_receiver._binding_generation(
                valid_entry["store_key"], valid_entry["account_key"]
            )

    def test_mirror_binding_transaction_prepared_rolls_back_and_committed_rolls_forward(self) -> None:
        store_key = "store-mirror-recovery"
        account_key = "account-mirror-recovery"
        previous_store = {
            "key": store_key,
            "confidence": "high",
            "identity_source": "test",
            "evidence_source": "",
            "account_keys": [],
            "last_seen": "2026-08-26 12:00:00",
        }
        previous_account = {
            "key": account_key,
            "confidence": "high",
            "identity_source": "test",
            "evidence_source": "",
            "store_key": "",
            "aliases": [],
            "last_seen": "2026-08-26 12:00:00",
        }
        next_store = {
            **previous_store,
            "account_keys": [account_key],
            "last_seen": "2026-08-26 12:01:00",
        }
        next_account = {
            **previous_account,
            "store_key": store_key,
            "last_seen": "2026-08-26 12:01:00",
        }
        previous_stores, previous_accounts = http_receiver._catalog_payloads(
            {store_key: previous_store}, {account_key: previous_account}
        )
        next_stores, next_accounts = http_receiver._catalog_payloads(
            {store_key: next_store}, {account_key: next_account}
        )
        settings = http_receiver.load_agent_settings()
        revocations = http_receiver._binding_revocations_payload({})

        def journal(phase: str) -> dict:
            return {
                "schema_version": 1,
                "operation": "mirror",
                "phase": phase,
                "prepared_at_ms": int(time.time() * 1000),
                "store_key": store_key,
                "account_key": account_key,
                "previous": {
                    "stores": previous_stores,
                    "accounts": previous_accounts,
                    "settings": settings,
                    "revocations": revocations,
                },
                "guard_revocations": revocations,
                "next": {
                    "stores": next_stores,
                    "accounts": next_accounts,
                    "settings": settings,
                    "revocations": revocations,
                },
            }

        # Crash after only the store side was written: a prepared mirror must
        # restore the exact unbound pair, never expose a one-sided relation.
        http_receiver._atomic_json_write(http_receiver._store_catalog_path(), next_stores)
        http_receiver._atomic_json_write(http_receiver._account_catalog_path(), previous_accounts)
        http_receiver._atomic_json_write(http_receiver._binding_revocations_path(), revocations)
        http_receiver._atomic_json_write(http_receiver._settings_path(), settings)
        http_receiver._atomic_json_write(http_receiver._binding_transaction_path(), journal("prepared"))
        http_receiver._recover_binding_transaction()
        rolled_back_store = next(
            item for item in http_receiver.list_store_identities() if item["key"] == store_key
        )
        rolled_back_account = next(
            item for item in http_receiver.list_qianchuan_accounts() if item["key"] == account_key
        )
        self.assertNotIn(account_key, rolled_back_store["account_keys"])
        self.assertEqual("", rolled_back_account["store_key"])
        self.assertFalse(http_receiver._binding_transaction_path().exists())

        # A committed marker means the logical pair won. Even if a crash left
        # only the account side visible, recovery must roll both sides forward.
        http_receiver._atomic_json_write(http_receiver._store_catalog_path(), previous_stores)
        http_receiver._atomic_json_write(http_receiver._account_catalog_path(), next_accounts)
        http_receiver._atomic_json_write(http_receiver._binding_transaction_path(), journal("committed"))
        http_receiver._recover_binding_transaction()
        rolled_forward_store = next(
            item for item in http_receiver.list_store_identities() if item["key"] == store_key
        )
        rolled_forward_account = next(
            item for item in http_receiver.list_qianchuan_accounts() if item["key"] == account_key
        )
        self.assertIn(account_key, rolled_forward_store["account_keys"])
        self.assertEqual(store_key, rolled_forward_account["store_key"])
        self.assertFalse(http_receiver._binding_transaction_path().exists())

    def test_onboarding_marks_evidence_backed_task_result_as_verified_value(self) -> None:
        task_id = "c" * 16
        first_task = {
            "id": task_id,
            "status": "done",
            "title": "复核商品主图",
            "action": "替换不合规主图",
            "evidence": "页面存在合规提示",
            "acceptance": "违规提示消失",
        }
        state = {
            "schema_version": 2,
            "scopes": {"store-a": {
                "started_at": "2026-08-04 10:00:00",
                "store_confirmed_at": "2026-08-04 10:00:00",
                "first_task_viewed_at": "2026-08-04 10:05:00",
            }},
        }
        http_receiver._atomic_json_write(
            http_receiver._suggestion_snapshots_path(),
            {f"store-a:2026-08-04|{task_id}": {
                "task_id": task_id,
                "scope": "store-a:2026-08-04",
                "evaluated": True,
                "evaluation": {"status": "inconclusive", "evaluated_at": "2026-08-04 11:00:00"},
            }},
        )
        snapshots = [
            {"source": "doudian", "page_type": page_type, "fresh": True, "captured_at": 1785808801, "quality_score": 80}
            for page_type in ("overview", "orders", "products", "shelf")
        ]
        with patch.object(http_receiver, "build_store_catalog", return_value={"selected_store_key": "store-a", "stores": [{"key": "store-a"}]}), \
             patch.object(http_receiver, "list_snapshots", return_value=snapshots), \
             patch.object(http_receiver, "build_ops_manager", return_value={"all_tasks": [first_task]}):
            status = http_receiver.build_onboarding_status(state=state)
        self.assertTrue(status["value_milestones"]["activation_complete"])
        self.assertTrue(status["value_milestones"]["verified_value_complete"])
        self.assertEqual(1, status["value_milestones"]["evidence_backed_task_result_count"])
        self.assertIn("已验证价值", status["status_label"])

    def test_embedded_qianchuan_scan_drives_plan_recommendations(self) -> None:
        http_receiver.save_data("doudian", {"page_type": "qianchuan_live", "quality": {"score": 90, "row_count": 1}, "tables": [{"headers": ["抖音号", "投放状态", "投放设置", "整体消耗(元)", "整体支付ROI", "整体成交订单数"], "rows": [["直播大屏\n测试店铺\n设置直播规划", "投放中", "ROI目标\n3.00", "500", "3.50", "6"]]}]})
        recommendations = http_receiver.build_plan_recommendations()
        item = next(value for value in recommendations if value["plan"] == "直播大屏 · 测试店铺")
        self.assertEqual(item["action_type"], "scale_cautiously")
        self.assertEqual(item["evidence"]["roi_target"], 3.0)

    def test_live_material_row_with_douyin_account_never_generates_plan_recommendation(self) -> None:
        http_receiver.save_data("doudian", {
            "page_type": "qianchuan_live",
            "captured_at": int(time.time() * 1000),
            "quality": {"score": 90, "row_count": 1},
            "tables": [{
                "headers": ["抖音号", "商品", "视频", "投放状态", "整体消耗(元)", "整体支付ROI", "整体成交订单数"],
                "rows": [["直播间账号", "兽醒纪男士活力裤", "素材 8/12", "投放中", "500", "0.20", "0"]],
            }],
        })

        self.assertEqual([], http_receiver.build_plan_recommendations())

    def test_auto_scan_status_is_saved_for_reports(self) -> None:
        saved = http_receiver.save_scan_status({"status": "partial", "account_mode": "auto", "index": 16, "total": 16, "success": 14, "failed": 2, "results": [{"id": "orders", "ok": True}]})
        self.assertEqual(saved["success"], 14)
        self.assertEqual(saved["account_mode"], "auto")
        self.assertEqual(http_receiver.load_scan_status()["failed"], 2)
        report = http_receiver.generate_daily_report("2026-07-22")
        self.assertIn("自动巡检：partial；成功 14 页，失败 2 页", Path(report["path"]).read_text(encoding="utf-8"))

    def test_scan_status_rejects_stale_runs_and_accepts_interrupted(self) -> None:
        first = http_receiver.save_scan_status({
            "status": "running", "run_id": "scan_run_a1", "revision": 2,
            "heartbeat_at": int(time.time() * 1000), "results": [],
        })
        self.assertEqual(first["run_id"], "scan_run_a1")
        with self.assertRaisesRegex(ValueError, "STALE_SCAN_REVISION"):
            http_receiver.save_scan_status({
                "status": "running", "run_id": "scan_run_a1", "revision": 1,
                "heartbeat_at": int(time.time() * 1000), "results": [],
            })
        second = http_receiver.save_scan_status({
            "status": "running", "run_id": "scan_run_b2", "revision": 1,
            "heartbeat_at": int(time.time() * 1000), "results": [],
        })
        self.assertEqual(second["run_id"], "scan_run_b2")
        with self.assertRaisesRegex(ValueError, "STALE_SCAN_RUN"):
            http_receiver.save_scan_status({
                "status": "completed", "run_id": "scan_run_a1", "revision": 3,
                "results": [],
            })
        interrupted = http_receiver.save_scan_status({
            "status": "interrupted", "run_id": "scan_run_b2", "revision": 2,
            "finished_at": int(time.time() * 1000), "results": [],
        })
        self.assertEqual(interrupted["status"], "interrupted")

    def test_scan_status_equal_revision_is_idempotent_but_cannot_replace_progress(self) -> None:
        current = http_receiver.save_scan_status({
            "status": "running",
            "run_id": "scan_equal_revision1",
            "revision": 4,
            "store_key": "store_selected",
            "planned_page_ids": ["overview", "orders"],
            "execution_page_ids": ["overview", "orders"],
            "attempted_page_ids": ["overview"],
            "pending_page_ids": ["orders"],
            "results": [{"id": "overview", "ok": True}],
        })

        replay = http_receiver.save_scan_status(dict(current))
        self.assertEqual(current, replay)

        stale_conflict = {**current, "attempted_page_ids": [], "results": []}
        with self.assertRaisesRegex(ValueError, "SCAN_REVISION_CONFLICT"):
            http_receiver.save_scan_status(stale_conflict)
        self.assertEqual(["overview"], http_receiver.load_scan_status()["attempted_page_ids"])

    def test_scan_status_immutable_terminals_reject_later_terminal_results(self) -> None:
        http_receiver.save_scan_status({
            "status": "cancelled",
            "run_id": "scan_cancelled_terminal1",
            "revision": 1,
            "finished_at": int(time.time() * 1000),
            "results": [],
        })
        with self.assertRaisesRegex(ValueError, "SCAN_TERMINAL_STATE"):
            http_receiver.save_scan_status({
                "status": "completed",
                "run_id": "scan_cancelled_terminal1",
                "revision": 2,
                "finished_at": int(time.time() * 1000),
                "results": [],
            })
        self.assertEqual("cancelled", http_receiver.load_scan_status()["status"])

        http_receiver.save_scan_status({
            "status": "running",
            "run_id": "scan_completed_terminal2",
            "revision": 1,
            "heartbeat_at": int(time.time() * 1000),
            "results": [],
        })
        http_receiver.save_scan_status({
            "status": "completed",
            "run_id": "scan_completed_terminal2",
            "revision": 2,
            "finished_at": int(time.time() * 1000),
            "results": [],
        })
        with self.assertRaisesRegex(ValueError, "SCAN_TERMINAL_STATE"):
            http_receiver.save_scan_status({
                "status": "partial",
                "run_id": "scan_completed_terminal2",
                "revision": 3,
                "finished_at": int(time.time() * 1000),
                "results": [],
            })
        self.assertEqual("completed", http_receiver.load_scan_status()["status"])

    def test_scan_status_persists_verified_root_lineage_through_recovery(self) -> None:
        now_ms = int(time.time() * 1000)
        root_run_id = "scan_root_13_page_contract"
        child_run_id = "scan_retry_13_page_contract"
        planned = ["overview", "orders", "products"]
        root = http_receiver.save_scan_status({
            "status": "interrupted",
            "scope": "full",
            "run_id": root_run_id,
            "root_run_id": root_run_id,
            "root_started_at": now_ms,
            "parent_run_id": "",
            "resumed_from_run_id": "",
            "started_at": now_ms,
            "finished_at": now_ms + 100,
            "interrupted": True,
            "interrupted_at": now_ms + 100,
            "interruption_reason": "recovery-alarm",
            "revision": 4,
            "planned_page_ids": planned,
            "execution_page_ids": ["overview", "orders", "products"],
            "attempted_page_ids": ["overview", "orders"],
            "pending_page_ids": ["orders", "products"],
            "results": [
                {"id": "overview", "source": "doudian", "ok": True, "quality": {"score": 90}},
                {"id": "orders", "source": "doudian", "ok": False, "error": "页面超时"},
            ],
            "recovery": {
                "schema_version": 1,
                "state": "paused_interrupted",
                "error_code": "SCAN_INTERRUPTED",
                "action": "resume_failed_pages",
                "message": "浏览器中断了巡店，断点已保留；请手动续跑。",
                "automatic_resume": False,
                "requires_user_action": True,
                "can_resume": True,
                "resume_page_ids": ["orders", "products"],
            },
        })
        self.assertEqual(root_run_id, root["root_run_id"])

        child = http_receiver.save_scan_status({
            "status": "running",
            "scope": "full",
            "run_id": child_run_id,
            "root_run_id": root_run_id,
            "root_started_at": now_ms,
            "parent_run_id": root_run_id,
            "resumed_from_run_id": root_run_id,
            "started_at": now_ms + 200,
            "heartbeat_at": now_ms + 200,
            "revision": 1,
            "planned_page_ids": planned,
            "execution_page_ids": ["orders", "products"],
            "attempted_page_ids": ["overview", "orders"],
            "pending_page_ids": ["orders", "products"],
            "results": root["results"],
            "recovery": {
                "schema_version": 1,
                "state": "running",
                "error_code": "",
                "action": "none",
                "message": "巡店正在运行。",
                "automatic_resume": False,
                "requires_user_action": False,
                "can_resume": False,
                "resume_page_ids": ["orders", "products"],
            },
        })

        loaded = http_receiver.load_scan_status()
        self.assertEqual(child, loaded)
        self.assertEqual(root_run_id, loaded["root_run_id"])
        self.assertEqual(root_run_id, loaded["parent_run_id"])
        self.assertEqual(["orders", "products"], loaded["execution_page_ids"])
        self.assertEqual("running", loaded["recovery"]["state"])

        receipt = http_receiver.build_scan_receipt()
        self.assertTrue(receipt["recovery_attempt"])
        self.assertTrue(receipt["lineage_verified"])
        self.assertEqual(root_run_id, receipt["root_run_id"])
        self.assertEqual(3, receipt["root_scope_page_count"])
        report = http_receiver.generate_daily_report("2026-08-24")
        report_text = Path(report["path"]).read_text(encoding="utf-8")
        self.assertIn(f"根轮 {root_run_id}（3 页）", report_text)
        self.assertIn(f"当前续跑 {child_run_id}", report_text)

    def test_scan_status_rejects_malformed_lineage_pages_and_recovery(self) -> None:
        now_ms = int(time.time() * 1000)
        root_run_id = "scan_root_validation_13"
        planned = ["overview", "orders"]
        http_receiver.save_scan_status({
            "status": "interrupted",
            "run_id": root_run_id,
            "root_run_id": root_run_id,
            "root_started_at": now_ms,
            "started_at": now_ms,
            "revision": 1,
            "planned_page_ids": planned,
            "execution_page_ids": planned,
            "attempted_page_ids": ["overview"],
            "pending_page_ids": ["orders"],
            "results": [{"id": "overview", "ok": True}],
        })
        base = {
            "status": "running",
            "run_id": "scan_child_validation_13",
            "root_run_id": root_run_id,
            "root_started_at": now_ms,
            "parent_run_id": root_run_id,
            "resumed_from_run_id": root_run_id,
            "started_at": now_ms + 10,
            "heartbeat_at": now_ms + 10,
            "revision": 1,
            "planned_page_ids": planned,
            "execution_page_ids": ["orders"],
            "attempted_page_ids": ["overview"],
            "pending_page_ids": ["orders"],
            "results": [{"id": "overview", "ok": True}],
            "recovery": {
                "schema_version": 1,
                "state": "running",
                "error_code": "",
                "action": "none",
                "message": "巡店正在运行。",
                "automatic_resume": False,
                "requires_user_action": False,
                "can_resume": False,
                "resume_page_ids": ["orders"],
            },
        }

        invalid_cases = []
        bad_root = json.loads(json.dumps(base, ensure_ascii=False))
        bad_root["root_run_id"] = "../bad-root"
        invalid_cases.append(("root", bad_root))
        wrong_root = json.loads(json.dumps(base, ensure_ascii=False))
        wrong_root["root_run_id"] = "scan_unrelated_root_13"
        invalid_cases.append(("wrong_root", wrong_root))
        bad_page = json.loads(json.dumps(base, ensure_ascii=False))
        bad_page["execution_page_ids"] = ["orders", "../cookie"]
        invalid_cases.append(("page", bad_page))
        bad_result = json.loads(json.dumps(base, ensure_ascii=False))
        bad_result["results"] = [{"id": 123, "ok": True}]
        invalid_cases.append(("result", bad_result))
        bad_recovery = json.loads(json.dumps(base, ensure_ascii=False))
        bad_recovery["recovery"]["state"] = "auto_resume_everything"
        invalid_cases.append(("recovery", bad_recovery))
        outside_recovery = json.loads(json.dumps(base, ensure_ascii=False))
        outside_recovery["recovery"]["resume_page_ids"] = ["funds"]
        invalid_cases.append(("recovery_scope", outside_recovery))

        for name, payload in invalid_cases:
            with self.subTest(name=name), self.assertRaises(ValueError):
                http_receiver.save_scan_status(payload)
        self.assertEqual(root_run_id, http_receiver.load_scan_status()["run_id"])

    def test_scan_page_push_is_idempotent_for_same_run_and_page(self) -> None:
        scope = {"store_key": "store_selected", "account_key": ""}
        context = {"run_id": "scan_run_idempotent1", "page_id": "overview"}
        http_receiver.save_scan_status({
            "status": "running",
            "run_id": context["run_id"],
            "revision": 1,
            "store_key": scope["store_key"],
            "account_key": scope["account_key"],
            "planned_page_ids": [context["page_id"]],
            "execution_page_ids": [context["page_id"]],
            "results": [],
        })
        first = http_receiver.save_scan_page_once(
            "doudian",
            {
                "page_type": "overview",
                "captured_at": 1_785_200_000_000,
                "store": {"key": "store_selected", "confidence": "confirmed_session"},
                "safe_metrics": {"成交金额": 100},
            },
            scope,
            context,
        )
        replay = http_receiver.save_scan_page_once(
            "doudian",
            {
                "page_type": "overview",
                "captured_at": 1_785_200_010_000,
                "store": {"key": "store_selected", "confidence": "confirmed_session"},
                "safe_metrics": {"成交金额": 999},
            },
            scope,
            context,
        )
        self.assertTrue(first["ok"])
        self.assertTrue(replay["idempotent_replay"])
        current = json.loads(http_receiver._snapshot_path("doudian", "overview").read_text(encoding="utf-8"))
        self.assertEqual(current["data"]["safe_metrics"]["成交金额"], 100)

    def test_scan_page_push_is_bound_to_active_server_lease_and_execution_scope(self) -> None:
        scope = {"store_key": "store_alpha1", "account_key": ""}
        run_id = "scan_server_lease1"
        page = {
            "page_type": "overview",
            "captured_at": int(time.time() * 1000),
            "store": {"key": "store_bravo2", "confidence": "confirmed_session"},
            "quality": {"score": 90},
        }

        with self.assertRaisesRegex(ValueError, "STALE_SCAN_RUN"):
            http_receiver.save_scan_page_once(
                "doudian", page, {"store_key": "store_bravo2", "account_key": ""},
                {"run_id": run_id, "page_id": "overview"},
            )

        active = http_receiver.save_scan_status({
            "status": "running",
            "run_id": run_id,
            "revision": 1,
            "store_key": scope["store_key"],
            "account_key": scope["account_key"],
            "planned_page_ids": ["overview", "orders"],
            "execution_page_ids": ["overview"],
            "results": [],
        })
        with self.assertRaisesRegex(ValueError, "SCAN_LEASE_STORE_MISMATCH"):
            http_receiver.save_scan_page_once(
                "doudian", page, {"store_key": "store_bravo2", "account_key": ""},
                {"run_id": run_id, "page_id": "overview"},
            )
        with self.assertRaisesRegex(ValueError, "SCAN_PAGE_OUTSIDE_EXECUTION_SCOPE"):
            http_receiver.save_scan_page_once(
                "doudian",
                {**page, "page_type": "orders", "store": {"key": scope["store_key"], "confidence": "confirmed_session"}},
                scope,
                {"run_id": run_id, "page_id": "orders"},
            )
        self.assertIsNone(http_receiver.load_data("doudian", "overview", store_key="store_bravo2"))

        accepted = http_receiver.save_scan_page_once(
            "doudian",
            {**page, "store": {"key": scope["store_key"], "confidence": "confirmed_session"}},
            scope,
            {"run_id": run_id, "page_id": "overview"},
        )
        self.assertTrue(accepted["ok"])

        http_receiver.save_scan_status({
            **active,
            "status": "completed",
            "revision": 2,
            "finished_at": int(time.time() * 1000),
        })
        with self.assertRaisesRegex(ValueError, "SCAN_NOT_WRITABLE"):
            http_receiver.save_scan_page_once(
                "doudian",
                {**page, "page_type": "orders", "store": {"key": scope["store_key"], "confidence": "confirmed_session"}},
                scope,
                {"run_id": run_id, "page_id": "orders"},
            )

        account_run_id = "scan_account_lease1"
        http_receiver.save_scan_status({
            "status": "running",
            "run_id": account_run_id,
            "revision": 1,
            "store_key": scope["store_key"],
            "account_key": "acct_alpha1",
            "planned_page_ids": ["qianchuan_overview"],
            "execution_page_ids": ["qianchuan_overview"],
            "results": [],
        })
        with self.assertRaisesRegex(ValueError, "SCAN_LEASE_ACCOUNT_MISMATCH"):
            http_receiver.save_scan_page_once(
                "qianchuan",
                {
                    "page_type": "overview",
                    "captured_at": int(time.time() * 1000),
                    "store": {"key": scope["store_key"], "confidence": "confirmed_session"},
                    "account": {"key": "acct_bravo2", "confidence": "confirmed_session"},
                    "quality": {"score": 90},
                },
                {"store_key": scope["store_key"], "account_key": "acct_bravo2"},
                {"run_id": account_run_id, "page_id": "qianchuan_overview"},
            )

    def test_scan_receipt_explains_coverage_quality_and_single_page_retry_targets(self) -> None:
        http_receiver.save_scan_status(
            {
                "status": "partial",
                "account_key": "acct_safe1234",
                "started_at": 1_785_200_000_000,
                "finished_at": 1_785_200_060_000,
                "total": 3,
                "success": 2,
                "failed": 1,
                "results": [
                    {
                        "id": "orders",
                        "label": "订单管理",
                        "source": "doudian",
                        "ok": True,
                        "collection_complete": True,
                        "quality": {"score": 90, "metric_count": 4, "row_count": 12},
                    },
                    {
                        "id": "qianchuan_campaigns",
                        "label": "千川商品推广",
                        "source": "qianchuan",
                        "ok": True,
                        "collection_complete": True,
                        "account_label": "主投放账号",
                        "quality": {"score": 60, "metric_count": 2, "row_count": 1},
                    },
                    {
                        "id": "qianchuan_video_library",
                        "label": "千川视频库",
                        "source": "qianchuan",
                        "ok": False,
                        "error": "页面类型未识别",
                    },
                ],
            }
        )
        receipt = http_receiver.build_scan_receipt()
        self.assertEqual(receipt["readiness"], "attention")
        self.assertFalse(receipt["analysis_ready"])
        self.assertEqual(receipt["account_label"], "千川账户 FE1234")
        self.assertEqual(receipt["summary"]["coverage_rate"], 100)
        self.assertEqual(receipt["summary"]["needs_review"], 1)
        self.assertEqual(receipt["failed_page_ids"], ["qianchuan_video_library"])
        self.assertEqual(receipt["sources"]["qianchuan"]["failed"], 1)
        self.assertTrue(any("单独重试" in warning for warning in receipt["warnings"]))

    def test_scan_receipt_includes_failed_and_unattempted_resume_pages(self) -> None:
        http_receiver.save_scan_status({
            "status": "partial",
            "run_id": "scan_resume_pages1",
            "revision": 1,
            "planned_page_ids": ["overview", "orders", "products"],
            "attempted_page_ids": ["overview", "orders"],
            "pending_page_ids": ["products"],
            "total": 3,
            "results": [
                {"id": "overview", "source": "doudian", "ok": True, "collection_complete": True},
                {"id": "orders", "source": "doudian", "ok": False, "error": "页面超时"},
            ],
        })
        receipt = http_receiver.build_scan_receipt()
        self.assertEqual(receipt["pending_page_ids"], ["products"])
        self.assertEqual(receipt["resume_page_ids"], ["orders", "products"])
        self.assertTrue(any("断点继续" in warning for warning in receipt["warnings"]))

    def test_full_scan_receipt_rejects_reduced_doudian_scope_as_one_hundred_percent(self) -> None:
        reduced_page_ids = [
            "overview", "orders", "refunds", "products", "inventory", "reviews",
            "shelf", "live", "short_video", "image_text", "recommend_card",
        ]
        http_receiver.save_scan_status({
            "status": "completed",
            "scope": "full",
            "planned_page_ids": reduced_page_ids,
            "attempted_page_ids": reduced_page_ids,
            "total": len(reduced_page_ids),
            "results": [
                {"id": page_id, "source": "doudian", "ok": True, "collection_complete": True, "quality": {"score": 90}}
                for page_id in reduced_page_ids
            ],
        })

        receipt = http_receiver.build_scan_receipt()

        self.assertEqual(receipt["summary"]["total"], 13)
        self.assertEqual(receipt["summary"]["completed"], 11)
        self.assertEqual(receipt["summary"]["coverage_rate"], 85)
        self.assertEqual(receipt["missing_contract_page_ids"], ["search", "funds"])
        self.assertEqual(receipt["pending_page_ids"], ["search", "funds"])
        self.assertFalse(receipt["analysis_ready"])
        self.assertEqual(receipt["readiness"], "attention")

    def test_full_scan_receipt_uses_canonical_doudian_denominator(self) -> None:
        page_ids = list(http_receiver.SCAN_FULL_DOUDIAN_PAGE_IDS)
        http_receiver.save_scan_status({
            "status": "completed",
            "scope": "full",
            "planned_page_ids": page_ids,
            "attempted_page_ids": page_ids,
            "total": len(page_ids),
            "results": [
                {"id": page_id, "source": "doudian", "ok": True, "collection_complete": True, "quality": {"score": 90}}
                for page_id in page_ids
            ],
        })

        receipt = http_receiver.build_scan_receipt()

        self.assertEqual(receipt["summary"]["coverage_rate"], 100)
        self.assertTrue(receipt["scope_contract"]["complete"])
        self.assertTrue(receipt["analysis_ready"])

    def test_quick_scan_is_first_value_ready_without_claiming_full_analysis_ready(self) -> None:
        http_receiver.save_scan_status({
            "status": "completed",
            "scope": "quick",
            "total": 2,
            "results": [
                {"id": "overview", "label": "经营概览", "source": "doudian", "ok": True, "collection_complete": True, "quality": {"score": 90, "row_count": 2}},
                {"id": "qianchuan_overview", "label": "千川首页", "source": "qianchuan", "ok": True, "collection_complete": True, "quality": {"score": 90, "row_count": 2}},
            ],
        })

        receipt = http_receiver.build_scan_receipt()

        self.assertEqual(receipt["readiness"], "quick_ready")
        self.assertTrue(receipt["first_value_ready"])
        self.assertFalse(receipt["analysis_ready"])

    def test_product_graph_scan_has_its_own_receipt_scope(self) -> None:
        saved = http_receiver.save_scan_status({
            "status": "completed",
            "scope": "product_graph",
            "targeted_page_ids": ["products", "inventory", "shelf"],
            "total": 3,
            "results": [
                {"id": page_id, "label": page_id, "source": "doudian", "ok": True, "collection_complete": True, "quality": {"score": 90, "row_count": 2}}
                for page_id in ("products", "inventory", "shelf")
            ],
        })

        receipt = http_receiver.build_scan_receipt()

        self.assertEqual("product_graph", saved["scope"])
        self.assertEqual("product_graph", receipt["scope"])
        self.assertEqual("product_graph_ready", receipt["readiness"])
        self.assertTrue(receipt["first_value_ready"])
        self.assertFalse(receipt["analysis_ready"])

    def test_history_snapshots_build_seven_day_trends(self) -> None:
        now_ms = int(time.time() * 1000)
        http_receiver.save_data("doudian", {"page_type": "shelf", "captured_at": now_ms - 60000, "quality": {"score": 70}, "safe_metrics": {"曝光人数": "20", "点击人数": "2"}})
        http_receiver.save_data("doudian", {"page_type": "shelf", "captured_at": now_ms, "quality": {"score": 70}, "safe_metrics": {"曝光人数": "30", "点击人数": "6"}})
        trends = http_receiver.build_trends(7, "doudian", "shelf")
        exposure = next(item for item in trends["changes"] if item["label"] == "曝光人数")
        self.assertEqual(exposure["first"], 20)
        self.assertEqual(exposure["last"], 30)
        self.assertEqual(exposure["delta_percent"], 50)

    def test_qianchuan_history_isolated_by_binding_generation_after_relink(self) -> None:
        store_key = "store-history-lease1"
        account_key = "acct-history-lease1"
        http_receiver._remember_store_identity({
            "key": store_key, "confidence": "high", "identity_source": "test",
        })
        http_receiver._remember_qianchuan_account({
            "key": account_key, "confidence": "high", "identity_source": "test",
        })
        http_receiver.link_store_account(store_key, account_key)
        first_scope = http_receiver._active_qianchuan_binding_scope(store_key, account_key)
        http_receiver.save_agent_settings({
            "store_key": store_key,
            "qianchuan_account_key": account_key,
        })
        http_receiver.save_data("qianchuan", {
            "page_type": "live_dashboard",
            "captured_at": first_scope["linked_at_ms"] + 1,
            "store": {"key": store_key, "confidence": "high"},
            "account": {
                "key": account_key,
                "store_key": store_key,
                "confidence": "high",
            },
            "quality": {"score": 90},
            "safe_metrics": {"消耗": 100, "成交订单": 2, "ROI": 1.2},
        })
        first_points = http_receiver.load_history("qianchuan", "live_dashboard", 1)
        self.assertEqual(1, len(first_points))
        self.assertEqual(first_scope["binding_generation"], first_points[0]["binding_scope"]["binding_generation"])
        self.assertEqual(first_scope["linked_at_ms"], first_points[0]["binding_scope"]["linked_at_ms"])

        http_receiver.unlink_store_account(store_key, account_key)
        http_receiver.link_store_account(store_key, account_key)
        second_scope = http_receiver._active_qianchuan_binding_scope(store_key, account_key)
        http_receiver.save_agent_settings({
            "store_key": store_key,
            "qianchuan_account_key": account_key,
        })
        self.assertEqual([], http_receiver.load_history("qianchuan", "live_dashboard", 1))

        http_receiver.save_data("qianchuan", {
            "page_type": "live_dashboard",
            "captured_at": second_scope["linked_at_ms"] + 1,
            "store": {"key": store_key, "confidence": "high"},
            "account": {
                "key": account_key,
                "store_key": store_key,
                "confidence": "high",
            },
            "quality": {"score": 90},
            "safe_metrics": {"消耗": 10, "成交订单": 1, "ROI": 2.0},
        })
        current_points = http_receiver.load_history("qianchuan", "live_dashboard", 1)
        self.assertEqual(1, len(current_points))
        self.assertEqual(second_scope["binding_generation"], current_points[0]["binding_scope"]["binding_generation"])
        self.assertEqual(10, current_points[0]["safe_metrics"]["消耗"])
        trends = http_receiver.build_trends(1, "qianchuan", "live_dashboard")
        self.assertEqual([], trends["changes"], "old-generation spend must not form a current trend")

    def test_http_push_requires_bridge_header_and_updates_catalog(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        body = json.dumps(
            {
                "source": "doudian",
                "data": {
                    "schema_version": 2,
                    "page_type": "overview",
                    "quality": {"score": 60, "metric_count": 1, "row_count": 0},
                    "metrics": {"订单": "2"},
                },
            }
        ).encode("utf-8")
        try:
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(
                    urllib.request.Request(
                        f"{base_url}/push",
                        data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                )
            self.assertEqual(context.exception.code, 403)

            response = urllib.request.urlopen(
                urllib.request.Request(
                    f"{base_url}/push",
                    data=body,
                    headers=self._internal_http_headers(),
                    method="POST",
                )
            )
            self.assertTrue(json.loads(response.read())["ok"])

            catalog = json.loads(self._internal_http_get(f"{base_url}/catalog").read())
            self.assertEqual(catalog["snapshots"][0]["page_type"], "overview")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_oceanengine_corrupt_sync_status_degrades_without_rewriting_evidence(self) -> None:
        path = http_receiver.DATA_DIR / "oceanengine_sync_status.json"
        evidence = b'{"unfinished":'
        path.write_bytes(evidence)
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            status = json.loads(
                self._internal_http_get(f"{base_url}/oauth/oceanengine/sync-status").read()
            )
            self.assertFalse(status["ok"])
            self.assertEqual("SYNC_STATUS_CORRUPT", status["error"]["code"])
            center = json.loads(
                self._internal_http_get(f"{base_url}/oauth/oceanengine/account-center").read()
            )
            self.assertIsInstance(center["accounts"], list)
            self.assertFalse(center["platform_write_enabled"])
            self.assertEqual(evidence, path.read_bytes())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_push_rejects_nonfinite_json_without_persisting_snapshot(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            for token in ("NaN", "Infinity", "-Infinity", "1e9999", "-1e9999"):
                with self.subTest(token=token), self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(
                        urllib.request.Request(
                            f"{base_url}/push",
                            data=(
                                '{"source":"doudian","data":{"schema_version":2,'
                                '"page_type":"overview","quality":{"score":90},'
                                f'"metrics":{{"orders":{token}}}}}}}'
                            ).encode("utf-8"),
                            headers=self._internal_http_headers(),
                            method="POST",
                        )
                    )
                self.assertEqual(400, context.exception.code)
                error = json.loads(context.exception.read())
                self.assertIn("not finite", error["error"])

            self.assertIsNone(http_receiver.load_data("doudian", "overview"))
            catalog = json.loads(self._internal_http_get(f"{base_url}/catalog").read())
            self.assertEqual([], catalog["snapshots"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_response_never_emits_nonstandard_json_from_legacy_snapshot(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            with (
                patch.object(
                    http_receiver,
                    "load_data",
                    return_value={
                        "schema_version": 2,
                        "page_type": "overview",
                        "metrics": {"orders": float("nan")},
                    },
                ),
                self.assertRaises(urllib.error.HTTPError) as context,
            ):
                self._internal_http_get(f"{base_url}/data/doudian/overview")
            self.assertEqual(500, context.exception.code)
            raw = context.exception.read().decode("utf-8")
            self.assertNotIn("NaN", raw)
            self.assertNotIn("Infinity", raw)
            self.assertEqual(
                "INTERNAL_RESPONSE_SERIALIZATION_FAILED",
                json.loads(raw)["error"],
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_save_data_rejects_internal_nonfinite_values_before_commit(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite JSON"):
            http_receiver.save_data(
                "doudian",
                {
                    "schema_version": 2,
                    "page_type": "overview",
                    "quality": {"score": 90},
                    "metrics": {"orders": float("nan")},
                },
            )
        self.assertIsNone(http_receiver.load_data("doudian", "overview"))

    def test_http_oceanengine_account_center_uses_safe_keys_and_previews_only(self) -> None:
        raw_account_id = "1234567890123456"
        account_key = http_receiver._local_identity_key("qianchuan_account_id", raw_account_id)
        oauth_status = {
            "connected": True,
            "refresh_token_expires_at": int(time.time()) + 30 * 24 * 60 * 60,
            "accounts": [{
                "account_id": raw_account_id,
                "account_name": "测试主账户",
                "account_role": "SHOP",
                "valid": True,
                "advertiser_count": 1,
            }],
            "secrets_exposed": False,
        }
        sync_status = {
            "synced_at": int(time.time()),
            "accounts": [{
                "account_key": account_key,
                "advertiser_count": 1,
                "endpoints": [
                    {"name": "关联广告账户", "ok": True, "count": 1},
                    {"name": "直播计划", "ok": True, "count": 2},
                    {"name": "直播经营报表", "ok": True, "count": 2},
                    {"name": "素材投放报表", "ok": True, "count": 4},
                ],
            }],
        }
        store_catalog = {
            "selected_account_key": account_key,
            "stores": [{"key": "store_v1_safe", "label": "测试店铺", "account_keys": [account_key]}],
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"

        def post(path: str, payload: dict) -> dict:
            response = urllib.request.urlopen(
                urllib.request.Request(
                    f"{base_url}{path}",
                    data=json.dumps(payload).encode("utf-8"),
                    headers=self._internal_http_headers(),
                    method="POST",
                )
            )
            return json.loads(response.read())

        try:
            with (
                patch.object(http_receiver.OceanEngineOAuth, "status", return_value=oauth_status),
                patch.object(http_receiver, "load_sync_status", return_value=sync_status),
                patch.object(http_receiver, "build_store_catalog", return_value=store_catalog),
            ):
                raw_response = self._internal_http_get(f"{base_url}/oauth/oceanengine/account-center").read()
                center = json.loads(raw_response)
                self.assertEqual(center["accounts"][0]["account_key"], account_key)
                self.assertNotIn(raw_account_id.encode("utf-8"), raw_response)
                self.assertFalse(center["platform_write_enabled"])

                saved = post("/oauth/oceanengine/account-center/preferences", {
                    "account_key": account_key,
                    "alias": "核心直播账户",
                    "group_name": "直播组",
                    "sync_enabled": True,
                    "managed": True,
                })
                self.assertTrue(saved["preference"]["managed"])
                preference_file = http_receiver.DATA_DIR / "oceanengine_account_center.json"
                self.assertNotIn(raw_account_id, preference_file.read_text(encoding="utf-8"))

                preview = post("/oauth/oceanengine/account-center/preview", {
                    "account_keys": [account_key],
                    "action": "budget_decrease",
                })["preview"]
                self.assertEqual(preview["eligible_count"], 1)
                self.assertEqual(preview["execution_kind"], "preview_only")
                self.assertFalse(preview["platform_write_enabled"])
                self.assertFalse(preview["automatic_submit"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_storage_lifecycle_is_read_only_and_does_not_expose_local_paths(self) -> None:
        for captured_at in (1000, 2000):
            http_receiver.save_data(
                "qianchuan",
                {
                    "page_type": "campaigns",
                    "captured_at": captured_at,
                    "safe_metrics": {"roi": 2.5},
                    "promotion_context": {
                        "evidence": {"captured_at_ms": captured_at},
                        "promotion_mode_evidence": {"captured_at_ms": captured_at},
                    },
                },
                trusted_origin="official_api_oauth_client",
            )
        before_rows = len(list(http_receiver._local_store().iter_snapshots()))
        before_backups = list((http_receiver.DATA_DIR.parent / "backup").glob("shop-*.db"))
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            raw = self._internal_http_get(
                f"http://127.0.0.1:{server.server_port}/storage/lifecycle"
            ).read()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        preview = json.loads(raw)
        self.assertEqual("preview_only", preview["mode"])
        self.assertFalse(preview["mutates_data"])
        self.assertFalse(preview["automatic_cleanup_enabled"])
        self.assertFalse(preview["apply_contract"]["implemented"])
        self.assertEqual(2, preview["snapshot_rows"])
        self.assertEqual("campaigns.json", preview["hotspots"][0]["source_name"])
        serialized = json.dumps(preview, ensure_ascii=False)
        self.assertNotIn(str(http_receiver.DATA_DIR.parent), serialized)
        self.assertNotIn("source_path", serialized)
        self.assertEqual(before_rows, len(list(http_receiver._local_store().iter_snapshots())))
        self.assertEqual(
            before_backups,
            list((http_receiver.DATA_DIR.parent / "backup").glob("shop-*.db")),
        )

    @unittest.skipUnless(http_receiver.COMMERCIAL_RUNTIME_LOADED, "commercial runtime required")
    def test_http_chengfang_shadow_loop_persists_candidates_and_stops_safely(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"

        def post(path: str, payload: dict) -> dict:
            response = urllib.request.urlopen(
                urllib.request.Request(
                    f"{base_url}{path}",
                    data=json.dumps(payload).encode("utf-8"),
                    headers=self._internal_http_headers(),
                    method="POST",
                )
            )
            return json.loads(response.read())

        try:
            with patch.object(http_receiver, "_current_promotion_context", return_value=complete_chengfang_context()):
                profile = post("/chengfang/autopilot/profile", {"profile": complete_chengfang_profile()})
                self.assertTrue(profile["saved"]["validation"]["ready"])

                started = post("/chengfang/autopilot/shadow", {"enabled": True})
                self.assertTrue(started["autopilot_runtime"]["decision_automation"]["running"])
                self.assertEqual("reduce_total_budget_candidate", started["evaluation"]["status"])
                self.assertFalse(started["execution_allowed"])
                self.assertFalse(started["platform_write_attempted"])

                status = json.loads(self._internal_http_get(f"{base_url}/chengfang/autopilot").read())
                self.assertEqual(1, status["candidate_count"])
                self.assertEqual(900, status["candidates"][0]["target_value"])
                self.assertFalse(status["write_automation"]["execution_allowed"])

                stopped = post("/chengfang/autopilot/stop", {"confirm": True})
                self.assertFalse(stopped["autopilot_runtime"]["decision_automation"]["running"])
                self.assertEqual(0, stopped["autopilot_runtime"]["candidate_count"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    @unittest.skipUnless(http_receiver.COMMERCIAL_RUNTIME_LOADED, "commercial runtime required")
    def test_http_chengfang_a2_simulation_is_whitelisted_bounded_and_read_back(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"

        def post(path: str, payload: dict) -> dict:
            response = urllib.request.urlopen(
                urllib.request.Request(
                    f"{base_url}{path}",
                    data=json.dumps(payload).encode("utf-8"),
                    headers=self._internal_http_headers(),
                    method="POST",
                )
            )
            return json.loads(response.read())

        try:
            with patch.object(http_receiver, "_current_promotion_context", return_value=complete_chengfang_context()):
                post("/chengfang/autopilot/profile", {"profile": complete_chengfang_profile()})
                started = post("/chengfang/autopilot/shadow", {"enabled": True})
                candidate_id = started["evaluation"]["candidate_id"]
                plan_key = started["autopilot_runtime"]["a2_pilot"]["plan_key"]
                configured = post("/chengfang/a2-pilot/configure", {
                    "master_enabled": True,
                    "execution_kind": "simulation",
                    "plan_whitelist": [plan_key],
                    "budget_cap": 1000,
                    "confirm": True,
                })
                self.assertTrue(configured["a2_pilot"]["master_enabled"])
                self.assertFalse(configured["a2_pilot"]["platform_write_enabled"])
                post("/chengfang/a2-pilot/candidate/review", {
                    "candidate_id": candidate_id,
                    "decision": "accept",
                })
                executed = post("/chengfang/a2-pilot/candidate/execute", {"candidate_id": candidate_id})
                self.assertEqual("verified", executed["execution"]["state"])
                self.assertEqual("simulation", executed["execution"]["execution_kind"])
                self.assertFalse(executed["execution"]["platform_write_attempted"])
                status = json.loads(self._internal_http_get(f"{base_url}/chengfang/a2-pilot").read())
                self.assertEqual(1, status["quota"]["daily_action_count"])
                self.assertFalse(status["evidence_gate"]["production_ready"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_chengfang_auto_readback_requires_a_json_boolean(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        runtime = MagicMock()
        runtime.execute_candidate.return_value = {"execution": {"state": "pending_readback"}}

        def request(payload: dict):
            return urllib.request.urlopen(urllib.request.Request(
                f"{base_url}/chengfang/a2-pilot/candidate/execute",
                data=json.dumps(payload).encode("utf-8"),
                headers=self._internal_http_headers(),
                method="POST",
            ))

        try:
            with patch.object(http_receiver, "_current_chengfang_runtime", return_value=runtime):
                for invalid in ("false", 0, 1, None):
                    with self.subTest(invalid=invalid), self.assertRaises(urllib.error.HTTPError) as raised:
                        request({"candidate_id": "candidate-safe", "auto_readback": invalid})
                    self.assertEqual(400, raised.exception.code)
                    payload = json.loads(raised.exception.read())
                    self.assertEqual("INVALID_REQUEST_PARAMETER", payload["error"])
                    self.assertEqual("auto_readback", payload["parameter"])
                runtime.execute_candidate.assert_not_called()

                response = json.loads(request({
                    "candidate_id": "candidate-safe",
                    "auto_readback": False,
                }).read())
                self.assertEqual("pending_readback", response["execution"]["state"])
                runtime.execute_candidate.assert_called_once_with(
                    "candidate-safe", auto_readback=False
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    @unittest.skipUnless(http_receiver.COMMERCIAL_RUNTIME_LOADED, "commercial runtime required")
    def test_http_chengfang_control_task_center_imports_reviews_simulates_and_reads_back(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"

        def post(path: str, payload: dict) -> dict:
            response = urllib.request.urlopen(
                urllib.request.Request(
                    f"{base_url}{path}",
                    data=json.dumps(payload).encode("utf-8"),
                    headers=self._internal_http_headers(),
                    method="POST",
                )
            )
            return json.loads(response.read())

        try:
            with patch.object(http_receiver, "_current_promotion_context", return_value=complete_chengfang_context()):
                post("/chengfang/autopilot/profile", {"profile": complete_chengfang_profile()})
                started = post("/chengfang/autopilot/shadow", {"enabled": True})
                candidate_id = started["evaluation"]["candidate_id"]

                initial = json.loads(self._internal_http_get(f"{base_url}/chengfang/control-tasks").read())
                self.assertEqual(1, initial["importable_candidate_count"])
                self.assertFalse(initial["platform_write_enabled"])
                self.assertEqual(["status", "budget", "duration"], [item["id"] for item in initial["families"]])

                created = post("/chengfang/control-tasks/draft", {"candidate_id": candidate_id})
                task_id = created["task"]["task_id"]
                self.assertEqual("budget", created["task"]["family"])
                self.assertEqual("review_required", created["task"]["state"])
                self.assertFalse(created["task"]["production_allowed"])

                reviewed = post("/chengfang/control-tasks/review", {
                    "task_id": task_id, "decision": "accept", "confirm": True,
                })
                self.assertEqual("approved", reviewed["task"]["state"])
                simulated = post("/chengfang/control-tasks/simulate", {"task_id": task_id})
                self.assertEqual("awaiting_readback", simulated["task"]["state"])
                self.assertFalse(simulated["task"]["platform_write_attempted"])
                readback = post("/chengfang/control-tasks/readback", {"task_id": task_id})
                self.assertEqual("verified", readback["task"]["state"])
                self.assertTrue(readback["task"]["readback"]["matched"])
                self.assertFalse(readback["task"]["readback"]["platform_write_observed"])

                final = json.loads(self._internal_http_get(f"{base_url}/chengfang/control-tasks").read())
                self.assertEqual(1, final["verified_count"])
                self.assertTrue(final["single_variable_only"])

                status_draft = post("/chengfang/control-tasks/draft", {
                    "use_current_plan": True,
                    "family": "status",
                    "operation": "PAUSE",
                    "current_value": "ACTIVE",
                    "target_value": "PAUSED",
                })["task"]
                self.assertEqual("review_required", status_draft["state"])
                self.assertEqual({"status": "PAUSED"}, status_draft["change"])
                self.assertIn("STATUS_WRITE_CONTRACT_NOT_ACCOUNT_VERIFIED", status_draft["production_blockers"])

                duration_draft = post("/chengfang/control-tasks/draft", {
                    "use_current_plan": True,
                    "family": "duration",
                    "operation": "EXTEND",
                    "current_value": 60,
                    "target_value": 90,
                })["task"]
                self.assertEqual("review_required", duration_draft["state"])
                self.assertEqual({"duration": 90}, duration_draft["change"])
                self.assertIn("DURATION_WRITE_CONTRACT_UNVERIFIED", duration_draft["production_blockers"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_chengfang_schedule_control_is_local_versioned_and_read_back(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"

        def post(path: str, payload: dict) -> dict:
            response = urllib.request.urlopen(
                urllib.request.Request(
                    f"{base_url}{path}",
                    data=json.dumps(payload).encode("utf-8"),
                    headers=self._internal_http_headers(),
                    method="POST",
                )
            )
            return json.loads(response.read())

        try:
            with patch.object(http_receiver, "_current_promotion_context", return_value=complete_chengfang_context()):
                initial = json.loads(self._internal_http_get(f"{base_url}/chengfang/schedule-control").read())
                self.assertEqual("Asia/Shanghai", initial["timezone"])
                self.assertFalse(initial["platform_write_enabled"])

                created = post("/chengfang/schedule-control/draft", {
                    "use_current_plan": True,
                    "enabled": True,
                    "time_ranges": [
                        {"start": "09:00", "end": "12:00"},
                        {"start": "18:00", "end": "22:00"},
                    ],
                })
                if not http_receiver.COMMERCIAL_RUNTIME_LOADED:
                    self.assertFalse(initial["scope_bound"])
                    self.assertEqual("blocked", created["revision"]["state"])
                    self.assertFalse(created["revision"]["simulation_allowed"])
                    self.assertTrue({
                        "SCOPE_BINDING_INCOMPLETE",
                        "PLAN_SCOPE_REQUIRED",
                    }.issubset(set(created["revision"]["validation"]["blockers"])))
                    return

                self.assertTrue(initial["scope_bound"])
                revision_id = created["revision"]["revision_id"]
                self.assertEqual("review_required", created["revision"]["state"])
                self.assertFalse(created["revision"]["production_allowed"])
                reviewed = post("/chengfang/schedule-control/review", {
                    "revision_id": revision_id, "decision": "accept", "confirm": True,
                })
                self.assertEqual("approved", reviewed["revision"]["state"])
                simulated = post("/chengfang/schedule-control/simulate", {"revision_id": revision_id})
                self.assertEqual("awaiting_readback", simulated["revision"]["state"])
                self.assertFalse(simulated["revision"]["simulation_receipt"]["platform_write_attempted"])
                verified = post("/chengfang/schedule-control/readback", {"revision_id": revision_id})
                self.assertEqual("verified", verified["revision"]["state"])
                self.assertTrue(verified["schedule_control"]["local_monitor_enabled"])
                self.assertFalse(verified["schedule_control"]["production_scheduler_enabled"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_chengfang_one_click_demo_is_confirmed_synthetic_and_runtime_isolated(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"

        def request(payload: dict) -> dict:
            response = urllib.request.urlopen(
                urllib.request.Request(
                    f"{base_url}/chengfang/a2-pilot/demo",
                    data=json.dumps(payload).encode("utf-8"),
                    headers=self._internal_http_headers(),
                    method="POST",
                )
            )
            return json.loads(response.read())

        try:
            with patch.object(
                http_receiver,
                "_current_chengfang_runtime",
                side_effect=AssertionError("synthetic demo must not open a real runtime"),
            ):
                result = request({"confirm": True, "environment": "demo"})
                self.assertTrue(result["ok"])
                self.assertTrue(result["synthetic"])
                self.assertTrue(result["demo_fixture"])
                self.assertFalse(result["platform_write_attempted"])
                self.assertFalse(result["runtime_persisted"])
                execution = result["runtime"]["a2_pilot"]["executions"][0]
                self.assertEqual("verified", execution["state"])
                self.assertFalse(execution["platform_write_attempted"])
                self.assertTrue(execution["readback"]["matched"])

                with self.assertRaises(urllib.error.HTTPError) as missing_confirmation:
                    request({"confirm": False, "environment": "demo"})
                self.assertEqual(400, missing_confirmation.exception.code)
                error = json.loads(missing_confirmation.exception.read())
                self.assertIn("confirm 必须为 true", error["error"])

                with self.assertRaises(urllib.error.HTTPError) as production_attempt:
                    request({"confirm": True, "environment": "production"})
                self.assertEqual(400, production_attempt.exception.code)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_feedback_supports_deferred_without_distorting_helpful_rate(self) -> None:
        http_receiver.save_feedback("task-1", "up", context="建议 A")
        http_receiver.save_feedback("task-2", "defer", context="建议 B")
        stats = http_receiver.get_feedback_stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["deferred"], 1)
        self.assertEqual(stats["helpful_rate"], 100.0)

    def test_http_action_confirmation_records_but_never_executes(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        now_ms = int(time.time() * 1000)
        draft = http_receiver.build_action_draft(
            operation_type="adjust_budget",
            operation_label="降低预算 20%",
            target_kind="qianchuan_plan",
            target_id="plan_123456",
            target_name="接口测试计划",
            account_key="acct_safe1234",
            account_label="测试账号",
            field="预算",
            current_value=500,
            target_value=400,
            source="qianchuan",
            page_type="campaigns",
            captured_at_ms=now_ms,
            quality_score=90,
            confidence="high",
            now_ms=now_ms,
        )

        def post(path: str, payload: dict) -> dict:
            response = urllib.request.urlopen(
                urllib.request.Request(
                    f"{base_url}{path}",
                    data=json.dumps(payload).encode("utf-8"),
                    headers=self._internal_http_headers(),
                    method="POST",
                )
            )
            return json.loads(response.read())

        try:
            forged = json.loads(json.dumps(draft))
            forged["promotion_context"] = {
                "promotion_mode": "standard",
                "account_scope": {"store_id": "store-safe", "account_id": "acct_other1234"},
                "strategy_id": "strategy-safe",
                "metric_contract": {"definition": "pay_roi", "version": "v1"},
                "data_quality": {"confidence": "high", "freshness_seconds": 0, "completeness": 0.9},
            }
            forged["blocked_reasons"] = []
            forged["can_confirm"] = True
            forged_hash = action_integrity_hash(forged)
            forged["integrity_hash"] = forged_hash
            forged["action_id"] = forged_hash[:24]
            forged["idempotency_key"] = f"dian-action-{forged_hash[:32]}"
            with self.assertRaises(urllib.error.HTTPError) as cross_account:
                post("/actions/confirm", {"action": forged})
            self.assertEqual(400, cross_account.exception.code)
            error = json.loads(cross_account.exception.read())
            self.assertIn("当前本地 Agent 签发", error["error"])

            confirmed = post("/actions/confirm", {"action": draft})
            self.assertTrue(confirmed["ok"])
            self.assertFalse(confirmed["executed"])
            self.assertFalse(confirmed["execution_enabled"])
            audit = json.loads(self._internal_http_get(f"{base_url}/actions/audit").read())
            self.assertEqual(audit["summary"]["confirmed"], 1)
            self.assertEqual(audit["summary"]["executed"], 0)

            cancelled = post("/actions/cancel", {"action_id": draft["action_id"]})
            self.assertEqual(cancelled["action"]["state"], "cancelled")
            self.assertFalse(cancelled["executed"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_operation_context_requires_all_daily_core_pages(self) -> None:
        now = int(time.time())
        catalog = {
            "selected_store_key": "store_daily_core",
            "selected_account_key": "acct_daily_core",
            "stores": [{
                "key": "store_daily_core",
                "label": "测试店铺",
                "state": "browser_only",
                "state_label": "网页数据",
                "channel": "browser_multi",
                "updated_at": now,
                "page_count": 5,
                "qianchuan_page_count": 1,
            }],
        }
        receipt = {
            "store_key": "store_daily_core",
            "account_key": "acct_daily_core",
            "finished_at": now,
            "analysis_ready": True,
            "summary": {"coverage_rate": 100},
        }
        snapshots = [
            {
                "source": "doudian", "page_type": page_type, "quality_score": 90,
                "age_seconds": 60, "timestamp_conflict": False,
            }
            for page_type in ["overview", "orders", "products", "shelf"]
        ]
        with patch.object(http_receiver, "list_snapshots", return_value=snapshots):
            ready = http_receiver.build_operation_context(catalog=catalog, receipt=receipt)
        self.assertEqual("ready", ready["state"])
        self.assertEqual("ready", ready["core_data"]["status"])

        snapshots[1] = {**snapshots[1], "age_seconds": 3 * 24 * 60 * 60}
        with patch.object(http_receiver, "list_snapshots", return_value=snapshots):
            review = http_receiver.build_operation_context(catalog=catalog, receipt=receipt)
        self.assertEqual("review", review["state"])
        self.assertEqual(["orders"], review["core_data"]["stale_types"])
        self.assertTrue(any("orders" in item for item in review["warnings"]))
        self.assertFalse(review["execution_review_allowed"])


if __name__ == "__main__":
    unittest.main()
