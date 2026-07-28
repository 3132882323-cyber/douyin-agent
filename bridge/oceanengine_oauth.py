"""巨量千川 OAuth：本机凭证保存、授权链接、回调和 Token 交换。

App Secret 与 Token 仅写入本机数据目录。Windows 使用当前用户 DPAPI 加密；
其他系统可通过环境变量临时提供密钥，模块不会把明文密钥写入磁盘。
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import html
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_APP_ID = "1871942906223351"
PUBLIC_CALLBACK_URL = (
    "https://dian-agent-oauth-3132882323.abuzz-cod-4955.chatgpt.site"
    "/api/oauth/oceanengine/callback"
)
QIANCHUAN_AUTHORIZE_URL = (
    "https://qianchuan.jinritemai.com/openapi/qc/audit/oauth.html"
)
TOKEN_URL = "https://api.oceanengine.com/open_api/oauth2/access_token/"
REFRESH_TOKEN_URL = "https://api.oceanengine.com/open_api/oauth2/refresh_token/"
AUTHORIZED_ACCOUNTS_URL = (
    "https://api.oceanengine.com/open_api/oauth2/advertiser/get/"
)
AUTH_SESSION_SECONDS = 15 * 60
HTTP_TIMEOUT_SECONDS = 15

_oauth_lock = threading.Lock()


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob_from_bytes(value: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(value)
    blob = _DataBlob(
        len(value),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    return blob, buffer


def _windows_protect(value: bytes, description: str) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("当前系统不支持 Windows DPAPI")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    source, source_buffer = _blob_from_bytes(value)
    target = _DataBlob()
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    ok = crypt32.CryptProtectData(
        ctypes.byref(source),
        description,
        None,
        None,
        None,
        0x01,
        ctypes.byref(target),
    )
    del source_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def _windows_unprotect(value: bytes) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("当前系统不支持 Windows DPAPI")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    source, source_buffer = _blob_from_bytes(value)
    target = _DataBlob()
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0x01,
        ctypes.byref(target),
    )
    del source_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_write_bytes(
        path,
        json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def _store_encrypted(path: Path, value: dict[str, Any], description: str) -> None:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    protected = _windows_protect(raw, description)
    _atomic_write_bytes(path, base64.b64encode(protected))


def _load_encrypted(path: Path) -> dict[str, Any]:
    protected = base64.b64decode(path.read_bytes(), validate=True)
    value = json.loads(_windows_unprotect(protected).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("本机授权文件格式错误")
    return value


def _request_json(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "DianAgent/2.25",
        **(headers or {}),
    }
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json; charset=utf-8"
        method = "POST"
    request = Request(url, data=data, headers=request_headers, method=method)
    with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
        raw = response.read(512 * 1024).decode("utf-8", errors="replace")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("巨量千川接口返回格式异常")
    return value


def _platform_data(response: dict[str, Any], action: str) -> dict[str, Any]:
    code = response.get("code", 0)
    if str(code) not in {"0", "200"}:
        message = str(response.get("message") or response.get("msg") or "未知错误")
        raise ValueError(f"{action}失败：{message}（{code}）")
    data = response.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"{action}失败：平台未返回有效数据")
    return data


def _normalize_accounts(value: Any, fallback_ids: list[str]) -> list[dict[str, Any]]:
    accounts: list[dict[str, Any]] = []
    seen: set[str] = set()
    rows = value if isinstance(value, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        account_id = str(
            row.get("advertiser_id")
            or row.get("account_id")
            or row.get("id")
            or ""
        ).strip()
        if not account_id or account_id in seen:
            continue
        seen.add(account_id)
        accounts.append(
            {
                "account_id": account_id,
                "account_name": str(
                    row.get("advertiser_name")
                    or row.get("account_name")
                    or f"千川账号 {account_id}"
                ),
                "account_role": str(
                    row.get("account_role") or row.get("advertiser_role") or ""
                ),
                "valid": bool(row.get("is_valid", True)),
            }
        )
    for account_id in fallback_ids:
        if account_id and account_id not in seen:
            seen.add(account_id)
            accounts.append(
                {
                    "account_id": account_id,
                    "account_name": f"千川账号 {account_id}",
                    "account_role": "",
                    "valid": True,
                }
            )
    return accounts


class OceanEngineOAuth:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.config_path = self.data_dir / "oceanengine_oauth.json"
        self.secret_path = self.data_dir / "oceanengine_app_secret.dpapi"
        self.token_path = self.data_dir / "oceanengine_tokens.dpapi"
        self.session_path = self.data_dir / "oceanengine_oauth_session.json"

    def _load_config(self) -> dict[str, Any]:
        value: dict[str, Any] = {"app_id": DEFAULT_APP_ID}
        if self.config_path.exists():
            try:
                saved = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    value.update(saved)
            except (OSError, json.JSONDecodeError):
                pass
        app_id = str(value.get("app_id") or DEFAULT_APP_ID).strip()
        return {"app_id": app_id if app_id.isdigit() else DEFAULT_APP_ID}

    def _load_secret(self) -> str:
        environment_secret = os.environ.get("OCEANENGINE_APP_SECRET", "").strip()
        if environment_secret:
            return environment_secret
        if not self.secret_path.exists() or sys.platform != "win32":
            return ""
        value = _load_encrypted(self.secret_path)
        return str(value.get("app_secret") or "")

    def _load_tokens(self) -> dict[str, Any]:
        if not self.token_path.exists() or sys.platform != "win32":
            return {}
        try:
            return _load_encrypted(self.token_path)
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def save_credentials(self, app_id: str, app_secret: str = "") -> None:
        app_id = str(app_id or "").strip()
        app_secret = str(app_secret or "").strip()
        if not app_id.isdigit() or not 10 <= len(app_id) <= 24:
            raise ValueError("App ID 格式不正确，请填写开放平台显示的数字 App ID。")
        if app_secret and not 8 <= len(app_secret) <= 256:
            raise ValueError("App Secret 格式不正确，请重新复制完整密钥。")
        if app_secret and sys.platform != "win32":
            raise ValueError(
                "当前系统不会把 App Secret 写入磁盘，请通过 OCEANENGINE_APP_SECRET 环境变量提供。"
            )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            self.config_path,
            {
                "app_id": app_id,
                "updated_at": int(time.time()),
                "secret_storage": "windows_dpapi" if sys.platform == "win32" else "environment",
            },
        )
        if app_secret:
            _store_encrypted(
                self.secret_path,
                {"app_secret": app_secret},
                "店策 Agent 巨量千川 App Secret",
            )
        if not self._load_secret():
            raise ValueError("请先填写 App Secret；它只会加密保存在这台电脑。")

    def status(self) -> dict[str, Any]:
        config = self._load_config()
        tokens = self._load_tokens()
        session = self._load_session()
        accounts = tokens.get("accounts")
        if not isinstance(accounts, list):
            accounts = []
        public_accounts = [
            {
                "account_id": str(account.get("account_id") or ""),
                "account_name": str(account.get("account_name") or ""),
                "account_role": str(account.get("account_role") or ""),
                "valid": bool(account.get("valid", True)),
                "advertiser_count": len(account.get("advertiser_ids") or []),
            }
            for account in accounts
            if isinstance(account, dict)
        ]
        expires_at = int(tokens.get("expires_at") or 0)
        connected = bool(tokens.get("access_token")) and (
            not expires_at or expires_at > int(time.time())
        )
        return {
            "app_id": config["app_id"],
            "callback_url": PUBLIC_CALLBACK_URL,
            "secret_saved": bool(self._load_secret()),
            "secret_storage": "windows_dpapi" if sys.platform == "win32" else "environment",
            "connected": connected,
            "needs_refresh": bool(tokens.get("access_token")) and not connected,
            "account_count": len(public_accounts),
            "accounts": public_accounts,
            "expires_at": expires_at or None,
            "authorization_in_progress": bool(session) and not connected,
            "authorized_at": tokens.get("authorized_at"),
            "last_error": str(tokens.get("last_error") or ""),
            "secrets_exposed": False,
        }

    def get_valid_access_token(self) -> str:
        """Return an internal access token, refreshing it before expiry.

        Callers must never include the returned value in logs or HTTP responses.
        """
        with _oauth_lock:
            tokens = self._load_tokens()
            access_token = str(tokens.get("access_token") or "")
            expires_at = int(tokens.get("expires_at") or 0)
            if access_token and (not expires_at or expires_at > int(time.time()) + 300):
                return access_token
            refresh_token = str(tokens.get("refresh_token") or "")
            app_secret = self._load_secret()
            if not refresh_token or not app_secret:
                raise ValueError("千川授权已过期，请重新授权账号。")
            config = self._load_config()
            response = _request_json(
                REFRESH_TOKEN_URL,
                payload={
                    "app_id": int(config["app_id"]),
                    "secret": app_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )
            data = _platform_data(response, "刷新 Access Token")
            next_access_token = str(data.get("access_token") or "")
            next_refresh_token = str(data.get("refresh_token") or refresh_token)
            if not next_access_token:
                raise ValueError("平台未返回新的 Access Token，请重新授权账号。")
            now = int(time.time())
            tokens.update(
                {
                    "access_token": next_access_token,
                    "refresh_token": next_refresh_token,
                    "expires_at": now + max(0, int(data.get("expires_in") or 0)),
                    "refresh_token_expires_at": now
                    + max(0, int(data.get("refresh_token_expires_in") or 0)),
                    "last_error": "",
                }
            )
            _store_encrypted(
                self.token_path,
                tokens,
                "店策 Agent 巨量千川 OAuth Token",
            )
            return next_access_token

    def authorized_accounts_private(self) -> list[dict[str, Any]]:
        """Return authorization metadata for the local API client only."""
        accounts = self._load_tokens().get("accounts")
        return accounts if isinstance(accounts, list) else []

    def save_account_advertisers(
        self, advertisers_by_account: dict[str, list[str]]
    ) -> None:
        """Persist resolved advertiser IDs inside the encrypted token record."""
        with _oauth_lock:
            tokens = self._load_tokens()
            accounts = tokens.get("accounts")
            if not isinstance(accounts, list):
                return
            for account in accounts:
                if not isinstance(account, dict):
                    continue
                account_id = str(account.get("account_id") or "")
                account["advertiser_ids"] = list(
                    dict.fromkeys(advertisers_by_account.get(account_id, []))
                )[:100]
            tokens["accounts"] = accounts
            _store_encrypted(
                self.token_path,
                tokens,
                "店策 Agent 巨量千川 OAuth Token",
            )

    def _load_session(self) -> dict[str, Any]:
        if not self.session_path.exists():
            return {}
        try:
            session = json.loads(self.session_path.read_text(encoding="utf-8"))
            if not isinstance(session, dict):
                return {}
            if int(session.get("expires_at") or 0) <= int(time.time()):
                self.session_path.unlink(missing_ok=True)
                return {}
            return session
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def start_authorization(self, app_id: str, app_secret: str = "") -> dict[str, Any]:
        with _oauth_lock:
            self.save_credentials(app_id, app_secret)
            state = secrets.token_urlsafe(32)
            now = int(time.time())
            _atomic_write_json(
                self.session_path,
                {
                    "state_hash": hashlib.sha256(state.encode("utf-8")).hexdigest(),
                    "created_at": now,
                    "expires_at": now + AUTH_SESSION_SECONDS,
                },
            )
            params = urlencode(
                {
                    "app_id": self._load_config()["app_id"],
                    "state": state,
                    "material_auth": "1",
                    "redirect_uri": PUBLIC_CALLBACK_URL,
                }
            )
            return {
                "authorize_url": f"{QIANCHUAN_AUTHORIZE_URL}?{params}",
                "expires_in": AUTH_SESSION_SECONDS,
                "callback_url": PUBLIC_CALLBACK_URL,
            }

    def _validate_callback(self, auth_code: str, state: str) -> None:
        if not auth_code:
            raise ValueError("平台没有返回授权码，请重新发起授权。")
        if not state:
            raise ValueError("授权状态校验失败，请从店策重新发起授权。")
        session = self._load_session()
        if not session:
            raise ValueError("本次授权已过期，请返回店策重新点击授权。")
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        if not secrets.compare_digest(str(session.get("state_hash") or ""), state_hash):
            raise ValueError("授权状态不匹配，店策已拒绝本次回调。")

    def _fetch_accounts(
        self,
        app_id: str,
        app_secret: str,
        access_token: str,
        fallback_ids: list[str],
    ) -> list[dict[str, Any]]:
        query = urlencode(
            {
                "app_id": app_id,
                "secret": app_secret,
                "access_token": access_token,
            }
        )
        response = _request_json(
            f"{AUTHORIZED_ACCOUNTS_URL}?{query}",
            headers={"Access-Token": access_token},
        )
        data = _platform_data(response, "读取授权账号")
        return _normalize_accounts(data.get("list"), fallback_ids)

    def complete_authorization(self, auth_code: str, state: str) -> dict[str, Any]:
        with _oauth_lock:
            self._validate_callback(str(auth_code or ""), str(state or ""))
            self.session_path.unlink(missing_ok=True)
            config = self._load_config()
            app_secret = self._load_secret()
            if not app_secret:
                raise ValueError("本机没有 App Secret，请返回店策重新填写后授权。")
            response = _request_json(
                TOKEN_URL,
                payload={
                    "app_id": int(config["app_id"]),
                    "secret": app_secret,
                    "grant_type": "auth_code",
                    "auth_code": str(auth_code),
                },
            )
            data = _platform_data(response, "换取 Access Token")
            access_token = str(data.get("access_token") or "")
            refresh_token = str(data.get("refresh_token") or "")
            if not access_token or not refresh_token:
                raise ValueError("平台未返回完整 Token，请重新授权。")
            fallback_ids = [
                str(value)
                for value in (data.get("advertiser_ids") or [])
                if str(value).strip()
            ]
            if not fallback_ids and data.get("advertiser_id"):
                fallback_ids = [str(data["advertiser_id"])]
            try:
                accounts = self._fetch_accounts(
                    config["app_id"],
                    app_secret,
                    access_token,
                    fallback_ids,
                )
                account_warning = ""
            except Exception:
                accounts = _normalize_accounts([], fallback_ids)
                account_warning = "Token 已保存，账号名称将在首次同步 API 数据后补齐。"
            now = int(time.time())
            expires_in = max(0, int(data.get("expires_in") or 0))
            token_record = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": now + expires_in if expires_in else 0,
                "refresh_token_expires_at": now
                + max(0, int(data.get("refresh_token_expires_in") or 0)),
                "authorized_at": now,
                "accounts": accounts,
                "last_error": account_warning,
            }
            _store_encrypted(
                self.token_path,
                token_record,
                "店策 Agent 巨量千川 OAuth Token",
            )
            return {
                "ok": True,
                "account_count": len(accounts),
                "accounts": accounts,
                "warning": account_warning,
            }

    @staticmethod
    def result_page(
        *,
        success: bool,
        title: str,
        message: str,
        account_count: int = 0,
    ) -> bytes:
        tone = "#079455" if success else "#b42318"
        background = "#ecfdf3" if success else "#fef3f2"
        safe_title = html.escape(title)
        safe_message = html.escape(message)
        count = (
            f"<p class='count'>本次已识别 <strong>{account_count}</strong> 个授权账号</p>"
            if success
            else ""
        )
        page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{safe_title}</title>
<style>
*{{box-sizing:border-box}}body{{min-height:100vh;margin:0;display:grid;place-items:center;padding:20px;background:#f4f7fb;color:#101828;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
main{{width:min(100%,560px);padding:32px;border:1px solid #d0d5dd;border-radius:20px;background:#fff;box-shadow:0 20px 60px rgba(16,24,40,.1)}}
.brand{{display:flex;align-items:center;gap:12px;margin-bottom:24px;color:#1849a9;font-weight:800}}.mark{{display:grid;width:42px;height:42px;place-items:center;border-radius:13px;background:linear-gradient(145deg,#153eaf,#3b82f6);color:#fff}}
.state{{padding:18px;border-radius:14px;background:{background};color:{tone}}}h1{{margin:0 0 8px;font-size:24px}}p{{margin:0;line-height:1.65}}.count{{margin-top:12px}}small{{display:block;margin-top:20px;color:#667085;line-height:1.6}}
</style></head><body><main><div class="brand"><span class="mark">策</span><span>店策 Agent</span></div>
<section class="state"><h1>{safe_title}</h1><p>{safe_message}</p>{count}</section>
<small>现在可以关闭本页并返回店策工作台。App Secret 与 Token 只保存在本机。</small>
</main></body></html>"""
        return page.encode("utf-8")
