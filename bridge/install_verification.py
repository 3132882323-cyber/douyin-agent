"""Close a pending install audit from a late authenticated extension report."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from activation_status import read_installed_extension_version


_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_EXTENSION_ID = re.compile(r"^[a-p]{32}$")
_MAX_VERIFICATION_BYTES = 64 * 1024
_verification_lock = threading.RLock()


def _safe_existing_verification_path(install_root: str | Path) -> Path | None:
    root = Path(install_root)
    try:
        if root.is_symlink() or not root.is_dir():
            return None
        resolved_root = root.resolve(strict=True)
        current = resolved_root
        for part in ("data", "runtime"):
            current = current / part
            if current.is_symlink() or not current.is_dir():
                return None
        path = current / "install-verification.json"
        if path.is_symlink() or not path.is_file():
            return None
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
        metadata = resolved.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            return None
        if metadata.st_size <= 0 or metadata.st_size > _MAX_VERIFICATION_BYTES:
            return None
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


def _read_verification(path: Path) -> dict[str, Any] | None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                return None
            if metadata.st_size <= 0 or metadata.st_size > _MAX_VERIFICATION_BYTES:
                return None
            raw = handle.read(_MAX_VERIFICATION_BYTES + 1)
            if len(raw) > _MAX_VERIFICATION_BYTES:
                return None
        value = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 80:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _atomic_replace(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def complete_late_install_verification(
    install_root: str | Path,
    report: dict[str, Any],
    *,
    authenticated_subject: str,
    origin_extension_id: str,
    origin_trusted: bool,
) -> bool:
    """Atomically promote only a pending, exact-version authenticated report.

    The caller must be the protected extension-source POST route.  This helper
    independently rechecks its origin/session binding, installer trust result,
    installed manifest and pending audit before writing the canonical receipt.
    """

    if not isinstance(report, dict) or origin_trusted is not True:
        return False
    extension_id = str(report.get("extension_id") or "").strip().lower()
    subject = str(authenticated_subject or "").strip().lower()
    origin_id = str(origin_extension_id or "").strip().lower()
    version = str(report.get("version") or "").strip()
    if (
        report.get("origin_verified") is not True
        or not _EXTENSION_ID.fullmatch(extension_id)
        or subject != extension_id
        or origin_id != extension_id
        or not _VERSION.fullmatch(version)
        or not _valid_timestamp(report.get("reported_at"))
    ):
        return False
    if read_installed_extension_version(install_root) != version:
        return False

    with _verification_lock:
        path = _safe_existing_verification_path(install_root)
        if path is None:
            return False
        current = _read_verification(path)
        if not isinstance(current, dict):
            return False
        if current.get("schema_version") != 1:
            return False
        if str(current.get("state") or "") == "verified":
            # Already-complete receipts are immutable.  Returning False also
            # avoids logging every routine extension provenance refresh as a
            # newly completed installation.
            return False
        if (
            str(current.get("state") or "") != "extension_report_required"
            or str(current.get("target_version") or "") != version
            or not _valid_timestamp(current.get("started_at"))
            or not _valid_timestamp(current.get("checked_at"))
        ):
            return False

        verified = {
            "schema_version": 1,
            "state": "verified",
            "target_version": version,
            "started_at": current["started_at"],
            "checked_at": current["checked_at"],
            "reported_at": report["reported_at"],
            "accepted_extension_id": extension_id,
            "accepted_scope": "late_authenticated_report",
        }
        try:
            # Recheck the file did not become a symbolic link after validation.
            if path.is_symlink() or not path.is_file():
                return False
            _atomic_replace(path, verified)
        except OSError:
            return False
    return True


__all__ = ["complete_late_install_verification"]
