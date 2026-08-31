"""Cross-platform per-user paths for Dian Agent.

The browser extension always talks to the same loopback port, while each
desktop platform keeps durable data in its conventional per-user location.
Environment overrides are intentionally supported for tests and managed
deployments.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def default_install_root() -> Path:
    """Return the platform-native, per-user Dian Agent install root."""

    configured = str(os.environ.get("DIAN_AGENT_INSTALL_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser()

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "DianAgent"
        return Path.home() / "AppData" / "Local" / "DianAgent"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "DianAgent"

    xdg_data_home = str(os.environ.get("XDG_DATA_HOME") or "").strip()
    base = Path(xdg_data_home).expanduser() if xdg_data_home else Path.home() / ".local" / "share"
    return base / "DianAgent"


def default_data_dir() -> Path:
    configured = str(os.environ.get("DIAN_AGENT_DATA_DIR") or "").strip()
    return Path(configured).expanduser() if configured else default_install_root() / "data"


def default_log_dir() -> Path:
    configured = str(os.environ.get("DIAN_AGENT_LOG_DIR") or "").strip()
    return Path(configured).expanduser() if configured else default_install_root() / "logs"


def platform_id() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


__all__ = ["default_data_dir", "default_install_root", "default_log_dir", "platform_id"]
