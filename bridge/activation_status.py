"""Public, non-sensitive browser-extension activation status.

The local Agent and the installed browser-extension files can be updated at
different times.  This module exposes only the versions needed to distinguish
an incomplete installation from a browser that still has an older extension
loaded.  It never returns installation paths, extension IDs or authorization
state.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any


ACTIVATION_CONTRACT_VERSION = 1
MAX_EXTENSION_MANIFEST_BYTES = 64 * 1024
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def _normalized_version(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if len(candidate) > 64 or not _VERSION.fullmatch(candidate):
        return None
    return candidate


def _contained_regular_manifest(install_root: str | Path) -> Path | None:
    """Resolve the active manifest without escaping the installation root.

    macOS uses an in-root ``extension-current`` symlink for atomic activation,
    so that one pointer is allowed only when its final target remains inside
    the non-symbolic installation root.  The manifest itself and every resolved
    target directory must still be regular, non-symbolic filesystem entries.
    """

    root = Path(install_root)
    try:
        if root.is_symlink() or not root.is_dir():
            return None
        resolved_root = root.resolve(strict=True)
        pointer = root / "extension-current"
        resolved_pointer = pointer.resolve(strict=True)
        resolved_pointer.relative_to(resolved_root)
        if not resolved_pointer.is_dir():
            return None

        # Reject nested symbolic-link chains after the intentionally supported
        # extension-current pointer has resolved to its in-root target.
        relative_pointer = resolved_pointer.relative_to(resolved_root)
        current = resolved_root
        for part in relative_pointer.parts:
            current = current / part
            if current.is_symlink() or not current.is_dir():
                return None

        manifest = resolved_pointer / "manifest.json"
        if manifest.is_symlink():
            return None
        resolved_manifest = manifest.resolve(strict=True)
        resolved_manifest.relative_to(resolved_pointer)
        resolved_manifest.relative_to(resolved_root)
        metadata = resolved_manifest.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            return None
        if metadata.st_size <= 0 or metadata.st_size > MAX_EXTENSION_MANIFEST_BYTES:
            return None
        return resolved_manifest
    except (OSError, RuntimeError, ValueError):
        return None


def _development_regular_manifest(source_root: str | Path) -> Path | None:
    """Return the exact repository extension manifest for source development.

    Unlike the packaged macOS activation pointer, a source checkout must not
    contain symbolic links anywhere in the ``extension/manifest.json`` path.
    The caller controls whether source fallback is allowed at all.
    """

    root = Path(source_root)
    try:
        if root.is_symlink() or not root.is_dir():
            return None
        resolved_root = root.resolve(strict=True)
        extension = resolved_root / "extension"
        if extension.is_symlink() or not extension.is_dir():
            return None
        resolved_extension = extension.resolve(strict=True)
        resolved_extension.relative_to(resolved_root)
        if resolved_extension.parent != resolved_root:
            return None
        manifest = resolved_extension / "manifest.json"
        if manifest.is_symlink():
            return None
        resolved_manifest = manifest.resolve(strict=True)
        resolved_manifest.relative_to(resolved_extension)
        if resolved_manifest.parent != resolved_extension:
            return None
        metadata = resolved_manifest.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            return None
        if metadata.st_size <= 0 or metadata.st_size > MAX_EXTENSION_MANIFEST_BYTES:
            return None
        return resolved_manifest
    except (OSError, RuntimeError, ValueError):
        return None


def _read_manifest_version(manifest: Path | None) -> str | None:
    if manifest is None:
        return None
    try:
        descriptor = os.open(manifest, os.O_RDONLY)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                return None
            if metadata.st_size <= 0 or metadata.st_size > MAX_EXTENSION_MANIFEST_BYTES:
                return None
            raw = handle.read(MAX_EXTENSION_MANIFEST_BYTES + 1)
            if len(raw) > MAX_EXTENSION_MANIFEST_BYTES:
                return None
        document = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    return _normalized_version(document.get("version"))


def read_installed_extension_version(install_root: str | Path) -> str | None:
    """Read only a validated version from the active installed manifest."""

    return _read_manifest_version(_contained_regular_manifest(install_root))


def read_development_extension_version(source_root: str | Path) -> str | None:
    """Read the repository sibling manifest after strict source-tree checks."""

    return _read_manifest_version(_development_regular_manifest(source_root))


def build_activation_status(
    install_root: str | Path,
    *,
    agent_version: str,
    reported_extension_version: str = "",
    development_source_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build the minimal public activation contract.

    ``reported_extension_version`` is supplied by the running extension.  An
    absent or malformed value never claims that browser activation completed.
    """

    required_version = _normalized_version(agent_version)
    if required_version is None:
        # Release builds already enforce this invariant.  Keep the public
        # endpoint fail-closed if a developer build violates it.
        required_version = ""
    installed_version = read_installed_extension_version(install_root)
    if installed_version is None and development_source_root is not None:
        installed_version = read_development_extension_version(
            development_source_root
        )
    reported_version = _normalized_version(reported_extension_version)

    if not required_version or installed_version is None:
        state = "install_or_repair_required"
    elif installed_version != required_version:
        state = "installed_version_mismatch"
    elif reported_version is None:
        state = "installed_unconfirmed"
    elif reported_version != required_version:
        state = "reload_required"
    else:
        state = "active"

    return {
        "activation_contract_version": ACTIVATION_CONTRACT_VERSION,
        "agent_version": required_version,
        "required_extension_version": required_version,
        "installed_extension_version": installed_version,
        "activation": {
            "state": state,
            "ready": state == "active",
            "reload_required": state == "reload_required",
        },
    }


__all__ = [
    "ACTIVATION_CONTRACT_VERSION",
    "MAX_EXTENSION_MANIFEST_BYTES",
    "build_activation_status",
    "read_development_extension_version",
    "read_installed_extension_version",
]
