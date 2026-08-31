#!/usr/bin/env python3
"""Fail-closed release gate for browser-extension distribution.

The local development bundle intentionally uses an unpacked extension.  That
is useful for development, but it can never prove that a consumer release is
free from manual extension reloads.  This checker keeps those two claims
separate and refuses the consumer claim until store identity, browser-owned
updates and an Agent/extension protocol compatibility contract all exist.
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


EXTENSION_ID = re.compile(r"^[a-p]{32}$")
OFFICIAL_STORES = {
    "chrome_web_store": {
        "update_url": "https://clients2.google.com/service/update2/crx",
        "listing_hosts": {"chromewebstore.google.com"},
    },
    "edge_addons": {
        "update_url": "https://edge.microsoft.com/extensionwebstorebase/v1/crx",
        "listing_hosts": {"microsoftedge.microsoft.com"},
    },
}


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    blocking: bool = True


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def derive_extension_id(public_key: str) -> str:
    try:
        decoded = base64.b64decode(str(public_key), validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("manifest.key is not valid base64") from exc
    if not decoded:
        raise ValueError("manifest.key is empty")
    prefix = hashlib.sha256(decoded).hexdigest()[:32]
    return "".join(chr(ord("a") + int(nibble, 16)) for nibble in prefix)


def _literal_string_set(node: ast.AST) -> set[str] | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"set", "frozenset"}:
        if not node.args:
            return set()
        if len(node.args) != 1:
            return None
        return _literal_string_set(node.args[0])
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values: set[str] = set()
        for item in node.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                return None
            values.add(item.value)
        return values
    return None


def read_official_trust_anchors(path: Path) -> dict[str, set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    value: ast.AST | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "OFFICIAL_EXTENSION_IDS_BY_STORE"
            for target in node.targets
        ):
            value = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "OFFICIAL_EXTENSION_IDS_BY_STORE"
        ):
            value = node.value
    if not isinstance(value, ast.Dict):
        return {}
    result: dict[str, set[str]] = {}
    for key_node, value_node in zip(value.keys, value.values):
        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
            continue
        ids = _literal_string_set(value_node)
        if ids is not None:
            result[key_node.value] = ids
    return result


def _contains_any(path: Path, needles: tuple[str, ...]) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8-sig")
    return any(needle in text for needle in needles)


def audit_distribution(
    source: Path,
    *,
    profile: str,
    manifest_path: Path | None = None,
    selected_store: str | None = None,
) -> dict[str, Any]:
    source = source.resolve()
    manifest_path = (manifest_path or source / "extension" / "manifest.json").resolve()
    contract_path = source / "extension" / "distribution.channels.json"
    findings: list[Finding] = []

    try:
        manifest = _read_json(manifest_path)
        contract = _read_json(contract_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "profile": profile,
            "findings": [asdict(Finding("DISTRIBUTION_CONTRACT_UNREADABLE", str(exc)))],
        }

    if contract.get("schema_version") != 1:
        findings.append(Finding("DISTRIBUTION_CONTRACT_SCHEMA_INVALID", "distribution.channels.json must use schema_version 1."))
    if manifest.get("manifest_version") != 3:
        findings.append(Finding("MANIFEST_VERSION_UNSUPPORTED", "The extension package must use Manifest V3."))

    derived_id = ""
    try:
        derived_id = derive_extension_id(str(manifest.get("key") or ""))
    except ValueError as exc:
        findings.append(Finding("MANIFEST_IDENTITY_UNPROVABLE", str(exc)))

    development = contract.get("development") if isinstance(contract.get("development"), dict) else {}
    if development.get("distribution_mode") != "unpacked_development":
        findings.append(Finding("DEVELOPMENT_CHANNEL_INVALID", "The development channel must remain explicitly unpacked."))
    if derived_id and development.get("extension_id") != derived_id:
        findings.append(Finding("DEVELOPMENT_ID_MISMATCH", "The checked-in development ID does not match manifest.key."))

    if profile == "internal":
        blocking = [item for item in findings if item.blocking]
        return {
            "ok": not blocking,
            "profile": profile,
            "manifest": str(manifest_path),
            "derived_extension_id": derived_id,
            "findings": [asdict(item) for item in findings],
        }

    consumer = contract.get("consumer_release") if isinstance(contract.get("consumer_release"), dict) else {}
    protocol = contract.get("protocol") if isinstance(contract.get("protocol"), dict) else {}
    stores = contract.get("stores") if isinstance(contract.get("stores"), dict) else {}

    if consumer.get("enabled") is not True:
        findings.append(Finding("CONSUMER_CHANNEL_DISABLED", "Consumer browser-store distribution has not been enabled."))
    if consumer.get("distribution_mode") != "browser_store" or consumer.get("update_owner") != "browser_store":
        findings.append(Finding("BROWSER_STORE_NOT_UPDATE_OWNER", "Consumer installation and updates must be owned by the browser store."))
    if consumer.get("manual_reload_required") is not False:
        findings.append(Finding("MANUAL_RELOAD_STILL_REQUIRED", "The current release contract still requires a manual extension reload."))
    if consumer.get("includes_unpacked_extension") is not False:
        findings.append(Finding("UNPACKED_EXTENSION_IN_CONSUMER_BUNDLE", "The consumer Agent bundle must not install an unpacked extension tree."))

    if (
        protocol.get("mode") != "negotiated_protocol"
        or protocol.get("exact_product_version_required") is not False
        or not isinstance(protocol.get("current"), int)
        or int(protocol.get("current") or 0) < 1
    ):
        findings.append(Finding("EXACT_PRODUCT_VERSION_LOCKSTEP", "Agent and extension must negotiate a protocol instead of requiring identical product versions."))

    required_stores = consumer.get("required_stores")
    if selected_store:
        required_stores = [selected_store]
    if not isinstance(required_stores, list) or not required_stores:
        findings.append(Finding("REQUIRED_STORE_MISSING", "At least one official browser store must be required."))
        required_stores = []

    trust_path = source / "bridge" / "promotion_readiness.py"
    trust_anchors = read_official_trust_anchors(trust_path) if trust_path.is_file() else {}
    for store_name in required_stores:
        official = OFFICIAL_STORES.get(str(store_name))
        store = stores.get(store_name) if isinstance(stores.get(store_name), dict) else {}
        if official is None:
            findings.append(Finding("STORE_UNSUPPORTED", f"Unsupported store in release contract: {store_name}"))
            continue
        extension_id = str(store.get("extension_id") or "")
        if store.get("status") != "published":
            findings.append(Finding("STORE_LISTING_NOT_PUBLISHED", f"{store_name} is not marked as published."))
        if not EXTENSION_ID.fullmatch(extension_id):
            findings.append(Finding("OFFICIAL_STORE_ID_MISSING", f"{store_name} has no valid official extension ID."))
        if store.get("update_url") != official["update_url"]:
            findings.append(Finding("STORE_UPDATE_URL_INVALID", f"{store_name} does not use its official update URL."))
        listing = urlparse(str(store.get("listing_url") or ""))
        if listing.scheme != "https" or listing.hostname not in official["listing_hosts"] or extension_id not in listing.path:
            findings.append(Finding("STORE_LISTING_URL_INVALID", f"{store_name} listing URL is missing, untrusted, or does not contain its extension ID."))
        if extension_id and extension_id not in trust_anchors.get(str(store_name), set()):
            findings.append(Finding("STORE_ID_NOT_IN_AGENT_TRUST_ANCHOR", f"{store_name} ID is not compiled into Agent promotion trust anchors."))
        if derived_id and extension_id and derived_id != extension_id:
            findings.append(Finding("STORE_PACKAGE_ID_MISMATCH", f"The selected package identity does not match the published {store_name} ID."))
        manifest_update_url = str(manifest.get("update_url") or "")
        if manifest_update_url and manifest_update_url != official["update_url"]:
            findings.append(Finding("MANIFEST_UPDATE_URL_CONFLICT", f"The package update_url conflicts with {store_name}."))

    installer_path = source / "tools" / "install_release.ps1"
    if _contains_any(installer_path, ("chrome://extensions/", "click Reload for Dian Agent")):
        findings.append(Finding("MANUAL_RELOAD_INSTALLER_PATH", "The consumer installer still opens the developer extension page or instructs Reload."))
    if _contains_any(installer_path, ("Copy-DirectoryContents $extensionSource $stableExtensionStage",)):
        findings.append(Finding("UNPACKED_INSTALLER_PATH", "The consumer installer still copies an unpacked extension into extension-current."))

    build_path = source / "tools" / "build_release_core.ps1"
    if _contains_any(build_path, ("Copy-Item -Path (Join-Path $outputRootFull \"dian-agent-modern\\*\")",)):
        findings.append(Finding("UNPACKED_PUBLIC_BUILD_PATH", "The public Windows bundle still embeds the unpacked browser package."))

    activation_path = source / "bridge" / "activation_status.py"
    if _contains_any(activation_path, ("installed_version != required_version", "reported_version != required_version")):
        findings.append(Finding("RUNTIME_VERSION_LOCKSTEP", "Activation still compares Agent and extension product versions for exact equality."))

    bridge_auth = source / "extension" / "bridge-auth.js"
    receiver = source / "bridge" / "http_receiver.py"
    if not _contains_any(bridge_auth, ("X-Dian-Agent-Protocol",)) or not _contains_any(receiver, ("X-Dian-Agent-Protocol",)):
        findings.append(Finding("PROTOCOL_NEGOTIATION_NOT_IMPLEMENTED", "Both extension and Agent must exchange an explicit compatibility protocol header."))

    native = contract.get("native_messaging") if isinstance(contract.get("native_messaging"), dict) else {}
    if native.get("solves_extension_install_or_update") is not False:
        findings.append(Finding("NATIVE_MESSAGING_SCOPE_INVALID", "Native Messaging is transport only and must not be treated as an extension installer/updater."))

    blocking = [item for item in findings if item.blocking]
    return {
        "ok": not blocking,
        "profile": profile,
        "manifest": str(manifest_path),
        "derived_extension_id": derived_id,
        "findings": [asdict(item) for item in findings],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--profile", choices=("internal", "consumer"), default="consumer")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--store", choices=tuple(OFFICIAL_STORES))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = audit_distribution(
        args.source,
        profile=args.profile,
        manifest_path=args.manifest,
        selected_store=args.store,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        state = "READY" if result["ok"] else "BLOCKED"
        print(f"Extension distribution {args.profile}: {state}")
        for finding in result.get("findings", []):
            print(f"- {finding['code']}: {finding['message']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
