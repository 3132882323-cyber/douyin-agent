"""Signed knowledge-pack updates with atomic activation and rollback.

Remote packages fail closed: hash, compatibility, expiry, public key,
Ed25519 support and signature must all validate before the active pack changes.
The bundled fallback is the only package type allowed to use
``trusted_builtin`` instead of a signature.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    from .rule_engine import validate_rules
except ImportError:  # Direct bridge script/test execution.
    from rule_engine import validate_rules

SUPPORTED_SCHEMA_VERSIONS = {1}
UPDATE_CHANNELS = {"stable": 0, "beta": 1, "internal": 2}
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5


def locate_default_pack_path() -> Path:
    """Locate the bundled pack in source, onedir and PyInstaller onefile builds."""
    relative = Path("assets") / "knowledge" / "default_pack.json"
    candidates: list[Path] = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / relative)
    candidates.extend(
        [
            Path(__file__).resolve().parent.parent / relative,
            Path(sys.executable).resolve().parent / relative,
        ]
    )
    return next((path for path in candidates if path.is_file()), candidates[0])


DEFAULT_PACK_PATH = locate_default_pack_path()
TELEMETRY_RESULTS = {"improved", "unchanged", "worsened", "unknown"}
TELEMETRY_INDUSTRIES = {
    "general",
    "apparel",
    "beauty",
    "food",
    "home",
    "digital",
    "maternal_child",
    "health",
    "sports",
    "automotive",
    "local_services",
}
TELEMETRY_FIELDS = {
    "industry",
    "rule_id",
    "spend_band",
    "roi_band",
    "accepted",
    "result",
    "pack_version",
    "agent_version",
}


def _pack_industry(pack: dict[str, Any]) -> str:
    """Read a display-only industry label without trusting arbitrary nesting."""

    metadata = pack.get("metadata") if isinstance(pack.get("metadata"), dict) else {}
    value = str(pack.get("industry") or metadata.get("industry") or "general").strip()
    return value[:40] or "general"


class PackValidationError(ValueError):
    """A knowledge pack failed a security or compatibility check."""


class UpdateError(RuntimeError):
    """A manifest, download or activation operation failed."""


class RollbackError(UpdateError):
    """No usable backup could be restored."""


def canonical_pack_bytes(pack: dict[str, Any]) -> bytes:
    """Canonical signed content, excluding only the two integrity fields."""
    if not isinstance(pack, dict):
        raise PackValidationError("knowledge pack must be an object")
    content = copy.deepcopy(pack)
    content.pop("sha256", None)
    content.pop("signature", None)
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_pack_sha256(pack: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_pack_bytes(pack)).hexdigest()


def _parse_time(value: Any, field: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise PackValidationError(f"{field} is required")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PackValidationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise PackValidationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _version_key(value: Any) -> tuple[tuple[int, ...], int, str]:
    text = str(value or "").strip().lower()
    if text.startswith("v"):
        text = text[1:]
    match = re.fullmatch(r"(\d+(?:\.\d+){1,4})(?:[-+]([0-9a-z.-]+))?", text)
    if not match:
        raise PackValidationError(f"invalid version: {value}")
    numbers = tuple(int(part) for part in match.group(1).split("."))
    prerelease = match.group(2) or ""
    # A final release sorts after a prerelease with identical numeric parts.
    return numbers, 1 if not prerelease else 0, prerelease


def compare_versions(left: Any, right: Any) -> int:
    left_key = _version_key(left)
    right_key = _version_key(right)
    width = max(len(left_key[0]), len(right_key[0]))
    left_numbers = left_key[0] + (0,) * (width - len(left_key[0]))
    right_numbers = right_key[0] + (0,) * (width - len(right_key[0]))
    normalized_left = (left_numbers, left_key[1], left_key[2])
    normalized_right = (right_numbers, right_key[1], right_key[2])
    return (normalized_left > normalized_right) - (normalized_left < normalized_right)


def _decode_public_key(public_key: str | bytes | None) -> bytes:
    if public_key is None:
        raise PackValidationError("remote knowledge pack requires an Ed25519 public key")
    if isinstance(public_key, bytes):
        key = public_key
    else:
        text = public_key.strip()
        try:
            key = bytes.fromhex(text) if re.fullmatch(r"[0-9a-fA-F]{64}", text) else base64.b64decode(text, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise PackValidationError("Ed25519 public key encoding is invalid") from exc
    if len(key) != 32:
        raise PackValidationError("Ed25519 public key must contain 32 bytes")
    return key


def _decode_signature(signature: Any) -> bytes:
    text = str(signature or "").strip()
    if not text:
        raise PackValidationError("remote knowledge pack signature is required")
    try:
        value = bytes.fromhex(text) if re.fullmatch(r"[0-9a-fA-F]{128}", text) else base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PackValidationError("Ed25519 signature encoding is invalid") from exc
    if len(value) != 64:
        raise PackValidationError("Ed25519 signature must contain 64 bytes")
    return value


def _verify_ed25519(public_key: str | bytes | None, signature: Any, message: bytes) -> None:
    key_bytes = _decode_public_key(public_key)
    signature_bytes = _decode_signature(signature)
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise PackValidationError("cryptography is required to verify remote knowledge packs") from exc
    try:
        Ed25519PublicKey.from_public_bytes(key_bytes).verify(signature_bytes, message)
    except (InvalidSignature, ValueError) as exc:
        raise PackValidationError("knowledge pack signature verification failed") from exc


def validate_knowledge_pack(
    pack: dict[str, Any],
    *,
    current_agent_version: str,
    source: str,
    public_key: str | bytes | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate and return a defensive copy of one knowledge pack."""
    if not isinstance(pack, dict):
        raise PackValidationError("knowledge pack must be an object")
    candidate = copy.deepcopy(pack)
    try:
        schema_version = int(candidate.get("schema_version") or 0)
    except (TypeError, ValueError) as exc:
        raise PackValidationError("schema_version must be an integer") from exc
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise PackValidationError("knowledge pack schema is not supported")
    _version_key(candidate.get("pack_version"))
    minimum = str(candidate.get("min_agent_version") or "")
    _version_key(minimum)
    if compare_versions(current_agent_version, minimum) < 0:
        raise PackValidationError(f"agent {current_agent_version} is older than required {minimum}")
    channel = str(candidate.get("channel") or "")
    if channel not in UPDATE_CHANNELS:
        raise PackValidationError("knowledge pack channel is invalid")
    published_at = _parse_time(candidate.get("published_at"), "published_at")
    expires_at = _parse_time(candidate.get("expires_at"), "expires_at")
    if expires_at <= published_at:
        raise PackValidationError("expires_at must be after published_at")
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if published_at > current_time + timedelta(minutes=5):
        raise PackValidationError("knowledge pack published_at is in the future")
    if expires_at <= current_time:
        raise PackValidationError("knowledge pack has expired")
    claimed_hash = str(candidate.get("sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", claimed_hash):
        raise PackValidationError("knowledge pack sha256 is required")
    computed_hash = compute_pack_sha256(candidate)
    if claimed_hash != computed_hash:
        raise PackValidationError("knowledge pack sha256 does not match canonical content")

    if source == "builtin":
        if candidate.get("trusted_builtin") is not True:
            raise PackValidationError("bundled knowledge pack must be explicitly trusted_builtin")
    elif source == "remote":
        if candidate.get("trusted_builtin"):
            raise PackValidationError("remote knowledge pack cannot claim trusted_builtin")
        _verify_ed25519(public_key, candidate.get("signature"), canonical_pack_bytes(candidate))
    else:
        raise PackValidationError("knowledge pack source must be builtin or remote")
    rule_errors = validate_rules(candidate)
    if rule_errors:
        summary = "; ".join(f"{item['rule_id']}: {item['error']}" for item in rule_errors[:5])
        raise PackValidationError("knowledge pack rules are invalid: " + summary)
    return candidate


def channel_allows(selected_channel: str, package_channel: str) -> bool:
    if selected_channel not in UPDATE_CHANNELS or package_channel not in UPDATE_CHANNELS:
        return False
    return UPDATE_CHANNELS[package_channel] <= UPDATE_CHANNELS[selected_channel]


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


class KnowledgePackStore:
    """Filesystem store with one active file and bounded verified backups."""

    def __init__(self, data_dir: str | Path, *, backup_count: int = DEFAULT_BACKUP_COUNT):
        self.root = Path(data_dir) / "knowledge"
        self.active_path = self.root / "active_pack.json"
        self.backup_dir = self.root / "backups"
        self.backup_count = max(1, min(int(backup_count), 50))

    def read_active(self) -> dict[str, Any] | None:
        if not self.active_path.exists():
            return None
        try:
            value = json.loads(self.active_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UpdateError("active knowledge pack cannot be read") from exc
        if not isinstance(value, dict):
            raise UpdateError("active knowledge pack must be an object")
        return value

    def _backup_name(self, pack: dict[str, Any]) -> str:
        version = re.sub(r"[^0-9A-Za-z_.-]", "_", str(pack.get("pack_version") or "unknown"))[:80]
        digest = hashlib.sha256(_json_bytes(pack)).hexdigest()[:12]
        return f"{version}-{digest}.json"

    def _prune(self) -> None:
        backups = sorted(self.backup_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for old in backups[self.backup_count :]:
            old.unlink(missing_ok=True)

    def activate(self, pack: dict[str, Any], *, backup_current: bool = True) -> Path:
        if backup_current and self.active_path.exists():
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            try:
                current = self.read_active()
            except UpdateError:
                # Preserve corrupt bytes for diagnosis, but never consider them
                # a rollback candidate (the extension is deliberately .invalid).
                raw = self.active_path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()[:12]
                _atomic_write(self.backup_dir / f"corrupt-{digest}.invalid", raw)
            else:
                if current is not None:
                    backup_path = self.backup_dir / self._backup_name(current)
                    if not backup_path.exists():
                        _atomic_write(backup_path, _json_bytes(current))
        _atomic_write(self.active_path, _json_bytes(pack))
        self._prune()
        return self.active_path

    def backups(self) -> list[Path]:
        if not self.backup_dir.exists():
            return []
        return sorted(self.backup_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)


DownloadFunction = Callable[[str, int, int], bytes]


def _secure_download(url: str, timeout_seconds: int, max_bytes: int) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise UpdateError("update downloads require an HTTPS URL")
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "DianAgent-UpdateCenter/1"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise UpdateError("update download is too large")
            content = response.read(max_bytes + 1)
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError("update download failed") from exc
    if len(content) > max_bytes:
        raise UpdateError("update download is too large")
    return content


class UpdateCenter:
    """Channel-aware client for checking, installing and rolling back packs."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        current_agent_version: str,
        channel: str = "stable",
        public_key: str | bytes | None = None,
        manifest_url: str | None = None,
        downloader: DownloadFunction | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        if channel not in UPDATE_CHANNELS:
            raise ValueError("update channel must be stable, beta or internal")
        _version_key(current_agent_version)
        self.store = KnowledgePackStore(data_dir)
        self.current_agent_version = current_agent_version
        self.channel = channel
        self.public_key = public_key
        self.manifest_url = manifest_url
        self._download = downloader or _secure_download
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _get_json(self, url: str) -> tuple[dict[str, Any], bytes]:
        content = self._download(url, 15, MAX_DOWNLOAD_BYTES)
        try:
            value = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateError("update response is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise UpdateError("update response must be a JSON object")
        return value, content

    def _validate_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(manifest, dict):
            raise UpdateError("manifest must be a JSON object")
        candidate = copy.deepcopy(manifest)
        required = {"pack_version", "channel", "min_agent_version", "url", "sha256"}
        missing = sorted(required - candidate.keys())
        if missing:
            raise UpdateError("manifest is missing: " + ", ".join(missing))
        if not channel_allows(self.channel, str(candidate.get("channel") or "")):
            raise UpdateError("manifest channel is not allowed by the selected update channel")
        try:
            _version_key(candidate["pack_version"])
            _version_key(candidate["min_agent_version"])
        except PackValidationError as exc:
            raise UpdateError(str(exc)) from exc
        if compare_versions(self.current_agent_version, candidate["min_agent_version"]) < 0:
            raise UpdateError("agent must be updated before this knowledge pack")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(candidate.get("sha256") or "")):
            raise UpdateError("manifest download sha256 is invalid")
        parsed_url = urlparse(str(candidate.get("url") or ""))
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise UpdateError("manifest knowledge pack URL must use HTTPS")
        return candidate

    def fetch_manifest(self, url: str | None = None) -> dict[str, Any]:
        manifest_url = url or self.manifest_url
        if not manifest_url:
            raise UpdateError("manifest URL is not configured")
        manifest, _ = self._get_json(manifest_url)
        return self._validate_manifest(manifest)

    def check_for_update(self, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
        candidate = self._validate_manifest(manifest) if manifest is not None else self.fetch_manifest()
        try:
            active = self.store.read_active()
        except UpdateError:
            active = None
        active_version = str(active.get("pack_version") or "0.0") if active else "0.0"
        available = compare_versions(candidate.get("pack_version"), active_version) > 0
        return {
            "available": available,
            "active_version": active_version,
            "candidate_version": str(candidate.get("pack_version") or ""),
            "channel": self.channel,
            "reason": "newer_pack" if available else "not_newer",
        }

    def install(self, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
        candidate = self._validate_manifest(manifest) if manifest is not None else self.fetch_manifest()
        pack, raw = self._get_json(str(candidate.get("url") or ""))
        download_hash = hashlib.sha256(raw).hexdigest()
        if download_hash != str(candidate.get("sha256") or "").lower():
            raise UpdateError("downloaded knowledge pack hash does not match manifest")
        for field in ("pack_version", "channel", "min_agent_version"):
            if str(pack.get(field) or "") != str(candidate.get(field) or ""):
                raise UpdateError(f"manifest and knowledge pack disagree on {field}")
        try:
            verified = validate_knowledge_pack(
                pack,
                current_agent_version=self.current_agent_version,
                source="remote",
                public_key=self.public_key,
                now=self._now(),
            )
        except PackValidationError as exc:
            raise UpdateError(str(exc)) from exc
        try:
            active = self.store.read_active()
        except UpdateError:
            active = None
        if active and compare_versions(verified["pack_version"], active.get("pack_version")) <= 0:
            raise UpdateError("knowledge pack is not newer than the active version")
        self.store.activate(verified)
        return {
            "ok": True,
            "status": "activated",
            "pack_version": verified["pack_version"],
            "channel": verified["channel"],
            "active_path": str(self.store.active_path),
        }

    def install_local(self, pack: dict[str, Any]) -> dict[str, Any]:
        """Verify and activate a locally selected signed industry pack.

        Local files are not a weaker trust path: they use the same Ed25519,
        compatibility, channel, expiry and rule validation as downloads.
        """

        try:
            verified = validate_knowledge_pack(
                pack,
                current_agent_version=self.current_agent_version,
                source="remote",
                public_key=self.public_key,
                now=self._now(),
            )
        except PackValidationError as exc:
            raise UpdateError(str(exc)) from exc
        if not channel_allows(self.channel, str(verified.get("channel") or "")):
            raise UpdateError("local knowledge pack channel is not allowed by the selected update channel")
        try:
            active = self.store.read_active()
        except UpdateError:
            active = None
        if active and compare_versions(verified["pack_version"], active.get("pack_version")) <= 0:
            raise UpdateError("local knowledge pack is not newer than the active version")
        self.store.activate(verified)
        return {
            "ok": True,
            "status": "activated",
            "install_mode": "local_signed_import",
            "pack_version": verified["pack_version"],
            "channel": verified["channel"],
            "industry": _pack_industry(verified),
            "active_path": str(self.store.active_path),
        }

    def rollback_candidates(self) -> list[dict[str, Any]]:
        """Describe rollback files without exposing their contents."""

        candidates: list[dict[str, Any]] = []
        for backup in self.store.backups():
            item: dict[str, Any] = {
                "file": backup.name,
                "pack_version": "",
                "industry": "general",
                "usable": False,
            }
            try:
                value = json.loads(backup.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise PackValidationError("knowledge pack must be an object")
                item["pack_version"] = str(value.get("pack_version") or "")
                item["industry"] = _pack_industry(value)
                source = "builtin" if value.get("trusted_builtin") is True else "remote"
                validate_knowledge_pack(
                    value,
                    current_agent_version=self.current_agent_version,
                    source=source,
                    public_key=self.public_key,
                    now=self._now(),
                )
                item["usable"] = True
            except (OSError, json.JSONDecodeError, PackValidationError) as exc:
                item["reason"] = str(exc)
            candidates.append(item)
        return candidates

    def rollback(self, *, pack_version: str | None = None) -> dict[str, Any]:
        for backup in self.store.backups():
            try:
                value = json.loads(backup.read_text(encoding="utf-8"))
                if pack_version is not None and str(value.get("pack_version") or "") != pack_version:
                    continue
                source = "builtin" if value.get("trusted_builtin") is True else "remote"
                verified = validate_knowledge_pack(
                    value,
                    current_agent_version=self.current_agent_version,
                    source=source,
                    public_key=self.public_key,
                    now=self._now(),
                )
                self.store.activate(verified, backup_current=True)
                return {"ok": True, "status": "rolled_back", "pack_version": verified["pack_version"]}
            except (OSError, json.JSONDecodeError, PackValidationError):
                continue
        raise RollbackError("no valid, compatible and unexpired backup is available")

    def load_effective_pack(self, builtin_path: str | Path = DEFAULT_PACK_PATH) -> dict[str, Any]:
        try:
            active = self.store.read_active()
        except UpdateError:
            active = None
        if active is not None:
            source = "builtin" if active.get("trusted_builtin") is True else "remote"
            try:
                return validate_knowledge_pack(
                    active,
                    current_agent_version=self.current_agent_version,
                    source=source,
                    public_key=self.public_key,
                    now=self._now(),
                )
            except PackValidationError:
                pass
        try:
            builtin = json.loads(Path(builtin_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UpdateError("bundled knowledge pack cannot be loaded") from exc
        return validate_knowledge_pack(
            builtin,
            current_agent_version=self.current_agent_version,
            source="builtin",
            now=self._now(),
        )


def create_opt_in_telemetry(payload: dict[str, Any], *, opted_in: bool) -> dict[str, Any] | None:
    """Build the only telemetry shape this module permits; never sends it."""
    if not opted_in:
        return None
    if not isinstance(payload, dict):
        raise ValueError("telemetry payload must be an object")
    clean = {key: payload[key] for key in TELEMETRY_FIELDS if key in payload}
    rule_id = str(clean.get("rule_id") or "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,79}", rule_id):
        raise ValueError("telemetry rule_id is invalid")
    industry = str(clean.get("industry") or "").strip().lower()
    if industry not in TELEMETRY_INDUSTRIES:
        raise ValueError("telemetry industry must use an approved industry slug")
    for field in ("spend_band", "roi_band"):
        value = str(clean.get(field) or "")
        if value != "unknown" and not re.fullmatch(r"(?:<|<=|>|>=)?\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?", value):
            raise ValueError(f"telemetry {field} must be a coarse band")
        clean[field] = value
    if not isinstance(clean.get("accepted"), bool):
        raise ValueError("telemetry accepted must be boolean")
    result = str(clean.get("result") or "")
    if result not in TELEMETRY_RESULTS:
        raise ValueError("telemetry result is invalid")
    clean["industry"] = industry
    clean["rule_id"] = rule_id
    clean["result"] = result
    for field in ("pack_version", "agent_version"):
        if field in clean:
            _version_key(clean[field])
            clean[field] = str(clean[field])[:40]
    clean["schema_version"] = 1
    clean["consent"] = "explicit_opt_in"
    return clean
