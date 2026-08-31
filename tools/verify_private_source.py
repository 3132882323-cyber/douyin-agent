#!/usr/bin/env python3
"""Verify that an internal staging tree has the real commercial runtime."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_private_source.py BRIDGE_DIR")
    bridge_dir = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(bridge_dir))
    from build_flavor import build_edition_status
    from chengfang_autopilot_runtime import ChengfangAutopilotRuntime
    from chengfang_official_adapter import OfficialChengfangBudgetAdapter
    from chengfang_production_controller import ChengfangProductionController
    from chengfang_production_targets import ChengfangProductionTargetService

    status = build_edition_status()
    if status.get("build_flavor") != "private_commercial":
        raise RuntimeError("private build flavor overlay is missing")
    if status.get("commercial_modules_included") is not True:
        raise RuntimeError("commercial build flag is missing")
    if ChengfangAutopilotRuntime.__module__ != "chengfang_autopilot_runtime":
        raise RuntimeError("real Chengfang runtime was not imported")
    if OfficialChengfangBudgetAdapter.__module__ != "chengfang_official_adapter":
        raise RuntimeError("official Chengfang write adapter was not imported")
    if ChengfangProductionController.__module__ != "chengfang_production_controller":
        raise RuntimeError("production write controller was not imported")
    if ChengfangProductionTargetService.__module__ != "chengfang_production_targets":
        raise RuntimeError("production target resolver was not imported")
    print("Internal commercial source verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
