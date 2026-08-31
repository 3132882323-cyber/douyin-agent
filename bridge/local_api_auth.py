"""Origin-bound authentication for the local Dian Agent HTTP service.

The browser extension never receives the installation secret.  An installer-
approved extension origin exchanges its public extension ID for a short-lived,
HMAC-signed session.  Local command-line clients can use the helper in this
module to mint a short-lived internal session after reading the user-owned
configuration directory; they do not need to impersonate a browser Origin.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


AUTH_HEADER = "X-Dian-Agent-Token"
PROTOCOL_HEADER = "X-Dian-Agent"
AUTH_SCHEMA_VERSION = 1
SESSION_TOKEN_VERSION = "v1"
SESSION_AUDIENCE = "dian-agent-local-api"
DEFAULT_SESSION_TTL_SECONDS = 8 * 60 * 60
MAX_SESSION_TTL_SECONDS = 24 * 60 * 60
INTERNAL_CLIENT_SUBJECT = "local-client"
_EXTENSION_ID = re.compile(r"^[a-p]{32}$")
_EXTENSION_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_auth_lock = threading.RLock()


class LocalApiAuthError(ValueError):
    """A safe, machine-readable local authentication failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _state_lock_path(store_root: str | Path) -> Path:
    return Path(store_root) / "config" / ".local-api-state.lock"


@contextmanager
def _interprocess_state_lock(store_root: str | Path) -> Iterator[None]:
    """Serialize auth/trust transactions across Agent and installer processes."""

    path = _state_lock_path(store_root)
    try:
        if path.parent.is_symlink() or (
            path.parent.exists() and not path.parent.is_dir()
        ):
            raise OSError("unsafe local API lock directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise OSError("local API lock is a symbolic link")
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        raise LocalApiAuthError(
            "agent_auth_lock_unavailable",
            "The local Agent authentication state is busy or unavailable.",
        ) from error

    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("local API lock is not a regular file")
        os.chmod(path, 0o600)
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as error:
        os.close(descriptor)
        raise LocalApiAuthError(
            "agent_auth_lock_unavailable",
            "The local Agent authentication state is busy or unavailable.",
        ) from error

    try:
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise LocalApiAuthError("agent_session_invalid", "The local Agent session is invalid.") from exc
    if _encode(decoded) != value:
        raise LocalApiAuthError("agent_session_invalid", "The local Agent session encoding is invalid.")
    return decoded


def _auth_path(store_root: str | Path) -> Path:
    return Path(store_root) / "config" / "local_api_auth.json"


def _trust_path(store_root: str | Path) -> Path:
    return Path(store_root) / "config" / "trusted_extension_ids.json"


def _path_exists(path: Path) -> bool:
    """Return True for every directory entry, including a broken symlink."""

    return path.exists() or path.is_symlink()


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_json_create(path: Path, value: dict[str, Any]) -> bool:
    """Install a complete JSON record only when no record exists yet."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        try:
            # Linking a fully flushed same-directory file is an atomic
            # create-if-absent operation.  It prevents two Agent processes
            # from each returning a different first-install signing secret.
            os.link(temporary, path)
        except FileExistsError:
            return False
        os.chmod(path, 0o600)
        return True
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _valid_config(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("schema_version") != AUTH_SCHEMA_VERSION:
        return False
    install_id = str(value.get("install_id") or "")
    if len(install_id) != 32 or any(character not in "0123456789abcdef" for character in install_id):
        return False
    try:
        secret = _decode(str(value.get("secret") or ""))
    except LocalApiAuthError:
        return False
    return len(secret) == 32


def _load_existing_auth(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise LocalApiAuthError(
            "agent_install_auth_corrupt",
            "The local Agent authentication record cannot be a symbolic link.",
        )
    if not path.exists():
        return None
    if not path.is_file():
        raise LocalApiAuthError(
            "agent_install_auth_corrupt",
            "The local Agent authentication record must be a regular file.",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalApiAuthError(
            "agent_install_auth_corrupt",
            "The local Agent authentication record is unreadable; repair the installation.",
        ) from exc
    if not _valid_config(value):
        raise LocalApiAuthError(
            "agent_install_auth_corrupt",
            "The local Agent authentication record is invalid; repair the installation.",
        )
    return value


def _new_install_auth() -> dict[str, Any]:
    return {
        "schema_version": AUTH_SCHEMA_VERSION,
        "install_id": uuid.uuid4().hex,
        "secret": _encode(secrets.token_bytes(32)),
        "created_at": int(time.time()),
    }


def _load_trusted_extension_registry(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise LocalApiAuthError(
            "agent_extension_trust_corrupt",
            "The extension trust registry cannot be a symbolic link.",
        )
    if not path.exists():
        return {"schema_version": AUTH_SCHEMA_VERSION, "extension_ids": []}
    if not path.is_file():
        raise LocalApiAuthError(
            "agent_extension_trust_corrupt",
            "The extension trust registry must be a regular file.",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalApiAuthError(
            "agent_extension_trust_corrupt",
            "The extension trust registry is unreadable; repair the installation.",
        ) from exc
    extension_ids = value.get("extension_ids") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != AUTH_SCHEMA_VERSION
        or not isinstance(extension_ids, list)
        or any(not isinstance(item, str) or not _EXTENSION_ID.fullmatch(item) for item in extension_ids)
    ):
        raise LocalApiAuthError(
            "agent_extension_trust_corrupt",
            "The extension trust registry is invalid; repair the installation.",
        )
    return value


def read_trusted_extension_ids(store_root: str | Path) -> frozenset[str]:
    """Read only a fully valid installer-managed extension trust registry.

    Callers that merely decide whether an Origin is trusted must fail closed:
    a malformed document, wrong schema, non-regular file or symlink contributes
    no IDs.  Provisioning and explicit repair use the stricter private loader so
    they can surface the exact error instead of hiding damaged state.
    """

    with _auth_lock:
        try:
            registry = _load_trusted_extension_registry(_trust_path(store_root))
        except LocalApiAuthError:
            return frozenset()
    return frozenset(str(item) for item in registry["extension_ids"])


def chromium_extension_id(manifest_path: str | Path) -> str:
    """Derive Chromium's stable extension ID from a manifest public key."""

    path = Path(manifest_path)
    if path.is_symlink() or not path.is_file():
        raise LocalApiAuthError(
            "agent_extension_manifest_invalid",
            "The extension manifest must be a regular, non-symbolic-link file.",
        )
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalApiAuthError(
            "agent_extension_manifest_invalid", "The extension manifest is unreadable or invalid."
        ) from exc
    key = manifest.get("key") if isinstance(manifest, dict) else None
    if not isinstance(key, str) or not key:
        raise LocalApiAuthError(
            "agent_extension_manifest_invalid", "The extension manifest is missing its fixed public key."
        )
    try:
        public_key = base64.b64decode(key.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise LocalApiAuthError(
            "agent_extension_manifest_invalid", "The extension manifest public key is not valid Base64."
        ) from exc
    if not public_key:
        raise LocalApiAuthError(
            "agent_extension_manifest_invalid", "The extension manifest public key is empty."
        )
    return "".join(
        chr(ord("a") + nibble)
        for byte in hashlib.sha256(public_key).digest()[:16]
        for nibble in ((byte >> 4) & 0x0F, byte & 0x0F)
    )


def _provision_local_api_trust_locked(
    store_root: str | Path,
    extension_id: str,
) -> dict[str, Any]:
    """Provision auth/trust while both the thread and process locks are held."""

    auth_path = _auth_path(store_root)
    trust_path = _trust_path(store_root)
    # Re-read both records only after acquiring the common process lock.  A
    # trust update and first-install auth creation are one logical transaction.
    existing_auth = _load_existing_auth(auth_path)
    trust = _load_trusted_extension_registry(trust_path)
    merged_ids = sorted(set([*trust["extension_ids"], extension_id]))
    trust_changed = not trust_path.exists() or merged_ids != trust["extension_ids"]
    trust["schema_version"] = AUTH_SCHEMA_VERSION
    trust["extension_ids"] = merged_ids

    if existing_auth is None:
        candidate = _new_install_auth()
        if _atomic_json_create(auth_path, candidate):
            existing_auth = candidate
        else:
            existing_auth = _load_existing_auth(auth_path)
            if existing_auth is None:
                raise LocalApiAuthError(
                    "agent_install_auth_corrupt",
                    "The local Agent authentication record could not be installed safely.",
                )
    else:
        os.chmod(auth_path, 0o600)
    if trust_changed:
        _atomic_json_write(trust_path, trust)
    else:
        os.chmod(trust_path, 0o600)
    return {
        "extension_id": extension_id,
        "install_id": str(existing_auth["install_id"]),
        "trusted_extension_ids": list(merged_ids),
        "auth_path": str(auth_path),
        "trust_path": str(trust_path),
    }


def provision_local_api_trust(store_root: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    """Atomically provision a fixed extension ID and a 256-bit install secret.

    Both existing records are validated before either is changed. Upgrades
    preserve the credential byte-for-byte and retain every approved extension
    ID; corrupt state stops installation instead of being silently replaced.
    """

    extension_id = chromium_extension_id(manifest_path)
    with _auth_lock:
        with _interprocess_state_lock(store_root):
            return _provision_local_api_trust_locked(store_root, extension_id)


def _validate_repair_paths(store_root: str | Path) -> Path:
    """Validate repair-owned directories without creating a backup record."""

    install_root = Path(store_root)
    if install_root.is_symlink() or (install_root.exists() and not install_root.is_dir()):
        raise LocalApiAuthError(
            "agent_repair_path_unsafe",
            "The local Agent installation root is not a safe regular directory.",
        )
    config_root = install_root / "config"
    if config_root.is_symlink() or (config_root.exists() and not config_root.is_dir()):
        raise LocalApiAuthError(
            "agent_repair_path_unsafe",
            "The local Agent config directory is not a safe regular directory.",
        )
    backup_root = config_root / "repair-backup"
    if backup_root.is_symlink() or (backup_root.exists() and not backup_root.is_dir()):
        raise LocalApiAuthError(
            "agent_repair_path_unsafe",
            "The local Agent repair backup path is not a safe regular directory.",
        )
    return backup_root


def _repair_backup_root(store_root: str | Path) -> Path:
    """Create a same-filesystem, non-symlink backup root for explicit repair."""

    backup_root = _validate_repair_paths(store_root)
    backup_root.parent.mkdir(parents=True, exist_ok=True)
    backup_root.mkdir(mode=0o700, parents=False, exist_ok=True)
    os.chmod(backup_root, 0o700)
    return backup_root


def _remove_generated_repair_file(path: Path) -> None:
    """Remove only a regular file generated by this repair transaction."""

    if not _path_exists(path):
        return
    if path.is_symlink() or not path.is_file():
        raise OSError(f"unsafe repair rollback target: {path}")
    path.unlink()


def repair_local_api_trust(store_root: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    """Explicitly repair local API credentials after preserving damaged state.

    A healthy installation follows the ordinary provisioning path byte-for-
    byte, so running Repair repeatedly is idempotent and never rotates an
    active secret.  Only a detected corrupt authentication/trust record enters
    the destructive branch.  That branch atomically moves both current records
    into ``config/repair-backup/<timestamp>-<nonce>``, rotates the installation
    identity and rebuilds trust solely from the manifest's fixed public key.
    """

    # Validate both the caller-controlled manifest and the owned config parent
    # before changing or backing up any existing state.
    extension_id = chromium_extension_id(manifest_path)
    _validate_repair_paths(store_root)
    with _auth_lock, _interprocess_state_lock(store_root):
        try:
            provisioned = _provision_local_api_trust_locked(store_root, extension_id)
        except LocalApiAuthError as error:
            if error.code not in {"agent_install_auth_corrupt", "agent_extension_trust_corrupt"}:
                raise
            repair_reason = error.code
        else:
            return {
                **provisioned,
                "repaired": False,
                "rotated": False,
                "repaired_files": [],
                "backup_path": None,
                "repair_reason": None,
            }

        auth_path = _auth_path(store_root)
        trust_path = _trust_path(store_root)
        backup_root = _repair_backup_root(store_root)
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        suffix = secrets.token_hex(4)
        final_backup = backup_root / f"{timestamp}-{suffix}"
        final_backup.mkdir(mode=0o700)
        os.chmod(final_backup, 0o700)

        moved: list[tuple[Path, Path]] = []
        generated: list[Path] = []
        try:
            for original in (auth_path, trust_path):
                if not _path_exists(original):
                    continue
                backup = final_backup / original.name
                os.replace(original, backup)
                moved.append((original, backup))

            new_auth = _new_install_auth()
            new_trust = {
                "schema_version": AUTH_SCHEMA_VERSION,
                "extension_ids": [extension_id],
            }
            _atomic_json_write(auth_path, new_auth)
            generated.append(auth_path)
            _atomic_json_write(trust_path, new_trust)
            generated.append(trust_path)
            _atomic_json_write(
                final_backup / "repair-receipt.json",
                {
                    "schema_version": AUTH_SCHEMA_VERSION,
                    "repaired_at": int(time.time()),
                    "repair_reason": repair_reason,
                    "repaired_files": [path.name for path, _backup in moved],
                    "extension_id": extension_id,
                    "credentials_rotated": True,
                },
            )
        except OSError as error:
            # Best-effort transaction rollback. Never claim success or discard
            # the only preserved copy if an unexpected filesystem node appears.
            rollback_errors: list[str] = []
            for path in reversed(generated):
                try:
                    _remove_generated_repair_file(path)
                except OSError as rollback_error:
                    rollback_errors.append(str(rollback_error))
            for original, backup in reversed(moved):
                try:
                    if _path_exists(backup) and not _path_exists(original):
                        os.replace(backup, original)
                except OSError as rollback_error:
                    rollback_errors.append(str(rollback_error))
            message = "The local Agent authentication repair could not be completed."
            if rollback_errors:
                message += " Preserved backup state requires manual recovery."
            raise LocalApiAuthError("agent_repair_failed", message) from error

    return {
        "extension_id": extension_id,
        "install_id": str(new_auth["install_id"]),
        "trusted_extension_ids": [extension_id],
        "auth_path": str(auth_path),
        "trust_path": str(trust_path),
        "repaired": True,
        "rotated": True,
        "repaired_files": [path.name for path, _backup in moved],
        "backup_path": str(final_backup),
        "repair_reason": repair_reason,
    }


def ensure_install_auth(store_root: str | Path) -> dict[str, Any]:
    """Load or create the user-owned 256-bit installation secret.

    Corrupt credentials are never silently replaced because doing so would
    invalidate every active session and could hide disk tampering.  Installers
    create this record before the Agent starts; source checkouts and tests get
    the same fail-closed creation path on first use.
    """

    path = _auth_path(store_root)
    with _auth_lock:
        with _interprocess_state_lock(store_root):
            value = _load_existing_auth(path)
            if value is not None:
                return value
            candidate = _new_install_auth()
            if _atomic_json_create(path, candidate):
                return candidate
            value = _load_existing_auth(path)
            if value is None:
                raise LocalApiAuthError(
                    "agent_install_auth_corrupt",
                    "The local Agent authentication record could not be installed safely.",
                )
            return value


def _secret(value: dict[str, Any]) -> bytes:
    secret = _decode(str(value.get("secret") or ""))
    if len(secret) != 32:
        raise LocalApiAuthError("agent_install_auth_corrupt", "The local Agent authentication secret is invalid.")
    return secret


def issue_session_token(
    store_root: str | Path,
    subject: str,
    *,
    extension_version: str | None = None,
    now: int | None = None,
    ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
) -> dict[str, Any]:
    normalized_subject = str(subject or "").strip().lower()
    if normalized_subject != INTERNAL_CLIENT_SUBJECT and not _EXTENSION_ID.fullmatch(normalized_subject):
        raise LocalApiAuthError("agent_session_subject_invalid", "The local Agent session subject is invalid.")
    normalized_extension_version = str(extension_version or "").strip()
    if normalized_subject == INTERNAL_CLIENT_SUBJECT:
        if normalized_extension_version:
            raise LocalApiAuthError(
                "agent_session_extension_version_invalid",
                "An internal local Agent session must not claim a browser extension version.",
            )
    elif (
        len(normalized_extension_version) > 64
        or not _EXTENSION_VERSION.fullmatch(normalized_extension_version)
    ):
        raise LocalApiAuthError(
            "agent_session_extension_version_required",
            "A valid browser extension version is required for this local Agent session.",
        )
    issued_at = int(time.time() if now is None else now)
    try:
        ttl = max(60, min(int(ttl_seconds), MAX_SESSION_TTL_SECONDS))
    except (TypeError, ValueError) as exc:
        raise LocalApiAuthError(
            "agent_session_lifetime_invalid", "The local Agent session lifetime is invalid."
        ) from exc
    config = ensure_install_auth(store_root)
    payload = {
        "a": SESSION_AUDIENCE,
        "v": AUTH_SCHEMA_VERSION,
        "i": str(config["install_id"]),
        "s": normalized_subject,
        "iat": issued_at,
        "exp": issued_at + ttl,
        "n": _encode(secrets.token_bytes(16)),
    }
    if normalized_subject != INTERNAL_CLIENT_SUBJECT:
        payload["ev"] = normalized_extension_version
    encoded_payload = _encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _encode(hmac.new(_secret(config), encoded_payload.encode("ascii"), hashlib.sha256).digest())
    result = {
        "access_token": f"{SESSION_TOKEN_VERSION}.{encoded_payload}.{signature}",
        "token_type": "DianAgent",
        "expires_in": ttl,
        "expires_at": payload["exp"],
        "install_id": payload["i"],
        "subject": normalized_subject,
    }
    if normalized_subject != INTERNAL_CLIENT_SUBJECT:
        result["extension_version"] = normalized_extension_version
    return result


def issue_internal_session_token(
    store_root: str | Path,
    *,
    now: int | None = None,
    ttl_seconds: int = 15 * 60,
) -> dict[str, Any]:
    """Issue a token for an official same-user CLI or maintenance client."""

    return issue_session_token(
        store_root,
        INTERNAL_CLIENT_SUBJECT,
        now=now,
        ttl_seconds=ttl_seconds,
    )


def validate_session_token(
    store_root: str | Path,
    token: str,
    *,
    expected_subject: str | None = None,
    expected_extension_version: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    raw = str(token or "").strip()
    if not raw:
        raise LocalApiAuthError("agent_session_required", "A local Agent session is required.")
    if len(raw) > 4096:
        raise LocalApiAuthError("agent_session_invalid", "The local Agent session is invalid.")
    parts = raw.split(".")
    if len(parts) != 3 or parts[0] != SESSION_TOKEN_VERSION:
        raise LocalApiAuthError("agent_session_invalid", "The local Agent session is invalid.")
    config = ensure_install_auth(store_root)
    try:
        signed_payload = parts[1].encode("ascii")
    except UnicodeEncodeError as exc:
        raise LocalApiAuthError("agent_session_invalid", "The local Agent session is invalid.") from exc
    expected_signature = hmac.new(_secret(config), signed_payload, hashlib.sha256).digest()
    supplied_signature = _decode(parts[2])
    if not hmac.compare_digest(expected_signature, supplied_signature):
        raise LocalApiAuthError("agent_session_invalid", "The local Agent session is invalid.")
    try:
        payload = json.loads(_decode(parts[1]).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalApiAuthError("agent_session_invalid", "The local Agent session is invalid.") from exc
    if not isinstance(payload, dict) or str(payload.get("i") or "") != str(config["install_id"]):
        raise LocalApiAuthError("agent_session_invalid", "The local Agent session belongs to another installation.")
    if payload.get("v") != AUTH_SCHEMA_VERSION or str(payload.get("a") or "") != SESSION_AUDIENCE:
        raise LocalApiAuthError("agent_session_invalid", "The local Agent session has the wrong audience.")
    subject = str(payload.get("s") or "").strip().lower()
    if subject != INTERNAL_CLIENT_SUBJECT and not _EXTENSION_ID.fullmatch(subject):
        raise LocalApiAuthError("agent_session_invalid", "The local Agent session subject is invalid.")
    expected = str(expected_subject or "").strip().lower()
    if expected and not hmac.compare_digest(subject, expected):
        raise LocalApiAuthError("agent_session_invalid", "The local Agent session belongs to another client.")
    token_extension_version = str(payload.get("ev") or "").strip()
    if subject == INTERNAL_CLIENT_SUBJECT:
        if token_extension_version:
            raise LocalApiAuthError(
                "agent_session_invalid",
                "The local Agent session contains an invalid browser version binding.",
            )
    else:
        expected_version = str(expected_extension_version or "").strip()
        if (
            not expected_version
            or len(expected_version) > 64
            or not _EXTENSION_VERSION.fullmatch(expected_version)
            or len(token_extension_version) > 64
            or not _EXTENSION_VERSION.fullmatch(token_extension_version)
            or not hmac.compare_digest(token_extension_version, expected_version)
        ):
            raise LocalApiAuthError(
                "agent_session_extension_version_mismatch",
                "The local Agent session belongs to another extension version.",
            )
    try:
        issued_at = int(payload.get("iat"))
        expires_at = int(payload.get("exp"))
    except (TypeError, ValueError) as exc:
        raise LocalApiAuthError("agent_session_invalid", "The local Agent session timestamps are invalid.") from exc
    current = int(time.time() if now is None else now)
    try:
        nonce = _decode(str(payload.get("n") or ""))
    except LocalApiAuthError as exc:
        raise LocalApiAuthError("agent_session_invalid", "The local Agent session nonce is invalid.") from exc
    if len(nonce) != 16:
        raise LocalApiAuthError("agent_session_invalid", "The local Agent session nonce is invalid.")
    if issued_at > current + 60:
        raise LocalApiAuthError("agent_session_invalid", "The local Agent session was issued in the future.")
    if expires_at <= current:
        raise LocalApiAuthError("agent_session_expired", "The local Agent session has expired.")
    if issued_at < 0 or expires_at <= issued_at or expires_at - issued_at > MAX_SESSION_TTL_SECONDS:
        raise LocalApiAuthError("agent_session_invalid", "The local Agent session lifetime is invalid.")
    result = {
        "valid": True,
        "subject": subject,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "install_id": str(payload["i"]),
        "token_id": hashlib.sha256(nonce).hexdigest()[:24],
    }
    if subject != INTERNAL_CLIENT_SUBJECT:
        result["extension_version"] = token_extension_version
    return result


__all__ = [
    "AUTH_HEADER",
    "DEFAULT_SESSION_TTL_SECONDS",
    "INTERNAL_CLIENT_SUBJECT",
    "LocalApiAuthError",
    "chromium_extension_id",
    "ensure_install_auth",
    "issue_internal_session_token",
    "issue_session_token",
    "provision_local_api_trust",
    "read_trusted_extension_ids",
    "repair_local_api_trust",
    "validate_session_token",
]
