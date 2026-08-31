#!/usr/bin/env python3
"""Source-installer wrapper for DianAgent's shared trust provisioner."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_ROOT = PROJECT_ROOT / "bridge"
if str(BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(BRIDGE_ROOT))

from local_api_auth import (  # noqa: E402
    LocalApiAuthError,
    chromium_extension_id as _chromium_extension_id,
    provision_local_api_trust,
)
from version import AGENT_VERSION  # noqa: E402


ProvisioningError = LocalApiAuthError


def chromium_extension_id(manifest_path: str | Path) -> str:
    return _chromium_extension_id(manifest_path)


def initialize(manifest_path: str | Path, install_root: str | Path) -> str:
    return str(provision_local_api_trust(install_root, manifest_path)["extension_id"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize DianAgent local API trust")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--install-root", required=True, type=Path)
    parser.add_argument("--json", action="store_true", help="print the public provisioning receipt")
    args = parser.parse_args(argv)
    try:
        result = provision_local_api_trust(args.install_root, args.manifest)
    except (LocalApiAuthError, OSError) as exc:
        parser.exit(2, f"Local API trust initialization failed: {exc}\n")
    if args.json:
        print(json.dumps({"ok": True, **result, "agent_version": AGENT_VERSION}, ensure_ascii=False, sort_keys=True))
    else:
        print(result["extension_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
