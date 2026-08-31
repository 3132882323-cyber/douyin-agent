from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import http_receiver


class AIHTTPIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_dir = http_receiver.DATA_DIR
        self._original_gateway_cache = http_receiver._ai_gateway_cache
        self._original_context_cache = dict(http_receiver._ai_context_cache)
        self._original_call_history = dict(http_receiver._ai_call_history)
        self._temp = tempfile.TemporaryDirectory()
        http_receiver.DATA_DIR = Path(self._temp.name) / "data"
        http_receiver.DATA_DIR.mkdir(parents=True, exist_ok=True)
        http_receiver._ai_gateway_cache = None
        http_receiver._ai_context_cache.clear()
        http_receiver._ai_call_history.clear()

    def tearDown(self) -> None:
        http_receiver.DATA_DIR = self._original_dir
        http_receiver._ai_gateway_cache = self._original_gateway_cache
        http_receiver._ai_context_cache.clear()
        http_receiver._ai_context_cache.update(self._original_context_cache)
        http_receiver._ai_call_history.clear()
        http_receiver._ai_call_history.update(self._original_call_history)
        self._temp.cleanup()

    @staticmethod
    def _settings() -> dict:
        return {
            **http_receiver.DEFAULT_AGENT_SETTINGS,
            "store_key": "store_v1_bound_scope",
            "qianchuan_account_key": "adacct_v1_bound_scope",
        }

    @staticmethod
    def _plan_console() -> dict:
        return {
            "rows": [
                {
                    "plan_id": "987654321",
                    "plan_name": "绝不能发送的真实计划名称",
                    "account_key": "adacct_v1_bound_scope",
                    "plan_type": "live",
                    "delivery_status": "投放中",
                    "captured_at_ms": int(time.time() * 1000),
                    "quality_score": 92,
                    "stale": False,
                    "budget": 1000,
                    "spend": 260,
                    "roi": 1.76,
                    "orders": 18,
                    "ctr": 3.2,
                    "cookie": "session-secret-must-never-leave",
                }
            ]
        }

    @staticmethod
    def _proposal(context: dict) -> dict:
        evidence = context["evidence"][0]
        return {
            "schema_version": 1,
            "context_id": context["context_id"],
            "account_ref": context["identity"]["account_ref"],
            "plan_id": evidence["plan_id"],
            "action": "hold",
            "delta_percent": None,
            "evidence_refs": [evidence["evidence_ref"]],
            "snapshot_hash": evidence["snapshot_hash"],
            "confidence": 0.9,
            "reason_codes": ["risk_guardrail"],
            "observe_minutes": 30,
            "rollback_condition": "执行前必须重新读取当前计划。",
            "created_at_ms": int(time.time() * 1000),
            "ttl_seconds": 120,
        }

    def test_context_wrapper_projects_only_anonymous_aggregate_plan_data(self) -> None:
        with (
            patch.object(http_receiver, "load_agent_settings", return_value=self._settings()),
            patch.object(http_receiver, "build_qianchuan_plan_console", return_value=self._plan_console()),
        ):
            context = http_receiver.get_ai_context_pack()
            preview = http_receiver.build_ai_context_preview()

        rendered = json.dumps(context, ensure_ascii=False)
        self.assertNotIn("真实计划名称", rendered)
        self.assertNotIn("session-secret", rendered)
        self.assertNotIn("store_v1_bound_scope", rendered)
        self.assertNotIn("adacct_v1_bound_scope", rendered)
        self.assertEqual(1, len(context["evidence"]))
        self.assertEqual(1.76, context["evidence"][0]["metrics"]["roi"])
        self.assertTrue(preview["ready"])
        self.assertFalse(preview["execution_allowed"])
        self.assertFalse(preview["secrets_exposed"])

    def test_local_provider_can_be_configured_without_network_or_secret(self) -> None:
        result = http_receiver.configure_ai_provider({
            "provider": "local",
            "model": "qwen3:8b",
            "base_url": "http://127.0.0.1:1234/v1",
            "remote_access_approved": False,
        })

        self.assertTrue(result["ok"])
        self.assertEqual("local", result["provider"])
        self.assertTrue(result["analysis_available"])
        self.assertFalse(result["network_checked"])
        self.assertFalse(result["execution_allowed"])

    def test_exact_cached_context_can_be_submitted_after_mcp_style_get(self) -> None:
        with (
            patch.object(http_receiver, "load_agent_settings", return_value=self._settings()),
            patch.object(http_receiver, "build_qianchuan_plan_console", return_value=self._plan_console()),
        ):
            context = http_receiver.get_ai_context_pack()
            result = http_receiver.submit_ai_proposal(
                self._proposal(context),
                provider_id="mcp",
                model="external-model",
                context_hash=context["context_hash"],
            )

        self.assertEqual(context["context_hash"], result["context_hash"])
        self.assertTrue(result["eligible_for_human_review"])
        self.assertFalse(result["can_execute"])
        self.assertFalse(result["execution_allowed"])

    def test_cached_context_is_rejected_when_current_plan_data_changes(self) -> None:
        settings = self._settings()
        original = self._plan_console()
        changed = self._plan_console()
        changed["rows"][0]["roi"] = 0.42
        with (
            patch.object(http_receiver, "load_agent_settings", return_value=settings),
            patch.object(http_receiver, "build_qianchuan_plan_console", return_value=original),
        ):
            context = http_receiver.get_ai_context_pack()
        with (
            patch.object(http_receiver, "load_agent_settings", return_value=settings),
            patch.object(http_receiver, "build_qianchuan_plan_console", return_value=changed),
        ):
            with self.assertRaisesRegex(ValueError, "经营数据或账户已变化"):
                http_receiver.submit_ai_proposal(
                    self._proposal(context),
                    context_hash=context["context_hash"],
                )

    def test_user_can_disable_every_ai_connection_without_platform_write(self) -> None:
        http_receiver.configure_ai_provider({
            "provider": "local",
            "model": "qwen3:8b",
            "base_url": "http://127.0.0.1:1234/v1",
            "remote_access_approved": False,
        })

        with self.assertRaisesRegex(ValueError, "confirm"):
            http_receiver.disable_all_ai_providers({})
        result = http_receiver.disable_all_ai_providers({"confirm": True})

        self.assertIn("local", result["disabled_providers"])
        self.assertFalse(result["analysis_available"])
        self.assertFalse(result["execution_allowed"])
        self.assertFalse(any(item["enabled"] for item in result["providers"]))

    def test_last_selected_provider_survives_reload_and_drives_shadow_analysis(self) -> None:
        http_receiver.configure_ai_provider({
            "provider": "local",
            "model": "local-model",
            "base_url": "http://127.0.0.1:1234/v1",
        })
        http_receiver.configure_ai_provider({
            "provider": "ollama",
            "model": "qwen3:8b",
            "base_url": "http://127.0.0.1:11434",
        })
        http_receiver._ai_gateway_cache = None

        status = http_receiver.get_ai_status()
        self.assertEqual("ollama", status["provider_id"])
        self.assertEqual("ollama", status["active_provider_id"])

        context = None
        with (
            patch.object(http_receiver, "load_agent_settings", return_value=self._settings()),
            patch.object(http_receiver, "build_qianchuan_plan_console", return_value=self._plan_console()),
        ):
            context = http_receiver.get_ai_context_pack()
        gateway = Mock()
        gateway.run_shadow.return_value = {
            "proposal_id": "shadow-1",
            "eligible_for_human_review": False,
            "proposal_candidates": [],
            "validation_errors": [],
            "created_at_ms": int(time.time() * 1000),
        }
        with (
            patch.object(http_receiver, "load_agent_settings", return_value=self._settings()),
            patch.object(http_receiver, "get_ai_context_pack", return_value=context),
            patch.object(http_receiver, "get_ai_status", return_value=status),
            patch.object(http_receiver, "_ai_gateway", return_value=gateway),
        ):
            result = http_receiver.run_ai_shadow({})

        self.assertTrue(result["ok"])
        self.assertEqual("ollama", gateway.run_shadow.call_args.kwargs["provider_id"])

    def test_remote_provider_requires_explicit_data_transfer_consent(self) -> None:
        gateway = Mock()
        with patch.object(http_receiver, "_ai_gateway", return_value=gateway):
            with self.assertRaisesRegex(ValueError, "勾选同意"):
                http_receiver.configure_ai_provider({
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "api_key": "not-written-provider-key",
                })
        gateway.configure.assert_not_called()

    def test_qwen_bailian_requires_consent(self) -> None:
        gateway = Mock()
        with patch.object(http_receiver, "_ai_gateway", return_value=gateway):
            with self.assertRaisesRegex(ValueError, "勾选同意"):
                http_receiver.configure_ai_provider({
                    "provider": "qwen_bailian",
                    "model": "qwen-plus",
                    "api_key": "not-written-bailian-key",
                })
        gateway.configure.assert_not_called()

    def test_all_chinese_cloud_presets_require_consent(self) -> None:
        gateway = Mock()
        providers = (
            ("glm_zhipu", "glm-5.2"),
            ("hunyuan_tencent", "hy3"),
            ("doubao_ark", "doubao-seed-2-0-lite-260215"),
        )
        with patch.object(http_receiver, "_ai_gateway", return_value=gateway):
            for provider, model in providers:
                with self.subTest(provider=provider):
                    with self.assertRaisesRegex(ValueError, "勾选同意"):
                        http_receiver.configure_ai_provider({
                            "provider": provider,
                            "model": model,
                            "api_key": "must-not-be-used-without-consent",
                        })
        gateway.configure.assert_not_called()

    def test_chinese_provider_aliases_are_canonicalized_before_configuration(self) -> None:
        providers = (
            ("glm", "glm_zhipu", "glm-5.2"),
            ("hunyuan", "hunyuan_tencent", "hy3"),
            ("doubao", "doubao_ark", "doubao-seed-2-0-lite-260215"),
        )
        for alias, canonical, model in providers:
            with self.subTest(alias=alias):
                gateway = Mock()
                gateway.configure.return_value = {"provider_id": canonical}
                status = {
                    "provider": canonical,
                    "provider_id": canonical,
                    "analysis_available": True,
                    "execution_allowed": False,
                }
                with (
                    patch.object(http_receiver, "_ai_gateway", return_value=gateway),
                    patch.object(http_receiver, "_write_active_ai_provider") as write_active,
                    patch.object(http_receiver, "get_ai_status", return_value=status),
                ):
                    result = http_receiver.configure_ai_provider({
                        "provider": alias,
                        "model": model,
                        "api_key": "provider-specific-test-key",
                        "remote_access_approved": True,
                    })

                self.assertEqual(canonical, result["provider_id"])
                self.assertFalse(result["execution_allowed"])
                self.assertEqual(canonical, gateway.configure.call_args.args[0])
                write_active.assert_called_once_with(canonical)

    def test_qwen_alias_is_canonicalized_before_configuration(self) -> None:
        gateway = Mock()
        gateway.configure.return_value = {"provider_id": "qwen_bailian"}
        status = {
            "provider": "qwen_bailian",
            "provider_id": "qwen_bailian",
            "analysis_available": True,
            "execution_allowed": False,
        }
        with (
            patch.object(http_receiver, "_ai_gateway", return_value=gateway),
            patch.object(http_receiver, "_write_active_ai_provider") as write_active,
            patch.object(http_receiver, "get_ai_status", return_value=status),
        ):
            result = http_receiver.configure_ai_provider({
                "provider": "bailian",
                "model": "qwen-plus",
                "api_key": "not-written-bailian-key",
                "remote_access_approved": True,
            })

        self.assertEqual("qwen_bailian", result["provider_id"])
        self.assertFalse(result["execution_allowed"])
        self.assertEqual("qwen_bailian", gateway.configure.call_args.args[0])
        self.assertTrue(gateway.configure.call_args.kwargs["remote_access_approved"])
        write_active.assert_called_once_with("qwen_bailian")

    def test_ephemeral_connection_test_is_forwarded_without_execution_capability(self) -> None:
        gateway = Mock()
        gateway.test_configuration.return_value = {
            "ok": True,
            "network_checked": True,
            "latency_ms": 32,
            "probe_data_scope": "synthetic_only",
            "configuration_persisted": False,
            "credential_persisted": False,
            "proposal_persisted": False,
            "secrets_exposed": False,
        }
        with patch.object(http_receiver, "_ai_gateway", return_value=gateway):
            result = http_receiver.test_ai_provider({
                "provider": "deepseek",
                "model": "deepseek-chat",
                "api_key": "ephemeral-provider-key",
                "remote_access_approved": True,
            })

        self.assertTrue(result["network_checked"])
        self.assertEqual("synthetic_only", result["probe_data_scope"])
        self.assertFalse(result["execution_allowed"])
        self.assertFalse(result["platform_write_attempted"])
        forwarded = gateway.test_configuration.call_args.args[0]
        self.assertEqual("ephemeral-provider-key", forwarded["api_key"])

    def test_failed_connection_probe_is_not_reported_as_success(self) -> None:
        gateway = Mock()
        gateway.test_configuration.return_value = {
            "ok": False,
            "network_checked": True,
            "error": "Provider 已响应，但未正确支持结构化工具调用。",
            "secrets_exposed": False,
        }
        with patch.object(http_receiver, "_ai_gateway", return_value=gateway):
            with self.assertRaisesRegex(ValueError, "未正确支持"):
                http_receiver.test_ai_provider({
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "api_key": "ephemeral-provider-key",
                    "remote_access_approved": True,
                })

    def test_saved_remote_connection_can_be_retested_without_key_echo(self) -> None:
        gateway = Mock()
        gateway.registry.public_config.return_value = {
            "enabled": True,
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1",
        }
        gateway.test_provider.return_value = {
            "ok": True,
            "network_checked": True,
            "latency_ms": 18,
            "probe_data_scope": "synthetic_only",
            "secrets_exposed": False,
        }
        with patch.object(http_receiver, "_ai_gateway", return_value=gateway):
            result = http_receiver.test_ai_provider({
                "provider": "deepseek",
                "model": "deepseek-chat",
                "base_url": "https://api.deepseek.com/v1",
                "remote_access_approved": True,
            })

        self.assertTrue(result["network_checked"])
        gateway.test_provider.assert_called_once_with("deepseek")
        gateway.test_configuration.assert_not_called()

    def test_ai_mutation_routes_reject_unpaired_callers_before_reading_secrets(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/ai/providers/configure",
                data=json.dumps({
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "api_key": "must-not-be-read",
                    "remote_access_approved": True,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Dian-Agent": "2"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as captured:
                urllib.request.urlopen(request)
            self.assertEqual(403, captured.exception.code)
            response = json.loads(captured.exception.read())
            self.assertEqual("paired_extension_required", response["error"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_ai_read_routes_reject_unpaired_callers(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_receiver.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path in ("/ai/status", "/ai/context-preview", "/ai/proposals"):
                with self.subTest(path=path):
                    with self.assertRaises(urllib.error.HTTPError) as captured:
                        urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}{path}")
                    self.assertEqual(401, captured.exception.code)
                    response = json.loads(captured.exception.read())
                    self.assertEqual("agent_session_required", response["error"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
