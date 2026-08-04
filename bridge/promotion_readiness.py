"""Promotion-readiness state that remains local and fail-closed.

This module deliberately does not publish data or mark a release ready on the
basis of a UI toggle.  Distribution evidence is either derived from the local
runtime or supplied by the release environment, and anonymous feedback is
stored in a bounded local queue only after explicit consent.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from .update_center import TELEMETRY_FIELDS, create_opt_in_telemetry
except ImportError:  # Direct bridge script/test execution.
    from update_center import TELEMETRY_FIELDS, create_opt_in_telemetry


EXTENSION_INSTALL_SOURCES = {
    "unknown",
    "unpacked",
    "release_bundle",
    "chrome_web_store",
    "edge_addons",
    "360_extension_store",
}
OFFICIAL_EXTENSION_STORES = {
    "chrome_web_store",
    "edge_addons",
    "360_extension_store",
}
# Store IDs are public trust anchors, not secrets. Keep them empty until each
# listing is actually published; an environment variable cannot create trust.
OFFICIAL_EXTENSION_IDS_BY_STORE: dict[str, frozenset[str]] = {
    "chrome_web_store": frozenset(),
    "edge_addons": frozenset(),
    "360_extension_store": frozenset(),
}
SUPPORTED_BROWSERS = {"unknown", "chrome", "edge", "360", "qq"}
RAW_SHOP_FIELD_NAMES = {
    "shop",
    "shop_id",
    "shop_name",
    "store",
    "store_id",
    "store_key",
    "account",
    "account_id",
    "account_key",
    "advertiser_id",
    "product",
    "product_id",
    "product_title",
    "order",
    "order_id",
    "phone",
    "mobile",
    "address",
    "raw",
    "raw_data",
    "snapshot",
    "metrics",
}
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_EXTENSION_ID = re.compile(r"^[a-p]{32}$")
TARGET_RELEASE_VERSION = "4.0.0"
REQUIRED_AUTHENTICODE_ARTIFACTS = {
    "agent",
    "updater",
    "installer_entry",
    "upgrade_entry",
    "maintenance_scripts",
}
_queue_lock = threading.Lock()


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _distribution_path(store_root: str | Path) -> Path:
    return Path(store_root) / "config" / "distribution_state.json"


def load_extension_install_state(store_root: str | Path) -> dict[str, Any]:
    defaults = {
        "source": "unknown",
        "browser": "unknown",
        "version": "",
        "extension_id": "",
        "reported_at": None,
        "evidence": "not_reported",
        "origin_verified": False,
    }
    path = _distribution_path(store_root)
    if not path.exists():
        return defaults
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(value, dict):
        return defaults
    source = str(value.get("source") or "unknown")
    browser = str(value.get("browser") or "unknown")
    defaults.update(
        {
            "source": source if source in EXTENSION_INSTALL_SOURCES else "unknown",
            "browser": browser if browser in SUPPORTED_BROWSERS else "unknown",
            "version": str(value.get("version") or "")[:40],
            "extension_id": str(value.get("extension_id") or "")[:64],
            "reported_at": value.get("reported_at"),
            "origin_verified": value.get("origin_verified") is True,
            "evidence": "extension_self_reported",
        }
    )
    return defaults


def save_extension_install_state(
    store_root: str | Path,
    payload: dict[str, Any],
    *,
    origin_extension_id: str | None = None,
) -> dict[str, Any]:
    """Persist only a small self-reported extension provenance record."""

    if not isinstance(payload, dict):
        raise ValueError("extension install source must be an object")
    allowed = {"source", "browser", "version", "extension_id"}
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        raise ValueError("extension install source contains unsupported fields: " + ", ".join(unexpected))
    source = str(payload.get("source") or "")
    browser = str(payload.get("browser") or "")
    version = str(payload.get("version") or "")
    extension_id = str(payload.get("extension_id") or "")
    if source not in EXTENSION_INSTALL_SOURCES - {"unknown"}:
        raise ValueError("extension install source is invalid")
    if browser not in SUPPORTED_BROWSERS - {"unknown"}:
        raise ValueError("extension browser is invalid")
    if not _VERSION.fullmatch(version):
        raise ValueError("extension version must use three-part semantic versioning")
    if extension_id and not _EXTENSION_ID.fullmatch(extension_id):
        raise ValueError("extension id is invalid")
    normalized_origin_id = str(origin_extension_id or "").strip().lower()
    origin_verified = bool(extension_id and normalized_origin_id == extension_id)
    value = {
        "source": source,
        "browser": browser,
        "version": version,
        "extension_id": extension_id,
        "reported_at": datetime.now(timezone.utc).isoformat(),
        "origin_verified": origin_verified,
    }
    _atomic_json_write(_distribution_path(store_root), value)
    return {**value, "evidence": "extension_self_reported"}


def _published_browser_stores() -> list[str]:
    configured = {
        value.strip()
        for value in os.environ.get("DIAN_AGENT_PUBLISHED_BROWSER_STORES", "").split(",")
        if value.strip()
    }
    return sorted(configured & OFFICIAL_EXTENSION_STORES)


def build_distribution_status(store_root: str | Path) -> dict[str, Any]:
    extension = load_extension_install_state(store_root)
    runtime_source = "packaged_executable" if getattr(sys, "frozen", False) else "source_checkout"
    release_source = str(os.environ.get("DIAN_AGENT_RELEASE_SOURCE") or "unconfigured")
    published_stores = _published_browser_stores()
    trusted_ids = OFFICIAL_EXTENSION_IDS_BY_STORE.get(extension["source"], frozenset())
    reported_store_install = extension["source"] in OFFICIAL_EXTENSION_STORES
    official_store_install = (
        reported_store_install
        and extension["origin_verified"]
        and extension["extension_id"] in trusted_ids
    )
    return {
        "agent": {
            "runtime_source": runtime_source,
            "release_source": release_source,
            "executable": str(Path(sys.executable).resolve()),
            "source_is_declared": release_source != "unconfigured",
        },
        "extension": {
            **extension,
            "reported_store_install": reported_store_install,
            "official_store_install": official_store_install,
            "official_extension_id_embedded": extension["extension_id"] in trusted_ids,
            "source_is_self_reported": extension["evidence"] == "extension_self_reported",
        },
        "browser_store_publication": {
            "published_stores": published_stores,
            "configured": bool(published_stores),
            "evidence": "release_environment" if published_stores else "not_configured",
        },
    }


class LocalAnonymousFeedbackQueue:
    """Bounded local-only queue for explicitly consented, coarse feedback."""

    def __init__(self, store_root: str | Path, *, maximum_events: int = 1000):
        self.path = Path(store_root) / "data" / "anonymous-feedback-queue.json"
        self.maximum_events = max(10, min(int(maximum_events), 10000))

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("anonymous feedback queue cannot be read") from exc
        if not isinstance(value, dict) or not isinstance(value.get("events"), list):
            raise ValueError("anonymous feedback queue is invalid")
        return [item for item in value["events"] if isinstance(item, dict)]

    def status(self, *, consent_enabled: bool) -> dict[str, Any]:
        error: str | None = None
        try:
            with _queue_lock:
                events = self._read()
        except ValueError as exc:
            events = []
            error = str(exc)
        status = {
            "enabled": consent_enabled is True,
            "consent": "explicit_opt_in" if consent_enabled is True else "not_granted",
            "mode": "local_queue_only",
            "upload_configured": False,
            "upload_attempted": False,
            "raw_shop_data_allowed": False,
            "allowed_fields": sorted(TELEMETRY_FIELDS),
            "queued_count": len(events),
            "maximum_events": self.maximum_events,
            "oldest_queued_at": events[0].get("queued_at") if events else None,
            "newest_queued_at": events[-1].get("queued_at") if events else None,
        }
        if error:
            status["status"] = "error"
            status["error"] = error
        else:
            status["status"] = "ready"
        return status

    def enqueue(self, payload: dict[str, Any], *, consent_enabled: bool) -> dict[str, Any]:
        if consent_enabled is not True:
            raise ValueError("explicit consent is required before anonymous feedback can be queued")
        if not isinstance(payload, dict):
            raise ValueError("anonymous feedback must be an object")
        unexpected = sorted(set(payload) - TELEMETRY_FIELDS)
        raw_fields = sorted({str(key).lower() for key in payload} & RAW_SHOP_FIELD_NAMES)
        if raw_fields:
            raise ValueError("raw shop data is not accepted: " + ", ".join(raw_fields))
        if unexpected:
            raise ValueError("anonymous feedback contains unsupported fields: " + ", ".join(unexpected))
        if any(isinstance(value, (dict, list, tuple, set)) for value in payload.values()):
            raise ValueError("anonymous feedback accepts coarse scalar fields only")
        clean = create_opt_in_telemetry(payload, opted_in=True)
        if clean is None:  # Defensive: consent was already checked above.
            raise ValueError("explicit consent is required")
        queued = {
            "event_id": uuid.uuid4().hex,
            "queued_at": datetime.now(timezone.utc).isoformat(),
            **clean,
        }
        with _queue_lock:
            events = self._read()
            events.append(queued)
            events = events[-self.maximum_events :]
            _atomic_json_write(self.path, {"schema_version": 1, "events": events})
        return queued

    def clear(self) -> int:
        """Clear only this queue, preserving all shop and operator data."""

        with _queue_lock:
            try:
                events = self._read()
            except ValueError:
                events = []
            _atomic_json_write(self.path, {"schema_version": 1, "events": []})
        return len(events)


def build_release_readiness(
    store_root: str | Path,
    *,
    production_ed25519_trust: bool,
    authenticode_artifacts: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Return evidence-backed v4.0 readiness; missing proof remains blocking."""

    distribution = build_distribution_status(store_root)
    authenticode = (
        _injected_authenticode_status(authenticode_artifacts)
        if authenticode_artifacts is not None
        else _verify_release_authenticode()
    )
    extension = distribution["extension"]
    publication = distribution["browser_store_publication"]
    extension_version_matches = extension["version"] == TARGET_RELEASE_VERSION
    extension_store_matches_publication = extension["source"] in publication["published_stores"]
    browser_store_ready = (
        publication["configured"]
        and extension["official_store_install"]
        and extension_version_matches
        and extension_store_matches_publication
    )
    checks = [
        {
            "id": "production_ed25519_trust",
            "ready": production_ed25519_trust is True,
            "blocking": True,
            "evidence": "embedded_production_public_key" if production_ed25519_trust else "missing",
        },
        {
            "id": "windows_authenticode",
            "ready": authenticode["ready"],
            "blocking": True,
            "evidence": "all_release_artifacts_verified" if authenticode["ready"] else "missing_or_unverifiable",
            "artifact_checks": authenticode["artifact_checks"],
        },
        {
            "id": "browser_store_publication",
            "ready": browser_store_ready,
            "blocking": True,
            "evidence": publication["evidence"] if browser_store_ready else "publication_or_current_install_not_verified",
            "published_stores": publication["published_stores"],
            "current_install_source": extension["source"],
            "official_store_install": extension["official_store_install"],
            "extension_version": extension["version"],
            "required_extension_version": TARGET_RELEASE_VERSION,
            "version_matches": extension_version_matches,
            "source_matches_publication": extension_store_matches_publication,
        },
    ]
    blockers = [item["id"] for item in checks if item["blocking"] and not item["ready"]]
    return {
        "target_version": TARGET_RELEASE_VERSION,
        "ready_for_public_release": not blockers,
        "status": "ready" if not blockers else "blocked",
        "blockers": blockers,
        "checks": checks,
        "claims_are_evidence_backed": True,
    }


def _injected_authenticode_status(values: dict[str, bool]) -> dict[str, Any]:
    """Unit-test hook requiring an explicit result for every release artifact."""

    if not isinstance(values, dict):
        raise ValueError("authenticode artifact evidence must be an object")
    unexpected = sorted(set(values) - REQUIRED_AUTHENTICODE_ARTIFACTS)
    if unexpected:
        raise ValueError("unknown authenticode artifacts: " + ", ".join(unexpected))
    checks = [
        {
            "id": artifact,
            "ready": values.get(artifact) is True,
            "evidence": "test_verified" if values.get(artifact) is True else "test_missing",
        }
        for artifact in sorted(REQUIRED_AUTHENTICODE_ARTIFACTS)
    ]
    return {"ready": all(item["ready"] for item in checks), "artifact_checks": checks}


def _release_root_from_executable() -> Path | None:
    configured = str(os.environ.get("DIAN_AGENT_RELEASE_ROOT") or "").strip()
    if configured:
        candidate = Path(configured).resolve()
        return candidate if candidate.is_dir() else None
    if not getattr(sys, "frozen", False):
        return None
    executable = Path(sys.executable).resolve()
    if executable.parent.name.lower() == "app":
        return executable.parent.parent
    if executable.parent.parent.name.lower() == "app":
        return executable.parents[2]
    return None


@lru_cache(maxsize=32)
def _authenticode_file_valid(path_text: str, modified_ns: int) -> bool:
    """Ask Windows to validate one immutable file version."""

    del modified_ns
    if os.name != "nt":
        return False
    command = (
        "& { param([string]$Target) "
        "(Get-AuthenticodeSignature -LiteralPath $Target).Status.ToString() }"
    )
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
                path_text,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "Valid"


def _signed_file_check(artifact: str, path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {"id": artifact, "ready": False, "evidence": "missing"}
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError:
        return {"id": artifact, "ready": False, "evidence": "unreadable"}
    valid = _authenticode_file_valid(str(path.resolve()), modified_ns)
    return {
        "id": artifact,
        "ready": valid,
        "evidence": "windows_signature_valid" if valid else "windows_signature_invalid",
        "filename": path.name,
    }


def _verify_release_authenticode() -> dict[str, Any]:
    """Verify the full public release chain, not only the running Agent."""

    release_root = _release_root_from_executable()
    if os.name != "nt" or not getattr(sys, "frozen", False) or release_root is None:
        checks = [
            {"id": artifact, "ready": False, "evidence": "packaged_release_not_detected"}
            for artifact in sorted(REQUIRED_AUTHENTICODE_ARTIFACTS)
        ]
        return {"ready": False, "artifact_checks": checks}

    executable = Path(sys.executable).resolve()
    agent = _signed_file_check("agent", executable)
    updater = _signed_file_check("updater", release_root / "tools" / "DianAgentUpdater.exe")

    installer_batch = release_root / "install_dian_agent.bat"
    installer_executable = release_root / "DianAgentInstaller.exe"
    installer = _signed_file_check("installer_entry", installer_executable)
    if installer_batch.is_file() and not installer["ready"]:
        installer["evidence"] = "unsigned_batch_entrypoint_requires_signed_installer"

    upgrade_batch = release_root / "upgrade_dian_agent.bat"
    upgrade_executable = release_root / "DianAgentUpgrade.exe"
    upgrade = _signed_file_check("upgrade_entry", upgrade_executable)
    if upgrade_batch.is_file() and not upgrade["ready"]:
        upgrade["evidence"] = "unsigned_batch_entrypoint_requires_signed_upgrader"

    maintenance_paths = [
        release_root / "tools" / name
        for name in (
            "install_release.ps1",
            "uninstall_release.ps1",
            "start_agent.ps1",
            "watchdog_release.ps1",
            "sync_release_tools.ps1",
        )
    ]
    maintenance_results = [_signed_file_check("maintenance_script", path) for path in maintenance_paths]
    maintenance = {
        "id": "maintenance_scripts",
        "ready": all(item["ready"] for item in maintenance_results),
        "evidence": "all_windows_signatures_valid" if all(item["ready"] for item in maintenance_results) else "missing_or_unsigned_scripts",
        "files": maintenance_results,
    }
    checks = [agent, updater, installer, upgrade, maintenance]
    return {"ready": all(item["ready"] for item in checks), "artifact_checks": checks}


__all__ = [
    "LocalAnonymousFeedbackQueue",
    "build_distribution_status",
    "build_release_readiness",
    "load_extension_install_state",
    "save_extension_install_state",
]
