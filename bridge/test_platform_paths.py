from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

import platform_paths


class PlatformPathTests(unittest.TestCase):
    def test_macos_uses_application_support(self) -> None:
        with (
            patch.object(platform_paths.sys, "platform", "darwin"),
            patch.object(platform_paths.Path, "home", return_value=Path("/Users/tester")),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertEqual(
                Path("/Users/tester/Library/Application Support/DianAgent"),
                platform_paths.default_install_root(),
            )

    def test_environment_override_has_priority(self) -> None:
        with patch.dict(os.environ, {"DIAN_AGENT_INSTALL_ROOT": "/managed/dian"}, clear=True):
            self.assertEqual(Path("/managed/dian"), platform_paths.default_install_root())

    def test_linux_uses_xdg_data_home(self) -> None:
        with (
            patch.object(platform_paths.sys, "platform", "linux"),
            patch.dict(os.environ, {"XDG_DATA_HOME": "/tmp/xdg"}, clear=True),
        ):
            self.assertEqual(Path("/tmp/xdg/DianAgent"), platform_paths.default_install_root())


if __name__ == "__main__":
    unittest.main()
