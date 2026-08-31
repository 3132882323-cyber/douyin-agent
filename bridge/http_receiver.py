"""店策 Agent 本地 companion service。

接收 Chrome 扩展提交的脱敏页面快照，按平台和页面类型原子保存，
并提供健康状态、数据目录和确定性经营诊断。仅监听 127.0.0.1。
"""

from __future__ import annotations

import copy
import json
import hashlib
import hmac
import logging
import math
import os
import platform
import re
import secrets
import sys
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from version import AGENT_VERSION
from local_api_auth import (
    AUTH_HEADER as LOCAL_AUTH_HEADER,
    INTERNAL_CLIENT_SUBJECT,
    LocalApiAuthError,
    ensure_install_auth,
    issue_session_token,
    provision_local_api_trust,
    repair_local_api_trust,
    validate_session_token,
)


def _run_local_api_trust_provisioner(arguments: list[str]) -> int:
    """Run an installer/repair trust command without starting the HTTP Agent."""

    if len(arguments) != 3 or arguments[0] not in {
        "--initialize-local-api-trust",
        "--repair-local-api-trust",
    }:
        print(
            "usage: DianAgent (--initialize-local-api-trust|--repair-local-api-trust) "
            "<manifest.json> <install-root>",
            file=sys.stderr,
        )
        return 2
    try:
        if arguments[0] == "--repair-local-api-trust":
            result = repair_local_api_trust(arguments[2], arguments[1])
        else:
            result = provision_local_api_trust(arguments[2], arguments[1])
    except (LocalApiAuthError, OSError) as error:
        print(f"Local API trust command failed [{getattr(error, 'code', 'io_error')}]: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **result, "agent_version": AGENT_VERSION}, ensure_ascii=False, sort_keys=True))
    return 0


# Native macOS packages use their bundled executable for provisioning, so end
# users do not need Python. Dispatch before application imports, logging,
# data-directory creation or server startup; this mode never listens on a port.
if __name__ == "__main__" and sys.argv[1:2] in (
    ["--initialize-local-api-trust"],
    ["--repair-local-api-trust"],
):
    raise SystemExit(_run_local_api_trust_provisioner(sys.argv[1:]))


from ai_context import build_ai_context_pack, validate_ai_context_pack
from ai_decision import DECISION_PROPOSAL_SCHEMA
from ai_gateway import AIGateway
from ai_provider import AIProviderError, resolve_provider_id
from activation_status import build_activation_status
from action_protocol import MAX_CAPTURE_FUTURE_SKEW_MS, assess_automation_readiness, build_action_draft as _build_action_draft_protocol, transition_action, validate_action_draft
from build_flavor import build_edition_status
from chengfang_demo import build_a2_demo_fixture
try:
    from chengfang_evidence import hydrate_chengfang_profile
    from chengfang_autopilot_runtime import ChengfangAutopilotRuntime, EVALUATION_INTERVAL_SECONDS
    from chengfang_official_adapter import OfficialChengfangBudgetAdapter
    from chengfang_production_controller import (
        ChengfangProductionController,
        ChengfangProductionControllerError,
    )
    from chengfang_production_targets import (
        ChengfangProductionTargetError,
        ChengfangProductionTargetService,
    )
    COMMERCIAL_RUNTIME_LOADED = True
except ModuleNotFoundError as error:
    if error.name not in {
        "chengfang_evidence",
        "chengfang_autopilot_runtime",
        "chengfang_official_adapter",
        "chengfang_production_controller",
        "chengfang_production_targets",
    }:
        raise
    from chengfang_public_fallback import (
        ChengfangAutopilotRuntime,
        EVALUATION_INTERVAL_SECONDS,
        hydrate_chengfang_profile,
    )
    COMMERCIAL_RUNTIME_LOADED = False

    class ChengfangProductionControllerError(ValueError):
        pass

    class ChengfangProductionTargetError(ValueError):
        pass

    ChengfangProductionController = None
    ChengfangProductionTargetService = None
    OfficialChengfangBudgetAdapter = None
from chengfang_control_tasks import ChengfangControlTaskCenter
from chengfang_schedule_control import ChengfangScheduleControl
from deployment_mode import blocked_browser_capability, request_origin_allowed, resolve_deployment_policy
from douyin_commerce_graph import build_product_operating_graph, extract_commerce_observations
from marketplace_readiness import build_marketplace_readiness
from integration_secret_store import IntegrationSecretStore, IntegrationSecretStoreError
from install_verification import complete_late_install_verification
from local_store import LocalStore, LocalStoreError, SCHEMA_VERSION as DATABASE_SCHEMA_VERSION
from offline_upgrade import PRODUCTION_OFFLINE_PUBLIC_KEYS
from oceanengine_account_center import OceanEngineAccountCenter
from oceanengine_data import OceanEngineDataClient, load_sync_status
from oceanengine_oauth import OceanEngineOAuth
from operator_memory import archive_operator_memory, list_operator_memory, upsert_operator_memory
from platform_paths import default_data_dir, default_log_dir, platform_id
from plan_collection_coverage import build_plan_collection_receipt, build_scoped_plan_collection_receipts
from promotion_mode import build_chengfang_dashboard_summary, build_chengfang_readiness, build_promotion_context, legacy_execution_guard
from product_capability import build_product_capability_diagnostic
from chengfang_contract import assess_page_fingerprint, build_chengfang_contract_registry
from promotion_readiness import (
    LocalAnonymousFeedbackQueue,
    build_distribution_status,
    build_release_readiness,
    extension_origin_trusted,
    extension_pairing_allowed,
    save_extension_install_state,
)
from rule_engine import RuleEngine, RulePackError
from update_center import RollbackError, UpdateCenter, UpdateError


logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(message)s")
logger = logging.getLogger("dian-agent-http")

BASE_DIR = Path(__file__).resolve().parent
_default_data_dir = default_data_dir() if getattr(sys, "frozen", False) else BASE_DIR / "data"
DATA_DIR = Path(os.environ.get("DIAN_AGENT_DATA_DIR", _default_data_dir))
DATA_DIR.mkdir(parents=True, exist_ok=True)
if getattr(sys, "frozen", False) or os.environ.get("DIAN_AGENT_LOG_DIR"):
    _log_dir = Path(os.environ.get("DIAN_AGENT_LOG_DIR", default_log_dir()))
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
MAX_WEBHOOK_RESPONSE_BYTES = 64 * 1024
ALLOWED_SOURCES = {"doudian", "qianchuan"}
SAFE_KEY = re.compile(r"^[a-z0-9_-]{1,48}$")
CURRENT_PAGE_STORE_EVIDENCE_SOURCES = frozenset({
    "url_parameter", "data_attribute", "overview_visible_text", "bootstrap_sec_shop_id",
})
SCAN_CORE_DOUDIAN_PAGE_IDS = ("overview", "orders", "products", "shelf")
SCAN_FULL_DOUDIAN_PAGE_IDS = (
    "overview", "orders", "products", "inventory", "refunds", "reviews", "shelf",
    "live", "short_video", "image_text", "search", "recommend_card", "funds",
)
SCAN_ADS_OPTIONAL_PAGE_IDS = (
    "qianchuan_video_library", "qianchuan_overview", "qianchuan_campaigns",
    "qianchuan_live", "qianchuan_live_dashboard",
)
STALE_SECONDS = 10 * 60
PLAN_CONSOLE_STALE_SECONDS = 30 * 60
CREATIVE_ANALYSIS_STALE_SECONDS = 24 * 60 * 60
SHELF_ANALYSIS_STALE_SECONDS = 30 * 60
LIVE_ANALYSIS_STALE_SECONDS = 30 * 60
INVENTORY_ANALYSIS_STALE_SECONDS = 24 * 60 * 60
REPORT_TEMPLATE_KEYS = {"default", "brief", "handover", "custom"}
REPORT_DELIVERY_SCHEMA_VERSION = 1
REPORT_GUARD_SCHEMA_VERSION = 1
INTEGRATION_METADATA_SCHEMA_VERSION = 2
INTEGRATION_SECRET_SCHEMA_VERSION = 1
REPORT_DELIVERY_RETRY_SECONDS = (60, 5 * 60, 15 * 60, 60 * 60, 6 * 60 * 60)
EXECUTION_ORIGINAL_VALUE_CONFIRMATIONS_REQUIRED = 2
EXECUTION_ORIGINAL_VALUE_MIN_CONFIRMATION_AGE_MS = 25_000
EXECUTION_FINAL_REREAD_MAX_CAPTURE_AGE_MS = 15_000
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
    "binding_registry_initialized": False,
}

# Thread-safe state mutation lock (prevents concurrent read-modify-write races)
_state_lock = threading.RLock()
_identity_secret_lock = threading.Lock()
_integration_secret_lock = threading.RLock()
_report_delivery_lock = threading.RLock()
_scan_push_lock = threading.Lock()
_current_page_store_grant_lock = threading.Lock()
_current_page_store_grants: dict[str, dict[str, int]] = {}
_auto_store_context_transaction_local = threading.local()
_ai_gateway_lock = threading.RLock()
_ai_gateway_cache: tuple[Path, AIGateway] | None = None
_ai_context_cache: dict[str, tuple[Path, dict[str, Any]]] = {}
_AI_CONTEXT_CACHE_LIMIT = 16
_ai_network_lock = threading.Lock()
_ai_rate_lock = threading.Lock()
_ai_call_history: dict[tuple[str, str, str], list[float]] = {}
_post_idempotency_lock = threading.RLock()
_post_idempotency_cache: dict[str, dict[str, Any]] = {}
_binding_execution_process_lock = threading.RLock()
_binding_execution_local = threading.local()
MAX_STORE_ACCOUNT_BINDINGS = 200
CURRENT_PAGE_STORE_GRANT_TTL_MS = 60_000
CURRENT_PAGE_STORE_GRANT_LIMIT = 32
CURRENT_PAGE_CAPTURE_FUTURE_TOLERANCE_MS = 1_000
EXTENSION_ID_HEADER = "X-Dian-Agent-Extension-Id"
CHROMIUM_EXTENSION_ID_PATTERN = re.compile(r"^[a-p]{32}$")


@contextmanager
def _binding_execution_lease(
    *,
    hold_state_lock: bool,
    validator: Callable[[], None] | None = None,
):
    """Cross-process lease shared by binding mutations and production I/O.

    Every outer entry uses the same order: local state lock, process lock, then
    the one-byte OS file lock.  Production I/O deliberately retains all three
    locks for GET/GET/POST/GET.  This makes the rare explicit write a short
    serial transaction, but prevents a scanner that already owns the state lock
    from deadlocking against a writer that later needs a state-bound target
    resolution.  Nested calls in the same thread reuse the outer file lease.
    """

    depth = int(getattr(_binding_execution_local, "depth", 0) or 0)
    if depth > 0:
        if hold_state_lock:
            with _state_lock:
                if validator is not None:
                    validator()
                yield
        else:
            with _state_lock:
                if validator is not None:
                    validator()
            yield
        return

    lock_path = DATA_DIR / "chengfang_production" / "binding-execution.lock"
    parent = lock_path.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise OSError("production binding lease path is unsafe")
    parent.mkdir(parents=True, exist_ok=True)
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise OSError("production binding lease path is unsafe")

    state_locked = False
    file_locked = False
    handle = None
    _state_lock.acquire()
    state_locked = True
    try:
        with _binding_execution_process_lock:
            try:
                handle = open(lock_path, "a+b")
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                file_locked = True
                # Initialize only after owning byte zero.  Windows permits a
                # one-byte lock beyond EOF, which removes the cold-start race
                # where two processes both observed an empty new lock file.
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.seek(0)
                    handle.write(b"0")
                    handle.flush()
                _binding_execution_local.depth = 1
                if validator is not None:
                    validator()
                if not hold_state_lock:
                    _state_lock.release()
                    state_locked = False
                yield
            finally:
                _binding_execution_local.depth = 0
                if handle is not None:
                    try:
                        if file_locked:
                            handle.seek(0)
                            if os.name == "nt":
                                import msvcrt

                                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                            else:
                                import fcntl

                                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        handle.close()
    finally:
        if state_locked:
            _state_lock.release()


def _guard_binding_state_mutation(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        with _binding_execution_lease(hold_state_lock=True):
            return function(*args, **kwargs)

    return guarded

# Health remains public.  The only protected endpoint retained for legacy
# originless maintenance clients is a deliberately redacted system summary.
# Raw settings, snapshots, reports and account data always require a session.
ORIGINLESS_MAINTENANCE_GET_PATHS = frozenset({"/system/status"})

# These routes can spend money, send a message, rotate authorization state or
# consume a one-shot local grant.  Identical authenticated retries are
# coalesced for a short window without changing the extension protocol.
IDEMPOTENT_POST_PATHS = frozenset({
    "/ai/providers/test",
    "/ai/shadow/run",
    "/chengfang/a2-pilot/candidate/execute",
    "/chengfang/production-write/execute",
    "/chengfang/production-write/reconcile",
    "/chengfang/production-write/stop",
    "/chengfang/production-write/resume",
    "/chengfang/control-tasks/simulate",
    "/chengfang/schedule-control/simulate",
    "/integrations/test",
    "/oauth/oceanengine/account-center/sync",
    "/oauth/oceanengine/start",
    "/oauth/oceanengine/sync",
    "/reports/generate",
    "/updates/apply",
    "/updates/rollback",
    "/actions/preflight/consume",
    "/actions/preflight/manual-reconcile/archive",
    "/actions/execution/result",
})
POST_IDEMPOTENCY_TTL_SECONDS = 30.0
POST_IDEMPOTENCY_INFLIGHT_SECONDS = 120.0
POST_IDEMPOTENCY_CACHE_LIMIT = 64

# TTL cache for expensive analysis results (5-second window)
_analysis_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL_SECONDS = 5


def _activation_development_source_root() -> Path | None:
    """Allow repo-manifest fallback only in the exact default source layout."""

    if getattr(sys, "frozen", False):
        return None
    if any(
        str(os.environ.get(name) or "").strip()
        for name in ("DIAN_AGENT_DATA_DIR", "DIAN_AGENT_INSTALL_ROOT")
    ):
        return None
    expected_data_dir = BASE_DIR / "data"
    try:
        if DATA_DIR.is_symlink() or expected_data_dir.is_symlink():
            return None
        if DATA_DIR.resolve(strict=True) != expected_data_dir.resolve(strict=True):
            return None
        source_root = BASE_DIR.parent
        if source_root.is_symlink() or not source_root.is_dir():
            return None
        return source_root
    except (OSError, RuntimeError):
        return None


def _current_extension_activation_status(reported_version: str = "") -> dict[str, Any]:
    """Resolve one canonical activation verdict for probes and authentication."""

    return build_activation_status(
        DATA_DIR.parent,
        agent_version=AGENT_VERSION,
        reported_extension_version=reported_version,
        development_source_root=_activation_development_source_root(),
    )


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


def _bounded_integer(
    value: Any,
    *,
    field_name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Parse one external integer without accepting booleans or truncating floats."""

    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer from {minimum} to {maximum}.")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        parsed = int(value.strip())
    else:
        raise ValueError(f"{field_name} must be an integer from {minimum} to {maximum}.")
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}.")
    return parsed


def _bounded_setting_integer(
    value: Any,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    """Validate a persisted integer setting without implicit defaults."""

    if value is None or value == "":
        raise ValueError(f"{field_name} must be an integer from {minimum} to {maximum}.")
    return _bounded_integer(
        value,
        field_name=field_name,
        default=minimum,
        minimum=minimum,
        maximum=maximum,
    )


def _bounded_setting_number(
    value: Any,
    *,
    field_name: str,
    minimum: float,
    maximum: float,
) -> float:
    """Validate one finite numeric setting without accepting booleans."""

    if isinstance(value, bool) or value is None or value == "":
        raise ValueError(f"{field_name} must be a number from {minimum:g} to {maximum:g}.")
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str) and re.fullmatch(
        r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?",
        value.strip(),
    ):
        parsed = float(value.strip())
    else:
        raise ValueError(f"{field_name} must be a number from {minimum:g} to {maximum:g}.")
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise ValueError(f"{field_name} must be between {minimum:g} and {maximum:g}.")
    return parsed


def _reject_nonfinite_json_constant(value: str) -> None:
    """Reject Python's non-standard NaN/Infinity JSON extensions at ingress."""

    raise ValueError(f"JSON number {value} is not finite.")


def _parse_finite_json_float(value: str) -> float:
    """Reject syntactically valid exponents that overflow to infinity."""

    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"JSON number {value} is not finite.")
    return parsed


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
            json.dump(payload, file, ensure_ascii=False, indent=2, allow_nan=False)
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
    """Initialize SQLite and adopt legacy JSON exactly once."""
    store = _local_store()
    try:
        store.initialize()
        legacy_dirs = [DATA_DIR / source for source in sorted(ALLOWED_SOURCES)]
        legacy_dirs.append(DATA_DIR / "qianchuan_accounts")
        legacy_dirs.append(DATA_DIR / "stores")
        legacy_files = [DATA_DIR / f"{source}.json" for source in sorted(ALLOWED_SOURCES)]
        existing = [path for path in [*legacy_dirs, *legacy_files] if path.exists()]
        imported = store.migrate_legacy_json_snapshots(existing)
        migration = store.legacy_json_migration_status()
        return {
            "status": "ready",
            "status_label": "本地数据库正常",
            "schema_version": store.get_schema_version(),
            "mirrored_snapshots": len(imported),
            "authoritative_source": "sqlite",
            "json_compatibility": migration["json_compatibility"],
            "legacy_migration": migration,
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


def build_storage_lifecycle_status() -> dict[str, Any]:
    """Return the explicit, read-only history-growth preview.

    This intentionally stays out of liveness and the routinely polled system
    summary because semantic duplicate analysis reads recent payload history.
    """

    try:
        return _local_store().snapshot_lifecycle_preview()
    except (LocalStoreError, OSError, ValueError) as error:
        logger.warning("Cannot build snapshot lifecycle preview: %s", error)
        return {
            "contract_version": 1,
            "status": "error",
            "mode": "preview_only",
            "mutates_data": False,
            "automatic_cleanup_enabled": False,
            "error_code": "snapshot_lifecycle_preview_failed",
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
        store_key = str(load_agent_settings().get("store_key") or "").lower()
        pack = center.load_effective_pack(store_key=store_key)
        catalog = center.knowledge_catalog(store_key=store_key)
        metadata = pack.get("metadata") if isinstance(pack.get("metadata"), dict) else {}
        rollback_candidates = center.rollback_candidates()
        trust_state = center.local_import_trust_status()
        selected = next((item for item in catalog.get("packs", []) if item.get("selected")), {})
        return {
            "status": "ready",
            "version": str(pack.get("pack_version") or "builtin"),
            "channel": str(pack.get("channel") or "stable"),
            "industry": str(pack.get("industry") or metadata.get("industry") or "general")[:40],
            "industry_label": str(selected.get("industry_label") or "通用电商")[:40],
            "pack_id": str(catalog.get("active_pack_id") or "general")[:64],
            "display_name": str(selected.get("display_name") or "通用电商经营知识包")[:80],
            "expires_at": selected.get("expires_at") or pack.get("expires_at"),
            "rule_count": len(pack.get("rules") or []),
            "base_rule_count": int((catalog.get("layers") or [{}])[0].get("rule_count") or 0),
            "layers": catalog.get("layers") or [],
            "fallback_reason": catalog.get("fallback_reason") or "",
            "store_key": store_key,
            "binding": catalog.get("binding"),
            "installed_count": int(catalog.get("installed_count") or 0),
            "available_count": int(catalog.get("available_count") or 0),
            "invalid_count": int(catalog.get("invalid_count") or 0),
            "packs": catalog.get("packs") or [],
            "rollback_available": any(item.get("usable") for item in rollback_candidates),
            "rollback_candidates": rollback_candidates,
            "source": str(selected.get("source") or "builtin"),
            "local_import_supported": True,
            "local_import_requires_ed25519": True,
            "local_import_trust_configured": trust_state["ready"],
            "local_import_trust_state": trust_state,
        }
    except (UpdateError, ValueError, OSError) as error:
        return {
            "status": "error",
            "version": "",
            "rule_count": 0,
            "packs": [],
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


def _record_managed_runtime_start() -> None:
    """Record starts made by an owned background service such as launchd."""

    source = str(os.environ.get("DIAN_AGENT_AUTOSTART_SOURCE") or "").strip()
    if not source:
        return
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    _atomic_json_write(
        _runtime_startup_state_path(),
        {
            "state": "healthy",
            "state_label": "自动启动与保活正常",
            "autostart_enabled": True,
            "keepalive_enabled": True,
            "hidden_launcher": True,
            "last_checked_at": now,
            "last_healthy_at": now,
            "last_recovery_at": now,
            "last_error": None,
            "source": source[:80],
        },
    )


def build_system_status() -> dict[str, Any]:
    settings = _load_update_settings()
    agent_settings = load_agent_settings()
    execution_mode = str(agent_settings.get("execution_mode") or "observe")
    if execution_mode not in {"observe", "shadow", "supervised"}:
        execution_mode = "observe"
    deployment_policy = resolve_deployment_policy()
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
    source_snapshot_counts = {
        source: sum(1 for item in catalog if item["source"] == source)
        for source in sorted(ALLOWED_SOURCES)
    }
    edition = build_edition_status()
    edition["commercial_runtime_loaded"] = COMMERCIAL_RUNTIME_LOADED
    if COMMERCIAL_RUNTIME_LOADED and edition["build_flavor"] == "public_community":
        # Editable developer checkouts may load ignored internal modules.  They
        # must not present themselves as either distributable build edition.
        edition = {
            **edition,
            "edition": "developer",
            "build_flavor": "developer_checkout",
            "commercial_modules_included": True,
            "redistributable": False,
        }
    return {
        "ready": product_operational,
        "product_operational": product_operational,
        "public_distribution_ready": public_distribution_ready,
        "agent_version": AGENT_VERSION,
        "edition": edition,
        "platform": platform_id(),
        "architecture": platform.machine().lower() or "unknown",
        "required_extension_version": AGENT_VERSION,
        "bridge_protocol_version": 2,
        "ai_required": False,
        "mode": "local_first",
        "program_update_mode": "offline_bundle" if sys.platform == "win32" else "manual_reinstall",
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
        "execution": {
            "mode": execution_mode,
            "mode_label": {
                "observe": "观察模式",
                "shadow": "影子模式",
                "supervised": "受控执行",
            }[execution_mode],
            "enabled": execution_mode == "supervised" and deployment_policy.browser_dom_execution,
        },
        "storage": {
            "snapshot_count": len(catalog),
            "lifecycle": {
                "available": database.get("status") == "ready",
                "endpoint": "/storage/lifecycle",
                "mode": "preview_only",
                "automatic_cleanup_enabled": False,
            },
            "sources": {
                source: {
                    "has_data": count > 0,
                    "pages": count,
                }
                for source, count in source_snapshot_counts.items()
            },
            "schema_warnings": _schema_version_check(),
            "disk": _disk_usage_check(),
        },
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


def _binding_revocations_path() -> Path:
    return DATA_DIR / "store_account_binding_revocations.json"


def _binding_transaction_path() -> Path:
    return DATA_DIR / "store_account_binding_transaction.json"


def _auto_store_context_transaction_path() -> Path:
    return DATA_DIR / "auto_store_context_transaction.json"


def _clear_auto_store_context_transaction() -> None:
    try:
        _auto_store_context_transaction_path().unlink(missing_ok=True)
    except OSError:
        logger.warning("Unable to remove completed automatic store-context transaction journal.")


def _restore_auto_store_context_files(
    previous_settings: dict[str, Any],
    previous_onboarding: dict[str, Any],
    *,
    settings_existed: bool,
    onboarding_existed: bool,
) -> None:
    if settings_existed:
        _atomic_json_write(_settings_path(), previous_settings)
    else:
        _settings_path().unlink(missing_ok=True)
    if onboarding_existed:
        _atomic_json_write(_onboarding_state_path(), previous_onboarding)
    else:
        _onboarding_state_path().unlink(missing_ok=True)


@_guard_binding_state_mutation
def _recover_auto_store_context_transaction() -> None:
    """Rollback an interrupted auto-selection before exposing either state file."""

    if getattr(_auto_store_context_transaction_local, "active", False):
        return
    path = _auto_store_context_transaction_path()
    if not path.exists():
        return
    with _state_lock:
        if not path.exists():
            return
        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise OSError("automatic store-context transaction journal is unreadable") from error
        previous = journal.get("previous") if isinstance(journal, dict) else None
        if (
            not isinstance(journal, dict)
            or journal.get("schema_version") != 1
            or journal.get("phase") not in {"prepared", "committed"}
            or not isinstance(previous, dict)
            or not isinstance(previous.get("settings"), dict)
            or not isinstance(previous.get("onboarding"), dict)
            or type(previous.get("settings_existed")) is not bool
            or type(previous.get("onboarding_existed")) is not bool
        ):
            raise OSError("automatic store-context transaction journal is invalid")
        if journal.get("phase") == "prepared":
            _restore_auto_store_context_files(
                previous["settings"],
                previous["onboarding"],
                settings_existed=previous["settings_existed"],
                onboarding_existed=previous["onboarding_existed"],
            )
        _clear_auto_store_context_transaction()


def _clear_binding_transaction() -> None:
    try:
        _binding_transaction_path().unlink(missing_ok=True)
    except OSError:
        logger.warning("Unable to remove completed store/account binding transaction journal.")


@_guard_binding_state_mutation
def _recover_binding_transaction() -> None:
    """Recover an interrupted multi-file link/unlink transition fail-closed."""

    path = _binding_transaction_path()
    if not path.exists():
        return
    with _state_lock:
        if not path.exists():
            return
        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise OSError("store/account binding transaction journal is unreadable") from error
        if not isinstance(journal, dict) or journal.get("schema_version") != 1:
            raise OSError("store/account binding transaction journal is invalid")
        operation = str(journal.get("operation") or "")
        phase = str(journal.get("phase") or "prepared")
        previous = journal.get("previous")
        next_state = journal.get("next")
        guard_revocations = journal.get("guard_revocations")
        if (
            operation not in {"link", "unlink", "mirror"}
            or phase not in {"prepared", "committed"}
            or not isinstance(previous, dict)
            or not isinstance(next_state, dict)
            or not all(
                isinstance(candidate.get(key), dict)
                for candidate in (previous, next_state)
                for key in ("stores", "accounts", "settings", "revocations")
            )
        ):
            raise OSError("store/account binding transaction journal payload is invalid")

        # A requested unlink always rolls forward: permission revocation wins
        # over availability. A prepared link or browser-evidence mirror rolls
        # back; a committed operation rolls forward. Both choices avoid
        # exposing a relation with only one side of the catalog written.
        roll_forward = operation == "unlink" or phase == "committed"
        target = next_state if roll_forward else previous
        if roll_forward and operation == "link" and isinstance(guard_revocations, dict):
            _atomic_json_write(_binding_revocations_path(), guard_revocations)
        else:
            _atomic_json_write(_binding_revocations_path(), target["revocations"])
        _atomic_json_write(_store_catalog_path(), target["stores"])
        _atomic_json_write(_account_catalog_path(), target["accounts"])
        if roll_forward and operation == "link":
            _atomic_json_write(_binding_revocations_path(), target["revocations"])
        _atomic_json_write(_settings_path(), target["settings"])
        _clear_binding_transaction()


def _load_binding_revocations() -> dict[str, dict[str, Any]]:
    path = _binding_revocations_path()
    if not path.exists():
        settings_path = DATA_DIR / "settings.json"
        if settings_path.exists():
            try:
                settings_value = json.loads(settings_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise OSError("settings are unreadable while checking binding registry state") from error
            if isinstance(settings_value, dict) and settings_value.get("binding_registry_initialized") is True:
                raise OSError("initialized store/account binding registry is missing")
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Unable to read store/account binding revocations: %s", path)
        # Corrupt revocation state is a permissions-boundary failure.  Treat it
        # as unreadable rather than silently allowing browser evidence to bind.
        raise OSError("store/account binding revocations are unreadable")
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("revocations"), list)
    ):
        raise OSError("store/account binding revocations have an invalid schema")
    entries = value["revocations"]
    result: dict[str, dict[str, Any]] = {}
    for item in entries:
        if not isinstance(item, dict):
            raise OSError("store/account binding revocation entry is invalid")
        store_key = str(item.get("store_key") or "").strip().lower()
        account_key = str(item.get("account_key") or "").strip().lower()
        generation = item.get("generation")
        linked_at_ms = item.get("linked_at_ms")
        revoked_at_ms = item.get("revoked_at_ms")
        reason = item.get("reason")
        relation_key = f"{store_key}:{account_key}"
        if (
            not SAFE_KEY.fullmatch(store_key)
            or not SAFE_KEY.fullmatch(account_key)
            or relation_key in result
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or not 0 <= generation <= 2_147_483_647
            or type(item.get("active")) is not bool
            or isinstance(linked_at_ms, bool)
            or not isinstance(linked_at_ms, int)
            or not 0 <= linked_at_ms <= 9_223_372_036_854_775_807
            or isinstance(revoked_at_ms, bool)
            or not isinstance(revoked_at_ms, int)
            or not 0 <= revoked_at_ms <= 9_223_372_036_854_775_807
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 80
        ):
            raise OSError("store/account binding revocation entry failed validation")
        result[relation_key] = {
            "store_key": store_key,
            "account_key": account_key,
            "generation": generation,
            "active": item["active"],
            "linked_at_ms": linked_at_ms,
            "revoked_at_ms": revoked_at_ms,
            "reason": reason,
        }
    return result


def _binding_revocations_payload(entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "updated_at": _now_label(),
        "revocations": sorted(
            entries.values(),
            key=lambda item: max(
                int(item.get("revoked_at_ms") or 0),
                int(item.get("linked_at_ms") or 0),
            ),
            reverse=True,
        ),
    }


def _save_binding_revocations(entries: dict[str, dict[str, Any]]) -> None:
    _atomic_json_write(_binding_revocations_path(), _binding_revocations_payload(entries))


def _binding_is_revoked(store_key: str, account_key: str) -> bool:
    store_key = str(store_key or "").strip().lower()
    account_key = str(account_key or "").strip().lower()
    if not SAFE_KEY.fullmatch(store_key) or not SAFE_KEY.fullmatch(account_key):
        return True
    state = _load_binding_revocations().get(f"{store_key}:{account_key}")
    return isinstance(state, dict) and state.get("active") is not True


def _binding_generation(store_key: str, account_key: str) -> int:
    """Return the monotonic explicit-binding generation (legacy/auto is zero)."""

    store_key = str(store_key or "").strip().lower()
    account_key = str(account_key or "").strip().lower()
    if not SAFE_KEY.fullmatch(store_key) or not SAFE_KEY.fullmatch(account_key):
        return -1
    state = _load_binding_revocations().get(f"{store_key}:{account_key}")
    if not isinstance(state, dict):
        return 0
    if state.get("active") is not True:
        return -1
    return max(0, int(state.get("generation") or 0))


def _guard_production_scope_change(reason: str):
    """Serialize store/account mutations with the private production sink.

    The controller's cross-process audit lock is also held by ``execute`` for
    the complete official write attempt.  Taking that lock before the existing
    local state transaction gives one global order: production audit first,
    then account catalogs.  Pending grants are revoked only after a successful
    scope transaction; executing or unknown writes block the mutation.
    """

    def decorate(function):
        @wraps(function)
        def guarded(*args, **kwargs):
            if not COMMERCIAL_RUNTIME_LOADED or ChengfangProductionController is None:
                return function(*args, **kwargs)
            try:
                with ChengfangProductionController.guard_scope_change(
                    DATA_DIR,
                    reason=reason,
                    integrity_key=_identity_secret(),
                ):
                    return function(*args, **kwargs)
            except ChengfangProductionControllerError as error:
                raise ValueError(error.code) from None

        return guarded

    return decorate


def _qianchuan_binding_scope_from_catalogs(
    store_key: str,
    account_key: str,
    stores: dict[str, dict[str, Any]],
    accounts: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    *,
    registry_initialized: bool,
) -> dict[str, Any] | None:
    """Resolve one binding lease from an already-consistent state snapshot."""

    store_key = str(store_key or "").strip().lower()
    account_key = str(account_key or "").strip().lower()
    if not SAFE_KEY.fullmatch(store_key) or not SAFE_KEY.fullmatch(account_key):
        return None
    store = stores.get(store_key)
    account = accounts.get(account_key)
    if not isinstance(store, dict) or not isinstance(account, dict):
        return None
    store_accounts = {
        str(value or "").strip().lower()
        for value in (store.get("account_keys") or [])
    }
    if account_key not in store_accounts or str(account.get("store_key") or "").strip().lower() != store_key:
        return None
    relation = registry.get(f"{store_key}:{account_key}")
    if isinstance(relation, dict):
        if relation.get("active") is not True:
            return None
        generation = int(relation.get("generation") or 0)
        linked_at_ms = int(relation.get("linked_at_ms") or 0)
        if registry_initialized and (generation <= 0 or linked_at_ms <= 0):
            return None
    elif registry_initialized:
        return None
    else:
        generation = 0
        linked_at_ms = 0
    return {
        "store_key": store_key,
        "account_key": account_key,
        "binding_generation": generation,
        "linked_at_ms": linked_at_ms,
    }


def _active_qianchuan_binding_scope(store_key: str, account_key: str) -> dict[str, Any] | None:
    """Resolve the exact current store/account lease used by current-data reads.

    Account identity alone is not a sufficient tenant boundary: the same
    advertiser account can be explicitly unlinked and later bound to another
    store.  The catalog pair and monotonic binding generation must therefore
    agree before an account snapshot can participate in current decisions.
    """

    stores = {str(item.get("key") or "").lower(): item for item in list_store_identities()}
    accounts = {str(item.get("key") or "").lower(): item for item in list_qianchuan_accounts()}
    registry = _load_binding_revocations()
    initialized = load_agent_settings().get("binding_registry_initialized") is True
    return _qianchuan_binding_scope_from_catalogs(
        store_key,
        account_key,
        stores,
        accounts,
        registry,
        registry_initialized=initialized,
    )


def _snapshot_is_forensic_only(payload: Any) -> bool:
    """Return whether a snapshot is evidence only and must never become current."""

    if not isinstance(payload, dict):
        return True
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    quarantine = data.get("quarantine") if isinstance(data.get("quarantine"), dict) else {}
    identity_resolution = str(data.get("identity_resolution") or "").strip().lower()
    page_type = str(payload.get("page_type") or data.get("page_type") or "").strip().lower()
    return bool(
        quarantine.get("active") is True
        or quarantine.get("excluded_from_current_data") is True
        or identity_resolution in {"conflict", "binding_revoked", "binding_epoch_stale"}
        or page_type.startswith("quarantine_")
    )


def _snapshot_matches_qianchuan_binding(payload: Any, scope: dict[str, Any] | None) -> bool:
    """Return whether a non-forensic snapshot belongs to the exact active lease."""

    if (
        not isinstance(payload, dict)
        or not isinstance(scope, dict)
        or _snapshot_is_forensic_only(payload)
    ):
        return False
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    snapshot_store = data.get("store") if isinstance(data.get("store"), dict) else {}
    snapshot_account = data.get("account") if isinstance(data.get("account"), dict) else {}
    if (
        str(snapshot_store.get("key") or "").strip().lower() != scope["store_key"]
        or str(snapshot_account.get("key") or "").strip().lower() != scope["account_key"]
    ):
        return False
    binding_scope = payload.get("binding_scope")
    current_generation = int(scope.get("binding_generation") or 0)
    if not isinstance(binding_scope, dict):
        # Generation-less snapshots are compatible only with installations
        # that have never initialized the explicit binding registry.
        return current_generation == 0
    generation = binding_scope.get("binding_generation")
    linked_at_ms = binding_scope.get("linked_at_ms")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or isinstance(linked_at_ms, bool)
        or not isinstance(linked_at_ms, int)
    ):
        return False
    return bool(
        str(binding_scope.get("store_key") or "").strip().lower() == scope["store_key"]
        and str(binding_scope.get("account_key") or "").strip().lower() == scope["account_key"]
        and generation == current_generation
        and linked_at_ms == int(scope.get("linked_at_ms") or 0)
    )


def _store_snapshot_path(store_key: str, source: str, page_type: str) -> Path:
    return DATA_DIR / "stores" / store_key / source / f"{page_type}.json"


def _quarantine_snapshot_path(source: str, page_type: str, captured_at_ms: int, payload: dict[str, Any]) -> Path:
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return DATA_DIR / "quarantine" / source / page_type / f"{captured_at_ms}-{digest}.json"


def _identity_secret_path() -> Path:
    config_root = DATA_DIR.parent / "config" if getattr(sys, "frozen", False) else DATA_DIR / "config"
    return config_root / "identity-secret-v1.bin"


def _identity_secret() -> bytes:
    path = _identity_secret_path()
    with _identity_secret_lock:
        # Re-read only after taking the process lock.  The HTTP server is
        # threaded and several first snapshots can resolve identity at once.
        if path.is_symlink():
            raise OSError("identity secret must not be a symbolic link")
        if path.exists():
            if not path.is_file():
                raise OSError("identity secret must be a regular file")
            secret = path.read_bytes()
            if len(secret) != 32:
                # Rotating an invalid record would silently orphan every HMAC
                # store/account key.  Stop and require explicit repair.
                raise OSError("identity secret is invalid; repair is required")
            return secret

        path.parent.mkdir(parents=True, exist_ok=True)
        secret = os.urandom(32)
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(secret)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temp_name, 0o600)
            try:
                # Link a fully flushed record into place without replacing an
                # identity another Agent process may have installed first.
                # A same-directory hard link is atomic on supported local
                # filesystems and avoids exposing a partially written secret.
                os.link(temp_name, path)
            except FileExistsError:
                if path.is_symlink():
                    raise OSError("identity secret must not be a symbolic link")
                if not path.is_file():
                    raise OSError("identity secret must be a regular file")
                committed = path.read_bytes()
                if len(committed) != 32:
                    raise OSError("identity secret is invalid; repair is required")
                return committed
            os.chmod(path, 0o600)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        # Read back the committed value rather than trusting a temporary
        # buffer; this also catches unexpected filesystem replacement.
        committed = path.read_bytes()
        if len(committed) != 32 or not hmac.compare_digest(secret, committed):
            raise OSError("identity secret commit verification failed")
        return committed


def _local_identity_key(kind: str, raw_id: str) -> str:
    namespaces = {
        "douyin_shop_id": ("store_v1", "douyin_shop"),
        "qianchuan_shop_id": ("store_v1", "douyin_shop"),
        # Keep the historical shared HMAC namespace so existing local account
        # keys remain stable. The claim kind still records whether evidence is
        # an advertiser (`aavid`) or a parent account/shop identifier.
        "qianchuan_advertiser_id": ("adacct_v1", "qianchuan_account"),
        "qianchuan_account_id": ("adacct_v1", "qianchuan_account"),
        "douyin_product_id": ("product_v1", "douyin_product"),
        "qianchuan_product_id": ("product_v1", "douyin_product"),
        "douyin_sku_id": ("sku_v1", "douyin_sku"),
        "merchant_product_code": ("merchant_v1", "merchant_product"),
        "qianchuan_plan_id": ("plan_v1", "qianchuan_plan"),
        "qianchuan_material_id": ("material_v1", "qianchuan_material"),
        "douyin_content_id": ("content_v1", "douyin_content"),
        "douyin_live_room_id": ("live_v1", "douyin_live_room"),
        "douyin_live_session_id": ("session_v1", "douyin_live_session"),
    }
    prefix, namespace = namespaces[kind]
    normalized = str(raw_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,80}", normalized):
        raise ValueError("invalid identity claim")
    digest = hmac.new(_identity_secret(), f"{namespace}\0{normalized}".encode("utf-8"), hashlib.sha256).hexdigest()[:26]
    return f"{prefix}_{digest}"


def _action_server_signature(action: dict[str, Any]) -> str:
    target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
    scope = action.get("proposal_scope") if isinstance(action.get("proposal_scope"), dict) else {}
    signature_version = int(action.get("server_signature_version") or 1)
    payload = {
        "version": signature_version,
        "action_id": str(action.get("action_id") or ""),
        "integrity_hash": str(action.get("integrity_hash") or ""),
        "store_key": str(scope.get("store_key") or ""),
        "account_key": str(target.get("account_key") or ""),
        "server_issued_at_ms": int(action.get("server_issued_at_ms") or 0),
    }
    if signature_version >= 2:
        payload["binding_generation"] = int(scope.get("binding_generation") or 0)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hmac.new(_identity_secret(), b"dian-action-v1\0" + raw, hashlib.sha256).hexdigest()


def _seal_action_draft(action: dict[str, Any]) -> dict[str, Any]:
    """Bind a protocol draft to this Agent installation and selected store."""

    _assert_active_binding_registry_consistent()
    sealed = dict(action)
    settings = load_agent_settings()
    promotion_context = build_promotion_context(sealed.get("promotion_context"))
    account_scope = promotion_context.get("account_scope") if isinstance(promotion_context.get("account_scope"), dict) else {}
    context_store = str(account_scope.get("store_id") or "").strip().lower()
    context_account = str(account_scope.get("account_id") or "").strip().lower()
    selected_store = str(settings.get("store_key") or "").strip().lower()
    selected_account = str(settings.get("qianchuan_account_key") or "").strip().lower()
    target_account = _action_target_account_key(sealed).strip().lower()
    if selected_store and context_store and selected_store != context_store:
        raise ValueError("方案店铺与当前选择店铺不一致，请切回正确店铺后重新生成方案。")
    if context_account and target_account and context_account != target_account:
        raise ValueError("方案投放账户与目标账户不一致，请重新读取当前账户。")
    if selected_account and target_account and selected_account != target_account:
        raise ValueError("方案目标账户与当前选择账户不一致，请切回正确账户后重新生成方案。")
    proposal_store = selected_store or context_store or "unresolved"
    sealed["proposal_scope"] = {
        "store_key": proposal_store,
        "account_key": target_account,
        "binding_generation": _binding_generation(proposal_store, target_account),
    }
    sealed["server_issued_at_ms"] = int(time.time() * 1000)
    sealed["server_signature_version"] = 2
    sealed["server_signature"] = _action_server_signature(sealed)
    return sealed


def _validate_action_server_seal(action: dict[str, Any]) -> None:
    if int(action.get("server_signature_version") or 1) not in {1, 2}:
        raise ValueError("动作签名版本不受支持，请重新生成方案。")
    signature = str(action.get("server_signature") or "")
    expected = _action_server_signature(action)
    if not re.fullmatch(r"[a-f0-9]{64}", signature) or not hmac.compare_digest(signature, expected):
        raise ValueError("动作不是由当前本地 Agent 签发，或签发后参数已变化，请重新生成方案。")
    scope = action.get("proposal_scope") if isinstance(action.get("proposal_scope"), dict) else {}
    selected_store = str(load_agent_settings().get("store_key") or "").strip().lower()
    selected_account = str(load_agent_settings().get("qianchuan_account_key") or "").strip().lower()
    signed_store = str(scope.get("store_key") or "").strip().lower()
    signed_account = str(scope.get("account_key") or "").strip().lower()
    promotion_context = build_promotion_context(action.get("promotion_context"))
    account_scope = promotion_context.get("account_scope") if isinstance(promotion_context.get("account_scope"), dict) else {}
    context_store = str(account_scope.get("store_id") or "").strip().lower()
    context_account = str(account_scope.get("account_id") or "").strip().lower()
    if not signed_store or (selected_store and signed_store != selected_store):
        raise ValueError("动作不属于当前店铺，请切回正确店铺后重新生成方案。")
    if signed_account != _action_target_account_key(action).strip().lower():
        raise ValueError("动作签发账户与目标账户不一致，请重新生成方案。")
    if context_store and context_store != signed_store:
        raise ValueError("动作投放上下文与签发店铺不一致，请重新读取当前店铺。")
    if context_account and context_account != signed_account:
        raise ValueError("动作投放上下文与签发账户不一致，请重新读取当前账户。")
    if selected_account and signed_account != selected_account:
        raise ValueError("动作不属于当前选择账户，请切回正确账户后重新生成方案。")


def build_action_draft(**kwargs: Any) -> dict[str, Any]:
    """Create a protocol draft and seal it as a canonical local proposal."""

    return _seal_action_draft(_build_action_draft_protocol(**kwargs))


_COMMERCE_ENTITY_HEADER_KINDS = {
    "商品id": "douyin_product_id",
    "抖音商品id": "douyin_product_id",
    "productid": "douyin_product_id",
    "goodsid": "douyin_product_id",
    "skuid": "douyin_sku_id",
    "商品skuid": "douyin_sku_id",
    "规格id": "douyin_sku_id",
    "商家编码": "merchant_product_code",
    "商品编码": "merchant_product_code",
    "货号": "merchant_product_code",
    "计划id": "qianchuan_plan_id",
    "广告计划id": "qianchuan_plan_id",
    "项目id": "qianchuan_plan_id",
    "素材id": "qianchuan_material_id",
    "创意id": "qianchuan_material_id",
    "视频id": "douyin_content_id",
    "内容id": "douyin_content_id",
    "直播间id": "douyin_live_room_id",
    "房间id": "douyin_live_room_id",
    "场次id": "douyin_live_session_id",
}

_COMMERCE_NAME_HEADERS = {
    "douyin_product_id": ("商品名称", "商品名", "商品"),
    "qianchuan_product_id": ("商品名称", "商品名", "商品"),
    "douyin_sku_id": ("规格名称", "规格", "商品名称", "商品名"),
    "merchant_product_code": ("商品名称", "商品名", "商品"),
    "qianchuan_plan_id": ("计划名称", "项目名称", "计划"),
    "qianchuan_material_id": ("素材名称", "创意名称", "视频名称", "素材", "视频"),
    "douyin_content_id": ("视频名称", "内容名称", "视频", "内容"),
    "douyin_live_room_id": ("直播间", "抖音号", "主播"),
    "douyin_live_session_id": ("场次名称", "直播场次", "场次"),
}

_COMMERCE_RELATIONS = {
    ("qianchuan_plan_id", "douyin_product_id"): "promotes",
    ("qianchuan_plan_id", "merchant_product_code"): "promotes",
    ("qianchuan_plan_id", "qianchuan_material_id"): "uses_material",
    ("qianchuan_plan_id", "douyin_content_id"): "uses_content",
    ("douyin_content_id", "douyin_product_id"): "promotes",
    ("qianchuan_material_id", "douyin_product_id"): "promotes",
    ("douyin_live_room_id", "douyin_product_id"): "features",
    ("douyin_live_session_id", "douyin_product_id"): "features",
    ("douyin_sku_id", "douyin_product_id"): "variant_of",
}


def _commerce_relation(left: dict[str, str], right: dict[str, str]) -> tuple[dict[str, str], dict[str, str], str] | None:
    """Return a semantically directed relation independent of table column order."""

    direct = _COMMERCE_RELATIONS.get((left["entity_type"], right["entity_type"]))
    if direct:
        return left, right, direct
    reverse = _COMMERCE_RELATIONS.get((right["entity_type"], left["entity_type"]))
    return (right, left, reverse) if reverse else None


def _commerce_header_key(value: Any) -> str:
    return re.sub(r"[\s\-_/（）()：:.]", "", str(value or "").strip().lower())


_PLAN_HEADER_ENTITY_PATTERN = (
    r"(?:(?:商品|直播|广告|推广|投放|全域推广|标准推广|乘方)?计划|"
    r"(?:广告|推广|投放)?项目|广告组|单元)"
)


def _normalized_business_header(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"(?:升序|降序|可排序|排序|筛选)", "", text)
    return re.sub(r"[\s\-_/（）()：:·|]", "", text)


def _plan_header_role(value: Any) -> str:
    """Classify Qianchuan plan headers using the browser extractor contract."""

    normalized = _normalized_business_header(value)
    if re.fullmatch(rf"{_PLAN_HEADER_ENTITY_PATTERN}(?:id|编号)", normalized, re.IGNORECASE):
        return "id"
    if re.fullmatch(
        rf"{_PLAN_HEADER_ENTITY_PATTERN}(?:名称|信息|详情)?(?:及|和|与|含)?(?:id|编号)?",
        normalized,
        re.IGNORECASE,
    ):
        return "name"
    return ""


def _is_plan_name_header(value: Any) -> bool:
    return _plan_header_role(value) == "name"


def _commerce_entity_kind_for_header(value: Any) -> str:
    key = _commerce_header_key(value)
    explicit = _COMMERCE_ENTITY_HEADER_KINDS.get(key, "")
    if explicit:
        return explicit
    return "qianchuan_plan_id" if _plan_header_role(value) == "id" else ""


def _commerce_raw_identifier(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text in {"-", "--", "[已隐藏]", "[标识无效]"}:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_-]{4,80}", text):
        return text
    matches = re.findall(r"(?:ID\s*[:：#-]?\s*)?([A-Za-z0-9_-]{4,80})", text, re.IGNORECASE)
    return matches[-1] if matches else ""


def _commerce_display_name(record: dict[str, Any], kind: str) -> str:
    aliases = {_commerce_header_key(value) for value in _COMMERCE_NAME_HEADERS.get(kind, ())}
    for label, value in record.items():
        matches = _is_plan_name_header(label) if kind == "qianchuan_plan_id" else _commerce_header_key(label) in aliases
        if matches:
            text = re.sub(r"\s+", " ", str(value or "")).strip()
            if text and text not in {"-", "--", "[已隐藏]"}:
                return text[:120]
    return ""


def _resolve_commerce_entities(data: dict[str, Any]) -> dict[str, Any]:
    """Replace raw commerce IDs with installation-local HMAC keys before disk writes.

    Exact identifiers are used only on the localhost bridge.  Display names are
    metadata, never join keys.  Plan IDs stay in their existing operational
    column because supervised execution contracts require the platform ID; a
    separate local entity key is still emitted for graph relations.
    """

    tables = data.get("tables")
    if not isinstance(tables, list):
        return data
    entities: dict[str, dict[str, Any]] = {}
    relations: dict[str, dict[str, Any]] = {}
    replace_in_table = {
        "douyin_product_id", "qianchuan_product_id", "douyin_sku_id", "merchant_product_code",
        "qianchuan_material_id", "douyin_content_id", "douyin_live_room_id", "douyin_live_session_id",
    }
    for table_index, table in enumerate(tables[:8]):
        if not isinstance(table, dict):
            continue
        headers = [str(value or "") for value in table.get("headers", [])]
        rows = table.get("rows")
        if not headers or not isinstance(rows, list):
            continue
        kind_by_index = {
            index: kind
            for index, header in enumerate(headers)
            if (kind := _commerce_entity_kind_for_header(header))
        }
        if not kind_by_index:
            continue
        entity_rows: list[dict[str, Any]] = []
        for row_index, row in enumerate(rows[:500]):
            if not isinstance(row, list):
                continue
            record = {header: row[index] if index < len(row) else "" for index, header in enumerate(headers)}
            row_entities: list[dict[str, str]] = []
            for column_index, kind in kind_by_index.items():
                if column_index >= len(row):
                    continue
                raw_id = _commerce_raw_identifier(row[column_index])
                if not raw_id:
                    if str(row[column_index] or "").strip():
                        row[column_index] = "[标识无效]"
                    continue
                try:
                    entity_key = _local_identity_key(kind, raw_id)
                except (KeyError, ValueError):
                    row[column_index] = "[标识无效]"
                    continue
                display_name = _commerce_display_name(record, kind)
                evidence = {
                    "page_type": str(data.get("page_type") or "")[:48],
                    "table_index": table_index,
                    "row_index": row_index,
                    "column_index": column_index,
                }
                entities.setdefault(entity_key, {
                    "entity_key": entity_key,
                    "entity_type": kind,
                    "display_name": display_name,
                    "confidence": "exact",
                    "evidence": evidence,
                })
                if display_name and not entities[entity_key].get("display_name"):
                    entities[entity_key]["display_name"] = display_name
                row_entities.append({"entity_key": entity_key, "entity_type": kind})
                if kind in replace_in_table:
                    row[column_index] = entity_key
            if row_entities:
                entity_rows.append({"row_index": row_index, "entities": row_entities})
            for left_index, left in enumerate(row_entities):
                for right in row_entities[left_index + 1:]:
                    resolved_relation = _commerce_relation(left, right)
                    if not resolved_relation:
                        continue
                    relation_from, relation_to, relation = resolved_relation
                    relation_key = hashlib.sha256(
                        f"{relation_from['entity_key']}|{relation}|{relation_to['entity_key']}".encode("utf-8")
                    ).hexdigest()[:24]
                    relations.setdefault(relation_key, {
                        "relation_key": relation_key,
                        "from_key": relation_from["entity_key"],
                        "to_key": relation_to["entity_key"],
                        "relation": relation,
                        "confidence": "exact_row",
                        "evidence": {
                            "page_type": str(data.get("page_type") or "")[:48],
                            "table_index": table_index,
                            "row_index": row_index,
                        },
                    })
        if entity_rows:
            table["entity_rows"] = entity_rows
    if entities:
        data["commerce_contract_version"] = 1
        data["commerce_entities"] = list(entities.values())
        data["commerce_relations"] = list(relations.values())
        privacy = data.get("privacy") if isinstance(data.get("privacy"), dict) else {}
        data["privacy"] = {
            **privacy,
            "commerce_ids_resolved_locally": True,
            "raw_product_ids_persisted": False,
            "name_only_join_allowed": False,
        }
    return data


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
    _recover_binding_transaction()
    path = _account_catalog_path()
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or "accounts" not in value
            or value.get("schema_version") not in {None, 2}
            or not isinstance(value.get("accounts"), list)
        ):
            raise OSError("Qianchuan account catalog has an invalid schema")
        accounts = value["accounts"]
        cleaned: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        needs_privacy_migration = value.get("schema_version") != 2
        for account in accounts:
            if not isinstance(account, dict):
                raise OSError("Qianchuan account catalog entry is invalid")
            key = str(account.get("key") or "").lower()
            if not SAFE_KEY.fullmatch(key) or key in seen_keys:
                raise OSError("Qianchuan account catalog key is invalid or duplicated")
            legacy_label = _normalized_account_label(account.get("label"))
            needs_privacy_migration = needs_privacy_migration or bool(legacy_label and legacy_label != _private_alias("account", key))
            if legacy_label and not _is_valid_qianchuan_account_label(legacy_label):
                # Old pre-HMAC catalogs contained navigation labels that were
                # never accounts. They are safe to discard only when they are
                # unbound legacy noise, never when they carry a relation.
                if account.get("store_key") or account.get("aliases"):
                    raise OSError("Bound Qianchuan account has an invalid legacy label")
                seen_keys.add(key)
                continue
            raw_aliases = account.get("aliases", [])
            if not isinstance(raw_aliases, list) or len(raw_aliases) > 20:
                raise OSError("Qianchuan account aliases are invalid")
            aliases = [str(alias).lower() for alias in raw_aliases]
            if (
                any(not SAFE_KEY.fullmatch(alias) or alias == key for alias in aliases)
                or len(aliases) != len(set(aliases))
            ):
                raise OSError("Qianchuan account alias is invalid or duplicated")
            store_key = str(account.get("store_key") or "").lower()
            if store_key and not SAFE_KEY.fullmatch(store_key):
                raise OSError("Qianchuan account store binding is invalid")
            cleaned.append({
                "key": key,
                "label": _private_alias("account", key),
                "confidence": str(account.get("confidence") or "medium")[:16],
                "identity_source": str(account.get("identity_source") or "legacy")[:40],
                "evidence_source": str(account.get("evidence_source") or "")[:32],
                "store_key": store_key,
                "aliases": aliases,
                "last_seen": str(account.get("last_seen") or "")[:32],
            })
            seen_keys.add(key)
        if needs_privacy_migration:
            _atomic_json_write(path, {"schema_version": 2, "accounts": [{key: value for key, value in item.items() if key != "label"} for item in cleaned]})
        return cleaned
    except json.JSONDecodeError as error:
        raise OSError("Qianchuan account catalog is unreadable") from error


def list_store_identities() -> list[dict[str, Any]]:
    _recover_binding_transaction()
    path = _store_catalog_path()
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or "stores" not in value
            or value.get("schema_version") not in {None, 1}
            or not isinstance(value.get("stores"), list)
        ):
            raise OSError("Store identity catalog has an invalid schema")
        stores = value["stores"]
        cleaned = []
        seen_keys: set[str] = set()
        for item in stores:
            if not isinstance(item, dict):
                raise OSError("Store identity catalog entry is invalid")
            key = str(item.get("key") or "").lower()
            if not SAFE_KEY.fullmatch(key) or key in seen_keys:
                raise OSError("Store identity catalog key is invalid or duplicated")
            raw_account_keys = item.get("account_keys", [])
            if not isinstance(raw_account_keys, list) or len(raw_account_keys) > MAX_STORE_ACCOUNT_BINDINGS:
                raise OSError("Store account bindings are invalid or exceed capacity")
            account_keys = [str(account_key).lower() for account_key in raw_account_keys]
            if (
                any(not SAFE_KEY.fullmatch(account_key) for account_key in account_keys)
                or len(account_keys) != len(set(account_keys))
            ):
                raise OSError("Store account binding is invalid or duplicated")
            cleaned.append({
                "key": key,
                "label": _private_alias("store", key),
                "confidence": str(item.get("confidence") or "medium")[:16],
                "identity_source": str(item.get("identity_source") or "legacy")[:40],
                "evidence_source": str(item.get("evidence_source") or "")[:32],
                "account_keys": account_keys,
                "last_seen": str(item.get("last_seen") or "")[:32],
            })
            seen_keys.add(key)
        if value.get("schema_version") != 1:
            _atomic_json_write(path, {"schema_version": 1, "stores": [{key: value for key, value in item.items() if key != "label"} for item in cleaned]})
        return cleaned
    except json.JSONDecodeError as error:
        raise OSError("Store identity catalog is unreadable") from error


def _assert_active_binding_registry_consistent(
    stores_value: list[dict[str, Any]] | None = None,
    accounts_value: list[dict[str, Any]] | None = None,
) -> None:
    """Fail closed when the signed permission registry and catalogs diverge."""

    stores = {
        str(item.get("key") or "").strip().lower(): item
        for item in (stores_value if stores_value is not None else list_store_identities())
        if isinstance(item, dict)
    }
    accounts = {
        str(item.get("key") or "").strip().lower(): item
        for item in (accounts_value if accounts_value is not None else list_qianchuan_accounts())
        if isinstance(item, dict)
    }
    active_account_stores: dict[str, str] = {}
    for relation in _load_binding_revocations().values():
        if not isinstance(relation, dict) or relation.get("active") is not True:
            continue
        store_key = str(relation.get("store_key") or "").strip().lower()
        account_key = str(relation.get("account_key") or "").strip().lower()
        previous_store = active_account_stores.get(account_key)
        if previous_store and previous_store != store_key:
            raise OSError("one Qianchuan account has multiple active store permissions")
        active_account_stores[account_key] = store_key
        account = accounts.get(account_key)
        store = stores.get(store_key)
        if (
            not isinstance(account, dict)
            or not isinstance(store, dict)
            or str(account.get("store_key") or "").strip().lower() != store_key
            or account_key not in {
                str(value).strip().lower() for value in (store.get("account_keys") or [])
            }
        ):
            raise OSError("active store/account permission is inconsistent with local catalogs")


@_guard_binding_state_mutation
def _remember_store_identity(store: dict[str, Any], account_key: str = "") -> None:
    key = str(store.get("key") or "").lower()
    if not SAFE_KEY.fullmatch(key):
        return
    with _state_lock:
        stores = {str(item.get("key")): item for item in list_store_identities()}
        previous = stores.get(key, {})
        requested_account_key = str(account_key or "").strip().lower()
        if requested_account_key and _binding_is_revoked(key, requested_account_key):
            requested_account_key = ""
        account_keys = list(dict.fromkeys([
            str(value).lower()
            for value in [
                *(previous.get("account_keys") or []),
                *([requested_account_key] if requested_account_key else []),
            ]
            if SAFE_KEY.fullmatch(str(value).lower())
        ]))
        if len(account_keys) > MAX_STORE_ACCOUNT_BINDINGS:
            raise ValueError(
                f"STORE_ACCOUNT_CAPACITY_EXCEEDED: one store supports at most {MAX_STORE_ACCOUNT_BINDINGS} bound accounts."
            )
        stores[key] = {
            "key": key,
            "confidence": str(store.get("confidence") or previous.get("confidence") or "medium")[:16],
            "identity_source": str(store.get("identity_source") or previous.get("identity_source") or "legacy")[:40],
            "evidence_source": str(store.get("evidence_source") or previous.get("evidence_source") or "")[:32],
            "account_keys": account_keys,
            "last_seen": _now_label(),
        }
        _atomic_json_write(_store_catalog_path(), {"schema_version": 1, "stores": sorted(stores.values(), key=lambda item: item.get("last_seen", ""), reverse=True)})


def _catalog_payloads(
    stores: dict[str, dict[str, Any]], accounts: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    store_payload = {
        "schema_version": 1,
        "stores": sorted(
            ({key: value for key, value in item.items() if key != "label"} for item in stores.values()),
            key=lambda item: item.get("last_seen", ""),
            reverse=True,
        ),
    }
    account_payload = {
        "schema_version": 2,
        "accounts": sorted(
            ({key: value for key, value in item.items() if key != "label"} for item in accounts.values()),
            key=lambda item: item.get("last_seen", ""),
            reverse=True,
        ),
    }
    return store_payload, account_payload


def _commit_binding_transaction(journal: dict[str, Any]) -> None:
    """Apply one prepared link/unlink journal in its fail-closed order."""

    _atomic_json_write(_binding_transaction_path(), journal)
    operation = str(journal["operation"])
    next_state = journal["next"]
    if operation == "link":
        _atomic_json_write(_binding_revocations_path(), journal["guard_revocations"])
    else:
        _atomic_json_write(_binding_revocations_path(), next_state["revocations"])
    _atomic_json_write(_store_catalog_path(), next_state["stores"])
    _atomic_json_write(_account_catalog_path(), next_state["accounts"])
    if operation == "link":
        _atomic_json_write(_binding_revocations_path(), next_state["revocations"])
    _atomic_json_write(_settings_path(), next_state["settings"])
    _atomic_json_write(
        _binding_transaction_path(),
        {**journal, "phase": "committed", "committed_at_ms": int(time.time() * 1000)},
    )
    _clear_binding_transaction()


@_guard_production_scope_change("store_account_linked")
@_guard_binding_state_mutation
def link_store_account(store_key: str, account_key: str) -> dict[str, Any]:
    store_key = str(store_key or "").strip().lower()
    account_key = str(account_key or "").strip().lower()
    with _state_lock:
        stores = {str(item.get("key") or ""): item for item in list_store_identities()}
        accounts = {str(item.get("key") or ""): item for item in list_qianchuan_accounts()}
        if store_key not in stores or account_key not in accounts:
            raise ValueError("店铺或千川账户不存在，请分别同步当前页面后再人工关联。")
        existing_store = str(accounts[account_key].get("store_key") or "").strip().lower()
        if existing_store and existing_store != store_key:
            raise ValueError("该千川账户已关联其他店铺，系统不会自动改绑。")
        linked_accounts = list(dict.fromkeys(str(value).lower() for value in stores[store_key].get("account_keys", [])))
        relation = _load_binding_revocations().get(f"{store_key}:{account_key}")
        registry_initialized = load_agent_settings().get("binding_registry_initialized") is True
        registry_relation_active = bool(
            isinstance(relation, dict)
            and relation.get("active") is True
            and int(relation.get("generation") or 0) > 0
            and int(relation.get("linked_at_ms") or 0) > 0
        )
        # Catalog fields are compatibility mirrors, not the permission source.
        # Once the explicit registry has been initialized, a catalog-only pair
        # must flow through this transaction so a real binding lease is minted.
        already_complete = bool(
            existing_store == store_key
            and account_key in linked_accounts
            and not _binding_is_revoked(store_key, account_key)
            and (not registry_initialized or registry_relation_active)
        )
        if already_complete:
            # The public selector would try to acquire the production audit
            # file lock again.  This transaction already owns audit -> binding,
            # so call the lock-assuming core directly and keep one lock order.
            return _select_store_context_locked(store_key, account_key)
        if account_key not in linked_accounts and len(linked_accounts) >= MAX_STORE_ACCOUNT_BINDINGS:
            raise ValueError(
                f"STORE_ACCOUNT_CAPACITY_EXCEEDED: 单店最多关联 {MAX_STORE_ACCOUNT_BINDINGS} 个广告账户，本次未写入任何关系。"
            )

        _prepare_execution_scope_change("store_account_link_changed")
        previous_settings = load_agent_settings()
        previous_stores, previous_accounts = _catalog_payloads(stores, accounts)
        previous_registry = _load_binding_revocations()
        previous_revocations = _binding_revocations_payload(previous_registry)
        relation_key = f"{store_key}:{account_key}"
        previous_relation = previous_registry.get(relation_key, {})
        next_generation = max(0, int(previous_relation.get("generation") or 0)) + 1
        now_ms = int(time.time() * 1000)
        guard_registry = {
            **previous_registry,
            relation_key: {
                "store_key": store_key,
                "account_key": account_key,
                "generation": next_generation,
                "active": False,
                "linked_at_ms": 0,
                "revoked_at_ms": now_ms,
                "reason": "link_transaction_prepared",
            },
        }
        next_registry = {
            **guard_registry,
            relation_key: {
                "store_key": store_key,
                "account_key": account_key,
                "generation": next_generation,
                "active": True,
                "linked_at_ms": now_ms,
                "revoked_at_ms": 0,
                "reason": "manual_confirmation",
            },
        }
        stores[store_key] = {
            **stores[store_key],
            "account_keys": list(dict.fromkeys([*linked_accounts, account_key])),
            "last_seen": _now_label(),
        }
        accounts[account_key] = {
            **accounts[account_key],
            "store_key": store_key,
            "evidence_source": "manual_confirmation",
            "last_seen": _now_label(),
        }
        next_stores, next_accounts = _catalog_payloads(stores, accounts)
        next_settings = {
            **previous_settings,
            "store_key": store_key,
            "qianchuan_account_key": account_key,
            "binding_registry_initialized": True,
        }
        journal = {
            "schema_version": 1,
            "operation": "link",
            "phase": "prepared",
            "prepared_at_ms": now_ms,
            "store_key": store_key,
            "account_key": account_key,
            "previous": {
                "stores": previous_stores,
                "accounts": previous_accounts,
                "settings": previous_settings,
                "revocations": previous_revocations,
            },
            "guard_revocations": _binding_revocations_payload(guard_registry),
            "next": {
                "stores": next_stores,
                "accounts": next_accounts,
                "settings": next_settings,
                "revocations": _binding_revocations_payload(next_registry),
            },
        }
        try:
            _commit_binding_transaction(journal)
        except OSError:
            _recover_binding_transaction()
            raise
        _confirm_onboarding_store(store_key)
        return build_store_catalog()


@_guard_production_scope_change("store_account_unlinked")
@_guard_binding_state_mutation
def unlink_store_account(store_key: str, account_key: str) -> dict[str, Any]:
    """Remove one confirmed store/account relation without deleting local data."""

    store_key = str(store_key or "").lower()
    account_key = str(account_key or "").lower()
    with _state_lock:
        stores = {str(item.get("key") or ""): item for item in list_store_identities()}
        accounts = {str(item.get("key") or ""): item for item in list_qianchuan_accounts()}
        if store_key not in stores:
            raise ValueError("指定店铺不存在，无法解绑千川账户。")
        if account_key not in accounts:
            raise ValueError("指定千川账户不存在，无法解绑店铺。")

        linked_accounts = list(stores[store_key].get("account_keys") or [])
        linked_store = str(accounts[account_key].get("store_key") or "")
        if linked_store and linked_store != store_key:
            raise ValueError("该千川账户关联的是其他店铺，不能从当前店铺解绑。")
        if account_key not in linked_accounts or linked_store != store_key:
            raise ValueError("该店铺与千川账户不存在完整匹配的关联，无法解绑。")

        _prepare_execution_scope_change("store_account_unlinked")
        previous_store_payload, previous_account_payload = _catalog_payloads(stores, accounts)
        previous_settings = load_agent_settings()
        previous_registry = _load_binding_revocations()
        previous_revocations = _binding_revocations_payload(previous_registry)

        stores[store_key] = {
            **stores[store_key],
            "account_keys": [value for value in linked_accounts if value != account_key],
        }
        accounts[account_key] = {**accounts[account_key], "store_key": ""}
        store_payload, account_payload = _catalog_payloads(stores, accounts)
        clear_current_account = (
            str(previous_settings.get("store_key") or "").lower() == store_key
            and str(previous_settings.get("qianchuan_account_key") or "").lower() == account_key
        )
        relation_key = f"{store_key}:{account_key}"
        previous_relation = previous_registry.get(relation_key, {})
        now_ms = int(time.time() * 1000)
        next_registry = {
            **previous_registry,
            relation_key: {
                "store_key": store_key,
                "account_key": account_key,
                "generation": max(0, int(previous_relation.get("generation") or 0)) + 1,
                "active": False,
                "linked_at_ms": int(previous_relation.get("linked_at_ms") or 0),
                "revoked_at_ms": now_ms,
                "reason": "manual_unlink",
            },
        }
        next_settings = {
            **previous_settings,
            "binding_registry_initialized": True,
            **({"store_key": store_key, "qianchuan_account_key": ""} if clear_current_account else {}),
        }
        journal = {
            "schema_version": 1,
            "operation": "unlink",
            "phase": "prepared",
            "prepared_at_ms": now_ms,
            "store_key": store_key,
            "account_key": account_key,
            "previous": {
                "stores": previous_store_payload,
                "accounts": previous_account_payload,
                "settings": previous_settings,
                "revocations": previous_revocations,
            },
            "guard_revocations": _binding_revocations_payload(next_registry),
            "next": {
                "stores": store_payload,
                "accounts": account_payload,
                "settings": next_settings,
                "revocations": _binding_revocations_payload(next_registry),
            },
        }
        try:
            _commit_binding_transaction(journal)
        except OSError:
            _recover_binding_transaction()
            raise
    return build_store_catalog()


def _select_store_context_locked(store_key: str, account_key: str = "") -> dict[str, Any]:
    """Select one scope while the caller owns audit -> binding locks."""

    store_key = str(store_key or "").strip().lower()
    account_key = str(account_key or "").strip().lower()
    with _state_lock:
        previous_settings = load_agent_settings()
        if not store_key:
            if previous_settings.get("store_key") or previous_settings.get("qianchuan_account_key"):
                _prepare_execution_scope_change("selected_store_cleared")
            _save_agent_settings_locked({"store_key": "", "qianchuan_account_key": ""})
            return build_store_catalog()
        stores = {str(item.get("key") or ""): item for item in list_store_identities()}
        accounts = {str(item.get("key") or ""): item for item in list_qianchuan_accounts()}
        binding_registry = _load_binding_revocations()
        binding_registry_initialized = previous_settings.get("binding_registry_initialized") is True
        if store_key not in stores:
            raise ValueError("请选择已识别的匿名店铺。")
        strict_linked = []
        for value in stores[store_key].get("account_keys", []):
            candidate = str(value).strip().lower()
            if (
                candidate in accounts
                and str(accounts[candidate].get("store_key") or "").strip().lower() == store_key
                and _qianchuan_binding_scope_from_catalogs(
                    store_key,
                    candidate,
                    stores,
                    accounts,
                    binding_registry,
                    registry_initialized=binding_registry_initialized,
                ) is not None
            ):
                strict_linked.append(candidate)
        strict_linked = list(dict.fromkeys(strict_linked))
        if account_key and account_key not in strict_linked:
            raise ValueError("该千川账户尚未与当前店铺完成双向确认，或该关系已解绑。")
        if not account_key and len(strict_linked) == 1:
            account_key = strict_linked[0]
        elif not account_key and len(strict_linked) > 1:
            account_key = ""
        if (
            str(previous_settings.get("store_key") or "").lower() != store_key
            or str(previous_settings.get("qianchuan_account_key") or "").lower() != account_key
        ):
            _prepare_execution_scope_change("selected_store_or_account_changed")
        _save_agent_settings_locked({"store_key": store_key, "qianchuan_account_key": account_key})
        _confirm_onboarding_store(store_key)
        return build_store_catalog()


@_guard_production_scope_change("selected_store_or_account_changed")
@_guard_binding_state_mutation
def select_store_context(store_key: str, account_key: str = "") -> dict[str, Any]:
    return _select_store_context_locked(store_key, account_key)


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
    store_identities = list_store_identities()
    accounts = list_qianchuan_accounts()
    accounts_by_key = {str(account.get("key") or ""): account for account in accounts}
    stores_by_key = {str(store.get("key") or ""): store for store in store_identities}
    binding_registry = _load_binding_revocations()
    binding_registry_initialized = settings.get("binding_registry_initialized") is True
    stores: list[dict[str, Any]] = []
    for store in store_identities:
        key = str(store.get("key") or "")
        doudian_dir = DATA_DIR / "stores" / key / "doudian"
        doudian_paths = list(doudian_dir.glob("*.json")) if doudian_dir.exists() else []
        declared_keys = list(dict.fromkeys([
            *(store.get("account_keys") or []),
            *(account_key for account_key, account in accounts_by_key.items() if account.get("store_key") == key),
        ]))
        binding_scopes: dict[str, dict[str, Any]] = {}
        linked_keys: list[str] = []
        for value in declared_keys:
            scope = _qianchuan_binding_scope_from_catalogs(
                key,
                value,
                stores_by_key,
                accounts_by_key,
                binding_registry,
                registry_initialized=binding_registry_initialized,
            )
            if scope is not None:
                linked_keys.append(value)
                binding_scopes[value] = scope
        inconsistent_keys = [value for value in declared_keys if value not in linked_keys]
        linked_accounts = [accounts_by_key[value] for value in linked_keys if value in accounts_by_key]
        qianchuan_paths: list[Path] = []
        for linked in linked_accounts:
            linked_key = str(linked.get("key") or "").strip().lower()
            try:
                binding_scope = binding_scopes.get(linked_key)
                if not binding_scope:
                    continue
                rows = _local_store().iter_latest_snapshots(
                    source_name="qianchuan",
                    account_key=linked_key,
                )
                account_paths = [
                    Path(str(row.get("source_path") or ""))
                    for row in rows
                    if str(row.get("source_path") or "").strip()
                    and _snapshot_matches_qianchuan_binding(row.get("payload"), binding_scope)
                ]
                # Generation-zero installations predate SQLite snapshot
                # metadata.  Their account-partition directory is safe for a
                # catalog *count* only after the current two-way pair has been
                # proven above; it never bypasses load_data's payload gate.
                if not account_paths and int(binding_scope.get("binding_generation") or 0) == 0:
                    legacy_account_dir = DATA_DIR / "qianchuan_accounts" / linked_key
                    if legacy_account_dir.exists():
                        account_paths.extend(legacy_account_dir.glob("*.json"))
                qianchuan_paths.extend(account_paths)
            except (LocalStoreError, OSError):
                logger.exception("Unable to enumerate current Qianchuan snapshots for %s/%s", key, linked_key)
        qianchuan_paths = list(dict.fromkeys(qianchuan_paths))
        snapshot_paths = [*doudian_paths, *qianchuan_paths]
        newest_timestamp = max(
            (path.stat().st_mtime for path in snapshot_paths if path.exists() and path.is_file()),
            default=0.0,
        )
        doudian_newest_timestamp = max(
            (path.stat().st_mtime for path in doudian_paths if path.exists() and path.is_file()),
            default=0.0,
        )
        doudian_fresh = bool(
            doudian_newest_timestamp
            and max(0.0, time.time() - doudian_newest_timestamp) < STALE_SECONDS
        )
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
            "doudian_updated_at": int(doudian_newest_timestamp) if doudian_newest_timestamp else None,
            "doudian_fresh": doudian_fresh,
            "qianchuan_page_count": len(qianchuan_paths),
            "account_keys": [str(item.get("key") or "") for item in linked_accounts],
            "binding_inconsistent": bool(inconsistent_keys),
            "binding_repair_required": bool(inconsistent_keys),
            "inconsistent_account_keys": inconsistent_keys,
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
    valid_account_keys = {
        str(account_key)
        for store in stores
        for account_key in (store.get("account_keys") or [])
    }
    unlinked_accounts = [
        {**account, "binding_repair_required": bool(account.get("store_key"))}
        for account in accounts
        if str(account.get("key") or "") not in valid_account_keys
    ]
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


def _oceanengine_account_center_inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Prepare a browser-safe account view while credentials remain in OAuth storage."""
    oauth_client = OceanEngineOAuth(DATA_DIR)
    oauth = oauth_client.status()
    private_accounts = oauth_client.authorized_accounts_private()
    account_sources = private_accounts or oauth.get("accounts", [])
    safe_accounts: list[dict[str, Any]] = []
    for account in account_sources:
        if not isinstance(account, dict):
            continue
        try:
            account_key = _local_identity_key(
                "qianchuan_account_id", str(account.get("account_id") or "")
            )
        except ValueError:
            continue
        safe_accounts.append({
            "account_key": account_key,
            "account_id": str(account.get("account_id") or ""),
            "account_name": str(account.get("account_name") or ""),
            "account_role": str(account.get("account_role") or ""),
            "valid": bool(account.get("valid", True)),
            "advertiser_count": int(account.get("advertiser_count") or len(account.get("advertiser_ids") or [])),
        })
    oauth = {**oauth, "accounts": safe_accounts, "secrets_exposed": False}
    return oauth, load_sync_status(DATA_DIR), build_store_catalog()


def build_oceanengine_account_center() -> dict[str, Any]:
    oauth, sync_status, store_catalog = _oceanengine_account_center_inputs()
    return OceanEngineAccountCenter(DATA_DIR).build(oauth, sync_status, store_catalog)


def _raw_oceanengine_account_ids(account_keys: list[str]) -> list[str]:
    """Resolve local HMAC keys server-side; raw identifiers never enter the extension."""
    requested = set(account_keys)
    resolved: list[str] = []
    for account in OceanEngineOAuth(DATA_DIR).authorized_accounts_private():
        if not isinstance(account, dict):
            continue
        raw_id = str(account.get("account_id") or "")
        try:
            account_key = _local_identity_key("qianchuan_account_id", raw_id)
        except ValueError:
            continue
        if account_key in requested:
            resolved.append(raw_id)
    if len(set(resolved)) != len(requested):
        raise ValueError("部分账户授权已变化，请刷新账户中心后重试。")
    return list(dict.fromkeys(resolved))


@_guard_binding_state_mutation
def _remember_qianchuan_account(account: dict[str, Any]) -> None:
    key = str(account.get("key") or "").lower()
    if not SAFE_KEY.fullmatch(key):
        return
    with _state_lock:
        accounts = {str(item.get("key")): item for item in list_qianchuan_accounts() if isinstance(item, dict)}
        previous = accounts.get(key, {})
        previous_store_key = str(previous.get("store_key") or "").strip().lower()
        incoming_store_key = str(account.get("store_key") or "").strip().lower()
        if not SAFE_KEY.fullmatch(incoming_store_key):
            incoming_store_key = ""
        if incoming_store_key and _binding_is_revoked(incoming_store_key, key):
            incoming_store_key = ""
        # Automatic page evidence can fill an empty binding, but it must never
        # move an already-bound account to another store. Rebinding is an
        # explicit user operation and is serialized by link_store_account().
        effective_store_key = previous_store_key or incoming_store_key
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
            "store_key": effective_store_key
            if SAFE_KEY.fullmatch(effective_store_key) else "",
            "aliases": list(dict.fromkeys(aliases)),
            "last_seen": _now_label(),
        }
        _atomic_json_write(_account_catalog_path(), {
            "schema_version": 2,
            "accounts": sorted(accounts.values(), key=lambda item: item.get("last_seen", ""), reverse=True),
        })


@_guard_binding_state_mutation
def _remember_qianchuan_identity_pair(store: dict[str, Any], account: dict[str, Any]) -> None:
    """Mirror browser identity evidence without overriding an explicit binding."""

    store_key = str(store.get("key") or "").strip().lower()
    account_key = str(account.get("key") or "").strip().lower()
    if not SAFE_KEY.fullmatch(store_key) or not SAFE_KEY.fullmatch(account_key):
        return
    with _state_lock:
        stores = {str(item.get("key") or ""): item for item in list_store_identities()}
        accounts = {str(item.get("key") or ""): item for item in list_qianchuan_accounts()}
        registry = _load_binding_revocations()
        # The signed registry is the permission boundary. If either catalog
        # lost an active relation, browser evidence must not reconstruct it as
        # a different generation-0 binding (or bind the same account twice).
        for relation in registry.values():
            if not isinstance(relation, dict) or relation.get("active") is not True:
                continue
            registered_store = str(relation.get("store_key") or "").strip().lower()
            registered_account = str(relation.get("account_key") or "").strip().lower()
            catalog_account = accounts.get(registered_account)
            catalog_store = stores.get(registered_store)
            if (
                not isinstance(catalog_account, dict)
                or not isinstance(catalog_store, dict)
                or str(catalog_account.get("store_key") or "").strip().lower() != registered_store
                or registered_account not in {
                    str(value).strip().lower() for value in (catalog_store.get("account_keys") or [])
                }
            ):
                raise OSError("active store/account registry relation is missing from a catalog")
            if registered_account == account_key and registered_store != store_key:
                raise OSError("browser evidence cannot move an actively registered account to another store")
        previous_store_payload, previous_account_payload = _catalog_payloads(stores, accounts)
        previous_account = accounts.get(account_key, {})
        previous_store = stores.get(store_key, {})
        previous_store_account_keys = [
            str(value).strip().lower()
            for value in (previous_store.get("account_keys") or [])
            if SAFE_KEY.fullmatch(str(value).strip().lower())
        ]
        account_keys = [value for value in previous_store_account_keys if value != account_key]
        capacity_available = account_key in previous_store_account_keys or len(account_keys) < MAX_STORE_ACCOUNT_BINDINGS
        existing_store_key = str(previous_account.get("store_key") or "").strip().lower()
        registry_initialized = load_agent_settings().get("binding_registry_initialized") is True
        active_relation = registry.get(f"{store_key}:{account_key}")
        explicitly_bound = bool(
            isinstance(active_relation, dict)
            and active_relation.get("active") is True
            and (
                not registry_initialized
                or (
                    int(active_relation.get("generation") or 0) > 0
                    and int(active_relation.get("linked_at_ms") or 0) > 0
                )
            )
        )
        # A lower-confidence refresh may omit ``account.store_key``.  It may
        # refresh metadata for an already bidirectional legacy pair, but it
        # must neither erase that pair nor create a new one.  Explicit
        # revocation still wins over both catalogs.
        catalog_bound = bool(
            not registry_initialized
            and existing_store_key == store_key
            and account_key in previous_store_account_keys
            and not _binding_is_revoked(store_key, account_key)
        )
        requested_binding = bool(
            explicitly_bound
            or catalog_bound
            or (
                not registry_initialized
                and str(account.get("store_key") or "").strip().lower() == store_key
                and not _binding_is_revoked(store_key, account_key)
                and (not existing_store_key or existing_store_key == store_key)
                and capacity_available
            )
        )
        aliases = list(dict.fromkeys([
            str(alias).lower()
            for alias in [*(previous_account.get("aliases") or []), *(account.get("aliases") or [])]
            if SAFE_KEY.fullmatch(str(alias).lower()) and str(alias).lower() != account_key
        ]))[:20]
        effective_store_key = (
            store_key
            if requested_binding
            else "" if registry_initialized else existing_store_key
        )
        accounts[account_key] = {
            "key": account_key,
            "confidence": str(account.get("confidence") or previous_account.get("confidence") or "medium")[:16],
            "identity_source": str(account.get("identity_source") or previous_account.get("identity_source") or "legacy")[:40],
            "evidence_source": str(account.get("evidence_source") or previous_account.get("evidence_source") or "")[:32],
            "store_key": effective_store_key,
            "aliases": aliases,
            "last_seen": _now_label(),
        }
        if requested_binding:
            account_keys.append(account_key)
        stores[store_key] = {
            "key": store_key,
            "confidence": str(store.get("confidence") or previous_store.get("confidence") or "medium")[:16],
            "identity_source": str(store.get("identity_source") or previous_store.get("identity_source") or "legacy")[:40],
            "evidence_source": str(store.get("evidence_source") or previous_store.get("evidence_source") or "")[:32],
            "account_keys": list(dict.fromkeys(account_keys)),
            "last_seen": _now_label(),
        }
        next_store_payload, next_account_payload = _catalog_payloads(stores, accounts)
        current_settings = load_agent_settings()
        current_revocations = _binding_revocations_payload(registry)
        journal = {
            "schema_version": 1,
            "operation": "mirror",
            "phase": "prepared",
            "prepared_at_ms": int(time.time() * 1000),
            "store_key": store_key,
            "account_key": account_key,
            "previous": {
                "stores": previous_store_payload,
                "accounts": previous_account_payload,
                "settings": current_settings,
                "revocations": current_revocations,
            },
            "guard_revocations": current_revocations,
            "next": {
                "stores": next_store_payload,
                "accounts": next_account_payload,
                "settings": current_settings,
                "revocations": current_revocations,
            },
        }
        try:
            _commit_binding_transaction(journal)
        except OSError:
            _recover_binding_transaction()
            raise


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


def save_data(
    source: str,
    data: dict[str, Any],
    *,
    trusted_origin: str | None = None,
    expected_store_key: str = "",
    expected_account_key: str = "",
) -> dict[str, Any]:
    if source not in ALLOWED_SOURCES:
        raise ValueError(f"unknown source: {source}")
    if not isinstance(data, dict):
        raise ValueError("data must be an object")

    try:
        json.dumps(data, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("data must contain only finite JSON values") from error

    data = dict(data)
    resolved_store, resolved_account, identity_resolution = _resolve_identity_claims(data)
    page_type = _safe_page_type(data.get("page_type"))
    now_ms = int(time.time() * 1000)
    captured_at_ms = int(data.get("captured_at") or data.get("timestamp") or now_ms)
    if captured_at_ms - now_ms > MAX_CAPTURE_FUTURE_SKEW_MS:
        raise ValueError("captured_at is too far in the future; check the system clock and resync")
    plan_coverage = _plan_snapshot_identity_coverage(data)
    browser_plan_collection = bool(
        source == "qianchuan"
        and trusted_origin != "official_api_oauth_client"
        and (
            page_type in {"campaigns", "qianchuan_campaigns", "plans"}
            or (page_type == "qianchuan_live" and plan_coverage["plan_table_count"] > 0)
        )
    )
    if browser_plan_collection:
        data, quarantined_plan_rows = _partition_plan_snapshot_rows(data)
        if plan_coverage["plan_table_count"] == 0:
            raise ValueError(
                "PLAN_TABLE_UNRECOGNIZED: 当前页面没有可验证的计划表结构，已拒绝覆盖现有计划数据。"
            )
        if plan_coverage["eligible_rows"] == 0 and not plan_coverage["explicit_empty"]:
            raise ValueError(
                "PLAN_TABLE_LOADING: 当前页面只识别到计划表头，计划业务行可能仍在加载；已保留现有计划数据并等待重试。"
            )
        if plan_coverage["eligible_rows"] > plan_coverage["identified_rows"] and plan_coverage["identified_rows"] == 0:
            raise ValueError(
                "PLAN_IDENTIFIERS_UNRESOLVED: 当前计划表存在缺少计划 ID 的业务行，已拒绝覆盖现有计划数据。"
            )
        partial = bool(quarantined_plan_rows)
        plan_coverage = {
            **plan_coverage,
            "persisted_identified_rows": plan_coverage["identified_rows"],
            "quarantined_rows": len(quarantined_plan_rows),
            "collection_state": "partial" if partial else "complete",
            "coverage_complete": not partial,
            "automatic_write_allowed": False,
        }
        quality = dict(data.get("quality") or {})
        quality["server_plan_identity_coverage"] = plan_coverage
        data["quality"] = quality
        collection = dict(data.get("plan_collection") or {})
        collection.update({
            "status": "partial" if partial else "identity_validated",
            "coverage_complete": not partial,
            "identified_rows": plan_coverage["identified_rows"],
            "quarantined_rows": len(quarantined_plan_rows),
            "automatic_write_allowed": False,
        })
        data["plan_collection"] = collection
        if quarantined_plan_rows:
            data["plan_row_quarantine"] = {
                "schema_version": 1,
                "active": True,
                "reason": "stable_plan_id_missing",
                "excluded_from_current_plan_rows": True,
                "rows": quarantined_plan_rows,
            }
    data = _resolve_commerce_entities(data)
    normalized = {
        **data,
        "schema_version": int(data.get("schema_version") or 1),
        "source": source,
        "page_type": page_type,
        "captured_at": captured_at_ms,
        "identity_resolution": identity_resolution,
    }
    # Binding generations are minted by the Agent. Browser input must never be
    # able to choose the generation that makes an old account snapshot current.
    normalized.pop("binding_scope", None)
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
        candidate_store_key = str(normalized["store"].get("key") or "").strip().lower()
        candidate_account_key = str(normalized["account"].get("key") or "").strip().lower()
        if (
            normalized["store"].get("confidence") == "high"
            and normalized["account"].get("confidence") == "high"
            and not _binding_is_revoked(candidate_store_key, candidate_account_key)
        ):
            normalized["account"]["store_key"] = candidate_store_key
        else:
            normalized["account"].pop("store_key", None)
            if _binding_is_revoked(candidate_store_key, candidate_account_key):
                normalized["identity_resolution"] = "binding_revoked"
    if source == "qianchuan" and isinstance(normalized.get("account"), dict):
        account = {**normalized["account"]}
        detected_key = str(account.get("key") or "").lower()
        canonical_key = _canonical_qianchuan_account_key(account)
        if canonical_key:
            account["key"] = canonical_key
        if canonical_key and detected_key and canonical_key != detected_key and SAFE_KEY.fullmatch(detected_key):
            account["aliases"] = list(dict.fromkeys([*(account.get("aliases") or []), detected_key]))[:20]
        normalized["account"] = account
        normalized_store_key = str((normalized.get("store") or {}).get("key") or "").strip().lower()
        if normalized_store_key and canonical_key and _binding_is_revoked(normalized_store_key, canonical_key):
            normalized["account"].pop("store_key", None)
            normalized["identity_resolution"] = "binding_revoked"
    if source == "qianchuan":
        raw_promotion = normalized.get("promotion_context") if isinstance(normalized.get("promotion_context"), dict) else {}
        raw_promotion_quality = raw_promotion.get("data_quality") if isinstance(raw_promotion.get("data_quality"), dict) else {}
        account_scope = raw_promotion.get("account_scope") if isinstance(raw_promotion.get("account_scope"), dict) else {}
        store_key = str((normalized.get("store") or {}).get("key") or account_scope.get("store_id") or "") if isinstance(normalized.get("store"), dict) else str(account_scope.get("store_id") or "")
        account_key = str((normalized.get("account") or {}).get("key") or account_scope.get("account_id") or "") if isinstance(normalized.get("account"), dict) else str(account_scope.get("account_id") or "")
        normalized_account_store = str((normalized.get("account") or {}).get("store_key") or "").strip().lower()
        binding_verified = bool(
            store_key
            and account_key
            and normalized_account_store == str(store_key).strip().lower()
            and normalized.get("identity_resolution") not in {"conflict", "binding_revoked", "binding_epoch_stale"}
            and not _binding_is_revoked(store_key, account_key)
        )
        snapshot_quality = normalized.get("quality") if isinstance(normalized.get("quality"), dict) else {}
        normalized["promotion_context"] = build_promotion_context({
            **raw_promotion,
            "account_scope": {
                **account_scope,
                "store_id": store_key,
                "account_id": account_key,
                "binding_status": "verified" if binding_verified else "unverified",
                "conflict": normalized.get("identity_resolution") in {"conflict", "binding_revoked", "binding_epoch_stale"},
            },
            "data_quality": {
                **raw_promotion_quality,
                "confidence": str(snapshot_quality.get("confidence") or ("high" if int(snapshot_quality.get("score") or 0) >= 80 else "medium" if int(snapshot_quality.get("score") or 0) >= 60 else "low")),
                "completeness": min(1.0, max(0.0, float(snapshot_quality.get("score") or 0) / 100)),
                "freshness_seconds": max(0, int((now_ms - captured_at_ms) / 1000)),
                "freshness_provided": True,
                "metric_conflict": raw_promotion_quality.get("metric_conflict") is True or normalized.get("identity_resolution") in {"conflict", "binding_epoch_stale"},
                "mode_conflict": raw_promotion_quality.get("mode_conflict") is True,
            },
        })
    expected_store_key = str(expected_store_key or "").strip().lower()
    expected_account_key = str(expected_account_key or "").strip().lower()
    for scope_name, scope_key in (("store", expected_store_key), ("account", expected_account_key)):
        if scope_key and not SAFE_KEY.fullmatch(scope_key):
            raise ValueError(f"invalid expected_{scope_name}_key")
    actual_store_key = str((normalized.get("store") or {}).get("key") or "").lower()
    actual_account_key = str((normalized.get("account") or {}).get("key") or "").lower()
    quarantine_active = normalized.get("identity_resolution") in {"conflict", "binding_revoked", "binding_epoch_stale"}
    binding_scope: dict[str, Any] | None = None
    if (
        source == "qianchuan"
        and not quarantine_active
        and SAFE_KEY.fullmatch(actual_store_key)
        and SAFE_KEY.fullmatch(actual_account_key)
    ):
        binding_generation = _binding_generation(actual_store_key, actual_account_key)
        if binding_generation < 0:
            normalized["identity_resolution"] = "binding_revoked"
            quarantine_active = True
        else:
            relation = _load_binding_revocations().get(f"{actual_store_key}:{actual_account_key}") or {}
            linked_at_ms = int(relation.get("linked_at_ms") or 0)
            if binding_generation > 0 and linked_at_ms > 0 and captured_at_ms < linked_at_ms:
                # The browser may finish a collection after the account was
                # rebound. Its capture time proves that the evidence belongs to
                # the previous lease, so never re-mint it into the new epoch.
                normalized["identity_resolution"] = "binding_epoch_stale"
                quarantine_active = True
            else:
                binding_scope = {
                    "store_key": actual_store_key,
                    "account_key": actual_account_key,
                    "binding_generation": binding_generation,
                    "linked_at_ms": linked_at_ms,
                    "authority": "agent_binding_registry",
                }
    # A genuine identity conflict is useful forensic evidence. Preserve it in
    # the quarantine partition even when it cannot satisfy the active scan
    # lease, but never mint a binding scope or publish it as current data.
    if not quarantine_active:
        if expected_store_key and not actual_store_key:
            raise ValueError("STORE_IDENTITY_UNRESOLVED: 当前页面无法证明属于本轮已选店铺，候选数据未写入。")
        if expected_store_key and actual_store_key != expected_store_key:
            raise ValueError("STORE_MISMATCH: 当前页面所属店铺与本轮已选店铺不一致，候选数据未写入。")
        if source == "qianchuan" and expected_account_key and not actual_account_key:
            raise ValueError("ACCOUNT_UNRESOLVED: 当前页面无法证明属于本轮千川账号，候选数据未写入。")
        if source == "qianchuan" and expected_account_key and actual_account_key != expected_account_key:
            raise ValueError("ACCOUNT_MISMATCH: 当前千川页面与本轮锁定账号不一致，候选数据未写入。")
    if quarantine_active:
        normalized["quarantine"] = {
            "active": True,
            "reason": (
                "binding_revoked"
                if normalized.get("identity_resolution") == "binding_revoked"
                else "binding_epoch_stale"
                if normalized.get("identity_resolution") == "binding_epoch_stale"
                else "identity_conflict"
            ),
            "excluded_from_current_data": True,
            "expected_store_key": expected_store_key,
            "expected_account_key": expected_account_key if source == "qianchuan" else "",
        }
    payload = {
        "source": source,
        "page_type": page_type,
        "data": normalized,
        "timestamp": time.time(),
        "saved_at": _now_label(),
        **({"binding_scope": binding_scope} if binding_scope else {}),
        "storage_provenance": {
            "producer": "agent_oauth_client_v1" if trusted_origin == "official_api_oauth_client" else "browser_bridge_v1",
            "trusted_official_api": trusted_origin == "official_api_oauth_client",
            "persistence_policy": "sqlite_first_fail_closed",
            "authoritative_source": "sqlite",
            "compatibility_mirror_policy": "best_effort_non_authoritative",
        },
    }
    primary_snapshot_path = _snapshot_path(source, page_type)
    store = normalized.get("store") if isinstance(normalized.get("store"), dict) else None
    account = normalized.get("account") if isinstance(normalized.get("account"), dict) else None
    if quarantine_active:
        quarantine_path = _quarantine_snapshot_path(source, page_type, captured_at_ms, payload)
        try:
            _local_store().persist_snapshot_bundle(
                payload,
                quarantine_path,
                snapshot_type=f"quarantine_{page_type}",
                commerce=None,
            )
            _atomic_json_write(quarantine_path, payload)
        except (LocalStoreError, OSError) as error:
            logger.exception("身份冲突快照隔离失败: %s/%s", source, page_type)
            raise LocalStoreError("身份冲突快照隔离失败，本次采集未生效。") from error
        logger.warning("已隔离身份冲突快照，未覆盖当前经营数据: %s/%s", source, page_type)
        return payload
    try:
        # Commit the durable snapshot and commerce graph before exposing the
        # compatibility JSON. A failed database transaction must never create
        # a newer UI-visible recommendation without an auditable source row.
        persistence_path = (
            _account_snapshot_path(str(account["key"]), page_type)
            if source == "qianchuan" and account
            else _store_snapshot_path(str(store["key"]), source, page_type)
            if store
            else primary_snapshot_path
        )
        local_store = _local_store()
        commerce_entities = normalized.get("commerce_entities") if isinstance(normalized.get("commerce_entities"), list) else []
        commerce_relations = normalized.get("commerce_relations") if isinstance(normalized.get("commerce_relations"), list) else []
        store_key = str((store or {}).get("key") or "").lower()
        account_key = str((account or {}).get("key") or "").lower()
        commerce_bundle = None
        if commerce_entities and store_key and normalized.get("identity_resolution") != "conflict" and (source != "qianchuan" or account_key):
            commerce_bundle = {
                "store_key": store_key,
                "account_key": account_key if source == "qianchuan" else "",
                "entities": commerce_entities,
                "relations": commerce_relations,
                "observations": extract_commerce_observations(normalized),
                "source": source,
                "page_type": page_type,
                "captured_at_ms": captured_at_ms,
                "identity_key_fingerprint": hashlib.sha256(_identity_secret()).hexdigest()[:16],
            }
        local_store.persist_snapshot_bundle(
            payload,
            persistence_path,
            snapshot_type=page_type,
            commerce=commerce_bundle,
        )
    except (LocalStoreError, OSError) as error:
        logger.exception("本地数据库或经营记忆写入失败，本次快照未对工作台可见: %s/%s", source, page_type)
        raise LocalStoreError("本地数据库写入失败，本次采集未生效；请检查磁盘后重试。") from error
    mirror_warnings: list[str] = []

    def best_effort_mirror(label: str, operation: Callable[[], None]) -> None:
        try:
            operation()
        except OSError:
            mirror_warnings.append(label)
            logger.warning(
                "SQLite 已提交，但兼容镜像写入失败（不影响权威快照）: %s/%s %s",
                source,
                page_type,
                label,
            )

    best_effort_mirror("primary_snapshot", lambda: _atomic_json_write(primary_snapshot_path, payload))
    if store and SAFE_KEY.fullmatch(str(store.get("key") or "")):
        best_effort_mirror(
            "store_snapshot",
            lambda: _atomic_json_write(
                _store_snapshot_path(str(store["key"]), source, page_type), payload
            ),
        )
    if source == "qianchuan" and account:
        account_key = str(account.get("key") or "").lower()
        if SAFE_KEY.fullmatch(account_key):
            best_effort_mirror(
                "account_snapshot",
                lambda: _atomic_json_write(_account_snapshot_path(account_key, page_type), payload),
            )
    if source == "qianchuan" and store and account:
        best_effort_mirror(
            "store_account_catalog",
            lambda: _remember_qianchuan_identity_pair(store, account),
        )
    else:
        if source == "qianchuan" and account:
            best_effort_mirror("account_catalog", lambda: _remember_qianchuan_account(account))
        if store:
            best_effort_mirror("store_catalog", lambda: _remember_store_identity(store, ""))
    # Backward-compatible latest snapshot for existing MCP clients.
    best_effort_mirror(
        "legacy_latest_snapshot",
        lambda: _atomic_json_write(DATA_DIR / f"{source}.json", payload),
    )
    best_effort_mirror("history_point", lambda: _save_history_point(payload))
    if mirror_warnings:
        payload["post_commit_warnings"] = [
            {
                "code": "JSON_COMPATIBILITY_MIRROR_DEGRADED",
                "message": "SQLite 权威快照已保存；兼容镜像将在后续采集时重试。",
                "targets": mirror_warnings,
            }
        ]
    logger.info("已保存 %s/%s 快照（质量 %s）", source, page_type, normalized.get("quality", {}).get("score", "-"))
    return payload


def load_data(source: str, page_type: str | None = None, account_key: str | None = None, store_key: str | None = None) -> dict[str, Any] | None:
    """Load committed Agent data from SQLite, never from the JSON mirror."""

    if source not in ALLOWED_SOURCES:
        return None
    settings = load_agent_settings()
    selected_store = str(store_key if store_key is not None else settings.get("store_key") or "").lower()
    selected_account = account_key
    source_path: Path | None = None
    source_directory: Path | None = None
    source_name: str | None = None
    snapshot_type: str | None = None
    account_filter: str | None = None
    qianchuan_scope: dict[str, Any] | None = None
    if source == "qianchuan" and selected_account is None:
        selected_account = str(settings.get("qianchuan_account_key") or "")
    if source == "qianchuan":
        safe_account = str(selected_account or "").lower()
        if not SAFE_KEY.fullmatch(selected_store) or not SAFE_KEY.fullmatch(safe_account):
            return None
        qianchuan_scope = _active_qianchuan_binding_scope(selected_store, safe_account)
        if not qianchuan_scope:
            return None
    if source == "qianchuan" and selected_account:
        safe_account = str(selected_account).lower()
        if not SAFE_KEY.fullmatch(safe_account):
            return None
        # Account identity is verified from the committed payload as well as
        # the path. This also migrates reads from older store-first SQLite rows.
        source_name = source
        snapshot_type = _safe_page_type(page_type) if page_type else None
        account_filter = safe_account
    elif source == "doudian" and selected_store:
        if not SAFE_KEY.fullmatch(selected_store):
            return None
        if page_type:
            source_path = _store_snapshot_path(selected_store, source, _safe_page_type(page_type))
        else:
            source_directory = DATA_DIR / "stores" / selected_store / source
    else:
        # Without an explicitly selected Qianchuan account, only genuine
        # legacy-unscoped rows are eligible. Never pick the newest row across
        # account partitions.
        source_directory = DATA_DIR / source
        snapshot_type = _safe_page_type(page_type) if page_type else None
    try:
        store = _local_store()
        if source == "qianchuan" and source_name and snapshot_type is None:
            # Quarantine rows use their own snapshot types and may be newer than
            # the last valid operating snapshot. Select the newest exact-lease
            # candidate rather than allowing forensic evidence to shadow or
            # replace current data.
            candidates = [
                candidate
                for candidate in store.iter_latest_snapshots(
                    source_name=source_name,
                    account_key=account_filter,
                )
                if _snapshot_matches_qianchuan_binding(candidate.get("payload"), qianchuan_scope)
            ]
            row = max(
                candidates,
                key=lambda candidate: (
                    float(candidate.get("captured_at") or 0),
                    int(candidate.get("id") or 0),
                ),
                default=None,
            )
        else:
            row = (
                store.latest_snapshot(source_path=source_path)
                if source_path is not None
                else store.latest_snapshot(
                    source_directory=source_directory,
                    snapshot_type=snapshot_type,
                )
                if source_directory is not None
                else store.latest_snapshot(
                    source_name=source_name,
                    snapshot_type=snapshot_type,
                    account_key=account_filter,
                )
            )
        value = row.get("payload") if row else None
        if source == "qianchuan" and not _snapshot_matches_qianchuan_binding(value, qianchuan_scope):
            return None
        return value if isinstance(value, dict) else None
    except (LocalStoreError, OSError):
        logger.exception("读取 SQLite 权威快照失败: %s/%s", source, page_type or "latest")
        return None


def _current_promotion_context(account_key: str | None = None) -> Any:
    snapshot = load_data("qianchuan", account_key=account_key)
    data = (snapshot or {}).get("data") if isinstance(snapshot, dict) else {}
    context = data.get("promotion_context") if isinstance(data, dict) else None
    if not isinstance(context, dict):
        return None
    # The browser capture time, not the later disk-save time, determines
    # whether evidence is still safe for an execution gate.
    captured_at_seconds = _timestamp_seconds(data.get("captured_at")) or _timestamp_seconds((snapshot or {}).get("timestamp"))
    if captured_at_seconds <= 0:
        return context
    quality = context.get("data_quality") if isinstance(context.get("data_quality"), dict) else {}
    now_seconds = time.time()
    future_timestamp = captured_at_seconds - now_seconds > MAX_CAPTURE_FUTURE_SKEW_MS / 1000
    freshness_seconds = (
        PLAN_CONSOLE_STALE_SECONDS + 1
        if future_timestamp
        else max(0, int(now_seconds - captured_at_seconds))
    )
    return {
        **context,
        "data_quality": {
            **quality,
            "freshness_seconds": freshness_seconds,
            "freshness_provided": True,
            "timestamp_conflict": future_timestamp,
        },
    }


_CHENGFANG_CONTEXT_UNSET = object()


def _current_chengfang_runtime(context: Any = _CHENGFANG_CONTEXT_UNSET) -> ChengfangAutopilotRuntime:
    return ChengfangAutopilotRuntime(
        DATA_DIR,
        _current_promotion_context() if context is _CHENGFANG_CONTEXT_UNSET else context,
    )


def _current_chengfang_control_task_center(
    runtime: ChengfangAutopilotRuntime | None = None,
) -> ChengfangControlTaskCenter:
    active_runtime = runtime or _current_chengfang_runtime()
    return ChengfangControlTaskCenter(DATA_DIR, active_runtime.scope_fingerprint)


def _current_chengfang_schedule_control(
    runtime: ChengfangAutopilotRuntime | None = None,
) -> ChengfangScheduleControl:
    active_runtime = runtime or _current_chengfang_runtime()
    return ChengfangScheduleControl(DATA_DIR, active_runtime.scope_fingerprint, active_runtime.pilot_plan_key)


def _importable_chengfang_control_candidates(
    runtime: ChengfangAutopilotRuntime,
) -> list[dict[str, Any]]:
    """Expose only opaque, reduction-only A1 candidates to the local center."""

    state = runtime.load()
    candidates = []
    for item in reversed(state.get("candidates", [])):
        if not isinstance(item, dict) or item.get("action_type") != "adjust_total_budget":
            continue
        if item.get("state") not in {"shadow_candidate", "accepted_for_pilot"}:
            continue
        try:
            current_value = float(item.get("current_value"))
            target_value = float(item.get("target_value"))
        except (TypeError, ValueError):
            continue
        if current_value <= 0 or target_value <= 0 or target_value >= current_value:
            continue
        candidate_id = str(item.get("candidate_id") or "")
        plan_key = str(item.get("plan_key") or "")
        if not candidate_id or not plan_key:
            continue
        candidates.append({
            "candidate_id": candidate_id,
            "family": "budget",
            "operation": "DECREASE",
            "plan_label": f"计划 {plan_key[-6:].upper()}",
            "current_value": current_value,
            "target_value": target_value,
            "reason_codes": [str(reason) for reason in item.get("reasons", [])[:8]],
            "evidence_source": str(item.get("evidence_source") or "a1_shadow"),
            "evidence_captured_at_ms": int(item.get("created_at") or 0),
        })
    return candidates[:10]


def build_current_chengfang_control_tasks() -> dict[str, Any]:
    runtime = _current_chengfang_runtime()
    candidates = _importable_chengfang_control_candidates(runtime)
    return _current_chengfang_control_task_center(runtime).summary(importable_candidates=candidates)


def build_current_chengfang_schedule_control() -> dict[str, Any]:
    runtime = _current_chengfang_runtime()
    return _current_chengfang_schedule_control(runtime).summary()


def create_chengfang_control_task(value: Any) -> dict[str, Any]:
    """Create a local draft, pinning candidate-derived fields server-side."""

    raw = value if isinstance(value, dict) else {}
    runtime = _current_chengfang_runtime()
    candidate_id = str(raw.get("candidate_id") or "").strip()
    draft_payload = dict(raw)
    if candidate_id:
        state = runtime.load()
        candidate = next((
            item for item in state.get("candidates", [])
            if isinstance(item, dict) and item.get("candidate_id") == candidate_id
        ), None)
        if candidate is None:
            raise ValueError("未找到对应的 A1 乘方候选")
        if candidate.get("action_type") != "adjust_total_budget":
            raise ValueError("当前只允许导入降预算候选")
        current_value = float(candidate.get("current_value"))
        target_value = float(candidate.get("target_value"))
        if current_value <= 0 or target_value <= 0 or target_value >= current_value:
            raise ValueError("候选不符合首发版降预算边界")
        draft_payload = {
            "family": "budget",
            "operation": "DECREASE",
            "current_value": current_value,
            "target_value": target_value,
            "plan_key": str(candidate.get("plan_key") or ""),
            "candidate_id": candidate_id,
            "reason_codes": candidate.get("reasons") if isinstance(candidate.get("reasons"), list) else [],
            "evidence": {
                "source": str(candidate.get("evidence_source") or "a1_shadow"),
                "captured_at_ms": int(candidate.get("created_at") or 0),
            },
        }
    elif raw.get("use_current_plan") is True:
        if not runtime.pilot_plan_key:
            raise ValueError("当前店铺与千川账户作用域尚未绑定")
        draft_payload = {
            "family": raw.get("family"),
            "operation": raw.get("operation"),
            "current_value": raw.get("current_value"),
            "target_value": raw.get("target_value"),
            "plan_key": runtime.pilot_plan_key,
            "reason_codes": ["OPERATOR_LOCAL_REHEARSAL"],
            "evidence": {
                "source": "operator_local_rehearsal",
                "captured_at_ms": int(time.time() * 1000),
            },
        }
    else:
        raise ValueError("控制任务必须来自当前 A1 候选或当前已绑定计划作用域")
    return _current_chengfang_control_task_center(runtime).create_draft(draft_payload)


def _current_chengfang_snapshots(account_key: str = "") -> list[dict[str, Any]]:
    """Load the selected account's candidate evidence snapshots once."""

    return [
        snapshot
        for page_type in ("qianchuan_live", "campaigns", "plans", "overview", "report")
        if (snapshot := load_data("qianchuan", page_type, account_key=account_key or None)) is not None
    ]


def _resolve_current_chengfang_session() -> tuple[str, Any, ChengfangAutopilotRuntime]:
    """Pin one account/context/runtime tuple for the duration of an operation."""

    settings = load_agent_settings()
    account_key = str(settings.get("qianchuan_account_key") or "")
    context = _current_promotion_context(account_key or None)
    runtime = _current_chengfang_runtime(context)
    return account_key, context, runtime


def _current_chengfang_production_components() -> tuple[Any, Any, Any]:
    """Build the private official-write chain inside the locked current scope.

    Raw platform identifiers are resolved only inside the target service and
    official adapter.  The HTTP layer receives opaque target/operation keys.
    """

    if (
        not COMMERCIAL_RUNTIME_LOADED
        or ChengfangProductionTargetService is None
        or OfficialChengfangBudgetAdapter is None
        or ChengfangProductionController is None
    ):
        raise ChengfangProductionControllerError("COMMERCIAL_RUNTIME_NOT_INCLUDED")
    store_key, account_key = _bound_current_scope()
    if not account_key:
        raise ChengfangProductionControllerError("QIANCHUAN_ACCOUNT_NOT_SELECTED")
    try:
        _assert_active_binding_registry_consistent()
        binding_scope = _active_qianchuan_binding_scope(store_key, account_key)
    except OSError:
        raise ChengfangProductionControllerError("ACCOUNT_BINDING_UNVERIFIED") from None
    if not isinstance(binding_scope, dict):
        raise ChengfangProductionControllerError("ACCOUNT_BINDING_UNVERIFIED")
    binding_generation = int(binding_scope.get("binding_generation") or 0)
    linked_at_ms = int(binding_scope.get("linked_at_ms") or 0)
    # Production writes require an explicit monotonic lease.  Generation-zero
    # compatibility bindings remain readable, but cannot authorize spending.
    if binding_generation <= 0 or linked_at_ms <= 0:
        raise ChengfangProductionControllerError("PRODUCTION_SCOPE_LEASE_REQUIRED")

    def assert_live_binding_scope() -> None:
        try:
            _assert_active_binding_registry_consistent()
            current = _active_qianchuan_binding_scope(store_key, account_key)
        except OSError:
            raise ChengfangProductionTargetError("SCOPE_LEASE_REVOKED") from None
        if (
            not isinstance(current, dict)
            or int(current.get("binding_generation") or 0) != binding_generation
            or int(current.get("linked_at_ms") or 0) != linked_at_ms
        ):
            raise ChengfangProductionTargetError("SCOPE_LEASE_REVOKED")

    snapshot_store = _local_store()
    snapshot_store.initialize()
    targets = ChengfangProductionTargetService(
        DATA_DIR,
        snapshot_store,
        _identity_secret(),
    )

    def resolve_adapter_target(operation: dict[str, Any]) -> dict[str, Any]:
        assert_live_binding_scope()
        resolve_exact = getattr(targets, "resolve_exact", None)
        if callable(resolve_exact):
            resolved = resolve_exact(
                target_key=operation.get("target_key"),
                snapshot_fingerprint=operation.get("snapshot_fingerprint"),
                expected_account_key=account_key,
                expected_store_key=store_key,
                expected_current_budget=operation.get("current_value"),
            )
        else:
            # Compatibility with an older private target service.  The
            # controller serializes this fallback, but current builds use the
            # operation-bound resolver above and never depend on a mutable
            # process-wide selection during execution or reconciliation.
            targets.select(
                operation.get("target_key"),
                operation.get("snapshot_fingerprint"),
                account_key=account_key,
                store_key=store_key,
            )
            resolved = targets.resolve(
                expected_account_key=account_key,
                expected_store_key=store_key,
                expected_current_budget=operation.get("current_value"),
            )
        if str(resolved.get("target_key") or "") != str(operation.get("target_key") or ""):
            raise ChengfangProductionTargetError("SELECTED_TARGET_MISMATCH")
        if int(resolved.get("binding_generation") or -1) != binding_generation:
            raise ChengfangProductionTargetError("SCOPE_LEASE_REVOKED")
        if int(resolved.get("binding_linked_at_ms") or -1) != linked_at_ms:
            raise ChengfangProductionTargetError("SCOPE_LEASE_REVOKED")
        assert_live_binding_scope()
        return resolved

    def resolve_recovery_adapter_target(operation: dict[str, Any]) -> dict[str, Any]:
        assert_live_binding_scope()
        resolve_for_recovery = getattr(targets, "resolve_for_recovery", None)
        if not callable(resolve_for_recovery):
            return resolve_adapter_target(operation)
        resolved = resolve_for_recovery(
            target_key=operation.get("target_key"),
            expected_account_key=account_key,
            expected_store_key=store_key,
        )
        if str(resolved.get("target_key") or "") != str(operation.get("target_key") or ""):
            raise ChengfangProductionTargetError("RECOVERY_TARGET_MISMATCH")
        if int(resolved.get("binding_generation") or -1) != binding_generation:
            raise ChengfangProductionTargetError("SCOPE_LEASE_REVOKED")
        if int(resolved.get("binding_linked_at_ms") or -1) != linked_at_ms:
            raise ChengfangProductionTargetError("SCOPE_LEASE_REVOKED")
        assert_live_binding_scope()
        return resolved

    def write_scope_lease(operation: dict[str, Any]):
        operation_generation = operation.get("binding_generation")
        operation_linked_at_ms = operation.get("binding_linked_at_ms")

        def validate_operation_lease() -> None:
            if (
                isinstance(operation_generation, bool)
                or not isinstance(operation_generation, int)
                or operation_generation != binding_generation
                or isinstance(operation_linked_at_ms, bool)
                or not isinstance(operation_linked_at_ms, int)
                or operation_linked_at_ms != linked_at_ms
            ):
                raise ChengfangProductionTargetError("SCOPE_LEASE_REVOKED")
            assert_live_binding_scope()

        return _binding_execution_lease(
            hold_state_lock=True,
            validator=validate_operation_lease,
        )

    adapter = OfficialChengfangBudgetAdapter(
        OceanEngineOAuth(DATA_DIR),
        resolve_adapter_target,
        recovery_target_resolver=resolve_recovery_adapter_target,
        write_scope_lease=write_scope_lease,
    )
    controller = ChengfangProductionController(
        DATA_DIR,
        targets,
        adapter,
        account_key=account_key,
        store_key=store_key,
        integrity_key=_identity_secret(),
        binding_generation=binding_generation,
        binding_linked_at_ms=linked_at_ms,
    )
    return targets, adapter, controller


def build_current_chengfang_production_write() -> dict[str, Any]:
    """Return the browser-safe status of the one-plan production canary."""

    base: dict[str, Any] = {
        "schema_version": 1,
        "available": False,
        "write_scope": "one_plan_budget_decrease",
        "official_api_only": True,
        "dom_write_fallback_allowed": False,
        "bulk_write_enabled": False,
        "budget_increase_enabled": False,
        "raw_identifiers_exposed": False,
        "targets": [],
        "target_count": 0,
        "operations": [],
        "operation_count": 0,
        "status_counts": {},
        "inflight_count": 0,
        "global_single_flight": True,
        "kill_switch": {
            "active": False,
            "activated_at_ms": None,
            "reason": "",
            "resumed_at_ms": None,
            "resume_confirmation_phrase": None,
        },
        "blockers": [],
    }
    if not COMMERCIAL_RUNTIME_LOADED:
        return {**base, "blockers": ["COMMERCIAL_RUNTIME_NOT_INCLUDED"]}
    try:
        global_summary = ChengfangProductionController.inspect_global_summary(
            DATA_DIR,
            integrity_key=_identity_secret(),
        )
    except (ChengfangProductionControllerError, OSError, ValueError):
        return {**base, "blockers": ["PRODUCTION_WRITE_STATUS_UNAVAILABLE"]}
    global_kill_switch = (
        global_summary.get("kill_switch")
        if isinstance(global_summary.get("kill_switch"), dict)
        else base["kill_switch"]
    )
    global_status = {
        "operations": global_summary.get("operations")
        if isinstance(global_summary.get("operations"), list)
        else [],
        "operation_count": int(global_summary.get("operation_count") or 0),
        "status_counts": global_summary.get("status_counts")
        if isinstance(global_summary.get("status_counts"), dict)
        else {},
        "inflight_count": int(global_summary.get("inflight_count") or 0),
        "global_single_flight": global_summary.get("global_single_flight") is True,
        "kill_switch": global_kill_switch,
    }
    global_blockers: list[str] = []
    if int((global_status["status_counts"] or {}).get("unknown") or 0):
        global_blockers.append("UNKNOWN_WRITE_REQUIRES_RECONCILIATION")
    if global_kill_switch.get("active") is True:
        global_blockers.append("PRODUCTION_WRITE_KILL_SWITCH_ACTIVE")
    settings = load_agent_settings()
    store_key = str(settings.get("store_key") or "").strip().lower()
    account_key = str(settings.get("qianchuan_account_key") or "").strip().lower()
    if not store_key:
        return {
            **base,
            **global_status,
            "blockers": [*global_blockers, "STORE_NOT_SELECTED"],
        }
    if not account_key:
        return {
            **base,
            **global_status,
            "blockers": [*global_blockers, "QIANCHUAN_ACCOUNT_NOT_SELECTED"],
        }
    try:
        oauth_status = OceanEngineOAuth(DATA_DIR).status()
        targets, adapter, controller = _current_chengfang_production_components()
        discovery = targets.discover(account_key=account_key, store_key=store_key)
        summary = controller.summary()
        capabilities = adapter.capabilities()
    except (ChengfangProductionControllerError, ChengfangProductionTargetError, LocalStoreError, OSError, ValueError):
        return {
            **base,
            **global_status,
            "oauth_connected": False,
            "blockers": [*global_blockers, "PRODUCTION_WRITE_STATUS_UNAVAILABLE"],
        }
    blockers = list(discovery.get("blockers") or [])
    if oauth_status.get("connected") is not True:
        blockers.append("OCEANENGINE_OAUTH_REQUIRED")
    unknown_count = int((summary.get("status_counts") or {}).get("unknown") or 0)
    if unknown_count and "UNKNOWN_WRITE_REQUIRES_RECONCILIATION" not in blockers:
        blockers.append("UNKNOWN_WRITE_REQUIRES_RECONCILIATION")
    kill_switch = summary.get("kill_switch") if isinstance(summary.get("kill_switch"), dict) else {}
    if kill_switch.get("active") is True:
        blockers.append("PRODUCTION_WRITE_KILL_SWITCH_ACTIVE")
    targets_public = discovery.get("targets") if isinstance(discovery.get("targets"), list) else []
    operations = summary.get("operations") if isinstance(summary.get("operations"), list) else []
    # Preserve recovery priority: an uncertain write or global stop must stay
    # ahead of setup issues such as a later-cleared account binding.  Sorting
    # alphabetically could send the operator into OAuth/setup while the real
    # safety incident remained hidden below it.
    effective_blockers = list(dict.fromkeys([*global_blockers, *blockers]))
    return {
        **base,
        "available": bool(
            capabilities.get("available") is True
            and discovery.get("ok") is True
            and oauth_status.get("connected") is True
            and not unknown_count
            and not effective_blockers
        ),
        "oauth_connected": oauth_status.get("connected") is True,
        "account_permission_verified_during_execution": True,
        "adapter": capabilities,
        "targets": targets_public,
        "target_count": len(targets_public),
        "operations": operations,
        "operation_count": len(operations),
        "status_counts": summary.get("status_counts") or {},
        "inflight_count": summary.get("inflight_count") or 0,
        "global_single_flight": summary.get("global_single_flight") is True,
        "kill_switch": kill_switch,
        "blockers": effective_blockers,
    }


def _hydrate_resolved_chengfang_profile(
    runtime: ChengfangAutopilotRuntime,
    context: Any,
    account_key: str,
    *,
    save: bool = False,
) -> dict[str, Any]:
    """Hydrate a profile without re-resolving a possibly switched account."""

    snapshots = _current_chengfang_snapshots(account_key)
    current = runtime.summary(build_chengfang_contract_registry()).get("profile", {})
    scope = (context or {}).get("account_scope") if isinstance((context or {}).get("account_scope"), dict) else {}
    hydrated = hydrate_chengfang_profile(current, snapshots, expected_scope={
        "store_id": scope.get("store_id"),
        "account_id": scope.get("account_id"),
        "strategy_id": (context or {}).get("strategy_id"),
        "promotion_mode": (context or {}).get("promotion_mode"),
        "metric_contract_version": ((context or {}).get("metric_contract") or {}).get("version"),
    })
    if save and hydrated.get("status") in {"hydrated", "partial"}:
        runtime.save_profile(hydrated["profile"], trusted_evidence=True)
        hydrated["profile"] = runtime.summary(build_chengfang_contract_registry()).get("profile", hydrated["profile"])
        hydrated["saved"] = True
    else:
        hydrated["saved"] = False
    return hydrated


def hydrate_current_chengfang_profile(*, save: bool = False) -> dict[str, Any]:
    """Hydrate the local profile from one pinned, fresh read-only session."""

    account_key, context, runtime = _resolve_current_chengfang_session()
    return _hydrate_resolved_chengfang_profile(runtime, context, account_key, save=save)


def build_current_chengfang_autopilot(context: Any = None) -> dict[str, Any]:
    active_context = _current_promotion_context() if context is None else context
    return _current_chengfang_runtime(active_context).summary(build_chengfang_contract_registry())


def run_chengfang_autopilot_cycle(trigger: str = "scheduler") -> dict[str, Any]:
    """Refresh local evidence and run A1/A2 safety without platform writes."""

    account_key, context, runtime = _resolve_current_chengfang_session()
    hydration = _hydrate_resolved_chengfang_profile(runtime, context, account_key, save=True)
    safety = runtime.enforce_a2_safety()
    return {**runtime.evaluate(trigger=trigger), "evidence_hydration": hydration, "a2_safety": safety}


def build_current_promotion_readiness() -> dict[str, Any]:
    """Expose the latest explicit mode evidence without assuming platform fields."""

    snapshot = load_data("qianchuan")
    data = (snapshot or {}).get("data") if isinstance(snapshot, dict) else {}
    context = data.get("promotion_context") if isinstance(data, dict) else None
    readiness = build_chengfang_readiness(context)
    field_contract = build_chengfang_contract_registry()
    autopilot_runtime = _current_chengfang_runtime(context).summary(field_contract)
    return {
        **readiness,
        "dashboard": build_chengfang_dashboard_summary(context),
        "field_contract": field_contract,
        "page_fingerprint": assess_page_fingerprint(None),
        "autopilot": autopilot_runtime["readiness"],
        "autopilot_runtime": autopilot_runtime,
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
    try:
        qianchuan_scope = _active_qianchuan_binding_scope(selected_store, selected_account)
    except OSError:
        logger.exception("Unable to resolve the active store/account binding while listing snapshots")
        return []
    store = _local_store()
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
        if source == "qianchuan" and not qianchuan_scope:
            continue
        try:
            rows = (
                store.iter_latest_snapshots(
                    source_name="qianchuan",
                    account_key=selected_account,
                )
                if source == "qianchuan" and selected_account
                else store.iter_latest_snapshots(source_directory=source_dir)
            )
            authoritative_rows = list(rows)
        except (LocalStoreError, OSError):
            logger.exception("读取 SQLite 权威快照目录失败: %s", source_dir)
            return []
        for row in authoritative_rows:
            snapshot = row.get("payload")
            if not isinstance(snapshot, dict):
                return []
            if source == "qianchuan" and not _snapshot_matches_qianchuan_binding(snapshot, qianchuan_scope):
                continue
            path = Path(str(row.get("source_path") or ""))
            data = snapshot.get("data", {})
            quality = data.get("quality", {}) if isinstance(data, dict) else {}
            captured_at = _timestamp_seconds(data.get("captured_at") if isinstance(data, dict) else 0)
            if captured_at <= 0:
                captured_at = _timestamp_seconds(snapshot.get("timestamp"))
            now_seconds = int(time.time())
            timestamp_conflict = captured_at <= 0 or captured_at - now_seconds > MAX_CAPTURE_FUTURE_SKEW_MS / 1000
            age = STALE_SECONDS + 1 if timestamp_conflict else max(0, now_seconds - captured_at)
            quality_score = int(quality.get("score", 0) or 0)
            items.append(
                {
                    "source": source,
                    "page_type": snapshot.get("page_type", path.stem),
                    "authoritative_source": "sqlite",
                    "saved_at": snapshot.get("saved_at"),
                    "captured_at": captured_at,
                    "age_seconds": age,
                    "fresh": age < STALE_SECONDS,
                    "timestamp_conflict": timestamp_conflict,
                    "rule_eligible": age < STALE_SECONDS and quality_score >= 60,
                    "title": data.get("title", "") if isinstance(data, dict) else "",
                    "url": data.get("url", "") if isinstance(data, dict) else "",
                    "quality_score": quality_score,
                    "metric_count": int(quality.get("metric_count", 0) or 0),
                    "row_count": int(quality.get("row_count", 0) or 0),
                    "warnings": quality.get("warnings", []),
                }
            )
    return sorted(items, key=lambda item: item.get("age_seconds", 10**9))


def _parse_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").replace("¥", "").replace("￥", "").strip()
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


def _history_point_matches_qianchuan_binding(
    point: Any,
    scope: dict[str, Any] | None,
) -> bool:
    """Keep trend history inside the exact current store/account lease."""

    if not isinstance(point, dict) or not isinstance(scope, dict):
        return False
    if (
        str(point.get("store_key") or "").strip().lower() != scope["store_key"]
        or str(point.get("account_key") or "").strip().lower() != scope["account_key"]
    ):
        return False
    current_generation = int(scope.get("binding_generation") or 0)
    binding_scope = point.get("binding_scope")
    if not isinstance(binding_scope, dict):
        return current_generation == 0
    generation = binding_scope.get("binding_generation")
    linked_at_ms = binding_scope.get("linked_at_ms")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or isinstance(linked_at_ms, bool)
        or not isinstance(linked_at_ms, int)
    ):
        return False
    return bool(
        str(binding_scope.get("store_key") or "").strip().lower() == scope["store_key"]
        and str(binding_scope.get("account_key") or "").strip().lower() == scope["account_key"]
        and generation == current_generation
        and linked_at_ms == int(scope.get("linked_at_ms") or 0)
    )


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
    if source == "qianchuan" and isinstance(snapshot.get("binding_scope"), dict):
        snapshot_scope = snapshot["binding_scope"]
        point["binding_scope"] = {
            "store_key": str(snapshot_scope.get("store_key") or "").strip().lower(),
            "account_key": str(snapshot_scope.get("account_key") or "").strip().lower(),
            "binding_generation": snapshot_scope.get("binding_generation"),
            "linked_at_ms": snapshot_scope.get("linked_at_ms"),
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
    selected_account = str(settings.get("qianchuan_account_key") or "").strip().lower()
    selected_store = str(settings.get("store_key") or "").strip().lower()
    try:
        qianchuan_scope = (
            _active_qianchuan_binding_scope(selected_store, selected_account)
            if SAFE_KEY.fullmatch(selected_store) and SAFE_KEY.fullmatch(selected_account)
            else None
        )
    except OSError:
        logger.exception("Unable to resolve the active store/account binding while loading history")
        qianchuan_scope = None
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
                if not isinstance(value, dict):
                    continue
                if source and value.get("source") != source:
                    continue
                if page_type and value.get("page_type") != page_type:
                    continue
                if selected_store and str(value.get("store_key") or "").strip().lower() != selected_store:
                    continue
                if value.get("source") == "qianchuan" and not _history_point_matches_qianchuan_binding(
                    value,
                    qianchuan_scope,
                ):
                    continue
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
        if item["source"] != source or item.get("rule_eligible") is not True:
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
        pack = _update_center().load_effective_pack(store_key=str(settings.get("store_key") or ""))
        result = RuleEngine(pack).evaluate(facts, rule_settings)
    except (UpdateError, RulePackError, ValueError, OSError):
        logger.exception("本地经营知识包判断失败，继续使用内置兼容诊断")
        return []
    alerts: list[dict[str, Any]] = []
    for item in result.get("diagnostics", []):
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        alerts.append({
            "level": "high" if item.get("level") in {"critical", "high"} else "warning" if item.get("level") == "medium" else "info",
            "confidence": "medium" if item.get("knowledge_layer") == "industry" else "high",
            "title": str(item.get("title") or "经营规则提醒"),
            "detail": str(item.get("message") or "本地经营知识包命中了一条规则。"),
            "action": str(action.get("label") or "请人工核对后处理"),
            "acceptance": item.get("acceptance") or {},
            "evidence": {
                "source": "knowledge_pack",
                "rule_id": item.get("rule_id"),
                "rule_version": item.get("rule_version"),
                "pack_version": item.get("knowledge_pack_version") or result.get("pack_version"),
                "pack_id": item.get("knowledge_pack_id") or str(pack.get("pack_id") or "general"),
                "knowledge_layer": item.get("knowledge_layer") or "general",
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
    fresh_business_snapshots = {
        (str(item.get("source") or ""), str(item.get("page_type") or ""))
        for item in catalog
        if item.get("fresh") is True
    }
    fresh_high_alerts = [
        item for item in alerts
        if item.get("level") == "high"
        and isinstance(item.get("evidence"), dict)
        and (
            str(item["evidence"].get("source") or ""),
            str(item["evidence"].get("page_type") or ""),
        ) in fresh_business_snapshots
    ]
    if not catalog:
        headline = "尚未收到经营数据"
        summary = "请打开已登录的抖店或千川后台，然后点击扩展中的“立即同步”。"
    elif fresh_count == 0:
        headline = "经营数据已过期，请先同步"
        summary = f"已找到 {len(catalog)} 类历史页面，但都已超过当前诊断时效；请先同步，再判断投放或经营异常。"
    elif fresh_high_alerts:
        headline = "今天先处理高优先级投放异常"
        summary = f"已覆盖 {len(catalog)} 类页面，其中 {fresh_count} 类数据在 10 分钟内更新。建议先核对证据，再执行调整。"
    elif fresh_count < len(catalog):
        headline = "部分经营数据需要更新"
        summary = f"已覆盖 {len(catalog)} 类页面，其中 {fresh_count} 类数据为最新；请先刷新过期页面，再统一判断经营优先级。"
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
    _recover_auto_store_context_transaction()
    _recover_binding_transaction()
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


def _requested_settings_scope_matches_locked(values: dict[str, Any]) -> bool:
    """Return true only when supplied scope fields equal the locked state."""

    current = load_agent_settings()
    for field_name in ("store_key", "qianchuan_account_key"):
        if field_name not in values:
            continue
        requested = str(values.get(field_name) or "").strip().lower()
        existing = str(current.get(field_name) or "").strip().lower()
        if requested != existing:
            return False
    return True


@_guard_production_scope_change("settings_scope_changed")
@_guard_binding_state_mutation
def _save_agent_settings_scope_change(values: dict[str, Any]) -> dict[str, Any]:
    """Persist a real store/account change under audit -> binding order."""

    with _state_lock:
        return _save_agent_settings_locked(values)


def save_agent_settings(values: dict[str, Any]) -> dict[str, Any]:
    """Validate, revoke stale grants and persist settings as one transition.

    A request that merely repeats the selected scope can stay on the binding
    lease.  A real store/account switch releases that lease and re-enters via
    the global production order (audit -> binding), so OAuth/UI changes cannot
    race an official write or create an audit/binding lock inversion.
    """

    has_scope_fields = isinstance(values, dict) and any(
        field_name in values for field_name in ("store_key", "qianchuan_account_key")
    )
    if has_scope_fields:
        with _binding_execution_lease(hold_state_lock=True):
            if _requested_settings_scope_matches_locked(values):
                return _save_agent_settings_locked(values)
        return _save_agent_settings_scope_change(values)
    with _binding_execution_lease(hold_state_lock=True):
        return _save_agent_settings_locked(values)


def _save_agent_settings_locked(values: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("settings must be an object")
    current = load_agent_settings()
    allowed = set(DEFAULT_AGENT_SETTINGS)
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")

    next_settings = {**current, **values}
    next_settings["roi_target"] = _bounded_setting_number(
        next_settings["roi_target"], field_name="roi_target", minimum=0.1, maximum=20.0
    )
    next_settings["min_spend_for_action"] = _bounded_setting_number(
        next_settings["min_spend_for_action"],
        field_name="min_spend_for_action",
        minimum=1.0,
        maximum=1_000_000.0,
    )
    next_settings["low_inventory_threshold"] = _bounded_setting_integer(
        next_settings["low_inventory_threshold"],
        field_name="low_inventory_threshold",
        minimum=0,
        maximum=1_000_000,
    )
    next_settings["critical_inventory_threshold"] = _bounded_setting_integer(
        next_settings["critical_inventory_threshold"],
        field_name="critical_inventory_threshold",
        minimum=0,
        maximum=1_000_000,
    )
    if next_settings["critical_inventory_threshold"] > next_settings["low_inventory_threshold"]:
        raise ValueError("critical_inventory_threshold must not exceed low_inventory_threshold.")
    next_settings["inventory_days_warning"] = _bounded_setting_number(
        next_settings["inventory_days_warning"],
        field_name="inventory_days_warning",
        minimum=0.1,
        maximum=365.0,
    )
    if "daily_report_enabled" in values and not isinstance(values["daily_report_enabled"], bool):
        raise ValueError("daily_report_enabled must be true or false")
    if not isinstance(next_settings["daily_report_enabled"], bool):
        # Fail closed if a legacy/corrupt settings file contains a truthy string.
        next_settings["daily_report_enabled"] = False
    if "binding_registry_initialized" in values and not isinstance(values["binding_registry_initialized"], bool):
        raise ValueError("binding_registry_initialized must be true or false")
    if current.get("binding_registry_initialized") is True and next_settings.get("binding_registry_initialized") is not True:
        raise ValueError("binding_registry_initialized cannot be disabled")
    next_settings["binding_registry_initialized"] = next_settings.get("binding_registry_initialized") is True
    report_time = str(next_settings["daily_report_time"])
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", report_time):
        raise ValueError("daily_report_time must be HH:MM")
    next_settings["daily_report_time"] = report_time
    next_settings["report_retention_days"] = _bounded_setting_integer(
        next_settings["report_retention_days"],
        field_name="report_retention_days",
        minimum=1,
        maximum=365,
    )
    report_template = str(next_settings.get("report_template") or "default")
    if report_template not in REPORT_TEMPLATE_KEYS:
        raise ValueError("report_template must be default, brief, handover or custom")
    next_settings["report_template"] = report_template
    custom_template = str(next_settings.get("custom_report_template") or "").strip()
    if len(custom_template) > 12_000:
        raise ValueError("custom_report_template is too long")
    next_settings["custom_report_template"] = custom_template or DEFAULT_CUSTOM_REPORT_TEMPLATE
    next_settings["history_retention_days"] = _bounded_setting_integer(
        next_settings["history_retention_days"],
        field_name="history_retention_days",
        minimum=1,
        maximum=365,
    )
    next_settings["max_daily_execution_count"] = _bounded_setting_integer(
        next_settings["max_daily_execution_count"],
        field_name="max_daily_execution_count",
        minimum=1,
        maximum=50,
    )
    next_settings["max_daily_budget_reduction"] = _bounded_setting_number(
        next_settings["max_daily_budget_reduction"],
        field_name="max_daily_budget_reduction",
        minimum=1.0,
        maximum=1_000_000.0,
    )
    next_settings["execution_cooldown_minutes"] = _bounded_setting_integer(
        next_settings["execution_cooldown_minutes"],
        field_name="execution_cooldown_minutes",
        minimum=0,
        maximum=1440,
    )
    execution_mode = str(next_settings.get("execution_mode") or "observe")
    if execution_mode not in {"observe", "shadow", "supervised"}:
        raise ValueError("execution_mode must be observe, shadow or supervised")
    next_settings["execution_mode"] = execution_mode
    execution_mode_revoked = (
        str(current.get("execution_mode") or "observe") == "supervised"
        and execution_mode != "supervised"
    )
    account_key = str(next_settings.get("qianchuan_account_key") or "").lower()
    if account_key and not SAFE_KEY.fullmatch(account_key):
        raise ValueError("invalid qianchuan_account_key")
    next_settings["qianchuan_account_key"] = account_key
    store_key = str(next_settings.get("store_key") or "").lower()
    if store_key and not SAFE_KEY.fullmatch(store_key):
        raise ValueError("invalid store_key")
    next_settings["store_key"] = store_key
    scope_changed = (
        store_key != str(current.get("store_key") or "").strip().lower()
        or account_key != str(current.get("qianchuan_account_key") or "").strip().lower()
    )
    if scope_changed:
        prepare_scope_change = globals().get("_prepare_execution_scope_change")
        if callable(prepare_scope_change):
            prepare_scope_change("settings_scope_changed")
    if execution_mode_revoked:
        invalidate_execution = globals().get("_invalidate_unconsumed_execution_authorization")
        if callable(invalidate_execution):
            invalidate_execution("execution_mode_revoked")
    _atomic_json_write(_settings_path(), next_settings)
    return next_settings


def _integrations_path() -> Path:
    return DATA_DIR / "integrations.json"


def _validate_integrations_path() -> Path:
    path = _integrations_path()
    parent = path.parent
    if parent.is_symlink() or not parent.exists() or not parent.is_dir():
        raise OSError("notification metadata parent is not a trusted directory")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise OSError("notification metadata path is not a regular file")
    return path


def _write_integration_metadata(value: dict[str, Any]) -> None:
    _atomic_json_write(_validate_integrations_path(), value)


def _integration_secret_store() -> IntegrationSecretStore:
    install_root = DATA_DIR.parent
    identity = ensure_install_auth(install_root)
    return IntegrationSecretStore(
        DATA_DIR,
        install_id=str(identity["install_id"]),
        install_root=install_root,
    )


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


def _empty_integration_values() -> dict[str, Any]:
    return {"feishu_webhook": "", "dingtalk_webhook": "", "auto_send_reports": False}


def _integration_destination_fingerprint(webhook: str) -> str:
    return hashlib.sha256(webhook.encode("utf-8")).hexdigest()[:16] if webhook else ""


def _validate_integration_secret_record(
    record: dict[str, Any],
    *,
    install_id: str,
) -> dict[str, Any]:
    if not isinstance(record, dict) or record.get("schema_version") != INTEGRATION_SECRET_SCHEMA_VERSION:
        raise IntegrationSecretStoreError("通知密钥记录格式无效；为保护 Webhook，本次读取已停止。")
    allowed = {
        "schema_version", "revision", "install_id", "auto_send_reports",
        "feishu_webhook", "dingtalk_webhook",
    }
    if set(record) - allowed:
        raise IntegrationSecretStoreError("通知密钥记录包含未知字段；为保护 Webhook，本次读取已停止。")
    revision = str(record.get("revision") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{32}", revision):
        raise IntegrationSecretStoreError("通知密钥记录缺少有效修订号；为保护 Webhook，本次读取已停止。")
    if str(record.get("install_id") or "").lower() != str(install_id or "").lower():
        raise IntegrationSecretStoreError("通知密钥记录不属于当前安装；为避免跨实例误发，本次读取已停止。")
    if not isinstance(record.get("auto_send_reports"), bool):
        raise IntegrationSecretStoreError("通知密钥记录中的发送设置无效；本次读取已停止。")
    values = _empty_integration_values()
    values["auto_send_reports"] = record["auto_send_reports"]
    try:
        for platform in ("feishu", "dingtalk"):
            key = f"{platform}_webhook"
            values[key] = _validate_webhook(platform, str(record.get(key) or ""))
    except ValueError as error:
        raise IntegrationSecretStoreError(
            "通知密钥记录未通过平台地址校验；为保护 Webhook，本次读取已停止。"
        ) from error
    return {**values, "revision": revision}


def _integration_metadata(values: dict[str, Any], revision: str, storage: str) -> dict[str, Any]:
    return {
        "schema_version": INTEGRATION_METADATA_SCHEMA_VERSION,
        "revision": revision,
        "auto_send_reports": bool(values.get("auto_send_reports")),
        "secret_storage": storage,
        "platforms": {
            platform: {
                "configured": bool(values.get(f"{platform}_webhook")),
                "fingerprint": _integration_destination_fingerprint(
                    str(values.get(f"{platform}_webhook") or "")
                ),
            }
            for platform in ("feishu", "dingtalk")
        },
    }


def _parse_integration_metadata(saved: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(saved, dict) or saved.get("schema_version") != INTEGRATION_METADATA_SCHEMA_VERSION:
        raise ValueError("通知连接元数据格式无效；为避免误发，本次读取已停止。")
    allowed = {
        "schema_version", "revision", "auto_send_reports", "secret_storage", "platforms",
    }
    if set(saved) - allowed:
        raise ValueError("通知连接元数据包含未知字段；为避免误发，本次读取已停止。")
    if not isinstance(saved.get("auto_send_reports"), bool):
        raise ValueError("通知连接元数据中的自动发送设置无效；本次读取已停止。")
    secret_storage = str(saved.get("secret_storage") or "").lower()
    if not re.fullmatch(r"[a-z0-9_]{1,32}", secret_storage):
        raise ValueError("通知连接元数据中的密钥存储标识无效；本次读取已停止。")
    revision = str(saved.get("revision") or "").lower()
    if revision and not re.fullmatch(r"[0-9a-f]{32}", revision):
        raise ValueError("通知连接元数据中的修订号无效；本次读取已停止。")
    platforms = saved.get("platforms")
    if not isinstance(platforms, dict):
        raise ValueError("通知连接元数据缺少平台状态；本次读取已停止。")
    if set(platforms) != {"feishu", "dingtalk"}:
        raise ValueError("通知连接元数据的平台集合无效；本次读取已停止。")
    configured: dict[str, bool] = {}
    for platform in ("feishu", "dingtalk"):
        state = platforms.get(platform)
        if not isinstance(state, dict) or not isinstance(state.get("configured"), bool):
            raise ValueError("通知连接元数据的平台状态无效；本次读取已停止。")
        if set(state) - {"configured", "fingerprint"}:
            raise ValueError("通知连接元数据的平台字段无效；本次读取已停止。")
        fingerprint = str(state.get("fingerprint") or "").lower()
        if state["configured"] and not re.fullmatch(r"[0-9a-f]{16}", fingerprint):
            raise ValueError("通知连接元数据的平台指纹无效；本次读取已停止。")
        if not state["configured"] and fingerprint:
            raise ValueError("通知连接元数据的平台状态不一致；本次读取已停止。")
        configured[platform] = state["configured"]
    if any(configured.values()) and not revision:
        raise ValueError("通知连接元数据缺少安全修订号；本次读取已停止。")
    return {
        "revision": revision,
        "auto_send_reports": saved["auto_send_reports"],
        "configured": configured,
    }


def _new_integration_secret_record(
    values: dict[str, Any],
    *,
    install_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": INTEGRATION_SECRET_SCHEMA_VERSION,
        "revision": os.urandom(16).hex(),
        "install_id": install_id,
        "auto_send_reports": bool(values.get("auto_send_reports")),
        "feishu_webhook": str(values.get("feishu_webhook") or ""),
        "dingtalk_webhook": str(values.get("dingtalk_webhook") or ""),
    }


def _load_integration_secrets_unlocked() -> dict[str, Any]:
    path = _validate_integrations_path()
    saved: dict[str, Any] | None = None
    read_error: Exception | None = None
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("notification metadata must be an object")
        except (OSError, json.JSONDecodeError, ValueError) as error:
            read_error = error

    legacy = bool(
        isinstance(saved, dict)
        and ({"feishu_webhook", "dingtalk_webhook"} & set(saved))
    )
    parsed_metadata: dict[str, Any] | None = None
    if saved is not None and not legacy and read_error is None:
        try:
            parsed_metadata = _parse_integration_metadata(saved)
        except ValueError as error:
            read_error = error

    secret_store = _integration_secret_store()
    secret_record = secret_store.load(
        required=bool(
            parsed_metadata
            and parsed_metadata.get("revision")
        ),
        legacy_revision=str(parsed_metadata.get("revision") or "") if parsed_metadata else "",
    )
    if secret_record:
        protected = _validate_integration_secret_record(
            secret_record,
            install_id=secret_store.install_id,
        )
        values = {
            key: protected[key]
            for key in ("feishu_webhook", "dingtalk_webhook", "auto_send_reports")
        }
        projection = _integration_metadata(
            values, protected["revision"], secret_store.label()
        )
        # The protected record is the transaction source of truth.  This heals
        # a crash after the secret commit but before metadata replacement.
        if saved != projection:
            _write_integration_metadata(projection)
        return values

    if read_error is not None:
        raise ValueError("通知连接元数据不可读；为避免误发，本次读取已停止。") from read_error

    if legacy:
        values = _empty_integration_values()
        try:
            for platform in ("feishu", "dingtalk"):
                key = f"{platform}_webhook"
                values[key] = _validate_webhook(platform, str(saved.get(key) or ""))
            legacy_auto_send = saved.get("auto_send_reports", False)
            if not isinstance(legacy_auto_send, bool):
                raise ValueError("旧版通知自动发送设置必须是布尔值。")
            values["auto_send_reports"] = legacy_auto_send
        except ValueError as error:
            raise ValueError("旧版通知连接配置无效；为避免误发，本次迁移已停止。") from error
        if values["feishu_webhook"] or values["dingtalk_webhook"]:
            record = _new_integration_secret_record(
                values,
                install_id=secret_store.install_id,
            )
            # Only replace the legacy plaintext file after native storage has
            # been written and read back successfully.
            secret_store.store(record)
            _write_integration_metadata(
                _integration_metadata(values, record["revision"], secret_store.label()),
            )
        else:
            _write_integration_metadata(_integration_metadata(values, "", secret_store.label()))
        return values

    if parsed_metadata is not None:
        # No platform claims to be connected, so no secret is required.
        values = _empty_integration_values()
        values["auto_send_reports"] = parsed_metadata["auto_send_reports"]
        return values
    return _empty_integration_values()


def _load_integration_secrets() -> dict[str, Any]:
    with _integration_secret_lock:
        return _load_integration_secrets_unlocked()


def get_integration_settings() -> dict[str, Any]:
    values = _load_integration_secrets()
    return {
        "feishu": {"configured": bool(values["feishu_webhook"]), "label": "已连接" if values["feishu_webhook"] else "未连接"},
        "dingtalk": {"configured": bool(values["dingtalk_webhook"]), "label": "已连接" if values["dingtalk_webhook"] else "未连接"},
        "auto_send_reports": bool(values["auto_send_reports"]),
        "secret_storage": _integration_secret_store().label(),
        "secrets_exposed": False,
    }


def save_integration_settings(values: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("integration settings must be an object")
    allowed = {"feishu_webhook", "dingtalk_webhook", "auto_send_reports"}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unknown integration settings: {', '.join(sorted(unknown))}")
    with _integration_secret_lock:
        current = _load_integration_secrets_unlocked()
        secret_store = _integration_secret_store()
        protected_record_exists = bool(secret_store.load(required=False))
        for platform in ("feishu", "dingtalk"):
            key = f"{platform}_webhook"
            if key in values:
                current[key] = _validate_webhook(platform, str(values.get(key) or ""))
        if "auto_send_reports" in values:
            if not isinstance(values["auto_send_reports"], bool):
                raise ValueError("auto_send_reports must be a boolean")
            current["auto_send_reports"] = values["auto_send_reports"]

        if current["feishu_webhook"] or current["dingtalk_webhook"] or protected_record_exists:
            record = _new_integration_secret_record(
                current,
                install_id=secret_store.install_id,
            )
            secret_store.store(record)
            revision = record["revision"]
        else:
            # A brand-new metadata-only setup does not require native secret
            # storage because it contains no credential.
            revision = ""
        _write_integration_metadata(
            _integration_metadata(current, revision, secret_store.label()),
        )
        return {
            "feishu": {"configured": bool(current["feishu_webhook"]), "label": "已连接" if current["feishu_webhook"] else "未连接"},
            "dingtalk": {"configured": bool(current["dingtalk_webhook"]), "label": "已连接" if current["dingtalk_webhook"] else "未连接"},
            "auto_send_reports": bool(current["auto_send_reports"]),
            "secret_storage": secret_store.label(),
            "secrets_exposed": False,
        }


class _NoWebhookRedirects(HTTPRedirectHandler):
    """Never forward a trusted Webhook request to a second origin."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _post_json(
    url: str,
    payload: dict[str, Any],
    timeout: float = 8.0,
    *,
    platform: str,
) -> dict[str, Any]:
    webhook = _validate_webhook(platform, url)
    request = Request(
        webhook,
        data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": f"DianAgent/{AGENT_VERSION}"},
        method="POST",
    )
    try:
        # Returning None from redirect_request turns every 3xx into an error
        # before the Location target is contacted.
        opener = build_opener(_NoWebhookRedirects())
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - allowlisted above
            final_url = _validate_webhook(platform, str(response.geturl() or ""))
            if final_url != webhook:
                raise ValueError("Webhook response origin changed; delivery was stopped.")
            raw_bytes = response.read(MAX_WEBHOOK_RESPONSE_BYTES + 1)
            if len(raw_bytes) > MAX_WEBHOOK_RESPONSE_BYTES:
                raise ValueError("Webhook response was too large.")
            raw = raw_bytes.decode("utf-8", errors="replace")
    except HTTPError as error:
        # ``HTTPError`` owns the response body/socket.  Reading only ``code``
        # and replacing the exception would otherwise defer socket cleanup to
        # the garbage collector on every rejected Webhook delivery.
        status_code = int(error.code)
        error.close()
        raise ValueError(f"Webhook delivery was rejected (HTTP {status_code}).") from error
    except (URLError, OSError) as error:
        raise ValueError("Webhook delivery failed before a trusted response was received.") from error
    result = json.loads(
        raw,
        parse_float=_parse_finite_json_float,
        parse_constant=_reject_nonfinite_json_constant,
    ) if raw else {}
    if not isinstance(result, dict):
        raise ValueError("Webhook response must be a JSON object.")
    return result


def _webhook_result_code(result: dict[str, Any], platform: str) -> int:
    fields = ("code", "StatusCode") if platform == "feishu" else ("errcode",)
    missing = object()
    value: Any = missing
    for field_name in fields:
        if field_name in result:
            value = result[field_name]
            break
    if value is missing or isinstance(value, bool):
        raise ValueError("平台通知响应缺少有效状态码。")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    raise ValueError("平台通知响应状态码格式无效。")


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
    result = _post_json(webhook, payload, platform=platform)
    success = _webhook_result_code(result, platform) == 0
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
            results.append({"platform": platform, "ok": False, "message": _safe_report_delivery_error(error)})
    return results


def _report_delivery_path() -> Path:
    """Persist scheduler delivery receipts separately from report contents."""
    return DATA_DIR / "report_deliveries.json"


def _load_report_delivery_state() -> dict[str, Any]:
    path = _report_delivery_path()
    if not path.exists():
        return {"schema_version": REPORT_DELIVERY_SCHEMA_VERSION, "reports": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("日报发送状态不可读；为避免重复发送，本轮已停止。") from error
    reports = value.get("reports") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != REPORT_DELIVERY_SCHEMA_VERSION
        or not isinstance(reports, dict)
    ):
        raise ValueError("日报发送状态格式无效；为避免重复发送，本轮已停止。")
    return {
        "schema_version": REPORT_DELIVERY_SCHEMA_VERSION,
        "reports": reports,
    }


def _configured_report_destinations() -> dict[str, str]:
    values = _load_integration_secrets()
    return {
        platform: hashlib.sha256(str(values[f"{platform}_webhook"]).encode("utf-8")).hexdigest()[:16]
        for platform in ("feishu", "dingtalk")
        if values.get(f"{platform}_webhook")
    }


def _report_store_scope(report: dict[str, Any]) -> str:
    explicit = str(report.get("store_scope") or "").lower()
    if SAFE_KEY.fullmatch(explicit):
        return explicit
    report_path = Path(str(report.get("path") or ""))
    if report_path.parent.parent.name == "reports" and SAFE_KEY.fullmatch(report_path.parent.name):
        return report_path.parent.name
    selected = str(load_agent_settings().get("store_key") or "legacy_unscoped").lower()
    return selected if SAFE_KEY.fullmatch(selected) else "legacy_unscoped"


def _safe_client_error(error: Exception) -> str:
    """Return a bounded validation error without common credential forms."""

    message = _safe_report_delivery_error(error)
    message = re.sub(
        r"(?i)\b(authorization\s*:\s*bearer)\s+[^\s,;]+",
        r"\1 [redacted]",
        message,
    )
    message = re.sub(
        r"(?i)(\b(?:api[_-]?key|app[_-]?secret|client[_-]?secret|access[_-]?token|"
        r"refresh[_-]?token|cookie|webhook)\b\s*[:=]\s*[\"']?)[^\"'\s,;&]+",
        r"\1[redacted]",
        message,
    )
    return message[:500]


def _safe_report_delivery_error(error: Exception) -> str:
    message = str(error or "发送失败")
    message = re.sub(r"https?://\S+", "[地址已隐藏]", message, flags=re.IGNORECASE)
    message = re.sub(r"(?i)(access_token=)[^&\s]+", r"\1[已隐藏]", message)
    return message[:500] or "发送失败"


def _validated_scheduled_report_contract(
    report: dict[str, Any], *, now: float | None = None
) -> dict[str, str]:
    """Fail closed unless an automatic report is current, scoped and intact.

    The Markdown file is only a presentation artifact.  Automatic delivery is
    authorized by this short-lived machine-readable guard, never by the mere
    existence of a file from an earlier scheduler run.
    """

    if not isinstance(report, dict):
        raise ValueError("scheduled report contract is missing")
    guard = report.get("delivery_guard")
    if not isinstance(guard, dict) or guard.get("schema_version") != REPORT_GUARD_SCHEMA_VERSION:
        raise ValueError("scheduled report freshness guard is missing or unsupported")

    current_seconds = float(time.time() if now is None else now)
    current_ms = int(current_seconds * 1000)
    checked_at_ms = int(guard.get("checked_at_ms") or 0)
    valid_until_ms = int(guard.get("valid_until_ms") or 0)
    if checked_at_ms <= 0 or checked_at_ms > current_ms + MAX_CAPTURE_FUTURE_SKEW_MS:
        raise ValueError("scheduled report freshness guard has an invalid timestamp")
    if valid_until_ms <= current_ms or valid_until_ms <= checked_at_ms:
        raise ValueError("scheduled report freshness guard has expired")
    if valid_until_ms - checked_at_ms > STALE_SECONDS * 1000:
        raise ValueError("scheduled report freshness guard exceeds its maximum lifetime")

    content = str(report.get("content") or "")
    supplied_digest = str(report.get("content_sha256") or "").lower()
    computed_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if not re.fullmatch(r"[a-f0-9]{64}", supplied_digest) or not hmac.compare_digest(
        supplied_digest, computed_digest
    ):
        raise ValueError("scheduled report content integrity check failed")

    content_kind = str(report.get("content_kind") or "")
    if content_kind not in {"operating_report", "sync_reminder"}:
        raise ValueError("scheduled report content kind is invalid")
    if str(guard.get("delivery_kind") or "") != content_kind:
        raise ValueError("scheduled report guard does not match its content kind")

    report_scope = str(report.get("store_scope") or "").lower()
    guard_scope = str(guard.get("store_scope") or "").lower()
    if not SAFE_KEY.fullmatch(report_scope) or report_scope != guard_scope:
        raise ValueError("scheduled report store scope is invalid")

    current_guard = build_scheduled_report_guard(now_ms=current_ms)
    current_scope = str(current_guard.get("store_scope") or "").lower()
    if current_scope != report_scope:
        raise ValueError("scheduled report store scope changed before delivery")

    report_safe = guard.get("safe_for_business_conclusions") is True
    current_safe = current_guard.get("safe_for_business_conclusions") is True
    if content_kind == "operating_report" and not (report_safe and current_safe):
        raise ValueError("scheduled operating report no longer has fresh business evidence")
    if content_kind == "sync_reminder" and (report_safe or current_safe):
        raise ValueError("scheduled sync reminder no longer matches current evidence")
    return {"store_scope": report_scope, "content_kind": content_kind}


def deliver_scheduled_report(
    report: dict[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    """Deliver one scheduled report with durable per-platform retry receipts.

    A successful platform receipt is final for the report date, while failed
    platforms remain eligible for bounded backoff retries. Manual report sends
    continue to use ``send_report_notifications`` and are intentionally not
    deduplicated by this scheduler-only state.
    """
    report_date = str(report.get("date") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
        raise ValueError("scheduled report requires a valid report date")
    current = float(time.time() if now is None else now)
    contract = _validated_scheduled_report_contract(report, now=current)
    destinations = _configured_report_destinations()
    configured = tuple(destinations)
    safe_scope = contract["store_scope"]
    content_kind = contract["content_kind"]
    report_key = f"{safe_scope}:{report_date}:{content_kind}"
    with _report_delivery_lock:
        document = _load_report_delivery_state()
        reports = document["reports"]
        entry = reports.get(report_key) if isinstance(reports.get(report_key), dict) else {}
        platforms = entry.get("platforms") if isinstance(entry.get("platforms"), dict) else {}
        attempted: list[dict[str, Any]] = []
        for platform in configured:
            previous = platforms.get(platform) if isinstance(platforms.get(platform), dict) else {}
            destination_fingerprint = destinations[platform]
            if previous.get("destination_fingerprint") != destination_fingerprint:
                previous = {}
            if previous.get("status") == "sent" and previous.get("destination_fingerprint") == destination_fingerprint:
                continue
            next_retry_at = float(previous.get("next_retry_at") or 0)
            if next_retry_at > current:
                continue
            attempts = int(previous.get("attempts") or 0) + 1
            try:
                result = send_notification(platform, str(report.get("content") or ""))
            except (OSError, ValueError, json.JSONDecodeError) as error:
                delay = REPORT_DELIVERY_RETRY_SECONDS[min(attempts - 1, len(REPORT_DELIVERY_RETRY_SECONDS) - 1)]
                receipt = {
                    "platform": platform,
                    "ok": False,
                    "status": "retry_pending",
                    "attempts": attempts,
                    "last_attempt_at": current,
                    "next_retry_at": current + delay,
                    "destination_fingerprint": destination_fingerprint,
                    "message": _safe_report_delivery_error(error),
                }
            else:
                receipt = {
                    "platform": platform,
                    "ok": True,
                    "status": "sent",
                    "attempts": attempts,
                    "last_attempt_at": current,
                    "sent_at": current,
                    "next_retry_at": None,
                    "destination_fingerprint": destination_fingerprint,
                    "message": str(result.get("message") or "发送成功。")[:500],
                }
            platforms[platform] = receipt
            attempted.append(receipt)
            entry.update({
                "report_date": report_date,
                "store_scope": safe_scope,
                "content_kind": content_kind,
                "platforms": platforms,
            })
            reports[report_key] = entry
            _atomic_json_write(_report_delivery_path(), document)

        # A removed webhook is no longer part of the delivery obligation, but
        # its historical receipt remains available for local audit.
        complete = bool(configured) and all(
            isinstance(platforms.get(platform), dict)
            and platforms[platform].get("status") == "sent"
            and platforms[platform].get("destination_fingerprint") == destinations[platform]
            for platform in configured
        )
        next_entry = {
            **entry,
            "report_date": report_date,
            "store_scope": safe_scope,
            "content_kind": content_kind,
            "report_path": str(report.get("path") or ""),
            "configured_platforms": list(configured),
            "status": "sent" if complete else "retry_pending" if configured else "not_configured",
            "platforms": platforms,
        }
        if not attempted and all(entry.get(key) == next_entry.get(key) for key in next_entry):
            return {**entry, "attempted": []}
        entry = {**next_entry, "updated_at": _now_label()}
        reports[report_key] = entry
        _atomic_json_write(_report_delivery_path(), document)
        return {**entry, "attempted": attempted}


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


def _plan_collection_snapshots() -> list[dict[str, Any]]:
    """Load the exact snapshot families represented in the plan console.

    The returned wrappers contain only the already-selected local store/account
    scope.  Coverage parsing happens in a pure domain helper and never promotes
    client-provided coverage metadata into an execution permission.
    """

    eligible = {
        "doudian": {"qianchuan_campaigns", "qianchuan_live", "qianchuan_report"},
        "qianchuan": {"campaigns", "qianchuan_live", "plans", "report"},
    }
    snapshots: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in list_snapshots():
        source = str(item.get("source") or "")
        page_type = str(item.get("page_type") or "")
        key = (source, page_type)
        if page_type not in eligible.get(source, set()) or key in seen:
            continue
        seen.add(key)
        snapshot = load_data(source, page_type)
        if isinstance(snapshot, dict):
            snapshots.append({"source": source, "page_type": page_type, "snapshot": snapshot})
    return snapshots


def _plan_collection_scope_key(account_key: Any, promotion_mode: Any, plan_type: Any) -> str:
    """Return the exact account/mode/type key used by scoped receipts."""

    account = str(account_key or "").strip().lower()[:128] or "unknown-account"
    mode = str(promotion_mode or "unknown").strip().lower()[:32]
    if mode not in {"standard", "full_domain", "chengfang", "suixintui"}:
        mode = "unknown"
    kind = str(plan_type or "unknown").strip().lower()[:24]
    if kind not in {"live", "product"}:
        kind = "unknown"
    return f"{account}|{mode}|{kind}"


def _plan_identity_key(
    account_key: Any,
    promotion_mode: Any,
    plan_type: Any,
    plan_id: Any,
) -> str:
    """Return the only identity allowed to merge or bind an actionable plan.

    A display name is deliberately never part of this key.  Names are mutable
    and non-unique, while the same plan id may legitimately exist on another
    promotion surface.  Rows without a stable id remain visible but do not get
    an actionable identity.
    """

    identifier = str(plan_id or "").strip()
    if not identifier:
        return ""
    account = str(account_key or "").strip().lower()[:128] or "unknown-account"
    mode = str(promotion_mode or "unknown").strip().lower()[:32]
    if mode not in {"standard", "full_domain", "chengfang", "suixintui"}:
        mode = "unknown"
    kind = str(plan_type or "unknown").strip().lower()[:24]
    if kind not in {"live", "product"}:
        kind = "unknown"
    return f"{account}|{mode}|{kind}|{identifier}"


def _plan_key(
    account_key: Any,
    promotion_mode: Any,
    plan_type: Any,
    plan_id: Any,
) -> str:
    """Expose an opaque stable key derived from the complete plan identity."""

    identity = _plan_identity_key(account_key, promotion_mode, plan_type, plan_id)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20] if identity else ""


def _plan_collection_gate_for_row(
    row: dict[str, Any],
    receipts_by_scope: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Bind one plan identity to one complete scoped collection receipt.

    A global receipt is deliberately never accepted here.  Product and live
    totals can legitimately differ, and a receipt for another account or
    promotion mode must not qualify this row for diagnosis or execution work.
    """

    scope_key = _plan_collection_scope_key(
        row.get("account_key"), row.get("promotion_mode"), row.get("plan_type")
    )
    receipt = receipts_by_scope.get(scope_key)
    blockers: list[dict[str, str]] = []
    plan_id = str(row.get("plan_id") or "").strip()
    if not plan_id:
        blockers.append({"code": "PLAN_IDENTITY_UNVERIFIED", "message": "计划缺少稳定计划 ID。"})
    if receipt is None:
        blockers.append({
            "code": "PLAN_SCOPE_RECEIPT_MISSING",
            "message": "当前广告账户、投放模式与计划类型没有对应的采集回执。",
        })
    else:
        scope = receipt.get("scope") if isinstance(receipt.get("scope"), dict) else {}
        identity_matches = bool(
            str(scope.get("account_key") or "").strip().lower() == str(row.get("account_key") or "").strip().lower()
            and str(scope.get("promotion_mode") or "").strip().lower() == str(row.get("promotion_mode") or "").strip().lower()
            and str(scope.get("plan_type") or "").strip().lower() == str(row.get("plan_type") or "").strip().lower()
        )
        if not identity_matches or receipt.get("scope_identity_verified") is not True:
            blockers.append({
                "code": "PLAN_SCOPE_IDENTITY_MISMATCH",
                "message": "采集回执与当前计划的账户、投放模式或计划类型不一致。",
            })
        if receipt.get("safe_to_claim_complete") is not True:
            blockers.append({
                "code": "PLAN_SCOPE_COVERAGE_INCOMPLETE",
                "message": "当前计划范围尚未完整采集，请完成翻页或滚动读取后重试。",
            })
    coverage_ready = not blockers
    binding_ready = row.get("eligible_for_local_binding") is True
    if coverage_ready and not binding_ready:
        row_blockers = [str(value) for value in (row.get("binding_blockers") or []) if str(value).strip()]
        blocker_code = (
            "PLAN_DATA_STALE"
            if row.get("stale") is True or any("过期" in value or "时间异常" in value for value in row_blockers)
            else "PLAN_BINDING_NOT_READY"
        )
        blockers.append({
            "code": blocker_code,
            "message": row_blockers[0] if row_blockers else "计划数据新鲜度、质量或本地身份绑定条件尚未通过。",
        })
    diagnosis_ready = coverage_ready and binding_ready
    supervised_draft_ready = diagnosis_ready
    blocker_codes = {item["code"] for item in blockers}
    if "PLAN_IDENTITY_UNVERIFIED" in blocker_codes:
        state = "plan_id_missing"
        next_action = {
            "code": "read_plan_id",
            "label": "补读计划 ID",
            "detail": "已识别到直播业务行，但缺少稳定计划 ID；打开计划详情或含计划 ID 的列表后重新读取。",
        }
    elif "PLAN_SCOPE_IDENTITY_MISMATCH" in blocker_codes:
        state = "identity_mismatch"
        next_action = {
            "code": "reselect_verified_scope",
            "label": "重新核对计划身份",
            "detail": "切回正确广告账户并重新读取，不能借用其他账户或模式的回执。",
        }
    elif "PLAN_SCOPE_COVERAGE_INCOMPLETE" in blocker_codes or "PLAN_SCOPE_RECEIPT_MISSING" in blocker_codes:
        state = "partial_collection"
        next_action = {
            "code": "continue_scoped_collection",
            "label": "继续读取当前范围",
            "detail": "保持当前广告账户、投放模式与计划类型，完成翻页或滚动采集。",
        }
    elif "PLAN_DATA_STALE" in blocker_codes:
        state = "complete_but_stale"
        next_action = {
            "code": "refresh_verified_scope",
            "label": "刷新已确认范围",
            "detail": "范围已经完整，但计划数据已过期；重新读取当前计划页后再诊断。",
        }
    elif blockers:
        state = "plan_not_ready"
        next_action = {
            "code": "repair_plan_evidence",
            "label": "补齐计划证据",
            "detail": "修复计划 ID、数据质量或账户绑定后重新生成草稿。",
        }
    else:
        state = "ready"
        next_action = {
            "code": "review_supervised_draft",
            "label": "生成受监督草稿",
            "detail": "只生成本地只读草稿；真实提交仍保持关闭。",
        }
    return {
        "scope_key": scope_key,
        "state": state,
        "receipt_status": str((receipt or {}).get("status") or "missing"),
        "scope_identity_matches": bool(receipt) and not any(
            item["code"] == "PLAN_SCOPE_IDENTITY_MISMATCH" for item in blockers
        ),
        "coverage_complete": bool(receipt and receipt.get("safe_to_claim_complete") is True),
        "read_only_diagnosis_ready": diagnosis_ready,
        "supervised_draft_ready": supervised_draft_ready,
        "blockers": blockers,
        "next_action": next_action,
        "execution_enabled": False,
    }


def _pick(record: dict[str, Any], keywords: tuple[str, ...]) -> tuple[str, Any] | tuple[None, None]:
    for label, value in record.items():
        normalized = str(label).lower().replace(" ", "")
        if any(keyword.lower().replace(" ", "") in normalized for keyword in keywords):
            return str(label), value
    return None, None


def _evidence_value(record: dict[str, Any], keywords: tuple[str, ...]) -> float | None:
    _, value = _pick(record, keywords)
    return _parse_number(value)


def _evidence_text(record: dict[str, Any], keywords: tuple[str, ...]) -> str:
    """Read a labeled text value without coercing statuses into numbers."""
    _, value = _pick(record, keywords)
    return str(value or "").strip()


def _normalize_delivery_status(value: Any) -> str:
    """Map common Qianchuan status labels to a small verification vocabulary."""
    normalized = re.sub(r"[\s·•|/]+", "", str(value or "")).strip().lower()
    if any(token in normalized for token in ("已暂停", "暂停中", "停止投放", "已停用", "暂停", "停用")):
        return "暂停"
    if any(token in normalized for token in ("正常投放", "投放中", "生效中", "运行中", "已启用", "启用")):
        return "投放中"
    return str(value or "").strip()


def _normalize_learning_health(value: Any) -> tuple[str, str]:
    """Normalize the read-only plan learning badge without inferring health."""

    raw = str(value or "").strip()
    normalized = re.sub(r"[\s_-]+", "", raw).upper()
    mapping = {
        "LEARNING": ("learning", "学习中"),
        "学习中": ("learning", "学习中"),
        "LEARNED": ("learned", "学习完成"),
        "学习完成": ("learned", "学习完成"),
        "学习期结束": ("learned", "学习完成"),
        "LEARNFAILED": ("failed", "学习失败"),
        "学习失败": ("failed", "学习失败"),
        "学习期失败": ("failed", "学习失败"),
        "DEFAULT": ("none", "无学习期状态"),
        "无学习期状态": ("none", "无学习期状态"),
        "无学习状态": ("none", "无学习期状态"),
        "UNAVAILABLE": ("unavailable", "暂不可读"),
        "暂不可读": ("unavailable", "暂不可读"),
    }
    if not raw:
        return "unavailable", "暂不可读"
    return mapping.get(normalized, ("unknown", raw[:40] or "状态未知"))


def _normalize_low_efficiency_health(value: Any) -> tuple[str, str]:
    """Keep the platform flag tri-state; unavailable is never false."""

    raw = str(value or "").strip()
    normalized = re.sub(r"[\s_-]+", "", raw).lower()
    if normalized in {"flagged", "平台标记低效", "低效", "是"}:
        return "flagged", "平台标记低效"
    if normalized in {"notflaggedcurrently", "本次未命中", "未命中"}:
        return "not_flagged_currently", "本次未命中"
    if normalized in {"unknown", "unavailable", "暂不可读"} or not raw:
        return "unknown", "暂不可读"
    return "unknown", raw[:40] or "暂不可读"


def _normalize_plan_diagnostic_source(value: Any) -> tuple[str, str]:
    raw = str(value or "").strip()
    normalized = re.sub(r"\s+", "", raw).lower()
    if not raw or normalized in {"unavailable", "暂不可读"}:
        return "unavailable", "暂不可读"
    if "官方api" in normalized:
        if "部分不可用" in normalized:
            return "official_api_partial", "官方 API（部分不可用）"
        if "状态未知" in normalized:
            return "official_api_unknown", "官方 API（状态未知）"
        return "official_api", "官方 API"
    return "unknown", raw[:60]


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
    delivery_status = _normalize_delivery_status(
        _evidence_text(record, ("投放状态", "计划状态", "状态"))
    )
    plan_id = _plan_identifier(record)
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
    page_type = str(entry.get("page_type") or "")
    plan_type = "live" if page_type == "qianchuan_live" else "product" if page_type in {"campaigns", "qianchuan_campaigns"} else "unknown"
    promotion_context = build_promotion_context(entry.get("promotion_context"))
    if promotion_context.get("promotion_mode") == "unknown":
        _, promotion_mode_value = _pick(record, ("推广类型", "推广模式", "投放模式"))
        inferred_context = build_promotion_context(promotion_mode_value)
        if inferred_context.get("promotion_mode") != "unknown":
            promotion_context = {
                **promotion_context,
                "promotion_mode": inferred_context["promotion_mode"],
                "promotion_mode_label": inferred_context.get("promotion_mode_label"),
            }
    compact_evidence.update({
        "collection_scope_required": True,
        "plan_type": plan_type,
        "collection_scope_key": _plan_collection_scope_key(
            entry.get("account_key"),
            promotion_context.get("promotion_mode"),
            plan_type,
        ),
    })
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
        page_type=page_type,
        captured_at_ms=int(entry.get("captured_at_ms") or 0),
        quality_score=int(entry.get("quality_score") or 0),
        confidence=confidence,
        evidence=compact_evidence,
        copy_text=copy_text,
        promotion_context=promotion_context,
    )


def _action_audit_path() -> Path:
    return DATA_DIR / "action_audit.json"


def _load_action_audit_unrecovered() -> dict[str, Any]:
    path = _action_audit_path()
    if not path.exists():
        return {"schema_version": 1, "actions": [], "execution_enabled": False}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.exception("读取操作审计记录失败: %s", path)
        # This is the durable at-most-once ledger. Treating a present but
        # unreadable file as empty could authorize the same plan twice after a
        # crash, antivirus lock, or disk corruption.
        raise OSError("action audit ledger is unreadable; supervised execution is locked") from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("actions"), list)
        or type(value.get("execution_enabled")) is not bool
        or any(not isinstance(item, dict) for item in value.get("actions", []))
    ):
        raise OSError("action audit ledger has an invalid schema; supervised execution is locked")
    action_ids = [str(item.get("action_id") or "") for item in value["actions"]]
    populated_ids = [item for item in action_ids if item]
    if len(populated_ids) != len(set(populated_ids)):
        raise OSError("action audit ledger contains duplicate action ids; supervised execution is locked")
    return {
        "schema_version": 1,
        "updated_at": value.get("updated_at"),
        "actions": list(value["actions"]),
        # The aggregate flag is informational only. A signed one-time
        # preflight/session is the sole authority to submit a page mutation.
        "execution_enabled": False,
    }


def load_action_audit() -> dict[str, Any]:
    # A hard crash may have interrupted the two-file authorization-consume
    # commit. Recover it before exposing either half of that state.
    baseline_recover = globals().get("_recover_execution_baseline_transaction")
    if callable(baseline_recover):
        baseline_recover()
    consume_recover = globals().get("_recover_execution_consume_transaction")
    if callable(consume_recover):
        consume_recover()
    archive_recover = globals().get("_recover_manual_reconcile_archive_transaction")
    if callable(archive_recover):
        archive_recover()
    return _load_action_audit_unrecovered()


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
            "executing": sum(item.get("state") == "executing" for item in audit["actions"]),
            "cancelled": sum(item.get("state") == "cancelled" for item in audit["actions"]),
            "executed": sum(item.get("state") in {"succeeded", "verified"} for item in audit["actions"]),
        },
    }


def _action_target_account_key(action: dict[str, Any]) -> str:
    target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
    return str(target.get("account_key") or "").strip().lower()


def _supervised_draft_collection_gate(
    action: dict[str, Any],
    plan_console: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Require an exact, fresh scoped receipt for captured-plan actions.

    Hand-built protocol fixtures and rollback drafts retain their existing
    validation path.  Actions emitted from the plan collector opt in through
    the signed ``collection_scope_required`` evidence marker and fail closed.
    """

    evidence = action.get("evidence_ref") if isinstance(action.get("evidence_ref"), dict) else {}
    if evidence.get("collection_scope_required") is not True:
        return {
            "required": False,
            "ready": True,
            "state": "not_required",
            "scope_key": "",
            "next_action": {"code": "continue_existing_validation", "label": "继续既有安全检查"},
            "blockers": [],
            "execution_enabled": False,
        }
    target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
    account_key = str(target.get("account_key") or "").strip().lower()
    plan_id = str(target.get("id") or "").strip()
    promotion_context = build_promotion_context(action.get("promotion_context"))
    promotion_mode = str(promotion_context.get("promotion_mode") or "unknown")
    plan_type = str(evidence.get("plan_type") or "unknown")
    expected_scope_key = _plan_collection_scope_key(account_key, promotion_mode, plan_type)
    signed_scope_key = str(evidence.get("collection_scope_key") or "")
    if signed_scope_key != expected_scope_key:
        return {
            "required": True,
            "ready": False,
            "state": "identity_mismatch",
            "scope_key": expected_scope_key,
            "next_action": {
                "code": "regenerate_scoped_draft",
                "label": "重新生成当前范围草稿",
                "detail": "草稿中的账户、投放模式或计划类型与签发范围不一致。",
            },
            "blockers": [{
                "code": "SIGNED_COLLECTION_SCOPE_MISMATCH",
                "message": "草稿绑定的采集范围与当前目标身份不一致。",
            }],
            "execution_enabled": False,
        }
    console = plan_console if isinstance(plan_console, dict) else build_qianchuan_plan_console()
    rows = console.get("rows") if isinstance(console.get("rows"), list) else []
    matches = [
        row for row in rows
        if isinstance(row, dict)
        and str(row.get("account_key") or "").strip().lower() == account_key
        and str(row.get("plan_id") or "").strip() == plan_id
        and str(row.get("promotion_mode") or "unknown") == promotion_mode
        and str(row.get("plan_type") or "unknown") == plan_type
        and str(row.get("collection_scope_key") or "") == expected_scope_key
    ]
    if len(matches) != 1:
        return {
            "required": True,
            "ready": False,
            "state": "identity_mismatch",
            "scope_key": expected_scope_key,
            "next_action": {
                "code": "refresh_exact_plan_identity",
                "label": "重新读取目标计划",
                "detail": "当前本地数据中未找到唯一且完全一致的账户、模式、类型和计划 ID。",
            },
            "blockers": [{
                "code": "PLAN_IDENTITY_NOT_UNIQUE_IN_SCOPE",
                "message": "目标计划未能在当前采集范围中唯一匹配。",
            }],
            "execution_enabled": False,
        }
    row = matches[0]
    blockers = [
        item for item in (row.get("automation_blockers") or [])
        if isinstance(item, dict)
    ]
    return {
        "required": True,
        "ready": row.get("supervised_draft_ready") is True,
        "state": str(row.get("collection_gate_state") or "plan_not_ready"),
        "scope_key": expected_scope_key,
        "next_action": row.get("automation_next_action") or {},
        "blockers": blockers,
        "execution_enabled": False,
    }


def _validate_action_session_binding(action: dict[str, Any], session: dict[str, Any]) -> None:
    _validate_action_server_seal(action)
    expected = {
        "action_id": str(action.get("action_id") or ""),
        "action_integrity_hash": str(action.get("integrity_hash") or ""),
        "action_server_signature": str(action.get("server_signature") or ""),
        "store_key": str((action.get("proposal_scope") or {}).get("store_key") or ""),
        "account_key": _action_target_account_key(action),
        "binding_generation": str(int((action.get("proposal_scope") or {}).get("binding_generation") or 0)),
    }
    if any(
        str(session.get(key) if key == "binding_generation" else session.get(key) or "") != value
        for key, value in expected.items()
    ):
        raise ValueError("动作与执行会话绑定信息不一致，授权已失效，请重新生成方案。")


def _validate_confirmed_action_for_execution(action: dict[str, Any], session: dict[str, Any]) -> None:
    _validate_action_session_binding(action, session)
    _assert_active_binding_registry_consistent()
    proposal_scope = action.get("proposal_scope") if isinstance(action.get("proposal_scope"), dict) else {}
    promotion_context = build_promotion_context(action.get("promotion_context"))
    account_scope = promotion_context.get("account_scope") if isinstance(promotion_context.get("account_scope"), dict) else {}
    signed_store = str(proposal_scope.get("store_key") or "").strip().lower()
    signed_account = str(proposal_scope.get("account_key") or "").strip().lower()
    signed_binding_generation = int(proposal_scope.get("binding_generation") or 0)
    context_store = str(account_scope.get("store_id") or "").strip().lower()
    context_account = str(account_scope.get("account_id") or "").strip().lower()
    settings = load_agent_settings()
    selected_store = str(settings.get("store_key") or "").strip().lower()
    selected_account = str(settings.get("qianchuan_account_key") or "").strip().lower()
    if (
        not SAFE_KEY.fullmatch(signed_store)
        or signed_store == "unresolved"
        or context_store != signed_store
        or selected_store != signed_store
    ):
        raise ValueError("执行店铺作用域未绑定或已变化，请重新选择店铺并生成方案。")
    if (
        not SAFE_KEY.fullmatch(signed_account)
        or context_account != signed_account
        or selected_account != signed_account
    ):
        raise ValueError("执行账户作用域未绑定或已变化，请重新选择千川账户并生成方案。")
    known_accounts = [
        item for item in list_qianchuan_accounts()
        if str(item.get("key") or "").strip().lower() == signed_account
    ]
    known_stores = [
        item for item in list_store_identities()
        if str(item.get("key") or "").strip().lower() == signed_store
    ]
    bidirectional_binding = bool(
        len(known_accounts) == 1
        and len(known_stores) == 1
        and str(known_accounts[0].get("store_key") or "").strip().lower() == signed_store
        and signed_account in {
            str(value or "").strip().lower()
            for value in (known_stores[0].get("account_keys") or [])
        }
    )
    if not bidirectional_binding:
        raise ValueError("店铺与千川账户的双向绑定不完整，请重新同步两边页面并人工确认关联。")
    current_binding_generation = _binding_generation(signed_store, signed_account)
    if current_binding_generation < 0 or current_binding_generation != signed_binding_generation:
        raise ValueError(
            "STORE_ACCOUNT_BINDING_CHANGED: 店铺与千川账户在方案签发后已解绑或重新绑定，旧方案永久失效。"
        )
    collection_gate = _supervised_draft_collection_gate(action)
    if collection_gate.get("ready") is not True:
        blocker_codes = ", ".join(
            str(item.get("code") or "PLAN_COLLECTION_SCOPE_NOT_READY")
            for item in (collection_gate.get("blockers") or [])
            if isinstance(item, dict)
        ) or "PLAN_COLLECTION_SCOPE_NOT_READY"
        raise ValueError(
            f"PLAN_COLLECTION_SCOPE_STALE: 最新计划采集范围已变化，一次性授权不再安全（{blocker_codes}）。"
        )
    errors = validate_action_draft(action)
    if errors:
        messages = "；".join(dict.fromkeys(str(item.get("message") or "动作校验失败") for item in errors))
        raise ValueError(messages)


def _execution_baseline_hash(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _execution_baseline_from_readback(action: dict[str, Any], readback: dict[str, Any]) -> dict[str, Any]:
    promotion_context = build_promotion_context(action.get("promotion_context"))
    execution_context = readback.get("execution_context") if isinstance(readback.get("execution_context"), dict) else {}
    return {
        "snapshot_ref": str(readback.get("snapshot_ref") or "")
        or f"qianchuan/{str((action.get('evidence_ref') or {}).get('page_type') or 'campaigns')}/{int(readback.get('captured_at_ms') or 0)}",
        "readback_token": str(execution_context.get("readback_token") or ""),
        "readback_purpose": str(execution_context.get("purpose") or ""),
        "captured_at_ms": int(readback.get("captured_at_ms") or 0),
        "quality_score": int(readback.get("quality_score") or 0),
        "store_key": str(readback.get("actual_store_key") or (action.get("proposal_scope") or {}).get("store_key") or ""),
        "account_key": str(readback.get("actual_account_key") or ""),
        "plan_id": str(readback.get("plan_id") or ""),
        "current_value": readback.get("current_value"),
        "delivery_status": readback.get("delivery_status"),
        "spend": readback.get("spend"),
        "roi": readback.get("roi"),
        "orders": readback.get("orders"),
        "metric_contract": promotion_context.get("metric_contract") or {},
        "document_instance_id": str(readback.get("document_instance_id") or ""),
        "document_url": str(readback.get("document_url") or "")[:500],
        "navigation_started_at_ms": int(readback.get("navigation_started_at_ms") or 0),
    }


def _action_plan_lock_active(action: dict[str, Any], *, now_ms: int) -> bool:
    # Archiving an unknown platform result releases unrelated work, but it is
    # never permission to replay the same plan. Keep this tombstone durable
    # even if a later independent readback resolves the action state.
    if action.get("plan_retry_blocked") is True:
        return True
    state = str(action.get("state") or "")
    if state == "confirmed":
        expires_at_ms = int(action.get("expires_at_ms") or 0)
        return expires_at_ms <= 0 or now_ms < expires_at_ms
    if state in {"executing", "succeeded"}:
        return True
    if state != "verified":
        return False
    evidence = action.get("evidence_ref") if isinstance(action.get("evidence_ref"), dict) else {}
    spend = evidence.get("spend")
    observation_minutes = (
        30 if isinstance(spend, (int, float)) and spend >= 1000
        else 120 if isinstance(spend, (int, float)) and spend >= 300
        else 24 * 60
    )
    executed_at_ms = int(action.get("execution_reported_at_ms") or action.get("verified_at_ms") or 0)
    return executed_at_ms <= 0 or now_ms < executed_at_ms + observation_minutes * 60_000


def _retain_action_audit_entries(
    actions: list[dict[str, Any]], *, now_ms: int, terminal_limit: int = 500
) -> list[dict[str, Any]]:
    """Trim only old, safe terminal history; never discard locks or quota proof."""

    ordered = sorted(
        (item for item in actions if isinstance(item, dict)),
        key=lambda item: int(item.get("state_updated_at_ms") or item.get("created_at_ms") or 0),
        reverse=True,
    )
    recent_execution_cutoff_ms = now_ms - 90 * 24 * 60 * 60 * 1000
    protected: list[dict[str, Any]] = []
    terminal: list[dict[str, Any]] = []
    for item in ordered:
        execution_at_ms = int(
            item.get("execution_reported_at_ms")
            or item.get("execution_started_at_ms")
            or item.get("verified_at_ms")
            or 0
        )
        if (
            _action_plan_lock_active(item, now_ms=now_ms)
            or item.get("manual_reconcile_required") is True
            or execution_at_ms >= recent_execution_cutoff_ms > 0
        ):
            protected.append(item)
        elif len(terminal) < terminal_limit:
            terminal.append(item)
    return sorted(
        [*protected, *terminal],
        key=lambda item: int(item.get("state_updated_at_ms") or item.get("created_at_ms") or 0),
        reverse=True,
    )


def confirm_action_draft(action: dict[str, Any]) -> dict[str, Any]:
    _validate_action_server_seal(action)
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
        if existing and existing.get("state") == "executing":
            raise ValueError("该操作已经进入执行，结果尚未确认；禁止重复提交，请先重新读取当前计划。")
        if existing and existing.get("state") in {"succeeded", "verified", "rolled_back"}:
            raise ValueError("该操作已经执行过，不能再次确认同一方案。")
        if existing and existing.get("state") == "failed":
            raise ValueError("该操作已有失败回执，请重新同步页面后生成新方案。")
        target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
        rollback_of = str((action.get("evidence_ref") or {}).get("rollback_of_action_id") or "")
        now_ms = int(time.time() * 1000)
        conflicting = next((
            item for item in audit["actions"]
            if item.get("action_id") != action_id
            and str((item.get("target_ref") or {}).get("account_key") or "") == str(target.get("account_key") or "")
            and str((item.get("target_ref") or {}).get("id") or "") == str(target.get("id") or "")
            and _action_plan_lock_active(item, now_ms=now_ms)
            and not (action.get("operation_type") == "restore_budget" and rollback_of == item.get("action_id"))
        ), None)
        if conflicting:
            raise ValueError("该计划已有正在执行或观察中的动作；完成回读复盘前不会再次修改同一计划。")
        confirmed = transition_action(action, "confirmed")
        confirmed["confirmed_at_ms"] = now_ms
        confirmed["confirmed_by"] = "local_user"
        confirmed["execution_note"] = "已确认方案，尚未执行任何千川页面操作。"
        actions = [item for item in audit["actions"] if item.get("action_id") != action_id]
        actions.append(confirmed)
        actions = _retain_action_audit_entries(actions, now_ms=now_ms)
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


def _execution_readback_snapshot_path(action_id: str, readback_token: str) -> Path:
    return DATA_DIR / "execution_readbacks" / action_id / f"{readback_token}.json"


def _load_execution_readback_snapshot(action_id: str, readback_token: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[a-f0-9]{24}", action_id) or not re.fullmatch(r"[a-f0-9]{64}", readback_token):
        raise ValueError("执行回读引用无效。")
    path = _execution_readback_snapshot_path(action_id, readback_token)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OSError("执行回读快照无法读取；动作继续锁定。") from error
    context = value.get("execution_context") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("source") != "qianchuan"
        or not isinstance(value.get("data"), dict)
        or not isinstance(context, dict)
        or context.get("action_id") != action_id
        or context.get("readback_token") != readback_token
    ):
        raise OSError("执行回读快照结构无效；动作继续锁定。")
    return value


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


def _find_plan_readback(
    action: dict[str, Any],
    *,
    readback_token: str = "",
    expected_purpose: str = "",
    authorization_id: str = "",
    snapshot_override: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
    account_key = str(target.get("account_key") or "").strip().lower()
    plan_id = str(target.get("id") or "")
    proposal_scope = action.get("proposal_scope") if isinstance(action.get("proposal_scope"), dict) else {}
    expected_store_key = str(proposal_scope.get("store_key") or "").strip().lower()
    if not expected_store_key or not account_key or not plan_id:
        return None
    evidence = action.get("evidence_ref") if isinstance(action.get("evidence_ref"), dict) else {}
    signed_page_type = str(evidence.get("page_type") or "").strip().lower()
    page_type = "qianchuan_live" if signed_page_type == "qianchuan_live" else "campaigns" if signed_page_type in {"campaigns", "qianchuan_campaigns"} else ""
    if not page_type:
        return None
    action_id = str(action.get("action_id") or "").strip().lower()
    readback_token = str(readback_token or "").strip().lower()
    if snapshot_override is not None:
        snapshot = snapshot_override
    elif readback_token:
        snapshot = _load_execution_readback_snapshot(action_id, readback_token)
    else:
        snapshot = load_data("qianchuan", page_type, account_key=account_key)
    if readback_token or snapshot_override is not None:
        context = (snapshot or {}).get("execution_context") if isinstance(snapshot, dict) else None
        if (
            not isinstance(context, dict)
            or context.get("action_id") != action_id
            or context.get("readback_token") != readback_token
            or (expected_purpose and context.get("purpose") != expected_purpose)
            or (authorization_id and context.get("authorization_id") != authorization_id)
        ):
            return None
    data = (snapshot or {}).get("data", {})
    if not isinstance(data, dict):
        return None
    observed_page_type = str(data.get("page_type") or "").strip().lower()
    if observed_page_type != page_type:
        return None
    observed_account = data.get("account") if isinstance(data.get("account"), dict) else {}
    observed_store = data.get("store") if isinstance(data.get("store"), dict) else {}
    actual_account_key = str(observed_account.get("key") or "").lower()
    actual_store_key = str(observed_store.get("key") or observed_account.get("store_key") or "").lower()
    # Account IDs are not globally sufficient identity. The same advertiser
    # may be re-linked after a store switch, so every execution/readback path
    # requires the original store and account pair. A mismatched snapshot is
    # treated as absent and can never authorize, verify, or score an action.
    if actual_account_key != account_key or actual_store_key != expected_store_key:
        return None

    # A plan id is not globally unique across Qianchuan surfaces.  Bind every
    # result readback to the same signed promotion mode and product/live scope
    # as the submitted action.  Strategy and metric fields are also compared
    # whenever the collector can observe them; an observed conflict is never
    # allowed to verify a write.
    expected_context = build_promotion_context(action.get("promotion_context"))
    observed_context = build_promotion_context(data.get("promotion_context"))
    expected_mode = str(expected_context.get("promotion_mode") or "unknown")
    observed_mode = str(observed_context.get("promotion_mode") or "unknown")
    if expected_mode == "unknown" or observed_mode != expected_mode:
        return None
    expected_plan_type = str(evidence.get("plan_type") or ("live" if page_type == "qianchuan_live" else "product")).strip().lower()
    if expected_plan_type not in {"live", "product"}:
        return None
    if (expected_plan_type == "live") != (page_type == "qianchuan_live"):
        return None
    expected_scope_key = _plan_collection_scope_key(account_key, expected_mode, expected_plan_type)
    signed_scope_key = str(evidence.get("collection_scope_key") or "").strip()
    if evidence.get("collection_scope_required") is True and signed_scope_key != expected_scope_key:
        return None
    expected_strategy = str(expected_context.get("strategy_id") or "").strip()
    observed_strategy = str(observed_context.get("strategy_id") or "").strip()
    if observed_strategy and observed_strategy != expected_strategy:
        return None
    expected_metric = expected_context.get("metric_contract") if isinstance(expected_context.get("metric_contract"), dict) else {}
    observed_metric = observed_context.get("metric_contract") if isinstance(observed_context.get("metric_contract"), dict) else {}
    observed_metric_definition = str(observed_metric.get("definition") or "unknown")
    observed_metric_version = str(observed_metric.get("version") or "")
    if observed_metric_definition != "unknown" and observed_metric_definition != str(expected_metric.get("definition") or "unknown"):
        return None
    if observed_metric_version and observed_metric_version != str(expected_metric.get("version") or ""):
        return None

    tables = data.get("tables") if isinstance(data.get("tables"), list) else []
    canonical_headers: list[str] = []
    candidates: list[dict[str, Any]] = []
    captured_at_ms = int(
        data.get("captured_at")
        or (float((snapshot or {}).get("timestamp", 0)) * 1000)
        or 0
    )
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
            if _plan_identifier(record) != plan_id:
                continue
            budget = _evidence_value(record, ("日预算", "每日预算", "预算上限", "预算"))
            delivery_status_raw = _evidence_text(record, ("投放状态", "计划状态", "状态"))
            delivery_status = _normalize_delivery_status(delivery_status_raw)
            spend = _evidence_value(record, ("消耗", "总消耗", "广告消耗"))
            roi = _evidence_value(record, ("支付roi", "roi", "整体roi"))
            orders = _evidence_value(record, ("成交订单", "支付订单", "成交订单数", "订单数"))
            candidates.append({
                "account_key": actual_account_key,
                "actual_account_key": actual_account_key,
                "actual_store_key": actual_store_key,
                "account_identity_matches": bool(actual_account_key and actual_account_key == account_key),
                "store_identity_matches": bool(actual_store_key and actual_store_key == expected_store_key),
                "document_instance_id": str(data.get("document_instance_id") or "").strip(),
                "document_url": str(data.get("document_url") or data.get("url") or "").strip()[:500],
                "navigation_started_at_ms": int(data.get("navigation_started_at_ms") or 0),
                "account_label": str(observed_account.get("label") or ""),
                "plan_id": plan_id,
                "plan_name": _clean_entity_name(next(iter(record.values()), ""), str(target.get("name") or plan_id)),
                "current_value": budget,
                "delivery_status": delivery_status,
                "delivery_status_raw": delivery_status_raw,
                "spend": spend,
                "roi": roi,
                "orders": orders,
                "captured_at_ms": captured_at_ms,
                "quality_score": int((data.get("quality") or {}).get("score", 0) or 0),
                "page_type": page_type,
                "promotion_mode": observed_mode,
                "plan_type": expected_plan_type,
                "collection_scope_key": expected_scope_key,
                "metric_contract": observed_metric,
                "snapshot_ref": str((snapshot or {}).get("snapshot_ref") or (snapshot or {}).get("path") or ""),
                "execution_context": copy.deepcopy((snapshot or {}).get("execution_context"))
                if isinstance((snapshot or {}).get("execution_context"), dict)
                else None,
            })
    if not candidates:
        return None

    def critical_signature(item: dict[str, Any]) -> tuple[Any, ...]:
        def number(value: Any) -> float | None:
            return round(float(value), 6) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) else None

        return (
            number(item.get("current_value")),
            str(item.get("delivery_status") or ""),
            number(item.get("spend")),
            number(item.get("roi")),
            number(item.get("orders")),
        )

    signatures = {critical_signature(item) for item in candidates}
    if len(signatures) != 1:
        # Duplicate plan ids with conflicting values are ambiguous regardless
        # of DOM order.  Keeping the action locked is safer than accepting the
        # first row and potentially verifying another surface or stale clone.
        return None
    result = dict(candidates[0])
    result["duplicate_match_count"] = len(candidates)
    return result


_EXECUTION_READBACK_CONTEXT_KEYS = {
    "purpose", "action_id", "authorization_id", "readback_token",
}
_EXECUTION_READBACK_PURPOSES = {"preconsume_baseline", "execution_readback"}


def _validated_execution_snapshot_context(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _EXECUTION_READBACK_CONTEXT_KEYS:
        raise ValueError("执行定向快照缺少完整上下文。")
    context = {key: str(value.get(key) or "").strip() for key in _EXECUTION_READBACK_CONTEXT_KEYS}
    if (
        any(context[key] != context[key].lower() for key in _EXECUTION_READBACK_CONTEXT_KEYS)
        or context["purpose"] not in _EXECUTION_READBACK_PURPOSES
        or not re.fullmatch(r"[a-f0-9]{24}", context["action_id"])
        or not re.fullmatch(r"[a-f0-9]{32}", context["authorization_id"])
        or not re.fullmatch(r"[a-f0-9]{64}", context["readback_token"])
    ):
        raise ValueError("执行定向快照上下文无效。")
    return context


def _execution_context_action_locked(context: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    session = load_execution_preflight().get("session")
    action = next(
        (
            item for item in load_action_audit().get("actions", [])
            if str(item.get("action_id") or "").lower() == context["action_id"]
        ),
        None,
    )
    if not isinstance(action, dict):
        raise ValueError("执行定向快照对应的动作不存在。")
    archived_readback = bool(
        context["purpose"] == "execution_readback"
        and action.get("manual_reconcile_archived") is True
        and action.get("plan_retry_blocked") is True
        and action.get("state") in {"executing", "succeeded"}
        and str(action.get("execution_authorization_id") or "").lower() == context["authorization_id"]
    )
    if not archived_readback and (
        not isinstance(session, dict)
        or str(session.get("action_id") or "").lower() != context["action_id"]
        or str(session.get("authorization_id") or "").lower() != context["authorization_id"]
    ):
        raise ValueError("执行定向快照与当前授权会话不一致。")
    now_ms = int(time.time() * 1000)
    if context["purpose"] == "preconsume_baseline":
        if (
            session.get("state") != "authorized"
            or session.get("authorization_consumed") is True
            or int(session.get("authorization_expires_at_ms") or 0) <= now_ms
            or action.get("state") != "confirmed"
        ):
            raise ValueError("执行前定向复核授权已使用或已失效。")
    elif not archived_readback and (
        session.get("authorization_consumed") is not True
        or str(session.get("state") or "") not in {"authorization_consumed", "manual_reconcile_required"}
        or action.get("state") not in {"executing", "succeeded"}
        or str(action.get("execution_authorization_id") or "").lower() != context["authorization_id"]
    ):
        raise ValueError("执行回读只能绑定当前已消费且仍锁定的授权。")
    return action, session if isinstance(session, dict) else {}


def _resolve_execution_identity_locked(
    action: dict[str, Any],
    data: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not isinstance(data, dict):
        raise ValueError("执行身份复核缺少页面快照。")
    candidate = copy.deepcopy(data)
    try:
        json.dumps(candidate, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("执行身份复核快照包含无效数据。") from error
    expected = _execution_request_for_action(action)
    expected_store = str(expected.get("store_key") or "").strip().lower()
    expected_account = str(expected.get("account_key") or "").strip().lower()
    if not SAFE_KEY.fullmatch(expected_store) or not SAFE_KEY.fullmatch(expected_account):
        raise ValueError("执行动作缺少有效店铺或账户范围。")
    _assert_active_binding_registry_consistent()
    accounts = {str(item.get("key") or "").lower(): item for item in list_qianchuan_accounts()}
    stores = {str(item.get("key") or "").lower(): item for item in list_store_identities()}
    bound_account = accounts.get(expected_account)
    bound_store = stores.get(expected_store)
    if (
        not isinstance(bound_account, dict)
        or not isinstance(bound_store, dict)
        or str(bound_account.get("store_key") or "").lower() != expected_store
        or expected_account not in {str(value).lower() for value in bound_store.get("account_keys", [])}
        or _binding_is_revoked(expected_store, expected_account)
    ):
        raise ValueError("执行店铺与千川账户的本地绑定已失效。")

    resolved_store, resolved_account, resolution = _resolve_identity_claims(candidate)
    if resolution == "conflict" or not isinstance(resolved_account, dict):
        raise ValueError("当前页面无法唯一证明千川账户身份。")
    resolved_account_key = _canonical_qianchuan_account_key(resolved_account)
    if (
        resolved_account_key != expected_account
        or str(resolved_account.get("confidence") or "") != "high"
        or str(resolved_account.get("evidence_source") or "") not in {"url_parameter", "data_attribute"}
    ):
        raise ValueError("当前页面未以 URL 或页面身份属性高置信证明授权千川账户。")
    if isinstance(resolved_store, dict):
        if (
            str(resolved_store.get("key") or "").lower() != expected_store
            or str(resolved_store.get("confidence") or "") != "high"
            or str(resolved_store.get("evidence_source") or "") not in {"url_parameter", "data_attribute"}
        ):
            raise ValueError("当前页面店铺身份与授权店铺不一致。")

    raw_page_type = _canonical_execution_page_type(candidate.get("page_type"))
    expected_page_type = _canonical_execution_page_type(expected.get("page_type"))
    observed_context = build_promotion_context(candidate.get("promotion_context"))
    expected_context = build_promotion_context(action.get("promotion_context"))
    observed_mode = str(observed_context.get("promotion_mode") or "unknown")
    expected_mode = str(expected_context.get("promotion_mode") or "unknown")
    if raw_page_type != expected_page_type or observed_mode != expected_mode:
        raise ValueError("当前页面类型或投放模式与授权动作不一致。")

    account = {
        **bound_account,
        "key": expected_account,
        "store_key": expected_store,
        "label": _private_alias("account", expected_account),
        "confidence": "high",
        "identity_source": str(resolved_account.get("identity_source") or "")[:40],
        "evidence_source": str(resolved_account.get("evidence_source") or "")[:32],
    }
    store = {
        **bound_store,
        "key": expected_store,
        "label": _private_alias("store", expected_store),
    }
    candidate["page_type"] = expected_page_type
    candidate["account"] = account
    candidate["store"] = store
    candidate["identity_resolution"] = "execution_scope_verified"
    candidate.pop("identity_conflicts", None)
    # Keep the isolated evidence subject to the same local-HMAC privacy
    # contract as canonical snapshots. Plan IDs remain operational identifiers;
    # product/SKU/material/content identifiers are replaced before disk writes.
    candidate = _resolve_commerce_entities(candidate)
    document_url = str(candidate.get("document_url") or candidate.get("url") or "")
    try:
        parsed_url = urlparse(document_url)
        if parsed_url.scheme in {"http", "https"} and parsed_url.netloc:
            candidate["document_url"] = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
    except ValueError:
        candidate["document_url"] = ""
    return candidate, store, account


def resolve_execution_identity_snapshot(
    action_id: str,
    authorization_id: str,
    data: dict[str, Any],
    execution_request: dict[str, Any],
) -> dict[str, Any]:
    context = _validated_execution_snapshot_context({
        "purpose": "preconsume_baseline",
        "action_id": action_id,
        "authorization_id": authorization_id,
        # Identity-only checks do not persist this correlation value, but use a
        # syntactically valid placeholder so the same strict session validator
        # protects both authorized and consumed probes.
        "readback_token": "0" * 64,
    })
    with _state_lock:
        session = load_execution_preflight().get("session")
        if isinstance(session, dict) and session.get("authorization_consumed") is True:
            context["purpose"] = "execution_readback"
        action, session = _execution_context_action_locked(context)
        request_digest = _validate_execution_request_binding_locked(
            action,
            session,
            context["authorization_id"],
            execution_request,
        )
        _, store, account = _resolve_execution_identity_locked(action, data)
    return {
        "ok": True,
        "account": account,
        "store": store,
        "identity_resolution": "execution_scope_verified",
        "execution_request_digest": request_digest,
        "execution_request_bound": True,
        "persisted": False,
        "execution_enabled": False,
    }


def save_targeted_execution_snapshot(
    source: str,
    data: dict[str, Any],
    expected_scope: dict[str, Any],
    execution_context: dict[str, Any],
) -> dict[str, Any]:
    if source != "qianchuan":
        raise ValueError("执行定向快照只支持千川计划页面。")
    context = _validated_execution_snapshot_context(execution_context)
    with _state_lock:
        action, _ = _execution_context_action_locked(context)
        expected = _execution_request_for_action(action)
        expected_store = str(expected.get("store_key") or "").lower()
        expected_account = str(expected.get("account_key") or "").lower()
        if (
            str(expected_scope.get("store_key") or "").strip().lower() != expected_store
            or str(expected_scope.get("account_key") or "").strip().lower() != expected_account
        ):
            raise ValueError("执行定向快照的店铺或账户范围与动作不一致。")
        quality = data.get("quality") if isinstance(data, dict) and isinstance(data.get("quality"), dict) else {}
        target_plan_id = str(quality.get("target_plan_id") or "").strip().lower()
        expected_plan_id = str(expected.get("plan_id") or "").strip().lower()
        if (
            quality.get("targeted") is not True
            or quality.get("scope") != "target_plan"
            or quality.get("coverage_complete") is not False
            or quality.get("collection_complete") is not False
            or quality.get("target_plan_found") is not True
            or target_plan_id != expected_plan_id
        ):
            raise ValueError("执行定向快照不是精确目标计划证据，且不得声明全量覆盖。")
        normalized, store, account = _resolve_execution_identity_locked(action, data)
        captured_at_ms = int(normalized.get("captured_at") or normalized.get("timestamp") or 0)
        now_ms = int(time.time() * 1000)
        if captured_at_ms <= 0 or captured_at_ms > now_ms + MAX_CAPTURE_FUTURE_SKEW_MS:
            raise ValueError("执行定向快照时间无效。")
        wrapper = {
            "schema_version": 1,
            "source": "qianchuan",
            "page_type": str(normalized.get("page_type") or ""),
            "data": normalized,
            "timestamp": captured_at_ms / 1000,
            "saved_at": _now_label(),
            "execution_context": context,
            "snapshot_ref": f"execution_readbacks/{context['action_id']}/{context['readback_token']}",
            "storage_provenance": {
                "producer": "browser_bridge_execution_readback_v1",
                "persistence_policy": "isolated_immutable_target_snapshot",
                "canonical_plan_data_modified": False,
            },
        }
        if _find_plan_readback(
            action,
            readback_token=context["readback_token"],
            expected_purpose=context["purpose"],
            authorization_id=context["authorization_id"],
            snapshot_override=wrapper,
        ) is None:
            raise ValueError("执行定向快照未找到唯一且与动作一致的目标计划。")
        path = _execution_readback_snapshot_path(context["action_id"], context["readback_token"])
        if path.exists():
            existing = _load_execution_readback_snapshot(context["action_id"], context["readback_token"])
            existing_digest = hashlib.sha256(json.dumps({
                "data": (existing or {}).get("data"),
                "execution_context": (existing or {}).get("execution_context"),
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            candidate_digest = hashlib.sha256(json.dumps({
                "data": wrapper["data"],
                "execution_context": wrapper["execution_context"],
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            if not hmac.compare_digest(existing_digest, candidate_digest):
                raise ValueError("执行回读令牌已绑定另一份快照，拒绝覆盖。")
            idempotent = True
        else:
            _atomic_json_write(path, wrapper)
            idempotent = False
    return {
        "ok": True,
        "accepted": True,
        "accepted_for_current_data": False,
        "current_snapshot_updated": False,
        "source": source,
        "page_type": wrapper["page_type"],
        "account": account,
        "store": store,
        "identity_resolution": "execution_scope_verified",
        "targeted_execution_snapshot": True,
        "canonical_plan_data_modified": False,
        "snapshot_ref": wrapper["snapshot_ref"],
        "idempotent_replay": idempotent,
    }


def _execution_preflight_path() -> Path:
    return DATA_DIR / "execution_preflight.json"


def _execution_consume_transaction_path() -> Path:
    return DATA_DIR / "execution_consume_transaction.json"


def _execution_baseline_transaction_path() -> Path:
    return DATA_DIR / "execution_baseline_transaction.json"


def _manual_reconcile_archive_transaction_path() -> Path:
    return DATA_DIR / "manual_reconcile_archive_transaction.json"


def _load_execution_preflight_unrecovered() -> dict[str, Any]:
    path = _execution_preflight_path()
    if not path.exists():
        return {"schema_version": 1, "session": None, "execution_enabled": False}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.exception("读取执行前检查会话失败: %s", path)
        # Missing is a valid first-run state; present-but-corrupt is not. A
        # fail-open fallback here would forget a consumed authorization.
        raise OSError("execution preflight ledger is unreadable; supervised execution is locked") from error
    session = value.get("session") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or type(value.get("execution_enabled")) is not bool
        or (session is not None and not isinstance(session, dict))
    ):
        raise OSError("execution preflight ledger has an invalid schema; supervised execution is locked")
    return {
        "schema_version": 1,
        "updated_at": value.get("updated_at"),
        "session": session,
        "execution_enabled": False,
    }


def load_execution_preflight() -> dict[str, Any]:
    _recover_execution_baseline_transaction()
    _recover_execution_consume_transaction()
    _recover_manual_reconcile_archive_transaction()
    return _load_execution_preflight_unrecovered()


def _save_execution_preflight(session: dict[str, Any] | None) -> None:
    _atomic_json_write(
        _execution_preflight_path(),
        {"schema_version": 1, "updated_at": _now_label(), "session": session, "execution_enabled": False},
    )


def _prepare_execution_scope_change(reason: str) -> None:
    """Invalidate unconsumed grants, or block rebinding during a live write.

    Store/account identity is part of the signed action scope. Switching that
    identity after preflight must never let a later page snapshot satisfy the
    old grant. Once a one-time grant has been consumed, rebinding is blocked
    until the exact-scope result is independently read back.
    """

    with _state_lock:
        session = load_execution_preflight().get("session")
        if not isinstance(session, dict):
            return
        state = str(session.get("state") or "")
        if state not in {
            "awaiting_reread",
            "ready_for_final_confirmation",
            "blocked",
            "authorized",
            "authorization_consumed",
            "manual_reconcile_required",
        }:
            return
        action = next(
            (
                item
                for item in load_action_audit().get("actions", [])
                if item.get("action_id") == session.get("action_id")
            ),
            None,
        )
        if (
            state in {"authorization_consumed", "manual_reconcile_required"}
            and isinstance(action, dict)
            and action.get("state") in {"executing", "succeeded"}
        ):
            raise ValueError(
                "当前投放动作已消费一次性授权且结果尚未完成原店铺回读；为避免跨店误判，暂不能切换或解绑店铺账号。"
            )
        now_ms = int(time.time() * 1000)
        _save_execution_preflight(
            {
                **session,
                "state": "invalidated",
                "invalidated_at_ms": now_ms,
                "invalidation_reason": str(reason or "scope_changed")[:80],
                "authorization_consumed": session.get("authorization_consumed") is True,
                "write_enabled": False,
                "execution_enabled": False,
            }
        )


def _invalidate_unconsumed_execution_authorization(reason: str) -> None:
    """Revoke every not-yet-consumed preflight when supervised mode is disabled."""

    with _state_lock:
        session = load_execution_preflight().get("session")
        if not isinstance(session, dict) or session.get("authorization_consumed") is True:
            return
        if str(session.get("state") or "") not in {
            "awaiting_reread", "ready_for_final_confirmation", "blocked", "authorized",
        }:
            return
        _save_execution_preflight({
            **session,
            "state": "invalidated",
            "invalidated_at_ms": int(time.time() * 1000),
            "invalidation_reason": str(reason or "execution_mode_revoked")[:80],
            "authorization_consumed": False,
            "write_enabled": False,
            "execution_enabled": False,
        })


def _require_supervised_execution_mode() -> None:
    if str(load_agent_settings().get("execution_mode") or "observe") != "supervised":
        _invalidate_unconsumed_execution_authorization("execution_mode_not_supervised")
        raise ValueError("当前已关闭受监督执行，原一次性授权已失效。")


def _clear_execution_consume_transaction() -> None:
    try:
        _execution_consume_transaction_path().unlink(missing_ok=True)
    except OSError:
        # A committed or rolled-back journal is harmless. The next read will
        # re-check both canonical files before trying to remove it again.
        logger.warning("Unable to remove the completed execution-consume journal.")


def _clear_execution_baseline_transaction() -> None:
    try:
        _execution_baseline_transaction_path().unlink(missing_ok=True)
    except OSError:
        logger.warning("Unable to remove the completed execution-baseline journal.")


def _clear_manual_reconcile_archive_transaction() -> None:
    try:
        _manual_reconcile_archive_transaction_path().unlink(missing_ok=True)
    except OSError:
        logger.warning("Unable to remove the completed manual-reconcile archive journal.")


def _recover_manual_reconcile_archive_transaction() -> None:
    """Roll an explicitly confirmed unknown-result archive pair forward.

    The action ledger is authoritative for the permanent same-plan tombstone;
    the singleton preflight file is only the current UI session. A retained
    journal must therefore repair a missing same-action session, but must never
    overwrite a newer plan session or a later verified/failed action state.
    """

    path = _manual_reconcile_archive_transaction_path()
    if not path.exists():
        return
    with _state_lock:
        if not path.exists():
            return
        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise OSError("Manual-reconcile archive journal is unreadable.") from error
        updated_action = journal.get("updated_action") if isinstance(journal, dict) else None
        updated_session = journal.get("updated_session") if isinstance(journal, dict) else None
        action_id = str(journal.get("action_id") or "") if isinstance(journal, dict) else ""
        authorization_id = str(journal.get("authorization_id") or "") if isinstance(journal, dict) else ""
        phase = str(journal.get("phase") or "") if isinstance(journal, dict) else ""
        if (
            not isinstance(journal, dict)
            or journal.get("schema_version") != 1
            or phase not in {"prepared", "committed"}
            or not re.fullmatch(r"[a-f0-9]{24}", action_id)
            or not re.fullmatch(r"[a-f0-9]{32}", authorization_id)
            or not isinstance(updated_action, dict)
            or not isinstance(updated_session, dict)
            or updated_action.get("action_id") != action_id
            or str(updated_action.get("execution_authorization_id") or "").lower() != authorization_id
            or updated_action.get("manual_reconcile_archived") is not True
            or updated_action.get("plan_retry_blocked") is not True
            or updated_session.get("action_id") != action_id
            or str(updated_session.get("authorization_id") or "").lower() != authorization_id
            or updated_session.get("authorization_consumed") is not True
            or updated_session.get("state") != "manual_reconcile_archived"
        ):
            raise OSError("Manual-reconcile archive journal binding is invalid.")

        audit = _load_action_audit_unrecovered()
        current_action = next(
            (item for item in audit.get("actions", []) if item.get("action_id") == action_id),
            None,
        )
        current_session = _load_execution_preflight_unrecovered().get("session")
        action_forward_committed = bool(
            isinstance(current_action, dict)
            and str(current_action.get("execution_authorization_id") or "").lower() == authorization_id
            and current_action.get("manual_reconcile_archived") is True
            and current_action.get("plan_retry_blocked") is True
        )
        session_forward_committed = bool(
            isinstance(current_session, dict)
            and current_session.get("action_id") == action_id
            and str(current_session.get("authorization_id") or "").lower() == authorization_id
            and current_session.get("authorization_consumed") is True
            and current_session.get("state") == "manual_reconcile_archived"
        )
        session_superseded = bool(
            isinstance(current_session, dict)
            and (
                current_session.get("action_id") != action_id
                or str(current_session.get("authorization_id") or "").lower() != authorization_id
            )
        )
        action_resolved_after_archive = bool(
            action_forward_committed
            and str(current_action.get("state") or "") in {"verified", "failed", "rolled_back"}
        )

        if phase == "committed" and not action_forward_committed:
            raise OSError("Committed manual-reconcile archive no longer matches its action record.")
        if action_forward_committed:
            if (
                not session_forward_committed
                and not session_superseded
                and not action_resolved_after_archive
            ):
                _save_execution_preflight(updated_session)
            _clear_manual_reconcile_archive_transaction()
            return
        if session_superseded:
            raise OSError("Prepared manual-reconcile archive was superseded before its action committed.")
        if (
            not isinstance(current_action, dict)
            or str(current_action.get("execution_authorization_id") or "").lower() != authorization_id
            or current_action.get("state") not in {"executing", "succeeded"}
            or current_action.get("manual_reconcile_required") is not True
        ):
            raise OSError("Prepared manual-reconcile archive no longer matches an unresolved action.")

        actions = [
            updated_action if item.get("action_id") == action_id else item
            for item in audit.get("actions", [])
            if isinstance(item, dict)
        ]
        _atomic_json_write(
            _action_audit_path(),
            {
                "schema_version": 1,
                "updated_at": _now_label(),
                "execution_enabled": False,
                "actions": actions,
            },
        )
        _save_execution_preflight(updated_session)
        _clear_manual_reconcile_archive_transaction()


def _recover_execution_baseline_transaction() -> None:
    """Roll an interrupted final-reread baseline pair forward atomically."""

    path = _execution_baseline_transaction_path()
    if not path.exists():
        return
    with _state_lock:
        if not path.exists():
            return
        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise OSError("Execution-baseline recovery journal is unreadable.") from error
        updated_action = journal.get("updated_action") if isinstance(journal, dict) else None
        updated_session = journal.get("updated_session") if isinstance(journal, dict) else None
        action_id = str(journal.get("action_id") or "") if isinstance(journal, dict) else ""
        authorization_id = str(journal.get("authorization_id") or "") if isinstance(journal, dict) else ""
        baseline_hash = str(journal.get("execution_baseline_hash") or "") if isinstance(journal, dict) else ""
        if (
            not isinstance(journal, dict)
            or journal.get("schema_version") != 1
            or str(journal.get("phase") or "") not in {"prepared", "committed"}
            or not isinstance(updated_action, dict)
            or not isinstance(updated_session, dict)
            or updated_action.get("action_id") != action_id
            or updated_session.get("action_id") != action_id
            or updated_session.get("authorization_id") != authorization_id
            or updated_action.get("execution_baseline_hash") != baseline_hash
            or updated_session.get("execution_baseline_hash") != baseline_hash
            or not re.fullmatch(r"[a-f0-9]{64}", baseline_hash)
        ):
            raise OSError("Execution-baseline recovery journal binding is invalid.")
        audit = _load_action_audit_unrecovered()
        current_action = next(
            (item for item in audit.get("actions", []) if item.get("action_id") == action_id),
            None,
        )
        current_session = _load_execution_preflight_unrecovered().get("session")
        action_forward_committed = bool(
            isinstance(current_action, dict)
            and current_action.get("execution_baseline_hash") == baseline_hash
        )
        session_forward_committed = bool(
            isinstance(current_session, dict)
            and current_session.get("execution_baseline_hash") == baseline_hash
            and current_session.get("authorization_id") == authorization_id
        )
        session_superseded = bool(
            isinstance(current_session, dict)
            and (
                current_session.get("action_id") != action_id
                or current_session.get("authorization_id") != authorization_id
            )
        )
        phase = str(journal.get("phase") or "")
        if phase == "committed" and action_forward_committed:
            # A later preflight may legitimately replace the singleton session
            # after this pair committed. A retained Windows journal is then a
            # tombstone, never an instruction to overwrite the newer session.
            if not session_forward_committed and not session_superseded and current_action.get("state") == "confirmed":
                _save_execution_preflight(updated_session)
            _clear_execution_baseline_transaction()
            return
        if phase == "committed":
            raise OSError("Committed execution-baseline journal no longer matches its action record.")
        if action_forward_committed and (session_forward_committed or session_superseded):
            _clear_execution_baseline_transaction()
            return
        if session_superseded:
            raise OSError("Prepared execution-baseline journal was superseded before its action committed.")
        if not (action_forward_committed and session_forward_committed):
            actions = [
                updated_action if item.get("action_id") == action_id else item
                for item in audit.get("actions", [])
                if isinstance(item, dict)
            ]
            if not any(item.get("action_id") == action_id for item in actions):
                actions.append(updated_action)
            _atomic_json_write(
                _action_audit_path(),
                {"schema_version": 1, "updated_at": _now_label(), "execution_enabled": False, "actions": actions},
            )
            _save_execution_preflight(updated_session)
        _clear_execution_baseline_transaction()


def _recover_execution_consume_transaction() -> None:
    """Repair an interrupted action/preflight consume pair before any read.

    The browser must never observe ``action=executing`` together with an
    unconsumed ``preflight=authorized`` grant. The journal contains only the
    exact before/after records and remains fail-closed if it is damaged.
    """

    path = _execution_consume_transaction_path()
    if not path.exists():
        return
    with _state_lock:
        if not path.exists():
            return
        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise OSError("Execution-consume recovery journal is unreadable.") from error
        if not isinstance(journal, dict) or journal.get("schema_version") != 1:
            raise OSError("Execution-consume recovery journal is invalid.")
        previous_action = journal.get("previous_action")
        executing_action = journal.get("executing_action")
        previous_session = journal.get("previous_session")
        consumed_session = journal.get("consumed_session")
        action_id = str(journal.get("action_id") or "")
        authorization_id = str(journal.get("authorization_id") or "")
        if (
            not isinstance(previous_action, dict)
            or not isinstance(executing_action, dict)
            or not isinstance(previous_session, dict)
            or not isinstance(consumed_session, dict)
            or previous_action.get("action_id") != action_id
            or executing_action.get("action_id") != action_id
            or previous_session.get("authorization_id") != authorization_id
            or consumed_session.get("authorization_id") != authorization_id
        ):
            raise OSError("Execution-consume recovery journal binding is invalid.")

        audit = _load_action_audit_unrecovered()
        current_action = next(
            (item for item in audit.get("actions", []) if item.get("action_id") == action_id),
            None,
        )
        current_session = _load_execution_preflight_unrecovered().get("session")
        action_forward_committed = bool(
            isinstance(current_action, dict)
            and current_action.get("execution_attempt_id") == authorization_id
            and current_action.get("state") in {"executing", "succeeded", "verified", "failed", "rolled_back"}
        )
        session_forward_committed = bool(
            isinstance(current_session, dict)
            and current_session.get("authorization_consumed") is True
            and current_session.get("authorization_id") == authorization_id
        )
        committed = action_forward_committed and session_forward_committed
        session_superseded = bool(
            isinstance(current_session, dict)
            and (
                current_session.get("action_id") != action_id
                or current_session.get("authorization_id") != authorization_id
            )
        )
        rolled_back = bool(
            isinstance(current_action, dict)
            and current_action.get("state") == "confirmed"
            and isinstance(current_session, dict)
            and current_session.get("state") == "authorized"
            and current_session.get("authorization_consumed") is not True
            and current_session.get("authorization_id") == authorization_id
        )
        phase = str(journal.get("phase") or "prepared")
        if phase == "committed":
            if not action_forward_committed:
                raise OSError("Committed execution-consume journal no longer matches canonical state.")
            if not session_forward_committed and not session_superseded:
                # The action is already irreversibly locked. Repair a missing
                # or stale same-authorization singleton forward; never revive
                # the previous reusable authorization.
                _save_execution_preflight(consumed_session)
            _clear_execution_consume_transaction()
            return
        if phase != "prepared":
            raise OSError("Execution-consume recovery journal phase is invalid.")
        if committed or rolled_back or (action_forward_committed and session_superseded):
            _clear_execution_consume_transaction()
            return
        if session_superseded:
            raise OSError("Prepared execution-consume journal was superseded before its action committed.")

        # Recovery defaults to the pre-write pair. No browser submit request
        # was returned unless both canonical writes completed, so rolling back
        # is the only retry-safe choice for a partial commit.
        restored_actions = [
            previous_action if item.get("action_id") == action_id else item
            for item in audit.get("actions", [])
            if isinstance(item, dict)
        ]
        if not any(item.get("action_id") == action_id for item in restored_actions):
            restored_actions.append(previous_action)
        _atomic_json_write(
            _action_audit_path(),
            {
                "schema_version": 1,
                "updated_at": _now_label(),
                "execution_enabled": False,
                "actions": restored_actions,
            },
        )
        _save_execution_preflight(previous_session)
        _clear_execution_consume_transaction()


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
        executed_at = int(item.get("execution_reported_at_ms") or item.get("execution_started_at_ms") or 0)
        # A platform write is an irreversible quota event even when a later
        # recovery action returns the plan to its original value.  Keeping the
        # original action in ``rolled_back`` must not make that write disappear
        # from the daily attempt ledger.
        counts_as_execution = item.get("state") in {"succeeded", "verified", "rolled_back"} or (
            item.get("state") == "executing" and item.get("manual_reconcile_required") is True
        )
        if not counts_as_execution or item_target.get("account_key") != account_key or executed_at <= 0:
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
    last_execution_ms = max(
        (
            int(item.get("execution_reported_at_ms") or item.get("execution_started_at_ms") or 0)
            for item in completed
        ),
        default=0,
    )
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
    original_evidence = original.get("evidence_ref") if isinstance(original.get("evidence_ref"), dict) else {}
    current_value = (readback or {}).get("current_value")
    reduced_value = change.get("target_value")
    original_value = change.get("current_value")
    if not readback or int(readback.get("captured_at_ms") or 0) <= int(original.get("execution_reported_at_ms") or 0):
        raise ValueError("缺少执行后的新页面数据，请先重新读取当前千川计划页。")
    if not all(isinstance(value, (int, float)) for value in (current_value, reduced_value, original_value)):
        raise ValueError("预算回读数据不完整，不能生成回滚。")
    if abs(float(current_value) - float(reduced_value)) > 0.01:
        raise ValueError("当前预算已被再次修改，不能按旧记录回滚。")

    original_page_type = str(original_evidence.get("page_type") or "").strip().lower()
    readback_page_type = str((readback or {}).get("page_type") or "").strip().lower()

    def canonical_page_type(value: str) -> str:
        if value == "qianchuan_live":
            return "qianchuan_live"
        if value in {"campaigns", "qianchuan_campaigns"}:
            return "campaigns"
        return ""

    canonical_original_page = canonical_page_type(original_page_type)
    canonical_readback_page = canonical_page_type(readback_page_type)
    if not canonical_original_page:
        raise ValueError("原动作缺少可验证的计划页面类型，不能生成回滚。")
    if canonical_readback_page and canonical_readback_page != canonical_original_page:
        raise ValueError("回滚回读页面类型与原动作不一致，不能生成回滚。")
    rollback_page_type = original_page_type

    expected_plan_type = "live" if canonical_original_page == "qianchuan_live" else "product"
    original_plan_type = str(original_evidence.get("plan_type") or expected_plan_type).strip().lower()
    readback_plan_type = str((readback or {}).get("plan_type") or original_plan_type).strip().lower()
    if original_plan_type != expected_plan_type or readback_plan_type != original_plan_type:
        raise ValueError("回滚计划类型与原动作或回读不一致，不能生成回滚。")

    promotion_context = build_promotion_context(original.get("promotion_context"))
    expected_scope_key = _plan_collection_scope_key(
        target.get("account_key"),
        promotion_context.get("promotion_mode"),
        original_plan_type,
    )
    original_scope_key = str(original_evidence.get("collection_scope_key") or "").strip()
    readback_scope_key = str((readback or {}).get("collection_scope_key") or "").strip()
    if any(
        scope_key and scope_key != expected_scope_key
        for scope_key in (original_scope_key, readback_scope_key)
    ):
        raise ValueError("回滚采集作用域与原动作或回读不一致，不能生成回滚。")
    rollback_scope_key = original_scope_key or readback_scope_key or expected_scope_key
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
        page_type=rollback_page_type,
        captured_at_ms=int(readback.get("captured_at_ms") or 0),
        quality_score=int(readback.get("quality_score") or 0),
        confidence="high",
        evidence={
            "rollback_of_action_id": action_id,
            "spend": readback.get("spend"),
            "roi": readback.get("roi"),
            "orders": readback.get("orders"),
            "plan_type": original_plan_type,
            "collection_scope_key": rollback_scope_key,
            **(
                {"collection_scope_required": True}
                if original_evidence.get("collection_scope_required") is True
                else {}
            ),
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
    _validate_action_server_seal(action)
    collection_gate = _supervised_draft_collection_gate(action)
    if collection_gate["ready"] is not True:
        next_action = collection_gate.get("next_action") if isinstance(collection_gate.get("next_action"), dict) else {}
        detail = str(next_action.get("detail") or next_action.get("label") or "请重新读取当前计划范围。")
        raise ValueError(f"计划采集范围尚未通过受监督执行检查：{detail}")
    promotion_guard = legacy_execution_guard(
        action.get("operation_type"),
        action.get("promotion_context"),
        expected_account_key=_action_target_account_key(action),
    )
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
        "action_integrity_hash": str(action.get("integrity_hash") or ""),
        "action_server_signature": str(action.get("server_signature") or ""),
        "store_key": str((action.get("proposal_scope") or {}).get("store_key") or ""),
        "account_key": _action_target_account_key(action),
        "binding_generation": int((action.get("proposal_scope") or {}).get("binding_generation") or 0),
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
        _require_supervised_execution_mode()
        current_audit = load_action_audit()
        current_action = next(
            (item for item in current_audit.get("actions", []) if item.get("action_id") == action_id),
            None,
        )
        if not current_action or current_action.get("state") != "confirmed":
            raise ValueError("方案在启动检查前已经变化，请重新生成。")
        current_store_key = str((current_action.get("proposal_scope") or {}).get("store_key") or "").strip().lower()
        current_account_key = _action_target_account_key(current_action)
        current_target = current_action.get("target_ref") if isinstance(current_action.get("target_ref"), dict) else {}
        current_plan_id = str(current_target.get("id") or "").strip().lower()
        permanent_retry_lock = next(
            (
                item for item in current_audit.get("actions", [])
                if item.get("action_id") != action_id
                and item.get("plan_retry_blocked") is True
                and _action_target_account_key(item) == current_account_key
                and str((item.get("proposal_scope") or {}).get("store_key") or "").strip().lower() == current_store_key
                and str((item.get("target_ref") or {}).get("id") or "").strip().lower() == current_plan_id
            ),
            None,
        )
        if permanent_retry_lock:
            raise ValueError(
                "该计划存在已归档的未知执行结果，为避免重复扣费或重复改价，已永久禁止自动重投；"
                "只能在千川官方后台人工核对。"
            )
        manual_reconcile_write = next(
            (
                item for item in current_audit.get("actions", [])
                if item.get("action_id") != action_id
                and item.get("state") in {"executing", "succeeded"}
                and item.get("manual_reconcile_required") is True
                and item.get("manual_reconcile_archived") is not True
            ),
            None,
        )
        if manual_reconcile_write:
            raise ValueError("仍有投放动作等待人工核对；当前版本只维护一个安全回读会话，完成原动作验收前不能启动其他账户的新计划。")
        pending_write = next(
            (
                item for item in current_audit.get("actions", [])
                if item.get("action_id") != action_id
                and item.get("state") in {"executing", "succeeded"}
                and item.get("manual_reconcile_archived") is not True
                and _action_target_account_key(item) == current_account_key
                and str((item.get("proposal_scope") or {}).get("store_key") or "").strip().lower() == current_store_key
            ),
            None,
        )
        if pending_write:
            raise ValueError("当前店铺与广告账户仍有投放动作等待页面回执、验收或人工核对；完成原动作回读前不能启动新计划。")
        existing = load_execution_preflight().get("session")
        if isinstance(existing, dict):
            existing_state = str(existing.get("state") or "")
            existing_action = next(
                (
                    item for item in current_audit.get("actions", [])
                    if item.get("action_id") == existing.get("action_id")
                ),
                None,
            )
            if existing_state == "authorization_consumed" and isinstance(existing_action, dict):
                existing_action_state = str(existing_action.get("state") or "")
                if existing_action_state == "executing" and existing_action.get("manual_reconcile_required") is True:
                    existing_state = "manual_reconcile_required"
                    existing = {
                        **existing,
                        "state": existing_state,
                        "manual_reconcile_required_at_ms": int(
                            existing_action.get("manual_reconcile_required_at_ms") or time.time() * 1000
                        ),
                    }
                    _save_execution_preflight(existing)
                elif existing_action_state in {"verified", "rolled_back", "failed"}:
                    existing_state = "execution_failed" if existing_action_state == "failed" else "completed"
                    existing = {
                        **existing,
                        "state": existing_state,
                        "execution_receipt_action_state": existing_action_state,
                        "completed_at_ms": int(time.time() * 1000),
                    }
                    _save_execution_preflight(existing)
            active_states = {
                "awaiting_reread",
                "ready_for_final_confirmation",
                "blocked",
                "authorized",
                "authorization_consumed",
                "manual_reconcile_required",
            }
            if existing_state in active_states and (
                existing.get("action_id") != action_id
                or existing_state in {"authorized", "authorization_consumed"}
            ):
                raise ValueError("已有另一执行检查或一次性授权未结束，请先停止未消费检查或完成原计划回读。")
        _validate_confirmed_action_for_execution(current_action, session)
        current_quota = assess_execution_quota(current_action)
        if not current_quota["allowed"]:
            raise ValueError("；".join(item["message"] for item in current_quota["blocked_reasons"]))
        session["quota"] = current_quota
        _save_execution_preflight(session)
    return build_execution_preflight_report()


def stop_execution_preflight(session_id: str) -> dict[str, Any]:
    session_id = str(session_id or "").lower()
    with _state_lock:
        stored = load_execution_preflight().get("session")
        if not stored or stored.get("session_id") != session_id:
            raise ValueError("未找到对应的执行前检查会话。")
        if stored.get("state") == "authorization_consumed" or stored.get("authorization_consumed") is True:
            raise ValueError("一次性授权已消费，页面结果尚未完成回读；不能把进行中的动作标记为已停止。")
        stopped = {
            **stored,
            "state": "stopped",
            "stopped_at_ms": int(time.time() * 1000),
            "write_enabled": False,
            "execution_enabled": False,
        }
        _save_execution_preflight(stopped)
    return build_execution_preflight_report()


def _manual_reconcile_archive_confirmation_text(action: dict[str, Any]) -> str:
    action_id = str(action.get("action_id") or "").lower()
    suffix = action_id[-8:] if re.fullmatch(r"[a-f0-9]{24}", action_id) else "无效编号"
    return f"确认归档未决动作 {suffix} 并永久禁止该计划自动重投"


def _manual_reconcile_archive_summary(action: dict[str, Any]) -> dict[str, Any]:
    target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
    scope = action.get("proposal_scope") if isinstance(action.get("proposal_scope"), dict) else {}
    return {
        "action_id": str(action.get("action_id") or ""),
        "store_key": str(scope.get("store_key") or ""),
        "account_key": _action_target_account_key(action),
        "plan_id": str(target.get("id") or ""),
        "plan_name": str(target.get("name") or ""),
        "archived_at_ms": int(action.get("manual_reconcile_archived_at_ms") or 0),
        "result_known": False,
        "safe_to_retry": False,
        "plan_retry_blocked": action.get("plan_retry_blocked") is True,
        "unrelated_plans_released": action.get("manual_reconcile_archived") is True,
        "readback_still_available": True,
    }


def archive_manual_reconcile(
    action_id: str,
    confirmation_text: str,
    resolution_note: str = "",
) -> dict[str, Any]:
    """Archive an unknown result without pretending success, failure or retry safety."""

    action_id = str(action_id or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{24}", action_id):
        raise ValueError("未决动作编号无效。")
    resolution_note = str(resolution_note or "").strip()
    if len(resolution_note) > 300:
        raise ValueError("人工核对备注不能超过 300 个字符。")

    with _state_lock:
        audit = load_action_audit()
        action = next(
            (item for item in audit.get("actions", []) if item.get("action_id") == action_id),
            None,
        )
        if not isinstance(action, dict):
            raise ValueError("未找到对应的未决动作。")
        expected_text = _manual_reconcile_archive_confirmation_text(action)
        if confirmation_text != expected_text:
            raise ValueError("归档确认口令不一致；未修改任何动作。")
        if action.get("manual_reconcile_archived") is True:
            if action.get("plan_retry_blocked") is not True:
                raise OSError("Archived manual-reconcile action lost its permanent retry tombstone.")
            return {
                "archived": _manual_reconcile_archive_summary(action),
                "preflight": build_execution_preflight_report(),
                "idempotent_replay": True,
            }
        if (
            action.get("state") not in {"executing", "succeeded"}
            or action.get("manual_reconcile_required") is not True
        ):
            raise ValueError("只有结果仍不确定且正在锁定的动作可以归档。")
        _validate_action_server_seal(action)
        authorization_id = str(action.get("execution_authorization_id") or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{32}", authorization_id):
            raise ValueError("未决动作缺少有效的一次性授权绑定，保持锁定。")
        session = load_execution_preflight().get("session")
        if (
            not isinstance(session, dict)
            or session.get("action_id") != action_id
            or str(session.get("authorization_id") or "").strip().lower() != authorization_id
            or session.get("authorization_consumed") is not True
            or str(session.get("state") or "") not in {
                "authorization_consumed", "manual_reconcile_required",
            }
        ):
            raise ValueError("当前会话与未决动作的一次性授权不一致，保持锁定。")
        _validate_action_session_binding(action, session)

        now_ms = int(time.time() * 1000)
        updated_action = {
            **action,
            "manual_reconcile_archived": True,
            "manual_reconcile_archived_at_ms": now_ms,
            "manual_reconcile_archive_resolution": "operator_archived_result_unknown",
            "manual_reconcile_archive_note": resolution_note,
            "plan_retry_blocked": True,
            "plan_retry_blocked_at_ms": now_ms,
            "plan_retry_block_reason": "platform_result_unknown_after_consumed_authorization",
            "state_updated_at_ms": now_ms,
            "execution_note": (
                "平台提交结果仍不确定；用户已显式归档未决动作。未将其标记为成功或失败，"
                "同一店铺、账户和计划永久禁止自动重投；其他计划可继续受监督执行。"
            ),
        }
        updated_session = {
            **session,
            "state": "manual_reconcile_archived",
            "manual_reconcile_archived": True,
            "manual_reconcile_archived_at_ms": now_ms,
            "plan_retry_blocked": True,
            "execution_receipt_action_state": str(action.get("state") or ""),
            "write_enabled": False,
            "execution_enabled": False,
        }
        journal = {
            "schema_version": 1,
            "phase": "prepared",
            "prepared_at_ms": now_ms,
            "action_id": action_id,
            "authorization_id": authorization_id,
            "updated_action": updated_action,
            "updated_session": updated_session,
        }
        _atomic_json_write(_manual_reconcile_archive_transaction_path(), journal)
        try:
            _atomic_json_write(
                _action_audit_path(),
                {
                    "schema_version": 1,
                    "updated_at": _now_label(),
                    "execution_enabled": False,
                    "actions": [
                        updated_action if item.get("action_id") == action_id else item
                        for item in audit.get("actions", [])
                    ],
                },
            )
            _save_execution_preflight(updated_session)
            _atomic_json_write(
                _manual_reconcile_archive_transaction_path(),
                {**journal, "phase": "committed", "committed_at_ms": int(time.time() * 1000)},
            )
        except OSError:
            try:
                _recover_manual_reconcile_archive_transaction()
            except OSError:
                logger.exception("Manual-reconcile archive recovery is deferred to the next state read.")
            raise
        _clear_manual_reconcile_archive_transaction()

    return {
        "archived": _manual_reconcile_archive_summary(updated_action),
        "preflight": build_execution_preflight_report(),
        "idempotent_replay": False,
    }


def authorize_execution_preflight(session_id: str, confirmation_text: str) -> dict[str, Any]:
    """Serialize final authorization with stop, scope-change and report CAS."""

    with _state_lock:
        return _authorize_execution_preflight_locked(session_id, confirmation_text)


def _execution_confirmation_text(operation_type: str, target_value: Any) -> str:
    """Return the one canonical confirmation phrase rendered by the UI."""

    if operation_type == "restore_budget":
        return f"确认恢复预算至{target_value}"
    if operation_type == "pause_plan":
        return "确认暂停该计划"
    return f"确认降低预算至{target_value}"


def _authorize_execution_preflight_locked(session_id: str, confirmation_text: str) -> dict[str, Any]:
    """Issue a short-lived, single-use grant after an exact final confirmation.

    The grant is deliberately not an execution command.  A future browser
    executor must consume it atomically and perform its own final page checks.
    """

    _require_supervised_execution_mode()
    session_id = str(session_id or "").lower()
    if not re.fullmatch(r"[a-f0-9]{24}", session_id):
        raise ValueError("执行前检查会话编号无效。")
    report = build_execution_preflight_report()
    session = report.get("session") if isinstance(report.get("session"), dict) else {}
    if session.get("session_id") != session_id:
        raise ValueError("未找到对应的执行前检查会话。")
    if report.get("state") != "ready_for_final_confirmation":
        raise ValueError("执行前检查尚未全部通过，不能生成最终授权。")
    canonical_action = next(
        (
            item for item in load_action_audit().get("actions", [])
            if item.get("action_id") == session.get("action_id")
        ),
        None,
    )
    if not canonical_action or canonical_action.get("state") != "confirmed":
        raise ValueError("授权对应的动作不可执行。")
    try:
        _validate_confirmed_action_for_execution(canonical_action, session)
    except ValueError:
        _save_execution_preflight({
            **session,
            "state": "invalidated",
            "invalidated_at_ms": int(time.time() * 1000),
            "invalidation_reason": "execution_scope_or_collection_changed",
            "authorization_consumed": False,
            "execution_enabled": False,
            "write_enabled": False,
        })
        raise
    action = report.get("action") if isinstance(report.get("action"), dict) else {}
    expected_text = _execution_confirmation_text(
        str(session.get("operation_type") or ""),
        action.get("target_value"),
    )
    if str(confirmation_text or "").strip() != expected_text:
        raise ValueError(f"确认口令不一致，请完整输入：{expected_text}")

    now_ms = int(time.time() * 1000)
    readback = report.get("readback") if isinstance(report.get("readback"), dict) else {}
    execution_baseline = _execution_baseline_from_readback(canonical_action, readback)
    baseline_hash = _execution_baseline_hash(execution_baseline)
    grant_seed = f"{session_id}:{session.get('action_id')}:{now_ms}:{os.urandom(16).hex()}".encode("utf-8")
    authorized = {
        **session,
        "state": "authorized",
        "authorized_at_ms": now_ms,
        "authorization_expires_at_ms": now_ms + 60 * 1000,
        "authorization_id": hashlib.sha256(grant_seed).hexdigest()[:32],
        "authorization_consumed": False,
        "execution_baseline_hash": baseline_hash,
        "authorized_baseline_document_instance_id": str(execution_baseline.get("document_instance_id") or ""),
        "confirmation_text_hash": hashlib.sha256(expected_text.encode("utf-8")).hexdigest(),
        "write_enabled": False,
        "execution_enabled": False,
    }
    with _state_lock:
        audit = load_action_audit()
        bound_action = next(
            (item for item in audit.get("actions", []) if item.get("action_id") == canonical_action.get("action_id")),
            None,
        )
        if not bound_action or bound_action.get("state") != "confirmed":
            raise ValueError("授权对应的动作已经变化，请重新开始执行前检查。")
        _validate_confirmed_action_for_execution(bound_action, session)
        bound_action = {
            **bound_action,
            "execution_baseline": execution_baseline,
            "execution_baseline_hash": baseline_hash,
            "preflight_authorized_at_ms": now_ms,
        }
        baseline_journal = {
            "schema_version": 1,
            "phase": "prepared",
            "prepared_at_ms": now_ms,
            "action_id": str(bound_action.get("action_id") or ""),
            "authorization_id": str(authorized.get("authorization_id") or ""),
            "execution_baseline_hash": baseline_hash,
            "updated_action": bound_action,
            "updated_session": authorized,
        }
        _atomic_json_write(_execution_baseline_transaction_path(), baseline_journal)
        try:
            _atomic_json_write(
                _action_audit_path(),
                {
                    "schema_version": 1,
                    "updated_at": _now_label(),
                    "execution_enabled": False,
                    "actions": [
                        bound_action if item.get("action_id") == bound_action.get("action_id") else item
                        for item in audit.get("actions", [])
                    ],
                },
            )
            _save_execution_preflight(authorized)
            _atomic_json_write(
                _execution_baseline_transaction_path(),
                {**baseline_journal, "phase": "committed", "committed_at_ms": int(time.time() * 1000)},
            )
        except OSError:
            try:
                _recover_execution_baseline_transaction()
            except OSError:
                logger.exception("Execution-baseline authorization recovery is deferred to the next state read.")
            raise
        _clear_execution_baseline_transaction()
    return build_execution_preflight_report()


def _canonical_execution_page_type(value: Any) -> str:
    page_type = str(value or "").strip().lower()
    return "campaigns" if page_type == "qianchuan_campaigns" else page_type


def _validated_execution_readback_context(
    action: dict[str, Any],
    baseline: dict[str, Any],
    value: Any,
) -> dict[str, Any]:
    """Validate optional browser scope metadata persisted for job recovery."""

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("执行回读上下文无效，授权未消费。")
    scope_hash = str(value.get("scope_hash") or "").strip().lower()
    document_id = str(value.get("document_instance_id") or "").strip()
    navigation_started_at_ms = value.get("navigation_started_at_ms")
    promotion_mode = str(value.get("promotion_mode") or "unknown").strip().lower()
    page_type = _canonical_execution_page_type(value.get("page_type"))
    expected_mode = str(build_promotion_context(action.get("promotion_context")).get("promotion_mode") or "unknown")
    expected_page_type = _canonical_execution_page_type((action.get("evidence_ref") or {}).get("page_type"))
    if (
        not re.fullmatch(r"[a-f0-9]{64}", scope_hash)
        or document_id != str(baseline.get("document_instance_id") or "")
        or isinstance(navigation_started_at_ms, bool)
        or not isinstance(navigation_started_at_ms, int)
        or navigation_started_at_ms != int(baseline.get("navigation_started_at_ms") or 0)
        or promotion_mode != expected_mode
        or not page_type
        or page_type != expected_page_type
    ):
        raise ValueError("执行回读上下文与最终硬刷新基线不一致，授权未消费。")
    return {
        "scope_hash": scope_hash,
        "document_instance_id": document_id,
        "navigation_started_at_ms": navigation_started_at_ms,
        "promotion_mode": promotion_mode,
        "page_type": page_type,
    }


def consume_execution_authorization(
    authorization_id: str,
    readback_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically consume a valid grant for a future in-process executor."""

    authorization_id = str(authorization_id or "").lower()
    if not re.fullmatch(r"[a-f0-9]{32}", authorization_id):
        raise ValueError("执行授权编号无效。")
    with _state_lock:
        _require_supervised_execution_mode()
        session = load_execution_preflight().get("session")
        if not session or session.get("authorization_id") != authorization_id:
            raise ValueError("未找到对应的执行授权。")
        if session.get("state") != "authorized" or session.get("authorization_consumed"):
            raise ValueError("执行授权已使用或已失效。")
        audit = load_action_audit()
        action = next(
            (item for item in audit.get("actions", []) if item.get("action_id") == session.get("action_id")),
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
        try:
            _validate_confirmed_action_for_execution(action, session)
        except ValueError:
            _save_execution_preflight({
                **session,
                "state": "invalidated",
                "invalidated_at_ms": int(time.time() * 1000),
                "invalidation_reason": "execution_scope_or_collection_changed",
                "authorization_consumed": False,
                "execution_enabled": False,
                "write_enabled": False,
            })
            raise
        baseline = action.get("execution_baseline") if isinstance(action.get("execution_baseline"), dict) else {}
        baseline_hash = str(action.get("execution_baseline_hash") or "")
        if (
            not baseline
            or not re.fullmatch(r"[a-f0-9]{64}", baseline_hash)
            or baseline_hash != _execution_baseline_hash(baseline)
            or baseline_hash != str(session.get("execution_baseline_hash") or "")
        ):
            raise ValueError("执行前指标基线缺失或已变化，授权已失效，请重新读取计划。")
        now_ms = int(time.time() * 1000)
        authorized_at_ms = int(session.get("authorized_at_ms") or 0)
        final_reread_at_ms = int(session.get("final_preconsume_reread_at_ms") or 0)
        final_token = str(session.get("final_preconsume_readback_token") or "")
        initial_document_id = str(session.get("authorized_baseline_document_instance_id") or "")
        final_document_id = str(session.get("final_preconsume_document_instance_id") or "")
        final_reread_valid = bool(
            final_reread_at_ms > authorized_at_ms > 0
            and final_reread_at_ms <= now_ms
            and now_ms - final_reread_at_ms <= 20_000
            and re.fullmatch(r"[a-f0-9]{64}", final_token)
            and final_token == str(action.get("final_preconsume_readback_token") or "")
            and final_token == str(baseline.get("readback_token") or "")
            and str(baseline.get("readback_purpose") or "") == "preconsume_baseline"
            and str(session.get("final_preconsume_authorization_id") or "") == authorization_id
            and str(action.get("final_preconsume_authorization_id") or "") == authorization_id
            and int(action.get("final_preconsume_reread_at_ms") or 0) == final_reread_at_ms
            and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", initial_document_id)
            and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", final_document_id)
            and final_document_id != initial_document_id
            and final_document_id == str(baseline.get("document_instance_id") or "")
        )
        if not final_reread_valid:
            _save_execution_preflight({
                **session,
                "state": "invalidated",
                "invalidated_at_ms": now_ms,
                "invalidation_reason": "final_preconsume_reread_missing_stale_or_unbound",
                "authorization_consumed": False,
                "execution_enabled": False,
                "write_enabled": False,
            })
            raise ValueError("必须先完成本次授权绑定的最终硬刷新复核，并在 20 秒内消费授权。")
        recovery_context = _validated_execution_readback_context(action, baseline, readback_context)
        promotion_guard = legacy_execution_guard(
            action.get("operation_type"),
            action.get("promotion_context"),
            expected_account_key=_action_target_account_key(action),
        )
        if not promotion_guard["allowed"]:
            raise ValueError(f"{promotion_guard['code']}：{promotion_guard['reason']}")
        quota = assess_execution_quota(action)
        if not quota["allowed"]:
            raise ValueError("；".join(item["message"] for item in quota["blocked_reasons"]))
        now_ms = int(time.time() * 1000)
        if int(session.get("authorization_expires_at_ms") or 0) <= now_ms:
            _save_execution_preflight({**session, "state": "expired", "execution_enabled": False, "write_enabled": False})
            raise ValueError("执行授权已过期。")
        executing = transition_action(action, "executing", allow_execution=True)
        executing.update({
            "execution_attempt_id": authorization_id,
            "execution_started_at_ms": now_ms,
            "execution_authorization_id": authorization_id,
            "execution_note": "授权已消费，浏览器提交结果尚未确认；禁止重复执行。",
            "execution_readback_context": recovery_context,
        })
        actions = [executing if item.get("action_id") == action.get("action_id") else item for item in audit["actions"]]
        consumed = {
            **session,
            "state": "authorization_consumed",
            "authorization_consumed": True,
            "authorization_consumed_at_ms": now_ms,
            "execution_attempt_id": authorization_id,
            "execute_before_ms": now_ms + 10_000,
            "execution_readback_context": recovery_context,
            "execution_enabled": False,
            "write_enabled": False,
        }
        # The action audit and one-time grant are one logical commit. Persist a
        # recovery journal first so a power loss or second-file write failure
        # can never expose ``executing`` together with a reusable authorized
        # grant. The browser receives no submit request until both writes have
        # completed.
        consume_journal = {
            "schema_version": 1,
            "phase": "prepared",
            "prepared_at_ms": now_ms,
            "action_id": str(action.get("action_id") or ""),
            "authorization_id": authorization_id,
            "previous_action": action,
            "executing_action": executing,
            "previous_session": session,
            "consumed_session": consumed,
        }
        _atomic_json_write(_execution_consume_transaction_path(), consume_journal)
        try:
            _atomic_json_write(
                _action_audit_path(),
                {"schema_version": 1, "updated_at": _now_label(), "execution_enabled": True, "actions": actions},
            )
            _save_execution_preflight(consumed)
            # Mark the pair forward-committed before best-effort deletion. If
            # Windows temporarily denies unlink, later reads must never roll a
            # verified/failed action back into a reusable authorization.
            _atomic_json_write(
                _execution_consume_transaction_path(),
                {**consume_journal, "phase": "committed", "committed_at_ms": int(time.time() * 1000)},
            )
        except OSError:
            try:
                _recover_execution_consume_transaction()
            except OSError:
                logger.exception("Execution-consume rollback is deferred to the next state read.")
            raise
        _clear_execution_consume_transaction()
    return {
        **consumed,
        "execution_request": {
            **_execution_request_for_action(executing),
            "execution_attempt_id": authorization_id,
            "execute_before_ms": consumed["execute_before_ms"],
        },
    }


def refresh_authorized_execution_baseline(authorization_id: str, readback_token: str) -> dict[str, Any]:
    """Seal a second hard-reloaded baseline immediately before consumption."""

    authorization_id = str(authorization_id or "").lower()
    readback_token = str(readback_token or "").lower()
    if not re.fullmatch(r"[a-f0-9]{32}", authorization_id):
        raise ValueError("执行授权编号无效。")
    if not re.fullmatch(r"[a-f0-9]{64}", readback_token):
        raise ValueError("执行前定向复核令牌无效。")
    with _state_lock:
        _require_supervised_execution_mode()
        session = load_execution_preflight().get("session")
        if not session or session.get("authorization_id") != authorization_id:
            raise ValueError("未找到对应的执行授权。")
        if session.get("state") != "authorized" or session.get("authorization_consumed"):
            raise ValueError("执行授权已使用或已失效。")
        now_ms = int(time.time() * 1000)
        if int(session.get("authorization_expires_at_ms") or 0) <= now_ms:
            _save_execution_preflight({**session, "state": "expired", "execution_enabled": False, "write_enabled": False})
            raise ValueError("执行授权已过期。")
        audit = load_action_audit()
        action = next(
            (item for item in audit.get("actions", []) if item.get("action_id") == session.get("action_id")),
            None,
        )
        if not action or action.get("state") != "confirmed":
            raise ValueError("授权对应的动作不可执行。")
        _validate_confirmed_action_for_execution(action, session)
        readback = _find_plan_readback(
            action,
            readback_token=readback_token,
            expected_purpose="preconsume_baseline",
            authorization_id=authorization_id,
        )
        previous_baseline = action.get("execution_baseline") if isinstance(action.get("execution_baseline"), dict) else {}
        previous_document_id = str(previous_baseline.get("document_instance_id") or "").strip()
        document_id = str((readback or {}).get("document_instance_id") or "").strip()
        captured_at_ms = int((readback or {}).get("captured_at_ms") or 0)
        navigation_started_at_ms = int((readback or {}).get("navigation_started_at_ms") or 0)
        authorized_at_ms = int(session.get("authorized_at_ms") or 0)
        fresh_document = bool(
            readback
            and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", document_id)
            and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", previous_document_id)
            and document_id != previous_document_id
            and navigation_started_at_ms > authorized_at_ms
            and navigation_started_at_ms <= captured_at_ms <= now_ms + MAX_CAPTURE_FUTURE_SKEW_MS
            and 0 <= now_ms - captured_at_ms <= EXECUTION_FINAL_REREAD_MAX_CAPTURE_AGE_MS
            and int(readback.get("quality_score") or 0) >= 70
            and readback.get("account_identity_matches") is True
            and readback.get("store_identity_matches") is True
        )
        change = action.get("change") if isinstance(action.get("change"), dict) else {}
        current_value = change.get("current_value")
        if str(action.get("operation_type") or "") == "pause_plan":
            value_matches = (
                _normalize_delivery_status((readback or {}).get("delivery_status"))
                == _normalize_delivery_status(current_value)
            )
        else:
            observed = (readback or {}).get("current_value")
            value_matches = bool(
                isinstance(observed, (int, float))
                and isinstance(current_value, (int, float))
                and abs(float(observed) - float(current_value)) <= 0.01
            )
        if not fresh_document or not value_matches:
            _save_execution_preflight({
                **session,
                "state": "invalidated",
                "invalidated_at_ms": now_ms,
                "invalidation_reason": "final_hard_reread_changed_or_unverified",
                "authorization_consumed": False,
                "execution_enabled": False,
                "write_enabled": False,
            })
            raise ValueError("最终硬刷新发现页面身份、计划状态或当前预算已变化；授权已失效，页面未提交。")
        baseline = _execution_baseline_from_readback(action, readback)
        baseline_hash = _execution_baseline_hash(baseline)
        updated_action = {
            **action,
            "execution_baseline": baseline,
            "execution_baseline_hash": baseline_hash,
            "final_preconsume_reread_at_ms": now_ms,
            "final_preconsume_readback_token": readback_token,
            "final_preconsume_authorization_id": authorization_id,
        }
        updated_session = {
            **session,
            "execution_baseline_hash": baseline_hash,
            "final_preconsume_document_instance_id": document_id,
            "final_preconsume_reread_at_ms": now_ms,
            "final_preconsume_readback_token": readback_token,
            "final_preconsume_authorization_id": authorization_id,
            "execution_enabled": False,
            "write_enabled": False,
        }
        baseline_journal = {
            "schema_version": 1,
            "phase": "prepared",
            "prepared_at_ms": now_ms,
            "action_id": str(updated_action.get("action_id") or ""),
            "authorization_id": authorization_id,
            "execution_baseline_hash": baseline_hash,
            "updated_action": updated_action,
            "updated_session": updated_session,
        }
        _atomic_json_write(_execution_baseline_transaction_path(), baseline_journal)
        try:
            _atomic_json_write(
                _action_audit_path(),
                {
                    "schema_version": 1,
                    "updated_at": _now_label(),
                    "execution_enabled": False,
                    "actions": [
                        updated_action if item.get("action_id") == updated_action.get("action_id") else item
                        for item in audit.get("actions", [])
                    ],
                },
            )
            _save_execution_preflight(updated_session)
            _atomic_json_write(
                _execution_baseline_transaction_path(),
                {**baseline_journal, "phase": "committed", "committed_at_ms": int(time.time() * 1000)},
            )
        except OSError:
            try:
                _recover_execution_baseline_transaction()
            except OSError:
                logger.exception("Execution-baseline recovery is deferred to the next state read.")
            raise
        _clear_execution_baseline_transaction()
        return _preview_execution_authorization_locked(authorization_id)


def _execution_request_for_action(action: dict[str, Any]) -> dict[str, Any]:
    target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
    change = action.get("change") if isinstance(action.get("change"), dict) else {}
    proposal_scope = action.get("proposal_scope") if isinstance(action.get("proposal_scope"), dict) else {}
    store_key = str(proposal_scope.get("store_key") or "").strip().lower()
    account_key = str(proposal_scope.get("account_key") or target.get("account_key") or "").strip().lower()
    promotion_context = build_promotion_context(action.get("promotion_context"))
    existing_scope = promotion_context.get("account_scope") if isinstance(promotion_context.get("account_scope"), dict) else {}
    # The signed proposal scope is the one source of truth sent to the browser.
    # Never let a second, stale promotion-context field select another store.
    promotion_context["account_scope"] = {
        **existing_scope,
        "store_id": store_key,
        "account_id": account_key,
    }
    return {
        "operation_type": action.get("operation_type"),
        "store_key": store_key,
        "account_key": account_key,
        "plan_id": target.get("id"),
        "plan_name": target.get("name"),
        "field": change.get("field"),
        "expected_current_value": change.get("current_value"),
        "target_value": change.get("target_value"),
        "page_type": _canonical_execution_page_type((action.get("evidence_ref") or {}).get("page_type")),
        "promotion_context": promotion_context,
        "mode": "supervised_submit",
    }


def _validate_execution_request_binding_locked(
    action: dict[str, Any],
    session: dict[str, Any],
    authorization_id: str,
    execution_request: dict[str, Any],
) -> str:
    """Bind the browser DOM probe to the exact server-derived action request."""

    if not isinstance(execution_request, dict):
        raise ValueError("执行页面请求缺少完整的一次性授权参数。")
    action_id = str(action.get("action_id") or "").strip().lower()
    authorization_id = str(authorization_id or "").strip().lower()
    if (
        not re.fullmatch(r"[a-f0-9]{24}", action_id)
        or not re.fullmatch(r"[a-f0-9]{32}", authorization_id)
        or str(session.get("action_id") or "").strip().lower() != action_id
        or str(session.get("authorization_id") or "").strip().lower() != authorization_id
    ):
        raise ValueError("执行页面请求与当前动作或一次性授权不一致。")
    expected = {
        **_execution_request_for_action(action),
        "action_id": action_id,
        "authorization_id": authorization_id,
        "execution_attempt_id": authorization_id,
    }
    if session.get("authorization_consumed") is True:
        execute_before_ms = int(session.get("execute_before_ms") or 0)
        if execute_before_ms <= int(time.time() * 1000):
            raise ValueError("一次性执行授权已过期，页面未提交。")
        expected["execute_before_ms"] = execute_before_ms
    try:
        expected_json = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        actual_json = json.dumps(execution_request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("执行页面请求包含无效字段。") from error
    if not hmac.compare_digest(expected_json.encode("utf-8"), actual_json.encode("utf-8")):
        raise ValueError("执行页面请求的计划、动作、预算或授权参数已变化，页面未提交。")
    return hashlib.sha256(expected_json.encode("utf-8")).hexdigest()


def preview_execution_authorization(authorization_id: str) -> dict[str, Any]:
    """Serialize preview with mode changes, scope changes and grant consumption."""

    with _state_lock:
        return _preview_execution_authorization_locked(authorization_id)


def _preview_execution_authorization_locked(authorization_id: str) -> dict[str, Any]:
    """Return probe parameters without consuming or extending the grant."""

    authorization_id = str(authorization_id or "").lower()
    if not re.fullmatch(r"[a-f0-9]{32}", authorization_id):
        raise ValueError("执行授权编号无效。")
    _require_supervised_execution_mode()
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
    try:
        _validate_confirmed_action_for_execution(action, session)
    except ValueError:
        _save_execution_preflight({
            **session,
            "state": "invalidated",
            "invalidated_at_ms": int(time.time() * 1000),
            "invalidation_reason": "execution_scope_or_collection_changed",
            "authorization_consumed": False,
            "execution_enabled": False,
            "write_enabled": False,
        })
        raise
    promotion_guard = legacy_execution_guard(
        action.get("operation_type"),
        action.get("promotion_context"),
        expected_account_key=_action_target_account_key(action),
    )
    if not promotion_guard["allowed"]:
        raise ValueError(f"{promotion_guard['code']}：{promotion_guard['reason']}")
    quota = assess_execution_quota(action)
    if not quota["allowed"]:
        raise ValueError("；".join(item["message"] for item in quota["blocked_reasons"]))
    baseline = action.get("execution_baseline") if isinstance(action.get("execution_baseline"), dict) else {}
    return {
        "action_id": session.get("action_id"),
        "session_id": session.get("session_id"),
        "authorized_at_ms": int(session.get("authorized_at_ms") or 0),
        "authorization_expires_at_ms": session.get("authorization_expires_at_ms"),
        "execution_request": _execution_request_for_action(action),
        "execution_baseline": {
            "document_instance_id": str(baseline.get("document_instance_id") or ""),
            "navigation_started_at_ms": int(baseline.get("navigation_started_at_ms") or 0),
            "captured_at_ms": int(baseline.get("captured_at_ms") or 0),
            "hash": str(action.get("execution_baseline_hash") or ""),
        },
        "quota": quota,
    }


def recover_execution_readback_job(action_id: str) -> dict[str, Any]:
    """Rebuild a read-only browser job from the durable consumed grant.

    This endpoint never creates or consumes an authorization and never returns
    a submit request. It exists solely so clearing/reinstalling the extension
    cannot strand an already-consumed plan forever.
    """

    action_id = str(action_id or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{24}", action_id):
        raise ValueError("人工回读缺少有效动作编号。")
    with _state_lock:
        audit = load_action_audit()
        session = load_execution_preflight().get("session")
        action = next(
            (item for item in audit.get("actions", []) if item.get("action_id") == action_id),
            None,
        )
        archived_recovery = bool(
            isinstance(action, dict)
            and action.get("state") in {"executing", "succeeded"}
            and action.get("manual_reconcile_required") is True
            and action.get("manual_reconcile_archived") is True
            and action.get("plan_retry_blocked") is True
        )
        session_matches = bool(
            isinstance(session, dict)
            and session.get("action_id") == action_id
            and session.get("authorization_consumed") is True
            and str(session.get("state") or "") in {
                "authorization_consumed", "manual_reconcile_required", "manual_reconcile_archived",
            }
        )
        if (
            not isinstance(action, dict)
            or action.get("state") not in {"executing", "succeeded"}
            or (not session_matches and not archived_recovery)
        ):
            raise ValueError("该动作没有可恢复的已消费回读任务；不会创建或重放执行授权。")
        bound_session = session if session_matches else {}
        authorization_id = str(
            action.get("execution_authorization_id")
            if archived_recovery
            else bound_session.get("authorization_id")
            or ""
        ).strip().lower()
        if (
            not re.fullmatch(r"[a-f0-9]{32}", authorization_id)
            or str(action.get("execution_authorization_id") or "").strip().lower() != authorization_id
        ):
            raise ValueError("动作与已消费授权绑定不一致，保持锁定并停止恢复。")
        target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
        proposal_scope = action.get("proposal_scope") if isinstance(action.get("proposal_scope"), dict) else {}
        evidence = action.get("evidence_ref") if isinstance(action.get("evidence_ref"), dict) else {}
        baseline = action.get("execution_baseline") if isinstance(action.get("execution_baseline"), dict) else {}
        context = action.get("execution_readback_context") if isinstance(action.get("execution_readback_context"), dict) else {}
        store_key = str(proposal_scope.get("store_key") or "").strip().lower()
        account_key = str(proposal_scope.get("account_key") or target.get("account_key") or "").strip().lower()
        plan_id = str(target.get("id") or "").strip().lower()
        promotion_mode = str(
            context.get("promotion_mode")
            or build_promotion_context(action.get("promotion_context")).get("promotion_mode")
            or "unknown"
        )
        page_type = _canonical_execution_page_type(context.get("page_type") or evidence.get("page_type"))
        baseline_hash = str(action.get("execution_baseline_hash") or "").strip().lower()
        if (
            not SAFE_KEY.fullmatch(store_key)
            or not SAFE_KEY.fullmatch(account_key)
            or not plan_id
            or len(plan_id) > 128
            or promotion_mode not in {"standard", "full_domain"}
            or page_type not in {"campaigns", "qianchuan_live"}
            or not baseline
            or not re.fullmatch(r"[a-f0-9]{64}", baseline_hash)
            or baseline_hash != _execution_baseline_hash(baseline)
        ):
            raise ValueError("已消费动作的回读范围不完整，保持锁定并停止恢复。")
        started_at_ms = int(
            action.get("execution_reported_at_ms")
            or action.get("execution_started_at_ms")
            or bound_session.get("authorization_consumed_at_ms")
            or 0
        )
        if started_at_ms <= 0:
            raise ValueError("已消费动作缺少执行时间，保持锁定并停止恢复。")
        receipt = action.get("execution_receipt") if isinstance(action.get("execution_receipt"), dict) else None
        scope_hash = str(context.get("scope_hash") or "").strip().lower()
        return {
            "schema_version": 2,
            "action_id": action_id,
            "authorization_id": authorization_id,
            "store_key": store_key,
            "account_key": account_key,
            "plan_id": plan_id,
            "started_at_ms": started_at_ms,
            "offsets_ms": [0, 5_000, 15_000, 30_000],
            "next_attempt_index": 0,
            "attempts": [],
            "status": "pending",
            "status_label": (
                "已从归档账本恢复只读验收；原计划永久禁止自动重投"
                if archived_recovery
                else "已从本地 Agent 恢复只读验收任务，绝不会重复提交"
            ),
            "journal_state": "readback_pending" if receipt else "submission_unknown",
            "baseline_document_instance_id": str(baseline.get("document_instance_id") or ""),
            "baseline_navigation_started_at_ms": int(baseline.get("navigation_started_at_ms") or 0),
            "baseline_scope_hash": scope_hash if re.fullmatch(r"[a-f0-9]{64}", scope_hash) else "",
            "baseline_promotion_mode": promotion_mode,
            "baseline_page_type": page_type,
            "execution_baseline_hash": baseline_hash,
            "reconstructed_from_agent": True,
            "manual_reconcile_archived": archived_recovery,
            "plan_retry_blocked": archived_recovery,
            "authorization_consumed": True,
            "write_enabled": False,
            "execution_enabled": False,
            "deferred_until_ms": 0,
            "next_attempt_at_ms": started_at_ms,
            "updated_at_ms": int(time.time() * 1000),
        }


def _execution_receipt_matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        actual_number = _parse_number(actual)
        return actual_number is not None and abs(float(expected) - actual_number) <= 0.01
    if str(expected or "") == "暂停":
        return _normalize_delivery_status(actual) == "暂停"
    return str(expected or "").strip() == str(actual or "").strip()


def record_execution_result(action_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Persist one receipt bound to a consumed grant and its exact action."""

    action_id = str(action_id or "").lower()
    if not re.fullmatch(r"[a-f0-9]{24}", action_id) or not isinstance(result, dict):
        raise ValueError("执行回执无效。")
    authorization_id = str(result.get("authorization_id") or "").lower()
    if not re.fullmatch(r"[a-f0-9]{32}", authorization_id):
        raise ValueError("执行回执必须绑定一次性授权。")
    if type(result.get("submitted")) is not bool:
        raise ValueError("执行回执必须明确说明页面已提交或明确未提交；未知状态只能等待独立回读。")
    submitted = result["submitted"]
    if not submitted and not (
        result.get("definite_not_submitted") is True
        and str(result.get("submission_phase") or "") == "pre_mutation"
        and result.get("mutation_started") is False
        and result.get("click_invoked") is False
        and result.get("recovery_unverified") is not True
    ):
        raise ValueError("未提交回执缺少点击前、未改表单的确定证据；动作保持锁定并等待独立回读。")
    platform_success_observed = result.get("platform_success_observed") is True
    now_ms = int(time.time() * 1000)
    with _state_lock:
        session = load_execution_preflight().get("session")
        if not session or session.get("authorization_id") != authorization_id:
            raise ValueError("执行回执对应的授权不存在。")
        if session.get("action_id") != action_id:
            raise ValueError("执行回执与授权动作不一致。")
        if (
            session.get("state") not in {
                "authorization_consumed", "manual_reconcile_required", "manual_reconcile_archived",
                "execution_failed", "completed",
            }
            or not session.get("authorization_consumed")
        ):
            raise ValueError("一次性授权尚未消费，不能记录执行回执。")
        audit = load_action_audit()
        action = next((item for item in audit["actions"] if item.get("action_id") == action_id), None)
        if not action:
            raise ValueError("执行动作不存在或未被本次授权锁定。")
        expected = _execution_request_for_action(action)
        bindings = {
            "operation_type": (expected.get("operation_type"), result.get("operation_type")),
            "store_key": (str(expected.get("store_key") or "").lower(), str(result.get("store_key") or "").lower()),
            "account_key": (str(expected.get("account_key") or "").lower(), str(result.get("account_key") or "").lower()),
            "plan_id": (expected.get("plan_id"), result.get("plan_id")),
            "target_value": (expected.get("target_value"), result.get("target_value")),
        }
        mismatched = [name for name, (wanted, received) in bindings.items() if not _execution_receipt_matches(wanted, received)]
        if mismatched:
            raise ValueError(f"执行回执与授权参数不一致：{', '.join(mismatched)}。")
        canonical_receipt = {
            "authorization_id": authorization_id,
            "submitted": submitted,
            "platform_success_observed": platform_success_observed,
            "operation_type": str(result.get("operation_type") or ""),
            "store_key": str(result.get("store_key") or "").lower(),
            "account_key": str(result.get("account_key") or "").lower(),
            "plan_id": str(result.get("plan_id") or ""),
            "target_value": result.get("target_value"),
            "error": str(result.get("error") or "")[:300],
            "definite_not_submitted": result.get("definite_not_submitted") is True,
            "submission_phase": str(result.get("submission_phase") or "")[:40],
            "mutation_started": result.get("mutation_started") is True,
            "click_invoked": result.get("click_invoked") is True,
            "recovery_unverified": result.get("recovery_unverified") is True,
        }
        previous_receipt = action.get("execution_receipt") if isinstance(action.get("execution_receipt"), dict) else None
        same_previous_receipt = bool(
            previous_receipt
            and previous_receipt.get("authorization_id") == authorization_id
            and previous_receipt.get("submitted") is submitted
            and previous_receipt.get("platform_success_observed") is platform_success_observed
            and all(
                _execution_receipt_matches(canonical_receipt.get(name), previous_receipt.get(name))
                for name in (
                    "operation_type", "store_key", "account_key", "plan_id", "target_value", "error",
                    "definite_not_submitted", "submission_phase", "mutation_started", "click_invoked",
                    "recovery_unverified",
                )
            )
        )
        if session.get("execution_receipt_recorded"):
            if same_previous_receipt:
                return action
            raise ValueError("该授权已记录不同的执行回执，不能覆盖。")
        if action.get("state") != "executing":
            # If the action write completed but the companion preflight write
            # was interrupted, an exact retry repairs only the missing marker.
            if same_previous_receipt and action.get("state") == "failed":
                _save_execution_preflight({
                    **session,
                    "state": "execution_failed",
                    "execution_receipt_recorded": True,
                    "execution_receipt_recorded_at_ms": now_ms,
                    "execution_receipt_action_state": action.get("state"),
                    "write_enabled": False,
                    "execution_enabled": False,
                })
                return action
            raise ValueError("执行动作未被本次授权锁定或已由其他结果结束。")
        if same_previous_receipt:
            _save_execution_preflight({
                **session,
                "state": "execution_failed" if action.get("state") == "failed" else session.get("state"),
                "execution_receipt_recorded": True,
                "execution_receipt_recorded_at_ms": now_ms,
                "execution_receipt_action_state": action.get("state"),
                "write_enabled": False,
                "execution_enabled": False,
            })
            return action
        # A DOM click or page toast can prove only that the browser attempted a
        # submission.  Platform success is established exclusively by a fresh
        # readback of the exact account, plan and target value below.
        if not submitted:
            updated = transition_action(action, "failed", allow_execution=True)
        else:
            updated = dict(action)
        updated.update({
            "execution_source": "qianchuan_browser_supervised",
            "execution_reported_at_ms": now_ms,
            "execution_receipt": canonical_receipt,
            "execution_note": (
                "浏览器已发起提交，但平台结果尚未验收；动作保持锁定，禁止重复执行，必须先回读。"
                if submitted
                else "浏览器明确返回未提交，本次动作记录为失败。"
            ),
        })
        actions = [updated if item.get("action_id") == action_id else item for item in audit["actions"]]
        _atomic_json_write(
            _action_audit_path(),
            {"schema_version": 1, "updated_at": _now_label(), "execution_enabled": True, "actions": actions},
        )
        _save_execution_preflight({
            **session,
            "state": "execution_failed" if updated.get("state") == "failed" else session.get("state"),
            "execution_receipt_recorded": True,
            "execution_receipt_recorded_at_ms": now_ms,
            "execution_receipt_action_state": updated.get("state"),
            "write_enabled": False,
            "execution_enabled": False,
        })
    return updated


def verify_execution_result(action_id: str, readback_token: str) -> dict[str, Any]:
    action_id = str(action_id or "").lower()
    readback_token = str(readback_token or "").lower()
    if not re.fullmatch(r"[a-f0-9]{64}", readback_token):
        raise ValueError("执行回读令牌无效。")
    with _state_lock:
        audit = load_action_audit()
        action = next((item for item in audit["actions"] if item.get("action_id") == action_id), None)
        if not action or action.get("state") not in {"executing", "succeeded", "verified"}:
            raise ValueError("只有已锁定或已提交动作可以进行页面回读验收。")
        authorization_id = str(action.get("execution_authorization_id") or "").lower()
        if not re.fullmatch(r"[a-f0-9]{32}", authorization_id):
            raise ValueError("执行动作缺少有效的一次性授权绑定。")
        readback = _find_plan_readback(
            action,
            readback_token=readback_token,
            expected_purpose="execution_readback",
            authorization_id=authorization_id,
        )
        change = action.get("change") if isinstance(action.get("change"), dict) else {}
        target_value = (action.get("change") or {}).get("target_value")
        current_value = change.get("current_value")
        submitted_at = int(action.get("execution_reported_at_ms") or action.get("execution_started_at_ms") or 0)
        readback_ms = int((readback or {}).get("captured_at_ms") or 0)
        execution_baseline = action.get("execution_baseline") if isinstance(action.get("execution_baseline"), dict) else {}
        baseline_document_id = str(execution_baseline.get("document_instance_id") or "").strip()
        readback_document_id = str((readback or {}).get("document_instance_id") or "").strip()
        navigation_started_at_ms = int((readback or {}).get("navigation_started_at_ms") or 0)
        independent_document = bool(
            re.fullmatch(r"[A-Za-z0-9_-]{16,128}", baseline_document_id)
            and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", readback_document_id)
            and readback_document_id != baseline_document_id
            and navigation_started_at_ms > submitted_at
            and navigation_started_at_ms <= readback_ms
        )
        fresh = bool(
            readback
            and independent_document
            and readback_ms > submitted_at
            and readback_ms <= int(time.time() * 1000) + MAX_CAPTURE_FUTURE_SKEW_MS
            and int(readback.get("quality_score") or 0) >= 70
            and readback.get("account_identity_matches") is True
            and readback.get("store_identity_matches") is True
        )
        operation_type = str(action.get("operation_type") or "")
        if operation_type == "pause_plan":
            matched = bool(
                fresh
                and str(target_value or "") == "暂停"
                and _normalize_delivery_status((readback or {}).get("delivery_status")) == "暂停"
            )
        else:
            matched = bool(
                fresh
                and isinstance((readback or {}).get("current_value"), (int, float))
                and isinstance(target_value, (int, float))
                and abs(float(readback["current_value"]) - float(target_value)) <= 0.01
            )
        original_matched = bool(
            fresh
            and (
                _normalize_delivery_status((readback or {}).get("delivery_status")) == _normalize_delivery_status(current_value)
                if operation_type == "pause_plan"
                else isinstance((readback or {}).get("current_value"), (int, float))
                and isinstance(current_value, (int, float))
                and abs(float(readback["current_value"]) - float(current_value)) <= 0.01
            )
        )
        if matched:
            manual_reconcile_was_required = action.get("manual_reconcile_required") is True
            if action.get("state") != "verified":
                if action.get("state") == "executing":
                    action = transition_action(action, "succeeded", allow_execution=True)
                    action["execution_reported_at_ms"] = int(action.get("execution_reported_at_ms") or readback_ms)
                    action["execution_note"] = "浏览器提交回执不作为成功依据；新页面已确认目标值生效。"
                action = transition_action(action, "verified")
            action.pop("next_readback_after_ms", None)
            action["manual_reconcile_required"] = False
            action.pop("manual_reconcile_required_at_ms", None)
            if manual_reconcile_was_required:
                action["manual_reconcile_resolved_at_ms"] = int(time.time() * 1000)
                action["manual_reconcile_resolution"] = "target_verified_by_independent_readback"
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
        elif action.get("state") in {"executing", "succeeded"} and original_matched:
            # 千川列表存在最终一致性延迟。一次新页面仍显示原值，只能证明
            # “当前尚未看到变更”，不能据此立即开放重试，否则可能重复调整。
            confirmations = [
                item for item in (action.get("original_value_readbacks") or [])
                if isinstance(item, dict)
                and int(item.get("captured_at_ms") or 0) > submitted_at
                and int(item.get("navigation_started_at_ms") or 0) > submitted_at
                and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", str(item.get("document_instance_id") or "").strip())
            ]
            if not any(str(item.get("document_instance_id") or "") == readback_document_id for item in confirmations):
                confirmations.append({
                    "captured_at_ms": readback_ms,
                    "document_instance_id": readback_document_id,
                    "navigation_started_at_ms": navigation_started_at_ms,
                    "snapshot_ref": str((readback or {}).get("snapshot_ref") or ""),
                    "quality_score": int((readback or {}).get("quality_score") or 0),
                    "current_value": (readback or {}).get("delivery_status") if operation_type == "pause_plan" else (readback or {}).get("current_value"),
                })
            confirmations = sorted(confirmations, key=lambda item: int(item.get("captured_at_ms") or 0))[-4:]
            action["original_value_readbacks"] = confirmations
            mature_confirmation = bool(
                readback_ms >= submitted_at + EXECUTION_ORIGINAL_VALUE_MIN_CONFIRMATION_AGE_MS
            )
            distinct_documents = {
                str(item.get("document_instance_id") or "") for item in confirmations
                if str(item.get("document_instance_id") or "")
            }
            enough_confirmations = len(distinct_documents) >= EXECUTION_ORIGINAL_VALUE_CONFIRMATIONS_REQUIRED
            if enough_confirmations and mature_confirmation:
                # An unchanged page proves only that the platform has not shown
                # the write yet. It cannot prove rejection: Qianchuan may apply
                # a queued request after tens of seconds or longer. Releasing
                # the lock here could submit the same budget change twice.
                action["execution_note"] = "多次独立回读仍为执行前原值，但平台提交结果仍不确定；动作继续锁定，禁止重复执行，请继续回读或在官方后台人工核对。"
                action["next_readback_after_ms"] = readback_ms + 60_000
                action["manual_reconcile_required"] = True
                action["manual_reconcile_required_at_ms"] = int(
                    action.get("manual_reconcile_required_at_ms") or readback_ms
                )
            else:
                action["execution_note"] = "平台暂未显示目标值，动作继续锁定；正在等待后续独立回读，请勿重复执行。"
                action["next_readback_after_ms"] = max(
                    submitted_at + EXECUTION_ORIGINAL_VALUE_MIN_CONFIRMATION_AGE_MS,
                    readback_ms + 1_000,
                )
            actions = [action if item.get("action_id") == action_id else item for item in audit["actions"]]
            _atomic_json_write(
                _action_audit_path(),
                {"schema_version": 1, "updated_at": _now_label(), "execution_enabled": True, "actions": actions},
            )
        if action.get("manual_reconcile_required") is True and action.get("state") == "executing":
            session = load_execution_preflight().get("session")
            if isinstance(session, dict) and session.get("action_id") == action_id:
                _save_execution_preflight({
                    **session,
                    "state": "manual_reconcile_required",
                    "manual_reconcile_required_at_ms": int(
                        action.get("manual_reconcile_required_at_ms") or time.time() * 1000
                    ),
                    "write_enabled": False,
                    "execution_enabled": False,
                })
        elif action.get("state") in {"verified", "failed"}:
            session = load_execution_preflight().get("session")
            if isinstance(session, dict) and session.get("action_id") == action_id:
                _save_execution_preflight({
                    **session,
                    "state": "completed" if action.get("state") == "verified" else "execution_failed",
                    "completed_at_ms": int(time.time() * 1000),
                    "execution_receipt_action_state": action.get("state"),
                    "write_enabled": False,
                    "execution_enabled": False,
                })
        confirmations = action.get("original_value_readbacks") or []
    return {
        "action_id": action_id,
        "readback_token": readback_token,
        "snapshot_ref": str((readback or {}).get("snapshot_ref") or ""),
        "verified": action.get("state") == "verified",
        "current_readback_matches": matched,
        "state": action.get("state"),
        "execution_unknown": action.get("state") in {"executing", "succeeded"},
        "independent_document": independent_document,
        "safe_to_retry": action.get("state") == "failed" and original_matched,
        "original_value_confirmation_count": len(confirmations),
        "original_value_confirmations_required": EXECUTION_ORIGINAL_VALUE_CONFIRMATIONS_REQUIRED,
        "next_readback_after_ms": int(action.get("next_readback_after_ms") or 0),
        "readback": readback,
    }


def _effect_metric_contract_signature(value: Any) -> tuple[str, str, str] | None:
    """Return a complete definition/version/period signature or fail closed."""

    if not isinstance(value, dict):
        return None
    definition = str(value.get("definition") or "").strip()
    version = str(value.get("version") or "").strip()
    period_values = [
        str(value.get(field) or "").strip()
        for field in ("attribution_window", "period")
        if str(value.get(field) or "").strip()
    ]
    if len(set(period_values)) > 1:
        return None
    period = period_values[0] if period_values else ""
    if not definition or definition == "unknown" or not version or not period:
        return None
    return definition, version, period


def build_execution_effectiveness_report(*, now_ms: int | None = None) -> dict[str, Any]:
    """Evaluate only verified actions with a fresh, minimally comparable sample."""

    now_ms = int(now_ms or time.time() * 1000)
    items: list[dict[str, Any]] = []
    for action in load_action_audit().get("actions", []):
        # A browser success receipt is not proof that the platform accepted the
        # change.  Only post-write readback may enter outcome evaluation.
        if action.get("state") != "verified":
            continue
        executed_at_ms = int(action.get("execution_reported_at_ms") or 0)
        if executed_at_ms <= 0:
            continue
        target = action.get("target_ref") if isinstance(action.get("target_ref"), dict) else {}
        change = action.get("change") if isinstance(action.get("change"), dict) else {}
        evidence = action.get("evidence_ref") if isinstance(action.get("evidence_ref"), dict) else {}
        execution_baseline = (
            action.get("execution_baseline") if isinstance(action.get("execution_baseline"), dict) else {}
        )
        before = {
            "spend": execution_baseline.get("spend") if execution_baseline else evidence.get("spend"),
            "roi": execution_baseline.get("roi") if execution_baseline else evidence.get("roi"),
            "orders": execution_baseline.get("orders") if execution_baseline else evidence.get("orders"),
            "budget": execution_baseline.get("current_value") if execution_baseline else change.get("current_value"),
            "snapshot_ref": execution_baseline.get("snapshot_ref"),
            "captured_at_ms": execution_baseline.get("captured_at_ms"),
            "quality_score": execution_baseline.get("quality_score"),
            "metric_contract": execution_baseline.get("metric_contract") or {},
        }
        after = _find_plan_readback(action)
        spend_value = before.get("spend")
        observation_minutes = 30 if isinstance(spend_value, (int, float)) and spend_value >= 1000 else 120 if isinstance(spend_value, (int, float)) and spend_value >= 300 else 24 * 60
        due_at_ms = executed_at_ms + observation_minutes * 60 * 1000
        after_identity_ready = bool(
            after
            and after.get("account_identity_matches") is True
            and after.get("store_identity_matches") is True
        )
        before_captured_at_ms = int(before.get("captured_at_ms") or 0)
        after_captured_at_ms = int((after or {}).get("captured_at_ms") or 0)
        before_quality_ready = int(before.get("quality_score") or 0) >= 70
        after_quality_ready = int((after or {}).get("quality_score") or 0) >= 70
        before_time_ready = 0 < before_captured_at_ms <= executed_at_ms
        after_time_ready = bool(
            after_identity_ready
            and due_at_ms <= after_captured_at_ms <= now_ms + MAX_CAPTURE_FUTURE_SKEW_MS
        )
        before_metric_signature = _effect_metric_contract_signature(before.get("metric_contract"))
        after_metric_signature = _effect_metric_contract_signature((after or {}).get("metric_contract"))
        metric_contract_matches = bool(
            before_metric_signature
            and after_metric_signature
            and before_metric_signature == after_metric_signature
        )
        operation_type = str(action.get("operation_type") or "")
        before_spend = before.get("spend")
        after_spend = (after or {}).get("spend")
        incremental_spend = (
            round(float(after_spend) - float(before_spend), 2)
            if isinstance(before_spend, (int, float))
            and isinstance(after_spend, (int, float))
            and float(after_spend) >= float(before_spend)
            else None
        )
        minimum_sample_spend = (
            round(max(50.0, min(200.0, float(before_spend) * 0.10)), 2)
            if isinstance(before_spend, (int, float)) and float(before_spend) >= 0
            else 50.0
        )
        if now_ms < due_at_ms:
            status = "waiting"
            label = f"等待 {observation_minutes // 60} 小时复查" if observation_minutes >= 60 else f"等待 {observation_minutes} 分钟复查"
            verdict = "观察窗口尚未结束，不追加动作。"
        elif not after_identity_ready or not after_time_ready or not after_quality_ready:
            status = "needs_reread"
            label = "等待重新读取"
            verdict = "观察窗口已结束，但回读身份、采集时间或质量未通过，请重新读取对应千川计划页。"
        elif not before_time_ready or not before_quality_ready or before_metric_signature is None:
            status = "inconclusive"
            label = "执行前基线不可比"
            verdict = "执行前基线缺少合格时间、质量或完整指标口径，不能形成效果结论。"
        elif after_metric_signature is None:
            status = "needs_reread"
            label = "回读口径待补齐"
            verdict = "效果回读缺少完整的指标定义、版本或统计周期，请切回相同口径后重新读取。"
        elif not metric_contract_matches:
            status = "inconclusive"
            label = "指标口径不一致"
            verdict = "执行前后指标定义、版本或统计周期不一致，当前数据不可比较。"
        elif operation_type == "pause_plan":
            paused = _normalize_delivery_status((after or {}).get("delivery_status")) == "暂停"
            tolerated_spend = max(10.0, float(before_spend or 0) * 0.02)
            if paused and incremental_spend is not None and incremental_spend <= tolerated_spend:
                status = "effective"
                label = "暂停守护有效"
                verdict = "计划保持暂停，观察期内未出现明显新增消耗。"
            elif not paused:
                status = "inconclusive"
                label = "状态已变化"
                verdict = "计划在验收后又发生状态变化，请确认是否由人工恢复投放。"
            elif incremental_spend is None:
                status = "inconclusive"
                label = "消耗不可比较"
                verdict = "暂停状态已回读，但缺少同口径消耗，不能判断止损效果。"
            else:
                status = "ineffective"
                label = "暂停后仍有消耗"
                verdict = "观察期新增消耗超出容忍范围，请核对归因延迟和计划状态。"
        elif operation_type == "restore_budget":
            status = "inconclusive"
            label = "恢复已回读"
            verdict = "预算恢复属于风险恢复动作，不把短期波动计为经营增量。"
        elif incremental_spend is None or incremental_spend < minimum_sample_spend:
            status = "inconclusive"
            label = "样本不足"
            verdict = f"执行后同口径新增消耗需达到 {minimum_sample_spend:g} 元，当前不足以判断。"
        else:
            before_roi = before.get("roi")
            after_roi = after.get("roi")
            if not isinstance(before_roi, (int, float)) or not isinstance(after_roi, (int, float)) or float(before_roi) <= 0:
                status = "inconclusive"
                label = "ROI 不可比较"
                verdict = "缺少调整前后同口径 ROI，不能用订单累计值替代效果判断。"
            else:
                roi_change = (float(after_roi) - float(before_roi)) / float(before_roi)
                if roi_change >= 0.10:
                    status = "effective"
                    label = "止损有效"
                    verdict = "在达到最小消耗样本后，ROI 较执行前改善至少 10%。"
                elif roi_change <= -0.15:
                    status = "rollback_recommended"
                    label = "建议恢复原预算"
                    verdict = "达到最小样本后 ROI 恶化超过 15%，建议人工确认恢复原预算。"
                elif roi_change <= -0.05:
                    status = "ineffective"
                    label = "暂未生效"
                    verdict = "达到最小样本后 ROI 仍有明显下降，先人工复核素材与承接。"
                else:
                    status = "inconclusive"
                    label = "变化不显著"
                    verdict = "ROI 波动尚未达到有效或失效阈值，继续观察，不追加动作。"
        items.append({
            "action_id": action.get("action_id"),
            "account_key": target.get("account_key"),
            "account_label": target.get("account_label"),
            "plan_name": target.get("name"),
            "executed_at_ms": executed_at_ms,
            "due_at_ms": due_at_ms,
            "observation_window_minutes": observation_minutes,
            "minimum_sample_spend": minimum_sample_spend,
            "incremental_spend": incremental_spend,
            "status": status,
            "status_label": label,
            "verdict": verdict,
            "before": before,
            "after": after,
            "effect_readback_gate": {
                "ready": bool(
                    after_identity_ready
                    and after_time_ready
                    and before_quality_ready
                    and after_quality_ready
                    and before_time_ready
                    and metric_contract_matches
                ),
                "before_quality_ready": before_quality_ready,
                "after_quality_ready": after_quality_ready,
                "before_time_ready": before_time_ready,
                "after_time_ready": after_time_ready,
                "metric_contract_matches": metric_contract_matches,
            },
            "change": {
                "from": change.get("current_value"),
                "to": change.get("target_value"),
            },
            "rollback_available": status in {"ineffective", "rollback_recommended"} and operation_type == "adjust_budget",
        })
    priority = {"rollback_recommended": 0, "ineffective": 1, "needs_reread": 2, "inconclusive": 3, "waiting": 4, "effective": 5}
    items.sort(key=lambda item: (priority.get(item["status"], 9), -item["executed_at_ms"]))
    evaluated = [item for item in items if item["status"] in {"effective", "ineffective", "rollback_recommended"}]
    effective_count = sum(item["status"] == "effective" for item in evaluated)
    return {
        "observation_window_policy": "高消耗30分钟、普通消耗2小时、低消耗24小时",
        "evaluation_policy": "只评估已完成页面回读、达到最小新增消耗样本且指标口径可比较的动作",
        "items": items[:100],
        "summary": {
            "total": len(items),
            "waiting": sum(item["status"] == "waiting" for item in items),
            "needs_reread": sum(item["status"] == "needs_reread" for item in items),
            "inconclusive": sum(item["status"] == "inconclusive" for item in items),
            "effective": effective_count,
            "ineffective": sum(item["status"] == "ineffective" for item in items),
            "rollback_recommended": sum(item["status"] == "rollback_recommended" for item in items),
            "evaluated": len(evaluated),
            "effective_rate": round(effective_count / len(evaluated), 4) if evaluated else None,
        },
    }


_EVIDENCE_BACKED_TASK_STATUSES = {"effective", "ineffective", "inconclusive"}
_EVALUATED_PROMOTION_STATUSES = {
    "effective", "ineffective", "rollback_recommended", "inconclusive",
}


def _scoped_task_evidence_counts(store_key: str) -> dict[str, int]:
    """Read persisted, evidence-backed task verdicts without refreshing reports."""
    if not store_key:
        return {status: 0 for status in sorted(_EVIDENCE_BACKED_TASK_STATUSES)}
    safe_store = _task_scope_key(store_key).rsplit(":", 1)[0]
    counts = {status: 0 for status in sorted(_EVIDENCE_BACKED_TASK_STATUSES)}
    for item in load_suggestion_snapshots().values():
        if not isinstance(item, dict):
            continue
        scope = str(item.get("scope") or "")
        # Legacy unscoped results cannot prove value for a selected store.
        if not scope or scope.rsplit(":", 1)[0] != safe_store:
            continue
        status = _suggestion_effect_status(item)
        if status in counts:
            counts[status] += 1
    return counts


def _verified_action_count_for_store(selected_store: dict[str, Any]) -> int:
    """Count only writes that have a matching post-write platform readback."""
    linked_accounts = {str(value) for value in selected_store.get("account_keys") or [] if value}
    if not linked_accounts:
        return 0
    return sum(
        1
        for action in load_action_audit().get("actions", [])
        if isinstance(action, dict)
        and action.get("state") == "verified"
        and isinstance(action.get("target_ref"), dict)
        and str(action["target_ref"].get("account_key") or "") in linked_accounts
    )


def _value_milestones(
    *,
    activation_complete: bool = False,
    manual_closed_tasks: int = 0,
    promotion_readback_actions: int = 0,
    promotion_evaluated_actions: int = 0,
    task_evidence_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    evidence_counts = {
        status: max(0, int((task_evidence_counts or {}).get(status) or 0))
        for status in sorted(_EVIDENCE_BACKED_TASK_STATUSES)
    }
    evidence_backed_results = sum(evidence_counts.values())
    # A viewed or manually closed task is activation/progress, not verified
    # value.  Verification starts only after a platform action readback or a
    # task verdict supported by a newer page snapshot.
    verified_value_complete = bool(promotion_readback_actions or evidence_backed_results)
    return {
        "activation_complete": bool(activation_complete),
        "manual_closed_task_count": max(0, int(manual_closed_tasks or 0)),
        "promotion_readback_action_count": max(0, int(promotion_readback_actions or 0)),
        "promotion_evaluated_action_count": max(0, int(promotion_evaluated_actions or 0)),
        "evidence_backed_task_result_count": evidence_backed_results,
        "evidence_backed_task_results": evidence_counts,
        "verified_result_count": max(0, int(promotion_readback_actions or 0)) + evidence_backed_results,
        "first_verified_value_complete": verified_value_complete,
        "verified_value_complete": verified_value_complete,
    }


def build_value_ledger() -> dict[str, Any]:
    """Summarize verified operating value without presenting estimates as settled revenue."""
    catalog = build_store_catalog()
    selected_store_key = str(catalog.get("selected_store_key") or "")
    selected_store = next((item for item in catalog.get("stores", []) if item.get("key") == selected_store_key), None)
    if not selected_store:
        milestones = _value_milestones()
        return {
            "generated_at": _now_label(),
            "summary": {"verified_actions": 0, "evaluated_actions": 0, "effective_actions": 0, "effective_rate": None, "protected_budget_capacity": 0.0, "paused_plans": 0, "reviewed_spend": 0.0, "waiting_review": 0, "completed_tasks": 0, "manual_closed_tasks": 0, "tasks_waiting_review": 0, "blocked_tasks": 0, "readback_promotion_actions": 0, "evaluated_promotion_actions": 0, "evidence_backed_task_results": 0, "verified_results": 0, "first_verified_value_complete": False},
            "value_milestones": milestones,
            "first_verified_value_complete": False,
            "recent": [], "recent_task_outcomes": [], "scope": "unresolved", "trusted_scope": False,
            "note": "尚未识别并选择匿名店铺；未归属数据不会累计到价值账本。",
        }
    effectiveness = build_execution_effectiveness_report()
    linked_accounts = set(selected_store.get("account_keys") or [])
    items = [item for item in effectiveness.get("items", []) if item.get("account_key") in linked_accounts]
    evaluated = [item for item in items if item.get("status") in {"effective", "ineffective", "rollback_recommended"}]
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
    task_effectiveness = get_effectiveness_report()
    # get_effectiveness_report refreshes provisional manual closures after a
    # new page sync; the scoped helper then excludes legacy unassigned data.
    task_evidence_counts = _scoped_task_evidence_counts(selected_store_key)
    manual_closed_tasks = sum(
        item.get("status") == "done" for item in task_states.values() if isinstance(item, dict)
    )
    evaluated_promotion_actions = sum(
        item.get("status") in _EVALUATED_PROMOTION_STATUSES for item in items
    )
    onboarding_scope = _onboarding_scope(_load_onboarding_state(), selected_store_key)
    milestones = _value_milestones(
        activation_complete=bool(onboarding_scope.get("first_task_viewed_at")),
        manual_closed_tasks=manual_closed_tasks,
        promotion_readback_actions=len(items),
        promotion_evaluated_actions=evaluated_promotion_actions,
        task_evidence_counts=task_evidence_counts,
    )
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
            "waiting_review": sum(item.get("status") in {"waiting", "needs_reread", "inconclusive"} for item in items),
            # completed_tasks remains as a compatibility alias.  The clearer
            # product term is manual_closed_tasks: closure alone proves no
            # operating effect.
            "completed_tasks": manual_closed_tasks,
            "manual_closed_tasks": manual_closed_tasks,
            "tasks_waiting_review": sum(item.get("status") == "observing" for item in task_states.values() if isinstance(item, dict)),
            "blocked_tasks": sum(item.get("status") == "blocked" for item in task_states.values() if isinstance(item, dict)),
            "readback_promotion_actions": len(items),
            "evaluated_promotion_actions": evaluated_promotion_actions,
            "evidence_backed_task_results": milestones["evidence_backed_task_result_count"],
            "verified_results": milestones["verified_result_count"],
            "first_verified_value_complete": milestones["first_verified_value_complete"],
        },
        "value_milestones": milestones,
        "first_verified_value_complete": milestones["first_verified_value_complete"],
        "recent": effective[:5],
        "recent_task_outcomes": task_outcomes[:10],
        "recent_verified_task_results": [
            item for item in task_effectiveness.get("recent_evaluations", [])
            if item.get("status") in _EVIDENCE_BACKED_TASK_STATUSES
            and str(item.get("scope") or "").rsplit(":", 1)[0]
            == _task_scope_key(selected_store_key).rsplit(":", 1)[0]
        ][:5],
        "scope": _task_scope_key(),
        "trusted_scope": True,
        "note": "人工结案和查看任务只代表流程进度；首次价值必须有页面回读或新快照支持的任务结果。受控预算幅度和复盘消耗用于衡量 Agent 参与范围，不等同于实际节省、结算收入或增量 GMV。",
    }


def build_execution_preflight_report() -> dict[str, Any]:
    """Build and persist a preflight state as one serialized transition."""

    with _state_lock:
        return _build_execution_preflight_report_locked()


def _recoverable_archived_readback_actions(audit: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [
        item for item in audit.get("actions", [])
        if isinstance(item, dict)
        and item.get("state") in {"executing", "succeeded"}
        and item.get("manual_reconcile_required") is True
        and item.get("manual_reconcile_archived") is True
        and item.get("plan_retry_blocked") is True
        and re.fullmatch(r"[a-f0-9]{24}", str(item.get("action_id") or ""))
        and re.fullmatch(r"[a-f0-9]{32}", str(item.get("execution_authorization_id") or ""))
    ]
    candidates.sort(
        key=lambda item: int(
            item.get("manual_reconcile_archived_at_ms")
            or item.get("manual_reconcile_required_at_ms")
            or item.get("execution_started_at_ms")
            or 0
        ),
        reverse=True,
    )
    return candidates


def _recoverable_archived_readback_action_ids(audit: dict[str, Any]) -> list[str]:
    # Do not truncate this safety index. An omitted older action could lose its
    # only reconstruction path after extension storage is cleared.
    return [
        str(item.get("action_id") or "")
        for item in _recoverable_archived_readback_actions(audit)
    ]


def _build_execution_preflight_report_locked() -> dict[str, Any]:
    """Recheck a short-lived session against the latest Qianchuan page."""

    audit = load_action_audit()
    archived_readback_actions = _recoverable_archived_readback_actions(audit)
    archived_readback_action_ids = [
        str(item.get("action_id") or "") for item in archived_readback_actions
    ]
    archived_readback_summaries = [
        _manual_reconcile_archive_summary(item) for item in archived_readback_actions
    ]
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
            "recoverable_readback_action_ids": archived_readback_action_ids,
            "recoverable_readback_actions": archived_readback_summaries,
        }

    now_ms = int(time.time() * 1000)
    state = str(stored.get("state") or "awaiting_reread")
    authorization_expires_at_ms = int(stored.get("authorization_expires_at_ms") or 0)
    if state == "authorized" and authorization_expires_at_ms <= now_ms:
        state = "expired"
        stored = {**stored, "state": state, "write_enabled": False, "execution_enabled": False}
        with _state_lock:
            _save_execution_preflight(stored)
    elif state not in {
        "stopped", "expired", "invalidated", "completed", "execution_failed", "manual_reconcile_required",
        "manual_reconcile_archived",
        "authorized", "authorization_consumed",
    } and int(stored.get("expires_at_ms") or 0) <= now_ms:
        state = "expired"
        stored = {**stored, "state": state, "write_enabled": False, "execution_enabled": False}
        with _state_lock:
            _save_execution_preflight(stored)

    action = next(
        (item for item in audit.get("actions", []) if item.get("action_id") == stored.get("action_id")),
        None,
    )
    # The receipt action write can survive a power loss while the companion
    # preflight marker does not. Reconcile only an exact consumed grant; never
    # infer a receipt from action state or browser storage.
    receipt = action.get("execution_receipt") if isinstance(action, dict) and isinstance(action.get("execution_receipt"), dict) else None
    receipt_matches_consumed_action = False
    if isinstance(action, dict) and isinstance(receipt, dict):
        expected_receipt = _execution_request_for_action(action)
        receipt_bindings = {
            "operation_type": (expected_receipt.get("operation_type"), receipt.get("operation_type")),
            "store_key": (str(expected_receipt.get("store_key") or "").lower(), str(receipt.get("store_key") or "").lower()),
            "account_key": (str(expected_receipt.get("account_key") or "").lower(), str(receipt.get("account_key") or "").lower()),
            "plan_id": (expected_receipt.get("plan_id"), receipt.get("plan_id")),
            "target_value": (expected_receipt.get("target_value"), receipt.get("target_value")),
        }
        receipt_matches_consumed_action = all(
            _execution_receipt_matches(wanted, received)
            for wanted, received in receipt_bindings.values()
        )
    if (
        isinstance(action, dict)
        and stored.get("authorization_consumed") is True
        and stored.get("execution_receipt_recorded") is not True
        and isinstance(receipt, dict)
        and receipt_matches_consumed_action
        and re.fullmatch(r"[a-f0-9]{32}", str(stored.get("authorization_id") or ""))
        and str(receipt.get("authorization_id") or "") == str(stored.get("authorization_id") or "")
        and str(action.get("execution_authorization_id") or "") == str(stored.get("authorization_id") or "")
    ):
        stored = {
            **stored,
            "execution_receipt_recorded": True,
            "execution_receipt_recorded_at_ms": int(
                stored.get("execution_receipt_recorded_at_ms")
                or action.get("execution_reported_at_ms")
                or now_ms
            ),
            "execution_receipt_action_state": str(action.get("state") or ""),
            "write_enabled": False,
            "execution_enabled": False,
        }
        _save_execution_preflight(stored)
    if state in {"authorization_consumed", "manual_reconcile_required", "manual_reconcile_archived"} and isinstance(action, dict):
        action_state = str(action.get("state") or "")
        if action_state in {"executing", "succeeded"} and action.get("manual_reconcile_required") is True:
            state = (
                "manual_reconcile_archived"
                if action.get("manual_reconcile_archived") is True and action.get("plan_retry_blocked") is True
                else "manual_reconcile_required"
            )
            stored = {
                **stored,
                "state": state,
                "manual_reconcile_required_at_ms": int(
                    action.get("manual_reconcile_required_at_ms") or now_ms
                ),
                "write_enabled": False,
                "execution_enabled": False,
            }
            _save_execution_preflight(stored)
        elif action_state in {"verified", "rolled_back", "failed"}:
            state = "execution_failed" if action_state == "failed" else "completed"
            stored = {
                **stored,
                "state": state,
                "execution_receipt_action_state": action_state,
                "completed_at_ms": int(stored.get("completed_at_ms") or now_ms),
                "write_enabled": False,
                "execution_enabled": False,
            }
            _save_execution_preflight(stored)
    binding_error = ""
    if isinstance(action, dict):
        try:
            _validate_confirmed_action_for_execution(action, stored)
        except ValueError as error:
            binding_error = str(error)
    else:
        binding_error = "执行会话对应的动作不存在。"
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
            "id": "canonical_action",
            "label": "动作由本机 Agent 签发且会话绑定一致",
            "passed": not binding_error,
            "detail": binding_error or "签名、店铺、账户和参数指纹均一致。",
        },
        {
            "id": "store_match",
            "label": "店铺身份与动作授权一致",
            "passed": bool(
                readback
                and readback.get("store_identity_matches") is True
                and str(readback.get("actual_store_key") or "") == str(stored.get("store_key") or "")
            ),
            "detail": f"授权店铺：{str(stored.get('store_key') or '未绑定')}",
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
            "id": "document_provenance",
            "label": "页面文档实例可验证",
            "passed": bool(
                readback
                and re.fullmatch(
                    r"[A-Za-z0-9_-]{16,128}",
                    str(readback.get("document_instance_id") or "").strip(),
                )
                and started_at_ms < int(readback.get("navigation_started_at_ms") or 0) <= int(readback.get("captured_at_ms") or 0)
            ),
            "detail": "执行前检查启动后必须硬刷新为新的页面文档；旧 DOM 或乐观值不能作为授权基线。",
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
        label = "执行结果待确认 · 禁止重复提交"
    elif state == "authorized":
        label = "最终授权已生成"
    elif state == "stopped":
        label = "已紧急停止"
    elif state == "invalidated":
        label = "店铺或账户已变化 · 授权已失效"
    elif state == "completed":
        label = "页面回读已完成"
    elif state == "execution_failed":
        label = "本次执行未生效或明确未提交"
    elif state == "manual_reconcile_required":
        label = "提交结果不确定 · 原计划继续锁定并等待人工核对"
    elif state == "manual_reconcile_archived":
        label = "未决结果已归档 · 同一计划永久禁止自动重投"
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
            "account_key": str(target.get("account_key") or ""),
            "store_key": str(((action or {}).get("proposal_scope") or {}).get("store_key") or ""),
            "page_type": str(((action or {}).get("evidence_ref") or {}).get("page_type") or ""),
            # The browser must display and return this exact server-rendered
            # phrase. Reconstructing it from a DOM dataset loses numeric
            # representation (for example 400.0 becomes 400).
            "confirmation_text": _execution_confirmation_text(operation_type, target_value),
            "archive_confirmation_text": (
                _manual_reconcile_archive_confirmation_text(action)
                if isinstance(action, dict) and action.get("manual_reconcile_required") is True
                else ""
            ),
            "manual_reconcile_archivable": bool(
                isinstance(action, dict)
                and action.get("state") in {"executing", "succeeded"}
                and action.get("manual_reconcile_required") is True
                and action.get("manual_reconcile_archived") is not True
            ),
            "manual_reconcile_archived": bool(
                isinstance(action, dict) and action.get("manual_reconcile_archived") is True
            ),
            "plan_retry_blocked": bool(
                isinstance(action, dict) and action.get("plan_retry_blocked") is True
            ),
            "safe_to_retry": False if isinstance(action, dict) and action.get("plan_retry_blocked") is True else None,
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
        "recoverable_readback_action_ids": archived_readback_action_ids,
        "recoverable_readback_actions": archived_readback_summaries,
        "next_step": (
            "授权凭证已被原子消费，动作已锁定；请重新读取当前计划确认最终状态，禁止再次提交。"
            if state == "authorization_consumed"
            else
            "已生成 60 秒单次授权凭证；受监督执行器将只提交本次降低预算动作。"
            if state == "authorized"
            else
            "结果仍不确定。可继续独立回读；如需继续经营，可输入归档口令释放其他计划，但同一计划将永久禁止自动重投。"
            if state == "manual_reconcile_required"
            else
            "未把该动作标记为成功或失败；其他计划已释放，同一计划永久禁止自动重投，仍可继续独立回读。"
            if state == "manual_reconcile_archived"
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


def _legacy_plan_task_id(item: dict[str, Any], title: str = "") -> str:
    """Return the pre-contract plan task id for one-way state migration only."""

    action_type = str(item.get("action_type") or "")
    task_identity = str(
        item.get("plan_identity_key")
        or item.get("id")
        or title
        or item.get("workbench_title")
        or "千川计划"
    )
    return hashlib.sha256(
        f"投放运营|{task_identity}|{action_type}".encode("utf-8")
    ).hexdigest()[:16]


def _legacy_contract_plan_task_id(
    item: dict[str, Any], *, store_key: str, account_key: str
) -> str:
    """Reproduce the v4.14.5 plan-contract id for one-way migration."""

    title = str(item.get("title") or item.get("workbench_title") or "")
    action_params = item.get("action_params") if isinstance(item.get("action_params"), dict) else {}
    operation_type = str(action_params.get("operation_type") or "").strip().lower()
    if "同步" in title:
        rule_id = "system.sync.plans"
    elif operation_type:
        rule_id = f"qianchuan.plan.{re.sub(r'[^a-z0-9_]+', '_', operation_type)[:48]}"
    else:
        suffix = hashlib.sha256(title.encode("utf-8")).hexdigest()[:10]
        rule_id = f"ops.plans.diagnosis.{suffix}"
    target_ref = action_params.get("target_ref") if isinstance(action_params.get("target_ref"), dict) else {}
    name = str(target_ref.get("name") or action_params.get("target") or title)[:160]
    identifier = str(target_ref.get("id") or "")[:96]
    if not identifier:
        identifier = hashlib.sha256(f"plan|{name}".encode("utf-8")).hexdigest()[:20]
    subject_kind = str(target_ref.get("kind") or "plan")
    task_key = hashlib.sha256(
        f"{rule_id}|{store_key}|{account_key}|{subject_kind}|{identifier}".encode("utf-8")
    ).hexdigest()[:24]
    return task_key[:16]


def _plan_workbench_fields(
    item: dict[str, Any], task_states: dict[str, Any] | None = None
) -> dict[str, Any]:
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
    legacy_task_id = _legacy_plan_task_id(item, title)
    return {
        **fields,
        "found": str(item.get("reason") or "当前计划数据异常"),
        "action": str(item.get("suggestion") or "请回到千川后台核对。"),
        "owner": "投放运营",
        "workbench_title": title,
        "legacy_task_ids": [legacy_task_id],
        "current_roi": roi,
    }


def _plan_ops_task_entry(item: dict[str, Any]) -> dict[str, Any]:
    """Translate one plan recommendation without discarding its identity."""

    legacy_ids = [
        str(value) for value in item.get("legacy_task_ids", [])
        if re.fullmatch(r"[a-f0-9]{16}", str(value or ""))
    ]
    if not legacy_ids:
        legacy_ids = [_legacy_plan_task_id(item, str(item.get("workbench_title") or ""))]
    task_entry: dict[str, Any] = {
        "level": item["level"],
        "owner": "投放运营",
        "title": item["workbench_title"],
        "action": item["action"],
        "acceptance": item["acceptance"],
        "evidence": item["found"],
        "impact": item["adjustment_range"],
        "observation_window": item["observation_window"],
        "confidence": item.get("confidence"),
        "action_type": item.get("action_type"),
        "plan_key": item.get("plan_key"),
        "plan_identity_key": item.get("plan_identity_key"),
        "plan_id": item.get("plan_id"),
        "account_key": item.get("account_key"),
        "promotion_mode": item.get("promotion_mode"),
        "plan_type": item.get("plan_type"),
        "source": item.get("source"),
        "legacy_task_ids": list(dict.fromkeys(legacy_ids)),
    }
    if isinstance(item.get("action_params"), dict):
        task_entry["action_params"] = item["action_params"]
    if isinstance(item.get("task_contract"), dict):
        task_entry["task_contract"] = item["task_contract"]
    task_entry["ambiguous_legacy_task_ids"] = [
        str(value) for value in item.get("ambiguous_legacy_task_ids", [])
        if re.fullmatch(r"[a-f0-9]{16}", str(value or ""))
    ]
    return task_entry


def _task_state_for_ids(
    states: dict[str, Any], canonical_id: str, legacy_ids: list[str]
) -> tuple[dict[str, Any], str]:
    """Resolve canonical state first, then a known legacy alias."""

    canonical_state = states.get(canonical_id)
    if isinstance(canonical_state, dict) and canonical_state:
        return canonical_state, canonical_id
    legacy_states = [
        (index, legacy_id, states.get(legacy_id))
        for index, legacy_id in enumerate(legacy_ids)
        if isinstance(states.get(legacy_id), dict) and states.get(legacy_id)
    ]
    if legacy_states:
        _, legacy_id, legacy_state = max(
            legacy_states,
            key=lambda value: (
                str(value[2].get("updated_at") or ""),
                -value[0],
            ),
        )
        return legacy_state, legacy_id
    return {}, ""


def build_plan_recommendations(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    settings = settings or load_agent_settings()
    roi_target = float(settings["roi_target"])
    min_spend = float(settings["min_spend_for_action"])
    results: list[dict[str, Any]] = []
    records = _table_records("doudian", {"qianchuan_campaigns", "qianchuan_live", "qianchuan_report"})
    records.extend(_table_records("qianchuan", {"campaigns", "qianchuan_live", "report"}))
    now_ms = int(time.time() * 1000)

    for entry in records:
        captured_at_ms = int(entry.get("captured_at_ms") or 0)
        future_timestamp = captured_at_ms - now_ms > MAX_CAPTURE_FUTURE_SKEW_MS
        data_age_seconds = (
            max(0, int((now_ms - captured_at_ms) / 1000))
            if captured_at_ms > 0 and not future_timestamp
            else None
        )
        # Performance recommendations are decisions, not historical reporting.
        # Missing, future-dated or expired plan evidence must send the operator
        # back to synchronization instead of turning unknown values into a
        # budget, ROI or creative conclusion.
        if data_age_seconds is None or data_age_seconds >= PLAN_CONSOLE_STALE_SECONDS:
            continue
        record = entry["record"]
        plan_id = _plan_identifier(record)
        plan = _plan_name(record, plan_id)
        if not plan and str(entry.get("page_type") or "") == "qianchuan_live":
            plan = _live_plan_display_name(record)
        # A material aggregate, account total or malformed virtual table is not
        # a plan. In particular, a generic 抖音号 cell is not plan evidence.
        if not plan:
            continue
        if re.match(r"^共\s*\d+\s*(?:条计划|个抖音号)", plan):
            continue
        page_type = str(entry.get("page_type") or "")
        plan_type = (
            "live" if page_type == "qianchuan_live"
            else "product" if page_type in {"campaigns", "qianchuan_campaigns"}
            else "unknown"
        )
        promotion_context = build_promotion_context(entry.get("promotion_context"))
        promotion_mode = str(promotion_context.get("promotion_mode") or "unknown")
        if promotion_mode == "unknown":
            _, promotion_mode_value = _pick(record, ("推广类型", "推广模式", "投放模式"))
            promotion_mode = str(build_promotion_context(promotion_mode_value).get("promotion_mode") or "unknown")
        account_scope = promotion_context.get("account_scope") if isinstance(promotion_context.get("account_scope"), dict) else {}
        account_key = str(entry.get("account_key") or account_scope.get("account_id") or "").strip().lower()[:128]
        plan_identity_key = _plan_identity_key(account_key, promotion_mode, plan_type, plan_id)
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
            "plan_id": plan_id,
            "plan_key": _plan_key(account_key, promotion_mode, plan_type, plan_id),
            "plan_identity_key": plan_identity_key,
            "account_key": account_key,
            "promotion_mode": promotion_mode,
            "plan_type": plan_type,
            "source": str(entry.get("source") or ""),
            "evidence": evidence,
            "confidence": confidence,
            "guardrail": "仅生成建议；执行前请核对统计周期、归因口径和当日预算。",
        }

        def action_payload(candidate_action_type: str) -> dict[str, Any]:
            if not plan_id:
                return {
                    "read_only": True,
                    "actionable": False,
                    "action_blocked_reasons": [{
                        "code": "TARGET_ID_MISSING",
                        "message": "缺少计划唯一 ID，仅保留只读诊断。",
                    }],
                }
            draft = _action_params_for_plan(
                plan, candidate_action_type, evidence, entry, confidence
            )
            return {
                "read_only": True,
                "actionable": draft.get("can_confirm") is True and not draft.get("blocked_reasons"),
                "action_params": draft,
            }

        if spend is not None and spend >= min_spend and (orders == 0 or orders is None and roi == 0):
            results.append(
                {
                    **base,
                    "level": "high",
                    "action_type": "stop_loss",
                    "suggestion": "先降预算 30% 或暂停新增消耗，检查素材、人群和商品承接后再恢复。",
                    "reason": f"消耗已达到 {spend:g}，但当前未观察到成交。",
                    **action_payload("stop_loss"),
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
                    **action_payload("reduce_budget"),
                }
            )
        elif roi is not None and roi < effective_roi_target:
            reason = f"ROI {roi:g} 低于目标 {effective_roi_target:g}，暂不适合放量。"
            suggestion = "预算保持不变，先优化素材点击率与商品承接；达到目标后再逐级放量。"
            if ctr is not None and ctr < 1:
                suggestion = "预算保持不变，优先更换前 3 秒表达、封面和卖点；不要先提高出价。"
                reason += f" 当前点击率为 {ctr:g}。"
            results.append({**base, "level": "warning", "action_type": "optimize", "suggestion": suggestion, "reason": reason, **action_payload("optimize")})
        elif roi is not None and roi >= effective_roi_target and (orders or 0) >= 3:
            results.append(
                {
                    **base,
                    "level": "opportunity",
                    "action_type": "scale_cautiously",
                    "suggestion": "可尝试增加预算 10%–15%，每次只调一次，并观察一个完整转化窗口。",
                    "reason": f"ROI {roi:g} 达到目标 {effective_roi_target:g}，且已有 {orders:g} 个成交。",
                    **action_payload("scale_cautiously"),
                }
            )

    if not results:
        eligible_summary_pages = {"overview", "report", "qianchuan_report"}
        roi_metrics = [
            match for match in _metric_matches("qianchuan", ("roi", "支付roi", "成交roi"))
            if match[0].get("fresh") is True and match[0].get("page_type") in eligible_summary_pages
        ]
        spend_metrics = [
            match for match in _metric_matches("qianchuan", ("消耗", "花费"))
            if match[0].get("fresh") is True and match[0].get("page_type") in eligible_summary_pages
        ]
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
        identity = str(item.get("plan_identity_key") or "")
        read_only_identity = "|".join((
            "readonly",
            str(item.get("source") or ""),
            str(item.get("id") or ""),
            str(item.get("account_key") or ""),
            str(item.get("promotion_mode") or "unknown"),
            str(item.get("plan_type") or "unknown"),
        ))
        unique.setdefault((identity or read_only_identity, item["action_type"]), item)
    audit_states = {
        str(action.get("action_id") or ""): action
        for action in load_action_audit().get("actions", [])
        if isinstance(action, dict)
    }
    persisted_settings = load_agent_settings()
    task_store_key = str(
        settings.get("store_key") or persisted_settings.get("store_key") or ""
    ).lower()
    task_account_key = str(
        settings.get("qianchuan_account_key")
        or persisted_settings.get("qianchuan_account_key")
        or ""
    ).lower()
    task_business_date = time.strftime("%Y-%m-%d")
    task_contract_now_ms = int(time.time() * 1000)
    task_source_index = _task_contract_source_index()
    task_states = load_task_states(task_store_key, task_business_date)
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
        workbench_item = {**item, **_plan_workbench_fields(item)}
        task_entry = _plan_ops_task_entry(workbench_item)
        task_contract = _build_task_contract(
            task_entry,
            task_source_index,
            store_key=task_store_key,
            account_key=task_account_key,
            business_date=task_business_date,
            now_ms=task_contract_now_ms,
        )
        task_id = str(task_contract.get("task_key") or "")[:16]
        legacy_task_ids = [
            *list(task_entry.get("legacy_task_ids") or []),
            _legacy_contract_plan_task_id(
                task_entry,
                store_key=task_store_key,
                account_key=task_account_key,
            ),
        ]
        legacy_task_ids = list(dict.fromkeys(
            legacy_id for legacy_id in legacy_task_ids
            if re.fullmatch(r"[a-f0-9]{16}", str(legacy_id or ""))
            and legacy_id != task_id
        ))
        task_state, _ = _task_state_for_ids(task_states, task_id, legacy_task_ids)
        cleaned.append({
            **workbench_item,
            "task_id": task_id,
            "task_key": task_contract.get("task_key"),
            "contract_version": 2,
            "contract_fingerprint": task_contract.get("contract_fingerprint"),
            "task_contract": task_contract,
            "legacy_task_ids": legacy_task_ids,
            "task_status": task_state.get("status", "todo"),
            "task_updated_at": task_state.get("updated_at"),
        })
    alias_counts: dict[str, int] = {}
    for recommendation in cleaned:
        for legacy_task_id in recommendation.get("legacy_task_ids", []):
            alias_counts[legacy_task_id] = alias_counts.get(legacy_task_id, 0) + 1
    for recommendation in cleaned:
        # The previous contract omitted promotion mode/type, so one old id can
        # represent several current plan identities.  Such an alias must never
        # copy or mutate state until it is uniquely attributable.
        unique_legacy_ids = [
            legacy_task_id
            for legacy_task_id in recommendation.get("legacy_task_ids", [])
            if alias_counts.get(legacy_task_id) == 1
        ]
        recommendation["ambiguous_legacy_task_ids"] = [
            legacy_task_id
            for legacy_task_id in recommendation.get("legacy_task_ids", [])
            if alias_counts.get(legacy_task_id, 0) > 1
        ]
        recommendation["legacy_task_ids"] = unique_legacy_ids
        task_state, _ = _task_state_for_ids(
            task_states, recommendation["task_id"], unique_legacy_ids
        )
        recommendation["task_status"] = task_state.get("status", "todo")
        recommendation["task_updated_at"] = task_state.get("updated_at")
    return cleaned


def build_qianchuan_plan_console() -> dict[str, Any]:
    """Normalize every captured plan row for the local read-only plan center."""

    now_ms = int(time.time() * 1000)
    records = _table_records("doudian", {"qianchuan_campaigns", "qianchuan_live", "qianchuan_report"})
    records.extend(_table_records("qianchuan", {"campaigns", "qianchuan_live", "plans", "report"}))
    recommendation_by_identity = {
        identity: item
        for item in build_plan_recommendations()
        if (
            identity := _plan_identity_key(
                item.get("account_key"),
                item.get("promotion_mode"),
                item.get("plan_type"),
                item.get("plan_id"),
            )
        )
    }
    mode_labels = {
        "standard": "标准计划",
        "full_domain": "全域推广",
        "chengfang": "乘方推广",
        "suixintui": "随心推",
        "unknown": "模式待识别",
    }
    type_labels = {"live": "直播计划", "product": "商品计划", "unknown": "类型待识别"}
    normalized: dict[str, dict[str, Any]] = {}

    for entry in records:
        record = entry["record"]
        plan_id = _plan_identifier(record)
        plan_name = _plan_name(record, plan_id)
        if not plan_name and str(entry.get("page_type") or "") == "qianchuan_live":
            plan_name = _live_plan_display_name(record)
        if not plan_name or re.match(r"^共\s*\d+\s*(?:条计划|个计划|个抖音号)", plan_name):
            continue

        promotion_context = entry.get("promotion_context") if isinstance(entry.get("promotion_context"), dict) else {}
        promotion_mode = str(promotion_context.get("promotion_mode") or "unknown")
        if promotion_mode == "unknown":
            _, promotion_mode_value = _pick(record, ("推广类型", "推广模式", "投放模式"))
            promotion_mode = build_promotion_context(promotion_mode_value)["promotion_mode"]
        if promotion_mode not in mode_labels:
            promotion_mode = "unknown"
        page_type = str(entry.get("page_type") or "")
        plan_type = "live" if page_type == "qianchuan_live" else "product" if page_type in {"campaigns", "qianchuan_campaigns", "plans"} else "unknown"
        account_scope = promotion_context.get("account_scope") if isinstance(promotion_context.get("account_scope"), dict) else {}
        account_key = str(entry.get("account_key") or account_scope.get("account_id") or "").lower()[:128]
        account_label = str(entry.get("account_label") or "").strip()[:80]
        if not account_label and account_key:
            account_label = f"匿名账户 {account_key[-4:].upper()}"

        _, status_value = _pick(record, ("投放状态", "计划状态", "状态"))
        delivery_status = _normalize_delivery_status(status_value) or "状态待识别"
        budget = _evidence_value(record, ("日预算", "总预算", "预算"))
        spend = _evidence_value(record, ("消耗", "花费", "支出"))
        roi = _evidence_value(record, ("支付roi", "成交roi", "roi"))
        orders = _evidence_value(record, ("成交订单", "支付订单", "成交数", "转化数"))
        ctr = _evidence_value(record, ("点击率", "ctr"))
        learning_phase, learning_status_label = _normalize_learning_health(
            _evidence_text(record, ("学习期状态",))
        )
        platform_low_efficiency, platform_low_efficiency_label = _normalize_low_efficiency_health(
            _evidence_text(record, ("平台低效",))
        )
        diagnostic_source, diagnostic_source_label = _normalize_plan_diagnostic_source(
            _evidence_text(record, ("诊断来源",))
        )
        quality_score = int(entry.get("quality_score") or 0)
        captured_at_ms = int(entry.get("captured_at_ms") or 0)
        future_timestamp = captured_at_ms - now_ms > MAX_CAPTURE_FUTURE_SKEW_MS
        data_age_seconds = (
            max(0, int((now_ms - captured_at_ms) / 1000))
            if captured_at_ms > 0 and not future_timestamp
            else None
        )
        stale = captured_at_ms <= 0 or future_timestamp or data_age_seconds >= PLAN_CONSOLE_STALE_SECONDS
        blockers = []
        if not account_key:
            blockers.append("广告账户身份缺失")
        if not plan_id:
            blockers.append("计划 ID 缺失")
        if promotion_mode == "unknown":
            blockers.append("投放模式尚未验真")
        if quality_score < 70:
            blockers.append("数据质量低于 70")
        if future_timestamp:
            blockers.append("计划采集时间异常晚于本机，请校准时间并重新同步")
        elif stale:
            blockers.append("计划数据已过期，请重新同步")

        plan_identity_key = _plan_identity_key(
            account_key, promotion_mode, plan_type, plan_id
        )
        recommendation = recommendation_by_identity.get(plan_identity_key, {})
        risk_level = str(recommendation.get("level") or "info")
        plan_key = _plan_key(account_key, promotion_mode, plan_type, plan_id)
        item = {
            "plan_key": plan_key,
            "plan_id": plan_id,
            "plan_name": plan_name[:100],
            "account_key": account_key,
            "account_label": account_label or "账户待识别",
            "promotion_mode": promotion_mode,
            "promotion_mode_label": mode_labels[promotion_mode],
            "plan_type": plan_type,
            "plan_type_label": type_labels[plan_type],
            "delivery_status": delivery_status[:60],
            "budget": budget,
            "spend": spend,
            "roi": roi,
            "orders": orders,
            "ctr": ctr,
            "learning_phase": learning_phase,
            "learning_status_label": learning_status_label,
            "platform_low_efficiency": platform_low_efficiency,
            "platform_low_efficiency_label": platform_low_efficiency_label,
            "diagnostic_source": diagnostic_source,
            "diagnostic_source_label": diagnostic_source_label,
            "risk_level": risk_level,
            "recommendation": str(recommendation.get("suggestion") or "")[:240],
            "quality_score": quality_score,
            "captured_at_ms": captured_at_ms,
            "data_age_seconds": data_age_seconds,
            "future_timestamp": future_timestamp,
            "stale": stale,
            "eligible_for_local_binding": not blockers,
            "binding_blockers": blockers,
            "read_only": True,
        }
        is_official_health = str(item.get("diagnostic_source") or "").startswith("official_api")
        item["_operating_is_official"] = is_official_health
        item["_health_captured_at_ms"] = captured_at_ms if is_official_health else 0
        # Never merge on display name or on account/id alone.  The same id may
        # exist on another promotion surface, while no-id rows remain separate
        # read-only observations and can never inherit an actionable identity.
        identity_key = plan_identity_key or "|".join((
            "readonly",
            str(entry.get("source") or ""),
            page_type,
            account_key,
            promotion_mode,
            plan_type,
            str(entry.get("table_index") or 0),
            str(entry.get("row_index") or 0),
            str(captured_at_ms),
            hashlib.sha256(
                json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16],
        ))
        previous = normalized.get(identity_key)
        if previous is None:
            normalized[identity_key] = item
            continue

        def operating_rank(candidate: dict[str, Any]) -> tuple[int, int, int]:
            evidence_count = sum(
                candidate.get(field) is not None
                for field in ("budget", "spend", "roi", "orders", "ctr")
            )
            evidence_count += int(candidate.get("delivery_status") != "状态待识别")
            evidence_count += int(candidate.get("plan_type") != "unknown")
            return (
                int(candidate.get("captured_at_ms") or 0),
                evidence_count,
                int(candidate.get("quality_score") or 0),
            )

        # When both sources describe the same plan, the browser row remains
        # the operating-metric snapshot. Official API fields are an independent
        # health overlay and must never erase spend/ROI/order evidence.
        previous_official = previous.get("_operating_is_official") is True
        current_official = item.get("_operating_is_official") is True
        if previous_official != current_official:
            primary = item if not current_official else previous
        else:
            primary = item if operating_rank(item) >= operating_rank(previous) else previous
        health_candidates = [
            candidate
            for candidate in (previous, item)
            if str(candidate.get("diagnostic_source") or "").startswith("official_api")
        ]
        health = max(
            health_candidates,
            key=lambda candidate: int(candidate.get("_health_captured_at_ms") or candidate.get("captured_at_ms") or 0),
            default=None,
        )
        merged = dict(primary)
        if health is not None:
            for field in (
                "learning_phase",
                "learning_status_label",
                "platform_low_efficiency",
                "platform_low_efficiency_label",
                "diagnostic_source",
                "diagnostic_source_label",
            ):
                merged[field] = health.get(field)
            merged["_health_captured_at_ms"] = int(
                health.get("_health_captured_at_ms") or health.get("captured_at_ms") or 0
            )
        normalized[identity_key] = merged

    status_order = {"投放中": 0, "暂停": 1, "状态待识别": 3}
    collected_rows = sorted(
        (
            {key: value for key, value in item.items() if not key.startswith("_")}
            for item in normalized.values()
        ),
        key=lambda item: (
            status_order.get(str(item.get("delivery_status")), 2),
            -(float(item.get("spend")) if isinstance(item.get("spend"), (int, float)) else -1),
            str(item.get("plan_name")),
        ),
    )
    rows = collected_rows[:500]
    collection_snapshots = _plan_collection_snapshots()
    collection_receipt = build_plan_collection_receipt(
        collected_rows,
        collection_snapshots,
        displayed_rows=len(rows),
    )
    # Scoped receipts describe the rows actually delivered to the workbench.
    # A scope that falls beyond the 500-row response cap must never be marked
    # complete merely because the local database contains additional rows.
    collection_receipts = build_scoped_plan_collection_receipts(rows, collection_snapshots)
    receipts_by_scope = {
        str(receipt.get("scope_key") or ""): receipt
        for receipt in collection_receipts
        if isinstance(receipt, dict) and str(receipt.get("scope_key") or "")
    }
    qualified_rows: list[dict[str, Any]] = []
    for row in rows:
        gate = _plan_collection_gate_for_row(row, receipts_by_scope)
        qualified_rows.append({
            **row,
            "collection_scope_key": gate["scope_key"],
            "collection_gate_state": gate["state"],
            "collection_scope_status": gate["receipt_status"],
            "collection_scope_identity_matches": gate["scope_identity_matches"],
            "read_only_diagnosis_ready": gate["read_only_diagnosis_ready"],
            "supervised_draft_ready": gate["supervised_draft_ready"],
            "automation_blockers": gate["blockers"],
            "automation_next_action": gate["next_action"],
            "execution_enabled": False,
            # Do not present a metric conclusion as actionable before the
            # exact scoped receipt proves that the relevant list was complete.
            "recommendation": (
                row.get("recommendation") if gate["read_only_diagnosis_ready"] else ""
            ),
        })
    rows = qualified_rows
    account_map: dict[str, dict[str, str]] = {}
    for row in rows:
        if row["account_key"]:
            account_map.setdefault(row["account_key"], {"account_key": row["account_key"], "account_label": row["account_label"]})
    total_spend = round(sum(float(row["spend"]) for row in rows if isinstance(row.get("spend"), (int, float))), 2)
    roi_rows = [row for row in rows if isinstance(row.get("roi"), (int, float)) and isinstance(row.get("spend"), (int, float)) and float(row["spend"]) > 0]
    roi_spend = sum(float(row["spend"]) for row in roi_rows)
    weighted_roi = round(sum(float(row["roi"]) * float(row["spend"]) for row in roi_rows) / roi_spend, 2) if roi_spend else None
    known_data_times = [
        int(row["captured_at_ms"])
        for row in rows
        if int(row.get("captured_at_ms") or 0) > 0 and row.get("future_timestamp") is not True
    ]
    data_as_of_ms = max(known_data_times) if known_data_times else None
    oldest_data_as_of_ms = min(known_data_times) if known_data_times else None
    # The top-level age represents the oldest row that can still participate
    # in a multi-plan action.  Keep the latest age separately for display so a
    # fresh row can never hide stale rows from the safety gate.
    data_age_seconds = max(0, int((now_ms - oldest_data_as_of_ms) / 1000)) if oldest_data_as_of_ms is not None else None
    latest_data_age_seconds = max(0, int((now_ms - data_as_of_ms) / 1000)) if data_as_of_ms is not None else None
    stale_count = sum(row["stale"] for row in rows)
    freshness_status = "missing" if not rows else "stale" if stale_count else "fresh"
    next_actions_by_scope: dict[str, dict[str, Any]] = {}
    for row in rows:
        action = row.get("automation_next_action") if isinstance(row.get("automation_next_action"), dict) else {}
        scope_key = str(row.get("collection_scope_key") or "unknown")
        if scope_key not in next_actions_by_scope and action:
            next_actions_by_scope[scope_key] = {"scope_key": scope_key, **action}
    next_action_priority = {
        "review_supervised_draft": 0,
        "refresh_verified_scope": 1,
        "read_plan_id": 2,
        "continue_scoped_collection": 3,
        "reselect_verified_scope": 4,
        "repair_plan_evidence": 5,
    }
    scoped_next_actions = sorted(
        next_actions_by_scope.values(),
        key=lambda item: (next_action_priority.get(str(item.get("code") or ""), 9), str(item.get("scope_key") or "")),
    )
    return {
        "schema_version": 1,
        "generated_at": _now_label(),
        "safe": True,
        "read_only": True,
        "platform_write_enabled": False,
        "automatic_batch_submit": False,
        "data_as_of_ms": data_as_of_ms,
        "oldest_data_as_of_ms": oldest_data_as_of_ms,
        "data_age_seconds": data_age_seconds,
        "latest_data_age_seconds": latest_data_age_seconds,
        "freshness_status": freshness_status,
        "accounts": list(account_map.values()),
        "rows": rows,
        "collection_receipt": collection_receipt,
        "collection_receipts": collection_receipts,
        "execution_preparation": {
            "mode": "read_only_to_supervised_draft",
            "diagnosis_ready": sum(row["read_only_diagnosis_ready"] for row in rows),
            "supervised_draft_ready": sum(row["supervised_draft_ready"] for row in rows),
            "blocked": sum(not row["supervised_draft_ready"] for row in rows),
            "execution_enabled": False,
            "next_step": str((scoped_next_actions[0] if scoped_next_actions else {}).get("detail") or "先读取一个明确的计划范围。"),
            "scope_next_actions": scoped_next_actions,
        },
        "summary": {
            "total": len(rows),
            "running": sum(row["delivery_status"] == "投放中" for row in rows),
            "paused": sum(row["delivery_status"] == "暂停" for row in rows),
            "binding_ready": sum(row["eligible_for_local_binding"] for row in rows),
            "diagnosis_ready": sum(row["read_only_diagnosis_ready"] for row in rows),
            "supervised_draft_ready": sum(row["supervised_draft_ready"] for row in rows),
            "stale": stale_count,
            "spend": total_spend,
            "weighted_roi": weighted_roi,
        },
        "notice": "计划中心只读取本机快照；本地绑定、筛选和批量草稿不会触发千川写入。",
    }


def build_automation_readiness(recommendations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build the future executor candidate queue without enabling execution."""

    recommendations = recommendations if recommendations is not None else build_plan_recommendations()
    requires_collection_gate = any(
        isinstance(item.get("action_params"), dict)
        and isinstance(item["action_params"].get("evidence_ref"), dict)
        and item["action_params"]["evidence_ref"].get("collection_scope_required") is True
        for item in recommendations
        if isinstance(item, dict)
    )
    plan_console = build_qianchuan_plan_console() if requires_collection_gate else None
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
        promotion_guard = legacy_execution_guard(
            action.get("operation_type"),
            action.get("promotion_context"),
            expected_account_key=_action_target_account_key(action),
        )
        if readiness["status"] in {"confirmable", "preflight_ready"} and promotion_guard.get("allowed") is not True:
            readiness = {
                **readiness,
                "status": "blocked",
                "status_label": "执行上下文未通过",
                "stage": "qualification",
                "next_step": str(promotion_guard.get("reason") or "请补齐计划身份与指标口径后重新同步。"),
                "can_enter_preflight": False,
                "blocked_reasons": [
                    *readiness.get("blocked_reasons", []),
                    {
                        "code": str(promotion_guard.get("code") or "PROMOTION_SCOPE_UNVERIFIED"),
                        "message": str(promotion_guard.get("reason") or "投放执行上下文尚未通过。"),
                    },
                ],
            }
        collection_gate = _supervised_draft_collection_gate(action, plan_console)
        if readiness["status"] in {"confirmable", "preflight_ready"} and collection_gate["ready"] is not True:
            next_action = collection_gate.get("next_action") if isinstance(collection_gate.get("next_action"), dict) else {}
            readiness = {
                **readiness,
                "status": "blocked",
                "status_label": (
                    "范围完整但数据已过期"
                    if collection_gate.get("state") == "complete_but_stale"
                    else "计划范围尚未验证"
                ),
                "stage": "collection_qualification",
                "next_step": str(next_action.get("detail") or next_action.get("label") or "重新读取当前计划范围。"),
                "can_enter_preflight": False,
                "blocked_reasons": [
                    *readiness.get("blocked_reasons", []),
                    *collection_gate.get("blockers", []),
                ],
            }
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
                "promotion_guard": promotion_guard,
                "collection_gate": collection_gate,
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
    stale_record_count = 0
    record_ages: list[int] = []
    now_ms = int(time.time() * 1000)
    # Include legacy `campaigns` because v2.5.x misclassified the real video
    # library route as a campaign-management page.
    records = _table_records("qianchuan", {"video_library", "materials", "campaigns"})
    for entry in records:
        captured_at_ms = int(entry.get("captured_at_ms") or 0)
        future_timestamp = captured_at_ms - now_ms > MAX_CAPTURE_FUTURE_SKEW_MS
        data_age_seconds = (
            max(0, int((now_ms - captured_at_ms) / 1000))
            if captured_at_ms > 0 and not future_timestamp
            else None
        )
        if data_age_seconds is None or data_age_seconds >= CREATIVE_ANALYSIS_STALE_SECONDS:
            stale_record_count += 1
            continue
        record_ages.append(data_age_seconds)
        record = entry["record"]
        _, raw_name = _pick(record, ("视频", "素材名称", "创意名称", "视频名称"))
        _, assessment = _pick(record, ("素材评估", "素材状态", "评估"))
        if raw_name is None or not any(keyword in "|".join(record) for keyword in ("视频", "素材评估", "时长", "创作者声明")):
            continue
        name = _clean_entity_name(raw_name, f"第 {entry['row_index'] + 1} 条视频")
        if re.match(r"^共\s*\d+\s*(?:个|条)?(?:视频|素材)", name) or re.fullmatch(r"素材ID[:：]?\s*\[已隐藏\]", name):
            continue
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
        recommendations.append({
            "level": "warning" if stale_record_count else "info",
            "owner": "投放运营",
            "title": "重新同步千川视频库" if stale_record_count else "同步千川视频库",
            "action": "登录巨量千川，打开素材工具中的视频库后点击同步或重新巡查。",
            "acceptance": "视频库出现 24 小时内采集的素材数量、消耗和素材评估。",
            "evidence": "现有素材快照已过期，不能作为今天的测试或停测依据。" if stale_record_count else "当前没有可识别的视频库表格。",
        })
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
        "data_status": "ready" if videos else "stale" if stale_record_count else "missing",
        "data_age_seconds": max(record_ages) if record_ages else None,
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
            "stale_records_ignored": stale_record_count,
        },
        "videos": videos[:30],
        "recommendations": recommendations,
        "test_matrix": test_matrix[:4],
        "memory": memory,
        "governance_policy": {
            "min_spend_for_action": min_spend,
            "roi_target": roi_target,
            "min_orders_for_scale": 3,
            "max_data_age_seconds": CREATIVE_ANALYSIS_STALE_SECONDS,
            "data_window": "current_page_filter",
            "data_window_label": "当前千川页面筛选范围",
            "source_label": "本地 Agent 经营设置与当前素材快照",
            "automatic_delete_enabled": False,
            "platform_write_enabled": False,
        },
        "analysis_method": "素材漏斗：展示 → 点击 → 成交 → ROI；每轮测试只改变一个内容变量。",
        "mode": "read_only",
    }


def _task_evidence_freshness(
    captured_at: Any,
    max_age_seconds: int,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Classify evidence used for a current operating task.

    Historical snapshots remain on disk and available to reports, but a task
    diagnosis may only use an explicit, non-future capture time inside its
    policy window.  Saved-at time is intentionally not a substitute: opening
    or migrating an old snapshot must not make its business evidence current.
    """

    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    try:
        captured_at_ms = int(float(captured_at or 0))
    except (TypeError, ValueError):
        captured_at_ms = 0
    if 0 < captured_at_ms < 10_000_000_000:
        captured_at_ms *= 1000
    if captured_at_ms <= 0:
        return {
            "status": "stale",
            "reason": "missing_capture_time",
            "captured_at_ms": 0,
            "data_age_seconds": None,
        }
    if captured_at_ms > now_ms:
        return {
            "status": "stale",
            "reason": "future_capture_time",
            "captured_at_ms": captured_at_ms,
            "data_age_seconds": None,
        }
    age_seconds = int((now_ms - captured_at_ms) / 1000)
    return {
        "status": "ready" if age_seconds < max_age_seconds else "stale",
        "reason": "fresh" if age_seconds < max_age_seconds else "expired",
        "captured_at_ms": captured_at_ms,
        "data_age_seconds": age_seconds,
    }


def _current_table_records(source: str, page_types: set[str], max_age_seconds: int) -> list[dict[str, Any]]:
    """Return only table rows that are safe to use in today's tasks."""

    return [
        entry
        for entry in _table_records(source, page_types)
        if _task_evidence_freshness(entry.get("captured_at_ms"), max_age_seconds)["status"] == "ready"
    ]


def _commerce_snapshot_contract_state(
    source: str,
    page_types: tuple[str, ...],
    max_age_seconds: int,
) -> dict[str, Any]:
    """Describe whether a stored page can participate in the product chain."""

    candidates: list[dict[str, Any]] = []
    for page_type in page_types:
        stored = load_data(source, page_type) or {}
        data = stored.get("data") if isinstance(stored.get("data"), dict) else {}
        if not data:
            continue
        freshness = _task_evidence_freshness(data.get("captured_at"), max_age_seconds)
        entities = data.get("commerce_entities") if isinstance(data.get("commerce_entities"), list) else []
        relations = data.get("commerce_relations") if isinstance(data.get("commerce_relations"), list) else []
        tables = data.get("tables") if isinstance(data.get("tables"), list) else []
        stable_entity_count = sum(
            1 for item in entities
            if isinstance(item, dict) and item.get("entity_type") in {
                "douyin_product_id", "qianchuan_product_id", "douyin_sku_id", "merchant_product_code",
            }
        )
        plan_entity_count = sum(
            1 for item in entities
            if isinstance(item, dict) and item.get("entity_type") == "qianchuan_plan_id"
        )
        mapping_count = sum(
            1 for item in relations
            if isinstance(item, dict) and item.get("relation") == "promotes"
        )
        entity_row_count = sum(
            len(table.get("entity_rows") or [])
            for table in tables if isinstance(table, dict) and isinstance(table.get("entity_rows"), list)
        )
        normalized_headers = {
            _commerce_header_key(header)
            for table in tables if isinstance(table, dict)
            for header in (table.get("headers") or [])
        }
        candidates.append({
            "source": source,
            "page_type": page_type,
            "captured_at_ms": int(freshness.get("captured_at_ms") or 0),
            "freshness": freshness.get("status"),
            "freshness_reason": freshness.get("reason"),
            "schema_version": int(data.get("schema_version") or 1),
            "commerce_contract_version": int(data.get("commerce_contract_version") or 0),
            "row_count": int((data.get("quality") or {}).get("row_count") or 0) if isinstance(data.get("quality"), dict) else 0,
            "quality_score": int((data.get("quality") or {}).get("score") or 0) if isinstance(data.get("quality"), dict) else 0,
            "stable_entity_count": stable_entity_count,
            "plan_entity_count": plan_entity_count,
            "mapping_count": mapping_count,
            "entity_row_count": entity_row_count,
            "has_inventory_metric": any(key in normalized_headers for key in {"可售库存", "库存", "库存数量", "总库存", "stock"}),
        })
    if not candidates:
        return {"status": "missing", "page_types": list(page_types), "captured_at_ms": 0, "row_count": 0}
    latest = max(candidates, key=lambda item: int(item.get("captured_at_ms") or 0))
    if latest["freshness"] != "ready":
        status = "stale"
    elif latest["schema_version"] < 3:
        status = "recapture_required"
    elif latest["commerce_contract_version"] < 1 or latest["stable_entity_count"] <= 0:
        status = "identity_missing"
    else:
        status = "ready"
    return {**latest, "status": status, "page_types": list(page_types)}


def _commerce_collection_readiness(graph: dict[str, Any]) -> dict[str, Any]:
    settings = load_agent_settings()
    store_key = str(settings.get("store_key") or "").lower()
    account_key = str(settings.get("qianchuan_account_key") or "").lower()
    product_state = _commerce_snapshot_contract_state("doudian", ("products",), INVENTORY_ANALYSIS_STALE_SECONDS)
    inventory_state = _commerce_snapshot_contract_state("doudian", ("inventory", "products"), INVENTORY_ANALYSIS_STALE_SECONDS)
    ads_state = _commerce_snapshot_contract_state("qianchuan", ("campaigns", "qianchuan_campaigns", "plans"), PLAN_CONSOLE_STALE_SECONDS) if account_key else {"status": "optional", "captured_at_ms": 0}

    steps = [
        {
            "key": "store",
            "label": "当前店铺",
            "status": "ready" if store_key else "blocked",
            "detail": "已按当前店铺隔离数据" if store_key else "先识别并选择当前抖店",
            "required": True,
        },
        {
            "key": "product_identity",
            "label": "商品身份",
            "status": "ready" if product_state.get("status") == "ready" else product_state.get("status"),
            "detail": "商品 ID 已安全匿名化" if product_state.get("status") == "ready" else "需要重新读取商品列表中的商品 ID",
            "required": True,
            "evidence": product_state,
        },
        {
            "key": "inventory",
            "label": "库存数据",
            "status": "ready" if inventory_state.get("status") == "ready" and inventory_state.get("has_inventory_metric") else "metric_missing" if inventory_state.get("status") == "ready" else inventory_state.get("status"),
            "detail": "已取得同一商品库存" if inventory_state.get("status") == "ready" and inventory_state.get("has_inventory_metric") else "需要刷新库存页并确认库存列",
            "required": True,
            "evidence": inventory_state,
        },
        {
            "key": "ads_mapping",
            "label": "千川商品映射",
            "status": "optional" if not account_key else "ready" if ads_state.get("status") == "ready" and int(ads_state.get("plan_entity_count") or 0) > 0 and int(ads_state.get("mapping_count") or 0) > 0 else "mapping_missing" if ads_state.get("status") == "ready" else ads_state.get("status"),
            "detail": "已取得计划 ID—商品精确关系" if account_key and ads_state.get("status") == "ready" and int(ads_state.get("mapping_count") or 0) > 0 else "未连接千川也可先完成商品经营诊断" if not account_key else "需要刷新千川商品计划页",
            "required": False,
            "evidence": ads_state,
        },
    ]
    required_ready = all(item["status"] == "ready" for item in steps if item["required"])
    ads_ready = steps[-1]["status"] == "ready"
    if not store_key:
        status = "store_required"
        next_action = {"kind": "navigate", "target_id": "connection-guide", "label": "先选择当前店铺", "detail": "确认店铺后才会开始采集，避免跨店混数。"}
    elif steps[1]["status"] != "ready" or steps[2]["status"] != "ready":
        status = "recapture_required" if "recapture_required" in {steps[1]["status"], steps[2]["status"]} else "product_data_required"
        next_action = {"kind": "targeted_scan", "page_ids": ["products", "inventory", "shelf"], "label": "刷新商品与库存（约 1 分钟）", "detail": "只读取建立单品链所需的 3 个页面，不运行整店 18 页巡检。"}
    elif not account_key:
        status = "base_ready"
        next_action = {"kind": "navigate", "target_id": "connection-guide", "label": "连接千川商品计划（可稍后）", "detail": "基础商品链已可使用；连接千川后再补投放归因。", "optional": True}
    elif not ads_ready:
        status = "ads_mapping_required"
        next_action = {"kind": "targeted_scan", "page_ids": ["qianchuan_campaigns"], "label": "刷新千川商品计划", "detail": "只读取当前已绑定千川账户的商品计划页。"}
    else:
        status = "ready"
        next_action = {"kind": "targeted_scan", "page_ids": ["products", "inventory", "qianchuan_campaigns"], "label": "更新单品经营链", "detail": "重新读取商品、库存和千川商品计划。"}
    return {
        "status": status,
        "base_ready": required_ready,
        "cross_channel_ready": required_ready and ads_ready,
        "steps": steps,
        "next_action": next_action,
        "policy": "千川映射可以稍后补齐，但缺商品身份或库存时不会生成自动放量候选。",
    }


def build_douyin_product_graph() -> dict[str, Any]:
    """Join fresh product-scoped evidence across Douyin commerce surfaces.

    Page types use their own freshness window.  The graph never falls back to
    product-name matching, and duplicate table evidence is collapsed before
    normalization.
    """

    record_groups = (
        ("doudian", {"shelf", "overview"}, SHELF_ANALYSIS_STALE_SECONDS),
        ("doudian", {"products", "inventory", "orders", "refunds", "reviews"}, INVENTORY_ANALYSIS_STALE_SECONDS),
        ("doudian", {"live", "qianchuan_live"}, LIVE_ANALYSIS_STALE_SECONDS),
        ("doudian", {"short_video", "image_text", "recommend_card"}, CREATIVE_ANALYSIS_STALE_SECONDS),
        ("qianchuan", {"campaigns", "plans", "report", "qianchuan_campaigns", "qianchuan_report", "overview"}, PLAN_CONSOLE_STALE_SECONDS),
        ("qianchuan", {"qianchuan_live", "live_dashboard"}, LIVE_ANALYSIS_STALE_SECONDS),
        ("qianchuan", {"video_library", "materials"}, CREATIVE_ANALYSIS_STALE_SECONDS),
    )
    records: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for source, page_types, max_age_seconds in record_groups:
        for entry in _current_table_records(source, page_types, max_age_seconds):
            signature = (
                entry.get("source"), entry.get("page_type"), entry.get("account_key"),
                entry.get("captured_at_ms"), entry.get("table_index"), entry.get("row_index"),
            )
            if signature in seen:
                continue
            seen.add(signature)
            records.append(entry)
    settings = load_agent_settings()
    graph = build_product_operating_graph(
        records,
        store_key=str(settings.get("store_key") or ""),
        settings=settings,
    )
    graph["collection_readiness"] = _commerce_collection_readiness(graph)
    scope = str(graph.get("store_key") or "")
    entity_keys = [
        str(product.get("product_id") or product.get("sku_id") or product.get("merchant_code") or "")
        for product in graph.get("products", []) if isinstance(product, dict)
    ]
    try:
        memory = _local_store().commerce_memory(
            store_key=scope,
            entity_keys=entity_keys,
            account_key=str(settings.get("qianchuan_account_key") or "") or None,
        )
    except (LocalStoreError, OSError):
        logger.exception("读取单品经营记忆失败")
        memory = {"status": "error", "history_days": 0, "observation_count": 0, "entities": {}}
    graph["memory"] = {key: value for key, value in memory.items() if key != "entities"}
    memory_by_entity = memory.get("entities") if isinstance(memory.get("entities"), dict) else {}
    for product, entity_key in zip(graph.get("products", []), entity_keys):
        if isinstance(product, dict):
            product["memory"] = memory_by_entity.get(entity_key, {
                "history_days": 0,
                "observation_count": 0,
                "metric_count": 0,
                "latest_metrics": {},
                "latest_metric_series": {},
            })
    graph["summary"]["memory_days"] = int(memory.get("history_days") or 0)
    graph["summary"]["memory_observations"] = int(memory.get("observation_count") or 0)
    return graph


def _inventory_task_state() -> dict[str, Any]:
    """Describe inventory evidence without exposing stale values as alerts."""

    candidates: list[dict[str, Any]] = []
    for item in list_snapshots():
        if item.get("source") != "doudian" or item.get("page_type") not in {"inventory", "products"}:
            continue
        snapshot = load_data("doudian", str(item.get("page_type") or "")) or {}
        data = snapshot.get("data") if isinstance(snapshot.get("data"), dict) else {}
        freshness = _task_evidence_freshness(
            data.get("captured_at"),
            INVENTORY_ANALYSIS_STALE_SECONDS,
        )
        candidates.append({**freshness, "page_type": item.get("page_type")})
    ready = [item for item in candidates if item["status"] == "ready"]
    if ready:
        newest = min(ready, key=lambda item: item.get("data_age_seconds") or 0)
        return {**newest, "status": "ready"}
    if candidates:
        return {**candidates[0], "status": "stale"}
    return {"status": "missing", "reason": "missing_snapshot", "captured_at_ms": 0, "data_age_seconds": None}


def build_inventory_alerts(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    settings = settings or load_agent_settings()
    low = int(settings["low_inventory_threshold"])
    critical = int(settings["critical_inventory_threshold"])
    days_warning = float(settings["inventory_days_warning"])
    results: list[dict[str, Any]] = []

    for entry in _current_table_records(
        "doudian",
        {"inventory", "products"},
        INVENTORY_ANALYSIS_STALE_SECONDS,
    ):
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


def _safe_snapshot_metrics(
    source: str,
    page_types: set[str],
    max_age_seconds: int,
) -> tuple[dict[str, Any], list[str], dict[str, Any] | None]:
    metrics: dict[str, Any] = {}
    signals: list[str] = []
    newest_ready: dict[str, Any] | None = None
    newest_stale: dict[str, Any] | None = None
    for item in list_snapshots():
        if item["source"] != source or item["page_type"] not in page_types:
            continue
        data = (load_data(source, item["page_type"]) or {}).get("data", {})
        freshness = _task_evidence_freshness(data.get("captured_at"), max_age_seconds)
        candidate = {
            **item,
            **freshness,
            "captured_at": freshness.get("captured_at_ms"),
        }
        if freshness["status"] != "ready":
            if newest_stale is None:
                newest_stale = candidate
            continue
        for key, value in (data.get("safe_metrics") or {}).items():
            metrics[str(key)] = value
        for signal in data.get("signals") or []:
            if signal not in signals:
                signals.append(str(signal))
        if newest_ready is None or int(candidate.get("data_age_seconds") or 0) < int(newest_ready.get("data_age_seconds") or 0):
            newest_ready = candidate
    return metrics, signals, newest_ready or newest_stale


def build_shelf_analysis() -> dict[str, Any]:
    metrics, signals, snapshot = _safe_snapshot_metrics(
        "doudian",
        {"shelf"},
        SHELF_ANALYSIS_STALE_SECONDS,
    )
    data_status = str((snapshot or {}).get("status") or "missing")
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
    if data_status != "ready":
        stale = data_status == "stale"
        actions = [{
            "level": "warning" if stale else "info",
            "owner": "货架运营",
            "title": "重新同步货架数据" if stale else "同步货架数据",
            "action": "打开商城运营概览并重新同步；旧快照只保留用于历史复盘。",
            "acceptance": "出现 30 分钟内采集的曝光、点击与成交漏斗。",
            "evidence": "现有货架快照已过期或采集时间异常，不能用于今日诊断。" if stale else "尚无货架页面快照。",
            "confidence": "high",
        }]
    return {"generated_at": _now_label(), "data_status": data_status, "snapshot": snapshot, "metrics": metrics, "funnel": {"exposure": exposure, "clicks": clicks, "buyers": buyers, "orders": orders, "payment": payment, "click_rate": click_rate}, "signals": signals, "recommendations": actions, "mode": "read_only", "governance_policy": {"max_data_age_seconds": SHELF_ANALYSIS_STALE_SECONDS}}


LIVE_PACING_ALIASES: dict[str, tuple[str, ...]] = {
    "spend": ("整体消耗(元)", "整体消耗", "广告消耗", "消耗", "花费"),
    "gmv": ("整体成交金额(元)", "整体成交金额", "净成交金额", "成交金额", "用户支付金额"),
    "orders": ("整体成交订单数", "净成交订单数", "直播间成交订单数", "成交订单数", "订单数"),
    "impressions": ("整体展现次数", "展示次数", "曝光次数", "曝光"),
    "views": ("进入直播间人数", "直播间观看人数", "观看次数", "进房人数"),
    "product_clicks": ("直播间商品点击人数", "商品点击人数", "商品点击次数", "商品点击"),
    "roi": ("整体支付ROI", "整体成交支付ROI", "净成交ROI", "支付ROI", "ROI"),
}
LIVE_PACING_CUMULATIVE_FIELDS = ("spend", "gmv", "orders", "impressions", "views", "product_clicks")


def _live_pacing_metric(metrics: dict[str, Any], aliases: tuple[str, ...]) -> float | None:
    for alias in aliases:
        value = _parse_number(metrics.get(alias))
        if value is not None:
            return value
    normalized_aliases = tuple(re.sub(r"\s+", "", alias).lower() for alias in aliases)
    for label, raw_value in metrics.items():
        normalized_label = re.sub(r"\s+", "", str(label)).lower()
        if any(alias in normalized_label for alias in normalized_aliases):
            value = _parse_number(raw_value)
            if value is not None:
                return value
    return None


def _live_pacing_point(point: dict[str, Any]) -> dict[str, Any] | None:
    captured_at = _parse_number(point.get("captured_at"))
    if captured_at is None or captured_at <= 0:
        return None
    if captured_at < 10_000_000_000:
        captured_at *= 1000
    metrics = point.get("safe_metrics") or (point.get("data") or {}).get("safe_metrics") or {}
    if not isinstance(metrics, dict):
        return None
    values = {key: _live_pacing_metric(metrics, aliases) for key, aliases in LIVE_PACING_ALIASES.items()}
    if all(value is None for value in values.values()):
        return None
    return {
        "captured_at": int(captured_at),
        "source": str(point.get("source") or ""),
        "page_type": str(point.get("page_type") or ""),
        "values": values,
    }


def _live_pacing_age_label(age_seconds: int | None) -> str:
    if age_seconds is None:
        return "等待数据"
    if age_seconds < 60:
        return "刚刚更新"
    if age_seconds < 3600:
        return f"{age_seconds // 60} 分钟前"
    return f"{age_seconds // 3600} 小时前"


def build_live_pacing_analysis(
    history_points: list[dict[str, Any]] | None = None,
    latest_funnel: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
    *,
    latest_captured_at: int | float | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Build a fail-closed, current-session live pacing readout from cumulative snapshots."""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    settings = settings or load_agent_settings()
    latest_funnel = latest_funnel or {}
    if history_points is None:
        def load_live_history() -> list[dict[str, Any]]:
            loaded: list[dict[str, Any]] = []
            for source, page_type in (
                ("qianchuan", "live_dashboard"),
                ("qianchuan", "qianchuan_live"),
                ("doudian", "qianchuan_live"),
                ("doudian", "live"),
            ):
                loaded.extend(load_history(source, page_type, 1))
            return loaded

        history_points = _cached("live_pacing_history", load_live_history)

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for raw_point in history_points:
        normalized = _live_pacing_point(raw_point)
        if normalized:
            groups.setdefault((normalized["source"], normalized["page_type"]), []).append(normalized)
    for points in groups.values():
        points.sort(key=lambda item: item["captured_at"])

    def group_score(item: tuple[tuple[str, str], list[dict[str, Any]]]) -> tuple[int, int, int, int, int]:
        (source, _page_type), points = item
        latest = points[-1]
        latest_values = latest["values"]
        future_timestamp = latest["captured_at"] > now_ms
        age_ms = now_ms - latest["captured_at"]
        freshness = 0 if future_timestamp else 2 if age_ms <= 5 * 60 * 1000 else 1 if age_ms <= 15 * 60 * 1000 else 0
        core_count = sum(latest_values.get(key) is not None for key in ("spend", "orders", "roi"))
        return freshness, core_count, 1 if len(points) >= 2 else 0, latest["captured_at"], 1 if source == "qianchuan" else 0

    selected_key: tuple[str, str] | None = None
    points: list[dict[str, Any]] = []
    if groups:
        selected_key, points = max(groups.items(), key=group_score)

    funnel_values = {key: _parse_number(latest_funnel.get(key)) for key in LIVE_PACING_ALIASES}
    snapshot_time = _parse_number(latest_captured_at)
    if snapshot_time is not None and snapshot_time < 10_000_000_000:
        snapshot_time *= 1000
    if not points and any(value is not None for value in funnel_values.values()):
        points = [{
            "captured_at": int(snapshot_time or now_ms),
            "source": "current_snapshot",
            "page_type": "live",
            "values": funnel_values.copy(),
        }]
        selected_key = ("current_snapshot", "live")
    elif points:
        for key, value in funnel_values.items():
            if value is not None and points[-1]["values"].get(key) is None:
                points[-1]["values"][key] = value

    session_points: list[dict[str, Any]] = []
    session_reset_detected = False
    for point in points:
        if session_points:
            previous = session_points[-1]
            gap_ms = point["captured_at"] - previous["captured_at"]
            decreased = any(
                previous["values"].get(field) is not None
                and point["values"].get(field) is not None
                and point["values"][field] + 1e-9 < previous["values"][field]
                for field in LIVE_PACING_CUMULATIVE_FIELDS
            )
            if decreased or gap_ms > 12 * 60 * 60 * 1000:
                session_points = []
                session_reset_detected = True
        session_points.append(point)

    intervals: list[dict[str, Any]] = []
    for previous, current in zip(session_points, session_points[1:]):
        duration_minutes = (current["captured_at"] - previous["captured_at"]) / 60_000
        if duration_minutes < 1 / 6 or duration_minutes > 360:
            continue
        deltas: dict[str, float | None] = {}
        for field in LIVE_PACING_CUMULATIVE_FIELDS:
            before = previous["values"].get(field)
            after = current["values"].get(field)
            deltas[field] = round(after - before, 4) if before is not None and after is not None and after >= before else None
        hours = duration_minutes / 60
        intervals.append({
            "start_at": previous["captured_at"],
            "end_at": current["captured_at"],
            "start_label": datetime.fromtimestamp(previous["captured_at"] / 1000).strftime("%H:%M"),
            "end_label": datetime.fromtimestamp(current["captured_at"] / 1000).strftime("%H:%M"),
            "duration_minutes": round(duration_minutes, 1),
            "spend_delta": deltas["spend"],
            "gmv_delta": deltas["gmv"],
            "orders_delta": deltas["orders"],
            "views_delta": deltas["views"],
            "product_clicks_delta": deltas["product_clicks"],
            "spend_per_hour": round(deltas["spend"] / hours, 2) if deltas["spend"] is not None else None,
            "gmv_per_hour": round(deltas["gmv"] / hours, 2) if deltas["gmv"] is not None else None,
            "orders_per_hour": round(deltas["orders"] / hours, 2) if deltas["orders"] is not None else None,
            "interval_roi": round(deltas["gmv"] / deltas["spend"], 3) if deltas["gmv"] is not None and deltas["spend"] and deltas["spend"] > 0 else None,
        })

    latest_point = session_points[-1] if session_points else None
    latest_values = latest_point["values"].copy() if latest_point else funnel_values.copy()
    for key, value in funnel_values.items():
        if latest_values.get(key) is None and value is not None:
            latest_values[key] = value
    if latest_values.get("roi") is None and latest_values.get("gmv") is not None and latest_values.get("spend") and latest_values["spend"] > 0:
        latest_values["roi"] = round(latest_values["gmv"] / latest_values["spend"], 3)

    latest_at = latest_point["captured_at"] if latest_point else int(snapshot_time or 0) or None
    future_timestamp = bool(latest_at and latest_at > now_ms)
    age_seconds = int((now_ms - latest_at) / 1000) if latest_at and not future_timestamp else None
    freshness = "stale" if future_timestamp else "fresh" if age_seconds is not None and age_seconds <= 300 else "delayed" if age_seconds is not None and age_seconds <= 900 else "stale" if age_seconds is not None else "missing"
    latest_interval = intervals[-1] if intervals else None
    previous_interval = intervals[-2] if len(intervals) >= 2 else None
    pace_state = "unknown"
    pace_label = "速度待积累"
    if latest_interval and latest_interval["spend_per_hour"] is not None and previous_interval and previous_interval["spend_per_hour"] is not None:
        previous_rate = previous_interval["spend_per_hour"]
        current_rate = latest_interval["spend_per_hour"]
        if previous_rate <= 0 < current_rate:
            pace_state, pace_label = "surging", "消耗开始加速"
        elif previous_rate > 0:
            rate_change = (current_rate - previous_rate) / previous_rate
            if rate_change >= 0.25:
                pace_state, pace_label = "surging", "消耗明显加速"
            elif rate_change <= -0.25:
                pace_state, pace_label = "slowing", "消耗明显放缓"
            else:
                pace_state, pace_label = "stable", "消耗速度稳定"
    elif latest_interval and latest_interval["spend_per_hour"] is not None:
        pace_state, pace_label = "measuring", "已取得首个速度窗口"

    target_roi = _parse_number(settings.get("roi_target"))
    min_spend = _parse_number(settings.get("min_spend_for_action"))
    blockers: list[str] = []
    if not latest_point:
        blockers.append("尚无直播快照")
    if freshness == "stale":
        blockers.append("直播采集时间异常或数据已超过 15 分钟，不能据此调整节奏")
    if not intervals:
        blockers.append("至少需要两个同场次快照才能计算消耗与成交速度")
    if latest_interval and latest_interval["spend_delta"] is None:
        blockers.append("当前直播快照缺少可比较的消耗字段")
    if latest_interval and latest_interval["orders_delta"] is None:
        blockers.append("当前直播快照缺少可比较的成交订单字段")
    if target_roi is None or target_roi <= 0:
        blockers.append("尚未配置本店 ROI 目标")
    if min_spend is None or min_spend <= 0:
        blockers.append("尚未配置动作消耗门槛")

    decision = {
        "code": "collect_samples",
        "level": "info",
        "title": "先积累同场次速度样本",
        "action": "保持当前投放设置，间隔同步直播大屏；至少取得两个同场次快照后再判断节奏。",
        "evidence": "当前无法计算可靠的增量窗口。",
    }
    if freshness == "stale":
        decision = {
            "code": "sync_stale_data", "level": "warning", "title": "先同步直播大屏",
            "action": "重新同步当前直播大屏；旧数据只用于复盘，不用于预算或状态候选。",
            "evidence": "直播快照的采集时间晚于当前时间。" if future_timestamp else f"最近数据为{_live_pacing_age_label(age_seconds)}。",
        }
    elif latest_interval and latest_interval["spend_delta"] is not None and latest_interval["orders_delta"] is not None:
        spend_delta = latest_interval["spend_delta"]
        orders_delta = latest_interval["orders_delta"]
        interval_roi = latest_interval["interval_roi"] if latest_interval["interval_roi"] is not None else latest_values.get("roi")
        if min_spend and spend_delta >= min_spend and orders_delta == 0 and interval_roi is not None and target_roi and interval_roi < target_roi:
            decision = {
                "code": "hold_spend_review", "level": "high", "title": "消耗增长但没有新增成交",
                "action": "先停止追加消耗，复核进房与商品承接；这里只生成停测或降预算候选，不直接写入千川。",
                "evidence": f"最近 {latest_interval['duration_minutes']:g} 分钟消耗增加 {spend_delta:g} 元、新增订单 0、ROI {interval_roi:g}，低于目标 {target_roi:g}。",
            }
        elif latest_interval["views_delta"] and latest_interval["product_clicks_delta"] == 0:
            decision = {
                "code": "fix_product_click", "level": "warning", "title": "有人进房但没有新增商品点击",
                "action": "保持预算不变，先调整讲解顺序、利益点和购物车引导，再观察下一个窗口。",
                "evidence": f"最近窗口新增进房 {latest_interval['views_delta']:g}，新增商品点击 0。",
            }
        elif latest_interval["product_clicks_delta"] and orders_delta == 0:
            decision = {
                "code": "fix_conversion", "level": "warning", "title": "商品有点击但没有新增成交",
                "action": "保持预算不变，检查价格权益、库存规格、信任证明和逼单节奏。",
                "evidence": f"最近窗口新增商品点击 {latest_interval['product_clicks_delta']:g}，新增订单 0。",
            }
        elif orders_delta > 0 and interval_roi is not None and target_roi and interval_roi >= target_roi:
            decision = {
                "code": "maintain_pace", "level": "safe", "title": "当前节奏有效，先维持",
                "action": "维持当前主要变量，等待完整观察窗口；满足生产授权与止损边界后才生成受控放量候选。",
                "evidence": f"最近窗口新增订单 {orders_delta:g}，ROI {interval_roi:g}，达到目标 {target_roi:g}。",
            }
        else:
            decision = {
                "code": "observe_next_window", "level": "info", "title": "样本已形成，继续观察一个窗口",
                "action": "暂不叠加新的预算、时长或状态动作，保持单变量观察。",
                "evidence": f"最近窗口消耗增加 {spend_delta:g} 元、新增订单 {orders_delta:g}。",
            }

    source_labels = {
        ("qianchuan", "live_dashboard"): "千川直播大屏",
        ("qianchuan", "qianchuan_live"): "千川直播计划页",
        ("doudian", "qianchuan_live"): "抖店千川直播页",
        ("doudian", "live"): "抖店直播页",
        ("current_snapshot", "live"): "当前直播快照",
    }
    status = "missing" if not latest_point else "stale" if freshness == "stale" else "warming" if not intervals else "blocked" if blockers else "ready"
    return {
        "schema_version": 1,
        "status": status,
        "status_label": {"missing": "等待直播数据", "stale": "数据已过期", "warming": "正在积累速度样本", "blocked": "数据口径待补齐", "ready": "直播节奏可判断"}[status],
        "source_label": source_labels.get(selected_key or ("", ""), "直播快照"),
        "window_label": "本场开播至当前 · 最近增量窗口",
        "freshness": freshness,
        "age_seconds": age_seconds,
        "age_label": _live_pacing_age_label(age_seconds),
        "session_point_count": len(session_points),
        "session_reset_detected": session_reset_detected,
        "pace_state": pace_state,
        "pace_label": pace_label,
        "latest_values": latest_values,
        "latest_interval": latest_interval,
        "recent_intervals": intervals[-3:],
        "target_roi": target_roi,
        "min_spend_for_action": min_spend,
        "blockers": blockers,
        "decision": decision,
        "refresh_contract": {
            "default": "manual",
            "allowed_seconds": [30, 60],
            "local_snapshot_only": True,
            "browser_page_capture_requires_explicit_click": True,
        },
        "platform_write_enabled": False,
        "automatic_status_change_enabled": False,
        "mode": "read_only",
    }


def build_live_analysis() -> dict[str, Any]:
    metrics, signals, snapshot = _safe_snapshot_metrics(
        "doudian",
        {"live", "qianchuan_live"},
        LIVE_ANALYSIS_STALE_SECONDS,
    )
    q_metrics, q_signals, q_snapshot = _safe_snapshot_metrics(
        "qianchuan",
        {"qianchuan_live", "live_dashboard"},
        LIVE_ANALYSIS_STALE_SECONDS,
    )
    metrics.update({key: value for key, value in q_metrics.items() if key not in metrics})
    signals.extend(signal for signal in q_signals if signal not in signals)
    live_records = _current_table_records(
        "qianchuan",
        {"qianchuan_live", "live_dashboard"},
        LIVE_ANALYSIS_STALE_SECONDS,
    )
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
    ready_snapshots = [item for item in (q_snapshot, snapshot) if (item or {}).get("status") == "ready"]
    has_snapshot = bool(snapshot or q_snapshot)
    data_status = "ready" if ready_snapshots else "stale" if has_snapshot else "missing"
    if data_status != "ready":
        stale = data_status == "stale"
        actions = [{
            "level": "warning" if stale else "info",
            "owner": "直播运营",
            "title": "重新同步直播大屏" if stale else "同步直播大屏",
            "action": "打开店铺直播或千川直播大屏并重新同步；旧快照只保留用于历史复盘。",
            "acceptance": "出现 30 分钟内采集的直播观看、商品点击、成交与消耗指标。",
            "evidence": "现有直播快照已过期或采集时间异常，不能用于今日诊断。" if stale else "尚无直播快照。",
            "confidence": "high",
        }]
    if refund_rate is not None and refund_rate >= 20:
        actions.append({"level": "warning", "owner": "直播运营", "title": "成交后退款偏高", "action": "核对主播承诺、商品预期、尺码说明和售后原因，避免素材与直播间过度承诺。", "acceptance": "退款率回落并且净成交 ROI 改善。", "evidence": f"当前退款率 {refund_rate:g}%。"})
    funnel = {"sessions": sessions, "impressions": impressions, "views": views, "enter_rate": enter_rate, "product_clicks": product_clicks, "product_click_rate": product_click_rate, "orders": orders, "conversion_rate": conversion_rate, "gmv": gmv, "spend": spend, "roi": roi, "refund_rate": refund_rate}
    latest_snapshot = min(
        ready_snapshots,
        key=lambda item: int(item.get("data_age_seconds") or 0),
    ) if ready_snapshots else q_snapshot or snapshot or {}
    pacing = build_live_pacing_analysis(
        latest_funnel=funnel,
        settings=load_agent_settings(),
        latest_captured_at=latest_snapshot.get("captured_at"),
    )
    return {"generated_at": _now_label(), "data_status": data_status, "snapshot": latest_snapshot or None, "metrics": metrics, "funnel": funnel, "pacing": pacing, "signals": signals, "recommendations": actions, "mode": "read_only", "governance_policy": {"max_data_age_seconds": LIVE_ANALYSIS_STALE_SECONDS}}


_TASK_CONTRACT_PROFILES: dict[str, dict[str, Any]] = {
    "product_graph": {
        "source": "mixed",
        "sources": ["doudian", "qianchuan"],
        "page_types": ["products", "inventory", "shelf", "live", "campaigns", "qianchuan_live", "materials", "refunds"],
        "max_age_seconds": INVENTORY_ANALYSIS_STALE_SECONDS,
        "workspace_key": "shelf-products",
        "target_id": "product-operating-graph",
        "platform": "doudian",
        "subject_kind": "product",
        "metric_keywords": ["库存", "点击率", "转化率", "roi", "退款率"],
    },
    "shelf": {
        "source": "doudian",
        "page_types": ["shelf", "mall", "overview"],
        "max_age_seconds": SHELF_ANALYSIS_STALE_SECONDS,
        "workspace_key": "shelf-products",
        "target_id": "shelf-actions",
        "platform": "doudian",
        "subject_kind": "shelf",
        "metric_keywords": ["点击率", "转化率"],
    },
    "live": {
        "source": "mixed",
        "sources": ["doudian", "qianchuan"],
        "page_types": ["live", "live_dashboard", "qianchuan_live"],
        "max_age_seconds": LIVE_ANALYSIS_STALE_SECONDS,
        "workspace_key": "live-operations",
        "target_id": "live-actions",
        "platform": "doudian",
        "subject_kind": "live_room",
        "metric_keywords": ["进房率", "点击率", "转化率", "roi", "退款率"],
    },
    "plans": {
        "source": "qianchuan",
        "sources": ["qianchuan", "doudian"],
        "page_types": ["campaigns", "plans", "report", "qianchuan_campaigns", "qianchuan_live", "qianchuan_report"],
        "max_age_seconds": PLAN_CONSOLE_STALE_SECONDS,
        "workspace_key": "promotion-overview",
        "target_id": "promotion-plan-center",
        "platform": "qianchuan",
        "subject_kind": "plan",
        "metric_keywords": ["roi", "点击率", "转化率"],
    },
    "creative": {
        "source": "qianchuan",
        "page_types": ["video_library", "materials", "campaigns"],
        "max_age_seconds": CREATIVE_ANALYSIS_STALE_SECONDS,
        "workspace_key": "content-operations",
        "target_id": "creative-actions",
        "platform": "qianchuan",
        "subject_kind": "creative",
        "metric_keywords": ["roi", "点击率", "转化率"],
    },
    "inventory": {
        "source": "doudian",
        "page_types": ["inventory", "products"],
        "max_age_seconds": INVENTORY_ANALYSIS_STALE_SECONDS,
        "workspace_key": "shelf-products",
        "target_id": "inventory",
        "platform": "doudian",
        "subject_kind": "product",
        "metric_keywords": ["库存", "可售", "销量"],
    },
}


def _task_contract_module(item: dict[str, Any]) -> str:
    action_params = item.get("action_params") if isinstance(item.get("action_params"), dict) else {}
    target_ref = action_params.get("target_ref") if isinstance(action_params.get("target_ref"), dict) else {}
    if target_ref.get("kind") in {"douyin_product", "douyin_sku"}:
        return "product_graph"
    if target_ref.get("kind") == "qianchuan_plan" or item.get("owner") == "投放运营":
        return "plans"
    context = " ".join(str(item.get(key) or "") for key in ("title", "action", "suggestion", "evidence"))
    if re.search(r"库存|补货|断货|可售", context):
        return "inventory"
    if re.search(r"素材|视频|创意|脚本|钩子|口播|封面", context):
        return "creative"
    if re.search(r"直播|进房|场次|开播", context):
        return "live"
    if re.search(r"投放|千川|计划|ROI|消耗|预算|出价", context, re.IGNORECASE):
        return "plans"
    return "shelf"


def _task_contract_source_index() -> list[dict[str, Any]]:
    index: list[dict[str, Any]] = []
    for snapshot in list_snapshots():
        source = str(snapshot.get("source") or "")
        page_type = str(snapshot.get("page_type") or "")
        stored = load_data(source, page_type) or {}
        data = stored.get("data") if isinstance(stored.get("data"), dict) else {}
        # Evidence time, never local save/open time, controls task eligibility.
        captured_at_ms = int(_task_evidence_freshness(
            data.get("captured_at"),
            max(profile.get("max_age_seconds") or 0 for profile in _TASK_CONTRACT_PROFILES.values()),
        ).get("captured_at_ms") or 0)
        quality = data.get("quality") if isinstance(data.get("quality"), dict) else {}
        index.append({
            "source": source,
            "page_type": page_type,
            "captured_at_ms": captured_at_ms,
            "quality_score": int(quality.get("score") or snapshot.get("quality_score") or 0),
        })
    return index


def _task_rule_id(item: dict[str, Any], module: str) -> str:
    title = str(item.get("title") or "")
    action_params = item.get("action_params") if isinstance(item.get("action_params"), dict) else {}
    operation_type = str(
        action_params.get("operation_type") or item.get("action_type") or ""
    ).strip().lower()
    if "同步" in title:
        return f"system.sync.{module}"
    if operation_type:
        prefix = "qianchuan.plan" if module == "plans" else f"ops.{module}"
        return f"{prefix}.{re.sub(r'[^a-z0-9_]+', '_', operation_type)[:48]}"
    suffix = hashlib.sha256(title.encode("utf-8")).hexdigest()[:10]
    return f"ops.{module}.diagnosis.{suffix}"


def _task_subject(item: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    action_params = item.get("action_params") if isinstance(item.get("action_params"), dict) else {}
    target_ref = action_params.get("target_ref") if isinstance(action_params.get("target_ref"), dict) else {}
    name = str(target_ref.get("name") or action_params.get("target") or item.get("title") or "")[:160]
    plan_key = str(item.get("plan_key") or "").strip().lower()
    if profile.get("subject_kind") == "plan" and re.fullmatch(r"[a-f0-9]{20}", plan_key):
        # The opaque key includes account, promotion mode, plan type and the
        # platform plan id.  A bare platform id can collide across surfaces.
        identifier = plan_key
        identifier_source = "local_plan_identity"
    else:
        identifier = str(target_ref.get("id") or "")[:96]
        if identifier:
            identifier_source = "platform"
        elif profile.get("subject_kind") == "plan":
            # A display name is neither unique nor stable enough to identify a
            # mutable plan task.  Keep malformed rows separately visible with
            # a non-actionable row identity; the contract gate below prevents
            # this diagnostic key from ever becoming an execution identity.
            row_seed = "|".join((
                "unverified-plan-row",
                str(item.get("source") or "")[:32],
                str(item.get("id") or "")[:128],
                str(item.get("account_key") or "").strip().lower()[:128],
                str(item.get("promotion_mode") or "unknown").strip().lower()[:32],
                str(item.get("plan_type") or "unknown").strip().lower()[:24],
            ))
            identifier = hashlib.sha256(row_seed.encode("utf-8")).hexdigest()[:20]
            identifier_source = "local_unverified_plan_row"
        else:
            identifier = hashlib.sha256(
                f"{profile['subject_kind']}|{name}".encode("utf-8")
            ).hexdigest()[:20]
            identifier_source = "derived"
    return {
        "kind": str(target_ref.get("kind") or profile["subject_kind"]),
        "id": identifier,
        "id_source": identifier_source,
        "name": name,
    }


def _task_source_refs(
    item: dict[str, Any],
    profile: dict[str, Any],
    source_index: list[dict[str, Any]],
    *,
    store_key: str,
    account_key: str,
    now_ms: int,
) -> list[dict[str, Any]]:
    action_params = item.get("action_params") if isinstance(item.get("action_params"), dict) else {}
    action_evidence = action_params.get("evidence_ref") if isinstance(action_params.get("evidence_ref"), dict) else {}
    candidates: list[dict[str, Any]] = []
    if action_evidence.get("source") and action_evidence.get("page_type"):
        candidates.append({
            "source": str(action_evidence.get("source") or ""),
            "page_type": str(action_evidence.get("page_type") or ""),
            "captured_at_ms": int(action_evidence.get("captured_at_ms") or 0),
            "quality_score": int(action_evidence.get("quality_score") or 0),
        })
    else:
        # A plan/creative action may carry an exact evidence reference.  Do not
        # replace it with a newer but unrelated page from the same module: the
        # contract must remain bound to the object that produced the action.
        allowed_sources = set(profile.get("sources") or [profile.get("source")])
        allowed_pages = set(profile.get("page_types") or [])
        candidates.extend(
            entry for entry in source_index
            if entry.get("source") in allowed_sources and entry.get("page_type") in allowed_pages
        )
    if not candidates:
        return []
    newest = max(candidates, key=lambda entry: int(entry.get("captured_at_ms") or 0))
    captured_at_ms = int(newest.get("captured_at_ms") or 0)
    max_age_seconds = int(profile.get("max_age_seconds") or 0)
    freshness = _task_evidence_freshness(captured_at_ms, max_age_seconds, now_ms=now_ms)
    snapshot_seed = "|".join([
        str(newest.get("source") or ""), str(newest.get("page_type") or ""),
        store_key, account_key, str(captured_at_ms),
    ])
    return [{
        "source": str(newest.get("source") or ""),
        "page_type": str(newest.get("page_type") or ""),
        "captured_at_ms": captured_at_ms,
        "expires_at_ms": captured_at_ms + max_age_seconds * 1000 if captured_at_ms else 0,
        "max_age_seconds": max_age_seconds,
        "quality_score": int(newest.get("quality_score") or 0),
        "freshness": freshness.get("status"),
        "freshness_reason": freshness.get("reason"),
        "snapshot_ref": hashlib.sha256(snapshot_seed.encode("utf-8")).hexdigest()[:24],
    }]


def _task_result(task_id: str, store_key: str) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for entry in load_suggestion_snapshots().values():
        if not isinstance(entry, dict) or str(entry.get("task_id") or "") != task_id:
            continue
        scope = str(entry.get("scope") or "")
        if scope and scope.rsplit(":", 1)[0] != store_key:
            continue
        candidates.append(entry)
    if not candidates:
        return {"status": "pending", "evaluated_at": None, "evidence_refs": []}
    latest = max(candidates, key=lambda entry: int(entry.get("captured_at_ms") or 0))
    evaluation = latest.get("evaluation") if isinstance(latest.get("evaluation"), dict) else {}
    return {
        "status": _suggestion_effect_status(latest),
        "evaluated_at": evaluation.get("evaluated_at"),
        "evidence_refs": list(latest.get("required_source_keys") or [])[:8],
    }


def _build_task_contract(
    item: dict[str, Any],
    source_index: list[dict[str, Any]],
    *,
    store_key: str,
    account_key: str,
    business_date: str,
    now_ms: int,
) -> dict[str, Any]:
    module = _task_contract_module(item)
    profile = _TASK_CONTRACT_PROFILES[module]
    rule_id = _task_rule_id(item, module)
    subject = _task_subject(item, profile)
    source_refs = _task_source_refs(
        item, profile, source_index,
        store_key=store_key, account_key=account_key, now_ms=now_ms,
    )
    sync_task = "同步" in str(item.get("title") or "")
    blockers: list[dict[str, str]] = []
    if not store_key:
        blockers.append({"code": "STORE_UNCONFIRMED", "message": "请先确认当前抖店，再开始记录任务。"})
    if not sync_task:
        if not source_refs:
            blockers.append({"code": "EVIDENCE_MISSING", "message": "缺少该任务对应的数据快照，请重新同步。"})
        elif source_refs[0].get("freshness") != "ready":
            blockers.append({"code": "EVIDENCE_STALE", "message": "该任务证据已经过期，请重新同步后再开始。"})
    if (
        module == "plans"
        and not sync_task
        and subject.get("id_source") == "local_unverified_plan_row"
    ):
        blockers.append({
            "code": "PLAN_IDENTITY_UNVERIFIED",
            "message": "计划缺少稳定计划 ID，仅保留只读诊断；请重新同步计划列表。",
        })
    action_params = item.get("action_params") if isinstance(item.get("action_params"), dict) else {}
    target_ref = action_params.get("target_ref") if isinstance(action_params.get("target_ref"), dict) else {}
    target_account = str(target_ref.get("account_key") or item.get("account_key") or "").strip().lower()
    if module == "plans" and target_account and account_key and target_account != account_key:
        blockers.append({"code": "ACCOUNT_MISMATCH", "message": "任务计划不属于当前千川账户，请切回正确账户。"})
    required_source_keys = [
        f"{ref['source']}/{ref['page_type']}" for ref in source_refs
        if ref.get("source") and ref.get("page_type")
    ]
    task_key = hashlib.sha256(
        f"{rule_id}|{store_key}|{account_key}|{subject['kind']}|{subject['id']}".encode("utf-8")
    ).hexdigest()[:24]
    task_id = task_key[:16]
    contract: dict[str, Any] = {
        "contract_version": 2,
        "rule_id": rule_id,
        "task_key": task_key,
        "scope": {
            "store_key": store_key,
            "account_key": account_key,
            "business_date": business_date,
        },
        "subject": subject,
        "source_refs": source_refs,
        "navigation": {
            "workspace_key": profile["workspace_key"],
            "target_id": profile["target_id"],
            "platform": profile["platform"],
            "page_id": source_refs[0].get("page_type") if source_refs else profile["page_types"][0],
        },
        "eligibility": {
            "can_start": not blockers,
            "blockers": blockers,
            "sync_task": sync_task,
        },
        "completion_contract": {
            "kind": "data_freshness" if sync_task else "metric_rule",
            "readback_required": True,
            "required_source_keys": required_source_keys,
            "metric_keywords": list(profile.get("metric_keywords") or []),
            "not_before": "started_at",
            "observation_window": str(item.get("observation_window") or "完成操作后重新同步对应页面"),
            "manual_close_counts_as_verified": False,
        },
        "result": _task_result(task_id, store_key),
    }
    fingerprint_payload = {key: value for key, value in contract.items() if key not in {"contract_fingerprint", "result"}}
    contract["contract_fingerprint"] = hashlib.sha256(
        json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    return contract


def build_ops_manager() -> dict[str, Any]:
    shelf, live = build_shelf_analysis(), build_live_analysis()
    plans, inventory = build_plan_recommendations(), build_inventory_alerts()
    inventory_state = _inventory_task_state()
    creative = build_qianchuan_creative_analysis()
    product_graph = build_douyin_product_graph()
    tasks = [
        *(product_graph.get("recommendations") or []),
        *shelf["recommendations"], *live["recommendations"], *creative["recommendations"],
    ]
    if inventory_state["status"] != "ready":
        stale_inventory = inventory_state["status"] == "stale"
        tasks.append({
            "level": "warning" if stale_inventory else "info",
            "owner": "商品运营",
            "title": "重新同步商品库存" if stale_inventory else "同步商品库存",
            "action": "打开商品或库存页面并重新同步；旧快照只保留用于历史复盘。",
            "acceptance": "出现 24 小时内采集的商品与 SKU 可售库存。",
            "evidence": "现有库存快照已过期或采集时间异常，不能用于今日库存告警。" if stale_inventory else "尚无可用于今日判断的库存快照。",
            "confidence": "high",
        })
    for item in plans[:5]:
        tasks.append(_plan_ops_task_entry(item))
    for item in inventory[:3]:
        inv_entry = {"level": item["level"], "owner": "商品运营", "title": f"{item['product']} · {item['title']}", "action": item["suggestion"], "acceptance": "补货或投放限制已人工确认。", "evidence": f"当前库存 {item['evidence']['stock']:g}。", "confidence": item.get("confidence")}
        if "action_params" in item:
            inv_entry["action_params"] = item["action_params"]
        tasks.append(inv_entry)
    priority = {"high": 0, "warning": 1, "opportunity": 2, "info": 3}
    tasks.sort(key=lambda item: (priority.get(item["level"], 9), 0 if "合规" in item["title"] or "主图" in item["title"] else 1))
    states = load_task_states()
    task_settings = load_agent_settings()
    task_store_key = str(task_settings.get("store_key") or "").lower()
    task_account_key = str(task_settings.get("qianchuan_account_key") or "").lower()
    task_business_date = time.strftime("%Y-%m-%d")
    task_contract_now_ms = int(time.time() * 1000)
    task_source_index = _task_contract_source_index()
    for item in tasks:
        # Severity/opportunity is business priority, not evidence quality.
        # Only preserve an explicit evidence confidence from the analyzer;
        # otherwise stay conservative regardless of how urgent the task is.
        evidence_confidence = str(item.get("confidence") or "").lower()
        item["confidence"] = evidence_confidence if evidence_confidence in {"low", "medium", "high"} else "medium"
        item["impact"] = "风险优先" if item["level"] == "high" else "增长机会" if item["level"] == "opportunity" else "影响转化"
        if not isinstance(item.get("task_contract"), dict):
            item["task_contract"] = _build_task_contract(
                item,
                task_source_index,
                store_key=task_store_key,
                account_key=task_account_key,
                business_date=task_business_date,
                now_ms=task_contract_now_ms,
            )
        item["id"] = item["task_contract"]["task_key"][:16]
        legacy_task_ids = [
            str(value) for value in item.get("legacy_task_ids", [])
            if re.fullmatch(r"[a-f0-9]{16}", str(value or ""))
            and str(value) != item["id"]
        ]
        item["legacy_task_ids"] = list(dict.fromkeys(legacy_task_ids))
        task_state, _ = _task_state_for_ids(states, item["id"], item["legacy_task_ids"])
        item["status"] = task_state.get("status", "todo")
        item["updated_at"] = task_state.get("updated_at")
        item["assignee"] = task_state.get("assignee") or item["owner"]
        item["last_operator"] = task_state.get("operator")
        item["blocked_reason"] = task_state.get("blocked_reason")
        item["history"] = task_state.get("history", [])[-10:]
        item["carried_over"] = bool(task_state.get("carried_from_scope"))
        item["contract_version"] = 2
        item["task_key"] = item["task_contract"]["task_key"]
        item["contract_fingerprint"] = item["task_contract"]["contract_fingerprint"]
    generated_ids = {item["id"] for item in tasks}
    generated_legacy_ids = {
        legacy_id
        for item in tasks
        for legacy_id in item.get("legacy_task_ids", [])
    }
    generated_ambiguous_legacy_ids = {
        legacy_id
        for plan in plans
        for legacy_id in plan.get("ambiguous_legacy_task_ids", [])
    }
    # A recommendation may disappear after a new scan even though a human is
    # still handling it.  Keep persisted active tasks visible until they are
    # explicitly closed, including tasks carried from a previous business day.
    for task_id, task_state in states.items():
        if (
            task_id in generated_ids
            or task_id in generated_legacy_ids
            or task_id in generated_ambiguous_legacy_ids
            or not isinstance(task_state, dict)
        ):
            continue
        if task_state.get("status") not in _ACTIVE_TASK_STATUSES:
            continue
        level = str(task_state.get("level") or "warning")
        tasks.append({
            "id": task_id,
            "level": level,
            "owner": task_state.get("owner") or "运营",
            "title": task_state.get("title") or "跨日经营任务",
            "action": task_state.get("action") or "按原任务继续处理，并在完成后同步最新数据。",
            "acceptance": task_state.get("acceptance") or "同步最新数据并填写结案结果。",
            "evidence": task_state.get("evidence") or "该任务已在此前经营日开始处理。",
            "observation_window": task_state.get("observation_window") or "完成操作后观察一个数据周期",
            "status": task_state.get("status"),
            "updated_at": task_state.get("updated_at"),
            "assignee": task_state.get("assignee") or task_state.get("owner") or "待分配",
            "last_operator": task_state.get("operator"),
            "blocked_reason": task_state.get("blocked_reason"),
            "history": task_state.get("history", [])[-10:],
            "carried_over": True,
            "confidence": "medium",
            "impact": "风险优先" if level == "high" else "影响转化",
            "task_contract": task_state.get("task_contract") if isinstance(task_state.get("task_contract"), dict) else None,
        })
    for item in tasks:
        if not isinstance(item.get("task_contract"), dict):
            item["task_contract"] = _build_task_contract(
                item,
                task_source_index,
                store_key=task_store_key,
                account_key=task_account_key,
                business_date=task_business_date,
                now_ms=task_contract_now_ms,
            )
        item["contract_version"] = 2
        item["task_key"] = item["task_contract"].get("task_key")
        item["contract_fingerprint"] = item["task_contract"].get("contract_fingerprint")
    unique_tasks: dict[str, dict[str, Any]] = {}
    for item in tasks:
        unique_tasks.setdefault(item["id"], item)
    tasks = list(unique_tasks.values())
    status_priority = {"doing": 0, "observing": 1, "blocked": 2, "todo": 3, "done": 4}
    tasks.sort(key=lambda item: (priority.get(item.get("level"), 9), status_priority.get(item.get("status"), 9)))
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
        unsynced_data.append({"page_id": "doudian_shelf", "label": "抖店商城经营", "reason": "货架快照已过期或采集时间异常" if shelf.get("data_status") == "stale" else "尚无货架快照"})
    if live.get("data_status") != "ready":
        unsynced_data.append({"page_id": "live_dashboard", "label": "直播大屏", "reason": "直播快照已过期或采集时间异常" if live.get("data_status") == "stale" else "尚无直播快照"})
    if inventory_state.get("status") != "ready":
        unsynced_data.append({"page_id": "inventory", "label": "商品库存", "reason": "库存快照已过期或采集时间异常" if inventory_state.get("status") == "stale" else "尚无库存快照"})
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
        "modules": {"product_graph": {"status": product_graph.get("status"), "action_count": len(product_graph.get("recommendations") or [])}, "shelf": {"status": shelf["data_status"], "action_count": len(shelf["recommendations"])}, "live": {"status": live["data_status"], "action_count": len(live["recommendations"])}, "qianchuan": {"action_count": len(plans)}, "creative": {"status": creative["data_status"], "action_count": len(creative["recommendations"])}, "inventory": {"status": inventory_state["status"], "alert_count": len(inventory)}},
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
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError("invalid business_date") from error
    return f"{safe_store}:{day}"


_ACTIVE_TASK_STATUSES = {"doing", "observing", "blocked"}
_TASK_TRANSITIONS = {
    "todo": {"doing", "blocked"},
    "doing": {"observing", "blocked"},
    "observing": {"done", "doing", "blocked"},
    "blocked": {"todo"},
    "done": {"todo"},
}


def _task_states_for_scope(
    document: dict[str, Any],
    store_key: str | None = None,
    business_date: str | None = None,
    *,
    include_carryover: bool = True,
) -> dict[str, Any]:
    """Read one store/day and carry only unfinished tasks from earlier days.

    Task scopes remain day based for auditability.  The merged view prevents a
    task that is already being handled from disappearing at midnight.  Current
    day state always wins, and completed/todo history is never carried forward.
    """
    scope = _task_scope_key(store_key, business_date)
    scopes = document.get("scopes") if isinstance(document.get("scopes"), dict) else {}
    current = scopes.get(scope) if isinstance(scopes.get(scope), dict) else {}
    merged = {
        str(task_id): dict(item)
        for task_id, item in current.items()
        if isinstance(item, dict)
    }
    if not include_carryover:
        return merged

    safe_store, target_day = scope.rsplit(":", 1)
    prefix = f"{safe_store}:"
    resolved_task_ids = set(merged)
    for candidate_scope in sorted(scopes, reverse=True):
        if not candidate_scope.startswith(prefix) or candidate_scope >= scope:
            continue
        candidate_day = candidate_scope.rsplit(":", 1)[-1]
        if candidate_day >= target_day:
            continue
        values = scopes.get(candidate_scope)
        if not isinstance(values, dict):
            continue
        for raw_task_id, item in values.items():
            task_id = str(raw_task_id)
            if task_id in resolved_task_ids:
                continue
            # The newest prior state resolves this ID even when it is terminal.
            # Otherwise a still-older active state can resurrect after closure.
            resolved_task_ids.add(task_id)
            if not isinstance(item, dict):
                continue
            if item.get("status") not in _ACTIVE_TASK_STATUSES:
                continue
            carried = dict(item)
            carried["carried_from_scope"] = candidate_scope
            merged[task_id] = carried
    return merged


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
    return _task_states_for_scope(document, store_key, business_date)


def _canonical_ops_task(task_id: str) -> dict[str, Any] | None:
    """Resolve the current server-authored task instead of trusting UI copy."""

    try:
        tasks = [
            item for item in build_ops_manager().get("all_tasks", [])
            if isinstance(item, dict)
        ]
        exact = next(
            (item for item in tasks if str(item.get("id") or "") == task_id),
            None,
        )
        if exact is not None:
            return exact
        aliases = [
            item for item in tasks
            if task_id in {
                str(value) for value in item.get("legacy_task_ids", [])
                if re.fullmatch(r"[a-f0-9]{16}", str(value or ""))
            }
        ]
        if len(aliases) == 1:
            return aliases[0]

        # The operations manager intentionally promotes only the first five
        # plan recommendations.  Plan-workbench cards outside that shortlist
        # still need the same server-authored contract when their state changes.
        plan_tasks: list[dict[str, Any]] = []
        ambiguous_plan_aliases: set[str] = set()
        for recommendation in build_plan_recommendations():
            if not isinstance(recommendation, dict):
                continue
            ambiguous_plan_aliases.update(
                str(value) for value in recommendation.get("ambiguous_legacy_task_ids", [])
                if re.fullmatch(r"[a-f0-9]{16}", str(value or ""))
            )
            contract = (
                recommendation.get("task_contract")
                if isinstance(recommendation.get("task_contract"), dict)
                else {}
            )
            canonical_id = str(
                recommendation.get("task_id") or contract.get("task_key") or ""
            )[:16]
            if not re.fullmatch(r"[a-f0-9]{16}", canonical_id) or not contract:
                continue
            task_entry = _plan_ops_task_entry(recommendation)
            task_entry.update({
                "id": canonical_id,
                "task_contract": contract,
                "task_key": contract.get("task_key"),
                "contract_fingerprint": contract.get("contract_fingerprint"),
                "status": recommendation.get("task_status", "todo"),
                "updated_at": recommendation.get("task_updated_at"),
            })
            plan_tasks.append(task_entry)
        if task_id in ambiguous_plan_aliases:
            raise ValueError("旧任务编号对应多个千川计划，无法安全判断归属；请刷新后使用新的任务编号。")
        exact_plan = next(
            (item for item in plan_tasks if item.get("id") == task_id),
            None,
        )
        if exact_plan is not None:
            return exact_plan
        plan_aliases = [
            item for item in plan_tasks
            if task_id in set(item.get("legacy_task_ids") or [])
        ]
        # Never guess when an old identifier is ambiguous.  A unique alias is
        # server-authored by the current recommendation and safe to migrate.
        return plan_aliases[0] if len(plan_aliases) == 1 else None
    except ValueError:
        raise
    except Exception:
        logger.exception("解析任务服务端合同失败: %s", task_id)
        return None


def update_task_state(
    task_id: str,
    status: str,
    *,
    operator: str = "",
    assignee: str = "",
    note: str = "",
    title: str = "",
    owner: str = "",
    action: str = "",
    acceptance: str = "",
    evidence: str = "",
    observation_window: str = "",
    level: str = "",
    contract_fingerprint: str = "",
    store_key: str | None = None,
    business_date: str | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{16}", str(task_id or "")):
        raise ValueError("invalid task_id")
    if status not in {"todo", "doing", "observing", "blocked", "done"}:
        raise ValueError("invalid task status")
    submitted_task_id = str(task_id)
    canonical_task = _canonical_ops_task(submitted_task_id)
    canonical_task_id = str((canonical_task or {}).get("id") or "")
    if not re.fullmatch(r"[a-f0-9]{16}", canonical_task_id):
        canonical_task_id = submitted_task_id
    legacy_task_ids = [
        str(value) for value in (canonical_task or {}).get("legacy_task_ids", [])
        if re.fullmatch(r"[a-f0-9]{16}", str(value or ""))
        and str(value) != canonical_task_id
    ]
    task_id = canonical_task_id
    canonical_contract = (
        canonical_task.get("task_contract")
        if isinstance(canonical_task, dict) and isinstance(canonical_task.get("task_contract"), dict)
        else {}
    )
    submitted_fingerprint = str(contract_fingerprint or "")[:64]
    current_fingerprint = str(canonical_contract.get("contract_fingerprint") or "")
    if submitted_fingerprint and (not current_fingerprint or submitted_fingerprint != current_fingerprint):
        raise ValueError("任务数据已经更新，请刷新任务后重新操作。")
    operator = str(operator or "本机运营")[:80]
    assignee = str(assignee or "")[:80]
    note = str(note or "")[:300]
    title = str((canonical_task or {}).get("title") or title or "")[:160]
    owner = str((canonical_task or {}).get("owner") or owner or "")[:80]
    action = str((canonical_task or {}).get("action") or action or "")[:500]
    acceptance = str((canonical_task or {}).get("acceptance") or acceptance or "")[:300]
    evidence = str((canonical_task or {}).get("evidence") or evidence or "")[:500]
    observation_window = str((canonical_task or {}).get("observation_window") or observation_window or "")[:160]
    level = str((canonical_task or {}).get("level") or level or "")[:32]
    if status == "blocked" and not note:
        raise ValueError("阻止任务时必须填写原因")
    if status == "done" and not note:
        raise ValueError("结案时必须填写结果说明")
    effective_store = str(store_key or load_agent_settings().get("store_key") or "").lower()
    known_stores = {str(item.get("key") or "") for item in list_store_identities()}
    if not effective_store or effective_store not in known_stores:
        raise ValueError("尚未识别当前店铺，任务状态不会写入未归属账本。")
    contract_store = str((canonical_contract.get("scope") or {}).get("store_key") or "").lower()
    if contract_store and contract_store != effective_store:
        raise ValueError("任务不属于当前店铺，请切回正确店铺后刷新。")
    store_key = effective_store
    with _state_lock:
        document = _load_task_state_document()
        scope = _task_scope_key(store_key, business_date)
        scopes = document.setdefault("scopes", {})
        states = scopes.setdefault(scope, {})
        scoped_states = _task_states_for_scope(
            document, store_key, business_date, include_carryover=True
        )
        previous, _ = _task_state_for_ids(scoped_states, task_id, legacy_task_ids)
        previous = dict(previous) if isinstance(previous, dict) else {}
        previous_status = previous.get("status", "todo")
        if previous_status == "todo" and status == "doing" and canonical_contract:
            eligibility = canonical_contract.get("eligibility") if isinstance(canonical_contract.get("eligibility"), dict) else {}
            if eligibility.get("can_start") is not True:
                messages = [
                    str(item.get("message") or "") for item in eligibility.get("blockers", [])
                    if isinstance(item, dict) and item.get("message")
                ]
                raise ValueError("；".join(messages) or "任务当前不满足开始条件，请重新同步后再试。")
        if status != previous_status and status not in _TASK_TRANSITIONS.get(previous_status, set()):
            if previous_status == "doing" and status == "done":
                raise ValueError("任务不能从进行中直接结案，请先进入效果观察")
            raise ValueError(f"任务不能从 {previous_status} 直接变为 {status}")
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
            "action": action or previous.get("action") or "",
            "acceptance": acceptance or previous.get("acceptance") or "",
            "evidence": evidence or previous.get("evidence") or "",
            "observation_window": observation_window or previous.get("observation_window") or "",
            "level": level or previous.get("level") or "warning",
            "contract_version": 2 if canonical_contract else previous.get("contract_version", 1),
            "task_contract": canonical_contract or previous.get("task_contract") or {},
            "contract_fingerprint": current_fingerprint or previous.get("contract_fingerprint") or "",
            "history": [*(previous.get("history") or []), event][-50:],
        }
        next_state.pop("_carried_from_scope", None)
        next_state.pop("carried_from_scope", None)
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
            next_state["completion_note"] = note
            next_state["blocked_reason"] = None
        states[task_id] = next_state
        for legacy_task_id in legacy_task_ids:
            states[legacy_task_id] = {
                "status": "done",
                "updated_at": now,
                "migration_tombstone": True,
                "migrated_to": task_id,
            }
        _atomic_json_write(_task_states_path(), document)
    if legacy_task_ids:
        _migrate_suggestion_snapshot_aliases(
            task_id,
            legacy_task_ids,
            store_key=store_key,
            business_date=business_date,
        )
    baseline_tracking = None
    # Establish the baseline on the server.  This is deliberately idempotent so
    # retries from the extension cannot replace the original before-state.
    if previous_status == "todo" and status == "doing":
        baseline = save_suggestion_snapshot(
            task_id,
            {
                "title": next_state.get("title"),
                "owner": next_state.get("owner"),
                "action": next_state.get("action"),
                "acceptance": next_state.get("acceptance"),
                "evidence": next_state.get("evidence"),
                "observation_window": next_state.get("observation_window"),
                "task_contract": next_state.get("task_contract"),
            },
            store_key=store_key,
            business_date=business_date,
        )
        commerce_run = _start_product_task_run(baseline)
        baseline_tracking = {
            "status": "reused" if baseline.get("baseline_reused") else "captured",
            "created_at": baseline.get("created_at"),
            "commerce_task_run_id": (commerce_run or {}).get("run_id"),
            "commerce_task_run_status": (commerce_run or {}).get("status"),
        }
    # When task transitions to done, evaluate suggestion effectiveness.
    if status == "done" and previous_status != "done":
        _evaluate_on_completion(
            task_id,
            completion_note=note,
            store_key=store_key,
            business_date=business_date,
        )
    result = {"task_id": task_id, "scope": scope, **next_state}
    if baseline_tracking:
        result["baseline_tracking"] = baseline_tracking
    return result


def _onboarding_state_path() -> Path:
    return DATA_DIR / "onboarding_state.json"


def _load_onboarding_state() -> dict[str, Any]:
    _recover_auto_store_context_transaction()
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
        item for item in snapshots
        if confirmed_at and int(item.get("captured_at") or 0) >= confirmed_at
    ]
    usable_snapshots = [item for item in formal_snapshots if int(item.get("quality_score") or 0) >= 60]
    fresh_usable_snapshots = [item for item in usable_snapshots if item.get("fresh")]
    formal_doudian_types = {
        str(item.get("page_type") or "") for item in usable_snapshots if item.get("source") == "doudian"
    }
    fresh_doudian_types = {
        str(item.get("page_type") or "") for item in fresh_usable_snapshots if item.get("source") == "doudian"
    }
    required_doudian_types = {"overview", "orders", "products", "shelf"}
    store_confirmed = bool(selected_key and confirmed_at)
    sync_complete = bool(store_confirmed and required_doudian_types.issubset(formal_doudian_types))
    current_sync_fresh = bool(store_confirmed and required_doudian_types.issubset(fresh_doudian_types))
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
    value_milestones = _value_milestones(
        activation_complete=first_task_viewed,
        manual_closed_tasks=sum(
            item.get("status") == "done" for item in (ops.get("all_tasks") or [])
            if isinstance(item, dict)
        ),
        promotion_readback_actions=_verified_action_count_for_store(selected_store),
        task_evidence_counts=_scoped_task_evidence_counts(selected_key),
    )
    activation_complete = all(item["complete"] for item in steps)
    if activation_complete:
        status_label = (
            "首次激活与已验证价值均已完成"
            if value_milestones["verified_value_complete"]
            else "首次激活已完成，价值待回读"
        )
    else:
        status_label = f"继续第 {current_index + 1} 步：{current['label']}"
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
    if current_sync_fresh:
        freshness_status = "fresh"
        freshness_label = "当前关键数据均在 10 分钟内更新"
    elif sync_complete:
        freshness_status = "stale"
        freshness_label = "首次采集已完成，当前关键数据需要刷新"
    elif fresh_doudian_types:
        freshness_status = "partial"
        freshness_label = "当前关键数据尚未同步完整"
    else:
        freshness_status = "missing"
        freshness_label = "当前没有可用的新鲜关键数据"
    return {
        "schema_version": 2,
        "status": "completed" if activation_complete else "in_progress",
        "status_label": status_label,
        "progress": {"completed": completed_count, "total": len(steps), "percent": round(completed_count / len(steps) * 100)},
        "value_milestones": value_milestones,
        "current_step": current,
        "steps": steps,
        "store_key": selected_key,
        "store_confirmed": store_confirmed,
        "store_confirmed_at": scoped_state.get("store_confirmed_at"),
        "selected_store": selected_store,
        "first_task": first_task,
        "missing_data": missing_data[:4],
        "optional_enhancements": optional_enhancements,
        "current_freshness": {
            "status": freshness_status,
            "status_label": freshness_label,
            "fresh": current_sync_fresh,
            "max_age_seconds": STALE_SECONDS,
            "fresh_snapshot_count": len(fresh_snapshots),
            "fresh_usable_snapshot_count": len(fresh_usable_snapshots),
            "fresh_required_page_types": sorted(required_doudian_types & fresh_doudian_types),
            "stale_or_missing_required_page_types": sorted(required_doudian_types - fresh_doudian_types),
        },
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
    core_data = context.get("core_data") if isinstance(context.get("core_data"), dict) else {}
    freshness = context.get("freshness") if isinstance(context.get("freshness"), dict) else {}
    operational_data_ready = bool(core_data.get("status") == "ready" and freshness.get("fresh"))
    refresh_page_ids = list(dict.fromkeys([
        str(page_id)
        for page_id in list(core_data.get("stale_types") or []) + list(core_data.get("missing_types") or [])
        if str(page_id) in {"overview", "orders", "products", "shelf"}
    ]))
    levels = [
        {"id": "L1", "label": "店铺已识别", "reached": l1, "next_action": "确认当前抖店"},
        {"id": "L2", "label": "核心经营数据已接入", "reached": l2, "next_action": "完成 3 分钟快速巡店"},
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
    elif not operational_data_ready:
        action = {
            "id": "refresh_core_data",
            "label": f"刷新 {len(refresh_page_ids) or 4} 个核心经营页",
            "eta": "约 3–5 分钟",
            "value": "只更新经营概览、订单、商品和商城中的过期或缺失页面，完成后自动复检投放准备度。",
            "failure": "店铺和投放账户已经接入，但当前核心经营数据已过期或缺失；连接成功不代表今天的数据已经可以用于执行判断。",
            "page_ids": refresh_page_ids or ["overview", "orders", "products", "shelf"],
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
        "operational": {
            "state": "execution_ready" if l4 else "data_fresh" if operational_data_ready else "refresh_required" if l2 else "setup_required",
            "state_label": "可进入受控执行" if l4 else "当前数据可用于判断" if operational_data_ready else "连接完成，数据待刷新" if l2 else "连接准备未完成",
            "connected_level": f"L{reached_level}" if reached_level else "L0",
            "core_data_fresh": operational_data_ready,
            "execution_review_ready": bool(context.get("execution_review_allowed")),
            "refresh_page_ids": refresh_page_ids,
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
        if not isinstance(value, dict) or value.get("schema_version") != 2:
            # Schema 1 mixed stores, accounts and failed samples. It is not a
            # trustworthy comparison source and is intentionally not migrated.
            return {}
        entries = value.get("entries")
        return entries if isinstance(entries, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _health_scope_key(scope: dict[str, str]) -> str:
    encoded = json.dumps(scope, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def _quality_explicit_empty(quality: dict[str, Any], result: dict[str, Any] | None = None) -> bool:
    result = result if isinstance(result, dict) else {}
    server_coverage = quality.get("server_plan_identity_coverage")
    return bool(
        quality.get("explicit_empty") is True
        or result.get("explicit_empty") is True
        or result.get("collection_state") == "confirmed_empty"
        or (
            isinstance(server_coverage, dict)
            and server_coverage.get("explicit_empty") is True
        )
    )


def update_health_baseline(
    source: str,
    page_type: str,
    quality: dict[str, Any],
    *,
    store_key: str,
    account_key: str,
    filter_contract: str,
    run_id: str,
    captured_at: int,
    explicit_empty: bool = False,
) -> bool:
    """Add one proven healthy full-scan sample without moving a mature baseline on failure."""

    if source not in ALLOWED_SOURCES or not page_type or not filter_contract or not run_id:
        return False
    scope = {
        "source": source,
        "page_type": page_type,
        "store_key": store_key,
        "account_key": account_key,
        "filter_contract": filter_contract,
    }
    key = _health_scope_key(scope)
    score = max(0, min(100, int(quality.get("score", 0) or 0)))
    row_count = max(0, int(quality.get("row_count", 0) or 0))
    metric_count = max(0, int(quality.get("metric_count", 0) or 0))
    if score < 70:
        return False
    with _state_lock:
        baselines = load_health_baselines()
        current = baselines.get(key) if isinstance(baselines.get(key), dict) else {}
        if current.get("scope") not in (None, scope):
            return False
        samples = [item for item in current.get("samples", []) if isinstance(item, dict)]
        if any(str(item.get("run_id") or "") == run_id for item in samples):
            return False
        baseline = current.get("baseline") if isinstance(current.get("baseline"), dict) else {}
        if int(baseline.get("sample_count") or 0) >= 5:
            avg_score = float(baseline.get("avg_quality_score") or 0)
            avg_rows = float(baseline.get("avg_row_count") or 0)
            avg_metrics = float(baseline.get("avg_metric_count") or 0)
            if avg_score > 30 and score < avg_score * 0.5:
                return False
            if not explicit_empty and avg_rows > 5 and row_count < avg_rows * 0.3:
                return False
            if avg_metrics > 3 and metric_count < avg_metrics * 0.3:
                return False
        samples.append({
            "quality_score": score,
            "row_count": row_count,
            "metric_count": metric_count,
            "explicit_empty": explicit_empty,
            "captured_at": max(0, int(captured_at or 0)),
            "run_id": run_id,
        })
        current["samples"] = samples[-30:]
        current["scope"] = scope
        accepted = current["samples"][-10:]
        if len(current["samples"]) >= 5:
            row_samples = [sample for sample in accepted if sample.get("explicit_empty") is not True]
            avg_score = sum(sample["quality_score"] for sample in accepted) / len(accepted)
            avg_rows = sum(sample["row_count"] for sample in row_samples) / len(row_samples) if row_samples else 0
            avg_metrics = sum(sample["metric_count"] for sample in accepted) / len(accepted)
            current["baseline"] = {
                "avg_quality_score": round(avg_score, 1),
                "avg_row_count": round(avg_rows, 1),
                "avg_metric_count": round(avg_metrics, 1),
                "sample_count": len(current["samples"]),
                "updated_at": max(0, int(captured_at or 0)),
            }
        baselines[key] = current
        _atomic_json_write(
            _health_baselines_path(),
            {"schema_version": 2, "entries": baselines},
        )
    return True


def _promote_scan_health_baselines(status: dict[str, Any]) -> int:
    """Promote only a complete, identity-bound full scan into selector history."""

    if (
        status.get("status") != "completed"
        or status.get("scope") != "full"
        or status.get("coverage_complete") is not True
    ):
        return 0
    store_key = str(status.get("store_key") or "").lower()
    account_key = str(status.get("account_key") or "").lower()
    run_id = str(status.get("run_id") or "").lower()
    if not store_key or not SAFE_KEY.fullmatch(store_key) or not run_id:
        return 0
    expected = list(SCAN_FULL_DOUDIAN_PAGE_IDS)
    if account_key:
        expected.extend(SCAN_ADS_OPTIONAL_PAGE_IDS)
    planned = status.get("planned_page_ids") if isinstance(status.get("planned_page_ids"), list) else []
    if any(page_id not in planned for page_id in expected):
        return 0
    by_id = {
        str(item.get("id") or ""): item
        for item in status.get("results", [])
        if isinstance(item, dict) and item.get("id")
    }
    if any(
        page_id not in by_id
        or by_id[page_id].get("ok") is not True
        or by_id[page_id].get("collection_complete") is not True
        for page_id in expected
    ):
        return 0
    promoted = 0
    for page_id in expected:
        result = by_id[page_id]
        source = str(result.get("source") or "")
        page_type = str(result.get("page_type") or "")
        quality = result.get("quality") if isinstance(result.get("quality"), dict) else {}
        expected_source = "qianchuan" if page_id.startswith("qianchuan_") else "doudian"
        if source != expected_source or not page_type:
            continue
        if str(result.get("store_key") or "").lower() != store_key:
            continue
        if source == "qianchuan" and str(result.get("account_key") or "").lower() != account_key:
            continue
        promoted += int(update_health_baseline(
            source,
            page_type,
            quality,
            store_key=store_key,
            account_key=account_key,
            filter_contract=page_id,
            run_id=run_id,
            captured_at=int(result.get("captured_at") or status.get("finished_at") or 0),
            explicit_empty=_quality_explicit_empty(quality, result),
        ))
    return promoted


def check_selector_health() -> dict[str, Any]:
    baselines = load_health_baselines()
    alerts: list[dict[str, Any]] = []
    settings = load_agent_settings()
    current_store = str(settings.get("store_key") or "").lower()
    current_account = str(settings.get("qianchuan_account_key") or "").lower()
    scan = load_scan_status()
    scan_scope_matches = (
        scan.get("status") == "completed"
        and scan.get("scope") == "full"
        and scan.get("coverage_complete") is True
        and str(scan.get("store_key") or "").lower() == current_store
        and str(scan.get("account_key") or "").lower() == current_account
    )
    scan_results = {
        str(item.get("id") or ""): item
        for item in scan.get("results", [])
        if scan_scope_matches and isinstance(item, dict) and item.get("id")
    }
    visible_baselines: dict[str, Any] = {}
    for key, entry in baselines.items():
        if not isinstance(entry, dict):
            continue
        scope = entry.get("scope") if isinstance(entry.get("scope"), dict) else {}
        if scope.get("store_key") != current_store or scope.get("account_key") != current_account:
            continue
        baseline = entry.get("baseline")
        if not baseline or baseline.get("sample_count", 0) < 3:
            continue
        visible_baselines[key] = baseline
        source = str(scope.get("source") or "")
        page_type = str(scope.get("page_type") or "")
        filter_contract = str(scope.get("filter_contract") or "")
        result = scan_results.get(filter_contract)
        if (
            isinstance(result, dict)
            and result.get("ok") is True
            and result.get("collection_complete") is True
        ):
            quality = result.get("quality") if isinstance(result.get("quality"), dict) else {}
            captured_at = int(result.get("captured_at") or 0)
            current_score = max(0, min(100, int(quality.get("score", 0) or 0)))
            current_rows = max(0, int(quality.get("row_count", 0) or 0))
            explicit_empty = _quality_explicit_empty(quality, result)
        else:
            # Without the same filter contract there is no safe comparison.
            captured_at = 0
            current_score = 0
            current_rows = 0
            explicit_empty = False
        age_seconds = max(0, int(time.time() - captured_at / 1000)) if captured_at > 0 else STALE_SECONDS + 1
        if age_seconds >= STALE_SECONDS:
            alerts.append({
                "level": "stale",
                "page": f"{source}/{page_type}",
                "title": f"{page_type} 数据已过期",
                "detail": "当前同店铺、同账户、同页面筛选口径没有新鲜的完整巡检结果。",
                "action": "请完成一次全店巡检后再判断页面结构是否异常。",
            })
            continue
        if (
            not isinstance(result, dict)
            or result.get("ok") is not True
            or result.get("collection_complete") is not True
        ):
            continue
        avg_score = baseline.get("avg_quality_score", 0)
        avg_rows = baseline.get("avg_row_count", 0)
        # Alert if quality dropped significantly
        if avg_score > 30 and current_score < avg_score * 0.5:
            alerts.append({
                "level": "high",
                "page": f"{source}/{page_type}",
                "title": f"{page_type} 采集质量异常下降",
                "detail": f"当前质量分 {current_score}，历史均值 {avg_score:.0f}。页面结构可能已改版。",
                "action": "请打开该页面检查字段是否正确识别，如需更新选择器请提交 Issue。",
            })
        elif not explicit_empty and avg_rows > 5 and current_rows < avg_rows * 0.3:
            alerts.append({
                "level": "warning",
                "page": f"{source}/{page_type}",
                "title": f"{page_type} 采集行数偏低",
                "detail": f"当前 {current_rows} 行，历史均值 {avg_rows:.0f} 行。可能是虚拟滚动未触发或列表缩短。",
                "action": "刷新页面后重试；如仍偏低，平台可能调整了分页或列表结构。",
            })
    # Overall health score
    total_pages = len(visible_baselines)
    healthy = len(visible_baselines)
    return {
        "generated_at": _now_label(),
        "total_tracked_pages": total_pages,
        "pages_with_baseline": healthy,
        "alerts": alerts,
        "baselines": visible_baselines,
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


def _migrate_suggestion_snapshot_aliases(
    canonical_task_id: str,
    legacy_task_ids: list[str],
    *,
    store_key: str,
    business_date: str | None = None,
) -> int:
    """Re-key uniquely resolved legacy baselines before readback evaluation."""

    aliases = {
        str(value) for value in legacy_task_ids
        if re.fullmatch(r"[a-f0-9]{16}", str(value or ""))
        and str(value) != canonical_task_id
    }
    if not aliases:
        return 0
    target_scope = _task_scope_key(store_key, business_date)
    safe_store, target_day = target_scope.rsplit(":", 1)
    migrated = 0
    with _state_lock:
        snapshots = load_suggestion_snapshots()
        candidates: list[tuple[int, str, str, dict[str, Any]]] = []
        for storage_key, item in snapshots.items():
            if not isinstance(item, dict):
                continue
            legacy_task_id = str(item.get("task_id") or str(storage_key).rsplit("|", 1)[-1])
            if legacy_task_id not in aliases:
                continue
            scope = str(item.get("scope") or "")
            try:
                item_store, item_day = scope.rsplit(":", 1)
            except ValueError:
                continue
            if item_store != safe_store or item_day > target_day:
                continue
            captured_at_ms = int(
                item.get("captured_at_ms")
                or _label_timestamp(item.get("created_at")) * 1000
            )
            candidates.append((captured_at_ms, str(storage_key), scope, item))
        # Earliest baseline wins within the same task/day, matching the existing
        # idempotent baseline contract.  A canonical entry always wins outright.
        for _, storage_key, scope, item in sorted(candidates):
            canonical_storage_key = f"{scope}|{canonical_task_id}"
            if canonical_storage_key not in snapshots:
                snapshots[canonical_storage_key] = {
                    **item,
                    "task_id": canonical_task_id,
                    "migrated_from_task_id": str(item.get("task_id") or ""),
                    "migrated_at": _now_label(),
                }
            snapshots.pop(storage_key, None)
            migrated += 1
        if migrated:
            _atomic_json_write(_suggestion_snapshots_path(), snapshots)
    return migrated


_PRODUCT_TASK_METRIC_LABELS = {
    "exposure": "曝光人数",
    "clicks": "点击人数",
    "click_rate": "点击率",
    "views": "观看次数",
    "orders": "成交订单数",
    "gmv": "成交金额",
    "conversion_rate": "转化率",
    "spend": "消耗",
    "roi": "ROI",
    "stock": "库存",
    "refund_rate": "退款率",
    "break_even_roi": "保本ROI",
    "profit_margin": "毛利率",
}


def _find_product_subject(subject: dict[str, Any]) -> dict[str, Any] | None:
    subject_id = str(subject.get("id") or "")
    if not subject_id or str(subject.get("id_source") or "") == "derived":
        return None
    graph = build_douyin_product_graph()
    return next((item for item in graph.get("products", []) if subject_id in {
        str(item.get("product_key") or ""), str(item.get("product_id") or ""),
        str(item.get("sku_id") or ""), str(item.get("merchant_code") or ""),
        str(item.get("entity_ref") or ""),
    }), None)


def _product_subject_entity_key(subject: dict[str, Any]) -> str:
    product = _find_product_subject(subject)
    if not isinstance(product, dict):
        return ""
    for key in ("product_id", "sku_id", "merchant_code"):
        value = str(product.get(key) or "").strip().lower()
        if re.fullmatch(r"(?:product_v1|sku_v1|merchant_v1)_[a-f0-9]{26}", value):
            return value
    return ""


_INVALID_PLAN_IDENTIFIERS = {
    "", "-", "--", "暂无", "未知", "unavailable", "unknown", "null", "none",
    "masked", "missing", "undefined", "n/a", "na", "[masked]", "[已隐藏]", "[标识无效]",
}


def _normalize_plan_identifier(value: Any) -> str:
    """Return only an explicit platform or browser-pseudonymized plan ID."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if text.lower() in {item.lower() for item in _INVALID_PLAN_IDENTIFIERS}:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_-]{4,80}", text):
        return text
    matches = re.findall(r"(?:^|\s)ID\s*[:：-]?\s*([A-Za-z0-9_-]{4,80})(?=\s|$)", text, re.IGNORECASE)
    return matches[-1] if len(set(matches)) == 1 else ""


def _extract_labeled_plan_identifier(value: Any) -> str:
    """Extract an ID only from an explicitly labelled line in a composite cell.

    Composite plan columns also carry human-readable names.  A name such as
    ``SUMMER_SALE`` or ``12345678`` is syntactically indistinguishable from a
    bare platform identifier, so only an ``ID: value`` sub-line is accepted
    outside a dedicated ID column.
    """

    candidates: set[str] = set()
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        match = re.fullmatch(
            rf"(?:{_PLAN_HEADER_ENTITY_PATTERN}\s*)?ID\s*[:：]\s*([A-Za-z0-9_-]{{4,80}})",
            line,
            re.IGNORECASE,
        )
        if not match:
            continue
        identifier = _normalize_plan_identifier(match.group(1))
        if identifier:
            candidates.add(identifier)
    return next(iter(candidates)) if len(candidates) == 1 else ""


def _plan_identifier(record: dict[str, Any]) -> str:
    for label, value in record.items():
        if _plan_header_role(label) == "id":
            identifier = _normalize_plan_identifier(value)
            if identifier:
                return identifier
    candidates: set[str] = set()
    for label, value in record.items():
        if not _is_plan_name_header(label):
            continue
        identifier = _extract_labeled_plan_identifier(value)
        if identifier:
            candidates.add(identifier)
    return next(iter(candidates)) if len(candidates) == 1 else ""


def _plan_name(record: dict[str, Any], plan_id: str = "") -> str:
    ignored = {"自定义", "推商品", "推直播间", "商品", "素材", "抖音号"}
    for label, value in record.items():
        if not _is_plan_name_header(label):
            continue
        lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
        for line in lines:
            if line in ignored or re.match(r"^ID\s*[:：-]", line, re.IGNORECASE):
                continue
            if re.match(r"^共\s*\d+\s*条计划$", line):
                continue
            return line[:100]
    return f"计划 {plan_id[-6:]}" if plan_id else ""


def _live_plan_display_name(record: dict[str, Any]) -> str:
    """Recognize a live-plan business row whose identity column is 抖音号.

    The current live console sometimes labels its first business column
    ``抖音号`` instead of ``计划``.  Material rows use the same column, so the
    fallback requires the visible "设置直播规划" marker plus an independent
    plan-control column.  It supplies display text only and never manufactures
    a plan ID.
    """

    normalized = {_commerce_header_key(label): value for label, value in record.items()}
    douyin_value = normalized.get("抖音号")
    if douyin_value is None:
        return ""
    lines = [line.strip() for line in str(douyin_value or "").splitlines() if line.strip()]
    if "设置直播规划" not in lines:
        return ""
    plan_control_headers = {
        "投放状态", "计划状态", "投放设置", "推广类型", "推广模式", "投放模式", "计划类型",
    }
    if not any(label in normalized for label in plan_control_headers):
        return ""
    display_lines = [line for line in lines if line not in {"设置直播规划", "素材"}]
    if not display_lines:
        return ""
    if display_lines[0] == "直播大屏" and len(display_lines) > 1:
        return f"直播大屏 · {display_lines[1]}"[:100]
    return display_lines[0][:100]


_EMPTY_PLAN_EVIDENCE_RE = re.compile(
    r"(?:暂无|没有|无)(?:符合条件的|相关的?)?(?:投放)?计划"
    r"|共\s*0\s*条计划"
    r"|(?:计划数|计划总数|当前计划)\s*[:：]?\s*0(?:\D|$)",
    re.IGNORECASE,
)

_EMPTY_PLAN_ROW_RE = re.compile(
    r"^(?:暂无(?:相关|符合条件的)?(?:投放)?计划|没有(?:相关|符合条件的)?(?:投放)?计划|"
    r"无(?:相关|符合条件的)?(?:投放)?计划|暂无数据|无数据|暂无内容|共\s*0\s*条计划|0\s*条计划)$",
    re.IGNORECASE,
)


def _is_explicit_empty_plan_row(row: list[Any]) -> bool:
    text = re.sub(r"\s+", "", " ".join(str(value or "") for value in row)).strip()
    return bool(text and _EMPTY_PLAN_ROW_RE.fullmatch(text))


def _has_explicit_empty_plan_evidence(data: dict[str, Any]) -> bool:
    values: list[str] = []
    if isinstance(data.get("signals"), list):
        values.extend(str(value or "") for value in data["signals"])
    if data.get("page_text"):
        values.append(str(data["page_text"]))
    return any(_EMPTY_PLAN_EVIDENCE_RE.search(value) for value in values)


def _plan_snapshot_identity_coverage(data: dict[str, Any]) -> dict[str, Any]:
    """Rebuild split-table context and verify plan identity before persistence."""

    plan_table_count = 0
    eligible_rows = 0
    identified_rows = 0
    explicit_empty_rows = 0
    pending_headers: list[str] = []
    tables = data.get("tables") if isinstance(data.get("tables"), list) else []
    for table in tables:
        if not isinstance(table, dict):
            pending_headers = []
            continue
        raw_headers = table.get("headers") if isinstance(table.get("headers"), list) else []
        headers = [str(value or "") for value in raw_headers]
        rows = table.get("rows") if isinstance(table.get("rows"), list) else []
        inherited = False
        has_plan_header = any(_is_plan_name_header(header) for header in headers)
        if not has_plan_header and pending_headers and any(
            isinstance(row, list) and len(row) == len(pending_headers) for row in rows
        ):
            headers = list(pending_headers)
            has_plan_header = True
            inherited = True
        if not has_plan_header:
            pending_headers = []
            continue

        plan_table_count += 1
        table_eligible = 0
        for row in rows:
            if not isinstance(row, list):
                continue
            if _is_explicit_empty_plan_row(row):
                explicit_empty_rows += 1
                continue
            record = {
                headers[index]: row[index]
                for index in range(min(len(headers), len(row)))
                if headers[index]
            }
            plan_id = _plan_identifier(record)
            plan_name = _plan_name(record, plan_id)
            if not plan_name:
                continue
            table_eligible += 1
            eligible_rows += 1
            if plan_id:
                identified_rows += 1
        pending_headers = [] if inherited or table_eligible else list(headers)

    explicit_empty = bool(explicit_empty_rows or _has_explicit_empty_plan_evidence(data))
    return {
        "plan_table_count": plan_table_count,
        "eligible_rows": eligible_rows,
        "identified_rows": identified_rows,
        "explicit_empty_rows": explicit_empty_rows,
        "explicit_empty": explicit_empty,
        "coverage_rate": round(identified_rows / eligible_rows * 100) if eligible_rows else (100 if explicit_empty else 0),
    }


def _headers_have_plan_contract(headers: list[str]) -> bool:
    """Probe the canonical plan parser without duplicating translated labels."""

    probe = {
        str(header or ""): f"plan_probe_{index:04d}"
        for index, header in enumerate(headers)
        if str(header or "")
    }
    return bool(probe and (_plan_identifier(probe) or _plan_name(probe)))


def _partition_plan_snapshot_rows(
    data: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Keep identified plan rows current and isolate rows missing stable IDs.

    A single malformed row must not discard valid rows from the same account
    and page.  Quarantined evidence is deliberately stored outside ``tables``
    so recommendation and execution readers cannot accidentally treat it as a
    current, targetable plan.
    """

    tables = data.get("tables") if isinstance(data.get("tables"), list) else []
    partitioned_tables: list[Any] = []
    quarantined: list[dict[str, Any]] = []
    pending_headers: list[str] = []
    for table_index, table in enumerate(tables):
        if not isinstance(table, dict):
            partitioned_tables.append(table)
            pending_headers = []
            continue
        raw_headers = table.get("headers") if isinstance(table.get("headers"), list) else []
        headers = [str(value or "") for value in raw_headers]
        rows = table.get("rows") if isinstance(table.get("rows"), list) else []
        effective_headers = list(headers)
        inherited = False
        has_plan_contract = _headers_have_plan_contract(effective_headers)
        if (
            not has_plan_contract
            and pending_headers
            and any(isinstance(row, list) and len(row) == len(pending_headers) for row in rows)
        ):
            effective_headers = list(pending_headers)
            has_plan_contract = True
            inherited = True
        if not has_plan_contract:
            partitioned_tables.append(dict(table))
            pending_headers = []
            continue

        kept_rows: list[Any] = []
        business_rows = 0
        for row_index, row in enumerate(rows):
            if not isinstance(row, list) or _is_explicit_empty_plan_row(row):
                kept_rows.append(row)
                continue
            record = {
                effective_headers[index]: row[index]
                for index in range(min(len(effective_headers), len(row)))
                if effective_headers[index]
            }
            plan_id = _plan_identifier(record)
            plan_name = _plan_name(record, plan_id)
            if not plan_name:
                kept_rows.append(row)
                continue
            business_rows += 1
            if plan_id:
                kept_rows.append(row)
                continue
            quarantined.append({
                "table_index": table_index,
                "row_index": row_index,
                "plan_name": plan_name[:100],
                "reason": "stable_plan_id_missing",
                "automatic_write_allowed": False,
            })
        partitioned_tables.append({**table, "rows": kept_rows})
        pending_headers = [] if inherited or business_rows else list(effective_headers)

    return {**data, "tables": partitioned_tables}, quarantined


def _finalize_oceanengine_sync_selection(result: dict[str, Any]) -> dict[str, Any]:
    """Never promote an OAuth parent/shop subject into an executable advertiser."""

    selected_key = str(load_agent_settings().get("qianchuan_account_key") or "").lower()
    parent_keys = {
        str(account.get("account_key") or "").lower()
        for account in result.get("accounts", [])
        if isinstance(account, dict) and str(account.get("account_key") or "")
    }
    cleared_parent = bool(selected_key and selected_key in parent_keys)
    if cleared_parent:
        save_agent_settings({"qianchuan_account_key": ""})
        selected_key = ""
    result["selected_account_key"] = selected_key
    result["selection_required"] = not bool(selected_key)
    result["selection_reason"] = (
        "官方授权主体不是可投放广告账户，请在千川页面读取并选择具体广告账户。"
        if not selected_key else ""
    )
    result["oauth_parent_selection_cleared"] = cleared_parent
    return result


def _collect_product_subject_metrics(subject: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    product = _find_product_subject(subject)
    if not isinstance(product, dict):
        return {}, {}
    metrics: dict[str, Any] = {}
    watermarks: dict[str, int] = {}
    evidence = product.get("evidence") if isinstance(product.get("evidence"), list) else []
    for channel, values in (product.get("metrics") or {}).items():
        if not isinstance(values, dict):
            continue
        channel_evidence = [item for item in evidence if isinstance(item, dict) and item.get("channel") == channel]
        if not channel_evidence:
            continue
        latest = max(channel_evidence, key=lambda item: int(item.get("captured_at_ms") or 0))
        source = str(latest.get("source") or "")
        page_type = str(latest.get("page_type") or "")
        if not source or not page_type:
            continue
        source_key = f"{source}/{page_type}"
        watermarks[source_key] = max(watermarks.get(source_key, 0), int(latest.get("captured_at_ms") or 0))
        for metric, value in values.items():
            label = _PRODUCT_TASK_METRIC_LABELS.get(str(metric))
            if label and value is not None:
                metrics[f"{source_key}/entity/{product['product_key']}/{label}"] = value
    return metrics, watermarks


def _collect_suggestion_metrics(
    store_key: str,
    subject: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Capture values together with per-page watermarks for honest comparisons."""
    selected_store = str(load_agent_settings().get("store_key") or "").lower()
    if not selected_store or selected_store != str(store_key or "").lower():
        return {}, {}
    if isinstance(subject, dict) and str(subject.get("kind") or "") in {"product", "douyin_product", "douyin_sku"}:
        return _collect_product_subject_metrics(subject)
    metrics: dict[str, Any] = {}
    watermarks: dict[str, int] = {}
    for item in list_snapshots():
        source = str(item.get("source") or "")
        page_type = str(item.get("page_type") or "")
        snapshot = load_data(source, page_type)
        data = (snapshot or {}).get("data", {})
        if not isinstance(data, dict):
            continue
        source_key = f"{source}/{page_type}"
        captured_at_ms = max(
            _timestamp_seconds(item.get("captured_at")) * 1000,
            _timestamp_seconds(data.get("captured_at")) * 1000,
            _timestamp_seconds((snapshot or {}).get("timestamp")) * 1000,
        )
        watermarks[source_key] = captured_at_ms
        safe_metrics = data.get("safe_metrics") if isinstance(data.get("safe_metrics"), dict) else {}
        for key, value in safe_metrics.items():
            metrics[f"{source_key}/{key}"] = value
    return metrics, watermarks


def _suggestion_contract_scope(context: dict[str, Any] | None) -> dict[str, Any]:
    contract = (context or {}).get("task_contract") if isinstance(context, dict) else None
    if not isinstance(contract, dict) or int(contract.get("contract_version") or 0) != 2:
        return {}
    completion = contract.get("completion_contract") if isinstance(contract.get("completion_contract"), dict) else {}
    scope = contract.get("scope") if isinstance(contract.get("scope"), dict) else {}
    subject = contract.get("subject") if isinstance(contract.get("subject"), dict) else {}
    source_keys = [
        str(value)[:120] for value in completion.get("required_source_keys", [])
        if re.fullmatch(r"[a-z0-9_-]+/[a-z0-9_-]+", str(value or ""), re.IGNORECASE)
    ][:12]
    metric_keywords = [str(value)[:80] for value in completion.get("metric_keywords", []) if str(value or "").strip()][:20]
    return {
        "contract_version": 2,
        "contract_fingerprint": str(contract.get("contract_fingerprint") or "")[:64],
        "task_key": str(contract.get("task_key") or "")[:64],
        "rule_id": str(contract.get("rule_id") or "")[:120],
        "store_key": str(scope.get("store_key") or "")[:80],
        "account_key": str(scope.get("account_key") or "")[:120],
        "subject": {
            "kind": str(subject.get("kind") or "")[:40],
            "id": str(subject.get("id") or "")[:96],
            "id_source": str(subject.get("id_source") or "")[:20],
            "name": str(subject.get("name") or "")[:160],
        },
        "required_source_keys": source_keys,
        "metric_keywords": metric_keywords,
        "completion_kind": str(completion.get("kind") or "metric_rule")[:40],
        "source_max_age_seconds": max(
            [int(ref.get("max_age_seconds") or 0) for ref in contract.get("source_refs", []) if isinstance(ref, dict)] or [0]
        ),
    }


def _filter_suggestion_metrics(
    metrics: dict[str, Any],
    watermarks: dict[str, int],
    contract_scope: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    required_sources = set(contract_scope.get("required_source_keys") or [])
    keywords = [str(value).lower() for value in contract_scope.get("metric_keywords") or []]
    if not required_sources:
        return metrics, watermarks
    filtered_watermarks = {
        key: value for key, value in watermarks.items() if key in required_sources
    }
    filtered_metrics: dict[str, Any] = {}
    for key, value in metrics.items():
        source_key = "/".join(str(key).split("/")[:2])
        metric_label = str(key).rsplit("/", 1)[-1].lower()
        if source_key not in required_sources:
            continue
        if keywords and not any(keyword in metric_label for keyword in keywords):
            continue
        filtered_metrics[key] = value
    return filtered_metrics, filtered_watermarks


def save_suggestion_snapshot(
    task_id: str,
    context: dict[str, Any] | None = None,
    *,
    store_key: str | None = None,
    business_date: str | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{16}", str(task_id or "")):
        raise ValueError("invalid task_id")
    selected_store = str(store_key or load_agent_settings().get("store_key") or "").lower()
    if not selected_store or selected_store not in {str(item.get("key") or "") for item in list_store_identities()}:
        raise ValueError("尚未识别当前店铺，暂不写入效果基线。")
    scope = _task_scope_key(selected_store, business_date)
    storage_key = f"{scope}|{task_id}"
    with _state_lock:
        snapshots = load_suggestion_snapshots()
        existing = snapshots.get(storage_key)
        if isinstance(existing, dict):
            return {**existing, "baseline_reused": True}
        contract_scope = _suggestion_contract_scope(context)
        metrics, watermarks = _collect_suggestion_metrics(selected_store, contract_scope.get("subject"))
        metrics, watermarks = _filter_suggestion_metrics(metrics, watermarks, contract_scope)
        clean_context = {
            key: str(value or "")[:500]
            for key, value in (context or {}).items()
            if key in {"title", "owner", "action", "acceptance", "evidence", "observation_window"}
        }
        captured_at_ms = int(time.time() * 1000)
        observation_window_minutes = _observation_window_minutes(clean_context.get("observation_window"))
        metric_watermarks = {
            key: int(watermarks.get("/".join(str(key).split("/")[:2])) or 0)
            for key in metrics
        }
        entry = {
            "schema_version": 3,
            "task_id": str(task_id),
            "scope": scope,
            "store_key": selected_store,
            "business_date": scope.rsplit(":", 1)[-1],
            "created_at": _now_label(),
            "captured_at_ms": captured_at_ms,
            "observation_window_minutes": observation_window_minutes,
            "due_at_ms": captured_at_ms + observation_window_minutes * 60_000,
            "context": clean_context,
            "task_contract": contract_scope,
            "required_source_keys": contract_scope.get("required_source_keys", []),
            "metric_keywords": contract_scope.get("metric_keywords", []),
            "metrics_snapshot": metrics,
            "source_watermarks": watermarks,
            "metric_watermarks": metric_watermarks,
            "evaluated": False,
            "evaluation": None,
        }
        snapshots[storage_key] = entry
        _atomic_json_write(_suggestion_snapshots_path(), snapshots)
    return entry


def _start_product_task_run(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Mirror one exact-product task into the durable SQLite run ledger."""

    contract = entry.get("task_contract") if isinstance(entry.get("task_contract"), dict) else {}
    subject = contract.get("subject") if isinstance(contract.get("subject"), dict) else {}
    if str(subject.get("kind") or "") not in {"product", "douyin_product", "douyin_sku"}:
        return None
    entity_key = _product_subject_entity_key(subject)
    if not entity_key:
        return None
    store_key = str(entry.get("store_key") or contract.get("store_key") or "").lower()
    account_key = str(contract.get("account_key") or "").lower()
    started_at_ms = int(entry.get("captured_at_ms") or 0)
    due_at_ms = int(entry.get("due_at_ms") or started_at_ms)
    try:
        store = _local_store()
        run = store.start_commerce_task_run(
            store_key=store_key,
            account_key=account_key,
            entity_key=entity_key,
            task_key=str(contract.get("task_key") or ""),
            subject_kind=str(subject.get("kind") or "product"),
            rule_id=str(contract.get("rule_id") or "ops.product_graph"),
            contract_fingerprint=str(contract.get("contract_fingerprint") or ""),
            business_date=str(entry.get("business_date") or ""),
            started_at_ms=started_at_ms,
            due_at_ms=due_at_ms,
            contract=contract,
        )
        baseline_rows = store.query_product_history(
            store_key=store_key,
            account_key=account_key or None,
            entity_key=entity_key,
            end_ms=started_at_ms + 1,
            limit=500,
        )
        if baseline_rows:
            store.attach_commerce_task_observations(
                str(run.get("run_id") or ""),
                "baseline",
                [int(item.get("id") or 0) for item in baseline_rows if int(item.get("id") or 0) > 0],
                store_key=store_key,
                account_key=account_key,
            )
            run = store.get_commerce_task_run(
                str(run.get("run_id") or ""),
                store_key=store_key,
                account_key=account_key,
            ) or run
        storage_key = f"{entry.get('scope')}|{entry.get('task_id')}"
        with _state_lock:
            snapshots = load_suggestion_snapshots()
            stored = snapshots.get(storage_key)
            if isinstance(stored, dict):
                stored["commerce_task_run_id"] = run.get("run_id")
                stored["commerce_entity_key"] = entity_key
                stored["commerce_task_run_status"] = run.get("status")
                snapshots[storage_key] = stored
                _atomic_json_write(_suggestion_snapshots_path(), snapshots)
        return run
    except (LocalStoreError, OSError, ValueError):
        logger.exception("写入单品任务运行账本失败: %s", entry.get("task_id"))
        return None


def _sync_product_task_run(entry: dict[str, Any], evaluation: dict[str, Any]) -> None:
    run_id = str(entry.get("commerce_task_run_id") or "")
    entity_key = str(entry.get("commerce_entity_key") or "")
    if not run_id or not entity_key:
        return
    contract = entry.get("task_contract") if isinstance(entry.get("task_contract"), dict) else {}
    store_key = str(entry.get("store_key") or contract.get("store_key") or "").lower()
    account_key = str(contract.get("account_key") or "").lower()
    status = str(evaluation.get("status") or "")
    try:
        store = _local_store()
        run = store.get_commerce_task_run(
            run_id, store_key=store_key, account_key=account_key,
        )
        if not run:
            return
        if status in {"awaiting_readback", "effective", "ineffective", "inconclusive"}:
            readback_rows = store.query_product_history(
                store_key=store_key,
                account_key=account_key or None,
                entity_key=entity_key,
                start_ms=int(run.get("due_at_ms") or 0),
                end_ms=int(time.time() * 1000) + 1,
                limit=500,
            )
            if readback_rows:
                store.attach_commerce_task_observations(
                    run_id,
                    "readback",
                    [int(item.get("id") or 0) for item in readback_rows if int(item.get("id") or 0) > 0],
                    store_key=store_key,
                    account_key=account_key,
                )
        current = str(run.get("status") or "running")
        if status == "observing" and current == "running":
            run = store.transition_commerce_task_run(
                run_id, "observing", store_key=store_key, account_key=account_key,
            )
        elif status == "awaiting_readback" and current in {"running", "observing"}:
            run = store.transition_commerce_task_run(
                run_id, "awaiting_readback", store_key=store_key, account_key=account_key,
            )
        elif status in {"effective", "ineffective", "inconclusive"} and current not in {"completed", "cancelled"}:
            run = store.transition_commerce_task_run(
                run_id,
                "completed",
                store_key=store_key,
                account_key=account_key,
                completed_at_ms=int(time.time() * 1000),
                verdict=status,
                result=evaluation,
            )
        entry["commerce_task_run_status"] = run.get("status")
    except (LocalStoreError, OSError, ValueError):
        logger.exception("更新单品任务运行账本失败: %s", run_id)


def _find_suggestion_snapshot(
    snapshots: dict[str, Any],
    task_id: str,
    store_key: str,
    business_date: str | None = None,
) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    target_scope = _task_scope_key(store_key, business_date)
    exact_key = f"{target_scope}|{task_id}"
    if isinstance(snapshots.get(exact_key), dict):
        return exact_key, snapshots[exact_key]
    safe_store, target_day = target_scope.rsplit(":", 1)
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for key, item in snapshots.items():
        if not isinstance(item, dict) or str(item.get("task_id") or key) != task_id:
            continue
        scope = str(item.get("scope") or "")
        if scope:
            try:
                item_store, item_day = scope.rsplit(":", 1)
            except ValueError:
                continue
            if item_store != safe_store or item_day > target_day:
                continue
        score = int(item.get("captured_at_ms") or _label_timestamp(item.get("created_at")) * 1000)
        candidates.append((score, str(key), item))
    if not candidates:
        return None, None
    _, key, item = max(candidates, key=lambda candidate: candidate[0])
    return key, item


def _metric_effect_direction(metric: str) -> str | None:
    normalized = str(metric or "").lower()
    if any(keyword in normalized for keyword in ("退款", "退货", "取消率", "投诉")):
        return "down"
    # Cumulative orders/GMV almost always rise over time and cannot prove that
    # one task caused an improvement.  Until interval/delta contracts exist,
    # only comparable rate metrics are eligible for automatic effectiveness.
    if any(keyword in normalized for keyword in ("roi", "转化率", "点击率")):
        return "up"
    return None


def _observation_window_minutes(value: Any) -> int:
    """Turn a human observation-window label into the earliest safe readback time."""

    text = str(value or "").strip().lower()
    if not text or any(keyword in text for keyword in ("立即", "马上", "实时")):
        return 0
    candidates: list[int] = []
    for raw_value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(分钟|小时|天)", text):
        number = float(raw_value)
        multiplier = 1 if unit == "分钟" else 60 if unit == "小时" else 1440
        candidates.append(max(0, int(number * multiplier)))
    # For labels such as "2–4 小时", use the lower bound: it is the first time
    # a readback is allowed, not a promise that the effect must already exist.
    return min(candidates) if candidates else 0


def _evaluate_on_completion(
    task_id: str,
    *,
    completion_note: str = "",
    store_key: str | None = None,
    business_date: str | None = None,
) -> dict[str, Any] | None:
    selected_store = str(store_key or load_agent_settings().get("store_key") or "").lower()
    snapshots = load_suggestion_snapshots()
    storage_key, entry = _find_suggestion_snapshot(
        snapshots, str(task_id), selected_store, business_date
    )
    if not entry:
        return None
    previous_evaluation = entry.get("evaluation") if isinstance(entry.get("evaluation"), dict) else {}
    previous_status = str(previous_evaluation.get("status") or "")
    if entry.get("evaluated") and previous_status not in {"manual_verified", "observing", "awaiting_readback"}:
        return None
    old_metrics = entry.get("metrics_snapshot") if isinstance(entry.get("metrics_snapshot"), dict) else {}
    baseline_watermarks = entry.get("source_watermarks") if isinstance(entry.get("source_watermarks"), dict) else {}
    baseline_ms = int(entry.get("captured_at_ms") or _label_timestamp(entry.get("created_at")) * 1000)
    baseline_metric_watermarks = (
        entry.get("metric_watermarks") if isinstance(entry.get("metric_watermarks"), dict) else {}
    )
    if not baseline_metric_watermarks:
        baseline_metric_watermarks = {
            key: int(baseline_watermarks.get("/".join(str(key).split("/")[:2])) or baseline_ms)
            for key in old_metrics
        }
    observation_minutes = int(
        entry.get("observation_window_minutes")
        if entry.get("observation_window_minutes") is not None
        else _observation_window_minutes((entry.get("context") or {}).get("observation_window"))
    )
    due_at_ms = int(entry.get("due_at_ms") or baseline_ms + observation_minutes * 60_000)
    now_ms = int(time.time() * 1000)
    entry["observation_window_minutes"] = observation_minutes
    entry["due_at_ms"] = due_at_ms
    contract_scope = entry.get("task_contract") if isinstance(entry.get("task_contract"), dict) else {}
    current_metrics, current_watermarks = _collect_suggestion_metrics(selected_store, contract_scope.get("subject"))
    current_metrics, current_watermarks = _filter_suggestion_metrics(
        current_metrics, current_watermarks, contract_scope,
    )
    required_sources = set(contract_scope.get("required_source_keys") or [])
    expected_account = str(contract_scope.get("account_key") or "")
    current_account = str(load_agent_settings().get("qianchuan_account_key") or "")
    account_mismatch = bool(
        expected_account
        and expected_account != current_account
        and any(source_key.startswith("qianchuan/") for source_key in required_sources)
    )
    if account_mismatch:
        current_metrics, current_watermarks = {}, {}
    newer_sources = {
        source_key
        for source_key, captured_at_ms in current_watermarks.items()
        if int(captured_at_ms or 0) > int(baseline_watermarks.get(source_key) or baseline_ms)
    }
    current_metric_watermarks = {
        key: int(current_watermarks.get("/".join(str(key).split("/")[:2])) or 0)
        for key in current_metrics
    }
    newer_metric_keys = {
        key for key in old_metrics
        if key in current_metrics
        and int(current_metric_watermarks.get(key) or 0)
        > int(baseline_metric_watermarks.get(key) or baseline_ms)
    }
    changes: list[dict[str, Any]] = []
    improvements = 0
    degradations = 0
    comparable_metrics = 0
    for key, old_value in old_metrics.items():
        if key not in newer_metric_keys:
            continue
        new_value = current_metrics.get(key)
        old_num = _parse_number(old_value)
        new_num = _parse_number(new_value)
        if old_num is None or new_num is None:
            continue
        delta = new_num - old_num
        delta_pct = round(delta / abs(old_num) * 100, 1) if old_num else None
        metric = str(key).rsplit("/", 1)[-1]
        direction = _metric_effect_direction(metric)
        changes.append({
            "metric": metric,
            "old": old_num,
            "new": new_num,
            "delta": delta,
            "delta_percent": delta_pct,
            "direction": direction,
        })
        if not direction:
            continue
        comparable_metrics += 1
        # Treat small movements as noise rather than forcing an effective/
        # ineffective verdict from normal page fluctuations.
        meaningful = delta_pct is not None and abs(delta_pct) >= 5.0
        improved = meaningful and (delta > 0 if direction == "up" else delta < 0)
        degraded = meaningful and (delta < 0 if direction == "up" else delta > 0)
        improvements += int(improved)
        degradations += int(degraded)

    completion_kind = str(contract_scope.get("completion_kind") or "metric_rule")
    max_age_seconds = int(contract_scope.get("source_max_age_seconds") or 0)
    fresh_newer_sources = {
        source_key for source_key in newer_sources
        if not max_age_seconds or int(current_watermarks.get(source_key) or 0) >= int(time.time() * 1000) - max_age_seconds * 1000
    }
    if now_ms < due_at_ms:
        status = "observing"
    elif completion_kind == "data_freshness" and fresh_newer_sources:
        status = "effective"
        improvements = max(improvements, 1)
        comparable_metrics = max(comparable_metrics, 1)
    elif account_mismatch or not newer_metric_keys:
        status = "awaiting_readback"
    elif not comparable_metrics:
        status = "inconclusive"
    elif improvements > degradations:
        status = "effective"
    elif degradations > improvements:
        status = "ineffective"
    else:
        status = "inconclusive"
    entry["evaluated"] = status in {"effective", "ineffective", "inconclusive"}
    entry["evaluation"] = {
        "status": status,
        "evaluated_at": _now_label(),
        "effective": status == "effective",
        "comparable": status in {"effective", "ineffective"},
        "completion_note": str(completion_note or "")[:300],
        "due_at_ms": due_at_ms,
        "observation_window_minutes": observation_minutes,
        "newer_source_count": len(newer_sources),
        "newer_metric_count": len(newer_metric_keys),
        "missing_metric_count": max(0, len(old_metrics) - len(newer_metric_keys)),
        "required_source_count": len(required_sources),
        "account_mismatch": account_mismatch,
        "comparable_metric_count": comparable_metrics,
        "improvements": improvements,
        "degradations": degradations,
        "changes": changes[:10],
    }
    _sync_product_task_run(entry, entry["evaluation"])
    snapshots[str(storage_key)] = entry
    _atomic_json_write(_suggestion_snapshots_path(), snapshots)
    return entry


def _suggestion_effect_status(item: dict[str, Any]) -> str:
    evaluation = item.get("evaluation") if isinstance(item.get("evaluation"), dict) else {}
    status = str(evaluation.get("status") or "")
    if status in {"observing", "awaiting_readback", "manual_verified", "inconclusive", "effective", "ineffective"}:
        return status
    if item.get("evaluated"):
        # v1 only stored a boolean.  Preserve its historical meaning.
        return "effective" if evaluation.get("effective") else "ineffective"
    return "pending"


def get_effectiveness_report() -> dict[str, Any]:
    snapshots = load_suggestion_snapshots()
    current_scope = _task_scope_key()
    selected_store = current_scope.rsplit(":", 1)[0]
    # Provisional outcomes can become evidence-backed after the observation
    # deadline and a fresh readback. Final automatic verdicts stay immutable.
    for item in list(snapshots.values()):
        if not isinstance(item, dict) or _suggestion_effect_status(item) not in {
            "manual_verified", "observing", "awaiting_readback",
        }:
            continue
        scope = str(item.get("scope") or "")
        if scope and scope.rsplit(":", 1)[0] != selected_store:
            continue
        _evaluate_on_completion(
            str(item.get("task_id") or ""),
            completion_note=str((item.get("evaluation") or {}).get("completion_note") or ""),
            store_key=selected_store,
            business_date=str(item.get("business_date") or "") or None,
        )
    snapshots = load_suggestion_snapshots()
    scoped: list[dict[str, Any]] = []
    for item in snapshots.values():
        if not isinstance(item, dict):
            continue
        scope = str(item.get("scope") or "")
        if scope and scope.rsplit(":", 1)[0] != selected_store:
            continue
        scoped.append(item)
    statuses = [_suggestion_effect_status(item) for item in scoped]
    counts = {
        status: statuses.count(status)
        for status in (
            "pending", "observing", "awaiting_readback", "manual_verified",
            "inconclusive", "effective", "ineffective",
        )
    }
    comparable = counts["effective"] + counts["ineffective"]
    evaluated = counts["manual_verified"] + counts["inconclusive"] + comparable
    recent_items = sorted(
        (item for item in scoped if _suggestion_effect_status(item) != "pending"),
        key=lambda item: str((item.get("evaluation") or {}).get("evaluated_at") or item.get("created_at") or ""),
        reverse=True,
    )[:10]
    return {
        "generated_at": _now_label(),
        "total_tracked": len(scoped),
        "total_evaluated": evaluated,
        "pending_count": counts["pending"],
        "observing_count": counts["observing"],
        "awaiting_readback_count": counts["awaiting_readback"],
        "manual_verified_count": counts["manual_verified"],
        "inconclusive_count": counts["inconclusive"],
        "effective_count": counts["effective"],
        "ineffective_count": counts["ineffective"],
        "comparable_count": comparable,
        "effective_rate": round(counts["effective"] / comparable * 100, 1) if comparable else 0,
        "recent_evaluations": [
            {
                "task_id": item.get("task_id"),
                "scope": item.get("scope"),
                "store_key": item.get("store_key"),
                "title": (item.get("context") or {}).get("title"),
                "status": _suggestion_effect_status(item),
                "effective": _suggestion_effect_status(item) == "effective",
                "changes": ((item.get("evaluation") or {}).get("changes") or [])[:5],
                "completion_note": (item.get("evaluation") or {}).get("completion_note"),
                "evaluated_at": (item.get("evaluation") or {}).get("evaluated_at"),
            }
            for item in recent_items
        ],
        "mode": "read_only",
    }


def _scan_status_path() -> Path:
    return DATA_DIR / "scan_status.json"


def _scan_run_status_path(run_id: str) -> Path:
    return DATA_DIR / "scan_runs" / f"{run_id}.json"


def _scan_page_receipt_path(run_id: str, page_id: str) -> Path:
    return DATA_DIR / "scan_page_receipts" / run_id / f"{page_id}.json"


_SCAN_RESULT_ALLOWED_FIELDS = {
    "id", "label", "source", "ok", "page_type", "quality", "captured_at",
    "account_key", "account_label", "account_confidence", "account_identity_source",
    "store_key", "store_confidence", "store_identity_source", "collection_complete",
    "warning_code", "warning", "collection_pages", "pagination_truncated",
    "virtual_scroll_truncated", "error", "error_code", "explicit_empty",
}
_SCAN_QUALITY_INTEGER_LIMITS = {
    "score": 100,
    "metric_count": 1_000_000,
    "table_count": 1_000_000,
    "row_count": 10_000_000,
    "pages_scanned": 10_000,
    "virtual_scroll_passes": 100_000,
}
_SCAN_QUALITY_BOOLEAN_FIELDS = {
    "collection_complete", "pagination_truncated", "virtual_scroll_truncated",
    "explicit_empty",
}


def _validated_scan_integer(
    value: Any,
    field_name: str,
    *,
    maximum: int,
    optional: bool = True,
) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise ValueError(f"invalid scan {field_name}")
    return value


def _validated_scan_text(
    value: Any,
    field_name: str,
    *,
    maximum: int,
    optional: bool = True,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if value is None and optional:
        return ""
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"invalid scan {field_name}")
    if pattern is not None and value and not pattern.fullmatch(value):
        raise ValueError(f"invalid scan {field_name}")
    return value


def _validated_scan_quality(value: Any, field_name: str = "result quality") -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"invalid scan {field_name}")
    normalized = dict(value)
    for key, maximum in _SCAN_QUALITY_INTEGER_LIMITS.items():
        if key in normalized:
            normalized[key] = _validated_scan_integer(
                normalized[key], f"{field_name}.{key}", maximum=maximum, optional=False
            )
    for key in _SCAN_QUALITY_BOOLEAN_FIELDS:
        if key in normalized and not isinstance(normalized[key], bool):
            raise ValueError(f"invalid scan {field_name}.{key}")
    if "warnings" in normalized:
        warnings = normalized["warnings"]
        if (
            not isinstance(warnings, list)
            or len(warnings) > 100
            or any(not isinstance(item, str) or len(item) > 500 for item in warnings)
        ):
            raise ValueError(f"invalid scan {field_name}.warnings")
    return normalized


def _validated_scan_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("invalid scan result")
    unknown = set(value) - _SCAN_RESULT_ALLOWED_FIELDS
    if unknown:
        raise ValueError(f"invalid scan result fields: {', '.join(sorted(unknown))}")
    page_id = _validated_scan_text(
        value.get("id"), "result.id", maximum=48, optional=False, pattern=_SCAN_PAGE_ID_PATTERN
    )
    if not isinstance(value.get("ok"), bool):
        raise ValueError("invalid scan result.ok")
    source = value.get("source")
    expected_source = "qianchuan" if page_id.startswith("qianchuan_") else "doudian"
    if source is None or source == "":
        source = expected_source
    if source not in ALLOWED_SOURCES or source != expected_source:
        raise ValueError("invalid scan result.source")

    normalized = dict(value)
    normalized["id"] = page_id
    normalized["source"] = source
    normalized["ok"] = value["ok"]
    # Older receipts did not carry this proof. They remain visible, but are
    # deliberately incomplete until a fresh scan supplies an explicit boolean.
    collection_complete = value.get("collection_complete", False)
    if not isinstance(collection_complete, bool):
        raise ValueError("invalid scan result.collection_complete")
    normalized["collection_complete"] = collection_complete
    normalized["captured_at"] = _validated_scan_integer(
        value.get("captured_at", 0), "result.captured_at", maximum=9_999_999_999_999,
        optional=False,
    )
    normalized["quality"] = _validated_scan_quality(value.get("quality"))

    if "page_type" in value:
        normalized["page_type"] = _validated_scan_text(
            value.get("page_type"), "result.page_type", maximum=48,
            pattern=_SCAN_PAGE_ID_PATTERN,
        )
    for key_name in ("store_key", "account_key"):
        if key_name in value:
            normalized[key_name] = _validated_scan_text(
                value.get(key_name), f"result.{key_name}", maximum=128,
                pattern=SAFE_KEY,
            ).lower()
    for key_name, maximum in (
        ("label", 80), ("account_label", 80), ("account_confidence", 32),
        ("account_identity_source", 64), ("store_confidence", 32),
        ("store_identity_source", 64), ("warning", 500), ("error", 500),
    ):
        if key_name in value:
            normalized[key_name] = _validated_scan_text(
                value.get(key_name), f"result.{key_name}", maximum=maximum
            )
    code_pattern = re.compile(r"[A-Z0-9_]{0,80}")
    for key_name in ("warning_code", "error_code"):
        if key_name in value:
            normalized[key_name] = _validated_scan_text(
                value.get(key_name), f"result.{key_name}", maximum=80,
                pattern=code_pattern,
            )
    for key_name in ("pagination_truncated", "virtual_scroll_truncated", "explicit_empty"):
        if key_name in value and not isinstance(value[key_name], bool):
            raise ValueError(f"invalid scan result.{key_name}")
    if "collection_pages" in value:
        normalized["collection_pages"] = _validated_scan_integer(
            value["collection_pages"], "result.collection_pages", maximum=10_000,
            optional=False,
        )
    return normalized


def _scan_status_corrupt() -> dict[str, Any]:
    return {
        "status": "error",
        "scope": "full",
        "coverage_complete": False,
        "index": 0,
        "total": 0,
        "success": 0,
        "failed": 0,
        "low_quality": 0,
        "results": [],
        "run_id": "",
        "revision": 0,
        "error": "巡检状态文件无法读取或格式无效，请重新发起巡店。",
        "error_code": "SCAN_STATUS_CORRUPT",
    }


def _validated_persisted_scan_status(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("scan status must be an object")
    normalized = dict(value)
    if normalized.get("status") not in {
        "idle", "running", "completed", "partial", "cancelled", "interrupted", "error"
    }:
        raise ValueError("invalid scan status")
    if normalized.get("scope") not in {None, "full", "quick", "product_graph"}:
        raise ValueError("invalid scan scope")
    results = normalized.get("results", [])
    if not isinstance(results, list) or len(results) > 100:
        raise ValueError("invalid scan results")
    normalized["results"] = [_validated_scan_result(item) for item in results]
    if len({item["id"] for item in normalized["results"]}) != len(normalized["results"]):
        raise ValueError("invalid scan results: duplicate page id")
    if "coverage_complete" in normalized and not isinstance(normalized["coverage_complete"], bool):
        raise ValueError("invalid scan coverage_complete")
    normalized["coverage_complete"] = normalized.get("coverage_complete") is True
    for key_name in ("index", "total", "success", "failed", "low_quality", "revision"):
        if key_name in normalized:
            normalized[key_name] = _validated_scan_integer(
                normalized[key_name], key_name, maximum=1_000_000, optional=False
            )
    for key_name in ("started_at", "finished_at", "heartbeat_at", "root_started_at", "interrupted_at"):
        if key_name in normalized:
            normalized[key_name] = _validated_scan_integer(
                normalized[key_name], key_name, maximum=9_999_999_999_999
            )
    if "owned_tab_id" in normalized:
        normalized["owned_tab_id"] = _validated_scan_integer(
            normalized["owned_tab_id"], "owned_tab_id", maximum=2_147_483_647
        )
    for key_name in ("store_key", "account_key"):
        if key_name in normalized:
            normalized[key_name] = _validated_scan_text(
                normalized.get(key_name), key_name, maximum=128, pattern=SAFE_KEY
            ).lower()
    return normalized


def _snapshot_push_response(source: str, saved: dict[str, Any], **extra: Any) -> dict[str, Any]:
    saved_data = saved.get("data", {}) if isinstance(saved.get("data"), dict) else {}
    quarantine = saved_data.get("quarantine") if isinstance(saved_data.get("quarantine"), dict) else {}
    quarantined = quarantine.get("active") is True
    identity_resolution = str(saved_data.get("identity_resolution") or "")
    response = {
        "ok": not quarantined,
        "accepted": not quarantined,
        "accepted_for_current_data": not quarantined,
        "current_snapshot_updated": not quarantined,
        "forensic_saved": quarantined,
        "source": source,
        "page_type": saved["page_type"],
        "account": saved_data.get("account") if isinstance(saved_data.get("account"), dict) else None,
        "store": saved_data.get("store") if isinstance(saved_data.get("store"), dict) else None,
        "identity_resolution": identity_resolution,
        "quarantined": quarantined,
        **extra,
    }
    if quarantined:
        error_code = (
            "ACCOUNT_BINDING_REVOKED"
            if identity_resolution == "binding_revoked"
            else "ACCOUNT_BINDING_EPOCH_STALE"
            if identity_resolution == "binding_epoch_stale"
            else "STORE_IDENTITY_CONFLICT"
        )
        response.update({
            "error_code": error_code,
            "error": "Candidate snapshot was preserved for review but rejected from current operating data.",
            "retryable": False,
        })
    return response


def issue_current_page_store_grant() -> dict[str, Any]:
    """Issue one short-lived, in-memory capability for the next current-page capture."""

    now_ms = int(time.time() * 1000)
    with _current_page_store_grant_lock:
        for token, grant in list(_current_page_store_grants.items()):
            if int(grant.get("expires_at_ms") or 0) < now_ms:
                _current_page_store_grants.pop(token, None)
        while len(_current_page_store_grants) >= CURRENT_PAGE_STORE_GRANT_LIMIT:
            oldest = min(
                _current_page_store_grants,
                key=lambda token: int(_current_page_store_grants[token].get("issued_at_ms") or 0),
            )
            _current_page_store_grants.pop(oldest, None)
        token = secrets.token_hex(32)
        grant = {
            "issued_at_ms": now_ms,
            "expires_at_ms": now_ms + CURRENT_PAGE_STORE_GRANT_TTL_MS,
        }
        _current_page_store_grants[token] = grant
    return {
        "current_page_token": token,
        "issued_at_ms": grant["issued_at_ms"],
        "expires_at_ms": grant["expires_at_ms"],
        "purpose": "confirm_current_doudian_overview",
    }


def _consume_current_page_store_grant(token: Any) -> dict[str, int] | None:
    token = str(token or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", token):
        return None
    now_ms = int(time.time() * 1000)
    with _current_page_store_grant_lock:
        grant = _current_page_store_grants.pop(token, None)
    if not isinstance(grant, dict) or int(grant.get("expires_at_ms") or 0) < now_ms:
        return None
    return {
        "issued_at_ms": int(grant.get("issued_at_ms") or 0),
        "expires_at_ms": int(grant.get("expires_at_ms") or 0),
    }


def _explicit_capture_timestamp_ms(data: dict[str, Any]) -> int:
    value = data.get("captured_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return 0
    captured_at_ms = int(value)
    return captured_at_ms if float(value) == captured_at_ms else 0


@_guard_production_scope_change("auto_selected_store_changed")
@_guard_binding_state_mutation
def _select_auto_confirmed_store_context(store_key: str) -> dict[str, Any]:
    """Commit selection + onboarding with rollback and crash-recovery journaling."""

    with _state_lock:
        settings_existed = _settings_path().exists()
        onboarding_existed = _onboarding_state_path().exists()
        previous_settings = load_agent_settings()
        previous_onboarding = _load_onboarding_state()
        journal = {
            "schema_version": 1,
            "phase": "prepared",
            "prepared_at_ms": int(time.time() * 1000),
            "store_key": store_key,
            "previous": {
                "settings": previous_settings,
                "onboarding": previous_onboarding,
                "settings_existed": settings_existed,
                "onboarding_existed": onboarding_existed,
            },
        }
        _atomic_json_write(_auto_store_context_transaction_path(), journal)
        _auto_store_context_transaction_local.active = True
        try:
            # The automatic transaction already owns audit -> binding.  Calling
            # the public decorated selector here would reacquire the audit OS
            # lock and could self-deadlock.
            catalog = _select_store_context_locked(store_key, "")
            _atomic_json_write(
                _auto_store_context_transaction_path(),
                {**journal, "phase": "committed", "committed_at_ms": int(time.time() * 1000)},
            )
            _clear_auto_store_context_transaction()
            return catalog
        except Exception:
            try:
                _restore_auto_store_context_files(
                    previous_settings,
                    previous_onboarding,
                    settings_existed=settings_existed,
                    onboarding_existed=onboarding_existed,
                )
                _clear_auto_store_context_transaction()
            except OSError as rollback_error:
                raise OSError("automatic store confirmation failed and rollback was unavailable") from rollback_error
            raise
        finally:
            _auto_store_context_transaction_local.active = False


def _auto_confirm_current_doudian_store(
    source: str,
    saved: dict[str, Any],
    grant: dict[str, int] | None,
    explicit_captured_at_ms: int,
) -> dict[str, Any] | None:
    """Select only the exact shop proven by this fresh current-page overview.

    The store catalog is intentionally never consulted to choose a candidate.
    It is only used by ``select_store_context`` to validate and commit the
    already-proven key with the existing execution-scope invalidation rules.
    """

    if (
        not isinstance(grant, dict)
        or source != "doudian"
        or str(saved.get("page_type") or "") != "overview"
    ):
        return None
    data = saved.get("data") if isinstance(saved.get("data"), dict) else {}
    quarantine = data.get("quarantine") if isinstance(data.get("quarantine"), dict) else {}
    store = data.get("store") if isinstance(data.get("store"), dict) else {}
    captured_at_ms = int(data.get("captured_at") or 0)
    now_ms = int(time.time() * 1000)
    issued_at_ms = int(grant.get("issued_at_ms") or 0)
    expires_at_ms = int(grant.get("expires_at_ms") or 0)
    if (
        quarantine.get("active") is True
        or str(data.get("identity_resolution") or "") != "resolved"
        or not str(data.get("reason") or "").startswith("manual-current-page-")
        or captured_at_ms <= 0
        or explicit_captured_at_ms != captured_at_ms
        or issued_at_ms <= 0
        or expires_at_ms <= now_ms
        or captured_at_ms < issued_at_ms - CURRENT_PAGE_CAPTURE_FUTURE_TOLERANCE_MS
        or captured_at_ms > now_ms + CURRENT_PAGE_CAPTURE_FUTURE_TOLERANCE_MS
        or now_ms - captured_at_ms >= STALE_SECONDS * 1000
        or str(store.get("confidence") or "") != "high"
        or str(store.get("identity_source") or "") != "hmac_douyin_shop_id"
        or str(store.get("evidence_source") or "") not in CURRENT_PAGE_STORE_EVIDENCE_SOURCES
    ):
        return None
    store_key = str(store.get("key") or "").strip().lower()
    if not SAFE_KEY.fullmatch(store_key):
        return None

    catalog = _select_auto_confirmed_store_context(store_key)
    if str(catalog.get("selected_store_key") or "").strip().lower() != store_key:
        raise OSError("fresh Doudian overview store context was not committed")
    return catalog


def save_scan_page_once(
    source: str,
    data: dict[str, Any],
    expected_scope: dict[str, Any] | None = None,
    scan_context: dict[str, Any] | None = None,
    execution_context: dict[str, Any] | None = None,
    current_page_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expected_scope = expected_scope if isinstance(expected_scope, dict) else {}
    scan_context = scan_context if isinstance(scan_context, dict) else {}
    execution_context = execution_context if isinstance(execution_context, dict) else {}
    current_page_context = current_page_context if isinstance(current_page_context, dict) else {}
    run_id = str(scan_context.get("run_id") or "").lower()
    page_id = str(scan_context.get("page_id") or "").lower()
    explicit_captured_at_ms = _explicit_capture_timestamp_ms(data) if isinstance(data, dict) else 0
    consumed_current_page_grant = _consume_current_page_store_grant(
        current_page_context.get("current_page_token")
    ) if current_page_context else None
    auto_confirm_grant = (
        consumed_current_page_grant
        if not run_id and not page_id and not execution_context
        else None
    )
    snapshot_quality = data.get("quality") if isinstance(data, dict) and isinstance(data.get("quality"), dict) else {}
    if snapshot_quality.get("targeted") is True and not execution_context:
        raise ValueError("定向计划快照必须绑定执行上下文，不能覆盖全量经营数据。")
    if execution_context:
        if run_id or page_id:
            raise ValueError("执行定向快照不能借用巡店运行范围。")
        return save_targeted_execution_snapshot(source, data, expected_scope, execution_context)
    if not run_id:
        saved = save_data(
            source,
            data,
            expected_store_key=str(expected_scope.get("store_key") or ""),
            expected_account_key=str(expected_scope.get("account_key") or ""),
        )
    else:
        if not re.fullmatch(r"[a-z0-9_-]{8,80}", run_id) or not re.fullmatch(r"[a-z0-9_-]{1,48}", page_id):
            raise ValueError("invalid scan page context")
        receipt_path = _scan_page_receipt_path(run_id, page_id)
        scope_fingerprint = hashlib.sha256(json.dumps({
            "source": source,
            "page_id": page_id,
            "store_key": str(expected_scope.get("store_key") or "").lower(),
            "account_key": str(expected_scope.get("account_key") or "").lower(),
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        with _scan_push_lock, _state_lock:
            try:
                existing = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else None
            except (OSError, json.JSONDecodeError):
                existing = None
            if isinstance(existing, dict) and existing.get("committed") is True:
                if existing.get("scope_fingerprint") != scope_fingerprint:
                    raise ValueError("SCAN_PAGE_SCOPE_CONFLICT: 页面回执范围与本轮巡店不一致。")
                response = existing.get("response")
                if isinstance(response, dict):
                    return {**response, "idempotent_replay": True}
            current_scan = _read_scan_status_file()
            current_run_id = str(current_scan.get("run_id") or "").lower()
            if not current_run_id or current_run_id != run_id:
                raise ValueError("STALE_SCAN_RUN: 旧巡店页面回写已拒绝。")
            if current_scan.get("status") != "running":
                raise ValueError("SCAN_NOT_WRITABLE: 本轮巡店已停止，迟到页面不会写入。")
            planned_page_ids = (
                current_scan.get("planned_page_ids")
                if isinstance(current_scan.get("planned_page_ids"), list)
                else []
            )
            execution_page_ids = (
                current_scan.get("execution_page_ids")
                if isinstance(current_scan.get("execution_page_ids"), list)
                else []
            )
            if planned_page_ids and page_id not in planned_page_ids:
                raise ValueError("SCAN_PAGE_OUTSIDE_ROOT_SCOPE: 页面不属于本轮巡店原始范围，候选数据未写入。")
            if execution_page_ids and page_id not in execution_page_ids:
                raise ValueError("SCAN_PAGE_OUTSIDE_EXECUTION_SCOPE: 页面不属于本次执行范围，候选数据未写入。")

            # The active server checkpoint is the shop/account lease.  A stale
            # tab must not be able to replace it by changing the client-supplied
            # expected_scope and then presenting matching data from another
            # shop.  Empty account scopes are compared too: they mean that this
            # run has not locked a Qianchuan account yet, not "accept any".
            leased_store_key = str(current_scan.get("store_key") or "").lower()
            leased_account_key = str(current_scan.get("account_key") or "").lower()
            requested_store_key = str(expected_scope.get("store_key") or "").strip().lower()
            requested_account_key = str(expected_scope.get("account_key") or "").strip().lower()
            if requested_store_key != leased_store_key:
                raise ValueError("SCAN_LEASE_STORE_MISMATCH: 页面回写店铺与本轮巡店租约不一致，候选数据未写入。")
            if requested_account_key != leased_account_key:
                raise ValueError("SCAN_LEASE_ACCOUNT_MISMATCH: 页面回写千川账户与本轮巡店租约不一致，候选数据未写入。")
            saved = save_data(
                source,
                data,
                expected_store_key=leased_store_key,
                expected_account_key=leased_account_key,
            )
            response = _snapshot_push_response(
                source,
                saved,
                run_id=run_id,
                page_id=page_id,
            )
            if response["quarantined"]:
                # The forensic record is durable, but this scan page never
                # committed current data and therefore must not gain an
                # idempotent success receipt.
                return response
            _atomic_json_write(receipt_path, {
                "schema_version": 1,
                "committed": True,
                "committed_at": _now_label(),
                "scope_fingerprint": scope_fingerprint,
                "response": response,
            })
            return response
    response = _snapshot_push_response(source, saved)
    catalog = _auto_confirm_current_doudian_store(
        source,
        saved,
        auto_confirm_grant,
        explicit_captured_at_ms,
    )
    if catalog is not None:
        response.update({
            "store_auto_confirmed": True,
            "selected_store_key": str(catalog.get("selected_store_key") or ""),
            "selected_account_key": str(catalog.get("selected_account_key") or ""),
        })
    return response


def _read_scan_status_file() -> dict[str, Any]:
    path = _scan_status_path()
    if not path.exists():
        return {"status": "idle", "index": 0, "total": 18, "success": 0, "failed": 0, "results": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return _validated_persisted_scan_status(value)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return _scan_status_corrupt()


_SCAN_RUN_ID_PATTERN = re.compile(r"[a-z0-9_-]{8,80}")
_SCAN_PAGE_ID_PATTERN = re.compile(r"[a-z0-9_-]{1,48}")
_SCAN_RECOVERY_STATES = {
    "complete", "running", "cancelled", "blocked_login", "blocked_identity",
    "blocked_page", "blocked_evidence", "partial_collection", "retryable",
    "paused_interrupted", "needs_review",
}
_SCAN_RECOVERY_ACTIONS = {
    "none", "start_new_scan", "login_then_resume", "confirm_account_then_resume",
    "switch_account_then_resume", "confirm_store_then_resume", "switch_store_then_resume",
    "select_store_then_resume", "open_expected_page_then_resume",
    "refresh_or_narrow_then_resume", "narrow_filters_then_resume",
    "refresh_then_resume", "check_network_then_resume", "wait_then_resume",
    "resume_failed_pages", "reload_extension_then_resume", "review_then_resume",
}


def _validated_scan_run_id(value: Any, field_name: str, *, optional: bool = True) -> str:
    if value is None or value == "":
        if optional:
            return ""
        raise ValueError(f"invalid scan {field_name}")
    if not isinstance(value, str):
        raise ValueError(f"invalid scan {field_name}")
    normalized = value.strip().lower()
    if normalized != value or not _SCAN_RUN_ID_PATTERN.fullmatch(normalized):
        raise ValueError(f"invalid scan {field_name}")
    return normalized


def _validated_scan_page_ids(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError(f"invalid {field_name}")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _SCAN_PAGE_ID_PATTERN.fullmatch(item):
            raise ValueError(f"invalid {field_name}")
        if item not in normalized:
            normalized.append(item)
    return normalized


def _validated_scan_timestamp(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 9_999_999_999_999:
        raise ValueError(f"invalid {field_name}")
    return value


def _validated_scan_recovery(value: Any) -> dict[str, Any]:
    allowed = {
        "schema_version", "state", "error_code", "action", "message",
        "automatic_resume", "requires_user_action", "can_resume", "resume_page_ids",
    }
    required = allowed
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("invalid scan recovery")
    if isinstance(value.get("schema_version"), bool) or value.get("schema_version") != 1:
        raise ValueError("invalid scan recovery schema_version")
    state = value.get("state")
    action = value.get("action")
    error_code = value.get("error_code")
    message = value.get("message")
    if not isinstance(state, str) or state not in _SCAN_RECOVERY_STATES:
        raise ValueError("invalid scan recovery state")
    if not isinstance(action, str) or action not in _SCAN_RECOVERY_ACTIONS:
        raise ValueError("invalid scan recovery action")
    if not isinstance(error_code, str) or not re.fullmatch(r"[A-Z0-9_]{0,80}", error_code):
        raise ValueError("invalid scan recovery error_code")
    if not isinstance(message, str) or len(message) > 500:
        raise ValueError("invalid scan recovery message")
    for field_name in ("automatic_resume", "requires_user_action", "can_resume"):
        if not isinstance(value.get(field_name), bool):
            raise ValueError(f"invalid scan recovery {field_name}")
    if value["automatic_resume"] is not False:
        raise ValueError("invalid scan recovery automatic_resume")
    normalized = dict(value)
    normalized["resume_page_ids"] = _validated_scan_page_ids(value.get("resume_page_ids"), "recovery.resume_page_ids")
    if state == "complete" and normalized["resume_page_ids"]:
        raise ValueError("invalid scan recovery complete state")
    return normalized


def save_scan_status(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("scan status must be an object")
    allowed = {
        "status", "scope", "targeted_page_ids", "planned_page_ids", "attempted_page_ids",
        "pending_page_ids", "coverage_complete", "reason", "store_key", "account_mode",
        "account_key", "account_label", "started_at", "finished_at", "heartbeat_at",
        "current", "index", "total", "success", "failed", "low_quality", "results",
        "error", "error_code", "run_id", "revision", "owned_tab_id", "resumed_from_run_id",
        "root_run_id", "root_started_at", "parent_run_id", "execution_page_ids", "recovery",
        "interrupted", "interrupted_at", "interruption_reason",
    }
    status = {key: value[key] for key in allowed if key in value}
    if status.get("status") not in {"idle", "running", "completed", "partial", "cancelled", "interrupted", "error"}:
        raise ValueError("invalid scan status")
    status = _validated_persisted_scan_status(status)
    results = status.get("results", [])
    result_page_ids: set[str] = set()
    for result in results:
        if result.get("id"):
            if result["id"] in result_page_ids:
                raise ValueError("invalid scan results: duplicate page id")
            result_page_ids.add(result["id"])
    if status.get("scope") not in {None, "full", "quick", "product_graph"}:
        raise ValueError("invalid scan scope")
    for key_name in ("store_key", "account_key"):
        key_value = str(status.get(key_name) or "").lower()
        if key_value and not SAFE_KEY.fullmatch(key_value):
            raise ValueError(f"invalid {key_name}")
        status[key_name] = key_value
    for result in results:
        result_store_key = str(result.get("store_key") or "").lower()
        result_account_key = str(result.get("account_key") or "").lower()
        if result_store_key and status["store_key"] and result_store_key != status["store_key"]:
            raise ValueError("invalid scan result.store_key: scope mismatch")
        if (
            result.get("source") == "qianchuan"
            and result_account_key
            and status["account_key"]
            and result_account_key != status["account_key"]
        ):
            raise ValueError("invalid scan result.account_key: scope mismatch")
    status["account_label"] = _private_alias("account", status["account_key"]) if status.get("account_key") else ""
    for page_list_name in (
        "targeted_page_ids", "planned_page_ids", "execution_page_ids",
        "attempted_page_ids", "pending_page_ids",
    ):
        if page_list_name in status:
            status[page_list_name] = _validated_scan_page_ids(status[page_list_name], page_list_name)
    run_id = _validated_scan_run_id(status.get("run_id"), "run_id")
    status["run_id"] = run_id
    root_run_id_supplied = "root_run_id" in status
    parent_run_id_supplied = "parent_run_id" in status
    resumed_run_id_supplied = "resumed_from_run_id" in status
    root_started_at_supplied = "root_started_at" in status
    root_run_id = _validated_scan_run_id(status.get("root_run_id"), "root_run_id")
    parent_run_id = _validated_scan_run_id(status.get("parent_run_id"), "parent_run_id")
    resumed_from_run_id = _validated_scan_run_id(status.get("resumed_from_run_id"), "resumed_from_run_id")
    if (parent_run_id or resumed_from_run_id) and parent_run_id != resumed_from_run_id:
        raise ValueError("invalid scan recovery parent linkage")
    if "root_started_at" in status:
        status["root_started_at"] = _validated_scan_timestamp(status.get("root_started_at"), "root_started_at")
    if not run_id and (root_run_id or parent_run_id or resumed_from_run_id or root_started_at_supplied):
        raise ValueError("invalid scan lineage without run_id")
    if "interrupted_at" in status:
        status["interrupted_at"] = _validated_scan_timestamp(status.get("interrupted_at"), "interrupted_at")
    if "interrupted" in status and not isinstance(status.get("interrupted"), bool):
        raise ValueError("invalid interrupted")
    if "interruption_reason" in status:
        interruption_reason = status.get("interruption_reason")
        if not isinstance(interruption_reason, str) or len(interruption_reason) > 120 or not re.fullmatch(r"[A-Za-z0-9 _.-]*", interruption_reason):
            raise ValueError("invalid interruption_reason")
    if "recovery" in status:
        status["recovery"] = _validated_scan_recovery(status["recovery"])
    planned_page_ids = status.get("planned_page_ids") if isinstance(status.get("planned_page_ids"), list) else []
    planned_set = set(planned_page_ids)
    for subset_name in ("execution_page_ids", "attempted_page_ids", "pending_page_ids"):
        subset = status.get(subset_name) if isinstance(status.get(subset_name), list) else []
        if planned_set and any(page_id not in planned_set for page_id in subset):
            raise ValueError(f"invalid {subset_name}: page is outside planned scope")
    recovery = status.get("recovery") if isinstance(status.get("recovery"), dict) else None
    if recovery and planned_set and any(page_id not in planned_set for page_id in recovery["resume_page_ids"]):
        raise ValueError("invalid scan recovery: page is outside planned scope")
    status["revision"] = max(0, int(status.get("revision") or 0))
    immutable_terminal_states = {"completed", "partial", "cancelled", "error"}
    with _state_lock:
        current = _read_scan_status_file()
        current_run_id = str(current.get("run_id") or "").lower()
        current_root_run_id = str(current.get("root_run_id") or current_run_id).lower()
        current_revision = int(current.get("revision") or 0)
        same_run = bool(run_id and current_run_id == run_id)
        if same_run:
            if not root_run_id_supplied:
                root_run_id = current_root_run_id
            elif current_root_run_id and root_run_id != current_root_run_id:
                raise ValueError("SCAN_ROOT_CONFLICT: 当前尝试不能更换根轮。")
            if not parent_run_id_supplied:
                parent_run_id = str(current.get("parent_run_id") or "").lower()
            if not resumed_run_id_supplied:
                resumed_from_run_id = str(current.get("resumed_from_run_id") or "").lower()
            if not root_started_at_supplied and "root_started_at" in current:
                status["root_started_at"] = current.get("root_started_at")
            elif root_started_at_supplied and current.get("root_started_at") not in {None, status.get("root_started_at")}:
                raise ValueError("SCAN_ROOT_CONFLICT: 当前尝试不能更换根轮时间。")
            current_planned = current.get("planned_page_ids") if isinstance(current.get("planned_page_ids"), list) else []
            if current_planned and planned_page_ids and planned_page_ids != current_planned:
                raise ValueError("SCAN_SCOPE_CONFLICT: 当前尝试不能更换根轮页面范围。")
            if status["revision"] < current_revision:
                raise ValueError("STALE_SCAN_REVISION: 旧巡店检查点已拒绝。")
            current_status = str(current.get("status") or "")
            next_status = str(status.get("status") or "")
            if current_status in immutable_terminal_states and next_status != current_status:
                raise ValueError("SCAN_TERMINAL_STATE: 已结束巡店状态不能被后续结果覆盖。")
            if current_status == "interrupted" and next_status == "running":
                raise ValueError("SCAN_TERMINAL_STATE: 已中断巡店必须通过新的恢复轮次继续。")
        elif run_id and current_run_id and status.get("status") != "running":
            raise ValueError("STALE_SCAN_RUN: 旧巡店结果已拒绝，当前任务不受影响。")
        if run_id:
            root_run_id = root_run_id or run_id
            if parent_run_id or resumed_from_run_id:
                if parent_run_id != resumed_from_run_id or parent_run_id == run_id or root_run_id == run_id:
                    raise ValueError("invalid scan recovery lineage")
                if same_run:
                    if str(current.get("parent_run_id") or "").lower() != parent_run_id:
                        raise ValueError("SCAN_PARENT_MISMATCH: 当前尝试不能更换父轮。")
                else:
                    if current_run_id != parent_run_id:
                        raise ValueError("SCAN_PARENT_MISMATCH: 续跑父轮不是当前检查点。")
                    if current_root_run_id and root_run_id != current_root_run_id:
                        raise ValueError("SCAN_ROOT_CONFLICT: 续跑不属于当前根轮。")
                    current_planned = current.get("planned_page_ids") if isinstance(current.get("planned_page_ids"), list) else []
                    if current_planned and planned_page_ids != current_planned:
                        raise ValueError("SCAN_SCOPE_CONFLICT: 续跑不能改变根轮页面范围。")
                    current_root_started_at = current.get("root_started_at", current.get("started_at"))
                    if current_root_started_at and status.get("root_started_at") != current_root_started_at:
                        raise ValueError("SCAN_ROOT_CONFLICT: 续跑根轮时间不一致。")
            elif root_run_id != run_id:
                raise ValueError("invalid scan root lineage")
            elif (
                status.get("root_started_at") is not None
                and status.get("started_at") is not None
                and status.get("root_started_at") != status.get("started_at")
            ):
                raise ValueError("SCAN_ROOT_CONFLICT: 根轮开始时间与本轮不一致。")
            status["root_run_id"] = root_run_id
            status["parent_run_id"] = parent_run_id
            status["resumed_from_run_id"] = resumed_from_run_id
            if "root_started_at" not in status and status.get("started_at") is not None:
                status["root_started_at"] = _validated_scan_timestamp(status.get("started_at"), "started_at")
        if same_run and status["revision"] == current_revision:
            if status != current:
                raise ValueError("SCAN_REVISION_CONFLICT: 相同巡店版本携带了不同状态，旧检查点已拒绝。")
            return current
        _atomic_json_write(_scan_status_path(), status)
        if run_id:
            _atomic_json_write(_scan_run_status_path(run_id), status)
    try:
        _promote_scan_health_baselines(status)
    except (OSError, TypeError, ValueError):
        logger.exception("完整巡检已保存，但选择器健康基线更新失败")
    return status


def load_scan_status() -> dict[str, Any]:
    value = _read_scan_status_file()
    if value.get("status") == "running":
        heartbeat_at = int(value.get("heartbeat_at") or value.get("started_at") or 0)
        now_ms = int(time.time() * 1000)
        if heartbeat_at and now_ms - heartbeat_at > 3 * 60 * 1000:
            value = {
                **value,
                "status": "interrupted",
                "finished_at": now_ms,
                "error": "巡店心跳已中断，浏览器恢复后将从未完成页面继续。",
                "error_code": "SCAN_LEASE_EXPIRED",
                "revision": int(value.get("revision") or 0) + 1,
            }
            with _state_lock:
                latest = _read_scan_status_file()
                if latest.get("run_id") == value.get("run_id") and latest.get("status") == "running":
                    _atomic_json_write(_scan_status_path(), value)
                    if value.get("run_id"):
                        _atomic_json_write(_scan_run_status_path(str(value["run_id"])), value)
                else:
                    value = latest
    return value


def build_scan_receipt() -> dict[str, Any]:
    """Turn the last browser scan into an operator-readable data receipt."""
    scan = load_scan_status()
    raw_results = scan.get("results") if isinstance(scan.get("results"), list) else []
    scan_scope = str(scan.get("scope") or "")

    def valid_page_ids(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(
            str(item)
            for item in value
            if re.fullmatch(r"[a-z0-9_-]{1,48}", str(item or ""))
        ))

    reported_planned_page_ids = valid_page_ids(scan.get("planned_page_ids"))
    if not reported_planned_page_ids:
        reported_planned_page_ids = valid_page_ids(scan.get("targeted_page_ids"))
    if not reported_planned_page_ids:
        reported_planned_page_ids = valid_page_ids([
            item.get("id") for item in raw_results if isinstance(item, dict)
        ])
    run_id = str(scan.get("run_id") or "")
    root_run_id = str(scan.get("root_run_id") or run_id)
    parent_run_id = str(scan.get("parent_run_id") or "")
    resumed_from_run_id = str(scan.get("resumed_from_run_id") or "")
    execution_page_ids = valid_page_ids(scan.get("execution_page_ids"))
    recovery_attempt = bool(run_id and root_run_id and run_id != root_run_id)
    lineage_verified = bool(
        run_id
        and root_run_id
        and (
            (not recovery_attempt and root_run_id == run_id and not parent_run_id and not resumed_from_run_id)
            or (
                recovery_attempt
                and parent_run_id
                and parent_run_id == resumed_from_run_id
                and parent_run_id != run_id
            )
        )
    )
    recovery_contract: dict[str, Any] | None = None
    if isinstance(scan.get("recovery"), dict):
        try:
            recovery_contract = _validated_scan_recovery(scan["recovery"])
        except ValueError:
            recovery_contract = None

    explicit_full_scope = scan_scope == "full"
    includes_ads = bool(scan.get("account_key")) or any(
        page_id in SCAN_ADS_OPTIONAL_PAGE_IDS for page_id in reported_planned_page_ids
    )
    expected_page_ids = (
        list(SCAN_FULL_DOUDIAN_PAGE_IDS)
        + (list(SCAN_ADS_OPTIONAL_PAGE_IDS) if includes_ads else [])
        if explicit_full_scope
        else list(reported_planned_page_ids)
    )
    missing_contract_page_ids = (
        [page_id for page_id in expected_page_ids if page_id not in set(reported_planned_page_ids)]
        if explicit_full_scope
        else []
    )
    scope_contract_complete = not missing_contract_page_ids
    if expected_page_ids:
        raw_by_id = {
            str(item.get("id") or ""): item
            for item in raw_results
            if isinstance(item, dict) and item.get("id")
        }
        raw_results = [raw_by_id[page_id] for page_id in expected_page_ids if page_id in raw_by_id]

    results: list[dict[str, Any]] = []
    source_totals = {
        "doudian": {"label": "抖店", "total": 0, "success": 0, "failed": 0, "needs_review": 0},
        "qianchuan": {"label": "千川", "total": 0, "success": 0, "failed": 0, "needs_review": 0},
    }
    if expected_page_ids:
        for page_id in expected_page_ids:
            source_totals["qianchuan" if page_id.startswith("qianchuan_") else "doudian"]["total"] += 1
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
        ok = raw.get("ok") is True
        collection_complete = raw.get("collection_complete") is True
        needs_review = ok and (not collection_complete or quality_score < 70)
        if not expected_page_ids:
            source_totals[source]["total"] += 1
        if not ok:
            source_totals[source]["failed"] += 1
        elif collection_complete:
            source_totals[source]["success"] += 1
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
                "collection_complete": collection_complete,
                "page_type": str(raw.get("page_type") or "")[:48],
                "quality_score": quality_score,
                "metric_count": max(0, int(quality.get("metric_count", 0) or 0)),
                "row_count": max(0, int(quality.get("row_count", 0) or 0)),
                "needs_review": needs_review,
                "error": str(raw.get("error") or "")[:300],
                "captured_at": int(raw.get("captured_at", 0) or 0),
            }
        )

    total = len(expected_page_ids) if expected_page_ids else max(int(scan.get("total", 0) or 0), len(results))
    success = sum(1 for item in results if item["ok"] and item["collection_complete"])
    failed = sum(1 for item in results if not item["ok"])
    needs_review = sum(1 for item in results if item["needs_review"])
    completed = len(results)
    coverage_rate = round(completed / total * 100) if total else 0
    status = str(scan.get("status") or "idle")
    attempted_page_ids = valid_page_ids(scan.get("attempted_page_ids"))
    if not attempted_page_ids:
        attempted_page_ids = [item["id"] for item in results]
    pending_page_ids = list(dict.fromkeys(
        valid_page_ids(scan.get("pending_page_ids"))
        + [page_id for page_id in expected_page_ids if page_id not in set(attempted_page_ids)]
    ))
    quick_scope = scan_scope == "quick"
    product_graph_scope = scan_scope == "product_graph"
    if status == "running":
        readiness = "running"
        readiness_label = "正在补齐单品经营链" if product_graph_scope else "正在采集首诊断数据" if quick_scope else "正在采集"
    elif not results:
        readiness = "empty"
        readiness_label = "等待巡查"
    elif status == "completed" and quick_scope and not failed and not needs_review:
        readiness = "quick_ready"
        readiness_label = "首诊断数据已就绪"
    elif status == "completed" and product_graph_scope and not failed and not needs_review:
        readiness = "product_graph_ready"
        readiness_label = "单品经营链数据已补齐"
    elif status == "completed" and scope_contract_complete and not failed and not needs_review and coverage_rate == 100:
        readiness = "ready"
        readiness_label = "数据可用于分析"
    else:
        readiness = "attention"
        readiness_label = "需要补采或复核"

    warnings: list[str] = []
    if failed:
        warnings.append(f"{failed} 个页面读取失败，可在体检单中单独重试。")
    if needs_review:
        incomplete = sum(1 for item in results if item["ok"] and not item["collection_complete"])
        low_quality = sum(
            1 for item in results
            if item["ok"] and item["collection_complete"] and item["quality_score"] < 70
        )
        if incomplete:
            warnings.append(f"{incomplete} 个页面缺少完整采集证明，相关建议已停止使用。")
        if low_quality:
            warnings.append(f"{low_quality} 个页面质量分低于 70，相关建议需要人工复核。")
    if total and completed < total:
        warnings.append(f"巡查仅覆盖 {completed}/{total} 个页面，数据不完整。")
    if missing_contract_page_ids:
        warnings.append(
            f"全店巡检范围缺少 {', '.join(missing_contract_page_ids)}，不能按 100% 完成。"
        )
    if pending_page_ids:
        warnings.append(f"还有 {len(pending_page_ids)} 个页面尚未执行，可从断点继续。")
    if status in {"cancelled", "interrupted", "error"} and scan.get("error"):
        warnings.append(str(scan.get("error"))[:300])
    if isinstance(scan.get("recovery"), dict) and recovery_contract is None:
        warnings.append("巡店恢复回执损坏，不能证明本次续跑范围。")

    return {
        "generated_at": _now_label(),
        "scan_status": status,
        "scope": scan_scope if scan_scope in {"quick", "product_graph"} else "full",
        "readiness": readiness,
        "readiness_label": readiness_label,
        "analysis_ready": readiness == "ready",
        "first_value_ready": readiness in {"quick_ready", "product_graph_ready", "ready"},
        "store_key": str(scan.get("store_key") or "")[:80],
        "account_key": str(scan.get("account_key") or "")[:80],
        "account_label": account_label,
        "started_at": int(scan.get("started_at", 0) or 0),
        "root_started_at": int(scan.get("root_started_at", scan.get("started_at", 0)) or 0),
        "finished_at": int(scan.get("finished_at", 0) or 0),
        "planned_page_ids": reported_planned_page_ids,
        "execution_page_ids": execution_page_ids,
        "expected_page_ids": expected_page_ids,
        "attempted_page_ids": attempted_page_ids,
        "missing_contract_page_ids": missing_contract_page_ids,
        "scope_contract": {
            "schema_version": 1,
            "core_doudian": list(SCAN_CORE_DOUDIAN_PAGE_IDS),
            "full_doudian": list(SCAN_FULL_DOUDIAN_PAGE_IDS),
            "ads_optional": list(SCAN_ADS_OPTIONAL_PAGE_IDS),
            "complete": scope_contract_complete,
        },
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
        "pending_page_ids": pending_page_ids,
        "resume_page_ids": list(dict.fromkeys([
            item["id"] for item in results
            if not item["ok"] or not item["collection_complete"]
        ] + pending_page_ids)),
        "run_id": run_id[:80],
        "root_run_id": root_run_id[:80],
        "parent_run_id": parent_run_id[:80],
        "resumed_from_run_id": resumed_from_run_id[:80],
        "recovery_attempt": recovery_attempt,
        "lineage_verified": lineage_verified,
        "root_scope_page_count": len(reported_planned_page_ids),
        "recovery": recovery_contract,
        "revision": int(scan.get("revision") or 0),
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
    selected_fresh = age_seconds is not None and age_seconds <= 24 * 60 * 60
    required_core_types = {"overview", "orders", "products", "shelf"}
    core_snapshots = {
        str(item.get("page_type") or ""): item
        for item in list_snapshots()
        if item.get("source") == "doudian"
        and str(item.get("page_type") or "") in required_core_types
        and int(item.get("quality_score") or 0) >= 60
    }
    ready_core_types = sorted(
        page_type for page_type, item in core_snapshots.items()
        if int(item.get("age_seconds") or 0) <= 24 * 60 * 60 and not item.get("timestamp_conflict")
    )
    stale_core_types = sorted(
        page_type for page_type, item in core_snapshots.items()
        if int(item.get("age_seconds") or 0) > 24 * 60 * 60 or item.get("timestamp_conflict")
    )
    missing_core_types = sorted(required_core_types - set(core_snapshots))
    core_data_ready = required_core_types.issubset(ready_core_types)
    fresh = selected_fresh and core_data_ready
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
        if stale_core_types:
            warnings.append(f"关键页面已超过 24 小时：{', '.join(stale_core_types)}")
        if missing_core_types:
            warnings.append(f"关键页面尚未采集或质量不足：{', '.join(missing_core_types)}")
        if not stale_core_types and not missing_core_types:
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
        "core_data": {
            "status": "ready" if core_data_ready else "stale" if stale_core_types else "missing",
            "required_types": sorted(required_core_types),
            "ready_types": ready_core_types,
            "stale_types": stale_core_types,
            "missing_types": missing_core_types,
        },
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
    inventory_state = _inventory_task_state()
    creative = build_qianchuan_creative_analysis(settings)
    product_graph = build_douyin_product_graph()
    return {
        "generated_at": _now_label(),
        "settings": settings,
        "plan_recommendations": plans,
        "inventory_alerts": inventory,
        "inventory_data_status": inventory_state,
        "shelf_analysis": build_shelf_analysis(),
        "live_analysis": build_live_analysis(),
        "creative_analysis": creative,
        "product_graph": product_graph,
        "summary": {
            "plan_actions": len(plans),
            "high_risk_plans": sum(1 for item in plans if item["level"] == "high"),
            "inventory_alerts": len(inventory),
            "critical_inventory": sum(1 for item in inventory if item["level"] == "high"),
            "creative_actions": len(creative["recommendations"]),
            "linked_products": int((product_graph.get("summary") or {}).get("cross_channel_products") or 0),
            "product_graph_actions": len(product_graph.get("recommendations") or []),
        },
        "mode": "read_only",
    }


def build_stop_loss_queue(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Turn plan diagnostics into a ranked, operator-friendly loss-control queue."""

    settings = settings or load_agent_settings()
    mode = str(settings.get("execution_mode") or "observe")
    min_spend = max(1.0, float(settings.get("min_spend_for_action") or 100))
    queue: list[dict[str, Any]] = []
    recommendations = build_plan_recommendations(settings)
    plan_console = build_qianchuan_plan_console() if mode == "supervised" else None
    for item in recommendations:
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
        action = item.get("action_params") if isinstance(item.get("action_params"), dict) else None
        action_blockers = [
            blocker for blocker in ((action or {}).get("blocked_reasons") or [])
            if isinstance(blocker, dict)
        ]
        if action is not None and plan_console is not None:
            scope_gate = _supervised_draft_collection_gate(action, plan_console=plan_console)
        elif action is not None:
            scope_gate = {
                "required": bool((action.get("evidence_ref") or {}).get("collection_scope_required")),
                "ready": False,
                "state": "execution_mode_not_supervised",
                "blockers": [],
            }
        else:
            scope_gate = {
                "required": True,
                "ready": False,
                "state": "action_draft_missing",
                "blockers": [{
                    "code": "ACTION_DRAFT_MISSING",
                    "message": "缺少绑定稳定计划 ID 的 canonical 动作草稿。",
                }],
            }
        scope_blockers = [
            blocker for blocker in (scope_gate.get("blockers") or [])
            if isinstance(blocker, dict)
        ]
        canonical_ready = bool(
            action
            and action.get("can_confirm") is True
            and not action_blockers
            and scope_gate.get("ready") is True
        )
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
            "can_start_execution": bool(
                mode == "supervised"
                and action_type in {"stop_loss", "reduce_budget"}
                and canonical_ready
            ),
            "execution_readiness": {
                "can_confirm": bool(action and action.get("can_confirm") is True),
                "blocked_reasons": [*action_blockers, *scope_blockers],
                "scope_gate": scope_gate,
                "ready": canonical_ready,
            },
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


def _scan_lineage_report_summary(scan: dict[str, Any], receipt: dict[str, Any]) -> str:
    run_id = str(receipt.get("run_id") or scan.get("run_id") or "")
    root_run_id = str(receipt.get("root_run_id") or scan.get("root_run_id") or run_id)
    if not run_id or not root_run_id:
        return "巡检证据链：旧版检查点未记录根轮标识。"
    page_count = max(0, int(receipt.get("root_scope_page_count") or 0))
    if receipt.get("recovery_attempt") is True:
        parent_run_id = str(receipt.get("parent_run_id") or scan.get("parent_run_id") or "")
        return (
            f"巡检证据链：根轮 {root_run_id}（{page_count} 页）；"
            f"当前续跑 {run_id}，父轮 {parent_run_id or '未记录'}；"
            f"链路{'已校验' if receipt.get('lineage_verified') is True else '待复核'}。"
        )
    return (
        f"巡检证据链：根轮 {root_run_id}（{page_count} 页）；"
        f"链路{'已校验' if receipt.get('lineage_verified') is True else '待复核'}。"
    )


def build_scheduled_report_guard(
    catalog: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Build the non-textual freshness contract used by automatic delivery."""

    checked_at_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    settings = settings if isinstance(settings, dict) else load_agent_settings()
    selected_store = str(settings.get("store_key") or "").strip().lower()
    valid_store = selected_store if SAFE_KEY.fullmatch(selected_store) else ""
    snapshots = catalog if isinstance(catalog, list) else list_snapshots()
    by_page = {
        str(item.get("page_type") or ""): item
        for item in snapshots
        if isinstance(item, dict) and item.get("source") == "doudian"
    }
    missing: list[str] = []
    stale: list[str] = []
    low_quality: list[str] = []
    timestamp_conflicts: list[str] = []
    expiry_candidates: list[int] = []
    captured_candidates: list[int] = []
    for page_type in SCAN_CORE_DOUDIAN_PAGE_IDS:
        item = by_page.get(page_type)
        if not isinstance(item, dict):
            missing.append(page_type)
            continue
        age_seconds = max(0, int(item.get("age_seconds") or 0))
        captured_at_seconds = int(item.get("captured_at") or 0)
        if captured_at_seconds > 0:
            captured_candidates.append(captured_at_seconds * 1000)
        if item.get("timestamp_conflict") is True:
            timestamp_conflicts.append(page_type)
            continue
        if item.get("fresh") is not True or age_seconds >= STALE_SECONDS:
            stale.append(page_type)
        quality_score = int(item.get("quality_score") or 0)
        if quality_score < 60 or item.get("rule_eligible") is not True:
            low_quality.append(page_type)
        expiry_candidates.append(checked_at_ms + max(0, STALE_SECONDS - age_seconds) * 1000)
    safe = not (not valid_store or missing or stale or low_quality or timestamp_conflicts)
    # A reminder is deliberately short-lived too: scheduler retries must never
    # reuse yesterday's diagnosis of which pages need synchronization.
    valid_until_ms = min(expiry_candidates) if safe and expiry_candidates else checked_at_ms + 5 * 60 * 1000
    return {
        "schema_version": REPORT_GUARD_SCHEMA_VERSION,
        "store_scope": valid_store or "unselected",
        "safe_for_business_conclusions": safe,
        "delivery_kind": "operating_report" if safe else "sync_reminder",
        "checked_at_ms": checked_at_ms,
        "valid_until_ms": valid_until_ms,
        "data_cutoff_at_ms": min(captured_candidates) if captured_candidates else 0,
        "required_pages": list(SCAN_CORE_DOUDIAN_PAGE_IDS),
        "missing": missing + ([] if valid_store else ["selected_store"]),
        "stale": stale,
        "low_quality": low_quality,
        "timestamp_conflicts": timestamp_conflicts,
    }


def _report_freshness_envelope(guard: dict[str, Any]) -> list[str]:
    scope = str(guard.get("store_scope") or "unselected")
    checked_at_ms = int(guard.get("checked_at_ms") or 0)
    checked_at = datetime.fromtimestamp(checked_at_ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if checked_at_ms else "未知"
    readiness = "核心经营数据已通过时效与质量校验" if guard.get("safe_for_business_conclusions") is True else "自动发送经营结论已阻止，需先同步核心页面"
    return [
        "> **数据安全范围（系统强制）**",
        f"> 店铺作用域：`{scope}`｜校验时间：{checked_at}｜状态：{readiness}",
    ]


def build_sync_reminder_report(report_date: str, guard: dict[str, Any] | None = None) -> dict[str, Any]:
    guard = guard if isinstance(guard, dict) else build_scheduled_report_guard()
    labels = {
        "overview": "经营概览", "orders": "订单", "products": "商品", "shelf": "商品卡",
        "selected_store": "店铺选择",
    }

    def render(values: Any) -> str:
        items = [labels.get(str(value), str(value)) for value in (values if isinstance(values, list) else [])]
        return "、".join(items) if items else "无"

    lines = [
        f"# 店策 Agent 数据同步提醒 - {report_date}",
        "",
        *_report_freshness_envelope(guard),
        "",
        "**本次未生成、未发送经营结论。**",
        "",
        f"- 缺失：{render(guard.get('missing'))}",
        f"- 已过期：{render(guard.get('stale'))}",
        f"- 质量不足：{render(guard.get('low_quality'))}",
        f"- 时间戳异常：{render(guard.get('timestamp_conflicts'))}",
        "- 下一步：打开经营工作台，完成一次核心巡店后重新生成。",
        "",
    ]
    content = "\n".join(lines)
    path = _reports_dir() / f"{report_date}.md"
    _atomic_text_write(path, content)
    return {
        "date": report_date,
        "generated_at": _now_label(),
        "generated_at_ms": int(time.time() * 1000),
        "path": str(path),
        "store_scope": str(guard.get("store_scope") or "unselected"),
        "template": "sync_reminder",
        "content_kind": "sync_reminder",
        "delivery_guard": guard,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "content": content,
    }


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
        f"{_scan_lineage_report_summary(scan, scan_receipt)}"
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
    try:
        datetime.strptime(report_date, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError("report_date must be a valid calendar date") from error
    insights = build_insights()
    action_center = build_action_center()
    ops = build_ops_manager()
    scan = load_scan_status()
    scan_receipt = build_scan_receipt()
    catalog = list_snapshots()
    delivery_guard = build_scheduled_report_guard(catalog, action_center.get("settings"))
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
        f"- {_scan_lineage_report_summary(scan, scan_receipt)}",
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
    # Templates control the report body, never the mandatory scope/freshness
    # envelope used by an operator to judge whether conclusions are current.
    lines = [*lines[:1], "", *_report_freshness_envelope(delivery_guard), "", *lines[1:]]
    content = "\n".join(lines)
    generated_at_ms = int(time.time() * 1000)
    _atomic_text_write(report_path, content)
    _cleanup_old_reports(int(action_center["settings"]["report_retention_days"]))
    return {
        "date": report_date,
        "generated_at": _now_label(),
        "generated_at_ms": generated_at_ms,
        "path": str(report_path),
        "store_scope": str(delivery_guard.get("store_scope") or "unselected"),
        "headline": insights["headline"],
        "summary": action_center["summary"],
        "template": template_key,
        "content_kind": "operating_report",
        "delivery_guard": delivery_guard,
        "stale_sources": stale_sources[:5],
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "content": content,
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
            report_date = now.strftime("%Y-%m-%d")
            # Always rebuild from the current catalog.  An existing Markdown
            # file is not proof that its conclusions are still current or even
            # belong to the store selected now.
            report = generate_daily_report(report_date)
            guard = report.get("delivery_guard") if isinstance(report, dict) else None
            if not isinstance(guard, dict) or guard.get("safe_for_business_conclusions") is not True:
                report = build_sync_reminder_report(report_date, guard)
            if report and str(report.get("date") or "") == report_date:
                if _load_integration_secrets().get("auto_send_reports"):
                    delivery = deliver_scheduled_report(report)
                    if delivery.get("attempted"):
                        logger.info("日报发送状态: %s", delivery.get("status"))
                if not target.exists():
                    # Defensive only: report generation is expected to create it.
                    logger.warning("日报生成后文件不存在: %s", target)
                else:
                    logger.info("已重新校验并生成每日经营报告: %s", target)
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


def _chengfang_autopilot_scheduler(stop_event: threading.Event) -> None:
    """Continuously run the local-only A1 loop inside the existing Agent process."""

    try:
        configured = int(os.environ.get("DIAN_AGENT_CHENGFANG_INTERVAL_SECONDS", EVALUATION_INTERVAL_SECONDS))
    except ValueError:
        configured = EVALUATION_INTERVAL_SECONDS
    interval = max(60, min(configured, 24 * 60 * 60))
    while not stop_event.is_set():
        try:
            result = run_chengfang_autopilot_cycle("scheduler")
            if result.get("status") not in {"skipped"} and not result.get("deduplicated"):
                logger.info("乘方 A1 影子评估已完成: %s", result.get("status"))
        except (LocalStoreError, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            logger.warning("乘方 A1 影子评估失败，真实投放未受影响: %s", error)
        stop_event.wait(interval)


def _bound_current_scope(
    requested_store: Any = "",
    requested_account: Any = "",
    *,
    require_store: bool = True,
) -> tuple[str, str]:
    """Bind a business operation to the selected local store/account."""

    settings = load_agent_settings()
    current_store = str(settings.get("store_key") or "").strip().lower()
    current_account = str(settings.get("qianchuan_account_key") or "").strip().lower()
    store = str(requested_store or "").strip().lower()
    account = str(requested_account or "").strip().lower()
    if require_store and not current_store:
        raise ValueError("Select the current store before accessing scoped business data.")
    if store and store != current_store:
        raise ValueError("The requested store does not match the current store scope.")
    if account and account != current_account:
        raise ValueError("The requested account does not match the current account scope.")
    return current_store, current_account


def _current_memory_scope(query: dict[str, list[str]] | None = None) -> tuple[str, str]:
    """Resolve memory only inside the locked current shop/account scope."""
    query = query or {}
    return _bound_current_scope(
        str((query.get("store_key") or [""])[0] or ""),
        str((query.get("account_key") or [""])[0] or ""),
        require_store=False,
    )


def _ai_gateway() -> AIGateway:
    """Return one proposal-only gateway for the active local data directory."""

    global _ai_gateway_cache
    data_dir = Path(DATA_DIR)
    with _ai_gateway_lock:
        if _ai_gateway_cache is None or _ai_gateway_cache[0] != data_dir:
            _ai_gateway_cache = (data_dir, AIGateway(data_dir))
        return _ai_gateway_cache[1]


def _ai_provider_alias(provider_id: str) -> str:
    return {
        "openai_responses": "openai",
        "ollama": "local",
    }.get(str(provider_id or ""), str(provider_id or ""))


_REMOTE_AI_PROVIDER_IDS = frozenset({
    "openai_responses",
    "deepseek",
    "qwen_bailian",
    "glm_zhipu",
    "hunyuan_tencent",
    "doubao_ark",
    "openai_compatible",
})


def _active_ai_provider_path() -> Path:
    return Path(DATA_DIR) / "ai" / "active_provider.json"


def _read_active_ai_provider() -> str:
    try:
        value = json.loads(_active_ai_provider_path().read_text(encoding="utf-8"))
        return resolve_provider_id(str(value.get("provider_id") or "")) if isinstance(value, dict) else ""
    except (OSError, ValueError, TypeError, json.JSONDecodeError, AIProviderError):
        return ""


def _write_active_ai_provider(provider_id: str) -> None:
    canonical = resolve_provider_id(provider_id) if provider_id else ""
    _atomic_json_write(
        _active_ai_provider_path(),
        {
            "schema_version": 1,
            "provider_id": canonical,
            "updated_at_ms": int(time.time() * 1000),
        },
    )


def _reserve_ai_network_call(kind: str, provider_id: str) -> None:
    """Bound model spend and reject overlapping requests fail-closed."""

    now = time.time()
    key = (str(Path(DATA_DIR)), kind, provider_id)
    window_seconds, limit, cooldown = (3600, 20, 5) if kind == "shadow" else (600, 10, 0)
    with _ai_rate_lock:
        history = [stamp for stamp in _ai_call_history.get(key, []) if now - stamp < 24 * 60 * 60]
        recent = [stamp for stamp in history if now - stamp < window_seconds]
        if len(recent) >= limit:
            raise ValueError("AI 请求已达到本地频率上限，请稍后再试")
        if cooldown and recent and now - recent[-1] < cooldown:
            raise ValueError("AI 影子分析刚刚运行过，请等待几秒后再试")
        if kind == "shadow" and len(history) >= 50:
            raise ValueError("今日 AI 影子分析已达到本地成本保护上限")
        if not _ai_network_lock.acquire(blocking=False):
            raise ValueError("已有 AI 请求正在运行，请等待完成后再试")
        history.append(now)
        _ai_call_history[key] = history


def _release_ai_network_call() -> None:
    if _ai_network_lock.locked():
        _ai_network_lock.release()


def _ai_context_policy(settings: dict[str, Any]) -> dict[str, Any]:
    """Tight proposal limits; these settings never enable platform writes."""

    return {
        "allowed_actions": ["hold", "decrease_budget", "pause", "restore"],
        "max_budget_decrease_percent": min(
            10.0,
            max(0.0, float(settings.get("ai_max_budget_decrease_percent") or 10.0)),
        ),
        "max_actions_per_hour": min(
            3,
            max(0, int(settings.get("ai_max_actions_per_hour") or 3)),
        ),
        "max_daily_budget_impact": max(
            0.0,
            float(settings.get("max_daily_budget_reduction") or 0.0),
        ),
        "min_quality_score": max(
            70,
            min(100, int(settings.get("ai_min_quality_score") or 70)),
        ),
        "max_data_age_seconds": min(
            600,
            max(30, int(settings.get("ai_max_data_age_seconds") or 180)),
        ),
        "min_confidence": max(
            0.75,
            min(1.0, float(settings.get("ai_min_confidence") or 0.8)),
        ),
        "proposal_ttl_seconds": min(
            300,
            max(30, int(settings.get("ai_proposal_ttl_seconds") or 180)),
        ),
    }


def _build_current_ai_context_pack() -> dict[str, Any]:
    """Build the only business payload that may be handed to an AI provider.

    The projector in :mod:`ai_context` ignores arbitrary text, URLs, raw page
    content and detail rows.  This wrapper intentionally supplies the plan
    console plus policy only, so a future caller cannot accidentally widen the
    cloud data boundary by passing complete snapshots.
    """

    settings = load_agent_settings()
    context_settings = {
        "store_key": str(settings.get("store_key") or ""),
        "account_key": str(settings.get("qianchuan_account_key") or ""),
        "anonymization_secret": _identity_secret(),
        "constraints": _ai_context_policy(settings),
    }
    return build_ai_context_pack(
        snapshots=[],
        plan_console=build_qianchuan_plan_console(),
        insights=[],
        settings=context_settings,
    )


def _ai_context_business_fingerprint(context: dict[str, Any]) -> str:
    """Hash source facts while ignoring request-time derived fields."""

    evidence: list[dict[str, Any]] = []
    for raw in context.get("evidence") or []:
        if not isinstance(raw, dict):
            continue
        quality = raw.get("quality") if isinstance(raw.get("quality"), dict) else {}
        evidence.append({
            "evidence_ref": raw.get("evidence_ref"),
            "source": raw.get("source"),
            "page_type": raw.get("page_type"),
            "account_ref": raw.get("account_ref"),
            "plan_id": raw.get("plan_id"),
            "captured_at_ms": raw.get("captured_at_ms"),
            "quality": {
                "score": quality.get("score"),
                "completeness": quality.get("completeness"),
                "confidence": quality.get("confidence"),
                "fresh": quality.get("fresh"),
                "future_timestamp": quality.get("future_timestamp"),
            },
            "metrics": raw.get("metrics"),
        })
    payload = {
        "identity": context.get("identity"),
        "evidence": evidence,
        "constraints": context.get("constraints"),
        "privacy_contract": context.get("privacy_contract"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _remember_ai_context(context: dict[str, Any]) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    context_hash = str(context.get("context_hash") or "")
    data_dir = Path(DATA_DIR)
    with _ai_gateway_lock:
        for key, (entry_dir, entry) in list(_ai_context_cache.items()):
            if entry_dir != data_dir or int(entry.get("expires_at_ms") or 0) <= now_ms:
                _ai_context_cache.pop(key, None)
        if re.fullmatch(r"[a-f0-9]{64}", context_hash):
            _ai_context_cache[context_hash] = (data_dir, copy.deepcopy(context))
        if len(_ai_context_cache) > _AI_CONTEXT_CACHE_LIMIT:
            oldest = sorted(
                _ai_context_cache,
                key=lambda key: int(_ai_context_cache[key][1].get("generated_at_ms") or 0),
            )[: len(_ai_context_cache) - _AI_CONTEXT_CACHE_LIMIT]
            for key in oldest:
                _ai_context_cache.pop(key, None)
    return copy.deepcopy(context)


def get_ai_context_pack() -> dict[str, Any]:
    """Return and briefly retain the exact context an external AI must cite."""

    return _remember_ai_context(_build_current_ai_context_pack())


def _resolve_cached_ai_context(proposal: dict[str, Any], context_hash: str) -> dict[str, Any]:
    supplied_hash = str(context_hash or "").strip().lower()
    context_id = str(proposal.get("context_id") or "").strip()
    data_dir = Path(DATA_DIR)
    cached: dict[str, Any] | None = None
    now_ms = int(time.time() * 1000)
    with _ai_gateway_lock:
        for key, (entry_dir, entry) in list(_ai_context_cache.items()):
            if entry_dir != data_dir or int(entry.get("expires_at_ms") or 0) <= now_ms:
                _ai_context_cache.pop(key, None)
                continue
            hash_matches = bool(supplied_hash) and hmac.compare_digest(supplied_hash, key)
            id_matches = not supplied_hash and context_id == str(entry.get("context_id") or "")
            if hash_matches or id_matches:
                cached = copy.deepcopy(entry)
                break
    if cached is None:
        raise ValueError("AI 提案引用的经营上下文不存在或已过期，请重新读取后再提交")
    errors = validate_ai_context_pack(cached, now_ms=now_ms)
    if errors:
        raise ValueError("AI 提案引用的经营上下文已失效，请重新读取后再提交")

    current = _build_current_ai_context_pack()
    if not hmac.compare_digest(
        _ai_context_business_fingerprint(cached),
        _ai_context_business_fingerprint(current),
    ):
        raise ValueError("AI 提案对应的经营数据或账户已变化，请重新读取后再提交")
    return cached


def build_ai_context_preview() -> dict[str, Any]:
    context = get_ai_context_pack()
    errors = validate_ai_context_pack(context)
    encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {
        "schema_version": 1,
        "ready": not errors,
        "context_id": str(context.get("context_id") or ""),
        "context_hash": str(context.get("context_hash") or ""),
        "evidence_count": len(context.get("evidence") or []),
        "identity_bound": bool((context.get("identity") or {}).get("identity_bound")),
        "included_fields": [
            "匿名店铺与千川账户引用",
            "单计划预算、消耗、ROI、订单数等聚合指标",
            "数据时间、质量分与证据哈希",
            "本地风控阈值与允许建议范围",
        ],
        "excluded_fields": [
            "Cookie、Token、API Key 与登录凭证",
            "真实店名、真实账户 ID 与计划名称",
            "消费者信息、订单明细与素材原文",
            "网页全文、截图、浏览器历史与隐藏字段",
        ],
        "generated_at": datetime.fromtimestamp(
            int(context.get("generated_at_ms") or 0) / 1000
        ).astimezone().isoformat(timespec="seconds") if context.get("generated_at_ms") else "",
        "estimated_tokens": max(1, int(len(encoded) / 3.2)),
        "validation_errors": errors,
        "store_label": "当前店铺（匿名范围）" if (context.get("identity") or {}).get("store_ref") else "尚未选择店铺",
        "proposal_only": True,
        "execution_allowed": False,
        "platform_write_attempted": False,
        "secrets_exposed": False,
    }


def get_ai_status(provider_id: str | None = None) -> dict[str, Any]:
    gateway = _ai_gateway()
    preferred = resolve_provider_id(provider_id) if provider_id else _read_active_ai_provider()
    health = gateway.status(preferred or None)
    rows = [row for row in health.get("providers", []) if isinstance(row, dict)]
    selected = next((row for row in rows if row.get("provider_id") == preferred), None) if preferred else None
    selected = selected or next((row for row in rows if row.get("ready")), None)
    selected = selected or next((row for row in rows if row.get("enabled")), None)
    selected = selected or next((row for row in rows if row.get("provider_id") == "openai_responses"), None)
    selected = selected or {}
    canonical_id = str(selected.get("provider_id") or "openai_responses")
    try:
        config = gateway.registry.public_config(canonical_id)
    except AIProviderError:
        config = {}
    credential = selected.get("credential") if isinstance(selected.get("credential"), dict) else {}
    return {
        "schema_version": 1,
        "provider": _ai_provider_alias(canonical_id),
        "provider_id": canonical_id,
        "active_provider_id": preferred or canonical_id,
        "model": str(selected.get("model") or config.get("model") or ""),
        "base_url": str(config.get("base_url") or ""),
        "remote_access_approved": bool(config.get("remote_access_approved")),
        "state": str(selected.get("state") or "disabled"),
        "configured": bool(selected.get("enabled") and (selected.get("model") or config.get("model"))),
        "analysis_available": bool(selected.get("ready")),
        "connected": bool(selected.get("ready")),
        "credential_saved": bool(credential.get("saved")),
        "api_key_saved": bool(credential.get("saved")),
        "network_checked": bool(selected.get("network_checked")),
        "providers": rows,
        "catalog": gateway.catalog(),
        "proposal_only": True,
        "execution_allowed": False,
        "execution_supported": False,
        "secrets_exposed": False,
    }


def _ai_public_proposal(envelope: dict[str, Any]) -> dict[str, Any]:
    candidates = envelope.get("proposal_candidates") if isinstance(envelope.get("proposal_candidates"), list) else []
    candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    arguments = candidate.get("arguments") if isinstance(candidate.get("arguments"), dict) else {}
    action = str(arguments.get("action") or "hold")
    action_label = {
        "hold": "继续观察",
        "decrease_budget": "建议降预算",
        "pause": "建议暂停",
        "restore": "建议恢复",
    }.get(action, "待人工判断")
    plan_id = str(arguments.get("plan_id") or "")
    reason_codes = [str(item) for item in arguments.get("reason_codes", []) if isinstance(item, str)]
    validation_errors = envelope.get("validation_errors") if isinstance(envelope.get("validation_errors"), list) else []
    eligible = bool(envelope.get("eligible_for_human_review"))
    if eligible:
        rationale = f"{action_label}；依据：{', '.join(reason_codes) or '聚合经营指标'}。"
    else:
        first_error = validation_errors[0] if validation_errors and isinstance(validation_errors[0], dict) else {}
        rationale = f"提案未通过本地校验：{str(first_error.get('message') or '数据或格式不满足安全要求')}"
    created_at_ms = int(envelope.get("created_at_ms") or 0)
    return {
        "proposal_id": str(envelope.get("proposal_id") or ""),
        "title": f"{('计划 ' + plan_id + ' · ') if plan_id else ''}{action_label}",
        "suggestion": rationale,
        "confidence": arguments.get("confidence"),
        "action": action,
        "plan_id": plan_id,
        "validation_state": str(envelope.get("validation_state") or "pending"),
        "eligible_for_human_review": eligible,
        "created_at": datetime.fromtimestamp(created_at_ms / 1000).astimezone().isoformat(timespec="seconds") if created_at_ms else "",
        "can_execute": False,
        "execution_allowed": False,
    }


def get_ai_proposals(limit: int = 50) -> dict[str, Any]:
    result = _ai_gateway().list_proposals(limit=limit)
    envelopes = [row for row in result.get("proposals", []) if isinstance(row, dict)]
    return {
        **result,
        "items": [_ai_public_proposal(row) for row in envelopes],
        "execution_allowed": False,
        "can_execute": False,
        "execution_performed": False,
    }


def submit_ai_proposal(
    proposal: dict[str, Any],
    provider_id: str = "mcp",
    model: str = "",
    context_hash: str = "",
) -> dict[str, Any]:
    if not isinstance(proposal, dict):
        raise ValueError("proposal 必须是 DecisionProposalV1 对象")
    context = _resolve_cached_ai_context(proposal, context_hash)
    current_hash = str(context.get("context_hash") or "")
    result = _ai_gateway().submit_proposal(
        proposal,
        context_pack=context,
        provider_id=str(provider_id or "mcp")[:80],
        model=str(model or "")[:120],
        context_hash=current_hash,
    )
    return {
        **result,
        "execution_allowed": False,
        "execution_performed": False,
    }


def configure_ai_provider(payload: dict[str, Any]) -> dict[str, Any]:
    provider_id = resolve_provider_id(str(payload.get("provider") or payload.get("provider_id") or ""))
    remote_access_approved = payload.get("remote_access_approved") is True
    if provider_id in _REMOTE_AI_PROVIDER_IDS and not remote_access_approved:
        raise ValueError("连接云端 AI 前，请先勾选同意发送上方预览的脱敏经营数据")
    result = _ai_gateway().configure(
        provider_id,
        enabled=True,
        model=str(payload.get("model") or ""),
        base_url=str(payload.get("base_url") or ""),
        api_key=str(payload.get("api_key") or ""),
        remote_access_approved=remote_access_approved,
    )
    _write_active_ai_provider(provider_id)
    return {
        "ok": True,
        **get_ai_status(provider_id),
        "provider_health": result,
        "message": "AI 连接已安全保存；仅开放分析与影子建议，不开放投放执行。",
    }


def disable_all_ai_providers(payload: dict[str, Any]) -> dict[str, Any]:
    """Revoke network use for every provider without exposing stored keys."""

    if payload.get("confirm") is not True:
        raise ValueError("confirm 必须为 true 才能停用全部 AI 连接")
    gateway = _ai_gateway()
    health = gateway.status()
    disabled: list[str] = []
    for row in health.get("providers", []):
        if not isinstance(row, dict) or not row.get("enabled"):
            continue
        provider_id = str(row.get("provider_id") or "")
        config = gateway.registry.public_config(provider_id)
        gateway.configure(
            provider_id,
            enabled=False,
            model=str(config.get("model") or ""),
            base_url=str(config.get("base_url") or ""),
            remote_access_approved=False,
        )
        disabled.append(provider_id)
    _write_active_ai_provider("")
    return {
        "ok": True,
        **get_ai_status(),
        "disabled_providers": disabled,
        "message": "所有 AI 分析连接已停用；不会再向模型发送经营数据。已保存密钥仍由系统加密保管。",
        "execution_allowed": False,
    }


def test_ai_provider(payload: dict[str, Any]) -> dict[str, Any]:
    provider_id = resolve_provider_id(str(payload.get("provider") or payload.get("provider_id") or ""))
    gateway = _ai_gateway()
    supplied_key = str(payload.get("api_key") or "")
    use_saved_configuration = False
    if not supplied_key and provider_id in _REMOTE_AI_PROVIDER_IDS:
        saved = gateway.registry.public_config(provider_id)
        requested_base_url = str(payload.get("base_url") or saved.get("base_url") or "").rstrip("/")
        saved_base_url = str(saved.get("base_url") or "").rstrip("/")
        use_saved_configuration = bool(
            saved.get("enabled")
            and str(saved.get("model") or "") == str(payload.get("model") or "")
            and requested_base_url == saved_base_url
        )
        if not use_saved_configuration:
            raise ValueError("测试未保存的云端配置时，请重新输入 API Key；已保存密钥不会回显到页面")
    tester = getattr(gateway, "test_configuration", None)
    _reserve_ai_network_call("probe", provider_id)
    try:
        if use_saved_configuration:
            result = gateway.test_provider(provider_id)
        elif callable(tester):
            result = tester({
                "provider_id": provider_id,
                "model": str(payload.get("model") or ""),
                "base_url": str(payload.get("base_url") or ""),
                "api_key": supplied_key,
                "remote_access_approved": payload.get("remote_access_approved") is True,
            })
        else:
            result = gateway.test_provider(provider_id)
    finally:
        _release_ai_network_call()
    if result.get("ok") is not True:
        raise ValueError(str(result.get("error") or "AI Provider 未通过结构化连接测试"))
    return {
        **result,
        "execution_allowed": False,
        "platform_write_attempted": False,
    }


def run_ai_shadow(payload: dict[str, Any]) -> dict[str, Any]:
    settings = load_agent_settings()
    selected_store = str(settings.get("store_key") or "").lower()
    requested_store = str(payload.get("store_key") or selected_store).lower()
    if requested_store and requested_store != selected_store:
        raise ValueError("AI 影子分析只能使用当前已选择店铺的脱敏上下文")
    context = get_ai_context_pack()
    context_errors = validate_ai_context_pack(context)
    if context_errors:
        codes = "、".join(str(item.get("code") or "INVALID_CONTEXT") for item in context_errors[:4])
        raise ValueError(f"当前经营数据尚不能用于 AI 分析：{codes}。请先选择店铺、绑定账户并快速巡店")
    provider_id = resolve_provider_id(str(payload.get("provider") or get_ai_status().get("provider_id") or ""))
    _reserve_ai_network_call("shadow", provider_id)
    try:
        result = _ai_gateway().run_shadow(
            provider_id=provider_id,
            context_pack=context,
            instruction="基于当前脱敏聚合指标，只给出一个风险最低、可复核的投放建议；证据不足时选择 hold。",
            proposal_schema=DECISION_PROPOSAL_SCHEMA,
        )
    finally:
        _release_ai_network_call()
    item = _ai_public_proposal(result)
    return {
        "ok": True,
        "proposal": result,
        "items": [item],
        "message": "影子分析已完成，提案已通过本地格式校验。" if result.get("eligible_for_human_review") else "影子分析已完成，但提案未通过本地校验，未进入人工复核。",
        "execution_allowed": False,
        "can_execute": False,
        "execution_performed": False,
        "platform_write_attempted": False,
    }


def _maintenance_system_status() -> dict[str, Any]:
    """Return the legacy CLI probe without shop data, paths or account state."""

    status = build_system_status()
    runtime = status.get("runtime") if isinstance(status.get("runtime"), dict) else {}
    return {
        "status_contract_version": 1,
        "ready": status.get("ready") is True,
        "product_operational": status.get("product_operational") is True,
        "public_distribution_ready": status.get("public_distribution_ready") is True,
        "agent_version": str(status.get("agent_version") or AGENT_VERSION),
        "required_extension_version": str(status.get("required_extension_version") or AGENT_VERSION),
        "bridge_protocol_version": int(status.get("bridge_protocol_version") or 2),
        "mode": str(status.get("mode") or "local_first"),
        "runtime": {
            "state": str(runtime.get("state") or "unknown")[:32],
            "autostart_enabled": runtime.get("autostart_enabled") is True,
            "keepalive_enabled": runtime.get("keepalive_enabled") is True,
        },
        "authentication_required_for_details": True,
    }


def _authenticated_session_status(session: dict[str, Any]) -> dict[str, Any]:
    """Return a cheap, request-scoped proof that local authentication worked.

    ``/system/status`` intentionally aggregates database, knowledge, release,
    storage and scan state.  It is useful to render the update centre, but it
    must not double as the browser extension's authentication heartbeat: a
    slow disk or one busy business module would then look like a broken local
    pairing.  This receipt is built only after ``_session_authorized`` has
    validated the token, origin, extension ID and extension version, and does
    not touch any business storage.
    """

    value = session if isinstance(session, dict) else {}
    subject = str(value.get("subject") or "").strip().lower()
    extension_version = str(value.get("extension_version") or "").strip()
    is_extension = subject != INTERNAL_CLIENT_SUBJECT
    return {
        "authentication_contract_version": 1,
        "authenticated": True,
        "client_kind": "browser_extension" if is_extension else "internal_maintenance",
        "session_subject": subject,
        "session_extension_version": extension_version if is_extension else None,
        "session_expires_at": int(value.get("expires_at") or 0),
        "agent_version": AGENT_VERSION,
        "required_extension_version": AGENT_VERSION,
        "bridge_protocol_version": 2,
    }


def _post_idempotency_fingerprint(
    path: str, payload: dict[str, Any], session: dict[str, Any]
) -> str:
    settings = load_agent_settings()
    canonical = json.dumps(
        {
            "data_root": str(Path(DATA_DIR).resolve()),
            "path": path,
            "subject": str(session.get("subject") or ""),
            "store_key": str(settings.get("store_key") or "").lower(),
            "account_key": str(settings.get("qianchuan_account_key") or "").lower(),
            "payload": payload,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _begin_idempotent_post(
    path: str, payload: dict[str, Any], session: dict[str, Any]
) -> dict[str, Any]:
    if path not in IDEMPOTENT_POST_PATHS:
        return {"state": "disabled"}
    key = _post_idempotency_fingerprint(path, payload, session)
    now = time.monotonic()
    with _post_idempotency_lock:
        stale = [
            item_key
            for item_key, item in _post_idempotency_cache.items()
            if (
                item.get("state") == "complete" and float(item.get("expires_at") or 0) <= now
            ) or (
                item.get("state") == "in_progress"
                and now - float(item.get("started_at") or 0) >= POST_IDEMPOTENCY_INFLIGHT_SECONDS
            )
        ]
        for item_key in stale:
            _post_idempotency_cache.pop(item_key, None)
        existing = _post_idempotency_cache.get(key)
        if existing and existing.get("state") == "complete":
            return {
                "state": "complete",
                "status": int(existing.get("status") or 200),
                "value": copy.deepcopy(existing.get("value")),
            }
        if existing and existing.get("state") == "in_progress":
            return {"state": "in_progress"}
        if len(_post_idempotency_cache) >= POST_IDEMPOTENCY_CACHE_LIMIT:
            # Remove the oldest completed receipt. In-flight requests are never
            # evicted, so capacity pressure fails closed instead of duplicating.
            completed = sorted(
                (
                    (float(item.get("expires_at") or 0), item_key)
                    for item_key, item in _post_idempotency_cache.items()
                    if item.get("state") == "complete"
                )
            )
            if completed:
                _post_idempotency_cache.pop(completed[0][1], None)
            else:
                return {"state": "capacity"}
        _post_idempotency_cache[key] = {"state": "in_progress", "started_at": now}
    return {"state": "reserved", "key": key}


def _finish_idempotent_post(key: str, value: Any, status: int) -> None:
    if not key:
        return
    with _post_idempotency_lock:
        current = _post_idempotency_cache.get(key)
        if not current or current.get("state") != "in_progress":
            return
        if 200 <= status < 300:
            _post_idempotency_cache[key] = {
                "state": "complete",
                "status": status,
                "value": copy.deepcopy(value),
                "expires_at": time.monotonic() + POST_IDEMPOTENCY_TTL_SECONDS,
            }
        else:
            _post_idempotency_cache.pop(key, None)


class Handler(BaseHTTPRequestHandler):
    server_version = f"DianAgent/{AGENT_VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug(fmt, *args)

    def _cors(self) -> None:
        origin = str(self.headers.get("Origin") or "").strip().lower().rstrip("/")
        policy = resolve_deployment_policy()
        extension_id = self._extension_origin_id()
        allowed = (
            extension_origin_trusted(DATA_DIR.parent, extension_id)
            if policy.local_companion and extension_id
            else request_origin_allowed(origin, policy) if not policy.local_companion else False
        )
        if allowed:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _extension_origin_id(self) -> str:
        origin = str(self.headers.get("Origin") or "").strip().lower().rstrip("/")
        match = re.fullmatch(r"chrome-extension://([a-p]{32})", origin)
        return match.group(1) if match else ""

    def _paired_extension_origin(self) -> bool:
        """Require the already paired Chromium extension for sensitive reads/writes.

        Chromium does not consistently attach ``Origin`` to extension GETs.
        ``_session_authorized`` therefore records the already HMAC-validated
        extension subject, which is as strong as the Origin-bound path and is
        safe to use as the fallback identity here.
        """

        extension_id = self._extension_origin_id()
        if not extension_id:
            subject = str(
                getattr(self, "_authenticated_session", {}).get("subject") or ""
            ).strip().lower()
            if CHROMIUM_EXTENSION_ID_PATTERN.fullmatch(subject):
                extension_id = subject
        return bool(extension_id and extension_origin_trusted(DATA_DIR.parent, extension_id))

    def _browser_like_without_origin(self) -> bool:
        if any(str(self.headers.get(name) or "") for name in ("Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-Dest")):
            return True
        user_agent = str(self.headers.get("User-Agent") or "").lower()
        return any(marker in user_agent for marker in ("chrome/", "chromium/", "edg/", "firefox/", "safari/"))

    def _session_authorized(self, *, originless_compatibility_path: str = "") -> bool:
        """Validate an origin-bound extension session or an internal CLI session."""

        origin = str(self.headers.get("Origin") or "").strip()
        origin_extension_id = self._extension_origin_id()
        claimed_extension_id = str(
            self.headers.get(EXTENSION_ID_HEADER) or ""
        ).strip().lower()
        extension_id = ""
        if origin:
            # Never let an arbitrary web Origin opt into extension semantics by
            # merely claiming a public extension ID in a request header.
            if (
                not origin_extension_id
                or (claimed_extension_id and claimed_extension_id != origin_extension_id)
                or not extension_origin_trusted(DATA_DIR.parent, origin_extension_id)
            ):
                self._json({
                    "error": "extension_origin_not_trusted",
                    "message": "当前浏览器扩展尚未由安装器或官方商店授权。",
                }, 403)
                return False
            extension_id = origin_extension_id
        elif claimed_extension_id:
            # Extension GET requests may omit Origin.  The claimed ID is not a
            # credential: the short-lived token below must still validate as a
            # token signed for this exact trusted subject and extension version.
            if (
                not CHROMIUM_EXTENSION_ID_PATTERN.fullmatch(claimed_extension_id)
                or not extension_origin_trusted(DATA_DIR.parent, claimed_extension_id)
            ):
                self._json({
                    "error": "extension_origin_not_trusted",
                    "message": "当前浏览器扩展尚未由安装器或官方商店授权。",
                }, 403)
                return False
            extension_id = claimed_extension_id

        if extension_id:
            reported_version = str(
                self.headers.get("X-Dian-Agent-Extension-Version") or ""
            ).strip()
            activation = _current_extension_activation_status(reported_version)
            if activation.get("activation", {}).get("ready") is not True:
                self._json({
                    "error": "agent_extension_version_mismatch",
                    "message": "The running extension, installed extension and local Agent versions must match.",
                    "reauthenticate": True,
                    **activation,
                }, 409)
                return False
            expected_subject = extension_id
            expected_extension_version = reported_version
        else:
            token = str(self.headers.get(LOCAL_AUTH_HEADER) or "").strip()
            if (
                not token
                and originless_compatibility_path in ORIGINLESS_MAINTENANCE_GET_PATHS
                and not self._browser_like_without_origin()
            ):
                # Only the redacted maintenance summary retains legacy urllib /
                # PowerShell compatibility. Raw business routes fail closed.
                self._originless_maintenance = True
                return True
            expected_subject = INTERNAL_CLIENT_SUBJECT
            expected_extension_version = None
        try:
            session = validate_session_token(
                DATA_DIR.parent,
                str(self.headers.get(LOCAL_AUTH_HEADER) or ""),
                expected_subject=expected_subject,
                expected_extension_version=expected_extension_version,
            )
        except LocalApiAuthError as error:
            status = (
                409
                if error.code == "agent_session_extension_version_mismatch"
                else 401
            )
            self._json({
                "error": error.code,
                "message": str(error),
                "reauthenticate": True,
                "session_endpoint": "/auth/session" if origin else None,
            }, status)
            return False
        self._authenticated_session = session
        return True

    def _json(self, value: Any, status: int = 200) -> None:
        try:
            body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            # Never emit Python's NaN/Infinity extensions or leave the client
            # with a truncated response when damaged legacy state contains an
            # unserializable value. External writes are rejected at ingress;
            # this is the final fail-closed guard for existing local evidence.
            logger.exception("Refusing to emit a non-standard JSON response")
            value = {
                "error": "INTERNAL_RESPONSE_SERIALIZATION_FAILED",
                "message": "Local response data is invalid; refresh the affected snapshot before retrying.",
                "retryable": True,
            }
            status = 500
            body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        idempotency_key = str(getattr(self, "_active_idempotency_key", "") or "")
        _finish_idempotent_post(idempotency_key, value, status)
        self._active_idempotency_key = ""
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        if getattr(self, "_idempotent_replay", False):
            self.send_header("X-Dian-Agent-Idempotent-Replay", "true")
            self.send_header("Access-Control-Expose-Headers", "X-Dian-Agent-Idempotent-Replay")
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
        self.send_header(
            "Access-Control-Allow-Headers",
            f"Content-Type, X-Dian-Agent, {EXTENSION_ID_HEADER}, X-Dian-Agent-Extension-Version, {LOCAL_AUTH_HEADER}",
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed_url = urlparse(self.path)
        path = unquote(parsed_url.path).rstrip("/") or "/"
        query = parse_qs(parsed_url.query)
        policy = resolve_deployment_policy()
        if not policy.local_companion and path not in {"/health", "/health/live", "/marketplace/status", "/marketplace/readiness"}:
            self._json({
                "error": "marketplace_authenticated_gateway_required",
                "deployment_mode": policy.mode,
                "message": "服务市场数据读取必须经租户/店铺绑定的认证网关。",
            }, 403)
            return
        auth_exempt = {
            "/activation/status",
            "/health",
            "/health/live",
            "/oauth/oceanengine/callback",
            "/marketplace/status",
            "/marketplace/readiness",
        }
        if policy.local_companion and path not in auth_exempt and not self._session_authorized(
            originless_compatibility_path=path
        ):
            return
        if path in {"/ai/status", "/ai/context-preview", "/ai/proposals"} and not self._paired_extension_origin():
            self._json({
                "error": "paired_extension_required",
                "message": "AI 状态、脱敏数据预览和提案仅允许当前已配对的店策扩展读取。",
            }, 403)
            return
        if path in {"/health", "/health/live"}:
            self._json(
                {
                    "status": "ok",
                    "health_contract_version": 2,
                    "probe": "liveness",
                    "version": AGENT_VERSION,
                    "platform": platform_id(),
                    "architecture": platform.machine().lower() or "unknown",
                    "bridge_protocol_version": 2,
                    "mode": policy.mode,
                    "mode_kind": "deployment",
                    "deployment": policy.public_status(),
                    "details": {
                        "status": "deferred",
                        "endpoint": "/system/status",
                        "sections": ["execution", "database", "storage", "scan"],
                    },
                }
            )
            return
        if path == "/activation/status":
            self._json(_current_extension_activation_status(str(
                self.headers.get("X-Dian-Agent-Extension-Version") or ""
            )))
            return
        if path == "/ai/status":
            self._json(get_ai_status(query.get("provider", [None])[0]))
            return
        if path == "/ai/context-preview":
            self._json(build_ai_context_preview())
            return
        if path == "/ai/proposals":
            try:
                limit = _bounded_integer(
                    query.get("limit", ["50"])[0],
                    field_name="limit",
                    default=50,
                    minimum=1,
                    maximum=100,
                )
            except ValueError as error:
                self._json({
                    "error": "INVALID_QUERY_PARAMETER",
                    "message": str(error),
                    "parameter": "limit",
                }, 400)
                return
            self._json(get_ai_proposals(limit))
            return
        if path == "/oauth/oceanengine/status":
            self._json(OceanEngineOAuth(DATA_DIR).status())
            return
        if path == "/oauth/oceanengine/sync-status":
            self._json(load_sync_status(DATA_DIR))
            return
        if path == "/oauth/oceanengine/account-center":
            self._json(build_oceanengine_account_center())
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
        if path in {"/product-operating-graph", "/commerce/product-chain"}:
            self._json(_cached("product_graph", build_douyin_product_graph))
            return
        if path == "/shelf-analysis":
            self._json(_cached("shelf", build_shelf_analysis))
            return
        if path == "/live-analysis":
            self._json(_cached("live", build_live_analysis))
            return
        if path == "/live-pacing-analysis":
            self._json(_cached("live_pacing", lambda: build_live_analysis()["pacing"]))
            return
        if path == "/qianchuan-creative-analysis":
            self._json(_cached("creative", build_qianchuan_creative_analysis))
            return
        if path == "/qianchuan-accounts":
            self._json(build_store_catalog())
            return
        if path == "/qianchuan/plan-console":
            self._json(build_qianchuan_plan_console())
            return
        if path == "/qianchuan/promotion-readiness":
            self._json(build_current_promotion_readiness())
            return
        if path == "/chengfang/autopilot":
            self._json(build_current_chengfang_autopilot())
            return
        if path == "/chengfang/a2-pilot":
            runtime = _current_chengfang_runtime()
            runtime.enforce_a2_safety()
            self._json(runtime.a2_pilot_summary())
            return
        if path == "/chengfang/production-write":
            self._json(build_current_chengfang_production_write())
            return
        if path == "/chengfang/control-tasks":
            self._json(build_current_chengfang_control_tasks())
            return
        if path == "/chengfang/schedule-control":
            self._json(build_current_chengfang_schedule_control())
            return
        if path == "/chengfang/evidence":
            self._json(hydrate_current_chengfang_profile(save=False))
            return
        if path == "/stores":
            self._json(build_store_catalog())
            return
        if path == "/auth/status":
            self._json(
                _authenticated_session_status(
                    getattr(self, "_authenticated_session", {})
                )
            )
            return
        if path == "/system/status":
            self._json(
                _maintenance_system_status()
                if getattr(self, "_originless_maintenance", False)
                else build_system_status()
            )
            return
        if path == "/storage/lifecycle":
            self._json(build_storage_lifecycle_status())
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
        if path == "/rules/packs":
            store_key = str(load_agent_settings().get("store_key") or "").lower()
            self._json(_update_center().knowledge_catalog(store_key=store_key))
            return
        if path == "/memory":
            try:
                store_key, account_key = _current_memory_scope(query)
            except ValueError as error:
                self._json({"error": "SCOPE_MISMATCH", "message": str(error)}, 409)
                return
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
            try:
                limit = _bounded_integer(
                    query.get("limit", ["100"])[0],
                    field_name="limit",
                    default=100,
                    minimum=1,
                    maximum=500,
                )
            except ValueError as error:
                self._json({"error": "INVALID_QUERY_PARAMETER", "message": str(error), "parameter": "limit"}, 400)
                return
            self._json(get_action_audit(limit))
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
        if path == "/actions/execution/readback-job":
            try:
                job = recover_execution_readback_job(query.get("action_id", [""])[0])
                self._json({"ok": True, "job": job, "executed": False, "write_enabled": False})
            except ValueError as error:
                self._json({"error": _safe_client_error(error), "executed": False, "write_enabled": False}, 400)
            except OSError:
                logger.exception("无法从本地账本恢复只读执行回读任务")
                self._json({
                    "error": "LOCAL_STORAGE_UNAVAILABLE",
                    "message": "本地执行账本暂时不可读；动作保持锁定，修复存储后再回读。",
                    "retryable": True,
                    "executed": False,
                    "write_enabled": False,
                }, 503)
            return
        if path == "/actions/shadow":
            self._json(build_shadow_execution_report())
            return
        if path == "/actions/effectiveness":
            self._json(build_execution_effectiveness_report())
            return
        if path == "/product/capability":
            self._json(build_product_capability_diagnostic(
                onboarding=build_onboarding_status(),
                operation_context=build_operation_context(),
                plan_console=build_qianchuan_plan_console(),
                automation_readiness=build_automation_readiness(),
                preflight=build_execution_preflight_report(),
                effectiveness=build_execution_effectiveness_report(),
            ))
            return
        if path == "/value-ledger":
            self._json(build_value_ledger())
            return
        if path == "/trends":
            try:
                days = _bounded_integer(
                    query.get("days", ["7"])[0],
                    field_name="days",
                    default=7,
                    minimum=1,
                    maximum=90,
                )
            except ValueError as error:
                self._json({"error": "INVALID_QUERY_PARAMETER", "message": str(error), "parameter": "days"}, 400)
                return
            self._json(build_trends(days, query.get("source", [None])[0], query.get("page_type", [None])[0]))
            return
        if path == "/settings":
            self._json(load_agent_settings())
            return
        if path == "/integrations":
            try:
                self._json(get_integration_settings())
            except (IntegrationSecretStoreError, OSError, ValueError):
                self._json({
                    "error": "INTEGRATION_SECRET_STORE_UNAVAILABLE",
                    "message": "通知密钥存储暂时不可用；为保护 Webhook，连接读取已停止。",
                    "retryable": True,
                    "secrets_exposed": False,
                }, 503)
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
        if path in {
            "/ai/providers/configure",
            "/ai/providers/test",
            "/ai/providers/disable-all",
            "/ai/shadow/run",
        } and not self._paired_extension_origin():
            self._json({
                "error": "paired_extension_required",
                "message": "AI 连接、密钥保存和影子分析仅允许当前已配对的店策扩展发起。",
            }, 403)
            return
        if path in {
            "/chengfang/production-write/prepare",
            "/chengfang/production-write/authorize",
            "/chengfang/production-write/execute",
            "/chengfang/production-write/reconcile",
            "/chengfang/production-write/cancel",
            "/chengfang/production-write/stop",
            "/chengfang/production-write/resume",
        } and not self._paired_extension_origin():
            self._json({
                "error": "paired_extension_required",
                "message": "乘方生产写入只允许当前已配对、已认证的店策扩展发起。",
            }, 403)
            return
        if path in {"/rules/packs/import", "/rules/packs/bind"} and not self._paired_extension_origin():
            self._json({
                "error": "paired_extension_required",
                "message": "行业知识包安装与绑定仅允许当前已配对的店策扩展发起。",
            }, 403)
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
            payload = json.loads(
                self.rfile.read(length),
                parse_float=_parse_finite_json_float,
                parse_constant=_reject_nonfinite_json_constant,
            )
            if not isinstance(payload, dict):
                raise ValueError("请求正文必须是 JSON 对象")
            if path == "/auth/session":
                if self.headers.get("X-Dian-Agent") != "2":
                    self._json({
                        "error": "bridge_protocol_upgrade_required",
                        "message": "安全会话仅支持 Dian Agent bridge protocol 2。",
                    }, 403)
                    return
                extension_id = self._extension_origin_id()
                requested_extension_id = str(payload.get("extension_id") or "").strip().lower()
                if (
                    not extension_id
                    or requested_extension_id != extension_id
                    or not extension_origin_trusted(DATA_DIR.parent, extension_id)
                ):
                    self._json({
                        "error": "extension_pairing_not_authorized",
                        "message": "该扩展 ID 尚未由安装器或官方商店预授权。",
                    }, 403)
                    return
                header_extension_version = str(
                    self.headers.get("X-Dian-Agent-Extension-Version") or ""
                ).strip()
                requested_extension_version = str(
                    payload.get("extension_version") or ""
                ).strip()
                activation = _current_extension_activation_status(
                    header_extension_version
                )
                if (
                    not header_extension_version
                    or requested_extension_version != header_extension_version
                    or activation.get("activation", {}).get("ready") is not True
                ):
                    self._json({
                        "error": "agent_extension_version_mismatch",
                        "message": "Update or reload the browser extension before connecting to this local Agent.",
                        "reauthenticate": True,
                        **activation,
                    }, 409)
                    return
                try:
                    session = issue_session_token(
                        DATA_DIR.parent,
                        extension_id,
                        extension_version=requested_extension_version,
                    )
                except LocalApiAuthError as error:
                    self._json({
                        "error": error.code,
                        "message": str(error),
                        "repair_required": True,
                    }, 503)
                    return
                self._json({
                    "ok": True,
                    **session,
                    "authentication": "origin_bound_short_lived_session",
                    "installation_secret_exposed": False,
                })
                return
            if not self._session_authorized():
                return
            idempotency = _begin_idempotent_post(
                path,
                payload,
                getattr(self, "_authenticated_session", {}),
            )
            if idempotency["state"] == "complete":
                self._idempotent_replay = True
                self._json(idempotency["value"], int(idempotency["status"]))
                return
            if idempotency["state"] == "in_progress":
                self._json({
                    "error": "REQUEST_ALREADY_IN_PROGRESS",
                    "message": "An identical protected request is already in progress.",
                    "retryable": True,
                }, 409)
                return
            if idempotency["state"] == "capacity":
                self._json({
                    "error": "IDEMPOTENCY_CAPACITY_EXHAUSTED",
                    "message": "Protected request capacity is temporarily exhausted.",
                    "retryable": True,
                }, 503)
                return
            self._active_idempotency_key = str(idempotency.get("key") or "")
            if path == "/ai/providers/configure":
                self._json(configure_ai_provider(payload))
                return
            if path == "/ai/providers/test":
                self._json(test_ai_provider(payload))
                return
            if path == "/ai/providers/disable-all":
                self._json(disable_all_ai_providers(payload))
                return
            if path == "/ai/shadow/run":
                self._json(run_ai_shadow(payload))
                return
            if path == "/stores/current-page-grant":
                self._json({"ok": True, **issue_current_page_store_grant()})
                return
            if path == "/push":
                source = payload.get("source")
                data = payload.get("data")
                expected_scope = payload.get("expected_scope") if isinstance(payload.get("expected_scope"), dict) else {}
                scan_context = payload.get("scan_context") if isinstance(payload.get("scan_context"), dict) else {}
                execution_context = payload.get("execution_context") if isinstance(payload.get("execution_context"), dict) else {}
                current_page_context = payload.get("current_page_context") if isinstance(payload.get("current_page_context"), dict) else {}
                push_response = save_scan_page_once(
                    source,
                    data,
                    expected_scope,
                    scan_context,
                    execution_context,
                    current_page_context,
                )
                _invalidate_cache()
                self._json(
                    push_response,
                    409 if push_response.get("quarantined") is True or push_response.get("accepted") is False else 200,
                )
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
            if path == "/stores/unlink":
                result = unlink_store_account(str(payload.get("store_key") or ""), str(payload.get("account_key") or ""))
                _invalidate_cache()
                self._json({"ok": True, **result})
                return
            if path == "/chengfang/autopilot/profile":
                runtime = _current_chengfang_runtime()
                profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else payload
                saved = runtime.save_profile(profile)
                self._json({"ok": True, "saved": saved, "autopilot_runtime": runtime.summary(build_chengfang_contract_registry())})
                return
            if path == "/chengfang/autopilot/shadow":
                if not isinstance(payload.get("enabled"), bool):
                    raise ValueError("enabled 必须为 true 或 false")
                runtime = _current_chengfang_runtime()
                profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else None
                shadow = runtime.set_shadow(payload["enabled"], profile)
                evaluation = runtime.evaluate(trigger="manual") if payload["enabled"] else None
                self._json({
                    "ok": True,
                    "shadow": shadow,
                    "evaluation": evaluation,
                    "autopilot_runtime": runtime.summary(build_chengfang_contract_registry()),
                    "execution_allowed": False,
                    "platform_write_attempted": False,
                })
                return
            if path == "/chengfang/autopilot/evaluate":
                account_key, context, runtime = _resolve_current_chengfang_session()
                if isinstance(payload.get("profile"), dict):
                    runtime.save_profile(payload["profile"])
                hydration = _hydrate_resolved_chengfang_profile(runtime, context, account_key, save=True)
                evaluation = runtime.evaluate(trigger=str(payload.get("trigger") or "manual"))
                self._json({
                    "ok": True,
                    "evaluation": evaluation,
                    "evidence_hydration": hydration,
                    "autopilot_runtime": runtime.summary(build_chengfang_contract_registry()),
                    "execution_allowed": False,
                    "platform_write_attempted": False,
                })
                return
            if path == "/chengfang/evidence/hydrate":
                account_key, context, runtime = _resolve_current_chengfang_session()
                hydrated = _hydrate_resolved_chengfang_profile(runtime, context, account_key, save=True)
                self._json({
                    "ok": hydrated.get("status") in {"hydrated", "partial"},
                    "evidence_hydration": hydrated,
                    "autopilot_runtime": runtime.summary(build_chengfang_contract_registry()),
                })
                return
            if path == "/chengfang/autopilot/feedback":
                runtime = _current_chengfang_runtime()
                evaluation = runtime.record_feedback(
                    payload.get("evaluation_id"),
                    payload.get("usefulness"),
                    readback_success=payload.get("readback_success"),
                    critical_incident=payload.get("critical_incident", False),
                    note=payload.get("note"),
                )
                self._json({
                    "ok": True,
                    "evaluation": evaluation,
                    "autopilot_runtime": runtime.summary(build_chengfang_contract_registry()),
                })
                return
            if path == "/chengfang/autopilot/stop":
                runtime = _current_chengfang_runtime()
                runtime.emergency_stop(confirm=payload.get("confirm") is True, reason=payload.get("reason"))
                production_stop = None
                if COMMERCIAL_RUNTIME_LOADED:
                    production_stop = ChengfangProductionController.emergency_stop_global(
                        DATA_DIR,
                        confirm=payload.get("confirm") is True,
                        reason=payload.get("reason") or "AUTOPILOT_EMERGENCY_STOP",
                        integrity_key=_identity_secret(),
                    )
                self._json({
                    "ok": True,
                    "autopilot_runtime": runtime.summary(build_chengfang_contract_registry()),
                    "execution_allowed": False,
                    "platform_write_attempted": False,
                    "production_stop": production_stop,
                    "production_write": build_current_chengfang_production_write(),
                })
                return
            if path == "/chengfang/autopilot/reset":
                runtime = _current_chengfang_runtime()
                runtime.reset(confirm=payload.get("confirm") is True)
                self._json({"ok": True, "autopilot_runtime": runtime.summary(build_chengfang_contract_registry())})
                return
            if path == "/chengfang/production-write/prepare":
                _targets, _adapter, controller = _current_chengfang_production_components()
                prepared = controller.prepare(
                    payload.get("target_key"),
                    payload.get("target_budget"),
                )
                self._json({
                    "ok": True,
                    "operation": prepared,
                    "production_write": build_current_chengfang_production_write(),
                })
                return
            if path == "/chengfang/production-write/authorize":
                ttl_seconds = _bounded_integer(
                    payload.get("ttl_seconds"),
                    field_name="ttl_seconds",
                    default=120,
                    minimum=30,
                    maximum=600,
                )
                _targets, _adapter, controller = _current_chengfang_production_components()
                operation = controller.authorize(
                    payload.get("operation_id"),
                    confirm=payload.get("confirm") is True,
                    confirmation_phrase=payload.get("confirmation_phrase"),
                    ttl_seconds=ttl_seconds,
                )
                self._json({
                    "ok": True,
                    "operation": operation,
                    "production_write": build_current_chengfang_production_write(),
                })
                return
            if path == "/chengfang/production-write/execute":
                _targets, _adapter, controller = _current_chengfang_production_components()
                operation = controller.execute(payload.get("operation_id"))
                self._json({
                    "ok": operation.get("status") == "verified",
                    "operation": operation,
                    "production_write": build_current_chengfang_production_write(),
                })
                return
            if path == "/chengfang/production-write/reconcile":
                if payload.get("confirm") is not True:
                    raise ChengfangProductionControllerError("EXPLICIT_RECONCILIATION_REQUIRED")
                _targets, _adapter, controller = _current_chengfang_production_components()
                operation = controller.reconcile(
                    payload.get("operation_id"),
                    client_observation=payload.get("observation") if "observation" in payload else None,
                )
                self._json({
                    "ok": operation.get("status") == "verified",
                    "operation": operation,
                    "production_write": build_current_chengfang_production_write(),
                })
                return
            if path == "/chengfang/production-write/cancel":
                if payload.get("confirm") is not True:
                    raise ChengfangProductionControllerError("EXPLICIT_CANCELLATION_REQUIRED")
                _targets, _adapter, controller = _current_chengfang_production_components()
                operation = controller.cancel(payload.get("operation_id"))
                self._json({
                    "ok": True,
                    "operation": operation,
                    "production_write": build_current_chengfang_production_write(),
                })
                return
            if path == "/chengfang/production-write/stop":
                stopped = ChengfangProductionController.emergency_stop_global(
                    DATA_DIR,
                    confirm=payload.get("confirm") is True,
                    reason=payload.get("reason"),
                    integrity_key=_identity_secret(),
                )
                self._json({
                    "ok": True,
                    "production_stop": stopped,
                    "production_write": build_current_chengfang_production_write(),
                })
                return
            if path == "/chengfang/production-write/resume":
                resumed = ChengfangProductionController.resume_global(
                    DATA_DIR,
                    confirm=payload.get("confirm") is True,
                    confirmation_phrase=payload.get("confirmation_phrase"),
                    integrity_key=_identity_secret(),
                )
                self._json({
                    "ok": True,
                    "production_resume": resumed,
                    "production_write": build_current_chengfang_production_write(),
                })
                return
            if path == "/chengfang/a2-pilot/demo":
                if str(payload.get("environment") or "demo") != "demo":
                    raise ValueError("独立 A2 演示只允许在 demo 环境运行")
                fixture = build_a2_demo_fixture(
                    confirm=payload.get("confirm") is True,
                    generated_at_ms=int(time.time() * 1000),
                )
                self._json({"ok": True, **fixture})
                return
            if path == "/chengfang/a2-pilot/configure":
                runtime = _current_chengfang_runtime()
                result = runtime.configure_a2_pilot(payload)
                self._json({"ok": True, "a2_pilot": result})
                return
            if path == "/chengfang/a2-pilot/candidate/review":
                runtime = _current_chengfang_runtime()
                candidate = runtime.review_candidate(
                    payload.get("candidate_id"),
                    payload.get("decision"),
                    note=payload.get("note"),
                )
                self._json({"ok": True, "candidate": candidate, "a2_pilot": runtime.a2_pilot_summary()})
                return
            if path == "/chengfang/a2-pilot/candidate/execute":
                if "auto_readback" in payload and not isinstance(payload["auto_readback"], bool):
                    self._json({
                        "error": "INVALID_REQUEST_PARAMETER",
                        "message": "auto_readback must be true or false.",
                        "parameter": "auto_readback",
                    }, 400)
                    return
                runtime = _current_chengfang_runtime()
                result = runtime.execute_candidate(
                    payload.get("candidate_id"),
                    auto_readback=payload.get("auto_readback", True),
                )
                self._json({"ok": True, **result})
                return
            if path == "/chengfang/a2-pilot/execution/readback":
                runtime = _current_chengfang_runtime()
                observation = payload.get("observation") if isinstance(payload.get("observation"), dict) else None
                result = runtime.readback_execution(payload.get("execution_id"), observation=observation)
                self._json({"ok": True, **result})
                return
            if path == "/chengfang/a2-pilot/stop":
                runtime = _current_chengfang_runtime()
                result = runtime.stop_a2_pilot(
                    confirm=payload.get("confirm") is True,
                    reason=payload.get("reason"),
                )
                self._json({"ok": True, "a2_pilot": result})
                return
            if path == "/chengfang/control-tasks/draft":
                result = create_chengfang_control_task(payload)
                self._json({"ok": True, **result, "control_tasks": build_current_chengfang_control_tasks()})
                return
            if path == "/chengfang/control-tasks/review":
                runtime = _current_chengfang_runtime()
                center = _current_chengfang_control_task_center(runtime)
                task = center.review(
                    payload.get("task_id"),
                    payload.get("decision"),
                    confirm=payload.get("confirm") is True,
                    note=payload.get("note"),
                )
                self._json({"ok": True, "task": task, "control_tasks": center.summary(
                    importable_candidates=_importable_chengfang_control_candidates(runtime),
                )})
                return
            if path == "/chengfang/control-tasks/simulate":
                runtime = _current_chengfang_runtime()
                center = _current_chengfang_control_task_center(runtime)
                result = center.simulate(payload.get("task_id"))
                self._json({"ok": True, **result, "control_tasks": center.summary(
                    importable_candidates=_importable_chengfang_control_candidates(runtime),
                )})
                return
            if path == "/chengfang/control-tasks/readback":
                runtime = _current_chengfang_runtime()
                center = _current_chengfang_control_task_center(runtime)
                observation = payload.get("observation") if isinstance(payload.get("observation"), dict) else None
                result = center.readback(payload.get("task_id"), observation)
                self._json({"ok": True, **result, "control_tasks": center.summary(
                    importable_candidates=_importable_chengfang_control_candidates(runtime),
                )})
                return
            if path == "/chengfang/schedule-control/draft":
                if payload.get("use_current_plan") is not True:
                    raise ValueError("定时启停必须绑定当前已选择的计划作用域")
                runtime = _current_chengfang_runtime()
                center = _current_chengfang_schedule_control(runtime)
                result = center.create_draft({
                    "enabled": payload.get("enabled"),
                    "time_ranges": payload.get("time_ranges"),
                })
                self._json({"ok": True, **result, "schedule_control": center.summary()})
                return
            if path == "/chengfang/schedule-control/review":
                runtime = _current_chengfang_runtime()
                center = _current_chengfang_schedule_control(runtime)
                revision = center.review(
                    payload.get("revision_id"),
                    payload.get("decision"),
                    confirm=payload.get("confirm") is True,
                )
                self._json({"ok": True, "revision": revision, "schedule_control": center.summary()})
                return
            if path == "/chengfang/schedule-control/simulate":
                runtime = _current_chengfang_runtime()
                center = _current_chengfang_schedule_control(runtime)
                result = center.simulate(payload.get("revision_id"))
                self._json({"ok": True, **result, "schedule_control": center.summary()})
                return
            if path == "/chengfang/schedule-control/readback":
                runtime = _current_chengfang_runtime()
                center = _current_chengfang_schedule_control(runtime)
                result = center.readback(payload.get("revision_id"))
                self._json({"ok": True, **result, "schedule_control": center.summary()})
                return
            if path == "/settings":
                current_settings = load_agent_settings()
                for scope_key in ("store_key", "qianchuan_account_key"):
                    if scope_key in payload and str(payload.get(scope_key) or "").lower() != str(
                        current_settings.get(scope_key) or ""
                    ).lower():
                        raise ValueError("Store/account scope changes must use /stores/select.")
                settings = save_agent_settings(payload)
                _invalidate_cache()
                self._json({"ok": True, "settings": settings})
                return
            if path == "/memory/upsert":
                store_key, account_key = _bound_current_scope(
                    payload.get("store_key"), payload.get("account_key")
                )
                memory_payload = dict(payload)
                memory_payload["store_key"] = store_key
                memory_payload["account_key"] = account_key
                saved = upsert_operator_memory(DATA_DIR, memory_payload)
                self._json(saved)
                return
            if path == "/memory/archive":
                store_key, account_key = _bound_current_scope(
                    payload.get("store_key"), payload.get("account_key")
                )
                archived = archive_operator_memory(
                    DATA_DIR,
                    payload.get("id"),
                    store_key,
                    account_key,
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
                requested_extension_id = str(payload.get("extension_id") or "").strip().lower()
                origin_extension_id = origin_match.group(1) if origin_match else ""
                if not origin_extension_id or requested_extension_id != origin_extension_id:
                    raise ValueError("extension source must match the requesting extension origin")
                if not extension_pairing_allowed(DATA_DIR.parent, origin_extension_id):
                    self._json({
                        "error": "extension_pairing_not_authorized",
                        "message": "该扩展 ID 尚未由安装器、官方商店或显式开发配对批准。",
                    }, 403)
                    return
                result = save_extension_install_state(
                    DATA_DIR.parent,
                    payload,
                    origin_extension_id=origin_extension_id,
                    pairing_authorized=True,
                )
                audit_completed = complete_late_install_verification(
                    DATA_DIR.parent,
                    result,
                    authenticated_subject=str(
                        getattr(self, "_authenticated_session", {}).get("subject") or ""
                    ),
                    origin_extension_id=origin_extension_id,
                    origin_trusted=extension_origin_trusted(
                        DATA_DIR.parent, origin_extension_id
                    ),
                )
                if audit_completed:
                    logger.info(
                        "Completed pending installation verification from an authenticated extension report."
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
                raise ValueError("旧版导入即启用入口已停用；请使用行业知识包中心安装后，再明确应用到当前店铺")
            if path == "/rules/packs/import":
                pack = payload.get("pack")
                if not isinstance(pack, dict):
                    raise ValueError("pack 必须是知识包对象")
                result = _update_center().import_industry_pack(pack)
                _save_update_settings({"last_check_at": _now_label(), "last_check": None})
                self._json({
                    **result,
                    "message": f"{result.get('display_name')} {result.get('pack_version')} 已验签安装；请选择当前店铺后再应用",
                })
                return
            if path == "/rules/packs/bind":
                current_store = str(load_agent_settings().get("store_key") or "").lower()
                requested_store = str(payload.get("store_key") or current_store).lower()
                if not current_store or requested_store != current_store:
                    raise ValueError("请先选择当前店铺；行业知识包不能跨店铺静默绑定")
                result = _update_center().bind_industry_pack(
                    current_store,
                    str(payload.get("pack_id") or "general"),
                    pack_version=str(payload.get("pack_version") or ""),
                )
                _invalidate_cache()
                self._json(result)
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
                pack = _update_center().load_effective_pack(
                    store_key=str(load_agent_settings().get("store_key") or "")
                )
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
            if path == "/oauth/oceanengine/account-center/preferences":
                oauth, sync_status, store_catalog = _oceanengine_account_center_inputs()
                center_service = OceanEngineAccountCenter(DATA_DIR)
                known_keys = {
                    str(account.get("account_key") or "")
                    for account in oauth.get("accounts", [])
                    if isinstance(account, dict)
                }
                preference = center_service.update_preference(
                    str(payload.get("account_key") or ""), payload, known_keys
                )
                center = center_service.build(oauth, sync_status, store_catalog)
                self._json({"ok": True, "preference": preference, "account_center": center})
                return
            if path == "/oauth/oceanengine/account-center/preview":
                account_keys = payload.get("account_keys")
                if not isinstance(account_keys, list):
                    raise ValueError("请选择需要预演的账户。")
                oauth, sync_status, store_catalog = _oceanengine_account_center_inputs()
                center_service = OceanEngineAccountCenter(DATA_DIR)
                center = center_service.build(oauth, sync_status, store_catalog)
                preview = center_service.preview(
                    [str(value) for value in account_keys],
                    str(payload.get("action") or ""),
                    center,
                )
                self._json({"ok": True, "preview": preview})
                return
            if path == "/oauth/oceanengine/account-center/sync":
                days = _bounded_integer(
                    payload.get("days"),
                    field_name="days",
                    default=7,
                    minimum=1,
                    maximum=30,
                )
                account_keys = payload.get("account_keys")
                if not isinstance(account_keys, list):
                    raise ValueError("请选择需要同步的账户。")
                oauth, sync_status, store_catalog = _oceanengine_account_center_inputs()
                center_service = OceanEngineAccountCenter(DATA_DIR)
                center = center_service.build(oauth, sync_status, store_catalog)
                preview = center_service.preview(
                    [str(value) for value in account_keys], "sync", center
                )
                eligible_keys = [str(item["account_key"]) for item in preview["eligible"]]
                if not eligible_keys:
                    raise ValueError("所选账户均不满足同步条件，请先修复授权或开启同步。")
                result = OceanEngineDataClient(OceanEngineOAuth(DATA_DIR)).sync(
                    save_data,
                    _raw_oceanengine_account_ids(eligible_keys),
                    days,
                )
                _invalidate_cache()
                self._json({
                    "ok": True,
                    "preview": preview,
                    "sync": result,
                    "account_center": build_oceanengine_account_center(),
                    "platform_write_enabled": False,
                })
                return
            if path == "/oauth/oceanengine/sync":
                days = _bounded_integer(
                    payload.get("days"),
                    field_name="days",
                    default=7,
                    minimum=1,
                    maximum=30,
                )
                account_ids = payload.get("account_ids")
                if account_ids is not None and not isinstance(account_ids, list):
                    raise ValueError("账号选择格式不正确。")
                result = OceanEngineDataClient(
                    OceanEngineOAuth(DATA_DIR)
                ).sync(
                    save_data,
                    [str(value) for value in account_ids] if account_ids else None,
                    days,
                )
                result = _finalize_oceanengine_sync_selection(result)
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
                if "notify" in payload and not isinstance(payload["notify"], bool):
                    raise ValueError("notify must be true or false.")
                report = generate_daily_report(payload.get("date"))
                deliveries = send_report_notifications(report) if payload.get("notify") is True else []
                self._json({"ok": True, "report": report, "deliveries": deliveries})
                return
            if path == "/tasks/update":
                task_store_key, _task_account_key = _bound_current_scope(
                    payload.get("store_key")
                )
                task = update_task_state(
                    str(payload.get("task_id") or ""),
                    str(payload.get("status") or ""),
                    operator=str(payload.get("operator") or ""),
                    assignee=str(payload.get("assignee") or ""),
                    note=str(payload.get("note") or ""),
                    title=str(payload.get("title") or ""),
                    owner=str(payload.get("owner") or ""),
                    action=str(payload.get("action") or ""),
                    acceptance=str(payload.get("acceptance") or ""),
                    evidence=str(payload.get("evidence") or ""),
                    observation_window=str(payload.get("observation_window") or ""),
                    level=str(payload.get("level") or ""),
                    contract_fingerprint=str(payload.get("contract_fingerprint") or ""),
                    store_key=task_store_key,
                    business_date=str(payload.get("business_date") or "") or None,
                )
                _invalidate_cache()
                self._json({"ok": True, "task": task})
                return
            if path == "/onboarding/update":
                self._json({"ok": True, "onboarding": update_onboarding_state(str(payload.get("event") or ""))})
                return
            if path == "/tasks/track":
                task_store_key, _task_account_key = _bound_current_scope(
                    payload.get("store_key")
                )
                self._json({"ok": True, "snapshot": save_suggestion_snapshot(
                    str(payload.get("task_id") or ""),
                    payload.get("context"),
                    store_key=task_store_key,
                    business_date=str(payload.get("business_date") or "") or None,
                )})
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
            if path == "/actions/preflight/manual-reconcile/archive":
                archived = archive_manual_reconcile(
                    str(payload.get("action_id") or ""),
                    str(payload.get("confirmation_text") or ""),
                    str(payload.get("resolution_note") or ""),
                )
                self._json({
                    "ok": True,
                    **archived,
                    "executed": False,
                    "execution_enabled": False,
                    "write_enabled": False,
                })
                return
            if path == "/actions/preflight/authorize":
                report = authorize_execution_preflight(
                    str(payload.get("session_id") or ""),
                    str(payload.get("confirmation_text") or ""),
                )
                self._json({"ok": True, "preflight": report, "executed": False, "execution_enabled": False})
                return
            if path == "/actions/preflight/consume":
                consumed = consume_execution_authorization(
                    str(payload.get("authorization_id") or ""),
                    payload.get("readback_context"),
                )
                self._json({"ok": True, "grant": consumed, "executed": False, "mode": "supervised_submit"})
                return
            if path == "/actions/preflight/final-reread":
                preview = refresh_authorized_execution_baseline(
                    str(payload.get("authorization_id") or ""),
                    str(payload.get("readback_token") or ""),
                )
                self._json({"ok": True, "preview": preview, "executed": False, "mode": "supervised_submit"})
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
            if path == "/actions/execution/identity-resolve":
                resolved = resolve_execution_identity_snapshot(
                    str(payload.get("action_id") or ""),
                    str(payload.get("authorization_id") or ""),
                    payload.get("data") if isinstance(payload.get("data"), dict) else {},
                    payload.get("execution_request") if isinstance(payload.get("execution_request"), dict) else {},
                )
                self._json(resolved)
                return
            if path == "/actions/execution/verify":
                verification = verify_execution_result(
                    str(payload.get("action_id") or ""),
                    str(payload.get("readback_token") or ""),
                )
                if not isinstance(verification.get("readback"), dict) or not str(verification.get("snapshot_ref") or ""):
                    self._json({
                        "error": "EXECUTION_READBACK_SNAPSHOT_NOT_FOUND",
                        "error_code": "EXECUTION_READBACK_SNAPSHOT_NOT_FOUND",
                        "message": "该回读令牌尚未绑定可验证快照。",
                        "retryable": False,
                    }, 404)
                    return
                self._json({"ok": True, "verification": verification})
                return
            if path == "/scan-status":
                self._json({"ok": True, "scan": save_scan_status(payload)})
                return
            self._json({"error": "not_found"}, 404)
        except IntegrationSecretStoreError:
            self._json({
                "error": "INTEGRATION_SECRET_STORE_UNAVAILABLE",
                "message": "通知密钥存储暂时不可用；为保护 Webhook，本次操作未生效。",
                "retryable": True,
                "secrets_exposed": False,
            }, 503)
        except (
            json.JSONDecodeError,
            ValueError,
            TypeError,
            RecursionError,
            UpdateError,
            RollbackError,
            RulePackError,
            AIProviderError,
            ChengfangProductionControllerError,
            ChengfangProductionTargetError,
        ) as error:
            self._json({"error": _safe_client_error(error)}, 400)
        except (LocalStoreError, OSError) as error:
            logger.exception("本地存储暂时不可用: %s", path)
            self._json({
                "error": "LOCAL_STORAGE_UNAVAILABLE",
                "message": "Local storage is temporarily unavailable. See the local Agent log for details.",
                "retryable": True,
            }, 503)


class LocalAgentHTTPServer(ThreadingHTTPServer):
    """Loopback-only HTTP server with a bounded burst queue for dashboard reads."""

    request_queue_size = 32


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
    LocalAgentHTTPServer.allow_reuse_address = True
    try:
        server = LocalAgentHTTPServer(("127.0.0.1", PORT), Handler)
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
    chengfang_scheduler = threading.Thread(target=_chengfang_autopilot_scheduler, args=(stop_event,), daemon=True)
    scheduler.start()
    update_scheduler.start()
    chengfang_scheduler.start()
    logger.info("店策 Agent 本地服务已启动: http://127.0.0.1:%d", PORT)
    logger.info("方案确认模式（不执行千川操作）；数据目录: %s", DATA_DIR)
    try:
        _record_managed_runtime_start()
    except OSError as error:
        logger.warning("无法记录自动启动状态，不影响本地服务: %s", error)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("服务已停止")
    finally:
        stop_event.set()
        scheduler.join(timeout=2)
        update_scheduler.join(timeout=2)
        chengfang_scheduler.join(timeout=2)
        server.server_close()


if __name__ == "__main__":
    main()
