"""Shared runtime state for the local companion service."""

from __future__ import annotations

import os
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DIAN_AGENT_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

PORT = int(os.environ.get("BRIDGE_PORT", "8765"))
MAX_BODY_BYTES = int(os.environ.get("BRIDGE_MAX_BODY", str(2 * 1024 * 1024)))
ALLOWED_SOURCES = {"doudian", "qianchuan"}
STALE_SECONDS = 10 * 60

_state_lock = threading.Lock()
_analysis_cache: dict[str, tuple[float, object]] = {}
_CACHE_TTL_SECONDS = 5


def set_data_dir(path: Path) -> Path:
    """Update the shared data directory (used by tests and startup)."""
    global DATA_DIR
    DATA_DIR = Path(path)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
