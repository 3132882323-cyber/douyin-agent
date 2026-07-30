"""Tests for execution mode labels and OAuth callback configuration."""

from __future__ import annotations

import importlib
import os
import unittest


class ExecutionModeTests(unittest.TestCase):
    def test_supervised_label_is_受监督执行(self) -> None:
        from execution_modes import EXECUTION_MODE_LABELS, execution_mode_label

        self.assertEqual(execution_mode_label("supervised"), "受监督执行")
        self.assertEqual(EXECUTION_MODE_LABELS["observe"], "观察模式")
        self.assertEqual(EXECUTION_MODE_LABELS["shadow"], "影子模式")


class OAuthCallbackConfigTests(unittest.TestCase):
    def test_callback_url_reads_environment(self) -> None:
        previous = os.environ.get("DIAN_AGENT_OAUTH_CALLBACK_URL")
        os.environ["DIAN_AGENT_OAUTH_CALLBACK_URL"] = "https://example.test/oauth/callback"
        try:
            import oceanengine_oauth

            importlib.reload(oceanengine_oauth)
            self.assertEqual(oceanengine_oauth.PUBLIC_CALLBACK_URL, "https://example.test/oauth/callback")
        finally:
            if previous is None:
                os.environ.pop("DIAN_AGENT_OAUTH_CALLBACK_URL", None)
            else:
                os.environ["DIAN_AGENT_OAUTH_CALLBACK_URL"] = previous
            import oceanengine_oauth

            importlib.reload(oceanengine_oauth)


if __name__ == "__main__":
    unittest.main()
