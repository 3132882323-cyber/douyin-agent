"""Local-first operator memory for the Dian Agent.

The memory store is deliberately small, scoped, and auditable.  It keeps
reusable operating facts without storing tokens, cookies, or raw page dumps.
Store and Qianchuan account keys are part of the scope so memories can never
silently leak between shops or advertisers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_ENTRIES = 2000
MAX_SCOPE_ENTRIES = 500
MAX_TITLE = 120
MAX_VALUE = 1200
MAX_EVIDENCE = 800
SAFE_KEY = re.compile(r"^[a-z0-9_-]{1,48}$")
MEMORY_TYPES = {"fact", "strategy", "preference", "outcome"}
CONFIDENCES = {"low", "medium", "high"}
SOURCES = {"user", "system", "outcome"}
_LOCK = threading.RLock()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _safe_key(value: Any, *, required: bool = False) -> str:
    key = str(value or "").strip().lower()
    if not key:
        if required:
            raise ValueError("memory scope requires a store_key")
        return ""
    if not SAFE_KEY.fullmatch(key):
        raise ValueError("invalid memory scope key")
    return key


def _scope(store_key: Any, account_key: Any = "") -> dict[str, str]:
    return {"store_key": _safe_key(store_key, required=True), "account_key": _safe_key(account_key)}


def _path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "memory" / "operator_memory.json"


def _empty() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "updated_at": None, "entries": []}


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(value, dict):
        return _empty()
    entries = value.get("entries")
    if not isinstance(entries, list):
        entries = []
    return {
        "schema_version": int(value.get("schema_version") or SCHEMA_VERSION),
        "updated_at": value.get("updated_at"),
        "entries": [item for item in entries if isinstance(item, dict)],
    }


def _write(path: Path, payload: dict[str, Any]) -> None:
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


def _matches(entry: dict[str, Any], scope: dict[str, str]) -> bool:
    item_scope = entry.get("scope") if isinstance(entry.get("scope"), dict) else {}
    return (
        str(item_scope.get("store_key") or "") == scope["store_key"]
        and str(item_scope.get("account_key") or "") == scope["account_key"]
    )


def _clean_entry(entry: dict[str, Any]) -> dict[str, Any]:
    scope = entry.get("scope") if isinstance(entry.get("scope"), dict) else {}
    return {
        "id": str(entry.get("id") or "")[:96],
        "scope": {
            "store_key": str(scope.get("store_key") or ""),
            "account_key": str(scope.get("account_key") or ""),
        },
        "type": str(entry.get("type") or "fact"),
        "title": str(entry.get("title") or "")[:MAX_TITLE],
        "value": str(entry.get("value") or "")[:MAX_VALUE],
        "source": str(entry.get("source") or "system"),
        "confidence": str(entry.get("confidence") or "medium"),
        "status": str(entry.get("status") or "active"),
        "evidence": str(entry.get("evidence") or "")[:MAX_EVIDENCE],
        "created_at": str(entry.get("created_at") or ""),
        "updated_at": str(entry.get("updated_at") or ""),
    }


def list_operator_memory(data_dir: str | Path, store_key: Any, account_key: Any = "") -> dict[str, Any]:
    """Return active memory for one store/account scope."""
    scope = _scope(store_key, account_key)
    with _LOCK:
        payload = _load(_path(data_dir))
        entries = [
            _clean_entry(item)
            for item in payload["entries"]
            if _matches(item, scope) and str(item.get("status") or "active") == "active"
        ]
    entries.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    counts = {memory_type: sum(item["type"] == memory_type for item in entries) for memory_type in sorted(MEMORY_TYPES)}
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": scope,
        "entries": entries,
        "count": len(entries),
        "counts": counts,
        "updated_at": payload.get("updated_at"),
        "storage": "local",
        "privacy": "不保存 Token、Cookie 或原始页面内容",
        "note": "记忆只对当前店铺和千川账号生效；确认绑定后才会参与建议。",
    }


def upsert_operator_memory(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("memory payload must be an object")
    scope = _scope(payload.get("store_key"), payload.get("account_key"))
    memory_type = str(payload.get("type") or "fact").strip().lower()
    if memory_type not in MEMORY_TYPES:
        raise ValueError("memory type must be fact, strategy, preference or outcome")
    title = str(payload.get("title") or "").strip()
    value = str(payload.get("value") or "").strip()
    if not title or not value:
        raise ValueError("memory title and value are required")
    if len(title) > MAX_TITLE or len(value) > MAX_VALUE:
        raise ValueError("memory title or value is too long")
    source = str(payload.get("source") or "user").strip().lower()
    confidence = str(payload.get("confidence") or "medium").strip().lower()
    if source not in SOURCES:
        raise ValueError("invalid memory source")
    if confidence not in CONFIDENCES:
        raise ValueError("invalid memory confidence")
    memory_id = str(payload.get("id") or "").strip()
    if not memory_id:
        fingerprint = "|".join((scope["store_key"], scope["account_key"], memory_type, title.lower()))
        memory_id = "mem_" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:20]
    if not re.fullmatch(r"mem_[a-f0-9]{20}", memory_id):
        raise ValueError("invalid memory id")
    now = _now()
    path = _path(data_dir)
    with _LOCK:
        document = _load(path)
        entries = document["entries"]
        existing = next(
            (item for item in entries if str(item.get("id") or "") == memory_id and _matches(item, scope)),
            None,
        )
        if existing is None:
            existing = {
                "id": memory_id,
                "scope": scope,
                "created_at": now,
            }
            entries.append(existing)
        existing.update(
            {
                "scope": scope,
                "type": memory_type,
                "title": title,
                "value": value,
                "source": source,
                "confidence": confidence,
                "status": "active",
                "evidence": str(payload.get("evidence") or "").strip()[:MAX_EVIDENCE],
                "updated_at": now,
            }
        )
        # Keep the newest memories first and cap both global and per-scope growth.
        entries[:] = [_clean_entry(item) for item in entries if isinstance(item, dict)]
        entries.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        scoped_ids = [item["id"] for item in entries if _matches(item, scope)]
        if len(scoped_ids) > MAX_SCOPE_ENTRIES:
            keep = set(scoped_ids[:MAX_SCOPE_ENTRIES])
            entries[:] = [item for item in entries if not _matches(item, scope) or item["id"] in keep]
        entries[:] = entries[:MAX_ENTRIES]
        document["schema_version"] = SCHEMA_VERSION
        document["updated_at"] = now
        _write(path, document)
        saved = next(item for item in entries if item["id"] == memory_id and _matches(item, scope))
    return {"ok": True, "entry": saved}


def archive_operator_memory(data_dir: str | Path, memory_id: Any, store_key: Any, account_key: Any = "") -> dict[str, Any]:
    scope = _scope(store_key, account_key)
    memory_id = str(memory_id or "").strip()
    if not re.fullmatch(r"mem_[a-f0-9]{20}", memory_id):
        raise ValueError("invalid memory id")
    path = _path(data_dir)
    with _LOCK:
        document = _load(path)
        target = next(
            (item for item in document["entries"] if str(item.get("id") or "") == memory_id and _matches(item, scope)),
            None,
        )
        if target is None:
            raise ValueError("memory not found in current scope")
        target["status"] = "archived"
        target["updated_at"] = _now()
        document["updated_at"] = target["updated_at"]
        _write(path, document)
        return {"ok": True, "entry": _clean_entry(target)}
