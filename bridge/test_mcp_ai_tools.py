from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

import server


def _call(name: str, arguments: dict | None = None) -> dict:
    content = asyncio.run(server.call_tool(name, arguments or {}))
    return json.loads(content[0].text)


class MCPAIToolTests(unittest.TestCase):
    def test_only_proposal_level_ai_tools_are_exposed(self) -> None:
        names = {tool.name for tool in server.TOOLS}
        self.assertEqual(
            {"get_ai_context_pack", "submit_ai_proposal", "get_ai_proposals"},
            names,
        )
        for forbidden in (
            "confirm_ai_proposal",
            "authorize_ai_proposal",
            "consume_ai_authorization",
            "execute_ai_proposal",
            "run_ai_execution",
        ):
            self.assertNotIn(forbidden, names)
        ai_names = {name for name in names if "ai_" in name or name.endswith("_ai")}
        for name in ai_names:
            self.assertFalse(
                any(verb in name for verb in ("confirm", "authorize", "consume", "execute")),
                f"AI MCP tool must not expose an execution capability: {name}",
            )
        submit_tool = next(tool for tool in server.TOOLS if tool.name == "submit_ai_proposal")
        proposal_schema = submit_tool.inputSchema["properties"]["proposal"]
        self.assertFalse(proposal_schema["additionalProperties"])
        self.assertIn("context_id", proposal_schema["required"])
        self.assertNotIn("current_value", proposal_schema["properties"])
        self.assertNotIn("target_value", proposal_schema["properties"])

    def test_raw_snapshot_tools_are_not_exposed_or_callable_by_name(self) -> None:
        for name in ("get_doudian_data", "get_qianchuan_data", "get_agent_settings", "update_operation_task"):
            with self.subTest(name=name):
                result = _call(name, {"include_page_text": True})
                self.assertEqual("MCP_TOOL_NOT_ALLOWED", result["error"]["code"])
                self.assertFalse(result["execution_allowed"])

    def test_context_pack_is_read_through_public_adapter_and_sealed(self) -> None:
        with patch.object(
            server,
            "_get_ai_context_pack",
            return_value={"context_id": "context-1", "execution_allowed": True},
        ) as adapter:
            result = _call("get_ai_context_pack")

        adapter.assert_called_once_with()
        self.assertEqual("context-1", result["context_id"])
        self.assertEqual("proposal_only", result["mode"])
        self.assertFalse(result["execution_allowed"])
        self.assertFalse(result["can_execute"])
        self.assertFalse(result["execution_performed"])

    def test_submit_only_enqueues_untrusted_proposal_and_is_sealed(self) -> None:
        proposal = {"schema_version": 1, "action": "hold", "plan_id": "plan-1"}
        with patch.object(
            server,
            "_submit_ai_proposal",
            return_value={"proposal_id": "proposal-1", "can_execute": True},
        ) as adapter:
            result = _call(
                "submit_ai_proposal",
                {
                    "proposal": proposal,
                    "provider_id": "deepseek",
                    "model": "deepseek-chat",
                    "context_hash": "hash-1",
                },
            )

        adapter.assert_called_once_with(
            proposal,
            provider_id="deepseek",
            model="deepseek-chat",
            context_hash="hash-1",
        )
        self.assertEqual("proposal-1", result["proposal_id"])
        self.assertFalse(result["execution_allowed"])
        self.assertFalse(result["can_execute"])
        self.assertFalse(result["execution_performed"])

    def test_missing_proposal_is_rejected_without_calling_adapter(self) -> None:
        with patch.object(server, "_submit_ai_proposal") as adapter:
            result = _call("submit_ai_proposal", {"provider_id": "openai"})

        adapter.assert_not_called()
        self.assertEqual("AI_PROPOSAL_REJECTED", result["error"]["code"])
        self.assertFalse(result["execution_allowed"])

    def test_proposal_list_limit_is_bounded_and_result_is_sealed(self) -> None:
        with patch.object(
            server,
            "_get_ai_proposals",
            return_value={"proposals": [], "execution_allowed": True},
        ) as adapter:
            result = _call("get_ai_proposals", {"limit": 999})

        adapter.assert_called_once_with(limit=100)
        self.assertFalse(result["execution_allowed"])
        self.assertFalse(result["can_execute"])

    def test_missing_ai_bridge_fails_closed(self) -> None:
        with patch.object(server, "_get_ai_context_pack", None):
            result = _call("get_ai_context_pack")

        self.assertEqual("AI_BRIDGE_UNAVAILABLE", result["error"]["code"])
        self.assertFalse(result["execution_allowed"])
        self.assertFalse(result["can_execute"])


if __name__ == "__main__":
    unittest.main()
