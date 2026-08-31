from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import ai_provider
from ai_provider import (
    AIProviderConfigurationError,
    AIProviderRegistry,
    AIProviderRequestError,
    AIProviderResponseError,
    PROVIDER_SPECS,
    request_ephemeral_proposal,
    resolve_provider_id,
)


class _Response:
    def __init__(self, value: dict | None = None, *, raw: bytes | None = None, headers=None):
        self.raw = raw if raw is not None else json.dumps(value or {}).encode("utf-8")
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self.raw


class AIProviderRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def test_catalog_and_health_are_offline_and_execution_is_unsupported(self) -> None:
        registry = AIProviderRegistry(self.data_dir)
        catalog = registry.catalog()
        self.assertEqual("offline", catalog["default_network_mode"])
        self.assertFalse(catalog["execution_supported"])
        self.assertEqual(
            {
                "openai_responses",
                "deepseek",
                "qwen_bailian",
                "glm_zhipu",
                "hunyuan_tencent",
                "doubao_ark",
                "openai_compatible",
                "ollama",
                "local",
            },
            {row["provider_id"] for row in catalog["providers"]},
        )
        with patch.object(ai_provider, "urlopen") as network:
            health = registry.health()
        network.assert_not_called()
        self.assertFalse(health["network_checked"])
        self.assertEqual(0, health["ready_count"])
        self.assertTrue(all(row["state"] == "disabled" for row in health["providers"]))

    def test_qwen_bailian_is_in_catalog_and_all_public_aliases_resolve(self) -> None:
        spec = PROVIDER_SPECS["qwen_bailian"]
        self.assertEqual("openai_chat", spec.protocol)
        self.assertEqual(
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            spec.default_base_url,
        )
        self.assertTrue(spec.requires_api_key)
        self.assertFalse(spec.local_only)
        self.assertIn("DASHSCOPE_API_KEY", spec.environment_keys)
        self.assertIn("BAILIAN_API_KEY", spec.environment_keys)
        for alias in ("qwen_bailian", "qwen", "tongyi", "bailian", "dashscope"):
            with self.subTest(alias=alias):
                self.assertEqual("qwen_bailian", resolve_provider_id(alias))

    def test_glm_hunyuan_and_doubao_specs_and_public_aliases(self) -> None:
        cases = (
            (
                "glm_zhipu",
                "https://open.bigmodel.cn/api/paas/v4",
                "ZAI_API_KEY",
                ("glm_zhipu", "glm", "chatglm", "zhipu", "zhipuai", "bigmodel", "zai", "智谱"),
            ),
            (
                "hunyuan_tencent",
                "https://tokenhub.tencentmaas.com/v1",
                "TOKENHUB_API_KEY",
                (
                    "hunyuan_tencent",
                    "hunyuan",
                    "tencent_hunyuan",
                    "tencent-hunyuan",
                    "tokenhub",
                    "hy3",
                    "混元",
                ),
            ),
            (
                "doubao_ark",
                "https://ark.cn-beijing.volces.com/api/v3",
                "ARK_API_KEY",
                ("doubao_ark", "doubao", "ark", "volcengine", "volcengine_ark", "豆包", "火山方舟"),
            ),
        )
        for provider_id, base_url, environment_key, aliases in cases:
            with self.subTest(provider_id=provider_id):
                spec = PROVIDER_SPECS[provider_id]
                self.assertEqual("openai_chat", spec.protocol)
                self.assertEqual(base_url, spec.default_base_url)
                self.assertTrue(spec.requires_api_key)
                self.assertFalse(spec.local_only)
                self.assertIn(environment_key, spec.environment_keys)
                if provider_id == "hunyuan_tencent":
                    self.assertLess(
                        spec.environment_keys.index("TOKENHUB_API_KEY"),
                        spec.environment_keys.index("HUNYUAN_API_KEY"),
                    )
                    self.assertIn("TENCENT_HUNYUAN_API_KEY", spec.environment_keys)
                for alias in aliases:
                    self.assertEqual(provider_id, resolve_provider_id(alias))

    def test_glm_hunyuan_and_doubao_only_accept_exact_official_api_roots(self) -> None:
        cases = (
            (
                "glm_zhipu",
                "glm-5.2",
                "ZAI_API_KEY",
                "https://open.bigmodel.cn/api/paas/v4",
                (
                    "http://open.bigmodel.cn/api/paas/v4",
                    "https://open.bigmodel.cn:8443/api/paas/v4",
                    "https://open.bigmodel.cn.evil.example/api/paas/v4",
                    "https://open.bigmodel.cn/api/coding/paas/v4",
                ),
            ),
            (
                "hunyuan_tencent",
                "hy3",
                "TOKENHUB_API_KEY",
                "https://tokenhub.tencentmaas.com/v1",
                (
                    "http://tokenhub.tencentmaas.com/v1",
                    "https://tokenhub.tencentmaas.com:8443/v1",
                    "https://tokenhub.tencentmaas.com.evil.example/v1",
                    "https://tokenhub.tencentmaas.com/v2",
                    "https://api.hunyuan.cloud.tencent.com/v1",
                ),
            ),
            (
                "doubao_ark",
                "ep-safe-endpoint-id",
                "ARK_API_KEY",
                "https://ark.cn-beijing.volces.com/api/v3",
                (
                    "http://ark.cn-beijing.volces.com/api/v3",
                    "https://ark.cn-beijing.volces.com:8443/api/v3",
                    "https://ark.cn-beijing.volces.com.evil.example/api/v3",
                    "https://ark.cn-beijing.volces.com/api/v4",
                ),
            ),
        )
        for index, (provider_id, model, env_name, base_url, rejected) in enumerate(cases):
            with self.subTest(provider_id=provider_id), patch.dict(
                os.environ, {env_name: f"{provider_id}-environment-key"}, clear=True
            ):
                status = AIProviderRegistry(self.data_dir / f"accepted-{index}").configure(
                    provider_id,
                    enabled=True,
                    model=model,
                    base_url=base_url + "/",
                    remote_access_approved=True,
                )
                self.assertEqual("ready", status["providers"][0]["state"])
                for candidate in rejected:
                    with self.assertRaisesRegex(
                        AIProviderConfigurationError, "HTTPS"
                    ):
                        AIProviderRegistry(
                            self.data_dir / f"rejected-{index}"
                        ).configure(
                            provider_id,
                            enabled=True,
                            model=model,
                            base_url=candidate,
                            remote_access_approved=True,
                        )

    def test_glm_hunyuan_and_doubao_require_remote_authorization(self) -> None:
        for provider_id, model, env_name in (
            ("glm", "glm-5.2", "ZAI_API_KEY"),
            ("hunyuan", "hy3", "TOKENHUB_API_KEY"),
            ("doubao", "doubao-seed-2-0-lite-260215", "ARK_API_KEY"),
        ):
            with self.subTest(provider_id=provider_id), patch.dict(
                os.environ, {env_name: "provider-environment-key"}, clear=True
            ):
                with self.assertRaisesRegex(AIProviderConfigurationError, "明确授权"):
                    AIProviderRegistry(self.data_dir / provider_id).configure(
                        provider_id, enabled=True, model=model
                    )

    def test_hunyuan_tokenhub_regions_are_exact_and_environment_key_is_origin_bound(self) -> None:
        alternate_hosts = (
            "tokenhub-intl.tencentmaas.com",
            "tokenhub.tencentmaas.cn",
            "tokenhub-intl.tencentmaas.cn",
        )
        for index, host in enumerate(alternate_hosts):
            base_url = f"https://{host}/v1"
            with self.subTest(host=host), patch.dict(
                os.environ,
                {
                    "TOKENHUB_API_KEY": "tokenhub-environment-key",
                    "DIAN_AGENT_AI_HUNYUAN_TENCENT_ORIGIN": base_url,
                },
                clear=True,
            ):
                status = AIProviderRegistry(
                    self.data_dir / f"tokenhub-{index}"
                ).configure(
                    "hunyuan",
                    enabled=True,
                    model="hy3",
                    base_url=base_url,
                    remote_access_approved=True,
                )
                self.assertEqual("ready", status["providers"][0]["state"])

        alternate_url = "https://tokenhub-intl.tencentmaas.com/v1"
        with patch.dict(
            os.environ, {"TOKENHUB_API_KEY": "tokenhub-environment-key"}, clear=True
        ):
            status = AIProviderRegistry(self.data_dir / "tokenhub-unbound").configure(
                "hunyuan",
                enabled=True,
                model="hy3",
                base_url=alternate_url,
                remote_access_approved=True,
            )
        self.assertEqual("api_key_required", status["providers"][0]["state"])
        self.assertEqual("origin_mismatch", status["providers"][0]["credential"]["source"])

    def test_qwen_bailian_only_accepts_official_https_openai_compatible_hosts(self) -> None:
        accepted = (
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
            "https://ws-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "https://ws-123.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
            "https://ws-123.ap-northeast-1.maas.aliyuncs.com/compatible-mode/v1",
            "https://ws-123.eu-central-1.maas.aliyuncs.com/compatible-mode/v1",
            "https://ws-123.us-east-1.maas.aliyuncs.com/compatible-mode/v1",
        )
        for index, base_url in enumerate(accepted):
            with self.subTest(base_url=base_url):
                nested_dir = self.data_dir / str(index)
                with patch.dict(
                    os.environ,
                    {
                        "DASHSCOPE_API_KEY": "qwen-environment-key",
                        "DIAN_AGENT_AI_QWEN_BAILIAN_ORIGIN": base_url,
                    },
                    clear=True,
                ):
                    status = AIProviderRegistry(nested_dir).configure(
                        "qwen",
                        enabled=True,
                        model="qwen-plus",
                        base_url=base_url,
                        remote_access_approved=True,
                    )
                self.assertEqual("ready", status["providers"][0]["state"])

        rejected = (
            "http://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://dashscope.aliyuncs.com:8443/compatible-mode/v1",
            "https://dashscope.aliyuncs.com.evil.example/compatible-mode/v1",
            "https://workspace.invalid-region.maas.aliyuncs.com/compatible-mode/v1",
            "https://workspace.cn-beijing.maas.aliyuncs.com.evil.example/compatible-mode/v1",
            "https://dashscope.aliyuncs.com/api/v1",
        )
        for base_url in rejected:
            with self.subTest(base_url=base_url):
                with self.assertRaisesRegex(AIProviderConfigurationError, "官方 HTTPS"):
                    AIProviderRegistry(self.data_dir).configure(
                        "qwen_bailian",
                        enabled=True,
                        model="qwen-plus",
                        base_url=base_url,
                        remote_access_approved=True,
                    )

    def test_qwen_bailian_requires_explicit_remote_authorization(self) -> None:
        with patch.dict(
            os.environ,
            {"DASHSCOPE_API_KEY": "qwen-environment-key"},
            clear=True,
        ):
            with self.assertRaisesRegex(AIProviderConfigurationError, "明确授权"):
                AIProviderRegistry(self.data_dir).configure(
                    "qwen", enabled=True, model="qwen-plus"
                )

    def test_qwen_environment_keys_and_origin_binding(self) -> None:
        for env_name in ("DASHSCOPE_API_KEY", "BAILIAN_API_KEY"):
            with self.subTest(env_name=env_name), patch.dict(
                os.environ, {env_name: "qwen-environment-key"}, clear=True
            ):
                status = AIProviderRegistry(self.data_dir / env_name).configure(
                    "bailian",
                    enabled=True,
                    model="qwen-plus",
                    remote_access_approved=True,
                )
                self.assertEqual("ready", status["providers"][0]["state"])
                self.assertEqual(
                    "environment", status["providers"][0]["credential"]["source"]
                )

        workspace_url = (
            "https://workspace-1.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        )
        with patch.dict(
            os.environ, {"DASHSCOPE_API_KEY": "qwen-environment-key"}, clear=True
        ):
            status = AIProviderRegistry(self.data_dir / "unbound-workspace").configure(
                "qwen",
                enabled=True,
                model="qwen-plus",
                base_url=workspace_url,
                remote_access_approved=True,
            )
        self.assertEqual("api_key_required", status["providers"][0]["state"])
        self.assertEqual("origin_mismatch", status["providers"][0]["credential"]["source"])

    def test_qwen_secure_store_key_is_not_reused_for_another_official_origin(self) -> None:
        saved: dict = {}

        def store(path: Path, value: dict, _description: str) -> None:
            saved.clear()
            saved.update(copy.deepcopy(value))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"encrypted-record")

        with (
            patch.object(ai_provider.sys, "platform", "win32"),
            patch.object(ai_provider, "_store_encrypted", side_effect=store),
            patch.object(
                ai_provider,
                "_load_encrypted",
                side_effect=lambda _path: copy.deepcopy(saved),
            ),
        ):
            registry = AIProviderRegistry(self.data_dir)
            first = registry.configure(
                "qwen",
                enabled=True,
                model="qwen-plus",
                remote_access_approved=True,
                api_key="qwen-origin-bound-key",
            )
            changed = registry.configure(
                "qwen",
                enabled=True,
                model="qwen-plus",
                base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                remote_access_approved=True,
            )

        self.assertEqual("ready", first["providers"][0]["state"])
        self.assertEqual("api_key_required", changed["providers"][0]["state"])
        self.assertEqual("origin_mismatch", changed["providers"][0]["credential"]["source"])
        self.assertEqual(
            "https://dashscope.aliyuncs.com:443",
            saved["keys"]["qwen_bailian"]["origin"],
        )
        self.assertNotIn("qwen-origin-bound-key", json.dumps(first))

    def test_api_key_is_stored_separately_and_never_written_to_public_json(self) -> None:
        saved: dict = {}

        def store(path: Path, value: dict, _description: str) -> None:
            saved.update(copy.deepcopy(value))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"encrypted-record")

        with (
            patch.object(ai_provider.sys, "platform", "win32"),
            patch.object(ai_provider, "_store_encrypted", side_effect=store),
            patch.object(ai_provider, "_load_encrypted", side_effect=lambda _path: copy.deepcopy(saved)),
        ):
            registry = AIProviderRegistry(self.data_dir)
            health = registry.configure(
                "deepseek",
                enabled=True,
                model="deepseek-chat",
                remote_access_approved=True,
                api_key="super-secret-provider-key",
            )
            self.assertEqual("ready", health["providers"][0]["state"])
            self.assertEqual("secure_store", health["providers"][0]["credential"]["source"])

        public = self.data_dir.joinpath("ai_providers.json").read_text(encoding="utf-8")
        self.assertNotIn("super-secret-provider-key", public)
        self.assertNotIn("api_key", public.lower())
        self.assertNotIn("super-secret-provider-key", json.dumps(health))
        self.assertEqual("super-secret-provider-key", saved["keys"]["deepseek"]["api_key"])
        self.assertEqual("https://api.deepseek.com:443", saved["keys"]["deepseek"]["origin"])

    def test_key_and_public_config_update_rolls_back_as_one_transaction(self) -> None:
        saved: dict = {}

        def store(path: Path, value: dict, _description: str) -> None:
            saved.clear()
            saved.update(copy.deepcopy(value))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"encrypted-record")

        real_atomic_write = ai_provider._atomic_write_json
        with (
            patch.object(ai_provider.sys, "platform", "win32"),
            patch.object(ai_provider, "_store_encrypted", side_effect=store),
            patch.object(
                ai_provider,
                "_load_encrypted",
                side_effect=lambda _path: copy.deepcopy(saved),
            ),
        ):
            registry = AIProviderRegistry(self.data_dir)
            registry.configure(
                "deepseek",
                enabled=True,
                model="deepseek-chat",
                remote_access_approved=True,
                api_key="original-provider-key",
            )
            original_public = registry.config_path.read_bytes()
            original_secret = copy.deepcopy(saved)

            with patch.object(
                ai_provider,
                "_atomic_write_json",
                side_effect=OSError("pending write failed"),
            ):
                with self.assertRaisesRegex(OSError, "pending write failed"):
                    registry.configure(
                        "deepseek",
                        enabled=True,
                        model="deepseek-reasoner",
                        remote_access_approved=True,
                        api_key="replacement-provider-key",
                    )
            self.assertEqual(original_public, registry.config_path.read_bytes())
            self.assertEqual(original_secret, saved)

            write_count = 0

            def fail_final_write(path: Path, value: dict) -> None:
                nonlocal write_count
                write_count += 1
                if write_count == 2:
                    raise OSError("final write failed")
                real_atomic_write(path, value)

            with patch.object(
                ai_provider,
                "_atomic_write_json",
                side_effect=fail_final_write,
            ):
                with self.assertRaisesRegex(OSError, "final write failed"):
                    registry.configure(
                        "deepseek",
                        enabled=True,
                        model="deepseek-reasoner",
                        remote_access_approved=True,
                        api_key="replacement-provider-key",
                    )

            self.assertEqual(3, write_count)
            self.assertEqual(original_public, registry.config_path.read_bytes())
            self.assertEqual(original_secret, saved)
            key, source = registry.secrets.load_key(
                "deepseek", expected_origin="https://api.deepseek.com:443"
            )
            self.assertEqual("original-provider-key", key)
            self.assertEqual("secure_store", source)

    def test_non_windows_key_persistence_fails_closed_but_environment_is_supported(self) -> None:
        registry = AIProviderRegistry(self.data_dir)
        with patch.object(ai_provider.sys, "platform", "linux"):
            with self.assertRaisesRegex(AIProviderConfigurationError, "环境变量"):
                registry.configure(
                    "deepseek",
                    enabled=True,
                    model="deepseek-chat",
                    remote_access_approved=True,
                    api_key="cannot-persist-this-key",
                )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "environment-only-key"}, clear=True):
            registry.configure(
                "deepseek",
                enabled=True,
                model="deepseek-chat",
                remote_access_approved=True,
            )
            self.assertEqual("ready", registry.health("deepseek")["providers"][0]["state"])

    def test_remote_requires_https_and_explicit_approval_while_local_is_loopback_only(self) -> None:
        registry = AIProviderRegistry(self.data_dir)
        with self.assertRaisesRegex(AIProviderConfigurationError, "明确授权"):
            registry.configure("deepseek", enabled=True, model="deepseek-chat")
        with self.assertRaisesRegex(AIProviderConfigurationError, "HTTPS"):
            registry.configure(
                "openai_compatible",
                enabled=True,
                model="model-1",
                base_url="http://example.com/v1",
                remote_access_approved=True,
            )
        with self.assertRaisesRegex(AIProviderConfigurationError, "固定 HTTPS"):
            registry.configure(
                "deepseek",
                enabled=True,
                model="deepseek-chat",
                base_url="https://attacker.example/v1",
                remote_access_approved=True,
            )
        with self.assertRaisesRegex(AIProviderConfigurationError, "私网"):
            registry.configure(
                "openai_compatible",
                enabled=True,
                model="model-1",
                base_url="https://169.254.169.254/v1",
                remote_access_approved=True,
            )
        with self.assertRaisesRegex(AIProviderConfigurationError, "回环地址"):
            registry.configure(
                "ollama", enabled=True, model="qwen2.5:7b", base_url="https://example.com"
            )
        status = registry.configure("ollama", enabled=True, model="qwen2.5:7b")
        self.assertTrue(status["providers"][0]["ready"])

    def test_saved_key_is_never_reused_after_custom_provider_origin_changes(self) -> None:
        saved: dict = {}

        def store(path: Path, value: dict, _description: str) -> None:
            saved.clear()
            saved.update(copy.deepcopy(value))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"encrypted-record")

        with (
            patch.object(ai_provider.sys, "platform", "win32"),
            patch.object(ai_provider, "_store_encrypted", side_effect=store),
            patch.object(ai_provider, "_load_encrypted", side_effect=lambda _path: copy.deepcopy(saved)),
        ):
            registry = AIProviderRegistry(self.data_dir)
            first = registry.configure(
                "openai_compatible",
                enabled=True,
                model="custom-model",
                base_url="https://first.example/v1",
                remote_access_approved=True,
                api_key="origin-bound-provider-key",
            )
            changed = registry.configure(
                "openai_compatible",
                enabled=True,
                model="custom-model",
                base_url="https://second.example/v1",
                remote_access_approved=True,
            )

        self.assertEqual("ready", first["providers"][0]["state"])
        self.assertEqual("api_key_required", changed["providers"][0]["state"])
        self.assertEqual("origin_mismatch", changed["providers"][0]["credential"]["source"])

    def test_disabled_provider_never_opens_network(self) -> None:
        registry = AIProviderRegistry(self.data_dir)
        with patch.object(ai_provider, "urlopen") as network:
            with self.assertRaisesRegex(AIProviderConfigurationError, "尚未启用"):
                registry.client("deepseek").request_proposal(
                    context_pack={"roi": 1.2}, instruction="hold", proposal_schema=None
                )
        network.assert_not_called()


class AIProviderNetworkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.environment = patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "openai-environment-key",
                "DEEPSEEK_API_KEY": "deepseek-environment-key",
                "DASHSCOPE_API_KEY": "qwen-environment-key",
                "ZAI_API_KEY": "glm-environment-key",
                "TOKENHUB_API_KEY": "tokenhub-environment-key",
                "ARK_API_KEY": "doubao-environment-key",
            },
            clear=True,
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def _registry(self, provider_id: str, model: str) -> AIProviderRegistry:
        registry = AIProviderRegistry(self.data_dir)
        registry.configure(
            provider_id,
            enabled=True,
            model=model,
            remote_access_approved=True,
            timeout_seconds=7,
        )
        return registry

    @staticmethod
    def _arguments() -> dict:
        return {
            "account_key": "account-hash",
            "plan_id": "plan-1",
            "action": "hold",
            "delta_percent": 0,
            "evidence_refs": ["snapshot-1"],
            "snapshot_hash": "abc",
            "confidence": 0.9,
            "observe_minutes": 30,
            "rollback_condition": "none",
        }

    def test_openai_responses_function_call_is_parsed_as_untrusted_proposal(self) -> None:
        registry = self._registry("openai_responses", "gpt-test")
        response = {
            "id": "resp-1",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "submit_ai_proposal",
                    "call_id": "call-1",
                    "arguments": json.dumps(self._arguments()),
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

        def fake_urlopen(request, timeout):
            self.assertEqual(7, timeout)
            self.assertEqual("https://api.openai.com/v1/responses", request.full_url)
            self.assertEqual("Bearer openai-environment-key", request.get_header("Authorization"))
            payload = json.loads(request.data)
            self.assertEqual("submit_ai_proposal", payload["tools"][0]["name"])
            self.assertEqual(ai_provider.MAX_OUTPUT_TOKENS, payload["max_output_tokens"])
            return _Response(response)

        with patch.object(ai_provider, "urlopen", side_effect=fake_urlopen):
            result = registry.client("openai").request_proposal(
                context_pack={"roi": 1.1}, instruction="保持保守", proposal_schema=None
            )
        candidate = result["proposal_candidates"][0]
        self.assertEqual("plan-1", candidate["arguments"]["plan_id"])
        self.assertFalse(candidate["trusted"])
        self.assertFalse(candidate["executable"])
        self.assertFalse(result["execution_performed"])
        self.assertNotIn("openai-environment-key", json.dumps(result))

    def test_deepseek_openai_compatible_tool_call_is_parsed(self) -> None:
        registry = self._registry("deepseek", "deepseek-chat")
        response = {
            "id": "chat-1",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-2",
                                "function": {
                                    "name": "submit_ai_proposal",
                                    "arguments": json.dumps(self._arguments()),
                                },
                            }
                        ]
                    },
                }
            ],
        }
        def fake_urlopen(request, timeout):
            self.assertEqual(7, timeout)
            payload = json.loads(request.data)
            self.assertEqual(ai_provider.MAX_OUTPUT_TOKENS, payload["max_tokens"])
            return _Response(response)

        with patch.object(ai_provider, "urlopen", side_effect=fake_urlopen):
            result = registry.client("deepseek").request_proposal(
                context_pack={"spend": 200}, instruction="诊断", proposal_schema=None
            )
        self.assertEqual("deepseek", result["provider_id"])
        self.assertEqual("hold", result["proposal_candidates"][0]["arguments"]["action"])

    def test_qwen_bailian_chat_tool_call_has_output_limit_and_never_exposes_key(self) -> None:
        registry = self._registry("qwen", "qwen-plus")
        response = {
            "id": "qwen-chat-1",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "qwen-call-1",
                                "function": {
                                    "name": "submit_ai_proposal",
                                    "arguments": json.dumps(self._arguments()),
                                },
                            }
                        ]
                    },
                }
            ],
            "usage": {"prompt_tokens": 21, "completion_tokens": 9},
        }

        def fake_urlopen(request, timeout):
            self.assertEqual(7, timeout)
            self.assertEqual(
                "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                request.full_url,
            )
            self.assertEqual(
                "Bearer qwen-environment-key", request.get_header("Authorization")
            )
            payload = json.loads(request.data)
            self.assertEqual("qwen-plus", payload["model"])
            self.assertFalse(payload["stream"])
            self.assertFalse(payload["enable_thinking"])
            self.assertEqual(ai_provider.MAX_OUTPUT_TOKENS, payload["max_tokens"])
            self.assertEqual(
                "submit_ai_proposal", payload["tools"][0]["function"]["name"]
            )
            self.assertNotIn("strict", payload["tools"][0]["function"])
            self.assertFalse(
                payload["tools"][0]["function"]["parameters"]["additionalProperties"]
            )
            self.assertEqual(
                "submit_ai_proposal",
                payload["tool_choice"]["function"]["name"],
            )
            self.assertNotIn("qwen-environment-key", request.data.decode("utf-8"))
            return _Response(response)

        with patch.object(ai_provider, "urlopen", side_effect=fake_urlopen):
            result = registry.client("tongyi").request_proposal(
                context_pack={"roi": 1.25},
                instruction="诊断当前计划",
                proposal_schema=None,
            )
        self.assertEqual("qwen_bailian", result["provider_id"])
        self.assertEqual("hold", result["proposal_candidates"][0]["arguments"]["action"])
        self.assertFalse(result["execution_performed"])
        self.assertNotIn("qwen-environment-key", json.dumps(result))

    def test_glm_hunyuan_and_doubao_chat_tools_are_provider_compatible(self) -> None:
        cases = (
            (
                "glm",
                "glm_zhipu",
                "glm-5.2",
                "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                "glm-environment-key",
                "thinking",
            ),
            (
                "hunyuan",
                "hunyuan_tencent",
                "hy3",
                "https://tokenhub.tencentmaas.com/v1/chat/completions",
                "tokenhub-environment-key",
                "thinking",
            ),
            (
                "doubao",
                "doubao_ark",
                "ep-safe-endpoint-id",
                "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
                "doubao-environment-key",
                "reasoning_effort",
            ),
        )
        for alias, provider_id, model, endpoint, api_key, reasoning_control in cases:
            with self.subTest(provider_id=provider_id):
                registry = self._registry(alias, model)
                response = {
                    "id": f"{provider_id}-chat-1",
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": f"{provider_id}-call-1",
                                        "function": {
                                            "name": "submit_ai_proposal",
                                            "arguments": json.dumps(self._arguments()),
                                        },
                                    }
                                ]
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 13, "completion_tokens": 7},
                }

                def fake_urlopen(request, timeout):
                    self.assertEqual(7, timeout)
                    self.assertEqual(endpoint, request.full_url)
                    self.assertEqual(f"Bearer {api_key}", request.get_header("Authorization"))
                    payload = json.loads(request.data)
                    self.assertEqual(model, payload["model"])
                    self.assertFalse(payload["stream"])
                    self.assertEqual(ai_provider.MAX_OUTPUT_TOKENS, payload["max_tokens"])
                    self.assertEqual(
                        "submit_ai_proposal", payload["tools"][0]["function"]["name"]
                    )
                    self.assertNotIn("strict", payload["tools"][0]["function"])
                    self.assertEqual("auto", payload["tool_choice"])
                    if reasoning_control == "reasoning_effort":
                        self.assertEqual("minimal", payload["reasoning_effort"])
                        self.assertNotIn("thinking", payload)
                    else:
                        self.assertEqual({"type": "disabled"}, payload["thinking"])
                        self.assertNotIn("reasoning_effort", payload)
                    self.assertNotIn(api_key, request.data.decode("utf-8"))
                    return _Response(response)

                with patch.object(ai_provider, "urlopen", side_effect=fake_urlopen):
                    result = registry.client(alias).request_proposal(
                        context_pack={"roi": 1.31},
                        instruction="诊断当前投放计划",
                        proposal_schema=None,
                    )
                self.assertEqual(provider_id, result["provider_id"])
                self.assertEqual(
                    "hold", result["proposal_candidates"][0]["arguments"]["action"]
                )
                self.assertFalse(result["execution_performed"])
                self.assertNotIn(api_key, json.dumps(result))

    def test_response_size_limit_is_enforced(self) -> None:
        registry = self._registry("deepseek", "deepseek-chat")
        raw = b"x" * (ai_provider.MAX_RESPONSE_BYTES + 1)
        with patch.object(ai_provider, "urlopen", return_value=_Response(raw=raw)):
            with self.assertRaisesRegex(AIProviderResponseError, "安全上限"):
                registry.client("deepseek").request_proposal(
                    context_pack={"roi": 1}, instruction="诊断", proposal_schema=None
                )

    def test_provider_response_and_tool_arguments_reject_nonfinite_json(self) -> None:
        for raw in (
            b'{"value":NaN}',
            b'{"value":Infinity}',
            b'{"value":1e9999}',
        ):
            with self.subTest(raw=raw), self.assertRaises(AIProviderResponseError):
                ai_provider._read_response_json(_Response(raw=raw))

        for arguments in (
            {"confidence": float("nan")},
            '{"confidence":Infinity}',
            '{"confidence":1e9999}',
        ):
            with self.subTest(arguments=arguments), self.assertRaises(AIProviderResponseError):
                ai_provider._coerce_arguments(arguments)

    def test_http_error_never_exposes_key_or_response_body(self) -> None:
        registry = self._registry("deepseek", "deepseek-chat")
        response = Mock()
        error = HTTPError(
            "https://api.deepseek.com/v1/chat/completions",
            401,
            "body-has-secret-value",
            {},
            response,
        )
        with patch.object(ai_provider, "urlopen", side_effect=error):
            with self.assertRaises(AIProviderRequestError) as raised:
                registry.client("deepseek").request_proposal(
                    context_pack={"roi": 1}, instruction="诊断", proposal_schema=None
                )
        message = str(raised.exception)
        self.assertNotIn("deepseek-environment-key", message)
        self.assertNotIn("body-has-secret-value", message)
        self.assertIn("401", message)
        response.close.assert_called_once_with()

    def test_ephemeral_configuration_probe_uses_memory_only(self) -> None:
        response = {
            "id": "probe-response",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "probe-call",
                                "function": {
                                    "name": "submit_ai_proposal",
                                    "arguments": '{"probe":"ok"}',
                                },
                            }
                        ]
                    },
                }
            ],
        }

        def fake_urlopen(request, timeout):
            self.assertEqual(4, timeout)
            self.assertEqual("Bearer unsaved-test-api-key", request.get_header("Authorization"))
            request_payload = json.loads(request.data)
            user_content = request_payload["messages"][1]["content"]
            self.assertIn('"synthetic":true', user_content)
            self.assertNotIn("store", user_content.lower())
            self.assertNotIn("account", user_content.lower())
            return _Response(response)

        with patch.object(ai_provider, "urlopen", side_effect=fake_urlopen):
            result = request_ephemeral_proposal(
                provider_id="deepseek",
                model="deepseek-chat",
                api_key="unsaved-test-api-key",
                remote_access_approved=True,
                timeout_seconds=4,
                context_pack={
                    "probe": "connectivity",
                    "synthetic": True,
                    "contains_business_data": False,
                },
                instruction="probe only",
                proposal_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["probe"],
                    "properties": {"probe": {"type": "string", "enum": ["ok"]}},
                },
            )
        self.assertEqual("ok", result["proposal_candidates"][0]["arguments"]["probe"])
        self.assertNotIn("unsaved-test-api-key", json.dumps(result))
        self.assertEqual([], list(self.data_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
