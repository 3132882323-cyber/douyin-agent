"""Snapshot storage and Qianchuan account catalog I/O."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import state

logger = logging.getLogger("dian-agent-http")
SAFE_KEY = re.compile(r"^[a-z0-9_-]{1,48}$")


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


def _snapshot_path(source: str, page_type: str) -> Path:
    return state.DATA_DIR / source / f"{page_type}.json"


def _account_snapshot_path(account_key: str, page_type: str) -> Path:
    return state.DATA_DIR / "qianchuan_accounts" / account_key / f"{page_type}.json"


def _account_catalog_path() -> Path:
    return state.DATA_DIR / "qianchuan_accounts.json"


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
        for account in accounts:
            if not isinstance(account, dict):
                continue
            key = str(account.get("key") or "").lower()
            label = _normalized_account_label(account.get("label"))
            if not SAFE_KEY.fullmatch(key) or not _is_valid_qianchuan_account_label(label):
                continue
            if key in seen_keys:
                continue
            aliases = [
                str(alias).lower()
                for alias in account.get("aliases", [])
                if SAFE_KEY.fullmatch(str(alias).lower()) and str(alias).lower() != key
            ][:20]
            cleaned.append({**account, "key": key, "label": label, "aliases": list(dict.fromkeys(aliases))})
            seen_keys.add(key)
        return cleaned
    except (OSError, json.JSONDecodeError):
        return []


def build_store_catalog() -> dict[str, Any]:
    """Build a multi-store view without mixing one store's metrics into another."""
    import http_receiver as facade

    selected_key = str(facade.load_agent_settings().get("qianchuan_account_key") or "")
    sync_status = facade.load_sync_status(state.DATA_DIR)
    official_by_key = {
        str(account.get("account_key") or ""): account
        for account in sync_status.get("accounts", [])
        if isinstance(account, dict)
    }
    stores: list[dict[str, Any]] = []
    for account in facade.list_qianchuan_accounts():
        key = str(account.get("key") or "")
        account_dir = state.DATA_DIR / "qianchuan_accounts" / key
        snapshot_paths = list(account_dir.glob("*.json")) if account_dir.exists() else []
        newest_timestamp = max(
            (path.stat().st_mtime for path in snapshot_paths),
            default=0.0,
        )
        official = official_by_key.get(key)
        channel = "official_api" if official else "browser"
        advertiser_count = (
            int(official.get("advertiser_count") or 0) if official else None
        )
        if official and advertiser_count == 0:
            state_name = "not_linked"
            state_label = "未关联广告账户"
        elif official:
            state_name = "ready"
            state_label = "官方 API 可用"
        elif snapshot_paths:
            state_name = "browser_only"
            state_label = "网页数据"
        else:
            state_name = "empty"
            state_label = "暂无数据"
        stores.append(
            {
                **account,
                "channel": channel,
                "advertiser_count": advertiser_count,
                "page_count": len(snapshot_paths),
                "updated_at": int(newest_timestamp) if newest_timestamp else None,
                "state": state_name,
                "state_label": state_label,
                "selected": key == selected_key,
            }
        )
    stores.sort(
        key=lambda item: (
            0 if item.get("selected") else 1,
            0 if item.get("state") == "ready" else 1,
            -(int(item.get("updated_at") or 0)),
        )
    )
    return {
        "mode": "multi_store",
        "stores": stores,
        "accounts": stores,
        "store_count": len(stores),
        "official_store_count": sum(
            item.get("channel") == "official_api" for item in stores
        ),
        "selected_store_key": selected_key,
        "selected_account_key": selected_key,
        "data_isolation": "per_store",
    }


def _remember_qianchuan_account(account: dict[str, Any]) -> None:
    key = str(account.get("key") or "").lower()
    if not SAFE_KEY.fullmatch(key):
        return
    label = _normalized_account_label(account.get("label") or "千川账号")
    if not _is_valid_qianchuan_account_label(label):
        return
    with state._state_lock:
        accounts = {str(item.get("key")): item for item in list_qianchuan_accounts() if isinstance(item, dict)}
        previous = accounts.get(key, {})
        aliases = [
            str(alias).lower()
            for alias in [*(previous.get("aliases") or []), *(account.get("aliases") or [])]
            if SAFE_KEY.fullmatch(str(alias).lower()) and str(alias).lower() != key
        ][:20]
        accounts[key] = {
            "key": key,
            "label": label,
            "confidence": str(account.get("confidence") or "medium")[:16],
            "identity_source": str(account.get("identity_source") or "legacy")[:32],
            "aliases": list(dict.fromkeys(aliases)),
            "last_seen": _now_label(),
        }
        _atomic_json_write(
            _account_catalog_path(),
            {"accounts": sorted(accounts.values(), key=lambda item: item.get("last_seen", ""), reverse=True)},
        )


def _canonical_qianchuan_account_key(account: dict[str, Any]) -> str:
    key = str(account.get("key") or "").lower()
    if not SAFE_KEY.fullmatch(key):
        return ""
    label = _normalized_account_label(account.get("label"))
    if not _is_valid_qianchuan_account_label(label):
        return key
    known_accounts = list_qianchuan_accounts()
    for known in known_accounts:
        aliases = {str(alias).lower() for alias in known.get("aliases", [])}
        if key in aliases and SAFE_KEY.fullmatch(str(known.get("key") or "")):
            return str(known["key"]).lower()
    label_key = re.sub(r"\s+", "", label).casefold()
    matches: list[dict[str, Any]] = []
    for known in known_accounts:
        known_label_key = re.sub(r"\s+", "", _normalized_account_label(known.get("label"))).casefold()
        if known_label_key == label_key and SAFE_KEY.fullmatch(str(known.get("key") or "")):
            matches.append(known)
    identity_source = str(account.get("identity_source") or "legacy")
    if identity_source == "platform_id":
        label_only_matches = [
            known for known in matches
            if str(known.get("identity_source") or "legacy") in {"account_label", "legacy"}
        ]
        if len(label_only_matches) == 1:
            return str(label_only_matches[0]["key"]).lower()
        return key
    platform_matches = [known for known in matches if known.get("identity_source") == "platform_id"]
    if len(platform_matches) == 1:
        return str(platform_matches[0]["key"]).lower()
    if not platform_matches and len(matches) == 1:
        return str(matches[0]["key"]).lower()
    return key


def _history_dir(source: str, page_type: str) -> Path:
    return state.DATA_DIR / "history" / source / page_type


def _save_history_point(snapshot: dict[str, Any]) -> None:
    from http_receiver import load_agent_settings  # late import to avoid cycle

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
    }
    directory = _history_dir(source, page_type)
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


def save_data(source: str, data: dict[str, Any]) -> dict[str, Any]:
    if source not in state.ALLOWED_SOURCES:
        raise ValueError(f"unknown source: {source}")
    if not isinstance(data, dict):
        raise ValueError("data must be an object")

    page_type = _safe_page_type(data.get("page_type"))
    captured_at_ms = int(data.get("captured_at") or data.get("timestamp") or int(time.time() * 1000))
    normalized = {
        **data,
        "schema_version": int(data.get("schema_version") or 1),
        "source": source,
        "page_type": page_type,
        "captured_at": captured_at_ms,
    }
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
    _atomic_json_write(_snapshot_path(source, page_type), payload)
    if source == "qianchuan" and isinstance(normalized.get("account"), dict):
        account = normalized["account"]
        account_key = str(account.get("key") or "").lower()
        if SAFE_KEY.fullmatch(account_key):
            _atomic_json_write(_account_snapshot_path(account_key, page_type), payload)
            _remember_qianchuan_account(account)
    _atomic_json_write(state.DATA_DIR / f"{source}.json", payload)
    _save_history_point(payload)
    logger.info("已保存 %s/%s 快照（质量 %s）", source, page_type, normalized.get("quality", {}).get("score", "-"))
    quality = normalized.get("quality") or {}
    if quality:
        try:
            from http_receiver import update_health_baseline  # late import to avoid cycle

            update_health_baseline(source, page_type, quality)
        except Exception:
            logger.exception("更新健康基线失败: %s/%s", source, page_type)
    return payload


def load_data(source: str, page_type: str | None = None, account_key: str | None = None) -> dict[str, Any] | None:
    if source not in state.ALLOWED_SOURCES:
        return None
    selected_account = account_key
    if source == "qianchuan" and selected_account is None:
        from http_receiver import load_agent_settings  # late import to avoid cycle

        selected_account = str(load_agent_settings().get("qianchuan_account_key") or "")
    if source == "qianchuan" and selected_account:
        safe_account = str(selected_account).lower()
        if not SAFE_KEY.fullmatch(safe_account):
            return None
        if page_type:
            path = _account_snapshot_path(safe_account, _safe_page_type(page_type))
        else:
            account_dir = state.DATA_DIR / "qianchuan_accounts" / safe_account
            candidates = (
                sorted(account_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
                if account_dir.exists()
                else []
            )
            path = candidates[0] if candidates else account_dir / "missing.json"
    else:
        path = _snapshot_path(source, _safe_page_type(page_type)) if page_type else state.DATA_DIR / f"{source}.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        logger.exception("读取快照失败: %s", path)
        return None


def list_snapshots() -> list[dict[str, Any]]:
    from http_receiver import load_agent_settings  # late import to avoid cycle

    items: list[dict[str, Any]] = []
    selected_account = str(load_agent_settings().get("qianchuan_account_key") or "").lower()
    for source in sorted(state.ALLOWED_SOURCES):
        paths: list[tuple[Path, str | None]] = []
        seen_pages: set[str] = set()
        if source == "qianchuan" and selected_account and SAFE_KEY.fullmatch(selected_account):
            account_dir = state.DATA_DIR / "qianchuan_accounts" / selected_account
            if account_dir.exists():
                for path in sorted(account_dir.glob("*.json")):
                    paths.append((path, selected_account))
                    seen_pages.add(path.stem)
        source_dir = state.DATA_DIR / source
        if source_dir.exists():
            for path in sorted(source_dir.glob("*.json")):
                # Prefer account-partitioned pages; still surface root-only pages
                # (e.g. official API plans) so reconcile does not go blind.
                if path.stem in seen_pages:
                    continue
                paths.append((path, "" if source == "qianchuan" else None))
        for path, account_key in paths:
            snapshot = load_data(source, path.stem, account_key=account_key)
            if not snapshot:
                continue
            data = snapshot.get("data", {})
            quality = data.get("quality", {}) if isinstance(data, dict) else {}
            age = max(0, int(time.time() - float(snapshot.get("timestamp", 0))))
            items.append(
                {
                    "source": source,
                    "page_type": snapshot.get("page_type", path.stem),
                    "account_key": str(((data.get("account") or {}) if isinstance(data, dict) else {}).get("key") or account_key or ""),
                    "saved_at": snapshot.get("saved_at"),
                    "age_seconds": age,
                    "fresh": age < state.STALE_SECONDS,
                    "title": data.get("title", "") if isinstance(data, dict) else "",
                    "url": data.get("url", "") if isinstance(data, dict) else "",
                    "quality_score": int(quality.get("score", 0) or 0),
                    "metric_count": int(quality.get("metric_count", 0) or 0),
                    "row_count": int(quality.get("row_count", 0) or 0),
                    "pagination_truncated": bool(quality.get("pagination_truncated")),
                    "warnings": quality.get("warnings", []),
                }
            )
    return sorted(items, key=lambda item: item.get("age_seconds", 10**9))


def load_history(source: str | None = None, page_type: str | None = None, days: int = 7) -> list[dict[str, Any]]:
    from http_receiver import load_agent_settings  # late import to avoid cycle

    days = min(90, max(1, int(days)))
    cutoff_ms = int((time.time() - days * 86400) * 1000)
    root = state.DATA_DIR / "history"
    if not root.exists():
        return []
    patterns = [root / source / page_type] if source and page_type else [root / source] if source else [root]
    points: list[dict[str, Any]] = []
    selected_account = str(load_agent_settings().get("qianchuan_account_key") or "") if source == "qianchuan" else ""
    for base in patterns:
        if not base.exists():
            continue
        for path in base.rglob("*.json"):
            try:
                if int(path.stem) < cutoff_ms:
                    continue
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict) and (not selected_account or value.get("account_key") == selected_account):
                    points.append(value)
            except (ValueError, OSError, json.JSONDecodeError):
                continue
    return sorted(points, key=lambda item: int(item.get("captured_at", 0)))
