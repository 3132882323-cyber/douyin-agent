"""Local-first Ocean Engine account governance and safe batch previews.

This module intentionally never stores platform identifiers or credentials.  The
HTTP layer supplies HMAC-derived account keys and the OAuth client keeps tokens
inside the operating-system credential boundary.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
ACCOUNT_KEY = re.compile(r"^adacct_v1_[a-f0-9]{26}$")
ALLOWED_BATCH_ACTIONS = {"sync", "schedule", "pause", "budget_decrease"}
MAX_BATCH_ACCOUNTS = 50
FRESH_SECONDS = 2 * 60 * 60
STALE_SECONDS = 24 * 60 * 60

_state_lock = threading.Lock()


class OceanEngineAccountCenterError(ValueError):
    """A safe local account-preference persistence failure."""


@contextmanager
def _interprocess_file_lock(path: Path) -> Iterator[None]:
    """Hold one advisory byte lock across Windows and POSIX processes."""

    if path.parent.is_symlink() or (
        path.parent.exists() and not path.parent.is_dir()
    ):
        raise OceanEngineAccountCenterError(
            "The account preference lock directory is not safe."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OceanEngineAccountCenterError(
            "The account preference lock cannot be a symbolic link."
        )
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OceanEngineAccountCenterError(
                "The account preference lock must be a regular file."
            )
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
        locked = True
        yield
    finally:
        if locked:
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
        else:
            os.close(descriptor)


def _now() -> int:
    return int(time.time())


def _clean_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _strict_boolean(payload: dict[str, Any], field_name: str, default: bool) -> bool:
    if field_name not in payload:
        return default
    value = payload[field_name]
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be true or false.")
    return value


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            Path(temporary_name).unlink(missing_ok=True)
        except OSError:
            pass


def _endpoint_capability(
    endpoints: list[dict[str, Any]],
    matcher: Any,
    *,
    blocked_without_mapping: bool = True,
    mapping_ready: bool = False,
) -> dict[str, Any]:
    matched = [item for item in endpoints if matcher(str(item.get("name") or ""))]
    if not matched:
        if blocked_without_mapping and not mapping_ready:
            return {"state": "blocked", "label": "等待广告账户映射", "verified": False}
        return {"state": "untested", "label": "尚未验证", "verified": False}
    successes = sum(bool(item.get("ok")) for item in matched)
    if successes == len(matched):
        return {"state": "verified", "label": "读取已验证", "verified": True}
    if successes:
        return {"state": "partial", "label": "部分接口可用", "verified": False}
    return {"state": "blocked", "label": "暂无读取权限", "verified": False}


class OceanEngineAccountCenter:
    """Build the safe multi-account view used by the local extension."""

    def __init__(self, data_dir: Path, now: int | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "oceanengine_account_center.json"
        self.lock_path = self.data_dir / ".oceanengine-account-center.lock"
        self.now = int(now if now is not None else _now())

    def _load_preferences(self) -> dict[str, dict[str, Any]]:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return {}
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OceanEngineAccountCenterError(
                "The account preference registry must be a regular file."
            )
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OceanEngineAccountCenterError(
                "The account preference registry is unreadable; no changes were made."
            ) from error
        accounts = payload.get("accounts") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != SCHEMA_VERSION
            or not isinstance(accounts, dict)
        ):
            raise OceanEngineAccountCenterError(
                "The account preference registry is invalid; no changes were made."
            )
        result: dict[str, dict[str, Any]] = {}
        for key, value in accounts.items():
            if not ACCOUNT_KEY.fullmatch(str(key)) or not isinstance(value, dict):
                raise OceanEngineAccountCenterError(
                    "The account preference registry contains an invalid account entry."
                )
            alias = value.get("alias", "")
            group_name = value.get("group_name", "")
            sync_enabled = value.get("sync_enabled", True)
            managed = value.get("managed", False)
            revision = value.get("revision", 0)
            updated_at = value.get("updated_at")
            if (
                not isinstance(alias, str)
                or not isinstance(group_name, str)
                or not isinstance(sync_enabled, bool)
                or not isinstance(managed, bool)
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 0
                or (
                    updated_at is not None
                    and (
                        isinstance(updated_at, bool)
                        or not isinstance(updated_at, int)
                        or updated_at < 0
                    )
                )
            ):
                raise OceanEngineAccountCenterError(
                    "The account preference registry contains invalid preference data."
                )
            result[str(key)] = value
        return result

    @staticmethod
    def _defaults() -> dict[str, Any]:
        return {
            "alias": "",
            "group_name": "未分组",
            "sync_enabled": True,
            "managed": False,
            "revision": 0,
            "updated_at": None,
        }

    @staticmethod
    def _masked_identifier(raw_identifier: Any, account_key: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9]", "", str(raw_identifier or ""))
        suffix = normalized[-4:] if normalized else account_key[-4:].upper()
        return f"•••• {suffix}"

    def _token_state(self, oauth: dict[str, Any], valid: bool) -> dict[str, Any]:
        refresh_expires_at = int(oauth.get("refresh_token_expires_at") or 0)
        if not oauth.get("connected") or not valid:
            return {"state": "expired", "label": "需要重新授权", "expires_at": refresh_expires_at or None}
        if refresh_expires_at and refresh_expires_at <= self.now:
            return {"state": "expired", "label": "授权已过期", "expires_at": refresh_expires_at}
        if refresh_expires_at and refresh_expires_at <= self.now + 7 * 24 * 60 * 60:
            return {"state": "expiring", "label": "7 天内到期", "expires_at": refresh_expires_at}
        return {"state": "active", "label": "授权有效", "expires_at": refresh_expires_at or None}

    def _freshness(self, synced_at: int) -> dict[str, Any]:
        if not synced_at:
            return {"state": "never", "label": "尚未同步", "age_seconds": None}
        age = max(0, self.now - synced_at)
        if age <= FRESH_SECONDS:
            return {"state": "fresh", "label": "数据新鲜", "age_seconds": age}
        if age <= STALE_SECONDS:
            return {"state": "aging", "label": "建议重新同步", "age_seconds": age}
        return {"state": "stale", "label": "数据已过期", "age_seconds": age}

    def build(
        self,
        oauth: dict[str, Any],
        sync_status: dict[str, Any],
        store_catalog: dict[str, Any],
    ) -> dict[str, Any]:
        preferences = self._load_preferences()
        sync_accounts = {
            str(item.get("account_key") or ""): item
            for item in sync_status.get("accounts", [])
            if isinstance(item, dict)
        }
        stores_by_account: dict[str, list[dict[str, str]]] = {}
        for store in store_catalog.get("stores", []):
            if not isinstance(store, dict):
                continue
            safe_store = {
                "key": str(store.get("key") or ""),
                "label": _clean_text(store.get("label") or "匿名店铺", 48),
            }
            for key in store.get("account_keys", []):
                stores_by_account.setdefault(str(key), []).append(safe_store)

        accounts: list[dict[str, Any]] = []
        for source in oauth.get("accounts", []):
            if not isinstance(source, dict):
                continue
            account_key = str(source.get("account_key") or "")
            if not ACCOUNT_KEY.fullmatch(account_key):
                continue
            pref = {**self._defaults(), **preferences.get(account_key, {})}
            valid = bool(source.get("valid", True))
            token = self._token_state(oauth, valid)
            synced = sync_accounts.get(account_key, {})
            endpoints = [
                item for item in synced.get("endpoints", []) if isinstance(item, dict)
            ]
            mapping_rows = [
                item for item in endpoints if str(item.get("name") or "") == "关联广告账户"
            ]
            advertiser_count = int(
                synced.get("advertiser_count")
                or source.get("advertiser_count")
                or 0
            )
            mapping_ok = bool(mapping_rows) and all(item.get("ok") for item in mapping_rows) and advertiser_count > 0
            authorization_ok = token["state"] in {"active", "expiring"}
            capabilities = [
                {
                    "id": "authorization",
                    "label": "官方授权",
                    "state": "verified" if authorization_ok else "blocked",
                    "status_label": "授权有效" if authorization_ok else "需要重新授权",
                    "verified": authorization_ok,
                },
                {
                    "id": "advertiser_mapping",
                    "label": "广告账户映射",
                    "state": "verified" if mapping_ok else "blocked" if mapping_rows else "untested",
                    "status_label": "映射已验证" if mapping_ok else "未找到广告账户" if mapping_rows else "尚未验证",
                    "verified": mapping_ok,
                },
            ]
            capability_specs = (
                ("plan_read", "计划读取", lambda name: "计划" in name or "全域推广" in name or "调控任务" in name),
                ("report_read", "经营报表", lambda name: "报表" in name),
                ("material_read", "素材读取", lambda name: "素材" in name or "视频素材库" in name),
            )
            for capability_id, label, matcher in capability_specs:
                derived = _endpoint_capability(
                    endpoints,
                    matcher,
                    mapping_ready=mapping_ok,
                )
                capabilities.append(
                    {
                        "id": capability_id,
                        "label": label,
                        "state": derived["state"],
                        "status_label": derived["label"],
                        "verified": derived["verified"],
                    }
                )
            capabilities.append(
                {
                    "id": "production_write",
                    "label": "真实投放写入",
                    "state": "blocked",
                    "status_label": "生产写入未开放",
                    "verified": False,
                }
            )
            synced_at = int(sync_status.get("synced_at") or 0) if synced else 0
            freshness = self._freshness(synced_at)
            failed_endpoints = sum(not bool(item.get("ok")) for item in endpoints)
            if not authorization_ok:
                state, state_label, next_action = "needs_auth", "需要授权", "重新授权千川账号"
            elif not bool(pref.get("sync_enabled", True)):
                state, state_label, next_action = "paused", "已暂停同步", "开启账户同步"
            elif not synced_at:
                state, state_label, next_action = "needs_sync", "等待首次同步", "同步该账户的官方数据"
            elif not mapping_ok or failed_endpoints:
                state, state_label, next_action = "degraded", "需要处理", "查看失败接口并重新同步"
            elif freshness["state"] == "stale":
                state, state_label, next_action = "needs_sync", "数据已过期", "重新同步官方数据"
            else:
                state, state_label, next_action = "ready", "经营就绪", "进入策略预演"

            default_name = _clean_text(source.get("account_name"), 48)
            if not default_name:
                default_name = f"千川账户 {account_key[-6:].upper()}"
            alias = _clean_text(pref.get("alias"), 40)
            accounts.append(
                {
                    "account_key": account_key,
                    "display_name": alias or default_name,
                    "platform_name": default_name,
                    "masked_id": self._masked_identifier(source.get("account_id"), account_key),
                    "role": _clean_text(source.get("account_role"), 32),
                    "valid": valid,
                    "selected": account_key == str(store_catalog.get("selected_account_key") or ""),
                    "advertiser_count": advertiser_count,
                    "linked_stores": stores_by_account.get(account_key, []),
                    "token": token,
                    "sync": {
                        "synced_at": synced_at or None,
                        "freshness": freshness,
                        "endpoint_count": len(endpoints),
                        "success_count": len(endpoints) - failed_endpoints,
                        "failure_count": failed_endpoints,
                    },
                    "capabilities": capabilities,
                    "preferences": {
                        "alias": alias,
                        "group_name": _clean_text(pref.get("group_name") or "未分组", 24) or "未分组",
                        "sync_enabled": bool(pref.get("sync_enabled", True)),
                        "managed": bool(pref.get("managed", False)),
                        "revision": int(pref.get("revision") or 0),
                        "updated_at": pref.get("updated_at"),
                    },
                    "state": state,
                    "state_label": state_label,
                    "next_action": next_action,
                }
            )

        state_order = {"needs_auth": 0, "degraded": 1, "needs_sync": 2, "paused": 3, "ready": 4}
        accounts.sort(
            key=lambda item: (
                0 if item["selected"] else 1,
                state_order.get(str(item["state"]), 9),
                str(item["display_name"]).lower(),
            )
        )
        groups = sorted({str(item["preferences"]["group_name"]) for item in accounts})
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": self.now,
            "summary": {
                "total": len(accounts),
                "connected": sum(item["token"]["state"] in {"active", "expiring"} for item in accounts),
                "managed": sum(bool(item["preferences"]["managed"]) for item in accounts),
                "attention": sum(item["state"] not in {"ready", "paused"} for item in accounts),
                "advertisers": sum(int(item["advertiser_count"]) for item in accounts),
                "groups": len(groups),
            },
            "groups": groups,
            "accounts": accounts,
            "platform_write_enabled": False,
            "automatic_batch_submit": False,
            "secrets_exposed": False,
            "notice": "账户密钥与 Token 不进入扩展；批量投放操作只生成影响预演，需逐批人工确认。",
        }

    def update_preference(
        self,
        account_key: str,
        payload: dict[str, Any],
        known_account_keys: set[str],
    ) -> dict[str, Any]:
        account_key = str(account_key or "")
        if not ACCOUNT_KEY.fullmatch(account_key) or account_key not in known_account_keys:
            raise ValueError("账户不存在或本机身份已变化，请刷新账户中心。")
        alias = _clean_text(payload.get("alias"), 40)
        group_name = _clean_text(payload.get("group_name") or "未分组", 24) or "未分组"
        sync_enabled = _strict_boolean(payload, "sync_enabled", True)
        managed = _strict_boolean(payload, "managed", False) and sync_enabled
        with _state_lock:
            with _interprocess_file_lock(self.lock_path):
                # Re-read only after acquiring the process-wide transaction
                # lock.  Otherwise two Agent instances can each overwrite the
                # other instance's newly-added account preference.
                accounts = self._load_preferences()
                previous = {**self._defaults(), **accounts.get(account_key, {})}
                preference = {
                    "alias": alias,
                    "group_name": group_name,
                    "sync_enabled": sync_enabled,
                    "managed": managed,
                    "revision": int(previous.get("revision") or 0) + 1,
                    "updated_at": self.now,
                }
                accounts[account_key] = preference
                _atomic_write(
                    self.path,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "updated_at": self.now,
                        "accounts": accounts,
                    },
                )
        return preference

    def preview(
        self,
        account_keys: list[str],
        action: str,
        center: dict[str, Any],
    ) -> dict[str, Any]:
        action = str(action or "")
        if action not in ALLOWED_BATCH_ACTIONS:
            raise ValueError("批量动作不受支持。")
        selected = list(dict.fromkeys(str(value or "") for value in account_keys))
        if not selected:
            raise ValueError("请至少选择一个账户。")
        if len(selected) > MAX_BATCH_ACCOUNTS:
            raise ValueError(f"单批最多选择 {MAX_BATCH_ACCOUNTS} 个账户。")
        account_map = {str(item.get("account_key") or ""): item for item in center.get("accounts", [])}
        if any(not ACCOUNT_KEY.fullmatch(key) or key not in account_map for key in selected):
            raise ValueError("所选账户已变化，请刷新账户中心后重试。")

        eligible: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for key in selected:
            account = account_map[key]
            capabilities = {str(item.get("id") or ""): item for item in account.get("capabilities", [])}
            reasons: list[str] = []
            if not capabilities.get("authorization", {}).get("verified"):
                reasons.append("官方授权不可用")
            if not account.get("preferences", {}).get("sync_enabled", True):
                reasons.append("账户同步已暂停")
            if action != "sync":
                if not account.get("preferences", {}).get("managed"):
                    reasons.append("尚未开启本地托管")
                if not capabilities.get("advertiser_mapping", {}).get("verified"):
                    reasons.append("广告账户映射未验证")
                if not capabilities.get("plan_read", {}).get("verified"):
                    reasons.append("计划读取未完整验证")
                if action == "budget_decrease" and not capabilities.get("report_read", {}).get("verified"):
                    reasons.append("经营报表未完整验证")
            row = {
                "account_key": key,
                "display_name": account.get("display_name"),
                "advertiser_count": int(account.get("advertiser_count") or 0),
            }
            if reasons:
                blocked.append({**row, "reasons": reasons})
            else:
                eligible.append(row)

        action_labels = {
            "sync": "同步官方数据",
            "schedule": "定时启停预演",
            "pause": "暂停计划预演",
            "budget_decrease": "降低预算预演",
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "previewed_at": self.now,
            "action": action,
            "action_label": action_labels[action],
            "selected_count": len(selected),
            "eligible_count": len(eligible),
            "blocked_count": len(blocked),
            "advertiser_count": sum(int(item["advertiser_count"]) for item in eligible),
            "eligible": eligible,
            "blocked": blocked,
            "execution_kind": "read_only_sync" if action == "sync" else "preview_only",
            "platform_write_enabled": False,
            "automatic_submit": False,
            "requires_human_review": action != "sync",
            "notice": "只读同步可在确认后执行；投放动作不会直接提交到平台。",
        }
