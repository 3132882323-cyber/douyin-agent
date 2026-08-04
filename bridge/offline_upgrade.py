"""Verified offline release installation with atomic activation and rollback.

An offline bundle is a ZIP containing ``offline-manifest.json`` plus files
below ``program/`` and ``extension/``.  Every payload file is declared by the
manifest and verified before anything is activated.  Releases are installed
under ``versions/<version>`` and ``current.json`` is the only mutable pointer.

The installer deliberately never copies files into mutable application
directories such as data, config, knowledge, backup or logs.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from types import MappingProxyType

MANIFEST_NAME = "offline-manifest.json"
SUPPORTED_MANIFEST_VERSIONS = {1}
ALLOWED_PAYLOAD_ROOTS = {"program", "extension"}
PROTECTED_INSTALL_ROOTS = {"data", "config", "knowledge", "backup", "backups", "logs"}
MAX_BUNDLE_FILES = 20_000
MAX_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}

# Production releases intentionally have no trust anchor until the release
# owner provisions an Ed25519 key offline and commits *only* its public key.
# An empty map is a safe deployment state: every production bundle is rejected.
# Keep private production keys outside this repository and outside build logs.
PRODUCTION_OFFLINE_PUBLIC_KEYS = MappingProxyType({})

# RFC 8032 test-vector public key.  Its matching private seed is public test
# material and may only be selected through the explicit development flag.
DEVELOPMENT_TEST_KEY_ID = "development-test-rfc8032-1"
DEVELOPMENT_TEST_PUBLIC_KEYS = MappingProxyType(
    {DEVELOPMENT_TEST_KEY_ID: "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"}
)


class OfflineUpgradeError(RuntimeError):
    """Base error for a rejected or failed offline upgrade."""


class ManifestValidationError(OfflineUpgradeError):
    """The offline bundle manifest is invalid or incompatible."""


class BundleValidationError(OfflineUpgradeError):
    """ZIP structure or payload integrity validation failed."""


class ActivationError(OfflineUpgradeError):
    """A verified release could not be activated or rolled back safely."""


@dataclass(frozen=True)
class VerifiedBundle:
    path: Path
    manifest: dict[str, Any]
    members: dict[str, zipfile.ZipInfo]


HealthCheck = Callable[[Path, dict[str, Any]], bool | None]


def canonical_offline_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """Return the deterministic bytes covered by the release signature.

    The signature object itself is omitted.  Everything else, including the
    distribution marker, compatibility window and every payload file hash and
    size, is signed.  JSON object order and insignificant whitespace therefore
    cannot change the signed meaning.
    """
    if not isinstance(manifest, dict):
        raise ManifestValidationError("offline manifest must be a JSON object")
    content = copy.deepcopy(manifest)
    content.pop("signature", None)
    try:
        return json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ManifestValidationError("offline manifest cannot be canonicalized") from exc


def _decode_ed25519_value(value: Any, *, expected_bytes: int, label: str) -> bytes:
    text = str(value or "").strip()
    if not text:
        raise ManifestValidationError(f"offline manifest {label} is required")
    try:
        decoded = (
            bytes.fromhex(text)
            if re.fullmatch(rf"[0-9a-fA-F]{{{expected_bytes * 2}}}", text)
            else base64.b64decode(text, validate=True)
        )
    except (ValueError, binascii.Error) as exc:
        raise ManifestValidationError(f"offline manifest {label} encoding is invalid") from exc
    if len(decoded) != expected_bytes:
        raise ManifestValidationError(
            f"offline manifest {label} must contain {expected_bytes} bytes"
        )
    return decoded


def verify_offline_manifest_signature(
    manifest: dict[str, Any],
    *,
    allow_test_keys: bool = False,
) -> None:
    """Verify the embedded Ed25519 signature against a pinned trust anchor.

    Production verification is the default and fails closed while no release
    public key is provisioned.  The repository test key is never considered
    unless ``allow_test_keys`` is explicitly true.
    """
    signature = manifest.get("signature") if isinstance(manifest, dict) else None
    if not isinstance(signature, dict):
        raise ManifestValidationError("offline manifest Ed25519 signature is required")
    if signature.get("algorithm") != "ed25519":
        raise ManifestValidationError("offline manifest signature algorithm must be ed25519")
    key_id = str(signature.get("key_id") or "").strip()
    if not key_id or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", key_id):
        raise ManifestValidationError("offline manifest signature key_id is invalid")

    distribution = str(manifest.get("distribution") or "").strip()
    if distribution not in {"production", "development_test"}:
        raise ManifestValidationError("offline manifest distribution must be production or development_test")
    if distribution == "development_test" and not allow_test_keys:
        raise ManifestValidationError("development-test offline bundles require explicit test-key mode")
    if distribution == "production" and key_id in DEVELOPMENT_TEST_PUBLIC_KEYS:
        raise ManifestValidationError("production offline bundles cannot use a development test key")

    trusted_keys = dict(PRODUCTION_OFFLINE_PUBLIC_KEYS)
    if allow_test_keys:
        trusted_keys.update(DEVELOPMENT_TEST_PUBLIC_KEYS)
    encoded_public_key = trusted_keys.get(key_id)
    if encoded_public_key is None:
        if distribution == "production" and not PRODUCTION_OFFLINE_PUBLIC_KEYS:
            raise ManifestValidationError("no production offline release public key is provisioned")
        raise ManifestValidationError(f"offline manifest signing key is not trusted: {key_id}")

    public_key = _decode_ed25519_value(encoded_public_key, expected_bytes=32, label="public key")
    signature_bytes = _decode_ed25519_value(signature.get("value"), expected_bytes=64, label="signature")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise ManifestValidationError("cryptography is required to verify offline release signatures") from exc
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature_bytes,
            canonical_offline_manifest_bytes(manifest),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ManifestValidationError("offline manifest signature verification failed") from exc


def default_install_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "DianAgent"
    return Path.home() / ".dian-agent"


def _version_key(value: Any) -> tuple[tuple[int, ...], int, str]:
    text = str(value or "").strip().lower()
    if text.startswith("v"):
        text = text[1:]
    # Keep the updater, launcher and watchdog on one unambiguous version
    # grammar.  Accept SemVer-like three-part versions only.
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:-([0-9a-z.-]+))?", text)
    if not match:
        raise ManifestValidationError(f"invalid release version: {value}")
    numbers = tuple(int(part) for part in match.group(1).split("."))
    prerelease = match.group(2) or ""
    return numbers, 1 if not prerelease else 0, prerelease


def compare_versions(left: Any, right: Any) -> int:
    left_key = _version_key(left)
    right_key = _version_key(right)
    width = max(len(left_key[0]), len(right_key[0]))
    normalized_left = (left_key[0] + (0,) * (width - len(left_key[0])), left_key[1], left_key[2])
    normalized_right = (right_key[0] + (0,) * (width - len(right_key[0])), right_key[1], right_key[2])
    return (normalized_left > normalized_right) - (normalized_left < normalized_right)


def _safe_bundle_path(value: Any) -> str:
    raw = str(value or "")
    if not raw or "\x00" in raw:
        raise BundleValidationError("bundle contains an empty or invalid path")
    # ZIP paths are POSIX paths.  Treat backslashes as separators as well so a
    # Windows extractor can never reinterpret a path that passed validation.
    normalized = raw.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//"):
        raise BundleValidationError(f"absolute bundle path is forbidden: {raw}")
    parts = PurePosixPath(normalized).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise BundleValidationError(f"path traversal is forbidden: {raw}")
    if any(":" in part for part in parts):
        raise BundleValidationError(f"drive-qualified bundle path is forbidden: {raw}")
    for part in parts:
        if any(ord(character) < 32 for character in part) or part.endswith((" ", ".")):
            raise BundleValidationError(f"Windows-unsafe bundle path is forbidden: {raw}")
        if part.split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES:
            raise BundleValidationError(f"Windows reserved device path is forbidden: {raw}")
    return "/".join(parts)


def _validate_payload_path(path: str) -> None:
    parts = PurePosixPath(path).parts
    root = parts[0].casefold() if parts else ""
    if root in PROTECTED_INSTALL_ROOTS:
        raise BundleValidationError(f"bundle cannot write protected directory: {parts[0]}")
    if root not in ALLOWED_PAYLOAD_ROOTS or len(parts) < 2:
        raise BundleValidationError("payload files must be below program/ or extension/")


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def validate_manifest(manifest: dict[str, Any], *, current_version: str) -> dict[str, Any]:
    """Validate schema, release version, compatibility and file declarations."""
    if not isinstance(manifest, dict):
        raise ManifestValidationError("offline manifest must be a JSON object")
    candidate = copy.deepcopy(manifest)
    try:
        manifest_version = int(candidate.get("manifest_version"))
    except (TypeError, ValueError) as exc:
        raise ManifestValidationError("manifest_version must be an integer") from exc
    if manifest_version not in SUPPORTED_MANIFEST_VERSIONS:
        raise ManifestValidationError("offline manifest version is not supported")
    if candidate.get("product") != "DianAgent":
        raise ManifestValidationError("offline bundle product must be DianAgent")

    release_version = str(candidate.get("version") or "")
    _version_key(release_version)
    _version_key(current_version)
    compatibility = candidate.get("compatibility")
    if not isinstance(compatibility, dict):
        raise ManifestValidationError("compatibility must be an object")
    if compatibility.get("platform") != "windows":
        raise ManifestValidationError("offline bundle platform must be windows")
    minimum = str(compatibility.get("min_current_version") or "")
    _version_key(minimum)
    if compare_versions(current_version, minimum) < 0:
        raise ManifestValidationError(
            f"current version {current_version} is older than supported {minimum}"
        )
    maximum_value = compatibility.get("max_current_version")
    if maximum_value not in (None, ""):
        maximum = str(maximum_value)
        _version_key(maximum)
        if compare_versions(current_version, maximum) > 0:
            raise ManifestValidationError(
                f"current version {current_version} is newer than supported {maximum}"
            )
    if compare_versions(release_version, current_version) <= 0:
        raise ManifestValidationError("offline bundle version must be newer than the current version")

    files = candidate.get("files")
    if not isinstance(files, list) or not files:
        raise ManifestValidationError("manifest files must be a non-empty array")
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise ManifestValidationError("each manifest file must be an object")
        try:
            path = _safe_bundle_path(item.get("path"))
            _validate_payload_path(path)
        except BundleValidationError as exc:
            raise ManifestValidationError(str(exc)) from exc
        folded = path.casefold()
        if folded in seen:
            raise ManifestValidationError(f"duplicate manifest path: {path}")
        seen.add(folded)
        digest = str(item.get("sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ManifestValidationError(f"invalid SHA-256 for {path}")
        size = item.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ManifestValidationError(f"invalid size for {path}")
        item["path"] = path
        item["sha256"] = digest
    return candidate


def _read_current_bytes(install_root: Path) -> bytes | None:
    path = install_root / "current.json"
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ActivationError("current.json cannot be read") from exc


def read_current(install_root: str | Path) -> dict[str, Any] | None:
    path = Path(install_root) / "current.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActivationError("current.json is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ActivationError("current.json schema is invalid")
    version = str(value.get("version") or "")
    _version_key(version)
    expected = f"versions/{version}"
    if str(value.get("version_path") or "").replace("\\", "/") != expected:
        raise ActivationError("current.json version_path is invalid")
    return value


def read_installed_version(install_root: str | Path) -> str:
    """Return the active version for both supported installation layouts.

    Fresh 3.7.x installations use ``app/<version>`` plus
    ``current-version.txt``.  The first offline upgrade switches to the
    versioned ``versions/<version>`` layout and ``current.json``.  Treating a
    fresh installation as 0.0.0 would incorrectly reject every bundle whose
    minimum protocol version is 3.7.0.
    """
    root = Path(install_root)
    current = read_current(root)
    if current is not None:
        return str(current["version"])
    version_path = root / "current-version.txt"
    try:
        version = version_path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return "0.0.0"
    except (OSError, UnicodeDecodeError) as exc:
        raise ActivationError("current-version.txt cannot be read") from exc
    try:
        _version_key(version)
    except ManifestValidationError as exc:
        raise ActivationError("current-version.txt contains an invalid version") from exc
    executable = root / "app" / version / "DianAgent.exe"
    if not executable.is_file():
        raise ActivationError("fresh installation version does not have a matching executable")
    return version


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _pointer_bytes(version: str) -> bytes:
    value = {
        "schema_version": 1,
        "version": version,
        "version_path": f"versions/{version}",
        "activated_at": datetime.now(timezone.utc).isoformat(),
    }
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def inspect_bundle(
    bundle_path: str | Path,
    *,
    current_version: str,
    max_files: int = MAX_BUNDLE_FILES,
    max_uncompressed_bytes: int = MAX_BUNDLE_BYTES,
    allow_test_keys: bool = False,
) -> VerifiedBundle:
    """Validate signature, ZIP metadata, declarations, sizes and hashes."""
    path = Path(bundle_path)
    try:
        archive = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise BundleValidationError("offline bundle is not a readable ZIP file") from exc
    with archive:
        members: dict[str, zipfile.ZipInfo] = {}
        total_bytes = 0
        for info in archive.infolist():
            normalized = _safe_bundle_path(info.filename.rstrip("/"))
            folded = normalized.casefold()
            if folded in members:
                raise BundleValidationError(f"duplicate or case-colliding ZIP path: {normalized}")
            if _is_symlink(info):
                raise BundleValidationError(f"symbolic links are forbidden: {normalized}")
            if info.flag_bits & 0x1:
                raise BundleValidationError("encrypted ZIP members are not supported")
            if info.is_dir():
                if normalized != MANIFEST_NAME:
                    _validate_payload_path(normalized + "/placeholder")
                continue
            members[folded] = info
            total_bytes += info.file_size
            if len(members) > max_files or total_bytes > max_uncompressed_bytes:
                raise BundleValidationError("offline bundle exceeds the safe extraction limit")

        manifest_info = members.get(MANIFEST_NAME.casefold())
        if manifest_info is None or _safe_bundle_path(manifest_info.filename) != MANIFEST_NAME:
            raise BundleValidationError(f"{MANIFEST_NAME} must exist at the ZIP root")
        if manifest_info.file_size > MAX_MANIFEST_BYTES:
            raise BundleValidationError("offline manifest exceeds the safe size limit")
        try:
            manifest_raw = archive.read(manifest_info)
            manifest_value = json.loads(manifest_raw.decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BundleValidationError("offline manifest is not valid UTF-8 JSON") from exc
        # Authenticity is established before any payload is extracted or an
        # installation directory is mutated.  Schema validation follows so
        # callers receive precise compatibility/path errors for signed input.
        verify_offline_manifest_signature(manifest_value, allow_test_keys=allow_test_keys)
        manifest = validate_manifest(manifest_value, current_version=current_version)
        declared = {str(item["path"]).casefold(): item for item in manifest["files"]}
        actual = set(members) - {MANIFEST_NAME.casefold()}
        if actual != set(declared):
            missing = sorted(set(declared) - actual)
            extra = sorted(actual - set(declared))
            details = []
            if missing:
                details.append("missing: " + ", ".join(missing[:5]))
            if extra:
                details.append("undeclared: " + ", ".join(extra[:5]))
            raise BundleValidationError("manifest file list does not match ZIP payload (" + "; ".join(details) + ")")

        for folded, item in declared.items():
            info = members[folded]
            if info.file_size != item["size"]:
                raise BundleValidationError(f"payload size does not match manifest: {item['path']}")
            digest = hashlib.sha256()
            with archive.open(info, "r") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != item["sha256"]:
                raise BundleValidationError(f"payload SHA-256 does not match manifest: {item['path']}")
        return VerifiedBundle(path=path.resolve(), manifest=manifest, members=members)


def _remove_release_dir(path: Path, versions_dir: Path) -> None:
    resolved = path.resolve()
    versions_resolved = versions_dir.resolve()
    if resolved.parent != versions_resolved or not path.name:
        raise ActivationError(f"refusing to remove unsafe release directory: {path}")
    if path.exists():
        shutil.rmtree(path)


def install_bundle(
    bundle_path: str | Path,
    install_root: str | Path | None = None,
    *,
    current_version: str | None = None,
    health_check: HealthCheck | None = None,
    allow_test_keys: bool = False,
) -> dict[str, Any]:
    """Verify, install and atomically activate one offline release.

    ``health_check`` runs after pointer activation.  A false result or raised
    exception restores the exact previous pointer and removes the failed
    release directory.
    """
    root = (Path(install_root) if install_root is not None else default_install_root()).expanduser().resolve()
    existing = read_current(root)
    installed_version = read_installed_version(root)
    if current_version is not None and installed_version != "0.0.0":
        if compare_versions(current_version, installed_version) != 0:
            source = "current.json" if existing is not None else "current-version.txt"
            raise ActivationError(f"explicit current version does not match {source}")
    effective_current = current_version or installed_version
    verified = inspect_bundle(
        bundle_path,
        current_version=effective_current,
        allow_test_keys=allow_test_keys,
    )
    version = str(verified.manifest["version"])

    versions_dir = root / "versions"
    target_dir = versions_dir / version
    versions_dir.mkdir(parents=True, exist_ok=True)
    if versions_dir.resolve() != versions_dir:
        raise ActivationError("versions directory cannot be a symbolic link or junction")
    if target_dir.exists():
        raise ActivationError(f"release version directory already exists: {version}")
    staging_dir = versions_dir / f".staging-{version}-{uuid.uuid4().hex}"
    previous_pointer = _read_current_bytes(root)
    pointer_switched = False
    target_created = False
    stable_extension = root / "extension-current"
    extension_stage = root / f".extension-current-stage-{uuid.uuid4().hex}"
    extension_backup = root / f".extension-current-backup-{uuid.uuid4().hex}"
    extension_switched = False

    try:
        staging_dir.mkdir()
        with zipfile.ZipFile(verified.path, "r") as archive:
            for item in verified.manifest["files"]:
                relative = Path(*PurePosixPath(item["path"]).parts)
                destination = staging_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with archive.open(verified.members[item["path"].casefold()], "r") as source, destination.open("wb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                        digest.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if digest.hexdigest() != item["sha256"] or destination.stat().st_size != item["size"]:
                    raise BundleValidationError(f"payload changed during extraction: {item['path']}")
        _atomic_write(
            staging_dir / MANIFEST_NAME,
            (json.dumps(verified.manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        os.replace(staging_dir, target_dir)
        target_created = True
        extension_source = target_dir / "extension" / "modern"
        if not extension_source.is_dir():
            raise ActivationError("offline bundle does not contain the modern browser extension")
        shutil.copytree(extension_source, extension_stage)
        if stable_extension.exists():
            os.replace(stable_extension, extension_backup)
        os.replace(extension_stage, stable_extension)
        extension_switched = True
        _atomic_write(root / "current.json", _pointer_bytes(version))
        pointer_switched = True

        if health_check is not None:
            result = health_check(target_dir, copy.deepcopy(verified.manifest))
            if result is False:
                raise ActivationError("post-activation health check failed")
        if extension_backup.exists():
            shutil.rmtree(extension_backup)
        return {
            "ok": True,
            "status": "activated",
            "version": version,
            "install_root": str(root.resolve()),
            "version_path": str(target_dir.resolve()),
            "current_path": str((root / "current.json").resolve()),
        }
    except Exception as exc:
        rollback_error: Exception | None = None
        try:
            if pointer_switched:
                if previous_pointer is None:
                    (root / "current.json").unlink(missing_ok=True)
                else:
                    _atomic_write(root / "current.json", previous_pointer)
            if extension_switched:
                if stable_extension.exists():
                    shutil.rmtree(stable_extension)
                if extension_backup.exists():
                    os.replace(extension_backup, stable_extension)
            elif extension_backup.exists() and not stable_extension.exists():
                os.replace(extension_backup, stable_extension)
            if extension_stage.exists():
                shutil.rmtree(extension_stage)
            if target_created:
                _remove_release_dir(target_dir, versions_dir)
            elif staging_dir.exists():
                _remove_release_dir(staging_dir, versions_dir)
        except Exception as rollback_exc:  # Preserve the triggering failure and expose rollback failure.
            rollback_error = rollback_exc
        if rollback_error is not None:
            raise ActivationError(f"upgrade failed and rollback also failed: {rollback_error}") from exc
        if isinstance(exc, OfflineUpgradeError):
            raise
        raise ActivationError(f"offline upgrade failed and was rolled back: {exc}") from exc


def packaged_agent_self_test(release_dir: Path, install_root: Path, timeout_seconds: int = 60) -> bool:
    """Run the newly extracted executable without binding the production port."""
    executable = release_dir / "program" / "DianAgent.exe"
    if not executable.is_file():
        return False
    environment = os.environ.copy()
    environment["DIAN_AGENT_SELF_TEST"] = "1"
    environment["DIAN_AGENT_DATA_DIR"] = str(install_root / "data")
    environment["DIAN_AGENT_LOG_DIR"] = str(install_root / "logs")
    try:
        completed = subprocess.run(
            [str(executable)],
            cwd=str(executable.parent),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _rollback_dir(install_root: Path) -> Path:
    return install_root / ".offline-upgrade-rollback"


def prepare_pending_upgrade(install_root: Path) -> Path:
    """Persist the active pointer and stable extension until real health is confirmed."""
    rollback = _rollback_dir(install_root)
    if rollback.exists():
        raise ActivationError("a previous offline upgrade is awaiting confirm or rollback")
    rollback.mkdir(parents=True)
    try:
        pointer = _read_current_bytes(install_root)
        previous = read_current(install_root) if pointer is not None else None
        if pointer is not None:
            _atomic_write(rollback / "previous-current.json", pointer)
        stable = install_root / "extension-current"
        had_extension = stable.is_dir()
        if had_extension:
            shutil.copytree(stable, rollback / "extension-current")
        state = {
            "schema_version": 1,
            "had_current": pointer is not None,
            "had_extension": had_extension,
            "previous_version": previous.get("version") if previous else read_installed_version(install_root),
            "new_version": None,
        }
        _atomic_write(rollback / "state.json", (json.dumps(state, indent=2, sort_keys=True) + "\n").encode())
    except Exception:
        if rollback.exists():
            shutil.rmtree(rollback)
        raise
    return rollback


def mark_pending_upgrade(install_root: Path, version: str) -> None:
    rollback = _rollback_dir(install_root)
    state_path = rollback / "state.json"
    if not state_path.is_file():
        raise ActivationError("offline rollback state is missing")
    state = _read_pending_state(install_root)
    active = read_current(install_root)
    if active is None or str(active.get("version") or "") != version:
        raise ActivationError("pending upgrade version does not match the active pointer")
    state["new_version"] = version
    _atomic_write(state_path, (json.dumps(state, indent=2, sort_keys=True) + "\n").encode())


def _read_pending_state(install_root: Path, *, require_new_version: bool = False) -> dict[str, Any]:
    rollback = _rollback_dir(install_root)
    state_path = rollback / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ActivationError("offline rollback state is missing") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActivationError("offline rollback state is invalid") from exc
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise ActivationError("offline rollback state schema is invalid")
    if not isinstance(state.get("had_current"), bool) or not isinstance(state.get("had_extension"), bool):
        raise ActivationError("offline rollback state flags are invalid")
    previous_version = str(state.get("previous_version") or "")
    if previous_version:
        try:
            _version_key(previous_version)
        except ManifestValidationError as exc:
            raise ActivationError("offline rollback previous version is invalid") from exc
    new_version = str(state.get("new_version") or "")
    if new_version:
        try:
            _version_key(new_version)
        except ManifestValidationError as exc:
            raise ActivationError("offline rollback target version is invalid") from exc
    elif require_new_version:
        raise ActivationError("offline rollback target version is missing")
    if state["had_current"] and not (rollback / "previous-current.json").is_file():
        raise ActivationError("previous active version pointer is missing; refusing destructive rollback")
    if state["had_extension"] and not (rollback / "extension-current").is_dir():
        raise ActivationError("previous browser extension backup is missing; refusing destructive rollback")
    return state


def _complete_pending_transaction(rollback: Path, label: str) -> None:
    completed = rollback.parent / f".offline-upgrade-{label}-{uuid.uuid4().hex}"
    try:
        os.replace(rollback, completed)
    except OSError as exc:
        raise ActivationError("offline upgrade transaction could not be finalized; retry confirm or rollback") from exc
    try:
        shutil.rmtree(completed)
    except OSError:
        # The active transaction name is already gone, so a harmless cleanup
        # remnant cannot block a later upgrade.
        pass


def confirm_pending_upgrade(install_root: str | Path) -> dict[str, Any]:
    root = Path(install_root)
    rollback = _rollback_dir(root)
    if not rollback.is_dir():
        raise ActivationError("no offline upgrade is awaiting confirmation")
    state = _read_pending_state(root, require_new_version=True)
    current = read_current(root)
    if current is None or str(current.get("version") or "") != str(state["new_version"]):
        raise ActivationError("active version does not match the pending upgrade; refusing confirmation")
    _complete_pending_transaction(rollback, "confirmed")
    return {"ok": True, "status": "confirmed", "version": state.get("new_version")}


def rollback_pending_upgrade(install_root: str | Path) -> dict[str, Any]:
    root = Path(install_root)
    rollback = _rollback_dir(root)
    if not rollback.is_dir():
        raise ActivationError("no offline upgrade is awaiting rollback")
    state = _read_pending_state(root)
    active_before_restore = read_current(root)
    previous_pointer = rollback / "previous-current.json"
    stable = root / "extension-current"
    previous_extension = rollback / "extension-current"
    extension_stage = root / f".extension-rollback-stage-{uuid.uuid4().hex}"
    failed_extension = root / f".extension-failed-{uuid.uuid4().hex}"
    stable_moved = False
    previous_activated = False
    try:
        if state["had_extension"]:
            shutil.copytree(previous_extension, extension_stage)
        if stable.exists():
            os.replace(stable, failed_extension)
            stable_moved = True
        if state["had_extension"]:
            os.replace(extension_stage, stable)
            previous_activated = True
        if state["had_current"]:
            _atomic_write(root / "current.json", previous_pointer.read_bytes())
        else:
            (root / "current.json").unlink(missing_ok=True)
    except Exception as exc:
        try:
            if previous_activated and stable.exists():
                shutil.rmtree(stable)
            if stable_moved and failed_extension.exists():
                os.replace(failed_extension, stable)
            if extension_stage.exists():
                shutil.rmtree(extension_stage)
        except Exception as restore_exc:
            raise ActivationError(f"rollback failed and extension restore also failed: {restore_exc}") from exc
        raise ActivationError(f"rollback failed before the previous pointer was activated: {exc}") from exc
    if failed_extension.exists():
        try:
            shutil.rmtree(failed_extension)
        except OSError:
            pass
    new_version = str(state.get("new_version") or "")
    if not new_version and active_before_restore is not None:
        active_version = str(active_before_restore.get("version") or "")
        if active_version != str(state.get("previous_version") or ""):
            new_version = active_version
    if new_version:
        target = root / "versions" / new_version
        if target.exists():
            try:
                _remove_release_dir(target, root / "versions")
            except OSError:
                # The failed EXE may still hold a Windows file lock.  It is no
                # longer active; leave the orphan for later cleanup rather than
                # turning a successful pointer rollback into a false failure.
                pass
    _complete_pending_transaction(rollback, "rolled-back")
    current = read_current(root)
    return {"ok": True, "status": "rolled_back", "version": current.get("version") if current else None}


MAINTENANCE_ROOT_PREFIXES = (
    ".offline-upgrade-confirmed-",
    ".offline-upgrade-rolled-back-",
    ".extension-failed-",
    ".extension-rollback-stage-",
    ".extension-current-stage-",
    ".extension-current-backup-",
    ".tools-stage-",
    ".tools-backup-",
)


def transaction_status(install_root: str | Path) -> dict[str, Any]:
    root = Path(install_root).expanduser().resolve()
    rollback = _rollback_dir(root)
    current = read_current(root)
    result: dict[str, Any] = {
        "ok": True,
        "pending": rollback.is_dir(),
        "current_version": current.get("version") if current else read_installed_version(root),
        "transaction_path": str(rollback),
    }
    if rollback.is_dir():
        state = _read_pending_state(root)
        result.update(
            {
                "previous_version": state.get("previous_version"),
                "pending_version": state.get("new_version"),
                "had_current": state.get("had_current"),
                "had_extension": state.get("had_extension"),
            }
        )
    return result


def _read_local_health(health_url: str, timeout_seconds: float = 3.0) -> dict[str, Any] | None:
    parsed = urlparse(str(health_url or ""))
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.username or parsed.password:
        raise ActivationError("recovery health URL must use local HTTP")
    try:
        with urlopen(Request(health_url, headers={"Accept": "application/json"}), timeout=timeout_seconds) as response:
            raw = response.read(65_537)
        if len(raw) > 65_536:
            raise ActivationError("local health response is too large")
        value = json.loads(raw.decode("utf-8"))
    except ActivationError:
        raise
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def recover_pending_upgrade(
    install_root: str | Path,
    *,
    health_url: str,
    rollback_if_unhealthy: bool = False,
) -> dict[str, Any]:
    """Resolve an interrupted transaction using exact local health evidence.

    A healthy active version is confirmed.  If power was lost before the
    target version was written to state, the target can only be repaired and
    confirmed when ``/health`` reports that exact active pointer.  An
    unhealthy version is rolled back only when the caller explicitly states
    that a real startup attempt has already failed.
    """
    root = Path(install_root).expanduser().resolve()
    rollback = _rollback_dir(root)
    if not rollback.is_dir():
        return {"ok": True, "status": "no_pending_transaction"}
    state = _read_pending_state(root)
    current = read_current(root)
    active_version = str(current.get("version") if current else read_installed_version(root))
    health = _read_local_health(health_url)
    healthy = bool(
        health
        and health.get("status") == "ok"
        and str(health.get("version") or "") == active_version
    )
    if healthy:
        pending_version = str(state.get("new_version") or "")
        previous_version = str(state.get("previous_version") or "")
        if not pending_version:
            if current is None or active_version == previous_version:
                result = rollback_pending_upgrade(root)
                result["status"] = "previous_version_healthy_transaction_closed"
                return result
            mark_pending_upgrade(root, active_version)
            pending_version = active_version
        if current is None or pending_version != active_version:
            raise ActivationError("healthy service does not match the pending upgrade pointer")
        result = confirm_pending_upgrade(root)
        result["status"] = "healthy_upgrade_confirmed"
        return result
    if not rollback_if_unhealthy:
        return {
            "ok": True,
            "status": "pending_health_unconfirmed",
            "active_version": active_version,
            "rollback_performed": False,
        }
    result = rollback_pending_upgrade(root)
    result["status"] = "unhealthy_upgrade_rolled_back"
    result["rollback_performed"] = True
    return result


def _latest_tree_mtime(path: Path) -> float:
    latest = path.stat().st_mtime
    for base, directories, files in os.walk(path, followlinks=False):
        for name in (*directories, *files):
            child = Path(base) / name
            try:
                latest = max(latest, child.lstat().st_mtime)
            except OSError:
                # A disappearing or locked child makes the tree ineligible for
                # deletion during this maintenance pass.
                return time.time()
    return latest


def _safe_maintenance_remove(path: Path, parent: Path) -> None:
    resolved_parent = parent.resolve()
    if path.parent.resolve() != resolved_parent or not path.name or path.is_symlink():
        raise ActivationError(f"refusing to remove unsafe maintenance directory: {path}")
    shutil.rmtree(path)


def cleanup_install_root(
    install_root: str | Path,
    *,
    dry_run: bool = True,
    keep_recent_versions: int = 2,
    min_age_hours: float = 168.0,
    now: float | None = None,
) -> dict[str, Any]:
    """Conservatively prune offline-upgrade leftovers.

    The active version, every version referenced by a pending transaction and
    the fresh ``app/<version>`` layout are never deletion candidates.  Locked
    Windows directories are reported and skipped.
    """
    if keep_recent_versions < 0:
        raise ActivationError("keep_recent_versions cannot be negative")
    if min_age_hours < 0:
        raise ActivationError("min_age_hours cannot be negative")
    root = Path(install_root).expanduser().resolve()
    versions = root / "versions"
    current = read_current(root)
    protected_versions: set[str] = set()
    if current is not None:
        protected_versions.add(str(current["version"]))
    pending = _rollback_dir(root).is_dir()
    pending_state_valid = True
    if pending:
        try:
            state = _read_pending_state(root)
            protected_versions.update(
                str(value) for value in (state.get("previous_version"), state.get("new_version")) if value
            )
        except ActivationError:
            # If the only recovery state is damaged, keep every installed
            # version.  Cleanup must never make manual recovery harder.
            pending_state_valid = False

    version_dirs: list[tuple[tuple[tuple[int, ...], int, str], Path]] = []
    if versions.is_dir() and not versions.is_symlink():
        for child in versions.iterdir():
            if not child.is_dir() or child.is_symlink() or child.name.startswith("."):
                continue
            try:
                key = _version_key(child.name)
            except ManifestValidationError:
                continue
            version_dirs.append((key, child))
    for _, child in sorted(version_dirs, key=lambda item: item[0], reverse=True)[:keep_recent_versions]:
        protected_versions.add(child.name)
    if pending and not pending_state_valid:
        protected_versions.update(child.name for _, child in version_dirs)

    candidates: list[tuple[Path, Path, str]] = []
    if versions.is_dir() and not versions.is_symlink():
        for child in versions.iterdir():
            if not child.is_dir() or child.is_symlink():
                continue
            if child.name.startswith(".staging-"):
                candidates.append((child, versions, "stale_staging"))
            elif child.name not in protected_versions:
                try:
                    _version_key(child.name)
                except ManifestValidationError:
                    continue
                candidates.append((child, versions, "unreferenced_version"))
    for child in root.iterdir() if root.is_dir() else ():
        if child.is_dir() and not child.is_symlink() and child.name.startswith(MAINTENANCE_ROOT_PREFIXES):
            candidates.append((child, root, "completed_transaction_or_stage"))

    timestamp = time.time() if now is None else float(now)
    minimum_age_seconds = min_age_hours * 3600
    records: list[dict[str, Any]] = []
    for path, parent, reason in sorted(candidates, key=lambda item: str(item[0]).casefold()):
        record = {"path": str(path), "reason": reason, "action": "skipped"}
        if pending and reason in {"stale_staging", "completed_transaction_or_stage"}:
            record["detail"] = "active offline upgrade transaction"
            records.append(record)
            continue
        try:
            age_seconds = max(0.0, timestamp - _latest_tree_mtime(path))
        except OSError as exc:
            record["detail"] = f"cannot inspect: {exc}"
            records.append(record)
            continue
        record["age_hours"] = round(age_seconds / 3600, 2)
        if age_seconds < minimum_age_seconds:
            record["detail"] = "younger than minimum age"
            records.append(record)
            continue
        if dry_run:
            record["action"] = "would_delete"
            records.append(record)
            continue
        try:
            _safe_maintenance_remove(path, parent)
            record["action"] = "deleted"
        except (OSError, ActivationError) as exc:
            record["detail"] = f"locked or unsafe: {exc}"
        records.append(record)

    result = {
        "ok": True,
        "dry_run": dry_run,
        "pending_transaction": pending,
        "pending_state_valid": pending_state_valid,
        "protected_versions": sorted(protected_versions, key=_version_key, reverse=True),
        "records": records,
        "summary": {
            "candidates": len(records),
            "deleted": sum(item["action"] == "deleted" for item in records),
            "would_delete": sum(item["action"] == "would_delete" for item in records),
            "skipped": sum(item["action"] == "skipped" for item in records),
        },
    }
    log_dir = root / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "offline-upgrade-maintenance.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), **result}, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        result["log_warning"] = "maintenance audit log could not be written"
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify and install DianAgent offline release bundles")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="verify a bundle without installing it")
    inspect_parser.add_argument("bundle")
    inspect_parser.add_argument("--current-version", required=True)
    inspect_parser.add_argument("--allow-test-keys", action="store_true", help="development tests only")
    install_parser = subparsers.add_parser("install", help="verify, install and activate a bundle")
    install_parser.add_argument("bundle")
    install_parser.add_argument("--install-root", default=str(default_install_root()))
    install_parser.add_argument("--current-version")
    install_parser.add_argument("--allow-test-keys", action="store_true", help="development tests only")
    current_parser = subparsers.add_parser("current", help="show the active version pointer")
    current_parser.add_argument("--install-root", default=str(default_install_root()))
    confirm_parser = subparsers.add_parser("confirm", help="confirm real service health and remove rollback state")
    confirm_parser.add_argument("--install-root", default=str(default_install_root()))
    rollback_parser = subparsers.add_parser("rollback", help="restore the previous program pointer and extension")
    rollback_parser.add_argument("--install-root", default=str(default_install_root()))
    status_parser = subparsers.add_parser("transaction-status", help="show an interrupted upgrade transaction")
    status_parser.add_argument("--install-root", default=str(default_install_root()))
    recover_parser = subparsers.add_parser("recover", help="resolve an interrupted upgrade from local health evidence")
    recover_parser.add_argument("--install-root", default=str(default_install_root()))
    recover_parser.add_argument("--health-url", required=True)
    recover_parser.add_argument("--rollback-if-unhealthy", action="store_true")
    cleanup_parser = subparsers.add_parser("cleanup", help="conservatively inspect or remove stale upgrade files")
    cleanup_parser.add_argument("--install-root", default=str(default_install_root()))
    cleanup_parser.add_argument("--keep-recent", type=int, default=2)
    cleanup_parser.add_argument("--min-age-hours", type=float, default=168.0)
    cleanup_parser.add_argument("--apply", action="store_true", help="delete eligible directories; default is dry-run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            result: Any = inspect_bundle(
                args.bundle,
                current_version=args.current_version,
                allow_test_keys=args.allow_test_keys,
            ).manifest
        elif args.command == "install":
            install_root = Path(args.install_root)
            prepare_pending_upgrade(install_root)
            try:
                result = install_bundle(
                    args.bundle,
                    install_root,
                    current_version=args.current_version,
                    health_check=lambda release, _manifest: packaged_agent_self_test(release, install_root),
                    allow_test_keys=args.allow_test_keys,
                )
                mark_pending_upgrade(install_root, str(result["version"]))
                result["status"] = "awaiting_real_health_confirmation"
            except Exception:
                rollback = _rollback_dir(install_root)
                if rollback.exists():
                    try:
                        rollback_pending_upgrade(install_root)
                    except Exception as rollback_exc:
                        raise ActivationError(
                            f"offline upgrade failed and persisted rollback also failed: {rollback_exc}"
                        ) from rollback_exc
                raise
        elif args.command == "confirm":
            result = confirm_pending_upgrade(args.install_root)
        elif args.command == "rollback":
            result = rollback_pending_upgrade(args.install_root)
        elif args.command == "transaction-status":
            result = transaction_status(args.install_root)
        elif args.command == "recover":
            result = recover_pending_upgrade(
                args.install_root,
                health_url=args.health_url,
                rollback_if_unhealthy=args.rollback_if_unhealthy,
            )
        elif args.command == "cleanup":
            result = cleanup_install_root(
                args.install_root,
                dry_run=not args.apply,
                keep_recent_versions=args.keep_recent,
                min_age_hours=args.min_age_hours,
            )
        else:
            result = read_current(args.install_root)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except OfflineUpgradeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
