"""Signed knowledge-pack updates with atomic activation and rollback.

Remote packages fail closed: hash, compatibility, expiry, public key,
Ed25519 support and signature must all validate before the active pack changes.
The bundled fallback is the only package type allowed to use
``trusted_builtin`` instead of a signature.
"""

from __future__ import annotations

import base64
import ast
import binascii
import copy
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
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
MAX_INSTALLED_VERSIONS_PER_PACK = 20
PACK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
INDUSTRY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")
STORE_KEY_PATTERN = re.compile(r"^[a-z0-9_-]{1,48}$")
PROTECTED_BASE_RULE_PREFIXES = ("system.",)
SUPPORTED_INDUSTRY_FACT_FIELDS = frozenset({
    "spend",
    "roi",
    "data_age_minutes",
    "inventory.available",
    "sales.last_24h",
})
SUPPORTED_KNOWLEDGE_SETTING_FIELDS = frozenset({
    "min_spend",
    "roi_target",
    "inventory_warning_line",
    "max_data_age_minutes",
})
WINDOWS_RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{number}" for number in range(1, 10)), *(f"lpt{number}" for number in range(1, 10))}
_STORE_LOCK = threading.RLock()

INDUSTRY_LABELS = {
    "general": "通用电商",
    "apparel": "服饰鞋包",
    "beauty": "美妆个护",
    "food": "食品生鲜",
    "home": "家居日用",
    "digital": "数码家电",
    "maternal_child": "母婴亲子",
    "health": "保健滋补",
    "sports": "运动户外",
    "automotive": "汽车用品",
    "local_services": "本地生活",
}


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


def locate_bundled_industry_pack_paths() -> list[Path]:
    """Locate optional built-in industry packs beside the default pack."""

    directory = DEFAULT_PACK_PATH.parent / "industry"
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


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


def _pack_identity(pack: dict[str, Any]) -> dict[str, Any]:
    """Return the safe catalog identity for a verified knowledge pack."""

    metadata = pack.get("metadata") if isinstance(pack.get("metadata"), dict) else {}
    industry = _pack_industry(pack).lower()
    if not INDUSTRY_PATTERN.fullmatch(industry) or industry in WINDOWS_RESERVED_NAMES:
        raise PackValidationError("knowledge pack industry must be a safe ASCII slug")
    default_pack_id = "general" if industry == "general" else f"industry.{industry}"
    pack_id = str(pack.get("pack_id") or metadata.get("pack_id") or default_pack_id).strip().lower()
    if not PACK_ID_PATTERN.fullmatch(pack_id) or any(part in WINDOWS_RESERVED_NAMES for part in pack_id.split(".")):
        raise PackValidationError("knowledge pack pack_id must be a safe ASCII slug")
    if (industry == "general") != (pack_id == "general"):
        raise PackValidationError("the general pack_id and general industry must be used together")
    display_name = str(
        pack.get("display_name")
        or metadata.get("display_name")
        or (f"{INDUSTRY_LABELS.get(industry, industry)}经营知识包" if industry != "general" else "通用电商经营知识包")
    ).strip()
    if not display_name or len(display_name) > 80:
        raise PackValidationError("knowledge pack display_name must contain 1-80 characters")
    raw_capabilities = pack.get("capabilities", metadata.get("capabilities", []))
    if raw_capabilities is None:
        raw_capabilities = []
    if not isinstance(raw_capabilities, list) or len(raw_capabilities) > 20:
        raise PackValidationError("knowledge pack capabilities must be a list with at most 20 items")
    capabilities: list[str] = []
    for value in raw_capabilities:
        label = str(value or "").strip()
        if not label or len(label) > 40:
            raise PackValidationError("knowledge pack capability labels must contain 1-40 characters")
        capabilities.append(label)
    return {
        "pack_id": pack_id,
        "industry": industry,
        "industry_label": INDUSTRY_LABELS.get(industry, industry),
        "display_name": display_name,
        "capabilities": capabilities,
    }


def _is_general_pack(pack: dict[str, Any]) -> bool:
    """Only the general namespace may become the global/base pack."""

    identity = _pack_identity(pack)
    return identity["industry"] == "general" and identity["pack_id"] == "general"


def _condition_contract_references(value: Any) -> tuple[set[str], set[str]]:
    """Collect fact and setting paths from every supported expression shape."""

    facts: set[str] = set()
    settings: set[str] = set()
    if isinstance(value, list):
        for item in value:
            item_facts, item_settings = _condition_contract_references(item)
            facts.update(item_facts)
            settings.update(item_settings)
        return facts, settings
    if not isinstance(value, dict):
        return facts, settings
    if "field" in value:
        facts.add(str(value.get("field") or ""))
    for key in ("setting", "value_from"):
        if key in value:
            settings.add(str(value.get(key) or ""))
    if "formula" in value:
        try:
            expression = ast.parse(str(value.get("formula") or ""), mode="eval")
            for node in ast.walk(expression):
                if isinstance(node, ast.Name):
                    if node.id in SUPPORTED_INDUSTRY_FACT_FIELDS:
                        facts.add(node.id)
                    else:
                        settings.add(node.id)
        except (SyntaxError, TypeError, ValueError):
            pass  # validate_rules reports the malformed formula itself.
    for child in value.values():
        child_facts, child_settings = _condition_contract_references(child)
        facts.update(child_facts)
        settings.update(child_settings)
    return facts, settings


def _validate_industry_metric_contract(pack: dict[str, Any]) -> None:
    """Fail closed when an industry pack references facts this Agent cannot supply."""

    identity = _pack_identity(pack)
    if identity["industry"] == "general":
        return
    raw_required = pack.get("required_metrics")
    if raw_required is None and isinstance(pack.get("metadata"), dict):
        raw_required = pack["metadata"].get("required_metrics")
    if raw_required is None:
        raw_required = []
    if not isinstance(raw_required, list) or any(not isinstance(item, str) for item in raw_required):
        raise PackValidationError("industry required_metrics must be a list of fact paths")
    required = [item.strip() for item in raw_required]
    if any(not item or item != raw for item, raw in zip(required, raw_required)) or len(set(required)) != len(required):
        raise PackValidationError("industry required_metrics must contain unique canonical fact paths")
    unknown_required = sorted(set(required) - SUPPORTED_INDUSTRY_FACT_FIELDS)
    if unknown_required:
        raise PackValidationError(
            "industry required_metrics contains unsupported facts: " + ", ".join(unknown_required[:5])
        )
    referenced: set[str] = set()
    setting_references: set[str] = set()
    for rule in pack.get("rules") or []:
        if isinstance(rule, dict):
            rule_facts, rule_settings = _condition_contract_references(rule.get("conditions"))
            referenced.update(rule_facts)
            setting_references.update(rule_settings)
    unknown_referenced = sorted(referenced - SUPPORTED_INDUSTRY_FACT_FIELDS)
    if unknown_referenced:
        raise PackValidationError("industry rules reference unsupported facts: " + ", ".join(unknown_referenced[:5]))
    unknown_settings = sorted(setting_references - SUPPORTED_KNOWLEDGE_SETTING_FIELDS)
    if unknown_settings:
        raise PackValidationError("industry rules reference unsupported settings: " + ", ".join(unknown_settings[:5]))
    missing = sorted(referenced - set(required))
    if missing:
        raise PackValidationError("industry required_metrics is missing referenced facts: " + ", ".join(missing[:5]))


class PackValidationError(ValueError):
    """A knowledge pack failed a security or compatibility check."""


class UpdateError(RuntimeError):
    """A manifest, download or activation operation failed."""


class RollbackError(UpdateError):
    """No usable backup could be restored."""


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"update JSON number {value} is not finite")


def _parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"update JSON number {value} is not finite")
    return parsed


def _strict_json_loads(value: str | bytes) -> Any:
    return json.loads(
        value,
        parse_float=_parse_finite_json_float,
        parse_constant=_reject_nonfinite_json_constant,
    )


def canonical_pack_bytes(pack: dict[str, Any]) -> bytes:
    """Canonical signed content, excluding only the two integrity fields."""
    if not isinstance(pack, dict):
        raise PackValidationError("knowledge pack must be an object")
    content = copy.deepcopy(pack)
    content.pop("sha256", None)
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
        raise PackValidationError("knowledge pack must contain standard finite JSON") from exc


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
    maximum = str(candidate.get("max_agent_version") or "").strip()
    if maximum:
        _version_key(maximum)
        if compare_versions(current_agent_version, maximum) > 0:
            raise PackValidationError(f"agent {current_agent_version} is newer than supported {maximum}")
    _pack_identity(candidate)
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
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def merge_knowledge_packs(base_pack: dict[str, Any], industry_pack: dict[str, Any]) -> dict[str, Any]:
    """Layer one industry pack over the verified general pack deterministically."""

    base_identity = _pack_identity(base_pack)
    industry_identity = _pack_identity(industry_pack)
    if industry_identity["industry"] == "general":
        raise UpdateError("general packs must be updated through the normal update channel")
    merged_rules: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    immutable_base_ids: set[str] = set()
    immutable_base_dedupe_keys: set[str] = set()
    for raw_rule in base_pack.get("rules") or []:
        rule = copy.deepcopy(raw_rule)
        rule_id = str(rule.get("rule_id") or "")
        rule["knowledge_layer"] = "general"
        rule["knowledge_pack_id"] = base_identity["pack_id"]
        rule["knowledge_pack_version"] = str(base_pack.get("pack_version") or "")
        merged_rules[rule_id] = rule
        order.append(rule_id)
        if rule.get("overridable") is not True:
            immutable_base_ids.add(rule_id)
            result = rule.get("result") if isinstance(rule.get("result"), dict) else {}
            immutable_base_dedupe_keys.add(str(result.get("dedupe_key") or rule_id))
    for raw_rule in industry_pack.get("rules") or []:
        rule = copy.deepcopy(raw_rule)
        rule_id = str(rule.get("rule_id") or "")
        if rule_id.startswith(PROTECTED_BASE_RULE_PREFIXES):
            continue
        result = rule.get("result") if isinstance(rule.get("result"), dict) else {}
        dedupe_key = str(result.get("dedupe_key") or rule_id)
        if rule_id in immutable_base_ids or dedupe_key in immutable_base_dedupe_keys:
            continue
        rule["knowledge_layer"] = "industry"
        rule["knowledge_pack_id"] = industry_identity["pack_id"]
        rule["knowledge_pack_version"] = str(industry_pack.get("pack_version") or "")
        if rule_id not in merged_rules:
            order.append(rule_id)
        merged_rules[rule_id] = rule
    merged = copy.deepcopy(base_pack)
    merged["rules"] = [merged_rules[rule_id] for rule_id in order]
    merged["pack_id"] = industry_identity["pack_id"]
    merged["industry"] = industry_identity["industry"]
    merged["display_name"] = industry_identity["display_name"]
    merged["capabilities"] = industry_identity["capabilities"]
    merged["pack_version"] = str(industry_pack.get("pack_version") or "")
    merged["effective_composite"] = True
    merged["metadata"] = {
        **(merged.get("metadata") if isinstance(merged.get("metadata"), dict) else {}),
        "layers": [
            {
                **base_identity,
                "pack_version": str(base_pack.get("pack_version") or ""),
                "rule_count": len(base_pack.get("rules") or []),
                "layer": "general",
            },
            {
                **industry_identity,
                "pack_version": str(industry_pack.get("pack_version") or ""),
                "rule_count": len(industry_pack.get("rules") or []),
                "layer": "industry",
            },
        ],
    }
    for field in ("sha256", "signature", "trusted_builtin"):
        merged.pop(field, None)
    errors = validate_rules(merged)
    if errors:
        summary = "; ".join(item["error"] for item in errors[:5])
        raise UpdateError("merged knowledge pack is invalid: " + summary)
    return merged


class KnowledgePackStore:
    """Filesystem store with one active file and bounded verified backups."""

    def __init__(self, data_dir: str | Path, *, backup_count: int = DEFAULT_BACKUP_COUNT):
        self.root = Path(data_dir) / "knowledge"
        self.active_path = self.root / "active_pack.json"
        self.backup_dir = self.root / "backups"
        self.pack_dir = self.root / "packs"
        self.bindings_path = self.root / "bindings.json"
        self.backup_count = max(1, min(int(backup_count), 50))
        self.last_activation_warnings: list[dict[str, Any]] = []

    def read_active(self) -> dict[str, Any] | None:
        if not self.active_path.exists():
            return None
        try:
            value = _strict_json_loads(self.active_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
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
        self.last_activation_warnings = []
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
        try:
            self._prune()
        except OSError:
            # Activation is already committed atomically. Backup retention is
            # maintenance, so reporting the whole update as failed would make
            # the API contradict the active on-disk version and invite a
            # duplicate retry. A later activation can prune the same backups.
            self.last_activation_warnings = [{
                "code": "BACKUP_PRUNE_DEFERRED",
                "message": "Knowledge pack was activated, but old backup cleanup will be retried later.",
                "activation_committed": True,
                "cleanup_retryable": True,
            }]
        return self.active_path

    def backups(self) -> list[Path]:
        if not self.backup_dir.exists():
            return []
        return sorted(self.backup_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)

    def _bindings_document(self) -> dict[str, Any]:
        default = {"schema_version": 1, "stores": {}, "history": []}
        if not self.bindings_path.exists():
            return default
        try:
            value = _strict_json_loads(self.bindings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return default
        try:
            schema_version = int(value.get("schema_version") or 0) if isinstance(value, dict) else 0
        except (TypeError, ValueError):
            schema_version = 0
        if not isinstance(value, dict) or schema_version != 1:
            return default
        stores = value.get("stores") if isinstance(value.get("stores"), dict) else {}
        history = value.get("history") if isinstance(value.get("history"), list) else []
        return {"schema_version": 1, "stores": stores, "history": history[-200:]}

    def read_binding(self, store_key: str) -> dict[str, Any] | None:
        key = str(store_key or "").strip().lower()
        if not STORE_KEY_PATTERN.fullmatch(key):
            return None
        value = self._bindings_document().get("stores", {}).get(key)
        return copy.deepcopy(value) if isinstance(value, dict) else None

    def installed_paths(self) -> list[Path]:
        if not self.pack_dir.exists():
            return []
        return sorted(
            (path for path in self.pack_dir.glob("*/*.json") if path.is_file()),
            key=lambda path: (path.parent.name, path.name),
        )

    def install_pack(self, pack: dict[str, Any]) -> tuple[Path, bool]:
        """Persist a verified pack without changing any store binding."""

        self.last_activation_warnings = []
        identity = _pack_identity(pack)
        version = str(pack.get("pack_version") or "")
        safe_version = re.sub(r"[^0-9A-Za-z_.-]", "_", version)[:80]
        digest = str(pack.get("sha256") or compute_pack_sha256(pack)).lower()
        directory = self.pack_dir / identity["pack_id"]
        target = directory / f"{safe_version}-{digest[:12]}.json"
        with _STORE_LOCK:
            directory.mkdir(parents=True, exist_ok=True)
            for existing in directory.glob(f"{safe_version}-*.json"):
                try:
                    value = _strict_json_loads(existing.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, ValueError):
                    continue
                if str(value.get("pack_version") or "") != version:
                    continue
                claimed_digest = str(value.get("sha256") or "").lower()
                try:
                    actual_digest = compute_pack_sha256(value)
                except (TypeError, ValueError):
                    actual_digest = ""
                if claimed_digest == digest:
                    if actual_digest == digest and _json_bytes(value) == _json_bytes(pack):
                        return existing, True
                    # The newly supplied object was already fully verified by
                    # UpdateCenter. Repair a corrupt signature/content copy in
                    # place instead of leaving the pack permanently unusable.
                    _atomic_write(existing, _json_bytes(pack))
                    return existing, False
                raise UpdateError("the same pack_id and version already exists with different content")
            _atomic_write(target, _json_bytes(pack))
            try:
                self._prune_installed(identity["pack_id"])
            except OSError:
                # The verified pack is already committed atomically. Retention
                # cleanup is retryable maintenance and must not turn a durable
                # install into an API-level failure.
                self.last_activation_warnings = [{
                    "code": "INSTALLED_PACK_PRUNE_DEFERRED",
                    "message": "Industry pack was installed, but old version cleanup will be retried later.",
                    "pack_install_committed": True,
                    "cleanup_retryable": True,
                }]
        return target, False

    def _prune_installed(self, pack_id: str) -> None:
        directory = self.pack_dir / pack_id
        paths = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        if len(paths) <= MAX_INSTALLED_VERSIONS_PER_PACK:
            return
        bindings = self._bindings_document().get("stores", {})
        protected = {
            (str(value.get("pack_id") or ""), str(value.get("pack_version") or ""), str(value.get("sha256") or ""))
            for value in bindings.values()
            if isinstance(value, dict)
        }
        kept = 0
        for path in paths:
            try:
                value = _strict_json_loads(path.read_text(encoding="utf-8"))
                identity = _pack_identity(value) if isinstance(value, dict) else {"pack_id": ""}
            except (OSError, json.JSONDecodeError, PackValidationError, TypeError, ValueError):
                value = {}
                identity = {"pack_id": ""}
            reference = (
                identity["pack_id"],
                str(value.get("pack_version") or "") if isinstance(value, dict) else "",
                str(value.get("sha256") or "") if isinstance(value, dict) else "",
            )
            if reference in protected or kept < MAX_INSTALLED_VERSIONS_PER_PACK:
                kept += 1
                continue
            path.unlink(missing_ok=True)

    def bind(self, store_key: str, pack: dict[str, Any], *, source: str) -> dict[str, Any]:
        key = str(store_key or "").strip().lower()
        if not STORE_KEY_PATTERN.fullmatch(key):
            raise UpdateError("store_key is invalid")
        identity = _pack_identity(pack)
        reference = {
            **identity,
            "pack_version": str(pack.get("pack_version") or ""),
            "sha256": str(pack.get("sha256") or ""),
            "source": source,
            "bound_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with _STORE_LOCK:
            document = self._bindings_document()
            previous = document["stores"].get(key)
            document["stores"][key] = reference
            document["history"].append({
                "store_key": key,
                "previous": {
                    "pack_id": str(previous.get("pack_id") or ""),
                    "pack_version": str(previous.get("pack_version") or ""),
                } if isinstance(previous, dict) else None,
                "next": {"pack_id": reference["pack_id"], "pack_version": reference["pack_version"]},
                "changed_at": reference["bound_at"],
                "result": "bound",
            })
            document["history"] = document["history"][-200:]
            _atomic_write(self.bindings_path, _json_bytes(document))
        return copy.deepcopy(reference)

    def unbind(self, store_key: str) -> bool:
        key = str(store_key or "").strip().lower()
        if not STORE_KEY_PATTERN.fullmatch(key):
            raise UpdateError("store_key is invalid")
        with _STORE_LOCK:
            document = self._bindings_document()
            previous = document["stores"].pop(key, None)
            if previous is None:
                return False
            changed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            document["history"].append({
                "store_key": key,
                "previous": {
                    "pack_id": str(previous.get("pack_id") or ""),
                    "pack_version": str(previous.get("pack_version") or ""),
                } if isinstance(previous, dict) else None,
                "next": {"pack_id": "general", "pack_version": ""},
                "changed_at": changed_at,
                "result": "unbound",
            })
            document["history"] = document["history"][-200:]
            _atomic_write(self.bindings_path, _json_bytes(document))
        return True


DownloadFunction = Callable[[str, int, int], bytes]


def _secure_download(url: str, timeout_seconds: int, max_bytes: int) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise UpdateError("update downloads require an HTTPS URL")
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "DianAgent-UpdateCenter/1"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            # urllib follows redirects automatically. Revalidate the effective
            # URL so an HTTPS manifest endpoint cannot silently downgrade the
            # knowledge-pack request to plaintext HTTP or embed credentials.
            effective = urlparse(str(response.geturl() or ""))
            if (
                effective.scheme != "https"
                or not effective.netloc
                or effective.username
                or effective.password
            ):
                raise UpdateError("update redirect must remain on HTTPS")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise UpdateError("update download is too large")
            content = response.read(max_bytes + 1)
    except UpdateError:
        raise
    except HTTPError as exc:
        exc.close()
        raise UpdateError("update download failed") from exc
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

    def _validated_active_general(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Return the raw active object and its verified general form, if any."""

        try:
            raw = self.store.read_active()
        except UpdateError:
            return ({}, None) if self.store.active_path.exists() else (None, None)
        if raw is None:
            return None, None
        try:
            verified = validate_knowledge_pack(
                raw,
                current_agent_version=self.current_agent_version,
                # A writable data-directory file can never self-assert bundled
                # trust. Only files read from the packaged assets directory use
                # source="builtin".
                source="remote",
                public_key=self.public_key,
                now=self._now(),
            )
            if not _is_general_pack(verified):
                return raw, None
            return raw, verified
        except PackValidationError:
            return raw, None

    def local_import_trust_status(self) -> dict[str, Any]:
        """Probe the custom-pack trust anchor instead of trusting a non-empty env value."""

        if self.public_key is None or not str(self.public_key).strip():
            return {"ready": False, "key_id": "", "error": "production Ed25519 public key is not configured"}
        try:
            key = _decode_public_key(self.public_key)
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            Ed25519PublicKey.from_public_bytes(key)
        except (ImportError, PackValidationError, ValueError) as exc:
            return {"ready": False, "key_id": "", "error": str(exc)}
        return {
            "ready": True,
            "key_id": hashlib.sha256(key).hexdigest()[:16],
            "error": "",
        }

    def _get_json(self, url: str) -> tuple[dict[str, Any], bytes]:
        content = self._download(url, 15, MAX_DOWNLOAD_BYTES)
        try:
            value = _strict_json_loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
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
        raw_active, verified_active = self._validated_active_general()
        try:
            effective_base = self._load_base_pack()
        except (UpdateError, PackValidationError):
            effective_base = verified_active
        active_version = str(effective_base.get("pack_version") or "0.0") if effective_base else "0.0"
        comparison = compare_versions(candidate.get("pack_version"), active_version)
        repair_required = raw_active is not None and verified_active is None
        available = comparison > 0 or (comparison == 0 and repair_required)
        return {
            "available": available,
            "active_version": active_version,
            "candidate_version": str(candidate.get("pack_version") or ""),
            "channel": self.channel,
            "reason": "repair_invalid_active" if available and repair_required and comparison == 0 else "newer_pack" if available else "not_newer",
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
        if not _is_general_pack(verified):
            raise UpdateError("industry packs must be installed from the industry pack center")
        raw_active, verified_active = self._validated_active_general()
        try:
            effective_base = self._load_base_pack()
        except (UpdateError, PackValidationError):
            effective_base = verified_active
        comparison = compare_versions(
            verified["pack_version"],
            effective_base.get("pack_version") if effective_base else "0.0",
        )
        repairing_equal_invalid = comparison == 0 and raw_active is not None and verified_active is None
        if comparison < 0 or (comparison == 0 and not repairing_equal_invalid):
            raise UpdateError("knowledge pack is not newer than the active version")
        self.store.activate(verified)
        return {
            "ok": True,
            "status": "activated",
            "pack_version": verified["pack_version"],
            "channel": verified["channel"],
            "active_path": str(self.store.active_path),
            "maintenance_warnings": copy.deepcopy(self.store.last_activation_warnings),
        }

    def install_local(self, pack: dict[str, Any]) -> dict[str, Any]:
        """Verify and activate a locally selected signed general pack.

        Local files are not a weaker trust path: they use the same Ed25519,
        compatibility, channel, expiry and rule validation as downloads.
        Industry packs use import_industry_pack() plus an explicit store bind.
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
        if not _is_general_pack(verified):
            raise UpdateError("industry packs must be installed first and explicitly applied to one store")
        if not channel_allows(self.channel, str(verified.get("channel") or "")):
            raise UpdateError("local knowledge pack channel is not allowed by the selected update channel")
        raw_active, verified_active = self._validated_active_general()
        try:
            effective_base = self._load_base_pack()
        except (UpdateError, PackValidationError):
            effective_base = verified_active
        comparison = compare_versions(
            verified["pack_version"],
            effective_base.get("pack_version") if effective_base else "0.0",
        )
        repairing_equal_invalid = comparison == 0 and raw_active is not None and verified_active is None
        if comparison < 0 or (comparison == 0 and not repairing_equal_invalid):
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
            "maintenance_warnings": copy.deepcopy(self.store.last_activation_warnings),
        }

    @staticmethod
    def _catalog_record(
        pack: dict[str, Any],
        *,
        source: str,
        compatible: bool = True,
        reason: str = "",
    ) -> dict[str, Any]:
        identity = _pack_identity(pack)
        metadata = pack.get("metadata") if isinstance(pack.get("metadata"), dict) else {}
        required_metrics = pack.get("required_metrics", metadata.get("required_metrics", []))
        if not isinstance(required_metrics, list):
            required_metrics = []
        return {
            **identity,
            "pack_version": str(pack.get("pack_version") or ""),
            "channel": str(pack.get("channel") or "stable"),
            "min_agent_version": str(pack.get("min_agent_version") or ""),
            "max_agent_version": str(pack.get("max_agent_version") or ""),
            "published_at": pack.get("published_at"),
            "expires_at": pack.get("expires_at"),
            "rule_count": len(pack.get("rules") or []),
            "required_metrics": [str(item)[:80] for item in required_metrics[:30]],
            "publisher": str(pack.get("publisher") or metadata.get("publisher") or "店策官方")[:80],
            "key_id": str(pack.get("key_id") or metadata.get("key_id") or ("builtin" if source == "builtin" else "configured_ed25519"))[:80],
            "sha256": str(pack.get("sha256") or ""),
            "source": source,
            "trusted": compatible,
            "compatible": compatible,
            "reason": reason[:300],
        }

    def _available_industry_records(self, *, include_invalid: bool = False) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        candidates: list[tuple[dict[str, Any], str]] = []
        for path in locate_bundled_industry_pack_paths():
            try:
                value = _strict_json_loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            if isinstance(value, dict):
                candidates.append((value, "builtin"))
        for path in self.store.installed_paths():
            try:
                value = _strict_json_loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            if isinstance(value, dict):
                candidates.append((value, "installed"))
        for raw, source in candidates:
            validation_source = "builtin" if source == "builtin" else "remote"
            try:
                verified = validate_knowledge_pack(
                    raw,
                    current_agent_version=self.current_agent_version,
                    source=validation_source,
                    public_key=self.public_key,
                    now=self._now(),
                )
                _validate_industry_metric_contract(verified)
                identity = _pack_identity(verified)
                if identity["industry"] == "general":
                    continue
                records.append({**self._catalog_record(verified, source=source), "_pack": verified})
            except (PackValidationError, TypeError, ValueError) as exc:
                if not include_invalid:
                    continue
                try:
                    identity = _pack_identity(raw)
                    if identity["industry"] == "general":
                        continue
                    records.append({
                        **self._catalog_record(raw, source=source, compatible=False, reason=str(exc)),
                        "_pack": raw,
                    })
                except (PackValidationError, TypeError, ValueError):
                    continue
        def record_version(item: dict[str, Any]) -> tuple[tuple[int, ...], int, str]:
            try:
                return _version_key(item.get("pack_version"))
            except PackValidationError:
                return ((), 0, "")

        records.sort(key=lambda item: (item["industry"], item["pack_id"], record_version(item)), reverse=True)
        return records

    def import_industry_pack(self, pack: dict[str, Any]) -> dict[str, Any]:
        """Verify and install an industry pack without activating it for any store."""

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
            _validate_industry_metric_contract(verified)
        except PackValidationError as exc:
            raise UpdateError(str(exc)) from exc
        if not channel_allows(self.channel, str(verified.get("channel") or "")):
            raise UpdateError("local knowledge pack channel is not allowed by the selected update channel")
        identity = _pack_identity(verified)
        if identity["industry"] == "general":
            raise UpdateError("industry is required; general packs use the normal update channel")
        if identity["pack_id"].startswith("industry."):
            raise UpdateError("the industry.* pack namespace is reserved for built-in packs")
        protected = [
            str(rule.get("rule_id") or "")
            for rule in verified.get("rules") or []
            if isinstance(rule, dict) and str(rule.get("rule_id") or "").startswith(PROTECTED_BASE_RULE_PREFIXES)
        ]
        if protected:
            raise UpdateError("industry packs cannot define protected system.* rules")
        base = self._load_base_pack()
        immutable_ids: set[str] = set()
        immutable_dedupe_keys: set[str] = set()
        for rule in base.get("rules") or []:
            if not isinstance(rule, dict) or rule.get("overridable") is True:
                continue
            rule_id = str(rule.get("rule_id") or "")
            result = rule.get("result") if isinstance(rule.get("result"), dict) else {}
            immutable_ids.add(rule_id)
            immutable_dedupe_keys.add(str(result.get("dedupe_key") or rule_id))
        collisions: list[str] = []
        for rule in verified.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            rule_id = str(rule.get("rule_id") or "")
            result = rule.get("result") if isinstance(rule.get("result"), dict) else {}
            dedupe_key = str(result.get("dedupe_key") or rule_id)
            if rule_id in immutable_ids or dedupe_key in immutable_dedupe_keys:
                collisions.append(rule_id)
        if collisions:
            raise UpdateError(
                "industry packs cannot override or deduplicate immutable general rules: "
                + ", ".join(sorted(collisions)[:5])
            )
        _, idempotent = self.store.install_pack(verified)
        return {
            "ok": True,
            "status": "installed",
            "activation_required": True,
            "idempotent": idempotent,
            "maintenance_warnings": copy.deepcopy(
                self.store.last_activation_warnings
            ),
            **self._catalog_record(verified, source="installed"),
        }

    def _find_industry_record(
        self,
        pack_id: str,
        *,
        pack_version: str = "",
        sha256: str = "",
    ) -> dict[str, Any] | None:
        wanted_id = str(pack_id or "").strip().lower()
        candidates = [item for item in self._available_industry_records() if item["pack_id"] == wanted_id]
        if pack_version:
            candidates = [item for item in candidates if item["pack_version"] == pack_version]
        if sha256:
            candidates = [item for item in candidates if item["sha256"] == sha256]
        if not candidates:
            return None
        return max(candidates, key=lambda item: _version_key(item["pack_version"]))

    def bind_industry_pack(
        self,
        store_key: str,
        pack_id: str,
        *,
        pack_version: str = "",
    ) -> dict[str, Any]:
        """Bind one verified pack version to exactly one anonymized store key."""

        key = str(store_key or "").strip().lower()
        if not STORE_KEY_PATTERN.fullmatch(key):
            raise UpdateError("select a valid store before applying an industry pack")
        wanted_id = str(pack_id or "general").strip().lower()
        if wanted_id == "general":
            self.store.unbind(key)
            return {
                "ok": True,
                "status": "bound",
                "store_key": key,
                "pack_id": "general",
                "industry": "general",
                "display_name": "通用电商经营知识包",
                "message": "当前店铺已使用通用电商规则",
            }
        if not PACK_ID_PATTERN.fullmatch(wanted_id):
            raise UpdateError("pack_id is invalid")
        record = self._find_industry_record(wanted_id, pack_version=pack_version)
        if record is None:
            raise UpdateError("the selected industry pack is not installed, compatible or trusted")
        reference = self.store.bind(key, record["_pack"], source=record["source"])
        return {
            "ok": True,
            "status": "bound",
            "store_key": key,
            **{name: value for name, value in reference.items() if name != "sha256"},
            "rule_count": record["rule_count"],
            "message": f"{record['display_name']} 已应用到当前店铺",
        }

    def _load_base_pack(self, builtin_path: str | Path = DEFAULT_PACK_PATH) -> dict[str, Any]:
        try:
            active = self.store.read_active()
        except UpdateError:
            active = None
        if active is not None:
            try:
                verified_active = validate_knowledge_pack(
                    active,
                    current_agent_version=self.current_agent_version,
                    source="remote",
                    public_key=self.public_key,
                    now=self._now(),
                )
                if _is_general_pack(verified_active):
                    return verified_active
            except PackValidationError:
                pass
        try:
            builtin = _strict_json_loads(Path(builtin_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise UpdateError("bundled knowledge pack cannot be loaded") from exc
        verified_builtin = validate_knowledge_pack(
            builtin,
            current_agent_version=self.current_agent_version,
            source="builtin",
            now=self._now(),
        )
        if not _is_general_pack(verified_builtin):
            raise UpdateError("bundled base knowledge pack must use the general namespace")
        return verified_builtin

    def resolve_effective_pack(
        self,
        *,
        store_key: str = "",
        builtin_path: str | Path = DEFAULT_PACK_PATH,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        base = self._load_base_pack(builtin_path)
        base_identity = _pack_identity(base)
        status = {
            "store_key": str(store_key or "").strip().lower(),
            "binding": None,
            "active_pack_id": "general",
            "active_industry": "general",
            "fallback_reason": "",
            "layers": [{
                **base_identity,
                "pack_version": str(base.get("pack_version") or ""),
                "rule_count": len(base.get("rules") or []),
                "layer": "general",
            }],
        }
        key = status["store_key"]
        if not key or not STORE_KEY_PATTERN.fullmatch(key):
            status["fallback_reason"] = "store_not_selected"
            return base, status
        binding = self.store.read_binding(key)
        status["binding"] = binding
        if not binding:
            status["fallback_reason"] = "industry_not_selected"
            return base, status
        record = self._find_industry_record(
            str(binding.get("pack_id") or ""),
            pack_version=str(binding.get("pack_version") or ""),
            sha256=str(binding.get("sha256") or ""),
        )
        if record is None:
            status["fallback_reason"] = "bound_pack_unavailable"
            return base, status
        try:
            merged = merge_knowledge_packs(base, record["_pack"])
        except (UpdateError, PackValidationError, TypeError, ValueError):
            status["fallback_reason"] = "bound_pack_merge_failed"
            return base, status
        status.update({
            "active_pack_id": record["pack_id"],
            "active_industry": record["industry"],
            "fallback_reason": "",
            "layers": (merged.get("metadata") or {}).get("layers", status["layers"]),
        })
        return merged, status

    def knowledge_catalog(self, *, store_key: str = "") -> dict[str, Any]:
        effective, effective_status = self.resolve_effective_pack(store_key=store_key)
        base = self._load_base_pack()
        _, verified_active = self._validated_active_general()
        has_active_base = verified_active is not None
        base_record = self._catalog_record(base, source="active" if has_active_base else "builtin")
        active_pack_id = effective_status["active_pack_id"]
        industry_records = self._available_industry_records(include_invalid=True)
        available_count = sum(item.get("compatible") is True for item in industry_records)
        invalid_count = len(industry_records) - available_count
        public_records = []
        for item in industry_records:
            public_records.append({
                **{key: value for key, value in item.items() if key != "_pack"},
                "selected": item["pack_id"] == active_pack_id and item["pack_version"] == str(effective.get("pack_version") or ""),
            })
        general = {
            **base_record,
            "pack_id": "general",
            "industry": "general",
            "industry_label": "通用电商",
            "display_name": "通用电商经营知识包",
            "capabilities": ["数据时效", "投放止损", "库存预警"],
            "selected": active_pack_id == "general",
        }
        return {
            "schema_version": 1,
            "store_key": effective_status["store_key"],
            "binding": effective_status["binding"],
            "active_pack_id": active_pack_id,
            "active_industry": effective_status["active_industry"],
            "fallback_reason": effective_status["fallback_reason"],
            "layers": effective_status["layers"],
            "effective_rule_count": len(effective.get("rules") or []),
            "installed_count": available_count,
            "available_count": available_count,
            "invalid_count": invalid_count,
            "packs": [general, *public_records],
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
                value = _strict_json_loads(backup.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise PackValidationError("knowledge pack must be an object")
                item["pack_version"] = str(value.get("pack_version") or "")
                item["industry"] = _pack_industry(value)
                verified = validate_knowledge_pack(
                    value,
                    current_agent_version=self.current_agent_version,
                    source="remote",
                    public_key=self.public_key,
                    now=self._now(),
                )
                if _is_general_pack(verified):
                    item["usable"] = True
                else:
                    item["reason"] = "industry backups cannot become the global base pack"
            except (OSError, json.JSONDecodeError, PackValidationError, ValueError) as exc:
                item["reason"] = str(exc)
            candidates.append(item)
        return candidates

    def rollback(self, *, pack_version: str | None = None) -> dict[str, Any]:
        for backup in self.store.backups():
            try:
                value = _strict_json_loads(backup.read_text(encoding="utf-8"))
                if pack_version is not None and str(value.get("pack_version") or "") != pack_version:
                    continue
                verified = validate_knowledge_pack(
                    value,
                    current_agent_version=self.current_agent_version,
                    source="remote",
                    public_key=self.public_key,
                    now=self._now(),
                )
                if not _is_general_pack(verified):
                    continue
                self.store.activate(verified, backup_current=True)
                return {
                    "ok": True,
                    "status": "rolled_back",
                    "pack_version": verified["pack_version"],
                    "maintenance_warnings": copy.deepcopy(self.store.last_activation_warnings),
                }
            except (OSError, json.JSONDecodeError, PackValidationError, ValueError):
                continue
        raise RollbackError("no valid, compatible and unexpired backup is available")

    def load_effective_pack(
        self,
        builtin_path: str | Path = DEFAULT_PACK_PATH,
        *,
        store_key: str = "",
    ) -> dict[str, Any]:
        if not store_key:
            return self._load_base_pack(builtin_path)
        effective, _ = self.resolve_effective_pack(store_key=store_key, builtin_path=builtin_path)
        return effective


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
