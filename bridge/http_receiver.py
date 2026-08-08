"""店策 Agent 本地 companion service。

接收 Chrome 扩展提交的脱敏页面快照，按平台和页面类型原子保存，
并提供健康状态、数据目录和确定性经营诊断。仅监听 127.0.0.1。
"""

from __future__ import annotations

import json
import hashlib
import hmac
import logging
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

from action_protocol import assess_automation_readiness, build_action_draft, transition_action, validate_action_draft
from deployment_mode import blocked_browser_capability, request_origin_allowed, resolve_deployment_policy
from marketplace_readiness import build_marketplace_readiness
from local_store import LocalStore, LocalStoreError, SCHEMA_VERSION as DATABASE_SCHEMA_VERSION
from offline_upgrade import PRODUCTION_OFFLINE_PUBLIC_KEYS
from oceanengine_data import OceanEngineDataClient, load_sync_status
from oceanengine_oauth import OceanEngineOAuth
from operator_memory import archive_operator_memory, list_operator_memory, upsert_operator_memory
from promotion_mode import build_chengfang_readiness, build_promotion_context, legacy_execution_guard
from promotion_readiness import (
    LocalAnonymousFeedbackQueue,
    build_distribution_status,
    build_release_readiness,
    save_extension_install_state,
)
from rule_engine import RuleEngine, RulePackError
from update_center import RollbackError, UpdateCenter, UpdateError
from version import AGENT_VERSION

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(message)s")
logger = logging.getLogger("dian-agent-http")

BASE_DIR = Path(__file__).resolve().parent
_default_data_dir = (
    Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "DianAgent" / "data"
    if getattr(sys, "frozen", False)
    else BASE_DIR / "data"
)
DATA_DIR = Path(os.environ.get("DIAN_AGENT_DATA_DIR", _default_data_dir))
DATA_DIR.mkdir(parents=True, exist_ok=True)
if getattr(sys, "frozen", False) or os.environ.get("DIAN_AGENT_LOG_DIR"):
    _log_dir = Path(os.environ.get("DIAN_AGENT_LOG_DIR", DATA_DIR.parent / "logs"))
    _log_dir.mkdir(parents=True, exist_ok=True)
    _file_handler = RotatingFileHandler(
        _log_dir / "dian-agent.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    _file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_file_handler)

PORT = int(os.environ.get("BRIDGE_PORT", "8765"))
MAX_BODY_BYTES = int(os.environ.get("BRIDGE_MAX_BODY", str(2 * 1024 * 1024)))
ALLOWED_SOURCES = {"doudian", "qianchuan"}
SAFE_KEY = re.compile(r"^[a-z0-9_-]{1,48}$")
STALE_SECONDS = 10 * 60
REPORT_TEMPLATE_KEYS = {"default", "brief", "handover", "custom"}
DEFAULT_CUSTOM_REPORT_TEMPLATE = """# 店策 Agent 经营日志 - {{date}}

## 今日结论
{{headline}}
{{summary}}

## 今日重点
{{top_tasks}}

## 千川计划
{{plans}}

## 经营数据明细
{{metrics}}

## 内容与素材复盘
{{content_review}}

## 执行与风险台账
{{execution_log}}

## 库存风险
{{inventory}}

## 数据状态
{{scan_status}}
"""
DEFAULT_AGENT_SETTINGS = {
    "roi_target": 1.5,
    "min_spend_for_action": 100.0,
    "low_inventory_threshold": 10,
    "critical_inventory_threshold": 3,
    "inventory_days_warning": 3.0,
    "daily_report_enabled": True,
    "daily_report_time": "09:00",
    "report_retention_days": 30,
    "report_template": "default",
    "custom_report_template": DEFAULT_CUSTOM_REPORT_TEMPLATE,
    "history_retention_days": 30,
    "qianchuan_account_key": "",
    "store_key": "",
    "max_daily_execution_count": 3,
    "max_daily_budget_reduction": 300.0,
    "execution_cooldown_minutes": 30,
    "execution_mode": "observe",
}

# Thread-safe state mutation lock (prevents concurrent read-modify-write races)
_state_lock = threading.Lock()

# TTL cache for expensive analysis results (5-second window)
_analysis_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL_SECONDS = 5


def _cached(key: str, builder):
    """Return cached result if fresh, else rebuild and cache."""
    now = time.time()
    entry = _analysis_cache.get(key)
    if entry and (now - entry[0]) < _CACHE_TTL_SECONDS:
        return entry[1]
    result = builder()
    _analysis_cache[key] = (now, result)
    return result


def _invalidate_cache() -> None:
    """Clear all cached analysis results (call after data changes)."""
    _analysis_cache.clear()


def _schema_version_check() -> list[str]:
    """Check if any on-disk snapshots have outdated schema versions."""
    warnings: list[str] = []
    current_version = 2
    for source in ALLOWED_SOURCES:
        source_dir = DATA_DIR / source
        if not source_dir.exists():
            continue
        for path in source_dir.glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    inner = data.get("data", {})
                    sv = int(inner.get("schema_version", 1) if isinstance(inner, dict) else 1)
                    if sv < current_version:
                        warnings.append(f"{source}/{path.stem} (schema v{sv})")
            except (OSError, json.JSONDecodeError, ValueError):
                continue
    return warnings[:10]


def _disk_usage_check() -> dict[str, Any]:
    """Check free disk space on the data directory's filesystem."""
    import shutil
    try:
        usage = shutil.disk_usage(str(DATA_DIR))
        free_mb = round(usage.free / (1024 * 1024), 1)
        total_mb = round(usage.total / (1024 * 1024), 1)
        return {
            "free_mb": free_mb,
            "total_mb": total_mb,
            "used_percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
            "warning": free_mb < 100,  # warn if less than 100MB free
        }
    except OSError:
        return {"free_mb": -1, "total_mb": 0, "used_percent": 0, "warning": False, "error": "无法读取磁盘信息"}


def _now_label() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _safe_page_type(value: Any) -> str:
    page_type = str(value or "unknown").lower()
    return page_type if SAFE_KEY.fullmatch(page_type) else "unknown"


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _local_store() -> LocalStore:
    """Resolve from DATA_DIR at call time so tests and portable installs stay isolated."""
    return LocalStore(DATA_DIR.parent)


def _initialize_local_store() -> dict[str, Any]:
    """Initialize SQLite and idempotently mirror existing legacy snapshots."""
    store = _local_store()
    try:
        store.initialize()
        legacy_dirs = [DATA_DIR / source for source in sorted(ALLOWED_SOURCES)]
        legacy_dirs.append(DATA_DIR / "qianchuan_accounts")
        existing = [path for path in legacy_dirs if path.exists()]
        imported = store.import_json_snapshots(existing) if existing else []
        return {
            "status": "ready",
            "status_label": "本地数据库正常",
            "schema_version": store.get_schema_version(),
            "mirrored_snapshots": len(imported),
            "path": str(store.paths.database),
        }
    except (LocalStoreError, OSError) as error:
        logger.exception("本地数据库初始化失败")
        return {
            "status": "error",
            "status_label": "数据库需要修复",
            "schema_version": 0,
            "error": str(error),
        }


def _database_status() -> dict[str, Any]:
    store = _local_store()
    try:
        store.initialize()
        backups = list(store.paths.backup.glob("shop-*.db")) if store.paths.backup.exists() else []
        return {
            "status": "ready",
            "status_label": "本地数据库正常",
            "schema_version": store.get_schema_version(),
            "current_schema_version": DATABASE_SCHEMA_VERSION,
            "backup_count": len(backups),
            "path": str(store.paths.database),
            "storage": "local_only",
        }
    except (LocalStoreError, OSError) as error:
        return {
            "status": "error",
            "status_label": "数据库需要修复",
            "schema_version": 0,
            "current_schema_version": DATABASE_SCHEMA_VERSION,
            "error": str(error),
            "storage": "local_only",
        }


def _update_settings_path() -> Path:
    return DATA_DIR.parent / "config" / "update_settings.json"


def _load_update_settings() -> dict[str, Any]:
    defaults = {
        "channel": "stable",
        "telemetry_enabled": False,
        "last_check_at": None,
        "last_check": None,
    }
    path = _update_settings_path()
    if not path.exists():
        return defaults
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(value, dict):
        return defaults
    channel = str(value.get("channel") or "stable")
    defaults["channel"] = channel if channel in {"stable", "beta", "internal"} else "stable"
    defaults["telemetry_enabled"] = value.get("telemetry_enabled") is True
    defaults["last_check_at"] = value.get("last_check_at")
    defaults["last_check"] = value.get("last_check") if isinstance(value.get("last_check"), dict) else None
    return defaults


def _save_update_settings(changes: dict[str, Any]) -> dict[str, Any]:
    current = _load_update_settings()
    if "channel" in changes:
        channel = str(changes["channel"] or "")
        if channel not in {"stable", "beta", "internal"}:
            raise ValueError("更新通道只能是 stable、beta 或 internal")
        current["channel"] = channel
    if "telemetry_enabled" in changes:
        current["telemetry_enabled"] = changes["telemetry_enabled"] is True
    if "last_check_at" in changes:
        current["last_check_at"] = changes["last_check_at"]
    if "last_check" in changes:
        current["last_check"] = changes["last_check"] if isinstance(changes["last_check"], dict) else None
    _atomic_json_write(_update_settings_path(), current)
    return current


def _update_center() -> UpdateCenter:
    settings = _load_update_settings()
    return UpdateCenter(
        DATA_DIR.parent,
        current_agent_version=AGENT_VERSION,
        channel=str(settings["channel"]),
        public_key=os.environ.get("DIAN_AGENT_UPDATE_PUBLIC_KEY"),
        manifest_url=os.environ.get("DIAN_AGENT_UPDATE_MANIFEST_URL"),
    )


def _knowledge_status() -> dict[str, Any]:
    center = _update_center()
    try:
        pack = center.load_effective_pack()
        metadata = pack.get("metadata") if isinstance(pack.get("metadata"), dict) else {}
        rollback_candidates = center.rollback_candidates()
        return {
            "status": "ready",
            "version": str(pack.get("pack_version") or "builtin"),
            "channel": str(pack.get("channel") or "stable"),
            "industry": str(pack.get("industry") or metadata.get("industry") or "general")[:40],
            "expires_at": pack.get("expires_at"),
            "rule_count": len(pack.get("rules") or []),
            "rollback_available": any(item.get("usable") for item in rollback_candidates),
            "rollback_candidates": rollback_candidates,
            "source": "active" if center.store.read_active() else "builtin",
            "local_import_supported": True,
            "local_import_requires_ed25519": True,
            "local_import_trust_configured": bool(center.public_key),
        }
    except (UpdateError, ValueError, OSError) as error:
        return {
            "status": "error",
            "version": "",
            "rule_count": 0,
            "rollback_available": bool(center.store.backups()),
            "error": str(error),
        }


def _runtime_startup_state_path() -> Path:
    return DATA_DIR / "runtime" / "startup-state.json"


def build_agent_runtime_status() -> dict[str, Any]:
    """Return a UI-safe view of automatic startup and recovery state."""

    defaults: dict[str, Any] = {
        "state": "unknown",
        "state_label": "尚未记录自动启动状态",
        "autostart_enabled": False,
        "keepalive_enabled": False,
        "hidden_launcher": False,
        "last_checked_at": None,
        "last_healthy_at": None,
        "last_recovery_at": None,
        "last_error": None,
        "source": "not_reported",
    }
    path = _runtime_startup_state_path()
    if not path.exists():
        return defaults
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {**defaults, "state": "error", "state_label": "自动启动状态文件损坏", "last_error": "startup_state_unreadable"}
    if not isinstance(value, dict):
        return defaults
    allowed = {
        "state", "state_label", "autostart_enabled", "keepalive_enabled",
        "hidden_launcher", "last_checked_at", "last_healthy_at",
        "last_recovery_at", "last_error", "source", "task_name",
    }
    safe = {key: value.get(key) for key in allowed if key in value}
    safe["autostart_enabled"] = value.get("autostart_enabled") is True
    safe["keepalive_enabled"] = value.get("keepalive_enabled") is True
    safe["hidden_launcher"] = value.get("hidden_launcher") is True
    safe["last_error"] = str(value.get("last_error") or "")[:300] or None
    return {**defaults, **safe}


def build_system_status() -> dict[str, Any]:
    settings = _load_update_settings()
    database = _database_status()
    knowledge = _knowledge_status()
    scan = load_scan_status()
    catalog = list_snapshots()
    finished_at = _timestamp_seconds(scan.get("finished_at"))
    last_check = settings.get("last_check") or {}
    distribution = build_distribution_status(DATA_DIR.parent)
    anonymous_feedback = LocalAnonymousFeedbackQueue(DATA_DIR.parent).status(
        consent_enabled=settings["telemetry_enabled"]
    )
    release_readiness = build_release_readiness(
        DATA_DIR.parent,
        production_ed25519_trust=bool(PRODUCTION_OFFLINE_PUBLIC_KEYS),
    )
    product_operational = database.get("status") == "ready" and knowledge.get("status") == "ready"
    public_distribution_ready = release_readiness["ready_for_public_release"] is True
    return {
        "ready": product_operational,
        "product_operational": product_operational,
        "public_distribution_ready": public_distribution_ready,
        "agent_version": AGENT_VERSION,
        "required_extension_version": AGENT_VERSION,
        "bridge_protocol_version": 2,
        "ai_required": False,
        "mode": "local_first",
        "program_update_mode": "offline_bundle",
        "online_program_updates_configured": False,
        "offline_upgrade_signature_ready": True,
        "offline_upgrade_production_trust_configured": bool(PRODUCTION_OFFLINE_PUBLIC_KEYS),
        "offline_upgrade_production_available": bool(PRODUCTION_OFFLINE_PUBLIC_KEYS),
        "channel": settings["channel"],
        "database": database,
        "knowledge": knowledge,
        "distribution": distribution,
        "release_readiness": release_readiness,
        "runtime": build_agent_runtime_status(),
        "scan": {
            "last_success_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(finished_at)) if finished_at and scan.get("status") in {"completed", "partial"} else None,
            "stale_page_count": sum(1 for item in catalog if not item.get("fresh")),
            "snapshot_count": len(catalog),
        },
        "update": {
            "available": bool(last_check.get("available")),
            "knowledge_available": bool(last_check.get("available")),
            "candidate_version": last_check.get("candidate_version"),
            "last_check_at": settings.get("last_check_at"),
            "error": last_check.get("error"),
            "message": (
                f"上次更新检查失败：{last_check.get('error')}"
                if last_check.get("error")
                else
                f"发现知识包 {last_check.get('candidate_version')}，校验通过后可更新。"
                if last_check.get("available")
                else "当前知识包可离线使用；可手动检查更新。"
            ),
        },
        "telemetry": {
            "enabled": settings["telemetry_enabled"],
            "mode": "explicit_opt_in",
            "raw_shop_data_uploaded": False,
            "local_queue": anonymous_feedback,
        },
    }


def _snapshot_path(source: str, page_type: str) -> Path:
    return DATA_DIR / source / f"{page_type}.json"


def _account_snapshot_path(account_key: str, page_type: str) -> Path:
    return DATA_DIR / "qianchuan_accounts" / account_key / f"{page_type}.json"


def _account_catalog_path() -> Path:
    return DATA_DIR / "qianchuan_accounts.json"


def _store_catalog_path() -> Path:
    return DATA_DIR / "store_identities.json"


def _store_snapshot_path(store_key: str, source: str, page_type: str) -> Path:
    return DATA_DIR / "stores" / store_key / source / f"{page_type}.json"


def _identity_secret_path() -> Path:
    config_root = DATA_DIR.parent / "config" if getattr(sys, "frozen", False) else DATA_DIR / "config"
    return config_root / "identity-secret-v1.bin"


def _identity_secret() -> bytes:
    path = _identity_secret_path()
    try:
        secret = path.read_bytes()
        if len(secret) == 32:
            return secret
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = os.urandom(32)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(secret)
    os.replace(temporary, path)
    return secret


def _local_identity_key(kind: str, raw_id: str) -> str:
    namespaces = {
        "douyin_shop_id": ("store_v1", "douyin_shop"),
        "qianchuan_shop_id": ("store_v1", "douyin_shop"),
        "qianchuan_advertiser_id": ("adacct_v1", "qianchuan_advertiser"),
        "qianchuan_account_id": ("adacct_v1", "qianchuan_account"),
    }
    prefix, namespace = namespaces[kind]
    normalized = str(raw_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,80}", normalized):
        raise ValueError("invalid identity claim")
    digest = hmac.new(_identity_secret(), f"{namespace}\0{normalized}".encode("utf-8"), hashlib.sha256).hexdigest()[:26]
    return f"{prefix}_{digest}"


def _private_alias(kind: str, key: str) -> str:
    suffix = re.sub(r"[^a-f0-9]", "", str(key).lower())[-6:].upper() or "LOCAL"
    return f"{'店铺' if kind == 'store' else '千川账户'} {suffix}"


def _resolve_identity_claims(data: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    claims = data.pop("identity_claims", [])
    identity_status = str(data.pop("identity_status", "") or "")
    if identity_status == "conflict":
        return None, None, "conflict"
    if not isinstance(claims, list):
        claims = []
    resolved: dict[str, list[dict[str, str]]] = {"store": [], "account": []}
    for claim in claims[:8]:
        if not isinstance(claim, dict):
            continue
        kind = str(claim.get("kind") or "")
        if kind not in {"douyin_shop_id", "qianchuan_shop_id", "qianchuan_advertiser_id", "qianchuan_account_id"}:
            continue
        try:
            key = _local_identity_key(kind, str(claim.get("raw_id") or ""))
        except (KeyError, ValueError):
            continue
        target = "store" if kind in {"douyin_shop_id", "qianchuan_shop_id"} else "account"
        resolved[target].append({
            "key": key,
            "confidence": str(claim.get("confidence") or "medium")[:16],
            "identity_source": f"hmac_{kind}",
            "evidence_source": str(claim.get("evidence_source") or "unknown")[:32],
        })
    store_values = {item["key"]: item for item in resolved["store"]}
    account_values = {item["key"]: item for item in resolved["account"]}
    if len(store_values) > 1 or len(account_values) > 1:
        return None, None, "conflict"
    store = next(iter(store_values.values()), None)
    account = next(iter(account_values.values()), None)
    if store:
        store["label"] = _private_alias("store", store["key"])
    if account:
        account["label"] = _private_alias("account", account["key"])
    return store, account, "resolved" if store or account else "unresolved"


def _normalized_account_label(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:80]


def _is_valid_qianchuan_account_label(value: Any) -> bool:
    label = _normalized_account_label(value)
    if len(label) < 2 or len(label) > 48:
        return False
    if re.fullmatch(r"(?:店铺|账号|账户|广告主|千川|巨量千川|全部账号|切换账号|账号管理|ID|ID[:：])", label, re.I):
        return False
    if re.search(r"我的资金|账户明细|账户余额|活动福利|福利明细|立即充值|消息中心|帮助中心|切换账号|账号管理|全部账号", label):
        return False
    return True


def list_qianchuan_accounts() -> list[dict[str, Any]]:
    path = _account_catalog_path()
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        accounts = value.get("accounts", []) if isinstance(value, dict) else []
        if not isinstance(accounts, list):
            return []
        cleaned: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        needs_privacy_migration = False
        for account in accounts:
            if not isinstance(account, dict):
                continue
            key = str(account.get("key") or "").lower()
            legacy_label = _normalized_account_label(account.get("label"))
            needs_privacy_migration = needs_privacy_migration or bool(legacy_label and legacy_label != _private_alias("account", key))
            if legacy_label and not _is_valid_qianchuan_account_label(legacy_label):
                continue
            if not SAFE_KEY.fullmatch(key):
                continue
            if key in seen_keys:
                continue
            aliases = [
                str(alias).lower()
                for alias in account.get("aliases", [])
                if SAFE_KEY.fullmatch(str(alias).lower()) and str(alias).lower() != key
            ][:20]
            store_key = str(account.get("store_key") or "").lower()
            cleaned.append({
                "key": key,
                "label": _private_alias("account", key),
                "confidence": str(account.get("confidence") or "medium")[:16],
                "identity_source": str(account.get("identity_source") or "legacy")[:40],
                "evidence_source": str(account.get("evidence_source") or "")[:32],
                "store_key": store_key if SAFE_KEY.fullmatch(store_key) else "",
                "aliases": list(dict.fromkeys(aliases)),
                "last_seen": str(account.get("last_seen") or "")[:32],
            })
            seen_keys.add(key)
        if needs_privacy_migration:
            _atomic_json_write(path, {"schema_version": 2, "accounts": [{key: value for key, value in item.items() if key != "label"} for item in cleaned]})
        return cleaned
    except (OSError, json.JSONDecodeError):
        return []


def list_store_identities() -> list[dict[str, Any]]:
    path = _store_catalog_path()
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        stores = value.get("stores", []) if isinstance(value, dict) else []
        cleaned = []
        for item in stores if isinstance(stores, list) else []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").lower()
            if not SAFE_KEY.fullmatch(key):
                continue
            account_keys = [
                str(value).lower() for value in item.get("account_keys", [])
                if SAFE_KEY.fullmatch(str(value).lower())
            ][:20]
            cleaned.append({
                "key": key,
                "label": _private_alias("store", key),
                "confidence": str(item.get("confidence") or "medium")[:16],
                "identity_source": str(item.get("identity_source") or "legacy")[:40],
                "evidence_source": str(item.get("evidence_source") or "")[:32],
                "account_keys": list(dict.fromkeys(account_keys)),
                "last_seen": str(item.get("last_seen") or "")[:32],
            })
        return cleaned
    except (OSError, json.JSONDecodeError):
        return []


def _remember_store_identity(store: dict[str, Any], account_key: str = "") -> None:
    key = str(store.get("key") or "").lower()
    if not SAFE_KEY.fullmatch(key):
        return
    with _state_lock:
        stores = {str(item.get("key")): item for item in list_store_identities()}
        previous = stores.get(key, {})
        account_keys = [
            str(value).lower() for value in [*(previous.get("account_keys") or []), account_key]
            if SAFE_KEY.fullmatch(str(value).lower())
        ][:20]
        stores[key] = {
            "key": key,
            "confidence": str(store.get("confidence") or previous.get("confidence") or "medium")[:16],
            "identity_source": str(store.get("identity_source") or previous.get("identity_source") or "legacy")[:40],
            "evidence_source": str(store.get("evidence_source") or previous.get("evidence_source") or "")[:32],
            "account_keys": list(dict.fromkeys(account_keys)),
            "last_seen": _now_label(),
        }
        _atomic_json_write(_store_catalog_path(), {"schema_version": 1, "stores": sorted(stores.values(), key=lambda item: item.get("last_seen", ""), reverse=True)})


def link_store_account(store_key: str, account_key: str) -> dict[str, Any]:
    store_key = str(store_key or "").lower()
    account_key = str(account_key or "").lower()
    stores = {str(item.get("key") or ""): item for item in list_store_identities()}
    accounts = {str(item.get("key") or ""): item for item in list_qianchuan_accounts()}
    if store_key not in stores or account_key not in accounts:
        raise ValueError("店铺或千川账户不存在，请分别同步当前页面后再人工关联。")
    existing_store = str(accounts[account_key].get("store_key") or "")
    if existing_store and existing_store != store_key:
        raise ValueError("该千川账户已关联其他店铺，系统不会自动改绑。")
    account = {**accounts[account_key], "store_key": store_key, "evidence_source": "manual_confirmation"}
    _remember_qianchuan_account(account)
    _remember_store_identity(stores[store_key], account_key)
    return select_store_context(store_key, account_key)


def select_store_context(store_key: str, account_key: str = "") -> dict[str, Any]:
    store_key = str(store_key or "").lower()
    account_key = str(account_key or "").lower()
    if not store_key:
        save_agent_settings({"store_key": "", "qianchuan_account_key": ""})
        return build_store_catalog()
    stores = {str(item.get("key") or ""): item for item in list_store_identities()}
    if store_key not in stores:
        raise ValueError("请选择已识别的匿名店铺。")
    linked = list(stores[store_key].get("account_keys") or [])
    if account_key and account_key not in linked:
        raise ValueError("该千川账户尚未与当前店铺确认关联。")
    if not account_key and len(linked) == 1:
        account_key = linked[0]
    elif not account_key and len(linked) > 1:
        account_key = ""
    save_agent_settings({"store_key": store_key, "qianchuan_account_key": account_key})
    _confirm_onboarding_store(store_key)
    return build_store_catalog()


def build_store_catalog() -> dict[str, Any]:
    """Build a multi-store view without mixing one store's metrics into another."""
    settings = load_agent_settings()
    selected_key = str(settings.get("store_key") or "").lower()
    selected_account_key = str(settings.get("qianchuan_account_key") or "").lower()
    sync_status = load_sync_status(DATA_DIR)
    official_by_key = {
        str(account.get("account_key") or ""): account
        for account in sync_status.get("accounts", [])
        if isinstance(account, dict)
    }
    accounts = list_qianchuan_accounts()
    accounts_by_key = {str(account.get("key") or ""): account for account in accounts}
    stores: list[dict[str, Any]] = []
    for store in list_store_identities():
        key = str(store.get("key") or "")
        doudian_dir = DATA_DIR / "stores" / key / "doudian"
        doudian_paths = list(doudian_dir.glob("*.json")) if doudian_dir.exists() else []
        linked_keys = list(dict.fromkeys([
            *(store.get("account_keys") or []),
            *(account_key for account_key, account in accounts_by_key.items() if account.get("store_key") == key),
        ]))
        linked_accounts = [accounts_by_key[value] for value in linked_keys if value in accounts_by_key]
        qianchuan_paths: list[Path] = []
        for linked in linked_accounts:
            account_dir = DATA_DIR / "qianchuan_accounts" / str(linked.get("key") or "")
            if account_dir.exists():
                qianchuan_paths.extend(account_dir.glob("*.json"))
        snapshot_paths = [*doudian_paths, *qianchuan_paths]
        newest_timestamp = max((path.stat().st_mtime for path in snapshot_paths), default=0.0)
        official_accounts = [official_by_key.get(str(linked.get("key") or "")) for linked in linked_accounts]
        official_accounts = [item for item in official_accounts if item]
        channel = "official_api" if official_accounts else "browser_multi" if doudian_paths and qianchuan_paths else "qianchuan_browser" if qianchuan_paths else "doudian_browser"
        advertiser_count = sum(int(item.get("advertiser_count") or 0) for item in official_accounts) if official_accounts else len(linked_accounts)
        if official_accounts and advertiser_count == 0 and not doudian_paths:
            state, state_label = "not_linked", "未关联广告账户"
        elif official_accounts:
            state, state_label = "ready", "官方 API 可用"
        elif doudian_paths and not qianchuan_paths:
            state, state_label = "doudian_ready", "抖店网页数据"
        elif snapshot_paths:
            state, state_label = "browser_only", "网页数据"
        else:
            state, state_label = "empty", "暂无数据"
        stores.append({
            **store,
            "channel": channel,
            "advertiser_count": advertiser_count,
            "page_count": len(snapshot_paths),
            "doudian_page_count": len(doudian_paths),
            "qianchuan_page_count": len(qianchuan_paths),
            "account_keys": [str(item.get("key") or "") for item in linked_accounts],
            "updated_at": int(newest_timestamp) if newest_timestamp else None,
            "state": state,
            "state_label": state_label,
            "selected": key == selected_key,
        })
    stores.sort(
        key=lambda item: (
            0 if item.get("selected") else 1,
            0 if item.get("state") == "ready" else 1,
            -(int(item.get("updated_at") or 0)),
        )
    )
    selected_store = next((item for item in stores if item.get("key") == selected_key), None)
    valid_selected_account = selected_account_key if selected_store and selected_account_key in selected_store.get("account_keys", []) else ""
    unlinked_accounts = [account for account in accounts if not account.get("store_key")]
    return {
        "mode": "multi_store",
        "stores": stores,
        "accounts": stores,
        "store_count": len(stores),
        "official_store_count": sum(
            item.get("channel") == "official_api" for item in stores
        ),
        "selected_store_key": selected_key,
        "selected_account_key": valid_selected_account,
        "unlinked_accounts": unlinked_accounts,
        "link_required": bool(selected_store and unlinked_accounts),
        "data_isolation": "per_store",
        "privacy": "hmac_local_identity_no_plaintext_labels",
    }


def _remember_qianchuan_account(account: dict[str, Any]) -> None:
    key = str(account.get("key") or "").lower()
    if not SAFE_KEY.fullmatch(key):
        return
    with _state_lock:
        accounts = {str(item.get("key")): item for item in list_qianchuan_accounts() if isinstance(item, dict)}
        previous = accounts.get(key, {})
        aliases = [
            str(alias).lower()
            for alias in [*(previous.get("aliases") or []), *(account.get("aliases") or [])]
            if SAFE_KEY.fullmatch(str(alias).lower()) and str(alias).lower() != key
        ][:20]
        accounts[key] = {
            "key": key,
            "confidence": str(account.get("confidence") or "medium")[:16],
            "identity_source": str(account.get("identity_source") or "legacy")[:40],
            "evidence_source": str(account.get("evidence_source") or "")[:32],
            "store_key": str(account.get("store_key") or previous.get("store_key") or "").lower()
            if SAFE_KEY.fullmatch(str(account.get("store_key") or previous.get("store_key") or "").lower()) else "",
            "aliases": list(dict.fromkeys(aliases)),
            "last_seen": _now_label(),
        }
        _atomic_json_write(_account_catalog_path(), {"accounts": sorted(accounts.values(), key=lambda item: item.get("last_seen", ""), reverse=True)})


def _canonical_qianchuan_account_key(account: dict[str, Any]) -> str:
    key = str(account.get("key") or "").lower()
    if not SAFE_KEY.fullmatch(key):
        return ""
    known_accounts = list_qianchuan_accounts()
    for known in known_accounts:
        aliases = {str(alias).lower() for alias in known.get("aliases", [])}
        if key in aliases and SAFE_KEY.fullmatch(str(known.get("key") or "")):
            return str(known["key"]).lower()
    # Visible labels are deliberately excluded from canonicalization. Two
    # accounts are the same only when their local keys or explicit aliases match.
    return key


def save_data(source: str, data: dict[str, Any]) -> dict[str, Any]:
    if source not in ALLOWED_SOURCES:
        raise ValueError(f"unknown source: {source}")
    if not isinstance(data, dict):
        raise ValueError("data must be an object")

    data = dict(data)
    resolved_store, resolved_account, identity_resolution = _resolve_identity_claims(data)
    page_type = _safe_page_type(data.get("page_type"))
    captured_at_ms = int(data.get("captured_at") or data.get("timestamp") or int(time.time() * 1000))
    normalized = {
        **data,
        "schema_version": int(data.get("schema_version") or 1),
        "source": source,
        "page_type": page_type,
        "captured_at": captured_at_ms,
        "identity_resolution": identity_resolution,
    }
    if resolved_store:
        normalized["store"] = resolved_store
    elif isinstance(normalized.get("store"), dict):
        legacy_store_key = str(normalized["store"].get("key") or "").lower()
        normalized["store"] = {
            "key": legacy_store_key,
            "label": _private_alias("store", legacy_store_key),
            "confidence": str(normalized["store"].get("confidence") or "legacy")[:16],
            "identity_source": str(normalized["store"].get("identity_source") or "legacy_prehashed")[:40],
        } if SAFE_KEY.fullmatch(legacy_store_key) else None
    if resolved_account:
        normalized["account"] = resolved_account
    elif isinstance(normalized.get("account"), dict):
        legacy_account_key = str(normalized["account"].get("key") or "").lower()
        normalized["account"] = {
            "key": legacy_account_key,
            "label": _private_alias("account", legacy_account_key),
            "confidence": str(normalized["account"].get("confidence") or "legacy")[:16],
            "identity_source": str(normalized["account"].get("identity_source") or "legacy_prehashed")[:40],
            "store_key": str(normalized["account"].get("store_key") or "").lower(),
            "aliases": normalized["account"].get("aliases", []),
        } if SAFE_KEY.fullmatch(legacy_account_key) else None
    if normalized.get("store") is None:
        normalized.pop("store", None)
    if normalized.get("account") is None:
        normalized.pop("account", None)
    if isinstance(normalized.get("account"), dict):
        known_account = next((item for item in list_qianchuan_accounts() if item.get("key") == normalized["account"].get("key")), None)
        known_store_key = str((known_account or {}).get("store_key") or "")
        claimed_store_key = str((normalized.get("store") or {}).get("key") or "") if isinstance(normalized.get("store"), dict) else ""
        if known_store_key and claimed_store_key and known_store_key != claimed_store_key:
            normalized.pop("store", None)
            normalized["account"].pop("store_key", None)
            normalized["identity_resolution"] = "conflict"
        elif known_store_key and not claimed_store_key:
            linked_store = next((item for item in list_store_identities() if item.get("key") == known_store_key), None)
            if linked_store:
                normalized["store"] = linked_store
                normalized["account"]["store_key"] = known_store_key
    if isinstance(normalized.get("store"), dict) and isinstance(normalized.get("account"), dict):
        if normalized["store"].get("confidence") == "high" and normalized["account"].get("confidence") == "high":
            normalized["account"]["store_key"] = normalized["store"]["key"]
        else:
            normalized["account"].pop("store_key", None)
    if source == "qianchuan" and isinstance(normalized.get("account"), dict):
        account = {**normalized["account"]}
        detected_key = str(account.get("key") or "").lower()
        canonical_key = _canonical_qianchuan_account_key(account)
        if canonical_key:
            account["key"] = canonical_key
        if canonical_key and detected_key and canonical_key != detected_key and SAFE_KEY.fullmatch(detected_key):
            account["aliases"] = list(dict.fromkeys([*(account.get("aliases") or []), detected_key]))[:20]
        normalized["account"] = account
    payload = {
        "source": source,
        "page_type": page_type,
        "data": normalized,
        "timestamp": time.time(),
        "saved_at": _now_label(),
    }
    primary_snapshot_path = _snapshot_path(source, page_type)
    _atomic_json_write(primary_snapshot_path, payload)
    store = normalized.get("store") if isinstance(normalized.get("store"), dict) else None
    account = normalized.get("account") if isinstance(normalized.get("account"), dict) else None
    if store and SAFE_KEY.fullmatch(str(store.get("key") or "")):
        _atomic_json_write(_store_snapshot_path(str(store["key"]), source, page_type), payload)
    try:
        # JSON remains the compatibility source during the staged migration;
        # SQLite receives the same snapshot for durable history and upgrades.
        persistence_path = _store_snapshot_path(str(store["key"]), source, page_type) if store else _account_snapshot_path(str(account["key"]), page_type) if account else primary_snapshot_path
        _local_store().persist_snapshot(payload, persistence_path, snapshot_type=page_type)
    except (LocalStoreError, OSError):
        logger.exception("本地数据库镜像失败，已保留兼容 JSON: %s/%s", source, page_type)
    if source == "qianchuan" and account:
        account_key = str(account.get("key") or "").lower()
        if SAFE_KEY.fullmatch(account_key):
            _atomic_json_write(_account_snapshot_path(account_key, page_type), payload)
            _remember_qianchuan_account(account)
    if store:
        linked_account_key = str(account.get("key") or "") if account and account.get("store_key") == store.get("key") else ""
        _remember_store_identity(store, linked_account_key)
    # Backward-compatible latest snapshot for existing MCP clients.
    _atomic_json_write(DATA_DIR / f"{source}.json", payload)
    _save_history_point(payload)
    logger.info("已保存 %s/%s 快照（质量 %s）", source, page_type, normalized.get("quality", {}).get("score", "-"))
    # Track selector health baseline for anomaly detection
    quality = normalized.get("quality") or {}
    if quality:
        try:
            update_health_baseline(source, page_type, quality)
        except Exception:
            logger.exception("更新健康基线失败: %s/%s", source, page_type)
    return payload


def load_data(source: str, page_type: str | None = None, account_key: str | None = None, store_key: str | None = None) -> dict[str, Any] | None:
    if source not in ALLOWED_SOURCES:
        return None
    settings = load_agent_settings()
    selected_store = str(store_key if store_key is not None else settings.get("store_key") or "").lower()
    selected_account = account_key
    if source == "qianchuan" and selected_account is None:
        selected_account = str(settings.get("qianchuan_account_key") or "")
    if source == "qianchuan" and selected_account:
        safe_account = str(selected_account).lower()
        if not SAFE_KEY.fullmatch(safe_account):
            return None
        if page_type:
            path = _account_snapshot_path(safe_account, _safe_page_type(page_type))
        else:
            account_dir = DATA_DIR / "qianchuan_accounts" / safe_account
            candidates = sorted(account_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True) if account_dir.exists() else []
            path = candidates[0] if candidates else account_dir / "missing.json"
    elif source == "doudian" and selected_store:
        if not SAFE_KEY.fullmatch(selected_store):
            return None
        if page_type:
            path = _store_snapshot_path(selected_store, source, _safe_page_type(page_type))
        else:
            store_dir = DATA_DIR / "stores" / selected_store / source
            candidates = sorted(store_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True) if store_dir.exists() else []
            path = candidates[0] if candidates else store_dir / "missing.json"
    else:
        path = _snapshot_path(source, _safe_page_type(page_type)) if page_type else DATA_DIR / f"{source}.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        logger.exception("读取快照失败: %s", path)
        return None


def build_current_promotion_readiness() -> dict[str, Any]:
    """Expose the latest explicit mode evidence without assuming platform fields."""

    snapshot = load_data("qianchuan")
    data = (snapshot or {}).get("data") if isinstance(snapshot, dict) else {}
    context = data.get("promotion_context") if isinstance(data, dict) else None
    readiness = build_chengfang_readiness(context)
    return {
        **readiness,
        "snapshot": {
            "page_type": str((snapshot or {}).get("page_type") or ""),
            "saved_at": (snapshot or {}).get("saved_at"),
            "available": bool(snapshot),
        },
    }


def list_snapshots() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    settings = load_agent_settings()
    selected_store = str(settings.get("store_key") or "").lower()
    selected_account = str(settings.get("qianchuan_account_key") or "").lower()
    for source in sorted(ALLOWED_SOURCES):
        source_dir = (
            DATA_DIR / "stores" / selected_store / "doudian"
            if source == "doudian" and selected_store
            else DATA_DIR / "qianchuan_accounts" / selected_account
            if source == "qianchuan" and selected_account
            else DATA_DIR / source
            if not selected_store
            else DATA_DIR / "missing"
        )
        if not source_dir.exists():
            continue
        for path in sorted(source_dir.glob("*.json")):
            snapshot = load_data(source, path.stem, account_key=selected_account if source == "qianchuan" else "", store_key=selected_store)
            if not snapshot:
                continue
            data = snapshot.get("data", {})
            quality = data.get("quality", {}) if isinstance(data, dict) else {}
            age = max(0, int(time.time() - float(snapshot.get("timestamp", 0))))
            items.append(
                {
                    "source": source,
                    "page_type": snapshot.get("page_type", path.stem),
                    "saved_at": snapshot.get("saved_at"),
                    "captured_at": _timestamp_seconds(snapshot.get("timestamp")),
                    "age_seconds": age,
                    "fresh": age < STALE_SECONDS,
                    "title": data.get("title", "") if isinstance(data, dict) else "",
                    "url": data.get("url", "") if isinstance(data, dict) else "",
                    "quality_score": int(quality.get("score", 0) or 0),
                    "metric_count": int(quality.get("metric_count", 0) or 0),
                    "row_count": int(quality.get("row_count", 0) or 0),
                    "warnings": quality.get("warnings", []),
                }
            )
    return sorted(items, key=lambda item: item.get("age_seconds", 10**9))


def _parse_number(value: Any) -> float | None:
    text = str(value or "").replace(",", "").replace("¥", "").replace("￥", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group(0))
    if "万" in text:
        number *= 10_000
    elif "亿" in text:
        number *= 100_000_000
    return number


def _history_dir(source: str, page_type: str, store_key: str = "") -> Path:
    return DATA_DIR / "history" / (store_key or "legacy_unscoped") / source / page_type


def _save_history_point(snapshot: dict[str, Any]) -> None:
    data = snapshot.get("data", {})
    source = str(snapshot.get("source") or "unknown")
    page_type = str(snapshot.get("page_type") or "unknown")
    captured_at = int(data.get("captured_at") or time.time() * 1000)
    point = {
        "source": source,
        "page_type": page_type,
        "captured_at": captured_at,
        "saved_at": snapshot.get("saved_at"),
        "metrics": data.get("metrics", {}),
        "safe_metrics": data.get("safe_metrics", {}),
        "quality": data.get("quality", {}),
        "account_key": str((data.get("account") or {}).get("key") or "") if isinstance(data.get("account"), dict) else "",
        "store_key": str((data.get("store") or {}).get("key") or (data.get("account") or {}).get("store_key") or "")
        if isinstance(data, dict) else "",
    }
    directory = _history_dir(source, page_type, point["store_key"])
    _atomic_json_write(directory / f"{captured_at}.json", point)
    retention_days = int(load_agent_settings().get("history_retention_days", 30))
    cutoff_ms = int((time.time() - retention_days * 86400) * 1000)
    paths = sorted(directory.glob("*.json"), key=lambda path: path.name, reverse=True)
    for path in paths[500:]:
        path.unlink(missing_ok=True)
    for path in paths[:500]:
        try:
            if int(path.stem) < cutoff_ms:
                path.unlink(missing_ok=True)
        except ValueError:
            continue


def load_history(source: str | None = None, page_type: str | None = None, days: int = 7) -> list[dict[str, Any]]:
    days = min(90, max(1, int(days)))
    cutoff_ms = int((time.time() - days * 86400) * 1000)
    root = DATA_DIR / "history"
    if not root.exists():
        return []
    points: list[dict[str, Any]] = []
    settings = load_agent_settings()
    selected_account = str(settings.get("qianchuan_account_key") or "") if source == "qianchuan" else ""
    selected_store = str(settings.get("store_key") or "")
    scoped_root = root / selected_store if selected_store else root
    patterns = [scoped_root / source / page_type] if selected_store and source and page_type else [scoped_root / source] if selected_store and source else [scoped_root]
    for base in patterns:
        if not base.exists():
            continue
        for path in base.rglob("*.json"):
            try:
                if int(path.stem) < cutoff_ms:
                    continue
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict) and (not source or value.get("source") == source) and (not page_type or value.get("page_type") == page_type) and (not selected_account or value.get("account_key") == selected_account) and (not selected_store or value.get("store_key") == selected_store):
                    points.append(value)
            except (ValueError, OSError, json.JSONDecodeError):
                continue
    return sorted(points, key=lambda item: int(item.get("captured_at", 0)))


def build_trends(days: int = 7, source: str | None = None, page_type: str | None = None) -> dict[str, Any]:
    points = load_history(source, page_type, days)
    series: dict[str, list[dict[str, Any]]] = {}
    for point in points:
        metrics = point.get("safe_metrics") or {}
        for label, raw_value in metrics.items():
            value = _parse_number(raw_value)
            if value is None:
                continue
            key = f"{point.get('source')}/{point.get('page_type')}/{label}"
            series.setdefault(key, []).append({"captured_at": point.get("captured_at"), "value": value, "raw": raw_value})
    changes = []
    for key, values in series.items():
        if len(values) < 2:
            continue
        first, last = values[0]["value"], values[-1]["value"]
        delta = last - first
        delta_percent = delta / abs(first) * 100 if first else None
        changes.append({"key": key, "label": key.rsplit("/", 1)[-1], "first": first, "last": last, "delta": delta, "delta_percent": delta_percent, "points": values[-48:]})
    changes.sort(key=lambda item: abs(item["delta_percent"] if item["delta_percent"] is not None else item["delta"]), reverse=True)
    return {"generated_at": _now_label(), "days": days, "history_points": len(points), "series_count": len(series), "changes": changes[:30], "mode": "read_only"}


def _metric_matches(source: str, keywords: tuple[str, ...]) -> list[tuple[dict[str, Any], str, Any]]:
    matches: list[tuple[dict[str, Any], str, Any]] = []
    for item in list_snapshots():
        if item["source"] != source:
            continue
        snapshot = load_data(source, item["page_type"])
        metrics = (snapshot or {}).get("data", {}).get("metrics", {})
        if not isinstance(metrics, dict):
            continue
        for label, value in metrics.items():
            if any(keyword.lower() in str(label).lower() for keyword in keywords):
                matches.append((item, str(label), value))
    return matches


def _age_label(seconds: int) -> str:
    if seconds < 60:
        return "刚刚更新"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def _evaluate_knowledge_rules(catalog: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn local snapshots into the stable fact vocabulary used by knowledge packs."""
    def first_number(source: str, keywords: tuple[str, ...]) -> float | None:
        for _item, _label, value in _metric_matches(source, keywords):
            number = _parse_number(value)
            if number is not None:
                return number
        return None
    inventory_values = [
        number
        for _item, _label, value in _metric_matches("doudian", ("可售库存", "库存"))
        if (number := _parse_number(value)) is not None
    ]
    facts = {
        "spend": first_number("qianchuan", ("消耗", "花费", "spend", "广告消耗")),
        "roi": first_number("qianchuan", ("支付roi", "成交roi", "roi")),
        "data_age_minutes": max((int(item.get("age_seconds") or 0) for item in catalog), default=0) / 60,
        "inventory": {"available": min(inventory_values) if inventory_values else None},
        "sales": {"last_24h": first_number("doudian", ("支付订单", "成交订单", "订单数", "订单")) or 0},
    }
    rule_settings = {
        **settings,
        "min_spend": settings.get("min_spend_for_action", 100),
        "inventory_warning_line": settings.get("low_inventory_threshold", 10),
        "max_data_age_minutes": 30,
    }
    try:
        pack = _update_center().load_effective_pack()
        result = RuleEngine(pack).evaluate(facts, rule_settings)
    except (UpdateError, RulePackError, ValueError, OSError):
        logger.exception("本地经营知识包判断失败，继续使用内置兼容诊断")
        return []
    alerts: list[dict[str, Any]] = []
    for item in result.get("diagnostics", []):
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        alerts.append({
            "level": "high" if item.get("level") in {"critical", "high"} else "warning" if item.get("level") == "medium" else "info",
            "confidence": "high",
            "title": str(item.get("title") or "经营规则提醒"),
            "detail": str(item.get("message") or "本地经营知识包命中了一条规则。"),
            "action": str(action.get("label") or "请人工核对后处理"),
            "acceptance": item.get("acceptance") or {},
            "evidence": {
                "source": "knowledge_pack",
                "rule_id": item.get("rule_id"),
                "rule_version": item.get("rule_version"),
                "pack_version": result.get("pack_version"),
                "facts": facts,
            },
            "execution_enabled": False,
        })
    return alerts


def build_insights() -> dict[str, Any]:
    catalog = list_snapshots()
    coverage = [{**item, "age_label": _age_label(item["age_seconds"])} for item in catalog]
    alerts: list[dict[str, Any]] = []
    settings = load_agent_settings()

    for item in catalog:
        if not item["fresh"]:
            alerts.append(
                {
                    "level": "warning",
                    "confidence": "high",
                    "title": f"{item['source']}/{item['page_type']} 数据已过期",
                    "detail": f"最后更新于 {item['saved_at']}",
                    "action": "打开对应后台页面并点击“同步并诊断”。",
                    "evidence": item,
                }
            )
        # Quality < 25: skip if snapshot is very recent (< 2 min, page may still be loading)
        if item["quality_score"] < 25:
            if item["age_seconds"] < 120:
                continue  # likely still loading, suppress false positive
            alerts.append(
                {
                    "level": "info",
                    "confidence": "medium" if item["age_seconds"] < 300 else "high",
                    "title": f"{item['page_type']} 页面字段不足",
                    "detail": f"数据采于 {_age_label(item['age_seconds'])}，质量分 {item['quality_score']}。",
                    "action": "刷新页面后重新同步；若仍失败，请更新页面适配器。",
                    "evidence": item,
                }
            )

    roi_metrics = _metric_matches("qianchuan", ("roi", "支付roi", "成交roi"))
    min_spend = float(settings.get("min_spend_for_action", 100.0))
    for item, label, value in roi_metrics[:3]:
        roi = _parse_number(value)
        if roi is not None and roi < 1:
            # Check if spend is above minimum threshold before alerting
            snapshot = load_data("qianchuan", item["page_type"])
            metrics = (snapshot or {}).get("data", {}).get("metrics", {})
            best_spend = None
            spend_label = ""
            for spend_label, spend_val in (metrics or {}).items():
                if any(keyword in str(spend_label) for keyword in ("消耗", "花费", "spend", "广告消耗")):
                    best_spend = _parse_number(spend_val)
                    if best_spend is not None:
                        break
            if best_spend is not None and best_spend < min_spend:
                continue  # spend too low for ROI to be actionable
            alerts.append(
                {
                    "level": "high",
                    "confidence": "high" if (best_spend is not None and best_spend >= min_spend * 2) else "medium",
                    "title": f"千川 {label} 低于 1",
                    "detail": f"当前 {label} = {value}" + (f"，消耗 {spend_label}" if best_spend is not None else "") + "。",
                    "action": "先核对统计周期和归因口径，再检查高消耗低成交计划；不要直接批量提价。",
                    "evidence": {"source": "qianchuan", "page_type": item["page_type"], "label": label, "value": value, "spend": best_spend},
                }
            )

    refund_metrics = _metric_matches("doudian", ("退款率", "退货率"))
    for item, label, value in refund_metrics[:3]:
        rate = _parse_number(value)
        if rate is not None and rate > 20:
            # Include period context if available in snapshot
            snapshot = load_data("doudian", item["page_type"])
            period_info = ""
            data = (snapshot or {}).get("data", {})
            for key in ("date_range", "period", "stat_date", "time_range"):
                period_val = data.get(key) or (data.get("safe_metrics") or {}).get(key)
                if period_val:
                    period_info = f"（统计周期: {period_val}）"
                    break
            alerts.append(
                {
                    "level": "warning",
                    "confidence": "medium" if item["age_seconds"] > 3600 else "high",
                    "title": f"{label} 偏高",
                    "detail": f"当前 {value}{period_info}。",
                    "action": "按商品和退款原因下钻，优先处理尺码、描述不符和质量类问题。",
                    "evidence": {"source": "doudian", "page_type": item["page_type"], "label": label, "value": value},
                }
            )

    inventory_metrics = _metric_matches("doudian", ("库存", "可售库存"))
    for item, label, value in inventory_metrics[:5]:
        inventory = _parse_number(value)
        if inventory is not None and 0 <= inventory <= 10:
            alerts.append(
                {
                    "level": "warning",
                    "confidence": "high",
                    "title": "发现低库存指标",
                    "detail": f"{label} = {value}。",
                    "action": "核对在投商品库存，避免有消耗但无法持续成交。",
                    "evidence": {"source": "doudian", "page_type": item["page_type"], "label": label, "value": value},
                }
            )

    present = {(item["source"], item["page_type"]) for item in catalog}
    recommended_pages = [
        ("doudian", "overview", "打开抖店经营首页，补齐经营概览"),
        ("doudian", "orders", "打开订单管理，补齐订单履约数据"),
        ("doudian", "products", "打开商品管理，补齐商品与库存数据"),
        ("qianchuan", "campaigns", "打开千川推广管理，补齐计划数据"),
        ("qianchuan", "report", "打开千川数据报表，补齐消耗与 ROI"),
    ]
    missing = [message for source, page_type, message in recommended_pages if (source, page_type) not in present]
    if missing:
        alerts.append(
            {
                "level": "info",
                "confidence": "high",
                "title": f"还有 {len(missing)} 类核心页面未同步",
                "detail": "；".join(missing[:3]),
                "action": "依次打开所需页面，每个页面只需同步一次即可进入本地目录。",
            }
        )

    existing_titles = {str(item.get("title") or "") for item in alerts}
    alerts.extend(item for item in _evaluate_knowledge_rules(catalog, settings) if item["title"] not in existing_titles)
    alerts.sort(key=lambda item: {"high": 0, "warning": 1, "info": 2}.get(str(item.get("level")), 3))

    fresh_count = sum(1 for item in catalog if item["fresh"])
    if not catalog:
        headline = "尚未收到经营数据"
        summary = "请打开已登录的抖店或千川后台，然后点击扩展中的“立即同步”。"
    elif alerts and alerts[0].get("level") == "high":
        headline = "今天先处理高优先级投放异常"
        summary = f"已覆盖 {len(catalog)} 类页面，其中 {fresh_count} 类数据在 10 分钟内更新。建议先核对证据，再执行调整。"
    else:
        headline = "经营数据链路已建立"
        summary = f"已覆盖 {len(catalog)} 类页面，其中 {fresh_count} 类数据为最新。当前建议以补齐数据和人工核对为主。"

    return {
        "generated_at": _now_label(),
        "headline": headline,
        "summary": summary,
        "coverage": coverage,
        "alerts": alerts[:10],
        "safety": {
            "mode": "read_only",
            "privacy": "masked_by_default",
            "note": "诊断来自当前网页快照，不等同于官方 API；所有建议需结合后台口径核对。",
        },
    }


def _settings_path() -> Path:
    return DATA_DIR / "settings.json"


def load_agent_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_AGENT_SETTINGS)
    path = _settings_path()
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as file:
                saved = json.load(file)
            if isinstance(saved, dict):
                settings.update(saved)
        except (OSError, json.JSONDecodeError):
            logger.exception("读取 Agent 设置失败: %s", path)
    return settings


def save_agent_settings(values: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("settings must be an object")
    current = load_agent_settings()
    allowed = set(DEFAULT_AGENT_SETTINGS)
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")

    next_settings = {**current, **values}
    next_settings["roi_target"] = min(20.0, max(0.1, float(next_settings["roi_target"])))
    next_settings["min_spend_for_action"] = min(1_000_000.0, max(0.0, float(next_settings["min_spend_for_action"])))
    next_settings["low_inventory_threshold"] = min(1_000_000, max(0, int(next_settings["low_inventory_threshold"])))
    next_settings["critical_inventory_threshold"] = min(
        next_settings["low_inventory_threshold"],
        max(0, int(next_settings["critical_inventory_threshold"])),
    )
    next_settings["inventory_days_warning"] = min(365.0, max(0.1, float(next_settings["inventory_days_warning"])))
    next_settings["daily_report_enabled"] = bool(next_settings["daily_report_enabled"])
    report_time = str(next_settings["daily_report_time"])
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", report_time):
        raise ValueError("daily_report_time must be HH:MM")
    next_settings["daily_report_time"] = report_time
    next_settings["report_retention_days"] = min(365, max(1, int(next_settings["report_retention_days"])))
    report_template = str(next_settings.get("report_template") or "default")
    if report_template not in REPORT_TEMPLATE_KEYS:
        raise ValueError("report_template must be default, brief, handover or custom")
    next_settings["report_template"] = report_template
    custom_template = str(next_settings.get("custom_report_template") or "").strip()
    if len(custom_template) > 12_000:
        raise ValueError("custom_report_template is too long")
    next_settings["custom_report_template"] = custom_template or DEFAULT_CUSTOM_REPORT_TEMPLATE
    next_settings["history_retention_days"] = min(365, max(1, int(next_settings["history_retention_days"])))
    next_settings["max_daily_execution_count"] = min(50, max(1, int(next_settings["max_daily_execution_count"])))
    next_settings["max_daily_budget_reduction"] = min(
        1_000_000.0,
        max(1.0, float(next_settings["max_daily_budget_reduction"])),
    )
    next_settings["execution_cooldown_minutes"] = min(1440, max(0, int(next_settings["execution_cooldown_minutes"])))
    execution_mode = str(next_settings.get("execution_mode") or "observe")
    if execution_mode not in {"observe", "shadow", "supervised"}:
        raise ValueError("execution_mode must be observe, shadow or supervised")
    next_settings["execution_mode"] = execution_mode
    account_key = str(next_settings.get("qianchuan_account_key") or "").lower()
    if account_key and not SAFE_KEY.fullmatch(account_key):
        raise ValueError("invalid qianchuan_account_key")
    next_settings["qianchuan_account_key"] = account_key
    store_key = str(next_settings.get("store_key") or "").lower()
    if store_key and not SAFE_KEY.fullmatch(store_key):
        raise ValueError("invalid store_key")
    next_settings["store_key"] = store_key
    _atomic_json_write(_settings_path(), next_settings)
    return next_settings


def _integrations_path() -> Path:
    return DATA_DIR / "integrations.json"


def _validate_webhook(platform: str, value: str) -> str:
    webhook = str(value or "").strip()
    if not webhook:
        return ""
    parsed = urlparse(webhook)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Webhook 必须是平台提供的 HTTPS 地址。")
    if platform == "feishu":
        valid = parsed.hostname == "open.feishu.cn" and parsed.path.startswith("/open-apis/bot/v2/hook/")
    elif platform == "dingtalk":
        valid = parsed.hostname == "oapi.dingtalk.com" and parsed.path == "/robot/send" and bool(parse_qs(parsed.query).get("access_token"))
    else:
        raise ValueError("不支持的通知平台。")
    if not valid:
        raise ValueError(f"{platform} Webhook 地址格式不正确。")
    return webhook


def _load_integration_secrets() -> dict[str, Any]:
    values = {"feishu_webhook": "", "dingtalk_webhook": "", "auto_send_reports": False}
    path = _integrations_path()
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                values.update({key: saved.get(key, values[key]) for key in values})
        except (OSError, json.JSONDecodeError):
            logger.exception("读取通知连接设置失败")
    return values


def get_integration_settings() -> dict[str, Any]:
    values = _load_integration_secrets()
    return {
        "feishu": {"configured": bool(values["feishu_webhook"]), "label": "已连接" if values["feishu_webhook"] else "未连接"},
        "dingtalk": {"configured": bool(values["dingtalk_webhook"]), "label": "已连接" if values["dingtalk_webhook"] else "未连接"},
        "auto_send_reports": bool(values["auto_send_reports"]),
        "secrets_exposed": False,
    }


def save_integration_settings(values: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("integration settings must be an object")
    allowed = {"feishu_webhook", "dingtalk_webhook", "auto_send_reports"}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unknown integration settings: {', '.join(sorted(unknown))}")
    current = _load_integration_secrets()
    for platform in ("feishu", "dingtalk"):
        key = f"{platform}_webhook"
        if key in values:
            current[key] = _validate_webhook(platform, str(values.get(key) or ""))
    if "auto_send_reports" in values:
        current["auto_send_reports"] = bool(values["auto_send_reports"])
    _atomic_json_write(_integrations_path(), current)
    return get_integration_settings()


def _post_json(url: str, payload: dict[str, Any], timeout: float = 8.0) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": f"DianAgent/{AGENT_VERSION}"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is allowlisted by _validate_webhook
        raw = response.read(64 * 1024).decode("utf-8", errors="replace")
    return json.loads(raw) if raw else {}


def send_notification(platform: str, message: str) -> dict[str, Any]:
    platform = str(platform or "").lower()
    values = _load_integration_secrets()
    key = f"{platform}_webhook"
    if key not in values:
        raise ValueError("不支持的通知平台。")
    webhook = _validate_webhook(platform, str(values.get(key) or ""))
    if not webhook:
        raise ValueError(f"{platform} 尚未连接。")
    text = str(message or "").strip()
    if not text:
        raise ValueError("通知内容不能为空。")
    if "店策 Agent" not in text:
        text = f"店策 Agent\n{text}"
    text = text[:12_000]
    payload = (
        {"msg_type": "text", "content": {"text": text}}
        if platform == "feishu"
        else {"msgtype": "text", "text": {"content": text}}
    )
    result = _post_json(webhook, payload)
    success = (
        int(result.get("code", result.get("StatusCode", 0)) or 0) == 0
        if platform == "feishu"
        else int(result.get("errcode", 0) or 0) == 0
    )
    if not success:
        raise ValueError(str(result.get("msg") or result.get("errmsg") or "平台拒绝了消息。"))
    return {"platform": platform, "ok": True, "message": "测试消息已发送。"}


def test_integration(platform: str) -> dict[str, Any]:
    return send_notification(platform, f"店策 Agent 连接测试\n时间：{_now_label()}\n连接成功，后续可发送经营日志。")


def send_report_notifications(report: dict[str, Any]) -> list[dict[str, Any]]:
    values = _load_integration_secrets()
    results: list[dict[str, Any]] = []
    for platform in ("feishu", "dingtalk"):
        if not values.get(f"{platform}_webhook"):
            continue
        try:
            results.append(send_notification(platform, str(report.get("content") or "")))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            results.append({"platform": platform, "ok": False, "message": str(error)})
    return results


def _table_records(source: str, page_types: set[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in list_snapshots():
        if item["source"] != source or item["page_type"] not in page_types:
            continue
        snapshot = load_data(source, item["page_type"])
        snapshot_data = (snapshot or {}).get("data", {})
        if not isinstance(snapshot_data, dict):
            continue
        tables = snapshot_data.get("tables", [])
        if not isinstance(tables, list):
            continue
        account = snapshot_data.get("account") if isinstance(snapshot_data.get("account"), dict) else {}
        quality = snapshot_data.get("quality") if isinstance(snapshot_data.get("quality"), dict) else {}
        captured_at_ms = int(
            snapshot_data.get("captured_at")
            or (float((snapshot or {}).get("timestamp", 0)) * 1000)
            or 0
        )
        canonical_headers: list[str] = []
        for table_index, table in enumerate(tables):
            if not isinstance(table, dict):
                continue
            headers = [str(value).strip() for value in table.get("headers", [])]
            rows = table.get("rows", [])
            if not isinstance(rows, list):
                continue
            header_like = bool(headers) and sum("\n" not in header and len(header) <= 40 for header in headers) >= max(2, len(headers) // 2)
            if header_like:
                canonical_headers = headers
            elif canonical_headers and headers and len(headers) == len(canonical_headers):
                # Legacy snapshots treated the first data row as headers when a
                # virtualized body table was separate from its header table.
                rows = [headers, *rows]
                headers = canonical_headers
            elif canonical_headers and not headers:
                headers = canonical_headers
            if not headers:
                continue
            for row_index, row in enumerate(rows):
                if not isinstance(row, list):
                    continue
                values = [str(value).strip() for value in row]
                record = {headers[index]: values[index] if index < len(values) else "" for index in range(len(headers))}
                records.append(
                    {
                        "source": source,
                        "page_type": item["page_type"],
                        "quality_score": int(quality.get("score", item["quality_score"]) or 0),
                        "captured_at_ms": captured_at_ms,
                        "account_key": str(account.get("key") or "").lower(),
                        "account_label": str(account.get("label") or ""),
                        "promotion_context": build_promotion_context(snapshot_data.get("promotion_context")),
                        "table_index": table_index,
                        "row_index": row_index,
                        "record": record,
                    }
                )
    return records


def _pick(record: dict[str, Any], keywords: tuple[str, ...]) -> tuple[str, Any] | tuple[None, None]:
    for label, value in record.items():
        normalized = str(label).lower().replace(" ", "")
        if any(keyword.lower().replace(" ", "") in normalized for keyword in keywords):
            return str(label), value
    return None, None


def _evidence_value(record: dict[str, Any], keywords: tuple[str, ...]) -> float | None:
    _, value = _pick(record, keywords)
    return _parse_number(value)


def _extract_labeled_number(record: dict[str, Any], label: str) -> float | None:
    pattern = re.compile(rf"{re.escape(label)}\s*[:：]?\s*\n?\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
    for value in record.values():
        match = pattern.search(str(value))
        if match:
            return float(match.group(1))
    return None


def _entity_identifier(record: dict[str, Any], keywords: tuple[str, ...]) -> str:
    """Read a platform identifier without ever substituting a row index or name."""
    for label, value in record.items():
        normalized_label = str(label).lower().replace(" ", "")
        if not any(keyword.lower().replace(" ", "") in normalized_label for keyword in keywords):
            continue
        text = str(value or "").strip()
        match = re.search(r"(?:id\s*[:：]\s*)?([a-z0-9][a-z0-9_-]{3,63})", text, re.IGNORECASE)
        if match:
            return match.group(1)
    for value in record.values():
        text = str(value or "")
        match = re.search(r"(?:计划|项目|广告组|单元)\s*ID\s*[:：]\s*([a-z0-9][a-z0-9_-]{3,63})", text, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _action_params_for_plan(
    plan: str,
    action_type: str,
    evidence: dict[str, Any],
    entry: dict[str, Any],
    confidence: str,
) -> dict[str, Any]:
    """Generate a policy-checked operation draft tied to a fresh account snapshot."""
    record = evidence.get("_record") if isinstance(evidence.get("_record"), dict) else {}
    budget = _evidence_value(record, ("日预算", "每日预算", "预算上限", "预算"))
    delivery_status = _evidence_value(record, ("投放状态", "计划状态", "状态"))
    plan_id = _entity_identifier(record, ("计划id", "项目id", "广告组id", "单元id"))
    operation_type = "replace_creative"
    operation_label = "优化素材"
    field = "素材"
    target_value: Any = None

    if action_type == "stop_loss" and str(entry.get("page_type") or "") in {"qianchuan_live", "campaigns"} and delivery_status:
        operation_type = "pause_plan"
        operation_label = "暂停单计划"
        field = "投放状态"
        target_value = "暂停"
        budget = delivery_status
    elif action_type in {"stop_loss", "reduce_budget", "scale_cautiously"}:
        operation_type = "adjust_budget"
        field = "预算"
        percent = -30 if action_type == "stop_loss" else -20 if action_type == "reduce_budget" else 10
        operation_label = f"{'降低' if percent < 0 else '增加'}预算 {abs(percent)}%"
        target_value = round(budget * (1 + percent / 100), 2) if budget and budget > 0 else None

    current_label = f"{budget:g}" if isinstance(budget, (int, float)) and budget > 0 else str(budget or "待重新读取")
    target_label = f"{target_value:g}" if isinstance(target_value, (int, float)) else "待重新计算"
    copy_text = (
        f"{plan} | 预算 {current_label} → {target_label}"
        if operation_type == "adjust_budget"
        else f"{plan} | 投放状态 {current_label} → 暂停"
        if operation_type == "pause_plan"
        else f"{plan} | 优化前 3 秒表达与卖点"
    )
    compact_evidence = {
        key: evidence.get(key)
        for key in ("spend", "roi", "roi_target", "orders", "ctr")
        if evidence.get(key) is not None
    }
    return build_action_draft(
        operation_type=operation_type,
        operation_label=operation_label,
        target_kind="qianchuan_plan",
        target_id=plan_id,
        target_name=plan,
        account_key=str(entry.get("account_key") or ""),
        account_label=str(entry.get("account_label") or ""),
        field=field,
        current_value=budget,
        target_value=target_value,
        source=str(entry.get("source") or ""),
        page_type=str(entry.get("page_type") or ""),
        captured_at_ms=int(entry.get("captured_at_ms") or 0),
        quality_score=int(entry.get("quality_score") or 0),
        confidence=confidence,
        evidence=compact_evidence,
        copy_text=copy_text,
        promotion_context=entry.get("promotion_context"),
    )


def _action_audit_path() -> Path:
    return DATA_DIR / "action_audit.json"


def load_action_audit() -> dict[str, Any]:
    path = _action_audit_path()
    if not path.exists():
        return {"schema_version": 1, "actions": [], "execution_enabled": False}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        actions = value.get("actions", []) if isinstance(value, dict) else []
        if not isinstance(actions, list):
            actions = []
        return {
            "schema_version": 1,
            "updated_at": value.get("updated_at") if isinstance(value, dict) else None,
            "actions": [item for item in actions if isinstance(item, dict)],
            "execution_enabled": False,
        }
    except (OSError, json.JSONDecodeError):
        logger.exception("读取操作审计记录失败: %s", path)
        return {"schema_version": 1, "actions": [], "execution_enabled": False}


def get_action_audit(limit: int = 100) -> dict[str, Any]:
    audit = load_action_audit()
    actions = sorted(
        audit["actions"],
        key=lambda item: int(item.get("state_updated_at_ms") or item.get("created_at_ms") or 0),
        reverse=True,
    )[: min(500, max(1, int(limit)))]
    return {
        **audit,
        "actions": actions,
        "summary": {
            "total": len(audit["actions"]),
            "confirmed": sum(item.get("state") == "confirmed" for item in audit["actions"]),
            "cancelled": sum(item.get("state") == "cancelled" for item in audit["actions"]),
            "executed": sum(item.get("state") in {"succeeded", "verified"} for item in audit["actions"]),
        },
    }


def confirm_action_draft(action: dict[str, Any]) -> dict[str, Any]:
    errors = validate_action_draft(action)
    if errors:
        messages = "；".join(dict.fromkeys(str(item.get("message") or "动作校验失败") for item in errors))
        raise ValueError(messages)
    action_id = str(action.get("action_id") or "")
    if not re.fullmatch(r"[a-f0-9]{24}", action_id):
        raise ValueError("动作编号无效，请重新生成方案。")
    with _state_lock:
        audit = load_action_audit()
        existing = next((item for item in audit["actions"] if item.get("action_id") == action_id), None)
        if existing and existing.get("state") == "confirmed":
            return existing
        if existing and existing.get("state") == "cancelled":
            raise ValueError("该操作确认已撤销，请重新同步千川数据后生成新方案。")
        confirmed = transition_action(action, "confirmed")
        confirmed["confirmed_at_ms"] = int(time.time() * 1000)
        confirmed["confirmed_by"] = "local_user"
        confirmed["execution_note"] = "已确认方案，尚未执行任何千川页面操作。"
        actions = [item for item in audit["actions"] if item.get("action_id") != action_id]
        actions.append(confirmed)
        actions = sorted(
            actions,
            key=lambda item: int(item.get("state_updated_at_ms") or item.get("created_at_ms") or 0),
            reverse=True,
        )[:500]
        _atomic_json_write(
            _action_audit_path(),
            {"schema_version": 1, "updated_at": _now_label(), "execution_enabled": False, "actions": actions},
        )
    return confirmed


def cancel_confirmed_action(action_id: str) -> dict[str, Any]:
    action_id = str(action_id or "").lower()
    if not re.fullmatch(r"[a-f0-9]{24}", action_id):
        raise ValueError("动作编号无效。")
    with _state_lock:
        audit = load_action_audit()
        existing = next((item for item in audit["actions"] if item.get("action_id") == action_id), None)
        if not existing:
            raise ValueError("未找到对应的操作确认记录。")
        if existing.get("state") == "cancelled":
            return existing
        cancelled = transition_action(existing, "cancelled")
        cancelled["cancelled_at_ms"] = int(time.time() * 1000)
        actions = [cancelled if item.get("action_id") == action_id else item for item in audit["actions"]]
        _atomic_json_write(
            _action_audit_path(),
            {"schema_version": 1, "updated_at": _now_label(), "execution_enabled": False, "actions": actions},
        )
    return cancelled


def _shadow_audit_path() -> Path:
    return DATA_DIR / "shadow_execution.json"


def load_shadow_execution() -> dict[str, Any]:
    path = _shadow_audit_path()
    if not path.exists():
        return {"schema_version": 1, "records": [], "execution_enabled": False}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        records = value.get("records", []) if isinstance(value, dict) else []
        return {
            "schema_version": 1,
            "updated_at": value.get("updated_at") if isinstance(value, dict) else None,
            "records": [item for item in records if isinstance(item, dict)] if isinstance(records, list) else [],
            "execution_enabled": False,
        }
    except (OSError, json.JSONDecodeError):
        logger.exception("读取影子执行记录失败: %s", path)
        return {"schema_version": 1, "records": [], "execution_enabled": False}


def mark_action_manually_applied(action_id: str) -> dict[str, Any]:
    """Record an operator claim without claiming or triggering execution."""
    action_id = str(action_id or "").lower()
    if not re.fullmatch(r"[a-f0-9]{24}", action_id):
        raise ValueError("动作编号无效。")
    action = next(
        (item for item in load_action_audit().get("actions", []) if item.get("action_id") == action_id),
        None,
    )
    if not action or action.get("state") != "confirmed":
        raise ValueError("只有已确认且未撤销的方案可以进入影子核验。")
    now_ms = int(time.time() * 1000)
    with _state_lock:
        shadow = load_shadow_execution()
        existing = next((item for item in shadow["records"] if item.get("action_id") == action_id), None)
        if existing:
            return existing
        record = {
            "action_id": action_id,
            "reported_state": "manually_applied",
            "reported_applied_at_ms": now_ms,
            "reported_by": "local_user",
            "execution_source": "official_qianchuan_manual",
            "execution_enabled": False,
            "note": "仅记录用户声明；插件未点击、提交或修改千川。",
        }
        records = [item for item in shadow["records"] if item.get("action_id") != action_id]
        records.append(record)
        _atomic_json_write(
            _shadow_audit_path(),
            {"schema_version": 1, "updated_at": _now_label(), "execution_enabled": False, "records": records[-500:]},
        )
    return record


def _find_plan_readback(action: dict[str, Any]) -> dict[str, Any] | None:
    target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
    account_key = str(target.get("account_key") or "")
    plan_id = str(target.get("id") or "")
    if not account_key or not plan_id:
        return None
    evidence = action.get("evidence_ref") if isinstance(action.get("evidence_ref"), dict) else {}
    page_type = str(evidence.get("page_type") or "campaigns")
    if page_type not in {"campaigns", "qianchuan_live"}:
        page_type = "campaigns"
    snapshot = load_data("qianchuan", page_type, account_key=account_key)
    data = (snapshot or {}).get("data", {})
    if not isinstance(data, dict):
        return None
    tables = data.get("tables") if isinstance(data.get("tables"), list) else []
    canonical_headers: list[str] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        headers = [str(value).strip() for value in table.get("headers", [])]
        rows = table.get("rows") if isinstance(table.get("rows"), list) else []
        if headers:
            canonical_headers = headers
        elif canonical_headers:
            headers = canonical_headers
        if not headers:
            continue
        for row in rows:
            if not isinstance(row, list):
                continue
            values = [str(value).strip() for value in row]
            record = {headers[index]: values[index] if index < len(values) else "" for index in range(len(headers))}
            if _entity_identifier(record, ("计划id", "项目id", "广告组id", "单元id")) != plan_id:
                continue
            budget = _evidence_value(record, ("日预算", "每日预算", "预算上限", "预算"))
            delivery_status = _evidence_value(record, ("投放状态", "计划状态", "状态"))
            spend = _evidence_value(record, ("消耗", "总消耗", "广告消耗"))
            roi = _evidence_value(record, ("支付roi", "roi", "整体roi"))
            orders = _evidence_value(record, ("成交订单", "支付订单", "成交订单数", "订单数"))
            captured_at_ms = int(
                data.get("captured_at")
                or (float((snapshot or {}).get("timestamp", 0)) * 1000)
                or 0
            )
            return {
                "account_key": account_key,
                "account_label": str((data.get("account") or {}).get("label") or ""),
                "plan_id": plan_id,
                "plan_name": _clean_entity_name(next(iter(record.values()), ""), str(target.get("name") or plan_id)),
                "current_value": budget,
                "delivery_status": delivery_status,
                "spend": spend,
                "roi": roi,
                "orders": orders,
                "captured_at_ms": captured_at_ms,
                "quality_score": int((data.get("quality") or {}).get("score", 0) or 0),
            }
    return None


def _execution_preflight_path() -> Path:
    return DATA_DIR / "execution_preflight.json"


def load_execution_preflight() -> dict[str, Any]:
    path = _execution_preflight_path()
    if not path.exists():
        return {"schema_version": 1, "session": None, "execution_enabled": False}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        session = value.get("session") if isinstance(value, dict) else None
        return {
            "schema_version": 1,
            "updated_at": value.get("updated_at") if isinstance(value, dict) else None,
            "session": session if isinstance(session, dict) else None,
            "execution_enabled": False,
        }
    except (OSError, json.JSONDecodeError):
        logger.exception("读取执行前检查会话失败: %s", path)
        return {"schema_version": 1, "session": None, "execution_enabled": False}


def _save_execution_preflight(session: dict[str, Any] | None) -> None:
    _atomic_json_write(
        _execution_preflight_path(),
        {"schema_version": 1, "updated_at": _now_label(), "session": session, "execution_enabled": False},
    )


def assess_execution_quota(action: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    """Apply per-account daily limits and a cooldown before any page write."""

    now_ms = int(now_ms or time.time() * 1000)
    settings = load_agent_settings()
    target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
    change = action.get("change") if isinstance(action.get("change"), dict) else {}
    account_key = str(target.get("account_key") or "")
    today = datetime.fromtimestamp(now_ms / 1000).date()
    completed: list[dict[str, Any]] = []
    for item in load_action_audit().get("actions", []):
        item_target = item.get("target_ref") if isinstance(item.get("target_ref"), dict) else {}
        executed_at = int(item.get("execution_reported_at_ms") or 0)
        if item.get("state") not in {"succeeded", "verified"} or item_target.get("account_key") != account_key or executed_at <= 0:
            continue
        if datetime.fromtimestamp(executed_at / 1000).date() == today:
            completed.append(item)
    reductions = []
    for item in completed:
        item_change = item.get("change") if isinstance(item.get("change"), dict) else {}
        current = item_change.get("current_value")
        target_value = item_change.get("target_value")
        if isinstance(current, (int, float)) and isinstance(target_value, (int, float)):
            reductions.append(max(0.0, float(current) - float(target_value)))
    proposed_reduction = (
        max(0.0, float(change["current_value"]) - float(change["target_value"]))
        if isinstance(change.get("current_value"), (int, float)) and isinstance(change.get("target_value"), (int, float))
        else 0.0
    )
    last_execution_ms = max((int(item.get("execution_reported_at_ms") or 0) for item in completed), default=0)
    cooldown_ms = int(settings["execution_cooldown_minutes"]) * 60 * 1000
    blockers: list[dict[str, str]] = []
    recovery_exemption = action.get("operation_type") == "restore_budget"
    if not recovery_exemption and len(completed) >= int(settings["max_daily_execution_count"]):
        blockers.append({"code": "DAILY_EXECUTION_COUNT_LIMIT", "message": "该账户今日执行次数已达到上限。"})
    if not recovery_exemption and sum(reductions) + proposed_reduction > float(settings["max_daily_budget_reduction"]):
        blockers.append({"code": "DAILY_BUDGET_REDUCTION_LIMIT", "message": "该账户今日累计预算影响金额将超过上限。"})
    if not recovery_exemption and last_execution_ms and now_ms - last_execution_ms < cooldown_ms:
        remaining = max(1, int((cooldown_ms - (now_ms - last_execution_ms) + 59_999) // 60_000))
        blockers.append({"code": "EXECUTION_COOLDOWN", "message": f"距离该账户上次执行不足冷却时间，请等待约 {remaining} 分钟。"})
    return {
        "allowed": not blockers,
        "account_key": account_key,
        "today_execution_count": len(completed),
        "max_daily_execution_count": int(settings["max_daily_execution_count"]),
        "today_budget_reduction": round(sum(reductions), 2),
        "proposed_budget_reduction": round(proposed_reduction, 2),
        "max_daily_budget_reduction": float(settings["max_daily_budget_reduction"]),
        "cooldown_minutes": int(settings["execution_cooldown_minutes"]),
        "blocked_reasons": blockers,
        "recovery_exemption": recovery_exemption,
    }


def create_budget_rollback_draft(action_id: str) -> dict[str, Any]:
    """Build a fresh restore-to-original-budget action from a verified write."""

    action_id = str(action_id or "").lower()
    original = next(
        (item for item in load_action_audit().get("actions", []) if item.get("action_id") == action_id),
        None,
    )
    if not original or original.get("state") != "verified" or original.get("operation_type") != "adjust_budget":
        raise ValueError("只有已完成页面验收的降低预算动作可以生成回滚。")
    readback = _find_plan_readback(original)
    target = original.get("target_ref") if isinstance(original.get("target_ref"), dict) else {}
    change = original.get("change") if isinstance(original.get("change"), dict) else {}
    current_value = (readback or {}).get("current_value")
    reduced_value = change.get("target_value")
    original_value = change.get("current_value")
    if not readback or int(readback.get("captured_at_ms") or 0) <= int(original.get("execution_reported_at_ms") or 0):
        raise ValueError("缺少执行后的新页面数据，请先重新读取当前千川计划页。")
    if not all(isinstance(value, (int, float)) for value in (current_value, reduced_value, original_value)):
        raise ValueError("预算回读数据不完整，不能生成回滚。")
    if abs(float(current_value) - float(reduced_value)) > 0.01:
        raise ValueError("当前预算已被再次修改，不能按旧记录回滚。")
    return build_action_draft(
        operation_type="restore_budget",
        operation_label=f"恢复原预算至 {float(original_value):g}",
        target_kind=str(target.get("kind") or "qianchuan_plan"),
        target_id=str(target.get("id") or ""),
        target_name=str(target.get("name") or ""),
        account_key=str(target.get("account_key") or ""),
        account_label=str(target.get("account_label") or ""),
        field=str(change.get("field") or "预算"),
        current_value=float(current_value),
        target_value=float(original_value),
        source="qianchuan",
        page_type="campaigns",
        captured_at_ms=int(readback.get("captured_at_ms") or 0),
        quality_score=int(readback.get("quality_score") or 0),
        confidence="high",
        evidence={
            "rollback_of_action_id": action_id,
            "spend": readback.get("spend"),
            "roi": readback.get("roi"),
            "orders": readback.get("orders"),
        },
        promotion_context=original.get("promotion_context"),
        copy_text=f"{target.get('name') or '千川计划'} | 预算 {float(current_value):g} → {float(original_value):g}（恢复原值）",
    )


def start_execution_preflight(action_id: str) -> dict[str, Any]:
    """Start a short-lived, read-only supervised-execution preflight."""

    execution_mode = load_agent_settings().get("execution_mode", "observe")
    if execution_mode == "observe":
        raise ValueError("当前账户处于观察模式，只生成诊断和建议，不能启动执行。")
    if execution_mode == "shadow":
        raise ValueError("当前账户处于影子模式，请在千川人工操作后回到插件核验结果。")
    if execution_mode != "supervised":
        raise ValueError("账户运行模式无效，不能启动执行。")
    action_id = str(action_id or "").lower()
    if not re.fullmatch(r"[a-f0-9]{24}", action_id):
        raise ValueError("动作编号无效。")
    action = next(
        (item for item in load_action_audit().get("actions", []) if item.get("action_id") == action_id),
        None,
    )
    if not action or action.get("state") != "confirmed":
        raise ValueError("只有已确认且未撤销的方案可以启动执行前检查。")
    promotion_guard = legacy_execution_guard(action.get("operation_type"), action.get("promotion_context"))
    if not promotion_guard["allowed"]:
        raise ValueError(f"{promotion_guard['code']}：{promotion_guard['reason']}")
    errors = validate_action_draft(action)
    if errors:
        messages = "；".join(dict.fromkeys(str(item.get("message") or "动作校验失败") for item in errors))
        raise ValueError(messages)
    change = action.get("change") if isinstance(action.get("change"), dict) else {}
    current_value = change.get("current_value")
    target_value = change.get("target_value")
    operation_type = str(action.get("operation_type") or "")
    if operation_type not in {"adjust_budget", "restore_budget", "pause_plan"}:
        raise ValueError("首批受监督执行只开放降低预算、恢复原预算和暂停单计划。")
    if operation_type == "pause_plan":
        if str(current_value or "") not in {"投放中", "启用", "生效中", "运行中"} or str(target_value or "") != "暂停":
            raise ValueError("暂停动作必须绑定当前仍在投放的单计划。")
    else:
        if not isinstance(current_value, (int, float)) or not isinstance(target_value, (int, float)):
            raise ValueError("预算数据不完整，不能执行。")
        if operation_type == "adjust_budget" and float(target_value) >= float(current_value):
            raise ValueError("降低预算动作不能增加预算。")
        if operation_type == "restore_budget" and float(target_value) <= float(current_value):
            raise ValueError("恢复预算动作必须回到更高的原预算。")
    quota = assess_execution_quota(action)
    if not quota["allowed"]:
        raise ValueError("；".join(item["message"] for item in quota["blocked_reasons"]))

    now_ms = int(time.time() * 1000)
    session_seed = f"{action_id}:{now_ms}".encode("utf-8")
    session = {
        "session_id": hashlib.sha256(session_seed).hexdigest()[:24],
        "action_id": action_id,
        "state": "awaiting_reread",
        "started_at_ms": now_ms,
        "expires_at_ms": now_ms + 3 * 60 * 1000,
        "pilot_scope": "reduce_restore_or_pause_single_plan",
        "operation_type": operation_type,
        "current_value": current_value,
        "target_value": target_value,
        "quota": quota,
        "write_enabled": False,
        "execution_enabled": False,
    }
    with _state_lock:
        _save_execution_preflight(session)
    return build_execution_preflight_report()


def stop_execution_preflight(session_id: str) -> dict[str, Any]:
    session_id = str(session_id or "").lower()
    stored = load_execution_preflight().get("session")
    if not stored or stored.get("session_id") != session_id:
        raise ValueError("未找到对应的执行前检查会话。")
    stopped = {
        **stored,
        "state": "stopped",
        "stopped_at_ms": int(time.time() * 1000),
        "write_enabled": False,
        "execution_enabled": False,
    }
    with _state_lock:
        _save_execution_preflight(stopped)
    return build_execution_preflight_report()


def authorize_execution_preflight(session_id: str, confirmation_text: str) -> dict[str, Any]:
    """Issue a short-lived, single-use grant after an exact final confirmation.

    The grant is deliberately not an execution command.  A future browser
    executor must consume it atomically and perform its own final page checks.
    """

    session_id = str(session_id or "").lower()
    if not re.fullmatch(r"[a-f0-9]{24}", session_id):
        raise ValueError("执行前检查会话编号无效。")
    report = build_execution_preflight_report()
    session = report.get("session") if isinstance(report.get("session"), dict) else {}
    if session.get("session_id") != session_id:
        raise ValueError("未找到对应的执行前检查会话。")
    if report.get("state") != "ready_for_final_confirmation":
        raise ValueError("执行前检查尚未全部通过，不能生成最终授权。")
    action = report.get("action") if isinstance(report.get("action"), dict) else {}
    if session.get("operation_type") == "restore_budget":
        expected_text = f"确认恢复预算至{action.get('target_value')}"
    elif session.get("operation_type") == "pause_plan":
        expected_text = "确认暂停该计划"
    else:
        expected_text = f"确认降低预算至{action.get('target_value')}"
    if str(confirmation_text or "").strip() != expected_text:
        raise ValueError(f"确认口令不一致，请完整输入：{expected_text}")

    now_ms = int(time.time() * 1000)
    grant_seed = f"{session_id}:{session.get('action_id')}:{now_ms}:{os.urandom(16).hex()}".encode("utf-8")
    authorized = {
        **session,
        "state": "authorized",
        "authorized_at_ms": now_ms,
        "authorization_expires_at_ms": now_ms + 60 * 1000,
        "authorization_id": hashlib.sha256(grant_seed).hexdigest()[:32],
        "authorization_consumed": False,
        "confirmation_text_hash": hashlib.sha256(expected_text.encode("utf-8")).hexdigest(),
        "write_enabled": False,
        "execution_enabled": False,
    }
    with _state_lock:
        _save_execution_preflight(authorized)
    return build_execution_preflight_report()


def consume_execution_authorization(authorization_id: str) -> dict[str, Any]:
    """Atomically consume a valid grant for a future in-process executor."""

    authorization_id = str(authorization_id or "").lower()
    if not re.fullmatch(r"[a-f0-9]{32}", authorization_id):
        raise ValueError("执行授权编号无效。")
    with _state_lock:
        session = load_execution_preflight().get("session")
        if not session or session.get("authorization_id") != authorization_id:
            raise ValueError("未找到对应的执行授权。")
        if session.get("state") != "authorized" or session.get("authorization_consumed"):
            raise ValueError("执行授权已使用或已失效。")
        action = next(
            (item for item in load_action_audit().get("actions", []) if item.get("action_id") == session.get("action_id")),
            None,
        )
        if not action or action.get("state") != "confirmed":
            invalidated = {
                **session,
                "state": "invalidated",
                "invalidated_at_ms": int(time.time() * 1000),
                "execution_enabled": False,
                "write_enabled": False,
            }
            _save_execution_preflight(invalidated)
            raise ValueError("授权对应的动作已撤销、已停止或不再允许执行。")
        promotion_guard = legacy_execution_guard(action.get("operation_type"), action.get("promotion_context"))
        if not promotion_guard["allowed"]:
            raise ValueError(f"{promotion_guard['code']}：{promotion_guard['reason']}")
        quota = assess_execution_quota(action)
        if not quota["allowed"]:
            raise ValueError("；".join(item["message"] for item in quota["blocked_reasons"]))
        now_ms = int(time.time() * 1000)
        if int(session.get("authorization_expires_at_ms") or 0) <= now_ms:
            _save_execution_preflight({**session, "state": "expired", "execution_enabled": False, "write_enabled": False})
            raise ValueError("执行授权已过期。")
        consumed = {
            **session,
            "state": "authorization_consumed",
            "authorization_consumed": True,
            "authorization_consumed_at_ms": now_ms,
            "execution_enabled": False,
            "write_enabled": False,
        }
        _save_execution_preflight(consumed)
    return {
        **consumed,
        "execution_request": _execution_request_for_action(action),
    }


def _execution_request_for_action(action: dict[str, Any]) -> dict[str, Any]:
    target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
    change = action.get("change") if isinstance(action.get("change"), dict) else {}
    return {
        "operation_type": action.get("operation_type"),
        "account_key": target.get("account_key"),
        "plan_id": target.get("id"),
        "plan_name": target.get("name"),
        "field": change.get("field"),
        "expected_current_value": change.get("current_value"),
        "target_value": change.get("target_value"),
        "promotion_context": build_promotion_context(action.get("promotion_context")),
        "mode": "supervised_submit",
    }


def preview_execution_authorization(authorization_id: str) -> dict[str, Any]:
    """Return probe parameters without consuming or extending the grant."""

    authorization_id = str(authorization_id or "").lower()
    if not re.fullmatch(r"[a-f0-9]{32}", authorization_id):
        raise ValueError("执行授权编号无效。")
    session = load_execution_preflight().get("session")
    if not session or session.get("authorization_id") != authorization_id:
        raise ValueError("未找到对应的执行授权。")
    if session.get("state") != "authorized" or session.get("authorization_consumed"):
        raise ValueError("执行授权已使用或已失效。")
    if int(session.get("authorization_expires_at_ms") or 0) <= int(time.time() * 1000):
        raise ValueError("执行授权已过期。")
    action = next(
        (item for item in load_action_audit().get("actions", []) if item.get("action_id") == session.get("action_id")),
        None,
    )
    if not action or action.get("state") != "confirmed":
        raise ValueError("授权对应的动作不可执行。")
    promotion_guard = legacy_execution_guard(action.get("operation_type"), action.get("promotion_context"))
    if not promotion_guard["allowed"]:
        raise ValueError(f"{promotion_guard['code']}：{promotion_guard['reason']}")
    quota = assess_execution_quota(action)
    if not quota["allowed"]:
        raise ValueError("；".join(item["message"] for item in quota["blocked_reasons"]))
    return {
        "action_id": session.get("action_id"),
        "authorization_expires_at_ms": session.get("authorization_expires_at_ms"),
        "execution_request": _execution_request_for_action(action),
        "quota": quota,
    }


def record_execution_result(action_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Persist the browser executor receipt without trusting it as verification."""

    action_id = str(action_id or "").lower()
    if not re.fullmatch(r"[a-f0-9]{24}", action_id) or not isinstance(result, dict):
        raise ValueError("执行回执无效。")
    submitted = result.get("submitted") is True
    now_ms = int(time.time() * 1000)
    with _state_lock:
        audit = load_action_audit()
        action = next((item for item in audit["actions"] if item.get("action_id") == action_id), None)
        if not action or action.get("state") != "confirmed":
            raise ValueError("执行动作不存在、已撤销或已记录。")
        executing = transition_action(action, "executing", allow_execution=True)
        updated = transition_action(executing, "succeeded" if submitted else "failed", allow_execution=True)
        updated.update({
            "execution_source": "qianchuan_browser_supervised",
            "execution_reported_at_ms": now_ms,
            "execution_receipt": {
                "submitted": submitted,
                "platform_success_observed": result.get("platform_success_observed") is True,
                "plan_id": str(result.get("plan_id") or ""),
                "target_value": result.get("target_value"),
                "error": str(result.get("error") or "")[:300],
            },
            "execution_note": "浏览器已提交，等待页面重新读取验收。" if submitted else "浏览器执行未完成，未记录为成功。",
        })
        actions = [updated if item.get("action_id") == action_id else item for item in audit["actions"]]
        _atomic_json_write(
            _action_audit_path(),
            {"schema_version": 1, "updated_at": _now_label(), "execution_enabled": True, "actions": actions},
        )
    return updated


def verify_execution_result(action_id: str) -> dict[str, Any]:
    action_id = str(action_id or "").lower()
    with _state_lock:
        audit = load_action_audit()
        action = next((item for item in audit["actions"] if item.get("action_id") == action_id), None)
        if not action or action.get("state") not in {"succeeded", "verified"}:
            raise ValueError("只有已提交动作可以进行页面回读验收。")
        readback = _find_plan_readback(action)
        target_value = (action.get("change") or {}).get("target_value")
        submitted_at = int(action.get("execution_reported_at_ms") or 0)
        matched = bool(
            readback
            and int(readback.get("captured_at_ms") or 0) > submitted_at
            and isinstance(readback.get("current_value"), (int, float))
            and isinstance(target_value, (int, float))
            and abs(float(readback["current_value"]) - float(target_value)) <= 0.01
        )
        if matched and action.get("state") != "verified":
            action = transition_action(action, "verified")
            action["verified_at_ms"] = int(time.time() * 1000)
            action["verification_readback"] = readback
            actions = [action if item.get("action_id") == action_id else item for item in audit["actions"]]
            rollback_of = str((action.get("evidence_ref") or {}).get("rollback_of_action_id") or "")
            if rollback_of:
                next_actions = []
                for item in actions:
                    if item.get("action_id") == rollback_of and item.get("state") == "verified":
                        rolled_back = transition_action(item, "rolled_back")
                        rolled_back["rolled_back_at_ms"] = action["verified_at_ms"]
                        rolled_back["rollback_action_id"] = action_id
                        next_actions.append(rolled_back)
                    else:
                        next_actions.append(item)
                actions = next_actions
            _atomic_json_write(
                _action_audit_path(),
                {"schema_version": 1, "updated_at": _now_label(), "execution_enabled": True, "actions": actions},
            )
    return {"action_id": action_id, "verified": matched, "state": action.get("state"), "readback": readback}


def build_execution_effectiveness_report(*, now_ms: int | None = None) -> dict[str, Any]:
    """Evaluate verified budget actions after a spend-sensitive observation window."""

    now_ms = int(now_ms or time.time() * 1000)
    items: list[dict[str, Any]] = []
    for action in load_action_audit().get("actions", []):
        if action.get("state") not in {"succeeded", "verified"}:
            continue
        executed_at_ms = int(action.get("execution_reported_at_ms") or 0)
        if executed_at_ms <= 0:
            continue
        target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
        change = action.get("change") if isinstance(action.get("change"), dict) else {}
        evidence = action.get("evidence_ref") if isinstance(action.get("evidence_ref"), dict) else {}
        before = {
            "spend": evidence.get("spend"),
            "roi": evidence.get("roi"),
            "orders": evidence.get("orders"),
            "budget": change.get("current_value"),
        }
        after = _find_plan_readback(action)
        spend_value = before.get("spend")
        observation_minutes = 30 if isinstance(spend_value, (int, float)) and spend_value >= 1000 else 120 if isinstance(spend_value, (int, float)) and spend_value >= 300 else 24 * 60
        due_at_ms = executed_at_ms + observation_minutes * 60 * 1000
        fresh_after = bool(after and int(after.get("captured_at_ms") or 0) > executed_at_ms)
        if now_ms < due_at_ms:
            status = "waiting"
            label = f"等待 {observation_minutes // 60} 小时复查" if observation_minutes >= 60 else f"等待 {observation_minutes} 分钟复查"
            verdict = "观察窗口尚未结束，不追加动作。"
        elif not fresh_after:
            status = "needs_reread"
            label = "等待重新读取"
            verdict = "请打开对应千川计划页重新读取后再判断效果。"
        else:
            before_roi = before.get("roi")
            after_roi = after.get("roi")
            before_orders = before.get("orders")
            after_orders = after.get("orders")
            improved = bool(
                isinstance(after_roi, (int, float))
                and isinstance(before_roi, (int, float))
                and float(after_roi) >= float(before_roi) * 1.2
            ) or bool(
                isinstance(after_orders, (int, float))
                and isinstance(before_orders, (int, float))
                and float(after_orders) > float(before_orders)
            )
            status = "effective" if improved else "review"
            label = "止损有效" if improved else "需要人工复核"
            verdict = (
                "ROI 或成交已经改善，保持当前预算并继续观察。"
                if improved
                else "暂未观察到明确改善；建议检查素材、人群和转化承接，必要时生成原预算回滚方案。"
            )
        items.append({
            "action_id": action.get("action_id"),
            "account_key": target.get("account_key"),
            "account_label": target.get("account_label"),
            "plan_name": target.get("name"),
            "executed_at_ms": executed_at_ms,
            "due_at_ms": due_at_ms,
            "observation_window_minutes": observation_minutes,
            "status": status,
            "status_label": label,
            "verdict": verdict,
            "before": before,
            "after": after,
            "change": {
                "from": change.get("current_value"),
                "to": change.get("target_value"),
            },
            "rollback_available": status == "review" and action.get("state") == "verified",
        })
    items.sort(key=lambda item: (0 if item["status"] in {"review", "needs_reread"} else 1, -item["executed_at_ms"]))
    return {
        "observation_window_policy": "高消耗30分钟、普通消耗2小时、低消耗24小时",
        "items": items[:100],
        "summary": {
            "total": len(items),
            "waiting": sum(item["status"] == "waiting" for item in items),
            "needs_reread": sum(item["status"] == "needs_reread" for item in items),
            "effective": sum(item["status"] == "effective" for item in items),
            "review": sum(item["status"] == "review" for item in items),
        },
    }


def build_value_ledger() -> dict[str, Any]:
    """Summarize verified operating value without presenting estimates as settled revenue."""
    catalog = build_store_catalog()
    selected_store_key = str(catalog.get("selected_store_key") or "")
    selected_store = next((item for item in catalog.get("stores", []) if item.get("key") == selected_store_key), None)
    if not selected_store:
        return {
            "generated_at": _now_label(),
            "summary": {"verified_actions": 0, "evaluated_actions": 0, "effective_actions": 0, "effective_rate": None, "protected_budget_capacity": 0.0, "paused_plans": 0, "reviewed_spend": 0.0, "waiting_review": 0, "completed_tasks": 0, "tasks_waiting_review": 0, "blocked_tasks": 0},
            "recent": [], "recent_task_outcomes": [], "scope": "unresolved", "trusted_scope": False,
            "note": "尚未识别并选择匿名店铺；未归属数据不会累计到价值账本。",
        }
    effectiveness = build_execution_effectiveness_report()
    linked_accounts = set(selected_store.get("account_keys") or [])
    items = [item for item in effectiveness.get("items", []) if item.get("account_key") in linked_accounts]
    evaluated = [item for item in items if item.get("status") in {"effective", "review"}]
    effective = [item for item in evaluated if item.get("status") == "effective"]
    protected_budget = 0.0
    reviewed_spend = 0.0
    paused_plans = 0
    for item in evaluated:
        before = item.get("before") if isinstance(item.get("before"), dict) else {}
        spend = before.get("spend")
        if isinstance(spend, (int, float)):
            reviewed_spend += max(0.0, float(spend))
    for item in effective:
        change = item.get("change") if isinstance(item.get("change"), dict) else {}
        source, target = change.get("from"), change.get("to")
        if isinstance(source, (int, float)) and isinstance(target, (int, float)):
            protected_budget += max(0.0, float(source) - float(target))
        elif str(target or "") == "暂停":
            paused_plans += 1
    effective_rate = round(len(effective) / len(evaluated) * 100, 1) if evaluated else None
    task_states = load_task_states()
    task_outcomes = [
        {"task_id": task_id, **state}
        for task_id, state in task_states.items()
        if isinstance(state, dict) and state.get("status") in {"observing", "blocked", "done"}
    ]
    task_outcomes.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return {
        "generated_at": _now_label(),
        "summary": {
            "verified_actions": len(items),
            "evaluated_actions": len(evaluated),
            "effective_actions": len(effective),
            "effective_rate": effective_rate,
            "protected_budget_capacity": round(protected_budget, 2),
            "paused_plans": paused_plans,
            "reviewed_spend": round(reviewed_spend, 2),
            "waiting_review": sum(item.get("status") in {"waiting", "needs_reread"} for item in items),
            "completed_tasks": sum(item.get("status") == "done" for item in task_states.values() if isinstance(item, dict)),
            "tasks_waiting_review": sum(item.get("status") == "observing" for item in task_states.values() if isinstance(item, dict)),
            "blocked_tasks": sum(item.get("status") == "blocked" for item in task_states.values() if isinstance(item, dict)),
        },
        "recent": effective[:5],
        "recent_task_outcomes": task_outcomes[:10],
        "scope": _task_scope_key(),
        "trusted_scope": True,
        "note": "受控预算幅度和复盘消耗用于衡量 Agent 参与范围，不等同于实际节省、结算收入或增量 GMV。",
    }


def build_execution_preflight_report() -> dict[str, Any]:
    """Recheck a short-lived session against the latest Qianchuan page."""

    stored = load_execution_preflight().get("session")
    if not stored:
        return {
            "mode": "supervised_preflight",
            "state": "idle",
            "state_label": "尚未启动",
            "execution_enabled": False,
            "write_enabled": False,
            "session": None,
            "checks": [],
        }

    now_ms = int(time.time() * 1000)
    state = str(stored.get("state") or "awaiting_reread")
    authorization_expires_at_ms = int(stored.get("authorization_expires_at_ms") or 0)
    if state == "authorized" and authorization_expires_at_ms <= now_ms:
        state = "expired"
        stored = {**stored, "state": state, "write_enabled": False, "execution_enabled": False}
        with _state_lock:
            _save_execution_preflight(stored)
    elif state not in {"stopped", "expired", "authorized"} and int(stored.get("expires_at_ms") or 0) <= now_ms:
        state = "expired"
        stored = {**stored, "state": state, "write_enabled": False, "execution_enabled": False}
        with _state_lock:
            _save_execution_preflight(stored)

    action = next(
        (item for item in load_action_audit().get("actions", []) if item.get("action_id") == stored.get("action_id")),
        None,
    )
    readback = _find_plan_readback(action) if isinstance(action, dict) else None
    target = action.get("target_ref") if isinstance(action, dict) and isinstance(action.get("target_ref"), dict) else {}
    change = action.get("change") if isinstance(action, dict) and isinstance(action.get("change"), dict) else {}
    started_at_ms = int(stored.get("started_at_ms") or 0)
    current_value = change.get("current_value")
    target_value = change.get("target_value")
    operation_type = str((action or {}).get("operation_type") or "")
    observed = (readback or {}).get("delivery_status") if operation_type == "pause_plan" else (readback or {}).get("current_value")
    checks = [
        {
            "id": "fresh_reread",
            "label": "确认后重新读取页面",
            "passed": bool(readback and int(readback.get("captured_at_ms") or 0) > started_at_ms),
            "detail": "必须使用本次检查启动后的新页面数据。",
        },
        {
            "id": "account_match",
            "label": "千川账号一致",
            "passed": bool(readback and readback.get("account_key") == target.get("account_key")),
            "detail": str((readback or {}).get("account_label") or target.get("account_label") or "账号未识别"),
        },
        {
            "id": "plan_match",
            "label": "计划唯一 ID 一致",
            "passed": bool(readback and readback.get("plan_id") == target.get("id")),
            "detail": str(target.get("id") or "缺少计划 ID"),
        },
        {
            "id": "quality",
            "label": "页面质量分不低于 70",
            "passed": bool(readback and int(readback.get("quality_score") or 0) >= 70),
            "detail": f"当前质量分 {int((readback or {}).get('quality_score') or 0)}",
        },
        {
            "id": "current_value_match",
            "label": "当前计划状态未被其他人修改" if operation_type == "pause_plan" else "当前预算未被其他人修改",
            "passed": bool(
                observed in {"投放中", "启用", "生效中", "运行中"}
                and str(current_value or "") in {"投放中", "启用", "生效中", "运行中"}
                if operation_type == "pause_plan" else
                isinstance(observed, (int, float))
                and isinstance(current_value, (int, float))
                and abs(float(observed) - float(current_value)) <= 0.01
            ),
            "detail": f"方案值 {current_value if current_value is not None else '--'}，页面值 {observed if observed is not None else '--'}",
        },
        {
            "id": "pilot_scope",
            "label": "符合首批止损或回滚范围",
            "passed": (
                operation_type == "pause_plan" and str(current_value or "") in {"投放中", "启用", "生效中", "运行中"} and str(target_value or "") == "暂停"
            ) if operation_type == "pause_plan" else bool(
                isinstance(current_value, (int, float))
                and isinstance(target_value, (int, float))
                and float(current_value) > 0
                and (0 < (float(current_value) - float(target_value)) / float(current_value) <= 0.30 if operation_type == "adjust_budget" else 0 < (float(target_value) - float(current_value)) / float(current_value) <= 0.50)
            ),
            "detail": "当前状态为投放中且目标为暂停。" if operation_type == "pause_plan" else "降低预算不超过 30%；恢复预算必须绑定原执行记录且增幅不超过 50%。",
        },
    ]

    if state == "authorization_consumed":
        label = "授权凭证已使用"
    elif state == "authorized":
        label = "最终授权已生成"
    elif state == "stopped":
        label = "已紧急停止"
    elif state == "expired":
        label = "检查会话已过期"
    elif not checks[0]["passed"]:
        state = "awaiting_reread"
        label = "等待重新读取当前千川页"
    elif all(item["passed"] for item in checks):
        state = "ready_for_final_confirmation"
        label = "执行前检查已通过"
    else:
        state = "blocked"
        label = "执行前检查未通过"

    report_session = {
        **stored,
        "state": state,
        "write_enabled": False,
        "execution_enabled": False,
    }
    if state != stored.get("state") and state not in {"awaiting_reread", "blocked"}:
        with _state_lock:
            _save_execution_preflight(report_session)
    return {
        "mode": "supervised_preflight",
        "state": state,
        "state_label": label,
        "execution_enabled": False,
        "write_enabled": False,
        "session": report_session,
        "action": {
            "plan_name": str(target.get("name") or ""),
            "account_label": str(target.get("account_label") or ""),
            "plan_id": str(target.get("id") or ""),
            "field": change.get("field"),
            "current_value": current_value,
            "target_value": target_value,
            "operation_type": action.get("operation_type") if isinstance(action, dict) else "",
            "impact_preview": {
                "budget_change": round(float(target_value) - float(current_value), 2) if isinstance(current_value, (int, float)) and isinstance(target_value, (int, float)) else None,
                "change_percent": round((float(target_value) - float(current_value)) / float(current_value) * 100, 1) if isinstance(current_value, (int, float)) and isinstance(target_value, (int, float)) and float(current_value) else None,
                "today_spend": ((action or {}).get("evidence_ref") or {}).get("spend") if isinstance((action or {}).get("evidence_ref"), dict) else None,
                "daily_budget_impact_limit": load_agent_settings().get("max_daily_budget_reduction"),
                "rollback_condition": "执行后页面验收成功，且当前预算仍等于本次目标值。",
            },
        },
        "readback": readback,
        "checks": checks,
        "next_step": (
            "授权凭证已被原子消费，不能再次使用；请等待平台提交回执和执行后页面验收。"
            if state == "authorization_consumed"
            else
            "已生成 60 秒单次授权凭证；受监督执行器将只提交本次降低预算动作。"
            if state == "authorized"
            else
            "全部闸门已通过；输入与目标预算绑定的最终确认口令后，受监督执行器将提交本次降低预算动作。"
            if state == "ready_for_final_confirmation"
            else "重新读取当前千川页面，系统会自动复核账号、计划、预算和质量。"
            if state == "awaiting_reread"
            else "停止当前会话后重新生成方案。"
            if state in {"blocked", "expired"}
            else "会话已停止，未执行任何千川操作。"
        ),
    }


def build_shadow_execution_report() -> dict[str, Any]:
    """Compare user-reported manual actions with a later Qianchuan readback."""
    actions = [
        item for item in load_action_audit().get("actions", [])
        if isinstance(item, dict) and item.get("state") == "confirmed"
    ]
    markers = {
        str(item.get("action_id") or ""): item
        for item in load_shadow_execution().get("records", [])
        if isinstance(item, dict)
    }
    items: list[dict[str, Any]] = []
    for action in actions:
        action_id = str(action.get("action_id") or "")
        target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
        change = action.get("change") if isinstance(action.get("change"), dict) else {}
        marker = markers.get(action_id)
        readback = _find_plan_readback(action)
        status = "awaiting_manual_action"
        status_label = "等待人工执行"
        detail = "方案已确认；请回到巨量千川人工执行，插件不会自动点击。"
        if marker:
            reported_at = int(marker.get("reported_applied_at_ms") or 0)
            if not readback or int(readback.get("captured_at_ms") or 0) <= reported_at:
                status = "awaiting_readback"
                status_label = "等待重新读取"
                detail = "已记录人工执行声明；请打开对应千川计划页面并重新读取。"
            else:
                observed = readback.get("current_value")
                current = change.get("current_value")
                target_value = change.get("target_value")
                if isinstance(observed, (int, float)) and isinstance(target_value, (int, float)) and abs(float(observed) - float(target_value)) <= 0.01:
                    status = "matched"
                    status_label = "回读已匹配"
                    detail = f"最新页面预算为 {observed:g}，与确认目标一致。"
                elif isinstance(observed, (int, float)) and isinstance(current, (int, float)) and abs(float(observed) - float(current)) <= 0.01:
                    status = "not_changed"
                    status_label = "页面尚未变化"
                    detail = f"最新页面预算仍为 {observed:g}，尚未观察到确认方案生效。"
                elif isinstance(observed, (int, float)):
                    status = "changed_differently"
                    status_label = "检测到其他修改"
                    detail = f"最新页面预算为 {observed:g}，与确认目标 {target_value} 不一致，请人工核对。"
                else:
                    status = "unverifiable"
                    status_label = "无法核验"
                    detail = "已读取计划，但没有获得可比较的当前预算。"
        items.append(
            {
                "action_id": action_id,
                "operation_type": action.get("operation_type"),
                "operation_label": action.get("operation_label"),
                "account_key": str(target.get("account_key") or ""),
                "account_label": str(target.get("account_label") or ""),
                "plan_id": str(target.get("id") or ""),
                "plan_name": str(target.get("name") or ""),
                "field": change.get("field"),
                "before_value": change.get("current_value"),
                "target_value": change.get("target_value"),
                "confirmed_at_ms": int(action.get("confirmed_at_ms") or 0),
                "reported_applied_at_ms": int((marker or {}).get("reported_applied_at_ms") or 0),
                "status": status,
                "status_label": status_label,
                "detail": detail,
                "readback": readback,
                "execution_enabled": False,
            }
        )
    order = {"changed_differently": 0, "not_changed": 1, "unverifiable": 2, "awaiting_readback": 3, "awaiting_manual_action": 4, "matched": 5}
    items.sort(key=lambda item: (order.get(item["status"], 9), -item["confirmed_at_ms"]))
    return {
        "generated_at": _now_label(),
        "mode": "shadow_only",
        "execution_enabled": False,
        "items": items,
        "summary": {
            "total": len(items),
            "awaiting_manual_action": sum(item["status"] == "awaiting_manual_action" for item in items),
            "awaiting_readback": sum(item["status"] == "awaiting_readback" for item in items),
            "matched": sum(item["status"] == "matched" for item in items),
            "needs_attention": sum(item["status"] in {"not_changed", "changed_differently", "unverifiable"} for item in items),
        },
    }


def _clean_entity_name(value: Any, fallback: str) -> str:
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    ignored = {"扶持中", "投放中", "商品", "素材", "保", "审核建议"}
    candidates = [
        line for line in lines
        if line not in ignored and not line.startswith("ID：") and not line.startswith("ID:") and not line.isdigit()
    ]
    return (max(candidates, key=len) if candidates else fallback)[:100]


def _plan_workbench_fields(item: dict[str, Any], task_states: dict[str, Any]) -> dict[str, Any]:
    action_type = str(item.get("action_type") or "")
    evidence = item.get("evidence") or {}
    roi = evidence.get("roi")
    roi_target = evidence.get("roi_target")
    ctr = evidence.get("ctr")
    orders = evidence.get("orders")
    definitions = {
        "stop_loss": {
            "diagnosis": "有点击无成交" if ctr and not orders else "高消耗未转化",
            "judgment": "继续消耗的边际风险已高于继续观察的价值，应先止损再排查素材、人群和商品承接。",
            "adjustment_range": "预算下调 30%，或暂停新增消耗；任何资金动作均由投手人工确认。",
            "observation_window": "调整后观察 2 小时或 1 个完整转化窗口。",
            "acceptance": f"出现有效成交，且 ROI 恢复到 {float(roi_target or 0) * 0.8:g} 以上；否则继续止损。",
        },
        "reduce_budget": {
            "diagnosis": "ROI 明显低于目标",
            "judgment": "当前消耗已达到判断门槛，低效计划继续原预算运行会放大亏损。",
            "adjustment_range": "单次预算建议下调 20%，不要同时修改出价、素材和人群。",
            "observation_window": "调整后观察 2 小时或 1 个完整转化窗口。",
            "acceptance": f"ROI 至少恢复到 {float(roi_target or 0) * 0.8:g}，且成交成本不继续上升。",
        },
        "optimize": {
            "diagnosis": "素材点击不足" if ctr is not None and ctr < 1 else "ROI 待改善",
            "judgment": "数据尚未达到强制止损条件，但当前效率不足以支持放量，应先修复转化瓶颈。",
            "adjustment_range": "预算保持不变；一次只替换 1 组素材或优化 1 个承接环节。",
            "observation_window": "新素材累计 100 次点击或运行 2 小时后复盘。",
            "acceptance": f"点击率改善且 ROI 达到目标 {float(roi_target or 0):g}；未改善则进入止损评估。",
        },
        "scale_cautiously": {
            "diagnosis": "表现稳定，可谨慎放量",
            "judgment": "当前 ROI 和成交样本达到放量条件，但仍需控制单次调整幅度，避免打乱模型。",
            "adjustment_range": "单次预算增加 10%–15%，一个观察窗口内只调整一次。",
            "observation_window": "放量后观察 2–4 小时或 1 个完整转化窗口。",
            "acceptance": f"ROI 保持在目标 {float(roi_target or 0):g} 以上，成交量增长且成本未明显上升。",
        },
        "inspect_plans": {
            "diagnosis": "账户汇总异常，待定位计划",
            "judgment": "只有账户汇总数据，无法安全定位到具体计划，不应直接批量调整。",
            "adjustment_range": "暂不调整预算；先同步计划列表并锁定异常计划。",
            "observation_window": "计划明细同步完成后立即重新诊断。",
            "acceptance": "定位到具体计划，并补齐消耗、ROI、成交和素材证据。",
        },
        "hold_and_observe": {
            "diagnosis": "账户表现稳定，继续观察",
            "judgment": "汇总表现达到目标，但计划级证据不足，暂不执行批量放量。",
            "adjustment_range": "预算保持不变，补齐计划明细后再判断。",
            "observation_window": "下一个完整转化窗口。",
            "acceptance": "计划级 ROI、成交和消耗数据完整，并确认无异常计划。",
        },
    }
    fields = definitions.get(action_type, {
        "diagnosis": "计划需要人工复核",
        "judgment": "当前证据不足以自动形成明确调整结论。",
        "adjustment_range": "暂不修改预算或出价。",
        "observation_window": "补齐数据后重新诊断。",
        "acceptance": "消耗、ROI、成交与素材证据完整。",
    })
    title = f"{item.get('plan') or '千川计划'} · {fields['diagnosis']}"
    task_id = hashlib.sha256(f"投放运营|{title}".encode("utf-8")).hexdigest()[:16]
    task_state = task_states.get(task_id, {})
    return {
        **fields,
        "found": str(item.get("reason") or "当前计划数据异常"),
        "action": str(item.get("suggestion") or "请回到千川后台核对。"),
        "owner": "投放运营",
        "workbench_title": title,
        "task_id": task_id,
        "task_status": task_state.get("status", "todo"),
        "task_updated_at": task_state.get("updated_at"),
        "current_roi": roi,
    }


def build_plan_recommendations(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    settings = settings or load_agent_settings()
    roi_target = float(settings["roi_target"])
    min_spend = float(settings["min_spend_for_action"])
    results: list[dict[str, Any]] = []
    records = _table_records("doudian", {"qianchuan_campaigns", "qianchuan_live", "qianchuan_report"})
    records.extend(_table_records("qianchuan", {"campaigns", "qianchuan_live", "report"}))

    for entry in records:
        record = entry["record"]
        _, plan_value = _pick(record, ("计划名称", "计划", "项目名称", "广告组", "单元名称", "抖音号"))
        plan_lines = [line.strip() for line in str(plan_value or "").splitlines() if line.strip() and line.strip() not in {"设置直播规划", "素材"}]
        if plan_lines and plan_lines[0] == "直播大屏" and len(plan_lines) > 1:
            plan = f"直播大屏 · {plan_lines[1]}"[:100]
        else:
            plan = (plan_lines[0] if plan_lines else f"第 {entry['row_index'] + 1} 行计划")[:100]
        if re.match(r"^共\s*\d+\s*(?:条计划|个抖音号)", plan):
            continue
        spend = _evidence_value(record, ("消耗", "花费", "支出"))
        roi = _evidence_value(record, ("支付roi", "成交roi", "roi"))
        orders = _evidence_value(record, ("成交订单", "支付订单", "成交数", "转化数"))
        ctr = _evidence_value(record, ("点击率", "ctr"))
        if spend is None and roi is None:
            continue
        _, status_value = _pick(record, ("投放状态", "计划状态", "状态"))
        if spend == 0 and "暂停" in str(status_value or ""):
            continue

        plan_roi_target = _extract_labeled_number(record, "ROI目标")
        effective_roi_target = plan_roi_target or roi_target
        evidence = {
            "spend": spend,
            "roi": roi,
            "roi_target": effective_roi_target,
            "orders": orders,
            "ctr": ctr,
            "page_type": entry["page_type"],
            "_record": record,
        }
        confidence = "high" if entry["quality_score"] >= 70 and spend is not None and roi is not None else "medium"
        base = {
            "id": f"{entry['page_type']}-{entry['table_index']}-{entry['row_index']}",
            "plan": plan,
            "evidence": evidence,
            "confidence": confidence,
            "guardrail": "仅生成建议；执行前请核对统计周期、归因口径和当日预算。",
        }

        if spend is not None and spend >= min_spend and (orders == 0 or orders is None and roi == 0):
            results.append(
                {
                    **base,
                    "level": "high",
                    "action_type": "stop_loss",
                    "suggestion": "先降预算 30% 或暂停新增消耗，检查素材、人群和商品承接后再恢复。",
                    "reason": f"消耗已达到 {spend:g}，但当前未观察到成交。",
                    "action_params": _action_params_for_plan(plan, "stop_loss", evidence, entry, confidence),
                }
            )
        elif roi is not None and spend is not None and spend >= min_spend and roi < effective_roi_target * 0.8:
            results.append(
                {
                    **base,
                    "level": "high",
                    "action_type": "reduce_budget",
                    "suggestion": "建议先降预算 20%，保留观察窗口；优先替换低点击素材并核对商品转化。",
                    "reason": f"ROI {roi:g} 明显低于目标 {effective_roi_target:g}，且消耗已达到判断门槛。",
                    "action_params": _action_params_for_plan(plan, "reduce_budget", evidence, entry, confidence),
                }
            )
        elif roi is not None and roi < effective_roi_target:
            reason = f"ROI {roi:g} 低于目标 {effective_roi_target:g}，暂不适合放量。"
            suggestion = "预算保持不变，先优化素材点击率与商品承接；达到目标后再逐级放量。"
            if ctr is not None and ctr < 1:
                suggestion = "预算保持不变，优先更换前 3 秒表达、封面和卖点；不要先提高出价。"
                reason += f" 当前点击率为 {ctr:g}。"
            results.append({**base, "level": "warning", "action_type": "optimize", "suggestion": suggestion, "reason": reason, "action_params": _action_params_for_plan(plan, "optimize", evidence, entry, confidence)})
        elif roi is not None and roi >= effective_roi_target and (orders or 0) >= 3:
            results.append(
                {
                    **base,
                    "level": "opportunity",
                    "action_type": "scale_cautiously",
                    "suggestion": "可尝试增加预算 10%–15%，每次只调一次，并观察一个完整转化窗口。",
                    "reason": f"ROI {roi:g} 达到目标 {effective_roi_target:g}，且已有 {orders:g} 个成交。",
                    "action_params": _action_params_for_plan(plan, "scale_cautiously", evidence, entry, confidence),
                }
            )

    if not results:
        roi_metrics = _metric_matches("qianchuan", ("roi", "支付roi", "成交roi"))
        spend_metrics = _metric_matches("qianchuan", ("消耗", "花费"))
        if roi_metrics:
            item, label, value = roi_metrics[0]
            roi = _parse_number(value)
            spend = _parse_number(spend_metrics[0][2]) if spend_metrics else None
            if roi is not None:
                results.append(
                    {
                        "id": "account-summary",
                        "plan": "账户汇总",
                        "level": "warning" if roi < roi_target else "opportunity",
                        "action_type": "inspect_plans" if roi < roi_target else "hold_and_observe",
                        "suggestion": "打开千川计划列表同步明细，定位具体计划后再调整预算。",
                        "reason": f"当前汇总 {label} 为 {value}，计划级证据尚不完整。",
                        "evidence": {"roi": roi, "spend": spend, "page_type": item["page_type"]},
                        "confidence": "low",
                        "guardrail": "没有计划明细时不建议执行批量调价。",
                    }
                )

    priority = {"high": 0, "warning": 1, "opportunity": 2, "info": 3}
    ordered = sorted(results, key=lambda item: (priority.get(item["level"], 9), -(item["evidence"].get("spend") or 0)))
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in ordered:
        unique.setdefault((item["plan"], item["action_type"]), item)
    task_states = load_task_states()
    audit_states = {
        str(action.get("action_id") or ""): action
        for action in load_action_audit().get("actions", [])
        if isinstance(action, dict)
    }
    cleaned: list[dict[str, Any]] = []
    for item in list(unique.values())[:20]:
        ev = item.get("evidence")
        if isinstance(ev, dict):
            ev.pop("_record", None)
        action_params = item.get("action_params")
        if isinstance(action_params, dict):
            saved_action = audit_states.get(str(action_params.get("action_id") or ""))
            if saved_action:
                action_params = {
                    **action_params,
                    "state": saved_action.get("state", action_params.get("state")),
                    "confirmed_at_ms": saved_action.get("confirmed_at_ms"),
                    "cancelled_at_ms": saved_action.get("cancelled_at_ms"),
                }
                item["action_params"] = action_params
        cleaned.append({**item, **_plan_workbench_fields(item, task_states)})
    return cleaned


def build_automation_readiness(recommendations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build the future executor candidate queue without enabling execution."""

    recommendations = recommendations if recommendations is not None else build_plan_recommendations()
    items: list[dict[str, Any]] = []
    for recommendation in recommendations:
        action = recommendation.get("action_params")
        if not isinstance(action, dict):
            items.append(
                {
                    "plan": str(recommendation.get("plan") or "千川计划"),
                    "level": str(recommendation.get("level") or "info"),
                    "operation_label": str(recommendation.get("suggestion") or "人工复核"),
                    "status": "manual_only",
                    "status_label": "仅人工处理",
                    "stage": "proposal",
                    "next_step": "缺少结构化动作参数，保留为人工运营建议。",
                    "can_enter_preflight": False,
                    "execution_enabled": False,
                    "blocked_reasons": [],
                }
            )
            continue

        readiness = assess_automation_readiness(action)
        change = action.get("change") if isinstance(action.get("change"), dict) else {}
        target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
        current_value = change.get("current_value")
        target_value = change.get("target_value")
        pilot_eligible = (
            action.get("operation_type") == "adjust_budget"
            and isinstance(current_value, (int, float))
            and isinstance(target_value, (int, float))
            and float(target_value) < float(current_value)
        ) or (
            action.get("operation_type") == "pause_plan"
            and str(current_value or "") in {"投放中", "启用", "生效中", "运行中"}
            and str(target_value or "") == "暂停"
        )
        if readiness["status"] in {"confirmable", "preflight_ready"} and not pilot_eligible:
            readiness = {
                **readiness,
                "status": "blocked",
                "status_label": "试运行暂不开放",
                "stage": "qualification",
                "next_step": "首批受监督执行只开放降低预算或暂停单计划；放量和其他动作继续人工处理。",
                "can_enter_preflight": False,
                "blocked_reasons": [
                    *readiness.get("blocked_reasons", []),
                    {"code": "PILOT_SCOPE_RESTRICTED", "message": "首批只允许降低预算或暂停单计划，不开放自动放量。"},
                ],
            }
        items.append(
            {
                "action_id": str(action.get("action_id") or ""),
                "plan": str(recommendation.get("plan") or target.get("name") or "千川计划"),
                "level": str(recommendation.get("level") or "info"),
                "operation_type": str(action.get("operation_type") or ""),
                "operation_label": str(action.get("operation_label") or recommendation.get("suggestion") or "人工复核"),
                "account_label": str(target.get("account_label") or target.get("account_key") or "账号未锁定"),
                "plan_id": str(target.get("id") or ""),
                "field": change.get("field"),
                "current_value": current_value,
                "target_value": target_value,
                **readiness,
            }
        )

    order = {"preflight_ready": 0, "confirmable": 1, "blocked": 2, "manual_only": 3}
    items.sort(key=lambda item: (order.get(str(item.get("status")), 9), 0 if item.get("level") == "high" else 1))
    summary = {
        "total": len(items),
        "preflight_ready": sum(item["status"] == "preflight_ready" for item in items),
        "confirmable": sum(item["status"] == "confirmable" for item in items),
        "blocked": sum(item["status"] == "blocked" for item in items),
        "manual_only": sum(item["status"] == "manual_only" for item in items),
    }
    return {
        "generated_at": _now_label(),
        "mode": "readiness_only",
        "current_stage": "supervised_preflight",
        "next_stage": "supervised_execution",
        "execution_enabled": False,
        "criteria": [
            "锁定千川账号与计划唯一 ID",
            "页面数据不超过 10 分钟且质量分不低于 70",
            "消耗、成交和 ROI 支持高置信度判断",
            "单次预算增加不超过 15%，降低不超过 30%",
            "执行前重新读取，执行后再次回读验收",
        ],
        "summary": summary,
        "items": items,
    }


def _content_memory_path(account_key: str) -> Path:
    safe_key = account_key if SAFE_KEY.fullmatch(account_key) else "unknown"
    return DATA_DIR / "content_memory" / f"{safe_key}.json"


def _duration_bucket(value: Any) -> str:
    text = str(value or "").strip()
    parts = [int(item) for item in re.findall(r"\d+", text)]
    if not parts:
        return "时长未知"
    seconds = parts[-1] + (parts[-2] * 60 if len(parts) >= 2 else 0)
    if seconds <= 15:
        return "15秒内"
    if seconds <= 30:
        return "16-30秒"
    if seconds <= 60:
        return "31-60秒"
    return "60秒以上"


def _update_content_memory(videos: list[dict[str, Any]], settings: dict[str, Any]) -> dict[str, Any]:
    account_key = str(settings.get("qianchuan_account_key") or "unknown").lower()
    path = _content_memory_path(account_key)
    saved: dict[str, Any] = {"schema_version": 1, "account_key": account_key, "observations": []}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                saved.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass
    observations = saved.get("observations") if isinstance(saved.get("observations"), list) else []
    known = {str(item.get("fingerprint") or "") for item in observations if isinstance(item, dict)}
    for video in videos:
        evidence = video.get("evidence") if isinstance(video.get("evidence"), dict) else {}
        observed_at_ms = int(video.get("observed_at_ms") or 0)
        fingerprint = hashlib.sha256(f"{account_key}|{observed_at_ms}|{video.get('name')}".encode("utf-8")).hexdigest()[:24]
        if fingerprint in known:
            continue
        tags = [item.strip() for item in re.split(r"[,，|/#]+", str(evidence.get("tags") or "")) if item.strip()][:8]
        outcome = "winner" if video.get("funnel_stage") == "scalable" else "risk" if video.get("level") == "high" else "learning"
        observations.append({
            "fingerprint": fingerprint,
            "observed_at_ms": observed_at_ms,
            "name": str(video.get("name") or "")[:120],
            "outcome": outcome,
            "tags": tags,
            "source": str(evidence.get("source") or "来源未知")[:60],
            "duration_bucket": _duration_bucket(evidence.get("duration")),
            "roi": evidence.get("roi"),
            "ctr": evidence.get("ctr"),
            "orders": evidence.get("orders"),
            "spend": evidence.get("spend"),
        })
        known.add(fingerprint)
    observations = observations[-1000:]

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in observations:
        if not isinstance(item, dict):
            continue
        facets = [("时长", str(item.get("duration_bucket") or "时长未知")), ("来源", str(item.get("source") or "来源未知"))]
        facets.extend(("标签", str(tag)) for tag in item.get("tags", []) if tag)
        for facet in facets:
            groups.setdefault(facet, []).append(item)
    patterns: list[dict[str, Any]] = []
    for (dimension, value), samples in groups.items():
        unique_names = {str(item.get("name") or "") for item in samples}
        wins = [item for item in samples if item.get("outcome") == "winner"]
        risks = [item for item in samples if item.get("outcome") == "risk"]
        roi_values = [float(item["roi"]) for item in samples if isinstance(item.get("roi"), (int, float))]
        if not wins and not risks:
            continue
        direction = "winner" if len(wins) > len(risks) else "risk" if len(risks) > len(wins) else "mixed"
        patterns.append({
            "dimension": dimension,
            "value": value,
            "direction": direction,
            "sample_count": len(unique_names),
            "win_count": len({str(item.get("name") or "") for item in wins}),
            "risk_count": len({str(item.get("name") or "") for item in risks}),
            "average_roi": round(sum(roi_values) / len(roi_values), 2) if roi_values else None,
            "confidence": "high" if len(unique_names) >= 5 else "medium" if len(unique_names) >= 2 else "low",
        })
    patterns.sort(key=lambda item: (0 if item["confidence"] == "high" else 1 if item["confidence"] == "medium" else 2, -(item["win_count"] + item["risk_count"])))
    payload = {
        "schema_version": 1,
        "account_key": account_key,
        "updated_at": _now_label(),
        "observations": observations,
        "patterns": patterns[:30],
    }
    _atomic_json_write(path, payload)
    return {
        "observation_count": len(observations),
        "pattern_count": len(patterns),
        "verified_pattern_count": sum(item["confidence"] in {"medium", "high"} for item in patterns),
        "patterns": patterns[:8],
        "note": "同一店铺至少积累 2 条不同素材后才标记为较可信规律；单条素材只作为线索。",
    }


def build_qianchuan_creative_analysis(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Analyze Qianchuan video-library rows for live-stream acquisition work."""
    settings = settings or load_agent_settings()
    roi_target = float(settings["roi_target"])
    min_spend = float(settings["min_spend_for_action"])
    videos: list[dict[str, Any]] = []
    # Include legacy `campaigns` because v2.5.x misclassified the real video
    # library route as a campaign-management page.
    records = _table_records("qianchuan", {"video_library", "materials", "campaigns"})
    for entry in records:
        record = entry["record"]
        _, raw_name = _pick(record, ("视频", "素材名称", "创意名称", "视频名称"))
        _, assessment = _pick(record, ("素材评估", "素材状态", "评估"))
        if raw_name is None or not any(keyword in "|".join(record) for keyword in ("视频", "素材评估", "时长", "创作者声明")):
            continue
        name = _clean_entity_name(raw_name, f"第 {entry['row_index'] + 1} 条视频")
        spend = _evidence_value(record, ("消耗", "花费"))
        roi = _evidence_value(record, ("支付roi", "成交roi", "roi"))
        orders = _evidence_value(record, ("成交订单", "支付订单", "转化数"))
        impressions = _evidence_value(record, ("展示", "曝光"))
        clicks = _evidence_value(record, ("点击数", "点击量"))
        ctr = _evidence_value(record, ("点击率", "ctr"))
        _, tags = _pick(record, ("标签",))
        _, source = _pick(record, ("来源",))
        _, duration = _pick(record, ("时长",))
        assessment_text = str(assessment or "")
        if ctr is None and isinstance(clicks, (int, float)) and isinstance(impressions, (int, float)) and impressions > 0:
            ctr = round(float(clicks) / float(impressions) * 100, 2)
        cvr = round(float(orders) / float(clicks) * 100, 2) if isinstance(orders, (int, float)) and isinstance(clicks, (int, float)) and clicks > 0 else None
        cpc = round(float(spend) / float(clicks), 2) if isinstance(spend, (int, float)) and isinstance(clicks, (int, float)) and clicks > 0 else None
        cost_per_order = round(float(spend) / float(orders), 2) if isinstance(spend, (int, float)) and isinstance(orders, (int, float)) and orders > 0 else None
        if spend == 0:
            funnel_stage = "untested"
            funnel_label = "尚未进入测试"
            test_hypothesis = "先固定人群、预算和时段，只测试一个钩子变量。"
        elif ctr is not None and ctr < 1:
            funnel_stage = "hook"
            funnel_label = "钩子吸引不足"
            test_hypothesis = "保留商品与人群，仅替换前 3 秒视觉、口播或字幕钩子。"
        elif (orders or 0) == 0 and (clicks or 0) > 0:
            funnel_stage = "conversion"
            funnel_label = "点击后转化不足"
            test_hypothesis = "保留有效钩子，单独测试卖点、价格利益点和直播间承接。"
        elif roi is not None and roi >= roi_target and (orders or 0) >= 3:
            funnel_stage = "scalable"
            funnel_label = "结构可复制"
            test_hypothesis = "保留胜出钩子，分别替换卖点、场景或主播，验证可复制性。"
        else:
            funnel_stage = "learning"
            funnel_label = "数据积累中"
            test_hypothesis = "继续积累完整转化窗口，暂不同时修改多个变量。"
        evidence = {
            "spend": spend,
            "roi": roi,
            "orders": orders,
            "impressions": impressions,
            "clicks": clicks,
            "ctr": ctr,
            "cvr": cvr,
            "cpc": cpc,
            "cost_per_order": cost_per_order,
            "assessment": assessment_text[:80],
            "tags": str(tags or "")[:80],
            "source": str(source or "")[:80],
            "duration": str(duration or "")[:40],
        }
        if spend is not None and spend >= min_spend and (orders == 0 or roi == 0):
            level, status = "high", "高消耗低转化"
            suggestion = "暂停继续复制该视频，先复盘前 3 秒、直播利益点和进房后承接；修改后用小预算重新测试。"
        elif roi is not None and roi >= roi_target and (orders or 0) >= 3:
            level, status = "opportunity", "可复制放量"
            suggestion = "保留原素材继续投放，并拆出同钩子、不同卖点或不同主播口播的变体，小步扩量验证。"
        elif any(keyword in assessment_text for keyword in ("优质", "高潜", "跑量")):
            level, status = "opportunity", "高潜素材"
            suggestion = "优先进入下一轮直播引流测试，补齐消耗、进房和成交数据后再决定放量。"
        elif spend == 0:
            level, status = "warning", "尚未测试"
            suggestion = "放入小预算素材测试组，统一人群、出价和时段后比较点击、进房与成交。"
        else:
            level, status = "info", "观察中"
            suggestion = "继续观察消耗、点击、进房和成交；数据不足时不要仅凭播放量判断素材。"
        # Build structured action_params for this video
        if level == "high":
            _ap = {"operation_type": "pause_creative", "operation_label": "暂停复制该素材", "target": name, "field": "素材状态", "current_value": status, "target_value": "暂停复制", "copy_text": f"{name} | 暂停复制 | {status}"}
        elif status == "可复制放量":
            _ap = {"operation_type": "duplicate_creative", "operation_label": "创建素材变体", "target": name, "field": "素材", "current_value": status, "target_value": "变体扩量", "copy_text": f"{name} | 创建变体 | 同钩子换卖点"}
        elif status == "高潜素材":
            _ap = {"operation_type": "test_creative", "operation_label": "进入下轮测试", "target": name, "field": "素材", "current_value": status, "target_value": "直播引流测试", "copy_text": f"{name} | 进入直播引流测试"}
        elif status == "尚未测试":
            _ap = {"operation_type": "test_creative", "operation_label": "小预算测试", "target": name, "field": "预算", "current_value": "0", "target_value": "测试预算", "copy_text": f"{name} | 加入小预算测试组"}
        else:
            _ap = {"operation_type": "observe", "operation_label": "继续观察", "target": name, "field": None, "current_value": status, "target_value": None, "copy_text": f"{name} | 继续观察"}
        videos.append(
            {
                "id": f"creative-{entry['table_index']}-{entry['row_index']}",
                "observed_at_ms": int(entry.get("captured_at_ms") or 0),
                "name": name,
                "level": level,
                "status": status,
                "funnel_stage": funnel_stage,
                "funnel_label": funnel_label,
                "test_hypothesis": test_hypothesis,
                "suggestion": suggestion,
                "action_params": _ap,
                "evidence": evidence,
                "confidence": "high" if entry["quality_score"] >= 70 and spend is not None else "medium",
                "guardrail": "只生成素材建议，不上传、删除或修改千川视频。",
            }
        )

    risky = [item for item in videos if item["level"] == "high"]
    opportunities = [item for item in videos if item["level"] == "opportunity"]
    untested = [item for item in videos if item["status"] == "尚未测试"]
    spending = [item for item in videos if (item["evidence"].get("spend") or 0) > 0]
    measured = [item for item in videos if item["evidence"].get("roi") is not None or item["evidence"].get("ctr") is not None]
    funnel_counts = {
        key: sum(item.get("funnel_stage") == key for item in videos)
        for key in ("untested", "hook", "conversion", "learning", "scalable")
    }
    test_matrix: list[dict[str, Any]] = []
    for stage, label in (("hook", "钩子测试"), ("conversion", "承接测试"), ("scalable", "胜出结构变体"), ("untested", "首轮基准测试")):
        matched = [item for item in videos if item.get("funnel_stage") == stage]
        if not matched:
            continue
        test_matrix.append({
            "stage": stage,
            "label": label,
            "count": len(matched),
            "hypothesis": matched[0].get("test_hypothesis"),
            "success_metric": "点击率提升且转化不下降" if stage == "hook" else "成交率或 ROI 提升" if stage == "conversion" else "变体达到原素材核心效率" if stage == "scalable" else "取得可比较的展示、点击和成交数据",
            "guardrail": "同一轮只改变一个变量，并保持人群、预算、出价和测试时段可比。",
        })
    recommendations: list[dict[str, Any]] = []
    if not videos:
        recommendations.append({"level": "info", "owner": "投放运营", "title": "同步千川视频库", "action": "登录巨量千川，打开素材工具中的视频库后点击同步或重新巡查。", "acceptance": "视频库出现素材数量、消耗和素材评估。", "evidence": "当前没有可识别的视频库表格。"})
    else:
        if risky:
            recommendations.append({"level": "high", "owner": "投放运营", "title": f"先处理 {len(risky)} 条高消耗低转化视频", "action": "停止继续复制低效素材，逐条复盘前 3 秒钩子、核心卖点、直播利益点和进房承接。", "acceptance": "低效素材不再新增无效消耗，改版素材完成小预算复测。", "evidence": f"视频库识别到 {len(risky)} 条达到消耗门槛但无成交或 ROI 为 0 的素材。"})
        if len(videos) < 3:
            recommendations.append({"level": "warning", "owner": "直播运营", "title": "直播引流素材储备不足", "action": "至少补齐开场钩子、商品卖点、直播利益点三类视频，再用相同投放条件横向测试。", "acceptance": "三类素材均有可比较的点击、进房和成交数据。", "evidence": f"当前视频库仅识别到 {len(videos)} 条素材。"})
        if untested and len(untested) >= max(2, len(videos) // 2):
            recommendations.append({"level": "warning", "owner": "投放运营", "title": "建立素材小预算测试矩阵", "action": "把未测试素材按钩子、卖点和场景分组，统一人群、出价、时段与预算，避免不同变量混测。", "acceptance": "每条候选素材都取得首轮消耗、点击和进房数据。", "evidence": f"{len(untested)}/{len(videos)} 条素材尚未获得消耗。"})
        if opportunities:
            recommendations.append({"level": "opportunity", "owner": "直播运营", "title": f"复用 {len(opportunities)} 条高潜素材结构", "action": "保留有效钩子，分别替换卖点、主播口播或直播利益点，形成可持续素材变体。", "acceptance": "变体素材达到原素材点击或进房效率，并至少有一条形成成交。", "evidence": f"视频库识别到 {len(opportunities)} 条高潜或达到 ROI 目标的素材。"})
        if len(measured) < len(videos):
            recommendations.append({"level": "info", "owner": "投放运营", "title": "补齐视频到直播成交链路", "action": "在千川报表中补充展示、点击、进房、商品点击、成交和 ROI，避免只按消耗或素材评估做判断。", "acceptance": "主要在投视频都能关联到点击、进房和成交指标。", "evidence": f"仅 {len(measured)}/{len(videos)} 条视频包含 ROI 或点击率字段。"})

    priority = {"high": 0, "warning": 1, "opportunity": 2, "info": 3}
    videos.sort(key=lambda item: (priority.get(item["level"], 9), -(item["evidence"].get("spend") or 0)))
    memory = _update_content_memory(videos, settings) if videos else {
        "observation_count": 0,
        "pattern_count": 0,
        "verified_pattern_count": 0,
        "patterns": [],
        "note": "同步素材数据后开始为当前店铺积累内容记忆。",
    }
    return {
        "generated_at": _now_label(),
        "data_status": "ready" if videos else "missing",
        "summary": {
            "total_videos": len(videos),
            "spending_videos": len(spending),
            "untested_videos": len(untested),
            "risky_videos": len(risky),
            "high_potential_videos": len(opportunities),
            "total_spend": round(sum(item["evidence"].get("spend") or 0 for item in videos), 2),
            "hook_bottleneck_videos": funnel_counts["hook"],
            "conversion_bottleneck_videos": funnel_counts["conversion"],
            "scalable_structure_videos": funnel_counts["scalable"],
        },
        "videos": videos[:30],
        "recommendations": recommendations,
        "test_matrix": test_matrix[:4],
        "memory": memory,
        "analysis_method": "素材漏斗：展示 → 点击 → 成交 → ROI；每轮测试只改变一个内容变量。",
        "mode": "read_only",
    }


def build_inventory_alerts(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    settings = settings or load_agent_settings()
    low = int(settings["low_inventory_threshold"])
    critical = int(settings["critical_inventory_threshold"])
    days_warning = float(settings["inventory_days_warning"])
    results: list[dict[str, Any]] = []

    for entry in _table_records("doudian", {"inventory", "products"}):
        record = entry["record"]
        _, product_value = _pick(record, ("商品名称", "商品", "sku名称", "规格名称"))
        _, sku_value = _pick(record, ("sku编码", "商家编码", "规格编码", "sku"))
        stock = _evidence_value(record, ("可售库存", "现货库存", "库存数量", "库存"))
        daily_sales = _evidence_value(record, ("日均销量", "近1日销量", "昨日销量"))
        seven_day_sales = _evidence_value(record, ("近7日销量", "7日销量"))
        if daily_sales is None and seven_day_sales is not None:
            daily_sales = seven_day_sales / 7
        if stock is None:
            continue
        days_of_cover = stock / daily_sales if daily_sales and daily_sales > 0 else None
        product = str(product_value or f"第 {entry['row_index'] + 1} 行商品")[:100]
        evidence = {"stock": stock, "daily_sales": daily_sales, "days_of_cover": days_of_cover, "page_type": entry["page_type"]}
        base = {
            "id": f"{entry['page_type']}-{entry['table_index']}-{entry['row_index']}",
            "product": product,
            "sku": str(sku_value or "")[:80],
            "evidence": evidence,
        }
        if stock <= 0:
            results.append({**base, "level": "high", "title": "已缺货", "suggestion": "立即暂停该商品继续放量，并核对补货时间。",
                            "action_params": {"operation_type": "pause_ad", "operation_label": "暂停该商品投放", "target": product, "field": "投放状态", "current_value": "投放中", "target_value": "暂停", "copy_text": f"{product} | 暂停千川投放 | 当前库存 0"}})
        elif stock <= critical:
            results.append({**base, "level": "high", "title": "库存极低", "suggestion": f"库存仅 {stock:g}，优先补货；补货确认前不要扩大千川消耗。",
                            "action_params": {"operation_type": "restock", "operation_label": "紧急补货", "target": product, "field": "库存", "current_value": stock, "target_value": None, "copy_text": f"{product} | 紧急补货 | 当前库存 {stock:g}"}})
        elif days_of_cover is not None and days_of_cover <= days_warning:
            results.append({**base, "level": "warning", "title": "预计即将售罄", "suggestion": f"按当前销量约可售 {days_of_cover:.1f} 天，建议补货或降低投放强度。",
                            "action_params": {"operation_type": "review_inventory", "operation_label": f"可售仅 {days_of_cover:.1f} 天", "target": product, "field": "库存", "current_value": stock, "target_value": None, "copy_text": f"{product} | 可售 {days_of_cover:.1f} 天 | 库存 {stock:g}"}})
        elif stock <= low:
            results.append({**base, "level": "warning", "title": "低库存", "suggestion": f"库存 {stock:g}，请核对在投计划和补货周期。",
                            "action_params": {"operation_type": "review_inventory", "operation_label": "核对库存", "target": product, "field": "库存", "current_value": stock, "target_value": None, "copy_text": f"{product} | 库存 {stock:g}"}})

    priority = {"high": 0, "warning": 1, "info": 2}
    return sorted(results, key=lambda item: (priority.get(item["level"], 9), item["evidence"]["stock"]))[:30]


def _safe_snapshot_metrics(source: str, page_types: set[str]) -> tuple[dict[str, Any], list[str], dict[str, Any] | None]:
    metrics: dict[str, Any] = {}
    signals: list[str] = []
    newest: dict[str, Any] | None = None
    for item in list_snapshots():
        if item["source"] != source or item["page_type"] not in page_types:
            continue
        data = (load_data(source, item["page_type"]) or {}).get("data", {})
        for key, value in (data.get("safe_metrics") or {}).items():
            metrics[str(key)] = value
        for signal in data.get("signals") or []:
            if signal not in signals:
                signals.append(str(signal))
        if newest is None or item["age_seconds"] < newest["age_seconds"]:
            newest = item
    return metrics, signals, newest


def build_shelf_analysis() -> dict[str, Any]:
    metrics, signals, snapshot = _safe_snapshot_metrics("doudian", {"shelf"})
    exposure = _parse_number(metrics.get("曝光人数"))
    clicks = _parse_number(metrics.get("点击人数"))
    buyers = _parse_number(metrics.get("成交人数"))
    orders = _parse_number(metrics.get("订单量"))
    payment = _parse_number(metrics.get("用户支付金额"))
    click_rate = clicks / exposure * 100 if exposure and clicks is not None else None
    actions: list[dict[str, Any]] = []
    if any("不良暗示" in signal for signal in signals):
        actions.append({"level": "high", "owner": "货架运营", "title": "先修复商品主图合规", "action": "替换存在不良暗示的主图并重新检查审核状态。", "acceptance": "违规提示消失，商品恢复正常分发资格。", "evidence": "页面明确提示商品主图存在不良暗示。"})
    if exposure and clicks and not buyers:
        actions.append({"level": "warning", "owner": "货架运营", "title": "点击后没有成交，先修承接", "action": "检查详情页首屏、价格权益、评价信任和规格选择；修复前不优先加流量。", "acceptance": "成交人数大于 0，点击成交率连续两个观察周期改善。", "evidence": f"曝光 {exposure:g}、点击 {clicks:g}、成交人数 {buyers or 0:g}，推算点击率 {click_rate:.1f}%。"})
    if any("猜你喜欢未入选" in signal for signal in signals):
        actions.append({"level": "warning", "owner": "货架运营", "title": "恢复猜你喜欢入选资格", "action": "按后台诊断逐项修复商品信息、主图和基础销量门槛。", "acceptance": "未入选商品数降为 0。", "evidence": next(signal for signal in signals if "猜你喜欢未入选" in signal)})
    if not snapshot:
        actions.append({"level": "info", "owner": "货架运营", "title": "缺少货架数据", "action": "打开商城运营概览并同步。", "acceptance": "出现曝光、点击、成交漏斗。", "evidence": "尚无货架页面快照。"})
    return {"generated_at": _now_label(), "data_status": "ready" if snapshot else "missing", "snapshot": snapshot, "metrics": metrics, "funnel": {"exposure": exposure, "clicks": clicks, "buyers": buyers, "orders": orders, "payment": payment, "click_rate": click_rate}, "signals": signals, "recommendations": actions, "mode": "read_only"}


def build_live_analysis() -> dict[str, Any]:
    metrics, signals, snapshot = _safe_snapshot_metrics("doudian", {"live", "qianchuan_live"})
    q_metrics, q_signals, q_snapshot = _safe_snapshot_metrics("qianchuan", {"qianchuan_live", "live_dashboard"})
    metrics.update({key: value for key, value in q_metrics.items() if key not in metrics})
    signals.extend(signal for signal in q_signals if signal not in signals)
    live_records = _table_records("qianchuan", {"qianchuan_live", "live_dashboard"})
    record = live_records[0]["record"] if live_records else {}
    sessions = _parse_number(metrics.get("直播场次"))
    impressions = _parse_number(metrics.get("展示次数")) or _evidence_value(record, ("展示", "曝光"))
    views = _parse_number(metrics.get("进入直播间人数") or metrics.get("直播间观看人数") or metrics.get("观看次数")) or _evidence_value(record, ("进入直播间", "观看人数"))
    product_clicks = _parse_number(metrics.get("直播间商品点击人数") or metrics.get("商品点击人数")) or _evidence_value(record, ("商品点击人数", "商品点击"))
    orders = _parse_number(metrics.get("整体成交订单数") or metrics.get("直播间成交订单数") or metrics.get("成交订单数")) or _evidence_value(record, ("整体成交订单", "净成交订单", "成交订单"))
    gmv = _parse_number(metrics.get("整体成交金额(元)") or metrics.get("直播间成交金额") or metrics.get("成交金额") or metrics.get("用户支付金额")) or _evidence_value(record, ("整体成交金额", "净成交金额", "成交金额"))
    spend = _parse_number(metrics.get("整体消耗(元)") or metrics.get("视频消耗") or metrics.get("投放消耗（店铺被投）")) or _evidence_value(record, ("整体消耗", "消耗", "花费"))
    roi = _parse_number(metrics.get("整体支付ROI") or metrics.get("净成交ROI")) or _evidence_value(record, ("整体支付roi", "净成交roi", "roi"))
    refund_rate = _parse_number(metrics.get("1小时内退款率")) or _evidence_value(record, ("退款率",))
    enter_rate = views / impressions * 100 if impressions and views is not None else None
    product_click_rate = product_clicks / views * 100 if views and product_clicks is not None else None
    conversion_rate = orders / product_clicks * 100 if product_clicks and orders is not None else None
    actions: list[dict[str, Any]] = []
    if sessions == 0 or any("当前待直播计划 0" in signal for signal in signals):
        actions.append({"level": "warning", "owner": "直播运营", "title": "先排一场基准直播", "action": "建立开播计划，确定主播、货盘、脚本和至少一个主推品；先跑出完整漏斗再谈 ROI 优化。", "acceptance": "直播场次大于 0，并取得观看、商品点击和成交三段数据。", "evidence": f"直播场次 {sessions or 0:g}，当前未形成可分析的直播样本。"})
    elif spend and impressions and not views:
        actions.append({"level": "high", "owner": "投放运营", "title": "视频有曝光但没有进房", "action": "优先更换前 3 秒钩子、封面文案和直播利益点，不要先提高出价。", "acceptance": "进房人数大于 0，进房率连续两个测试周期改善。", "evidence": f"展示 {impressions:g}，进房 {views or 0:g}，已消耗 {spend:g}。"})
    elif views and not product_clicks:
        actions.append({"level": "warning", "owner": "直播运营", "title": "有人看但不点商品", "action": "优化开场钩子、商品讲解顺序和购物车引导。", "acceptance": "商品点击率连续两个场次提升。", "evidence": f"观看 {views:g}，商品点击 {product_clicks or 0:g}。"})
    elif product_clicks and not orders:
        actions.append({"level": "warning", "owner": "直播运营", "title": "商品有点击但未成交", "action": "检查价格机制、库存规格、信任证明和逼单节奏。", "acceptance": "成交订单数大于 0。", "evidence": f"商品点击 {product_clicks:g}，成交订单 {orders or 0:g}。"})
    if spend and not orders:
        actions.insert(0, {"level": "high", "owner": "投放运营", "title": "直播投放先止损", "action": "降低或暂停新增消耗，核查直播间承接后再恢复。", "acceptance": "恢复投放前取得自然流量成交或明确修复项。", "evidence": f"直播投放消耗 {spend:g}，成交订单 {orders or 0:g}。"})
    if not snapshot and not q_snapshot:
        actions.append({"level": "info", "owner": "直播运营", "title": "缺少直播大屏数据", "action": "打开店铺直播或千川直播大屏并同步。", "acceptance": "出现直播场次与观看转化指标。", "evidence": "尚无直播快照。"})
    if refund_rate is not None and refund_rate >= 20:
        actions.append({"level": "warning", "owner": "直播运营", "title": "成交后退款偏高", "action": "核对主播承诺、商品预期、尺码说明和售后原因，避免素材与直播间过度承诺。", "acceptance": "退款率回落并且净成交 ROI 改善。", "evidence": f"当前退款率 {refund_rate:g}%。"})
    return {"generated_at": _now_label(), "data_status": "ready" if snapshot or q_snapshot else "missing", "snapshot": snapshot or q_snapshot, "metrics": metrics, "funnel": {"sessions": sessions, "impressions": impressions, "views": views, "enter_rate": enter_rate, "product_clicks": product_clicks, "product_click_rate": product_click_rate, "orders": orders, "conversion_rate": conversion_rate, "gmv": gmv, "spend": spend, "roi": roi, "refund_rate": refund_rate}, "signals": signals, "recommendations": actions, "mode": "read_only"}


def build_ops_manager() -> dict[str, Any]:
    shelf, live = build_shelf_analysis(), build_live_analysis()
    plans, inventory = build_plan_recommendations(), build_inventory_alerts()
    creative = build_qianchuan_creative_analysis()
    tasks = [*shelf["recommendations"], *live["recommendations"], *creative["recommendations"]]
    for item in plans[:5]:
        task_entry = {
            "level": item["level"],
            "owner": "投放运营",
            "title": item["workbench_title"],
            "action": item["action"],
            "acceptance": item["acceptance"],
            "evidence": item["found"],
            "impact": item["adjustment_range"],
            "observation_window": item["observation_window"],
        }
        if "action_params" in item:
            task_entry["action_params"] = item["action_params"]
        tasks.append(task_entry)
    for item in inventory[:3]:
        inv_entry = {"level": item["level"], "owner": "商品运营", "title": f"{item['product']} · {item['title']}", "action": item["suggestion"], "acceptance": "补货或投放限制已人工确认。", "evidence": f"当前库存 {item['evidence']['stock']:g}。"}
        if "action_params" in item:
            inv_entry["action_params"] = item["action_params"]
        tasks.append(inv_entry)
    priority = {"high": 0, "warning": 1, "opportunity": 2, "info": 3}
    tasks.sort(key=lambda item: (priority.get(item["level"], 9), 0 if "合规" in item["title"] or "主图" in item["title"] else 1))
    states = load_task_states()
    for item in tasks:
        raw_key = f"{item['owner']}|{item['title']}"
        item["id"] = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
        task_state = states.get(item["id"], {})
        item["status"] = task_state.get("status", "todo")
        item["updated_at"] = task_state.get("updated_at")
        item["assignee"] = task_state.get("assignee") or item["owner"]
        item["last_operator"] = task_state.get("operator")
        item["blocked_reason"] = task_state.get("blocked_reason")
        item["history"] = task_state.get("history", [])[-10:]
        item["confidence"] = "high" if item["level"] in {"high", "opportunity"} else "medium"
        item["impact"] = "风险优先" if item["level"] == "high" else "增长机会" if item["level"] == "opportunity" else "影响转化"
    unique_tasks: dict[str, dict[str, Any]] = {}
    for item in tasks:
        unique_tasks.setdefault(item["id"], item)
    tasks = list(unique_tasks.values())
    active = [item for item in tasks if item["status"] != "done"]
    must_do = [item for item in active if item["level"] != "opportunity"][:3]
    opportunities = [item for item in active if item["level"] == "opportunity"][:3]
    progress = {status: sum(1 for item in tasks if item["status"] == status) for status in ("todo", "doing", "observing", "blocked", "done")}
    receipt = build_scan_receipt()
    unsynced_data = [
        {
            "page_id": item.get("id"),
            "label": item.get("label") or item.get("page_type") or item.get("id"),
            "reason": item.get("error") or ("数据质量需要复核" if item.get("needs_review") else "读取失败"),
        }
        for item in receipt.get("results", [])
        if not item.get("ok") or item.get("needs_review")
    ][:8]
    if shelf.get("data_status") != "ready":
        unsynced_data.append({"page_id": "doudian_shelf", "label": "抖店商城经营", "reason": "尚无货架快照"})
    if live.get("data_status") != "ready":
        unsynced_data.append({"page_id": "live_dashboard", "label": "直播大屏", "reason": "尚无直播快照"})
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    yesterday_results = []
    for result in build_execution_effectiveness_report().get("items", []):
        executed_at = _timestamp_seconds(result.get("executed_at_ms"))
        if executed_at and time.strftime("%Y-%m-%d", time.localtime(executed_at)) == yesterday:
            yesterday_results.append(result)
    max_risk = next((item for item in active if item.get("level") == "high"), active[0] if active else None)
    return {
        "generated_at": _now_label(),
        "headline": "先处理风险与转化瓶颈，再安排放量",
        "must_do": must_do,
        "growth_opportunities": opportunities,
        "today_top_actions": active[:10],
        "all_tasks": tasks,
        "progress": {**progress, "total": len(tasks), "completed_rate": round(progress["done"] / len(tasks) * 100) if tasks else 0},
        "today_focus": {
            "top_three": active[:3],
            "max_risk": max_risk,
            "yesterday_result": yesterday_results[0] if yesterday_results else None,
            "unsynced_data": unsynced_data[:8],
        },
        "roles": ["货架商品", "直播投放", "内容"],
        "modules": {"shelf": {"status": shelf["data_status"], "action_count": len(shelf["recommendations"])}, "live": {"status": live["data_status"], "action_count": len(live["recommendations"])}, "qianchuan": {"action_count": len(plans)}, "creative": {"status": creative["data_status"], "action_count": len(creative["recommendations"])}, "inventory": {"alert_count": len(inventory)}},
        "mode": "read_only",
    }


def _task_states_path() -> Path:
    return DATA_DIR / "task_states.json"


def _task_scope_key(store_key: str | None = None, business_date: str | None = None) -> str:
    settings = load_agent_settings()
    selected_store = str(store_key or settings.get("store_key") or "unscoped").lower()
    safe_store = re.sub(r"[^a-z0-9_-]", "_", selected_store)[:80] or "unscoped"
    day = str(business_date or time.strftime("%Y-%m-%d"))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("invalid business_date")
    return f"{safe_store}:{day}"


def _load_task_state_document() -> dict[str, Any]:
    path = _task_states_path()
    if not path.exists():
        return {"schema_version": 2, "scopes": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return {"schema_version": 2, "scopes": {}}
        if value.get("schema_version") == 2 and isinstance(value.get("scopes"), dict):
            return value
        # v1 stored one global task map. Keep it readable in the current scope
        # and migrate on the next mutation instead of discarding user history.
        legacy = {
            key: item for key, item in value.items()
            if re.fullmatch(r"[a-f0-9]{16}", str(key)) and isinstance(item, dict)
        }
        return {"schema_version": 2, "scopes": {_task_scope_key(): legacy}}
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 2, "scopes": {}}


def load_task_states(store_key: str | None = None, business_date: str | None = None) -> dict[str, Any]:
    document = _load_task_state_document()
    scope = _task_scope_key(store_key, business_date)
    values = document.get("scopes", {}).get(scope, {})
    return values if isinstance(values, dict) else {}


def update_task_state(
    task_id: str,
    status: str,
    *,
    operator: str = "",
    assignee: str = "",
    note: str = "",
    title: str = "",
    owner: str = "",
    store_key: str | None = None,
    business_date: str | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{16}", str(task_id or "")):
        raise ValueError("invalid task_id")
    if status not in {"todo", "doing", "observing", "blocked", "done"}:
        raise ValueError("invalid task status")
    operator = str(operator or "本机运营")[:80]
    assignee = str(assignee or "")[:80]
    note = str(note or "")[:300]
    title = str(title or "")[:160]
    owner = str(owner or "")[:80]
    if status == "blocked" and not note:
        raise ValueError("阻止任务时必须填写原因")
    effective_store = str(store_key or load_agent_settings().get("store_key") or "").lower()
    known_stores = {str(item.get("key") or "") for item in list_store_identities()}
    if not effective_store or effective_store not in known_stores:
        raise ValueError("尚未识别当前店铺，任务状态不会写入未归属账本。")
    store_key = effective_store
    with _state_lock:
        document = _load_task_state_document()
        scope = _task_scope_key(store_key, business_date)
        scopes = document.setdefault("scopes", {})
        states = scopes.setdefault(scope, {})
        previous = states.get(task_id, {}) if isinstance(states.get(task_id), dict) else {}
        previous_status = previous.get("status", "todo")
        now = _now_label()
        event_type = "transferred" if assignee and assignee != previous.get("assignee") and status == previous_status else "status_changed"
        event = {
            "event": event_type,
            "from": previous_status,
            "to": status,
            "operator": operator,
            "assignee": assignee or previous.get("assignee") or owner,
            "note": note,
            "at": now,
        }
        next_state = {
            **previous,
            "status": status,
            "updated_at": now,
            "operator": operator,
            "assignee": assignee or previous.get("assignee") or owner,
            "title": title or previous.get("title") or "",
            "owner": owner or previous.get("owner") or "",
            "history": [*(previous.get("history") or []), event][-50:],
        }
        if status == "doing":
            next_state["started_at"] = previous.get("started_at") or now
            next_state["blocked_reason"] = None
        elif status == "observing":
            next_state["review_started_at"] = now
            next_state["blocked_reason"] = None
        elif status == "blocked":
            next_state["blocked_at"] = now
            next_state["blocked_reason"] = note
        elif status == "done":
            next_state["completed_at"] = now
            next_state["blocked_reason"] = None
        states[task_id] = next_state
        _atomic_json_write(_task_states_path(), document)
    # When task transitions to done, evaluate suggestion effectiveness
    if status == "done" and previous_status != "done":
        _evaluate_on_completion(task_id)
    return {"task_id": task_id, "scope": scope, **states[task_id]}


def _onboarding_state_path() -> Path:
    return DATA_DIR / "onboarding_state.json"


def _load_onboarding_state() -> dict[str, Any]:
    path = _onboarding_state_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _onboarding_scope(state: dict[str, Any], store_key: str) -> dict[str, Any]:
    """Return one store's onboarding state without trusting legacy global progress."""
    scopes = state.get("scopes") if isinstance(state.get("scopes"), dict) else {}
    scope = scopes.get(store_key) if store_key and isinstance(scopes.get(store_key), dict) else {}
    return dict(scope)


def _label_timestamp(value: Any) -> int:
    try:
        return int(time.mktime(time.strptime(str(value or ""), "%Y-%m-%d %H:%M:%S")))
    except (TypeError, ValueError):
        return 0


def _confirm_onboarding_store(store_key: str) -> None:
    """Persist an explicit, store-scoped confirmation without erasing prior data."""
    store_key = str(store_key or "").lower()
    if not SAFE_KEY.fullmatch(store_key):
        return
    with _state_lock:
        state = _load_onboarding_state()
        scopes = state.get("scopes") if isinstance(state.get("scopes"), dict) else {}
        scope = scopes.get(store_key) if isinstance(scopes.get(store_key), dict) else {}
        now = _now_label()
        scope["started_at"] = scope.get("started_at") or now
        scope["store_confirmed_at"] = scope.get("store_confirmed_at") or now
        scope["updated_at"] = now
        scopes[store_key] = scope
        state.update({"schema_version": 2, "scopes": scopes, "updated_at": now})
        _atomic_json_write(_onboarding_state_path(), state)


def update_onboarding_state(event: str) -> dict[str, Any]:
    if event not in {"start", "first_task_viewed", "reset"}:
        raise ValueError("invalid onboarding event")
    with _state_lock:
        selected_key = str(build_store_catalog().get("selected_store_key") or "")
        if event == "reset":
            state = _load_onboarding_state()
            scopes = state.get("scopes") if isinstance(state.get("scopes"), dict) else {}
            scopes[selected_key or "unscoped"] = {"started_at": _now_label(), "last_event": "reset"}
            state.update({"schema_version": 2, "scopes": scopes})
        else:
            state = _load_onboarding_state()
            scopes = state.get("scopes") if isinstance(state.get("scopes"), dict) else {}
            scope_key = selected_key or "unscoped"
            scope = scopes.get(scope_key) if isinstance(scopes.get(scope_key), dict) else {}
            scope["started_at"] = scope.get("started_at") or _now_label()
            scope["last_event"] = event
            if event == "first_task_viewed":
                scope["first_task_viewed_at"] = _now_label()
            scope["updated_at"] = _now_label()
            scopes[scope_key] = scope
            state.update({"schema_version": 2, "scopes": scopes})
        state["updated_at"] = _now_label()
        _atomic_json_write(_onboarding_state_path(), state)
    return build_onboarding_status(state=state)


def build_onboarding_status(*, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a resumable first-value journey from real local evidence."""

    state = state if isinstance(state, dict) else _load_onboarding_state()
    catalog = build_store_catalog()
    selected_key = str(catalog.get("selected_store_key") or "")
    selected_store = next((item for item in catalog.get("stores", []) if item.get("key") == selected_key), {})
    scoped_state = _onboarding_scope(state, selected_key)
    confirmed_at = _label_timestamp(scoped_state.get("store_confirmed_at"))
    snapshots = list_snapshots()
    ops = build_ops_manager()
    active_tasks = [item for item in ops.get("all_tasks", []) if item.get("status") != "done"]
    first_task = active_tasks[0] if active_tasks else (ops.get("all_tasks") or [None])[0]
    fresh_snapshots = [item for item in snapshots if item.get("fresh")]
    formal_snapshots = [
        item for item in fresh_snapshots
        if confirmed_at and int(item.get("captured_at") or 0) >= confirmed_at
    ]
    usable_snapshots = [item for item in formal_snapshots if int(item.get("quality_score") or 0) >= 60]
    formal_doudian_types = {
        str(item.get("page_type") or "") for item in usable_snapshots if item.get("source") == "doudian"
    }
    required_doudian_types = {"overview", "orders", "products", "shelf"}
    store_confirmed = bool(selected_key and confirmed_at)
    sync_complete = bool(store_confirmed and required_doudian_types.issubset(formal_doudian_types))
    first_task_ready = bool(sync_complete and first_task)
    first_task_viewed = bool(scoped_state.get("first_task_viewed_at")) or bool(
        first_task and first_task.get("status") in {"doing", "observing", "blocked", "done"}
    ) if first_task_ready else False
    steps = [
        {"id": "environment", "label": "环境检查", "complete": True, "action": "none", "instruction": "本地 Agent 已连接。"},
        {"id": "store", "label": "确认抖店店铺", "complete": store_confirmed, "action": "select_store", "instruction": "先识别并确认当前抖店，后续数据、任务和日志才会按店隔离。"},
        {"id": "sync", "label": "完成快速巡店", "complete": sync_complete, "action": "quick_scan", "instruction": "确认店铺后同步经营概览、订单、商品和商城四类关键页面，约 3 分钟。"},
        {"id": "first_task", "label": "生成第一条今日任务", "complete": first_task_ready, "action": "view_first_task", "instruction": "系统会用最新证据生成一个明确下一步。"},
        {"id": "evidence", "label": "查看证据与下一步", "complete": first_task_viewed, "action": "view_first_task", "instruction": "核对数据依据、负责人和完成验收标准。"},
    ]
    current_index = next((index for index, item in enumerate(steps) if not item["complete"]), len(steps) - 1)
    current = steps[current_index]
    completed_count = sum(1 for item in steps if item["complete"])
    missing_data: list[dict[str, str]] = []
    sources = {item.get("source") for item in formal_snapshots}
    for page_type, label, path in [
        ("overview", "经营概览", "抖店后台 → 首页/经营概览 → 点击重新识别"),
        ("orders", "订单", "抖店后台 → 订单 → 点击单页重试"),
        ("products", "商品", "抖店后台 → 商品 → 点击单页重试"),
        ("shelf", "商城经营", "抖店后台 → 商城 → 商城经营 → 点击单页重试"),
    ]:
        if page_type not in formal_doudian_types:
            missing_data.append({"id": f"doudian_{page_type}", "label": label, "path": path})
    optional_enhancements: list[dict[str, str]] = []
    if "qianchuan" not in sources:
        optional_enhancements.append({"id": "qianchuan_overview", "label": "千川投放增强", "path": "巨量千川 → 首页 → 同步当前账户后人工确认关联"})
    return {
        "schema_version": 2,
        "status": "completed" if all(item["complete"] for item in steps) else "in_progress",
        "status_label": "首次经营闭环已建立" if all(item["complete"] for item in steps) else f"继续第 {current_index + 1} 步：{current['label']}",
        "progress": {"completed": completed_count, "total": len(steps), "percent": round(completed_count / len(steps) * 100)},
        "current_step": current,
        "steps": steps,
        "store_key": selected_key,
        "store_confirmed": store_confirmed,
        "store_confirmed_at": scoped_state.get("store_confirmed_at"),
        "selected_store": selected_store,
        "first_task": first_task,
        "missing_data": missing_data[:4],
        "optional_enhancements": optional_enhancements,
        "started_at": scoped_state.get("started_at"),
        "updated_at": scoped_state.get("updated_at") or state.get("updated_at"),
        "discovered": {
            "store_count": int(catalog.get("store_count") or 0),
            "snapshot_count": len(fresh_snapshots),
            "formal_snapshot_count": len(formal_snapshots),
            "usable_snapshot_count": len(usable_snapshots),
            "out_of_order": bool(fresh_snapshots and not sync_complete),
            "note": "已发现历史页面，但需先确认店铺并重新快速巡店，才计入正式进度。" if fresh_snapshots and not sync_complete else "",
        },
        "resume_supported": True,
        "note": "纯抖店数据可以完成首次经营闭环；千川仅增强投放分析，未关联时不会阻塞抖店任务。",
    }


def build_connection_guide() -> dict[str, Any]:
    """Converge identity, first value and optional ads into one next-best action."""
    catalog = build_store_catalog()
    onboarding = build_onboarding_status()
    context = build_operation_context(catalog=catalog)
    readiness = build_automation_readiness()
    selected_key = str(catalog.get("selected_store_key") or "")
    selected = next((item for item in catalog.get("stores", []) if item.get("key") == selected_key), {})
    l1 = bool(onboarding.get("store_confirmed"))
    l2 = bool(l1 and next((item.get("complete") for item in onboarding.get("steps", []) if item.get("id") == "sync"), False))
    l3 = bool(l2 and catalog.get("selected_account_key") and int(selected.get("qianchuan_page_count") or 0) > 0)
    readiness_summary = readiness.get("summary") if isinstance(readiness.get("summary"), dict) else {}
    l4 = bool(l3 and context.get("execution_review_allowed") and int(readiness_summary.get("preflight_ready") or 0) > 0)
    reached_level = 4 if l4 else 3 if l3 else 2 if l2 else 1 if l1 else 0
    levels = [
        {"id": "L1", "label": "店铺已识别", "reached": l1, "next_action": "确认当前抖店"},
        {"id": "L2", "label": "经营数据可用", "reached": l2, "next_action": "完成 3 分钟快速巡店"},
        {"id": "L3", "label": "投放已连接", "reached": l3, "next_action": "同步并关联当前千川页"},
        {"id": "L4", "label": "受控执行可用", "reached": l4, "next_action": "选择方案并完成安全检查"},
    ]
    store_count = int(catalog.get("store_count") or 0)
    first_value_complete = onboarding.get("status") == "completed"
    current_step = onboarding.get("current_step") if isinstance(onboarding.get("current_step"), dict) else {}
    if not store_count:
        action = {
            "id": "identify_store", "label": "打开抖店经营概览并识别", "eta": "约 1 分钟",
            "value": "识别成功后，数据、任务和日志会按店铺隔离。",
            "failure": "尚未发现抖店身份，因为经营概览页还未完成读取。现在请打开目标页面并点击重新识别。",
        }
    elif not l1:
        action = {
            "id": "confirm_store", "label": "确认这是我的店铺", "eta": "不到 1 分钟",
            "value": "确认后才会开始计算这家店的正式经营进度。",
            "failure": "已经发现店铺，但还没有得到你的确认，所以历史快照不会计入进度。现在请选择店铺并确认。",
        }
    elif not l2:
        action = {
            "id": "quick_scan", "label": "开始 3 分钟快速巡店", "eta": "约 3–5 分钟",
            "value": "同步经营概览、订单、商品和商城后，生成第一条今日任务。",
            "failure": "关键页面还没有在确认店铺后完整同步，因此旧快照只算已发现。现在点击快速巡店，失败页可单独重试。",
        }
    elif current_step.get("id") in {"first_task", "evidence"} and not first_value_complete:
        action = {
            "id": "view_first_task", "label": "查看第一条经营任务", "eta": "约 2 分钟",
            "value": "看到问题、证据、动作和验收标准，首次闭环才算完成。",
            "failure": "经营数据已经可用，但第一条任务还未确认查看。现在打开任务卡并核对证据。",
        }
    elif not l3:
        action = {
            "id": "sync_qianchuan", "label": "同步当前千川页", "eta": "约 1–3 分钟",
            "value": "可选：连接后才会出现投放建议和受控执行；不影响抖店巡店。",
            "optional": True,
            "failure": "尚未连接千川，因此投放自动化未开启；纯抖店诊断仍可正常使用。现在可同步当前千川页，或暂不使用投放功能。",
        }
    elif not l4:
        action = {
            "id": "view_ad_candidates", "label": "选择一个投放方案", "eta": "约 3 分钟",
            "value": "选择方案后再确认授权，内部安全检查会按需展开。",
            "failure": "千川已连接，但还没有方案同时满足计划 ID、时效、质量、额度和人工授权。现在选择候选并查看缺少项。",
        }
    else:
        action = {
            "id": "view_controlled_execution", "label": "进入受控执行", "eta": "约 3–5 分钟",
            "value": "按选择方案、确认授权、查看结果三步完成单次受监督调整。",
        }
    tutorial = [
        {"id": "connect_doudian", "label": "连接抖店", "complete": l1, "detail": "只读取已登录经营页面，不读取密码。"},
        {"id": "quick_scan", "label": "快速巡店", "complete": l2, "detail": "约 3–5 分钟，确认店铺后的数据才计入进度。"},
        {"id": "first_task", "label": "查看第一条任务", "complete": first_value_complete, "detail": "核对证据、建议动作和验收标准。"},
        {"id": "optional_qianchuan", "label": "按需连接千川", "complete": l3, "optional": True, "detail": "不投放可以跳过；连接后才显示计划建议。"},
    ]
    return {
        "schema_version": 1,
        "status": "collapsed" if first_value_complete else "expanded",
        "collapsed": first_value_complete,
        "store": {
            "key": selected_key,
            "label": str(selected.get("label") or "尚未识别店铺"),
            "updated_at": selected.get("updated_at"),
        },
        "level": f"L{reached_level}" if reached_level else "L0",
        "level_label": levels[reached_level - 1]["label"] if reached_level else "尚未连接店铺",
        "levels": levels,
        "next_upgrade": action,
        "tutorial": tutorial,
        "onboarding": onboarding,
        "operation_context": context,
        "automation": {
            "mode": "three_step" if l3 else "off",
            "qianchuan_connected": l3,
            "candidate_count": len(readiness.get("items") or []),
            "steps": ["选择方案", "确认授权", "查看结果"] if l3 else [],
            "safety_details_available": True,
        },
        "note": "千川是可选增强；没有千川时不阻塞抖店首次诊断。",
    }


# ---------------------------------------------------------------------------
# User feedback on suggestions (thumbs up / down)
# ---------------------------------------------------------------------------

def _feedback_path() -> Path:
    return DATA_DIR / "feedback.json"


def load_feedback() -> list[dict[str, Any]]:
    path = _feedback_path()
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_feedback(task_id: str, rating: str, comment: str = "", context: str = "") -> dict[str, Any]:
    if rating not in {"up", "down", "defer"}:
        raise ValueError("rating must be 'up', 'down' or 'defer'")
    with _state_lock:
        feedback = load_feedback()
        entry = {
            "task_id": str(task_id or "")[:64],
            "rating": rating,
            "comment": str(comment or "")[:500],
            "context": str(context or "")[:200],
            "created_at": _now_label(),
        }
        feedback.append(entry)
        _atomic_json_write(_feedback_path(), feedback[-2000:])
    return entry


def get_feedback_stats() -> dict[str, Any]:
    feedback = load_feedback()
    up = sum(1 for item in feedback if item.get("rating") == "up")
    down = sum(1 for item in feedback if item.get("rating") == "down")
    deferred = sum(1 for item in feedback if item.get("rating") == "defer")
    total = up + down + deferred
    rated = up + down
    return {
        "total": total,
        "helpful": up,
        "not_helpful": down,
        "deferred": deferred,
        "helpful_rate": round(up / rated * 100, 1) if rated else 0,
        "recent": feedback[-10:],
    }


# ---------------------------------------------------------------------------
# Selector / collection health monitoring
# ---------------------------------------------------------------------------

def _health_baselines_path() -> Path:
    return DATA_DIR / "health_baselines.json"


def load_health_baselines() -> dict[str, Any]:
    path = _health_baselines_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def update_health_baseline(source: str, page_type: str, quality: dict[str, Any]) -> None:
    key = f"{source}/{page_type}"
    with _state_lock:
        baselines = load_health_baselines()
        current = baselines.get(key, {"samples": []})
        samples = current.get("samples", [])
        samples.append({
            "quality_score": int(quality.get("score", 0) or 0),
            "row_count": int(quality.get("row_count", 0) or 0),
            "metric_count": int(quality.get("metric_count", 0) or 0),
            "captured_at": int(time.time() * 1000),
        })
        # Keep last 30 samples per page
        current["samples"] = samples[-30:]
        if len(samples) >= 5:
            avg_score = sum(s["quality_score"] for s in samples[-10:]) / min(len(samples), 10)
            avg_rows = sum(s["row_count"] for s in samples[-10:]) / min(len(samples), 10)
            avg_metrics = sum(s["metric_count"] for s in samples[-10:]) / min(len(samples), 10)
            current["baseline"] = {
                "avg_quality_score": round(avg_score, 1),
                "avg_row_count": round(avg_rows, 1),
                "avg_metric_count": round(avg_metrics, 1),
                "sample_count": len(samples),
            }
        baselines[key] = current
        _atomic_json_write(_health_baselines_path(), baselines)


def check_selector_health() -> dict[str, Any]:
    baselines = load_health_baselines()
    alerts: list[dict[str, Any]] = []
    snapshots = list_snapshots()
    for item in snapshots:
        key = f"{item['source']}/{item['page_type']}"
        baseline = baselines.get(key, {}).get("baseline")
        if not baseline or baseline.get("sample_count", 0) < 3:
            continue
        current_score = item.get("quality_score", 0)
        avg_score = baseline.get("avg_quality_score", 0)
        current_rows = item.get("quality_score", 0)  # Use quality score as proxy
        avg_rows = baseline.get("avg_row_count", 0)
        # Alert if quality dropped significantly
        if avg_score > 30 and current_score < avg_score * 0.5:
            alerts.append({
                "level": "high",
                "page": f"{item['source']}/{item['page_type']}",
                "title": f"{item['page_type']} 采集质量异常下降",
                "detail": f"当前质量分 {current_score}，历史均值 {avg_score:.0f}。页面结构可能已改版。",
                "action": "请打开该页面检查字段是否正确识别，如需更新选择器请提交 Issue。",
            })
        elif avg_rows > 5 and item.get("row_count", 0) < avg_rows * 0.3:
            alerts.append({
                "level": "warning",
                "page": f"{item['source']}/{item['page_type']}",
                "title": f"{item['page_type']} 采集行数偏低",
                "detail": f"当前 {item.get('row_count', 0)} 行，历史均值 {avg_rows:.0f} 行。可能是虚拟滚动未触发或列表缩短。",
                "action": "刷新页面后重试；如仍偏低，平台可能调整了分页或列表结构。",
            })
    # Overall health score
    total_pages = len(baselines)
    healthy = sum(1 for key, value in baselines.items() if value.get("baseline", {}).get("sample_count", 0) >= 3)
    return {
        "generated_at": _now_label(),
        "total_tracked_pages": total_pages,
        "pages_with_baseline": healthy,
        "alerts": alerts,
        "baselines": {key: value.get("baseline", {}) for key, value in baselines.items()},
        "mode": "read_only",
    }


# ---------------------------------------------------------------------------
# Task export to clipboard-friendly formats
# ---------------------------------------------------------------------------

def export_tasks(fmt: str = "clipboard") -> dict[str, Any]:
    ops = build_ops_manager()
    tasks = ops.get("all_tasks", [])
    today_label = time.strftime("%Y-%m-%d")
    todo = [item for item in tasks if item.get("status") == "todo"]
    doing = [item for item in tasks if item.get("status") == "doing"]
    observing = [item for item in tasks if item.get("status") == "observing"]
    blocked = [item for item in tasks if item.get("status") == "blocked"]
    done = [item for item in tasks if item.get("status") == "done"]
    total = len(tasks)
    completed = len(done)

    if fmt == "markdown":
        lines = [f"# 店策 Agent 任务清单 - {today_label}", "", f"共 {total} 项，已完成 {completed} 项", ""]
        if todo:
            lines.append("## 待处理")
            lines.extend(f"- [{item['owner']}] {item['title']}：{item['action']}" for item in todo)
        if doing:
            lines.append("\n## 进行中")
            lines.extend(f"- [{item['owner']}] {item['title']}：{item['action']}" for item in doing)
        if observing:
            lines.append("\n## 待观察")
            lines.extend(f"- [{item['owner']}] {item['title']}" for item in observing)
        if blocked:
            lines.append("\n## 已阻止")
            lines.extend(f"- [{item['assignee']}] {item['title']}：{item.get('blocked_reason') or '等待解除阻止'}" for item in blocked)
        if done:
            lines.append("\n## 已完成")
            lines.extend(f"- ~~[{item['owner']}] {item['title']}~~" for item in done)
        return {"format": "markdown", "content": "\n".join(lines), "task_count": total}

    # Default: plain text for clipboard (works in Feishu, WeChat Work, DingTalk)
    lines = [f"店策 Agent 任务清单 {today_label}", f"共 {total} 项 | 已完成 {completed} 项", ""]
    lines.append("【待处理】")
    for item in todo:
        lines.append(f"  [{item['owner']}] {item['title']}")
        lines.append(f"    → {item['action']}")
    lines.append("")
    lines.append("【进行中】")
    for item in doing:
        lines.append(f"  [{item['owner']}] {item['title']}")
    lines.append("")
    lines.append("【待观察】")
    for item in observing:
        lines.append(f"  [{item['owner']}] {item['title']}")
    lines.append("")
    lines.append("【已阻止】")
    for item in blocked:
        lines.append(f"  [{item['assignee']}] {item['title']}：{item.get('blocked_reason') or '等待解除阻止'}")
    lines.append("")
    lines.append("【已完成】")
    for item in done:
        lines.append(f"  ✓ [{item['owner']}] {item['title']}")
    return {"format": "clipboard", "content": "\n".join(lines), "task_count": total}


# ---------------------------------------------------------------------------
# Suggestion effectiveness tracking (close the loop)
# ---------------------------------------------------------------------------

def _suggestion_snapshots_path() -> Path:
    return DATA_DIR / "suggestion_snapshots.json"


def load_suggestion_snapshots() -> dict[str, Any]:
    path = _suggestion_snapshots_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_suggestion_snapshot(task_id: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    selected_store = str(load_agent_settings().get("store_key") or "")
    if not selected_store or selected_store not in {str(item.get("key") or "") for item in list_store_identities()}:
        raise ValueError("尚未识别当前店铺，暂不写入效果基线。")
    with _state_lock:
        snapshots = load_suggestion_snapshots()
        scope = _task_scope_key()
        # Capture current metrics as baseline
        metrics: dict[str, Any] = {}
        for item in list_snapshots():
            snapshot = load_data(item["source"], item["page_type"])
            data = (snapshot or {}).get("data", {})
            for key, value in (data.get("safe_metrics") or {}).items():
                metrics[f"{item['source']}/{item['page_type']}/{key}"] = value
        entry = {
            "task_id": str(task_id or "")[:64],
            "scope": scope,
            "created_at": _now_label(),
            "context": context or {},
            "metrics_snapshot": metrics,
            "evaluated": False,
            "evaluation": None,
        }
        snapshots[f"{scope}|{task_id}"] = entry
        _atomic_json_write(_suggestion_snapshots_path(), snapshots)
    return entry


def _evaluate_on_completion(task_id: str) -> dict[str, Any] | None:
    snapshots = load_suggestion_snapshots()
    scope = _task_scope_key()
    storage_key = f"{scope}|{task_id}"
    entry = snapshots.get(storage_key) or snapshots.get(str(task_id))
    if not entry or entry.get("evaluated"):
        return None
    old_metrics = entry.get("metrics_snapshot", {})
    # Get current metrics
    current_metrics: dict[str, Any] = {}
    for item in list_snapshots():
        snapshot = load_data(item["source"], item["page_type"])
        data = (snapshot or {}).get("data", {})
        for key, value in (data.get("safe_metrics") or {}).items():
            current_metrics[f"{item['source']}/{item['page_type']}/{key}"] = value
    # Compare key metrics
    changes: list[dict[str, Any]] = []
    for key, old_value in old_metrics.items():
        new_value = current_metrics.get(key)
        if new_value is None:
            continue
        old_num = _parse_number(old_value)
        new_num = _parse_number(new_value)
        if old_num is not None and new_num is not None and old_num != 0:
            delta = new_num - old_num
            delta_pct = delta / abs(old_num) * 100
            changes.append({"metric": key.rsplit("/", 1)[-1], "old": old_num, "new": new_num, "delta": delta, "delta_percent": round(delta_pct, 1)})
    # Judge each metric in its correct direction. Growth metrics improve when
    # they rise; risk metrics such as refunds improve when they fall.
    positive_metrics = ("成交", "roi", "ROI", "转化", "订单", "点击率", "gmv", "GMV")
    negative_metrics = ("退款", "退货", "取消率", "投诉")
    improvements = 0
    degradations = 0
    for change in changes:
        metric = change["metric"]
        delta = change["delta"]
        if any(keyword in metric for keyword in positive_metrics):
            improvements += int(delta > 0)
            degradations += int(delta < 0)
        elif any(keyword in metric for keyword in negative_metrics):
            improvements += int(delta < 0)
            degradations += int(delta > 0)
    effective = improvements > degradations
    entry["evaluated"] = True
    entry["evaluation"] = {
        "evaluated_at": _now_label(),
        "effective": effective,
        "improvements": improvements,
        "degradations": degradations,
        "changes": changes[:10],
    }
    snapshots[storage_key] = entry
    if storage_key != str(task_id):
        snapshots.pop(str(task_id), None)
    _atomic_json_write(_suggestion_snapshots_path(), snapshots)
    return entry


def get_effectiveness_report() -> dict[str, Any]:
    snapshots = load_suggestion_snapshots()
    scope = _task_scope_key()
    scoped = [
        item for item in snapshots.values()
        if isinstance(item, dict) and item.get("scope", scope) == scope
    ]
    evaluated = [item for item in scoped if item.get("evaluated")]
    effective = sum(1 for item in evaluated if item.get("evaluation", {}).get("effective"))
    total = len(evaluated)
    return {
        "generated_at": _now_label(),
        "total_tracked": len(scoped),
        "total_evaluated": total,
        "effective_count": effective,
        "effective_rate": round(effective / total * 100, 1) if total else 0,
        "recent_evaluations": [
            {
                "task_id": item["task_id"],
                "effective": item["evaluation"]["effective"],
                "changes": item["evaluation"]["changes"][:5],
                "evaluated_at": item["evaluation"]["evaluated_at"],
            }
            for item in evaluated[-10:]
        ],
        "mode": "read_only",
    }


def _scan_status_path() -> Path:
    return DATA_DIR / "scan_status.json"


def save_scan_status(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("scan status must be an object")
    allowed = {"status", "scope", "targeted_page_ids", "coverage_complete", "reason", "store_key", "account_mode", "account_key", "account_label", "started_at", "finished_at", "current", "index", "total", "success", "failed", "low_quality", "results", "error"}
    status = {key: value[key] for key in allowed if key in value}
    if status.get("status") not in {"idle", "running", "completed", "partial", "cancelled", "error"}:
        raise ValueError("invalid scan status")
    results = status.get("results", [])
    if not isinstance(results, list) or len(results) > 100:
        raise ValueError("invalid scan results")
    if status.get("scope") not in {None, "full", "quick"}:
        raise ValueError("invalid scan scope")
    for key_name in ("store_key", "account_key"):
        key_value = str(status.get(key_name) or "").lower()
        if key_value and not SAFE_KEY.fullmatch(key_value):
            raise ValueError(f"invalid {key_name}")
        status[key_name] = key_value
    status["account_label"] = _private_alias("account", status["account_key"]) if status.get("account_key") else ""
    targeted_page_ids = status.get("targeted_page_ids", [])
    if not isinstance(targeted_page_ids, list) or len(targeted_page_ids) > 20:
        raise ValueError("invalid targeted page ids")
    _atomic_json_write(_scan_status_path(), status)
    return status


def load_scan_status() -> dict[str, Any]:
    path = _scan_status_path()
    if not path.exists():
        return {"status": "idle", "index": 0, "total": 18, "success": 0, "failed": 0, "results": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"status": "idle"}
    except (OSError, json.JSONDecodeError):
        return {"status": "error", "error": "巡检状态文件无法读取"}


def build_scan_receipt() -> dict[str, Any]:
    """Turn the last browser scan into an operator-readable data receipt."""
    scan = load_scan_status()
    raw_results = scan.get("results") if isinstance(scan.get("results"), list) else []
    results: list[dict[str, Any]] = []
    source_totals = {
        "doudian": {"label": "抖店", "total": 0, "success": 0, "failed": 0, "needs_review": 0},
        "qianchuan": {"label": "千川", "total": 0, "success": 0, "failed": 0, "needs_review": 0},
    }
    account_label = str(scan.get("account_label") or "")[:80]
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        page_id = str(raw.get("id") or "unknown")[:64]
        source = str(raw.get("source") or ("qianchuan" if page_id.startswith("qianchuan") else "doudian"))
        if source not in source_totals:
            source = "doudian"
        quality = raw.get("quality") if isinstance(raw.get("quality"), dict) else {}
        quality_score = max(0, min(100, int(quality.get("score", 0) or 0)))
        ok = bool(raw.get("ok"))
        needs_review = ok and quality_score < 70
        source_totals[source]["total"] += 1
        source_totals[source]["success" if ok else "failed"] += 1
        if needs_review:
            source_totals[source]["needs_review"] += 1
        if source == "qianchuan" and raw.get("account_label") and not account_label:
            account_label = str(raw.get("account_label"))[:80]
        results.append(
            {
                "id": page_id,
                "label": str(raw.get("label") or page_id)[:80],
                "source": source,
                "ok": ok,
                "page_type": str(raw.get("page_type") or "")[:48],
                "quality_score": quality_score,
                "metric_count": max(0, int(quality.get("metric_count", 0) or 0)),
                "row_count": max(0, int(quality.get("row_count", 0) or 0)),
                "needs_review": needs_review,
                "error": str(raw.get("error") or "")[:300],
                "captured_at": int(raw.get("captured_at", 0) or 0),
            }
        )

    total = max(int(scan.get("total", 0) or 0), len(results))
    success = sum(1 for item in results if item["ok"])
    failed = sum(1 for item in results if not item["ok"])
    needs_review = sum(1 for item in results if item["needs_review"])
    completed = len(results)
    coverage_rate = round(completed / total * 100) if total else 0
    status = str(scan.get("status") or "idle")
    quick_scope = str(scan.get("scope") or "") == "quick"
    if status == "running":
        readiness = "running"
        readiness_label = "正在采集首诊断数据" if quick_scope else "正在采集"
    elif not results:
        readiness = "empty"
        readiness_label = "等待巡查"
    elif status == "completed" and quick_scope and not failed and not needs_review:
        readiness = "quick_ready"
        readiness_label = "首诊断数据已就绪"
    elif status == "completed" and not failed and not needs_review and coverage_rate == 100:
        readiness = "ready"
        readiness_label = "数据可用于分析"
    else:
        readiness = "attention"
        readiness_label = "需要补采或复核"

    warnings: list[str] = []
    if failed:
        warnings.append(f"{failed} 个页面读取失败，可在体检单中单独重试。")
    if needs_review:
        warnings.append(f"{needs_review} 个页面质量分低于 70，相关建议需要人工复核。")
    if total and completed < total:
        warnings.append(f"巡查仅覆盖 {completed}/{total} 个页面，数据不完整。")
    if status in {"cancelled", "error"} and scan.get("error"):
        warnings.append(str(scan.get("error"))[:300])

    return {
        "generated_at": _now_label(),
        "scan_status": status,
        "scope": "quick" if quick_scope else "full",
        "readiness": readiness,
        "readiness_label": readiness_label,
        "analysis_ready": readiness == "ready",
        "first_value_ready": readiness in {"quick_ready", "ready"},
        "store_key": str(scan.get("store_key") or "")[:80],
        "account_key": str(scan.get("account_key") or "")[:80],
        "account_label": account_label,
        "started_at": int(scan.get("started_at", 0) or 0),
        "finished_at": int(scan.get("finished_at", 0) or 0),
        "summary": {
            "total": total,
            "completed": completed,
            "success": success,
            "failed": failed,
            "needs_review": needs_review,
            "coverage_rate": coverage_rate,
            "row_count": sum(item["row_count"] for item in results),
        },
        "sources": source_totals,
        "results": results,
        "failed_page_ids": [item["id"] for item in results if not item["ok"]],
        "warnings": warnings,
        "mode": "read_only",
    }


def _timestamp_seconds(value: Any) -> int:
    """Normalize browser millisecond timestamps and server second timestamps."""
    try:
        timestamp = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return timestamp // 1000 if timestamp > 10_000_000_000 else timestamp


def build_operation_context(
    catalog: dict[str, Any] | None = None,
    receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the identity and data-trust gate shown before any recommendation."""
    catalog = catalog or build_store_catalog()
    receipt = receipt or build_scan_receipt()
    stores = catalog.get("stores") if isinstance(catalog.get("stores"), list) else []
    selected_key = str(catalog.get("selected_store_key") or catalog.get("selected_account_key") or "")
    selected = next(
        (
            item
            for item in stores
            if isinstance(item, dict) and str(item.get("key") or "") == selected_key
        ),
        None,
    )
    receipt_store_key = str(receipt.get("store_key") or "")
    receipt_account_key = str(receipt.get("account_key") or "")
    selected_account_key = str(catalog.get("selected_account_key") or "")
    identity_match = receipt_store_key == selected_key if receipt_store_key else (
        receipt_account_key == (selected_account_key or selected_key) if receipt_account_key else True
    )
    updated_at = max(
        _timestamp_seconds(selected.get("updated_at") if selected else 0),
        _timestamp_seconds(receipt.get("finished_at")),
    )
    age_seconds = max(0, int(time.time()) - updated_at) if updated_at else None
    fresh = age_seconds is not None and age_seconds <= 24 * 60 * 60
    official_ready = bool(
        selected
        and selected.get("channel") == "official_api"
        and selected.get("state") == "ready"
        and int(selected.get("page_count") or 0) > 0
    )
    browser_ready = bool(receipt.get("analysis_ready"))
    coverage_rate = int((receipt.get("summary") or {}).get("coverage_rate") or 0)
    blockers: list[str] = []
    warnings: list[str] = []

    if not selected:
        blockers.append("尚未选择当前店铺")
    elif selected.get("state") == "not_linked":
        blockers.append("当前店铺未关联可用的千川广告账户")
    elif selected.get("state") == "empty":
        blockers.append("当前店铺还没有可分析的数据")
    if not identity_match:
        blockers.append("最近巡检账号与当前店铺不一致")
    if selected and not fresh:
        warnings.append("当前店铺数据已超过 24 小时或缺少更新时间")
    if selected and selected.get("channel") == "browser" and not browser_ready:
        warnings.append("网页巡检尚未完整通过，相关建议需要人工复核")
    warnings.extend(
        str(item)[:200]
        for item in receipt.get("warnings", [])
        if isinstance(item, str)
    )

    if blockers:
        state = "blocked"
        state_label = "暂停经营判断"
        decision_policy = "blocked"
        next_action = "先确认店铺、千川账户和数据来源，再生成经营建议。"
    elif fresh and identity_match and (official_ready or browser_ready):
        state = "ready"
        state_label = "数据可信，可进入处理"
        decision_policy = "reviewable"
        next_action = "可以处理今日任务；涉及预算、启停和资金的动作仍需执行前复核。"
    else:
        state = "review"
        state_label = "建议仅供人工复核"
        decision_policy = "manual_review"
        next_action = "先同步官方数据或完成一次全店巡检，补齐后再进入投放执行准备。"

    source_label = "尚未绑定"
    if selected:
        source_label = "千川官方 API" if selected.get("channel") == "official_api" else "抖店 + 千川网页" if selected.get("channel") == "browser_multi" else "千川网页" if selected.get("channel") == "qianchuan_browser" else "抖店网页"
    freshness_label = "暂无更新时间"
    if age_seconds is not None:
        if age_seconds < 60:
            freshness_label = "刚刚更新"
        elif age_seconds < 60 * 60:
            freshness_label = f"{max(1, age_seconds // 60)} 分钟前"
        elif age_seconds < 24 * 60 * 60:
            freshness_label = f"{max(1, age_seconds // 3600)} 小时前"
        else:
            freshness_label = f"{max(1, age_seconds // 86400)} 天前"

    return {
        "generated_at": _now_label(),
        "state": state,
        "state_label": state_label,
        "decision_policy": decision_policy,
        "analysis_allowed": state != "blocked",
        "execution_review_allowed": state == "ready" and bool(selected_account_key) and int(selected.get("qianchuan_page_count") or 0) > 0 if selected else False,
        "selected_store": {
            "key": selected_key,
            "label": str(selected.get("label") or "未命名店铺") if selected else "尚未选择",
            "state": str(selected.get("state") or "empty") if selected else "empty",
            "state_label": str(selected.get("state_label") or "暂无数据") if selected else "尚未绑定",
            "channel": str(selected.get("channel") or "") if selected else "",
            "advertiser_count": selected.get("advertiser_count") if selected else None,
        },
        "identity_match": identity_match,
        "source_label": source_label,
        "freshness": {
            "updated_at": updated_at or None,
            "age_seconds": age_seconds,
            "fresh": fresh,
            "label": freshness_label,
        },
        "coverage": {
            "rate": coverage_rate,
            "label": f"{coverage_rate}% 网页覆盖" if receipt.get("summary") else "未生成网页体检",
            "official_ready": official_ready,
            "browser_ready": browser_ready,
        },
        "blockers": blockers,
        "warnings": list(dict.fromkeys(warnings))[:8],
        "next_action": next_action,
        "mode": "read_only",
    }


def build_action_center() -> dict[str, Any]:
    settings = load_agent_settings()
    plans = build_plan_recommendations(settings)
    inventory = build_inventory_alerts(settings)
    creative = build_qianchuan_creative_analysis(settings)
    return {
        "generated_at": _now_label(),
        "settings": settings,
        "plan_recommendations": plans,
        "inventory_alerts": inventory,
        "shelf_analysis": build_shelf_analysis(),
        "live_analysis": build_live_analysis(),
        "creative_analysis": creative,
        "summary": {
            "plan_actions": len(plans),
            "high_risk_plans": sum(1 for item in plans if item["level"] == "high"),
            "inventory_alerts": len(inventory),
            "critical_inventory": sum(1 for item in inventory if item["level"] == "high"),
            "creative_actions": len(creative["recommendations"]),
        },
        "mode": "read_only",
    }


def build_stop_loss_queue(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Turn plan diagnostics into a ranked, operator-friendly loss-control queue."""

    settings = settings or load_agent_settings()
    mode = str(settings.get("execution_mode") or "observe")
    min_spend = max(1.0, float(settings.get("min_spend_for_action") or 100))
    queue: list[dict[str, Any]] = []
    for item in build_plan_recommendations(settings):
        action_type = str(item.get("action_type") or "")
        if action_type not in {"stop_loss", "reduce_budget", "optimize", "inspect_plans"}:
            continue
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        spend = float(evidence.get("spend") or 0)
        roi = evidence.get("roi")
        target = float(evidence.get("roi_target") or settings.get("roi_target") or 1.5)
        roi_gap = 0.0 if not isinstance(roi, (int, float)) or target <= 0 else max(0.0, (target - float(roi)) / target)
        severity = {"stop_loss": 45, "reduce_budget": 35, "optimize": 18, "inspect_plans": 10}.get(action_type, 0)
        spend_component = min(30, round(spend / min_spend * 12.0))
        roi_component = min(25, round(roi_gap * 25.0))
        confidence_component = 0 if item.get("confidence") == "low" else 5 if item.get("confidence") == "medium" else 10
        risk_score = min(100, severity + spend_component + roi_component + confidence_component)
        reduction = 0.30 if action_type == "stop_loss" else 0.20 if action_type == "reduce_budget" else 0.0
        saving_high = round(spend * reduction, 2)
        saving_low = round(saving_high * 0.5, 2)
        if item.get("confidence") == "low" or action_type == "inspect_plans":
            bucket = "data_missing"
        elif action_type in {"stop_loss", "reduce_budget"} and risk_score >= 60:
            bucket = "must_handle"
        else:
            bucket = "observe"
        queue.append({
            **item,
            "risk_score": risk_score,
            "risk_components": [
                {"key": "action", "label": "动作风险", "score": severity, "max_score": 45},
                {"key": "spend", "label": "消耗风险", "score": spend_component, "max_score": 30},
                {"key": "roi_gap", "label": "ROI偏差", "score": roi_component, "max_score": 25},
                {"key": "confidence", "label": "数据可信度", "score": confidence_component, "max_score": 10},
            ],
            "bucket": bucket,
            "bucket_label": {"must_handle": "必须处理", "observe": "继续观察", "data_missing": "补齐数据"}[bucket],
            "estimated_savings_low": saving_low,
            "estimated_savings_high": saving_high,
            "estimated_savings_label": f"预计可避免继续无效消耗 ¥{saving_low:g}–¥{saving_high:g}" if saving_high else "暂不估算可避免消耗",
            "execution_mode": mode,
            "can_start_execution": mode == "supervised" and action_type in {"stop_loss", "reduce_budget"},
        })
    bucket_order = {"must_handle": 0, "observe": 1, "data_missing": 2}
    queue.sort(key=lambda item: (bucket_order[item["bucket"]], -item["risk_score"], -(item.get("evidence", {}).get("spend") or 0)))
    return {
        "generated_at": _now_label(),
        "execution_mode": mode,
        "execution_mode_label": {"observe": "观察模式", "shadow": "影子模式", "supervised": "受控执行"}[mode],
        "items": queue[:10],
        "summary": {
            "must_handle": sum(1 for item in queue if item["bucket"] == "must_handle"),
            "observe": sum(1 for item in queue if item["bucket"] == "observe"),
            "data_missing": sum(1 for item in queue if item["bucket"] == "data_missing"),
            "estimated_savings_low": round(sum(item["estimated_savings_low"] for item in queue), 2),
            "estimated_savings_high": round(sum(item["estimated_savings_high"] for item in queue), 2),
        },
        "estimate_note": "金额按当前消耗与建议降幅保守估算，表示可能避免的后续无效消耗，不代表实际结算结果。",
    }


def build_strategy_simulation(
    queue_report: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare read-only operating policies before any campaign is authorized."""

    settings = settings or load_agent_settings()
    queue_report = queue_report or build_stop_loss_queue(settings)
    queue = queue_report.get("items") if isinstance(queue_report.get("items"), list) else []
    policies = [
        {"key": "protect_roi", "label": "保 ROI", "description": "优先阻断高风险消耗，适合利润承压或预算紧张阶段。", "risk_threshold": 55, "action_strength": 1.0},
        {"key": "balanced", "label": "均衡经营", "description": "只处理证据较充分的高风险计划，兼顾消耗与成交稳定。", "risk_threshold": 65, "action_strength": 0.8},
        {"key": "cautious_growth", "label": "谨慎增长", "description": "仅处理最明确的亏损计划，其余保留预算继续观察。", "risk_threshold": 75, "action_strength": 0.5},
    ]
    scenarios: list[dict[str, Any]] = []
    for policy in policies:
        selected = [
            item for item in queue
            if item.get("bucket") == "must_handle" and int(item.get("risk_score") or 0) >= policy["risk_threshold"]
        ]
        budget_impact = 0.0
        orders_at_risk = 0.0
        for item in selected:
            action = item.get("action_params") if isinstance(item.get("action_params"), dict) else {}
            change = action.get("change") if isinstance(action.get("change"), dict) else {}
            current_value, target_value = change.get("current_value"), change.get("target_value")
            if isinstance(current_value, (int, float)) and isinstance(target_value, (int, float)):
                budget_impact += max(0.0, float(current_value) - float(target_value)) * policy["action_strength"]
            evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
            orders = evidence.get("orders")
            if isinstance(orders, (int, float)):
                orders_at_risk += max(0.0, float(orders)) * 0.1 * policy["action_strength"]
        scenarios.append({
            **policy,
            "selected_plan_count": len(selected),
            "selected_plan_names": [str(item.get("plan") or "千川计划") for item in selected[:5]],
            "estimated_budget_impact": round(budget_impact, 2),
            "estimated_avoided_waste_low": round(sum(float(item.get("estimated_savings_low") or 0) for item in selected) * policy["action_strength"], 2),
            "estimated_avoided_waste_high": round(sum(float(item.get("estimated_savings_high") or 0) for item in selected) * policy["action_strength"], 2),
            "estimated_orders_at_risk": round(orders_at_risk, 1),
            "can_execute": False,
        })
    decision_store = load_strategy_decisions()
    return {
        "generated_at": _now_label(),
        "recommended_policy": "balanced",
        "recommended_reason": "默认采用均衡经营：只纳入证据充分的高风险计划，再由投手逐项确认。",
        "scenarios": scenarios,
        "execution_enabled": False,
        "selected_decision": decision_store.get("current"),
        "note": "这是基于当前快照的静态模拟，不会修改计划；订单风险为保守提示，不是因果预测。",
    }


def _strategy_decisions_path() -> Path:
    return DATA_DIR / "strategy_decisions.json"


def load_strategy_decisions() -> dict[str, Any]:
    path = _strategy_decisions_path()
    if not path.exists():
        return {"schema_version": 1, "current": None, "history": []}
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        return payload if isinstance(payload, dict) else {"schema_version": 1, "current": None, "history": []}
    except (OSError, json.JSONDecodeError):
        logger.exception("读取策略决策单失败: %s", path)
        return {"schema_version": 1, "current": None, "history": []}


def save_strategy_decision(policy_key: str) -> dict[str, Any]:
    simulation = build_strategy_simulation()
    scenario = next((item for item in simulation["scenarios"] if item["key"] == policy_key), None)
    if not scenario:
        raise ValueError("策略类型无效。")
    now_ms = int(time.time() * 1000)
    decision = {
        "decision_id": hashlib.sha256(f"{policy_key}:{now_ms}".encode("utf-8")).hexdigest()[:20],
        "policy_key": policy_key,
        "policy_label": scenario["label"],
        "selected_at_ms": now_ms,
        "selected_at": _now_label(),
        "scenario": scenario,
        "execution_enabled": False,
        "next_step": "策略已记录；请回到今日止损队列，逐个核对并授权计划。",
    }
    store = load_strategy_decisions()
    history = store.get("history") if isinstance(store.get("history"), list) else []
    history.append(decision)
    _atomic_json_write(_strategy_decisions_path(), {"schema_version": 1, "current": decision, "history": history[-100:]})
    return decision


def _atomic_text_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _reports_dir() -> Path:
    store_key = str(load_agent_settings().get("store_key") or "legacy_unscoped").lower()
    safe_scope = store_key if SAFE_KEY.fullmatch(store_key) else "legacy_unscoped"
    return DATA_DIR / "reports" / safe_scope


def _report_list(items: list[str], empty: str) -> str:
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, 1)) if items else f"- {empty}"


def _render_selected_report(
    template_key: str,
    custom_template: str,
    report_date: str,
    insights: dict[str, Any],
    action_center: dict[str, Any],
    ops: dict[str, Any],
    scan: dict[str, Any],
    scan_receipt: dict[str, Any],
) -> list[str] | None:
    if template_key == "default":
        return None
    top_tasks = [
        f"[{item['owner']}] {item['title']}：{item['action']}（验收：{item['acceptance']}）"
        for item in ops.get("today_top_actions", [])[:8]
    ]
    plans = [
        f"{item['plan']}：{item['suggestion']}（{item['reason']}）"
        for item in action_center.get("plan_recommendations", [])[:8]
    ]
    inventory = [
        f"{item['product']}：{item['title']}；{item['suggestion']}"
        for item in action_center.get("inventory_alerts", [])[:8]
    ]
    alerts = [
        f"{item['title']}：{item.get('action') or item.get('detail') or ''}"
        for item in insights.get("alerts", [])[:6]
    ]
    plan_items = action_center.get("plan_recommendations", [])
    creative = action_center.get("creative_analysis") if isinstance(action_center.get("creative_analysis"), dict) else {}
    creative_summary = creative.get("summary") if isinstance(creative.get("summary"), dict) else {}
    metrics = [
        f"- 计划建议 {len(plan_items)} 条；今日待办 {len(ops.get('today_top_actions', []))} 条；库存预警 {len(action_center.get('inventory_alerts', []))} 条",
        f"- 内容样本 {creative_summary.get('total_videos', 0)} 条；有消耗 {creative_summary.get('spending_videos', 0)} 条；未测试 {creative_summary.get('untested_videos', 0)} 条",
        f"- 数据覆盖 {scan_receipt['summary'].get('coverage_rate', 0)}%；需人工复核 {scan_receipt['summary'].get('needs_review', 0)} 项",
    ]
    content_review = [
        f"- {item.get('title', '内容建议')}：{item.get('action', '')}（依据：{item.get('evidence', '')}）"
        for item in creative.get('recommendations', [])[:8]
    ]
    memory = creative.get("memory") if isinstance(creative.get("memory"), dict) else {}
    for item in memory.get("patterns", [])[:5]:
        confidence = {"high": "高可信", "medium": "较可信", "low": "仅作线索"}.get(str(item.get("confidence") or ""), "仅作线索")
        content_review.append(
            f"- 本店内容记忆：{item.get('dimension')}“{item.get('value')}”｜胜出 {item.get('win_count', 0)} 条｜风险 {item.get('risk_count', 0)} 条｜{confidence}"
        )
    execution_log = [
        "- 所有预算、暂停、恢复动作均需单计划、单次授权，并以页面回读作为成功条件。",
        "- 本日报只记录建议与回执，不把建议数量当作投放结果；实际结果需下一周期复盘。",
    ]
    scan_status = (
        f"巡检 {scan.get('status', 'idle')}，成功 {scan.get('success', 0)} 页，"
        f"失败 {scan.get('failed', 0)} 页；体检覆盖率 {scan_receipt['summary']['coverage_rate']}%，"
        f"需复核 {scan_receipt['summary']['needs_review']} 页。"
    )
    context = {
        "date": report_date,
        "generated_at": _now_label(),
        "headline": str(insights.get("headline") or "暂无结论"),
        "summary": str(insights.get("summary") or "暂无摘要"),
        "top_tasks": _report_list(top_tasks, "暂无待办任务。"),
        "plans": _report_list(plans, "暂无千川调整建议。"),
        "inventory": _report_list(inventory, "暂无库存预警。"),
        "alerts": _report_list(alerts, "暂无其他异常。"),
        "scan_status": scan_status,
        "metrics": _report_list(metrics, "暂无可用经营数据"),
        "content_review": _report_list(content_review, "暂无内容复盘建议"),
        "execution_log": _report_list(execution_log, "暂无执行记录"),
    }
    if template_key == "brief":
        return [
            f"# 店策 Agent 老板简报 - {report_date}",
            "",
            f"> 生成时间：{context['generated_at']}｜模式：只读建议",
            "",
            "## 一句话结论",
            "",
            f"- {context['headline']}",
            f"- {context['summary']}",
            "",
            "## 今天先做",
            "",
            context["top_tasks"],
            "",
            "## 需要关注",
            "",
            context["alerts"],
            "",
            "## 数据状态",
            "",
            f"- {scan_status}",
            "",
        ]
    if template_key == "handover":
        return [
            f"# 店策 Agent 运营交接日志 - {report_date}",
            "",
            f"> 交接生成时间：{context['generated_at']}｜所有执行动作需人工确认",
            "",
            "## 本班结论",
            "",
            f"- {context['headline']}",
            f"- {context['summary']}",
            "",
            "## 下一班优先事项",
            "",
            context["top_tasks"],
            "",
            "## 千川待处理",
            "",
            context["plans"],
            "",
            "## 库存待处理",
            "",
            context["inventory"],
            "",
            "## 数据交接",
            "",
            f"- {scan_status}",
            "",
        ]
    template = custom_template or DEFAULT_CUSTOM_REPORT_TEMPLATE
    for key, value in context.items():
        template = template.replace(f"{{{{{key}}}}}", str(value))
    return template.splitlines()


def generate_daily_report(report_date: str | None = None) -> dict[str, Any]:
    report_date = report_date or time.strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
        raise ValueError("report_date must be YYYY-MM-DD")
    insights = build_insights()
    action_center = build_action_center()
    ops = build_ops_manager()
    scan = load_scan_status()
    scan_receipt = build_scan_receipt()
    catalog = list_snapshots()
    report_path = _reports_dir() / f"{report_date}.md"

    # Build staleness map: source/page_type -> saved_at and age
    freshness_map: dict[str, dict[str, Any]] = {}
    for item in catalog:
        freshness_map[f"{item['source']}/{item['page_type']}"] = {
            "saved_at": item.get("saved_at", ""),
            "age_seconds": item.get("age_seconds", 0),
            "fresh": item.get("fresh", False),
        }

    # Identify stale data sources (>1 hour)
    stale_sources = [
        f"{key} (更新于 {info['saved_at']})"
        for key, info in freshness_map.items()
        if info["age_seconds"] > 3600
    ]

    lines = [
        f"# 店策 Agent 每日经营报告 - {report_date}",
        "",
        f"> 生成时间：{_now_label()}｜模式：只读建议",
        "",
    ]

    # Staleness warning section
    if stale_sources:
        lines.extend([
            "⚠️ **数据时效提醒**：以下数据源超过 1 小时未更新，建议重新同步后再做决策：",
            "",
        ])
        for src in stale_sources[:5]:
            lines.append(f"- {src}")
        lines.append("")

    lines.extend([
        "## 今日结论",
        "",
        f"- {insights['headline']}",
        f"- {insights['summary']}",
        f"- 已同步页面：{len(insights['coverage'])}；千川调整项：{len(action_center['plan_recommendations'])}；库存预警：{len(action_center['inventory_alerts'])}",
        f"- 自动巡检：{scan.get('status', 'idle')}；成功 {scan.get('success', 0)} 页，失败 {scan.get('failed', 0)} 页，低质量 {scan.get('low_quality', 0)} 页。",
        f"- 数据体检：{scan_receipt['readiness_label']}；覆盖率 {scan_receipt['summary']['coverage_rate']}%；需复核 {scan_receipt['summary']['needs_review']} 页。",
        "",
        "## 今日重点任务",
        "",
    ])
    for index, item in enumerate(ops["today_top_actions"][:8], 1):
        source_key = item.get("source", "")
        data_time = freshness_map.get(source_key, {}).get("saved_at", "未知")
        lines.extend([f"{index}. **[{item['owner']}] {item['title']}**：{item['action']}", f"   - 依据：{item['evidence']}｜验收：{item['acceptance']}｜数据时间：{data_time}"])
    lines.extend(["", "## 货架商品", ""])
    for item in action_center["shelf_analysis"]["recommendations"]:
        lines.append(f"- **{item['title']}**：{item['action']}（{item['evidence']}）")
    if not action_center["shelf_analysis"]["recommendations"]:
        lines.append("- 暂无货架专项建议。")
    lines.extend(["", "## 直播投放", ""])
    for item in action_center["live_analysis"]["recommendations"]:
        lines.append(f"- **{item['title']}**：{item['action']}（{item['evidence']}）")
    if not action_center["live_analysis"]["recommendations"]:
        lines.append("- 暂无直播专项建议。")
    lines.extend([
        "",
        "## 千川计划调整建议",
        "",
    ])
    plans = action_center["plan_recommendations"]
    if plans:
        qianchuan_time = freshness_map.get("qianchuan/report", freshness_map.get("qianchuan/campaigns", {})).get("saved_at", "未知")
        for index, item in enumerate(plans[:10], 1):
            lines.extend([f"{index}. **{item['plan']}**：{item['suggestion']}", f"   - 依据：{item['reason']}｜数据时间：{qianchuan_time}"])
    else:
        lines.append("- 暂无可执行建议；请同步千川计划列表和报表页面。")
    lines.extend(["", "## 内容", ""])
    creative = action_center["creative_analysis"]
    summary = creative["summary"]
    lines.append(f"- 视频 {summary['total_videos']} 条；在投/有消耗 {summary['spending_videos']} 条；未测试 {summary['untested_videos']} 条；高风险 {summary['risky_videos']} 条；高潜 {summary['high_potential_videos']} 条。")
    for item in creative["recommendations"][:8]:
        lines.append(f"- **{item['title']}**：{item['action']}（{item['evidence']}）")
    content_memory = creative.get("memory") if isinstance(creative.get("memory"), dict) else {}
    lines.extend(["", "### 本店内容记忆", ""])
    patterns = content_memory.get("patterns") if isinstance(content_memory.get("patterns"), list) else []
    if patterns:
        for item in patterns[:8]:
            confidence = {"high": "高可信", "medium": "较可信", "low": "仅作线索"}.get(str(item.get("confidence") or ""), "仅作线索")
            lines.append(f"- {item.get('dimension')} **{item.get('value')}**：胜出 {item.get('win_count', 0)} 条，风险 {item.get('risk_count', 0)} 条，{confidence}。")
    else:
        lines.append("- 尚未积累足够的不同素材，暂不输出店铺内容规律。")
    lines.extend(["", "## 经营数据明细", ""])
    lines.extend([
        f"- 千川计划建议：{len(plans)} 条；可进入受监督动作的计划需具备唯一计划标识、当前状态和页面回读。",
        f"- 内容样本：{summary['total_videos']} 条；有消耗 {summary['spending_videos']} 条；未测试 {summary['untested_videos']} 条；高风险 {summary['risky_videos']} 条。",
        f"- 数据覆盖：{scan_receipt['summary']['coverage_rate']}%；需要复核：{scan_receipt['summary']['needs_review']} 项；低质量页面：{scan.get('low_quality', 0)} 项。",
        "- 口径：消耗、ROI、成交、点击率等只作为当前周期诊断输入，不能替代平台结算数据。",
    ])
    lines.extend(["", "## 执行与风险台账", "", "- 预算调整、暂停、恢复均按单计划执行，必须经过授权；平台回执和下一次同步结果分别记录。", "- 失败或状态不一致的动作自动停留在人工处理，不重试、不批量扩散。"])
    lines.extend(["", "## 库存预警", ""])
    inventory = action_center["inventory_alerts"]
    if inventory:
        doudian_time = freshness_map.get("doudian/products", {}).get("saved_at", "未知")
        for index, item in enumerate(inventory[:15], 1):
            lines.append(f"{index}. **{item['product']}**：{item['title']}；{item['suggestion']}（数据时间：{doudian_time}）")
    else:
        lines.append("- 暂无库存预警，或尚未同步商品/库存页面。")
    lines.extend(["", "## 其他优先事项", ""])
    for index, item in enumerate(insights["alerts"][:8], 1):
        confidence_tag = f"[{item['confidence']}]" if item.get("confidence") else ""
        lines.append(f"{index}. **{item['title']}** {confidence_tag}：{item.get('action') or item.get('detail') or ''}")
    lines.extend(
        [
            "",
            "## 安全边界",
            "",
            "- 本报告来自已登录网页的本地脱敏快照，不等同于官方 API 数据。",
            "- 所有预算、启停和店铺变更建议必须在后台核对统计周期与归因口径后人工确认。",
            "",
        ]
    )
    template_key = str(action_center["settings"].get("report_template") or "default")
    selected_lines = _render_selected_report(
        template_key,
        str(action_center["settings"].get("custom_report_template") or ""),
        report_date,
        insights,
        action_center,
        ops,
        scan,
        scan_receipt,
    )
    if selected_lines is not None:
        lines = selected_lines
    _atomic_text_write(report_path, "\n".join(lines))
    _cleanup_old_reports(int(action_center["settings"]["report_retention_days"]))
    return {
        "date": report_date,
        "generated_at": _now_label(),
        "path": str(report_path),
        "headline": insights["headline"],
        "summary": action_center["summary"],
        "template": template_key,
        "stale_sources": stale_sources[:5],
        "content": "\n".join(lines),
    }


def _cleanup_old_reports(retention_days: int) -> None:
    cutoff = time.time() - retention_days * 86400
    reports_dir = _reports_dir()
    if not reports_dir.exists():
        return
    for path in reports_dir.glob("*.md"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            logger.exception("清理旧日报失败: %s", path)


def load_latest_report() -> dict[str, Any] | None:
    reports_dir = _reports_dir()
    if not reports_dir.exists():
        return None
    paths = sorted(reports_dir.glob("*.md"), reverse=True)
    if not paths:
        return None
    path = paths[0]
    try:
        return {"date": path.stem, "path": str(path), "content": path.read_text(encoding="utf-8")}
    except OSError:
        logger.exception("读取日报失败: %s", path)
        return None


def _daily_report_scheduler(stop_event: threading.Event) -> None:
    while not stop_event.wait(30):
        try:
            settings = load_agent_settings()
            if not settings["daily_report_enabled"]:
                continue
            now = datetime.now()
            if now.strftime("%H:%M") < settings["daily_report_time"]:
                continue
            target = _reports_dir() / f"{now:%Y-%m-%d}.md"
            if not target.exists():
                report = generate_daily_report(now.strftime("%Y-%m-%d"))
                if _load_integration_secrets().get("auto_send_reports"):
                    send_report_notifications(report)
                logger.info("已生成每日经营报告: %s", target)
        except Exception:
            logger.exception("生成定时日报失败")


def _knowledge_update_scheduler(stop_event: threading.Event) -> None:
    """Check the configured signed knowledge feed once per day."""
    while not stop_event.is_set():
        if os.environ.get("DIAN_AGENT_UPDATE_MANIFEST_URL"):
            try:
                center = _update_center()
                result = center.check_for_update()
                _save_update_settings({"last_check_at": _now_label(), "last_check": result})
                if result.get("available"):
                    installed = center.install()
                    # Loading and constructing the engine is the final local
                    # canary before this process exposes the new rules.
                    RuleEngine(center.load_effective_pack())
                    _save_update_settings({
                        "last_check_at": _now_label(),
                        "last_check": {"available": False, "candidate_version": installed.get("pack_version")},
                    })
                    _invalidate_cache()
                    logger.info("经营知识包已自动更新: %s", installed.get("pack_version"))
            except (UpdateError, RulePackError, ValueError, OSError) as error:
                logger.warning("经营知识包自动更新失败，继续使用当前版本: %s", error)
                _save_update_settings({"last_check_at": _now_label(), "last_check": {"available": False, "error": str(error)}})
        stop_event.wait(24 * 60 * 60)


def _current_memory_scope(query: dict[str, list[str]] | None = None) -> tuple[str, str]:
    """Resolve memory scope from an explicit request or the locked current shop."""
    query = query or {}
    catalog = build_store_catalog()
    store_key = str((query.get("store_key") or [""])[0] or catalog.get("selected_store_key") or "").lower()
    account_key = str((query.get("account_key") or [""])[0] or catalog.get("selected_account_key") or "").lower()
    return store_key, account_key


class Handler(BaseHTTPRequestHandler):
    server_version = f"DianAgent/{AGENT_VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug(fmt, *args)

    def _cors(self) -> None:
        origin = self.headers.get("Origin", "")
        if request_origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Dian-Agent")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed_url = urlparse(self.path)
        path = unquote(parsed_url.path).rstrip("/") or "/"
        query = parse_qs(parsed_url.query)
        policy = resolve_deployment_policy()
        if not policy.local_companion and path not in {"/health", "/marketplace/status", "/marketplace/readiness"}:
            self._json({
                "error": "marketplace_authenticated_gateway_required",
                "deployment_mode": policy.mode,
                "message": "服务市场数据读取必须经租户/店铺绑定的认证网关。",
            }, 403)
            return
        if path == "/health":
            catalog = list_snapshots() if policy.local_companion else []
            schema_warnings = _schema_version_check()
            disk_info = _disk_usage_check()
            agent_settings = load_agent_settings()
            execution_mode = str(agent_settings.get("execution_mode") or "observe")
            self._json(
                {
                    "status": "ok",
                    "version": AGENT_VERSION,
                    "bridge_protocol_version": 2,
                    "mode": execution_mode,
                    "execution_mode_label": {"observe": "观察模式", "shadow": "影子模式", "supervised": "受控执行"}.get(execution_mode, "未知模式"),
                    "execution_enabled": execution_mode == "supervised" and policy.browser_dom_execution,
                    "deployment": policy.public_status(),
                    "snapshot_count": len(catalog),
                    "schema_warnings": schema_warnings,
                    "disk": disk_info,
                    "sources": {
                        source: {
                            "has_data": any(item["source"] == source for item in catalog),
                            "pages": sum(1 for item in catalog if item["source"] == source),
                        }
                        for source in sorted(ALLOWED_SOURCES)
                    },
                }
            )
            return
        if path == "/oauth/oceanengine/status":
            self._json(OceanEngineOAuth(DATA_DIR).status())
            return
        if path == "/oauth/oceanengine/sync-status":
            self._json(load_sync_status(DATA_DIR))
            return
        if path == "/oauth/oceanengine/callback":
            oauth = OceanEngineOAuth(DATA_DIR)
            platform_error = str(
                query.get("error", query.get("error_code", [""]))[0] or ""
            )
            if platform_error:
                self._html(
                    oauth.result_page(
                        success=False,
                        title="千川授权未完成",
                        message="平台返回了取消或失败结果，请回到店策重新点击授权。",
                    ),
                    400,
                )
                return
            try:
                result = oauth.complete_authorization(
                    str(query.get("auth_code", query.get("code", [""]))[0] or ""),
                    str(query.get("state", [""])[0] or ""),
                )
                warning = str(result.get("warning") or "")
                message = "官方 API 已连接，店策可以读取本次授权的千川账号。"
                if warning:
                    message = f"{message} {warning}"
                self._html(
                    oauth.result_page(
                        success=True,
                        title="千川账号授权成功",
                        message=message,
                        account_count=int(result.get("account_count") or 0),
                    )
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                logger.warning("千川 OAuth 回调处理失败: %s", error)
                self._html(
                    oauth.result_page(
                        success=False,
                        title="千川授权未完成",
                        message=str(error),
                    ),
                    400,
                )
            return
        if path == "/catalog":
            self._json({"snapshots": list_snapshots()})
            return
        if path in {"/insights", "/brief"}:
            self._json(_cached("insights", build_insights))
            return
        if path in {"/action-center", "/recommendations"}:
            self._json(_cached("action_center", build_action_center))
            return
        if path == "/shelf-analysis":
            self._json(_cached("shelf", build_shelf_analysis))
            return
        if path == "/live-analysis":
            self._json(_cached("live", build_live_analysis))
            return
        if path == "/qianchuan-creative-analysis":
            self._json(_cached("creative", build_qianchuan_creative_analysis))
            return
        if path == "/qianchuan-accounts":
            self._json(build_store_catalog())
            return
        if path == "/qianchuan/promotion-readiness":
            self._json(build_current_promotion_readiness())
            return
        if path == "/stores":
            self._json(build_store_catalog())
            return
        if path == "/system/status":
            self._json(build_system_status())
            return
        if path == "/runtime/status":
            self._json(build_agent_runtime_status())
            return
        if path == "/onboarding/status":
            self._json(build_onboarding_status())
            return
        if path == "/connection-guide":
            self._json(build_connection_guide())
            return
        if path == "/distribution/status":
            self._json(build_distribution_status(DATA_DIR.parent))
            return
        if path in {"/marketplace/status", "/marketplace/readiness"}:
            self._json(build_marketplace_readiness())
            return
        if path == "/telemetry/status":
            settings = _load_update_settings()
            self._json(LocalAnonymousFeedbackQueue(DATA_DIR.parent).status(
                consent_enabled=settings["telemetry_enabled"]
            ))
            return
        if path == "/release/readiness":
            self._json(build_release_readiness(
                DATA_DIR.parent,
                production_ed25519_trust=bool(PRODUCTION_OFFLINE_PUBLIC_KEYS),
            ))
            return
        if path == "/rules/status":
            self._json(_knowledge_status())
            return
        if path == "/memory":
            store_key, account_key = _current_memory_scope(query)
            if not store_key:
                self._json({
                    "schema_version": 1,
                    "scope": {"store_key": "", "account_key": account_key},
                    "entries": [],
                    "count": 0,
                    "counts": {},
                    "storage": "local",
                    "note": "请先选择当前店铺，再读取经营记忆。",
                })
                return
            self._json(list_operator_memory(DATA_DIR, store_key, account_key))
            return
        if path == "/ops-manager":
            self._json(_cached("ops_manager", build_ops_manager))
            return
        if path == "/tasks":
            ops = _cached("ops_manager", build_ops_manager)
            self._json({"states": load_task_states(), "tasks": ops["all_tasks"]})
            return
        if path == "/scan-status":
            self._json(load_scan_status())
            return
        if path == "/scan-receipt":
            self._json(build_scan_receipt())
            return
        if path == "/operation-context":
            self._json(build_operation_context())
            return
        if path == "/health-monitor":
            self._json(check_selector_health())
            return
        if path == "/feedback":
            self._json(get_feedback_stats())
            return
        if path.startswith("/tasks/export"):
            fmt = query.get("format", ["clipboard"])[0]
            self._json(export_tasks(fmt))
            return
        if path == "/effectiveness":
            self._json(get_effectiveness_report())
            return
        if path == "/actions/audit":
            self._json(get_action_audit(int(query.get("limit", ["100"])[0])))
            return
        if path == "/actions/readiness":
            self._json(build_automation_readiness())
            return
        if path == "/actions/stop-loss-queue":
            self._json(build_stop_loss_queue())
            return
        if path == "/actions/strategy-simulation":
            self._json(build_strategy_simulation())
            return
        if path == "/actions/preflight":
            self._json(build_execution_preflight_report())
            return
        if path == "/actions/shadow":
            self._json(build_shadow_execution_report())
            return
        if path == "/actions/effectiveness":
            self._json(build_execution_effectiveness_report())
            return
        if path == "/value-ledger":
            self._json(build_value_ledger())
            return
        if path == "/trends":
            self._json(build_trends(int(query.get("days", ["7"])[0]), query.get("source", [None])[0], query.get("page_type", [None])[0]))
            return
        if path == "/settings":
            self._json(load_agent_settings())
            return
        if path == "/integrations":
            self._json(get_integration_settings())
            return
        if path == "/reports/latest":
            report = load_latest_report()
            self._json(report or {"error": "report_not_found"}, 200 if report else 404)
            return
        if path.startswith("/data/"):
            parts = [part for part in path.split("/") if part]
            source = parts[1] if len(parts) > 1 else ""
            page_type = parts[2] if len(parts) > 2 else None
            snapshot = load_data(source, page_type)
            if snapshot:
                self._json(snapshot)
            else:
                self._json({"error": "snapshot_not_found", "source": source, "page_type": page_type}, 404)
            return
        self._json({"error": "not_found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path).rstrip("/") or "/"
        policy = resolve_deployment_policy()
        blocked_capability = blocked_browser_capability(path, policy)
        if blocked_capability:
            self._json({
                "error": blocked_capability,
                "deployment_mode": policy.mode,
                "message": "服务市场模式只允许官方 OAuth/Open API 数据链路，已拒绝本地扩展通道。",
            }, 403)
            return
        if not policy.local_companion:
            self._json({
                "error": "marketplace_authenticated_gateway_required",
                "deployment_mode": policy.mode,
                "message": "当前 HTTP 接收器不提供云端身份认证，服务市场写操作必须经租户/店铺绑定的认证网关。",
            }, 403)
            return
        if self.headers.get("X-Dian-Agent") not in {"1", "2"}:
            self._json({"error": "missing_bridge_header"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json({"error": "invalid_content_length"}, 400)
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json({"error": "body_too_large", "max_bytes": MAX_BODY_BYTES}, 413)
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if path == "/push":
                source = payload.get("source")
                data = payload.get("data")
                saved = save_data(source, data)
                _invalidate_cache()
                account = saved.get("data", {}).get("account") if isinstance(saved.get("data"), dict) else None
                store = saved.get("data", {}).get("store") if isinstance(saved.get("data"), dict) else None
                self._json({"ok": True, "source": source, "page_type": saved["page_type"], "account": account, "store": store, "identity_resolution": saved.get("data", {}).get("identity_resolution")})
                return
            if path == "/stores/select":
                result = select_store_context(str(payload.get("store_key") or ""), str(payload.get("account_key") or ""))
                _invalidate_cache()
                self._json({"ok": True, **result})
                return
            if path == "/stores/link":
                result = link_store_account(str(payload.get("store_key") or ""), str(payload.get("account_key") or ""))
                _invalidate_cache()
                self._json({"ok": True, **result})
                return
            if path == "/settings":
                settings = save_agent_settings(payload)
                _invalidate_cache()
                self._json({"ok": True, "settings": settings})
                return
            if path == "/memory/upsert":
                store_key, account_key = _current_memory_scope({})
                memory_payload = dict(payload)
                memory_payload["store_key"] = str(payload.get("store_key") or store_key)
                memory_payload["account_key"] = str(payload.get("account_key") or account_key)
                saved = upsert_operator_memory(DATA_DIR, memory_payload)
                self._json(saved)
                return
            if path == "/memory/archive":
                store_key, account_key = _current_memory_scope({})
                archived = archive_operator_memory(
                    DATA_DIR,
                    payload.get("id"),
                    str(payload.get("store_key") or store_key),
                    str(payload.get("account_key") or account_key),
                )
                self._json(archived)
                return
            if path == "/updates/channel":
                settings = _save_update_settings({"channel": payload.get("channel")})
                self._json({"ok": True, "channel": settings["channel"], "message": "更新通道已保存"})
                return
            if path == "/telemetry/settings":
                if not isinstance(payload.get("enabled"), bool):
                    raise ValueError("enabled 必须是布尔值")
                settings = _save_update_settings({"telemetry_enabled": payload["enabled"]})
                self._json({
                    "ok": True,
                    "enabled": settings["telemetry_enabled"],
                    "raw_shop_data_uploaded": False,
                    "message": "匿名改进计划已开启" if settings["telemetry_enabled"] else "匿名改进计划已关闭",
                })
                return
            if path == "/telemetry/queue":
                settings = _load_update_settings()
                queued = LocalAnonymousFeedbackQueue(DATA_DIR.parent).enqueue(
                    payload,
                    consent_enabled=settings["telemetry_enabled"],
                )
                self._json({
                    "ok": True,
                    "queued": queued,
                    "upload_attempted": False,
                    "mode": "local_queue_only",
                })
                return
            if path == "/telemetry/queue/clear":
                if payload.get("confirm") is not True:
                    raise ValueError("confirm 必须为 true 才能清空匿名反馈队列")
                removed = LocalAnonymousFeedbackQueue(DATA_DIR.parent).clear()
                self._json({
                    "ok": True,
                    "removed": removed,
                    "shop_data_removed": False,
                })
                return
            if path == "/distribution/extension-source":
                origin = str(self.headers.get("Origin") or "").strip().lower()
                origin_match = re.fullmatch(r"chrome-extension://([a-p]{32})", origin)
                result = save_extension_install_state(
                    DATA_DIR.parent,
                    payload,
                    origin_extension_id=origin_match.group(1) if origin_match else None,
                )
                self._json({"ok": True, "extension": result})
                return
            if path == "/updates/check":
                center = _update_center()
                result = center.check_for_update()
                _save_update_settings({"last_check_at": _now_label(), "last_check": result})
                self._json({"ok": True, **result, "message": "发现新的知识包" if result.get("available") else "当前已是最新知识包"})
                return
            if path == "/updates/apply":
                if str(payload.get("component") or "knowledge") != "knowledge":
                    raise ValueError("当前只支持独立更新知识包；程序和扩展必须使用签名安装包")
                result = _update_center().install()
                _save_update_settings({"last_check_at": _now_label(), "last_check": None})
                self._json({**result, "message": f"知识包 {result.get('pack_version')} 已验证并启用"})
                return
            if path == "/rules/import-local":
                pack = payload.get("pack")
                if not isinstance(pack, dict):
                    raise ValueError("pack 必须是知识包对象")
                result = _update_center().install_local(pack)
                _save_update_settings({"last_check_at": _now_label(), "last_check": None})
                self._json({**result, "message": f"行业知识包 {result.get('pack_version')} 已验证并启用"})
                return
            if path == "/updates/rollback":
                if str(payload.get("component") or "knowledge") != "knowledge":
                    raise ValueError("当前只支持知识包回滚")
                requested_version = str(payload.get("pack_version") or "").strip() or None
                result = _update_center().rollback(pack_version=requested_version)
                _save_update_settings({"last_check_at": _now_label(), "last_check": None})
                self._json({**result, "message": f"已回滚到知识包 {result.get('pack_version')}"})
                return
            if path == "/rules/evaluate":
                facts = payload.get("facts")
                if not isinstance(facts, dict):
                    raise ValueError("facts 必须是对象")
                pack = _update_center().load_effective_pack()
                result = RuleEngine(pack).evaluate(facts, payload.get("settings") or load_agent_settings())
                self._json({"ok": True, **result})
                return
            if path == "/actions/strategy/select":
                decision = save_strategy_decision(str(payload.get("policy_key") or ""))
                _invalidate_cache()
                self._json({"ok": True, "decision": decision, "execution_enabled": False})
                return
            if path == "/oauth/oceanengine/start":
                result = OceanEngineOAuth(DATA_DIR).start_authorization(
                    str(payload.get("app_id") or ""),
                    str(payload.get("app_secret") or ""),
                )
                self._json({"ok": True, **result})
                return
            if path == "/oauth/oceanengine/sync":
                account_ids = payload.get("account_ids")
                if account_ids is not None and not isinstance(account_ids, list):
                    raise ValueError("账号选择格式不正确。")
                result = OceanEngineDataClient(
                    OceanEngineOAuth(DATA_DIR)
                ).sync(
                    save_data,
                    [str(value) for value in account_ids] if account_ids else None,
                    int(payload.get("days") or 7),
                )
                selected_key = str(
                    load_agent_settings().get("qianchuan_account_key") or ""
                )
                if not selected_key:
                    active_account = next(
                        (
                            account
                            for account in result.get("accounts", [])
                            if int(account.get("advertiser_count") or 0) > 0
                        ),
                        None,
                    )
                    if active_account:
                        selected_key = str(active_account.get("account_key") or "")
                        save_agent_settings(
                            {"qianchuan_account_key": selected_key}
                        )
                result["selected_account_key"] = selected_key
                _invalidate_cache()
                self._json(result)
                return
            if path == "/integrations/settings":
                self._json({"ok": True, "integrations": save_integration_settings(payload)})
                return
            if path == "/integrations/test":
                self._json({"ok": True, "result": test_integration(str(payload.get("platform") or ""))})
                return
            if path == "/reports/generate":
                report = generate_daily_report(payload.get("date"))
                deliveries = send_report_notifications(report) if payload.get("notify") else []
                self._json({"ok": True, "report": report, "deliveries": deliveries})
                return
            if path == "/tasks/update":
                task = update_task_state(
                    str(payload.get("task_id") or ""),
                    str(payload.get("status") or ""),
                    operator=str(payload.get("operator") or ""),
                    assignee=str(payload.get("assignee") or ""),
                    note=str(payload.get("note") or ""),
                    title=str(payload.get("title") or ""),
                    owner=str(payload.get("owner") or ""),
                    store_key=str(payload.get("store_key") or "") or None,
                    business_date=str(payload.get("business_date") or "") or None,
                )
                _invalidate_cache()
                self._json({"ok": True, "task": task})
                return
            if path == "/onboarding/update":
                self._json({"ok": True, "onboarding": update_onboarding_state(str(payload.get("event") or ""))})
                return
            if path == "/tasks/track":
                self._json({"ok": True, "snapshot": save_suggestion_snapshot(str(payload.get("task_id") or ""), payload.get("context"))})
                return
            if path == "/feedback":
                self._json({"ok": True, "feedback": save_feedback(
                    str(payload.get("task_id") or ""),
                    str(payload.get("rating") or ""),
                    str(payload.get("comment") or ""),
                    str(payload.get("context") or ""),
                )})
                return
            if path == "/actions/confirm":
                action = payload.get("action")
                if not isinstance(action, dict):
                    raise ValueError("缺少有效的操作草稿。")
                confirmed = confirm_action_draft(action)
                _invalidate_cache()
                self._json({"ok": True, "action": confirmed, "executed": False, "execution_enabled": False})
                return
            if path == "/actions/cancel":
                cancelled = cancel_confirmed_action(str(payload.get("action_id") or ""))
                _invalidate_cache()
                self._json({"ok": True, "action": cancelled, "executed": False, "execution_enabled": False})
                return
            if path == "/actions/manual-applied":
                marker = mark_action_manually_applied(str(payload.get("action_id") or ""))
                _invalidate_cache()
                self._json({"ok": True, "marker": marker, "executed_by_plugin": False, "execution_enabled": False})
                return
            if path == "/actions/preflight/start":
                report = start_execution_preflight(str(payload.get("action_id") or ""))
                self._json({"ok": True, "preflight": report, "executed": False, "execution_enabled": False})
                return
            if path == "/actions/preflight/stop":
                report = stop_execution_preflight(str(payload.get("session_id") or ""))
                self._json({"ok": True, "preflight": report, "executed": False, "execution_enabled": False})
                return
            if path == "/actions/preflight/authorize":
                report = authorize_execution_preflight(
                    str(payload.get("session_id") or ""),
                    str(payload.get("confirmation_text") or ""),
                )
                self._json({"ok": True, "preflight": report, "executed": False, "execution_enabled": False})
                return
            if path == "/actions/preflight/consume":
                consumed = consume_execution_authorization(str(payload.get("authorization_id") or ""))
                self._json({"ok": True, "grant": consumed, "executed": False, "mode": "supervised_submit"})
                return
            if path == "/actions/preflight/preview":
                preview = preview_execution_authorization(str(payload.get("authorization_id") or ""))
                self._json({"ok": True, "preview": preview})
                return
            if path == "/actions/rollback/create":
                draft = create_budget_rollback_draft(str(payload.get("action_id") or ""))
                self._json({"ok": True, "action": draft})
                return
            if path == "/actions/execution/result":
                action = record_execution_result(str(payload.get("action_id") or ""), payload.get("result") or {})
                self._json({"ok": True, "action": action})
                return
            if path == "/actions/execution/verify":
                verification = verify_execution_result(str(payload.get("action_id") or ""))
                self._json({"ok": True, "verification": verification})
                return
            if path == "/scan-status":
                self._json({"ok": True, "scan": save_scan_status(payload)})
                return
            self._json({"error": "not_found"}, 404)
        except (json.JSONDecodeError, ValueError, TypeError, UpdateError, RollbackError, RulePackError) as error:
            self._json({"error": str(error)}, 400)


def main() -> None:
    database = _initialize_local_store()
    if database.get("status") != "ready":
        logger.error("SQLite 初始化失败，服务将仅使用兼容 JSON，等待用户修复: %s", database.get("error"))
    if os.environ.get("DIAN_AGENT_SELF_TEST") == "1":
        knowledge = _knowledge_status()
        if database.get("status") == "ready" and knowledge.get("status") == "ready":
            logger.info("独立 Agent 自检通过: database=%s knowledge=%s", database.get("schema_version"), knowledge.get("version"))
            return
        logger.error("独立 Agent 自检失败: database=%s knowledge=%s", database, knowledge)
        raise SystemExit(1)
    # allow_reuse_address must be set before __init__ calls server_bind()
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as error:
        logger.error(
            "端口 %d 无法使用，扩展只能连接此端口。请关闭占用该端口的程序后重试：%s",
            PORT,
            error,
        )
        sys.exit(1)
    stop_event = threading.Event()
    scheduler = threading.Thread(target=_daily_report_scheduler, args=(stop_event,), daemon=True)
    update_scheduler = threading.Thread(target=_knowledge_update_scheduler, args=(stop_event,), daemon=True)
    scheduler.start()
    update_scheduler.start()
    logger.info("店策 Agent 本地服务已启动: http://127.0.0.1:%d", PORT)
    logger.info("方案确认模式（不执行千川操作）；数据目录: %s", DATA_DIR)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("服务已停止")
    finally:
        stop_event.set()
        scheduler.join(timeout=2)
        update_scheduler.join(timeout=2)
        server.server_close()


if __name__ == "__main__":
    main()
