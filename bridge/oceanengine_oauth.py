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
import math
import os
import secrets
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from version import AGENT_VERSION

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
MAX_OAUTH_RESPONSE_BYTES = 512 * 1024
MAX_TOKEN_CHARS = 8192
MAX_TOKEN_LIFETIME_SECONDS = 10 * 365 * 24 * 60 * 60
MACOS_KEYCHAIN_ACCOUNT = "DianAgent"
MACOS_APP_SECRET_SERVICE = "com.dianagent.oceanengine.app-secret"
MACOS_TOKEN_SERVICE = "com.dianagent.oceanengine.tokens"

_oauth_lock = threading.Lock()


class _RejectOceanEngineRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        # OAuth requests carry the App Secret, authorization code or refresh
        # token. Never let urllib forward them to a redirected destination.
        try:
            if fp is not None:
                fp.close()
        finally:
            raise URLError("巨量引擎认证接口返回了重定向，已拒绝转发凭证。")


_OCEANENGINE_OPENER = build_opener(_RejectOceanEngineRedirects())


def urlopen(request: Request, timeout: float):
    """Patchable network seam that refuses credential-bearing redirects."""

    return _OCEANENGINE_OPENER.open(request, timeout=timeout)


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"OAuth JSON number {value} is not finite")


def _parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"OAuth JSON number {value} is not finite")
    return parsed


def _required_token(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"OceanEngine {field_name} must be a string")
    token = value.strip()
    if not 8 <= len(token) <= MAX_TOKEN_CHARS:
        raise ValueError(f"OceanEngine {field_name} length is invalid")
    if any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise ValueError(f"OceanEngine {field_name} contains invalid characters")
    return token


def _required_lifetime(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"OceanEngine {field_name} must be a positive integer")
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        seconds = int(value)
    else:
        raise ValueError(f"OceanEngine {field_name} must be a positive integer")
    if not 1 <= seconds <= MAX_TOKEN_LIFETIME_SECONDS:
        raise ValueError(f"OceanEngine {field_name} is outside the accepted range")
    return seconds


def _stored_expiry(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return 0
    return value


def _stored_token(value: Any, field_name: str) -> str:
    try:
        return _required_token(value, field_name)
    except ValueError:
        return ""


def _normalize_advertiser_ids(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError(f"OceanEngine {field_name} must be a bounded list")
    result: list[str] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise ValueError(f"OceanEngine {field_name} contains an invalid account ID")
        account_id = str(item).strip()
        if not account_id.isascii() or not account_id.isdigit() or len(account_id) > 32:
            raise ValueError(f"OceanEngine {field_name} contains an invalid account ID")
        if account_id not in result:
            result.append(account_id)
    return result


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
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"),
    )


def _store_encrypted(path: Path, value: dict[str, Any], description: str) -> None:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    protected = _windows_protect(raw, description)
    _atomic_write_bytes(path, base64.b64encode(protected))


def _load_encrypted(path: Path) -> dict[str, Any]:
    protected = base64.b64decode(path.read_bytes(), validate=True)
    value = json.loads(_windows_unprotect(protected).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("本机授权文件格式错误")
    return value


def _macos_keychain_store(
    service: str,
    value: dict[str, Any],
    *,
    account: str = MACOS_KEYCHAIN_ACCOUNT,
) -> None:
    """Store one JSON record through macOS Keychain Services."""

    if sys.platform != "darwin":
        raise RuntimeError("当前系统不支持 macOS 钥匙串")
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    try:
        security = ctypes.CDLL(
            "/System/Library/Frameworks/Security.framework/Security"
        )
        core_foundation = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
    except OSError as error:
        raise RuntimeError("无法访问 macOS 钥匙串，请确认当前用户已解锁登录钥匙串。") from error

    service_bytes = service.encode("utf-8")
    account_bytes = str(account or "").encode("utf-8")
    if not account_bytes:
        raise RuntimeError("macOS 钥匙串账户标识不能为空。")
    value_bytes = raw.encode("utf-8")
    value_buffer = ctypes.create_string_buffer(value_bytes)
    item_ref = ctypes.c_void_p()
    security.SecKeychainFindGenericPassword.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    status = security.SecKeychainFindGenericPassword(
        None,
        len(service_bytes),
        service_bytes,
        len(account_bytes),
        account_bytes,
        None,
        None,
        ctypes.byref(item_ref),
    )
    if status == 0:
        security.SecKeychainItemModifyAttributesAndData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        status = security.SecKeychainItemModifyAttributesAndData(
            item_ref,
            None,
            len(value_bytes),
            ctypes.cast(value_buffer, ctypes.c_void_p),
        )
        core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
        core_foundation.CFRelease(item_ref)
    elif status == -25300:  # errSecItemNotFound
        security.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        status = security.SecKeychainAddGenericPassword(
            None,
            len(service_bytes),
            service_bytes,
            len(account_bytes),
            account_bytes,
            len(value_bytes),
            ctypes.cast(value_buffer, ctypes.c_void_p),
            None,
        )
    if status != 0:
        raise RuntimeError("macOS 钥匙串拒绝保存凭证，请解锁登录钥匙串后重试。")


def _macos_keychain_load(
    service: str,
    *,
    account: str = MACOS_KEYCHAIN_ACCOUNT,
) -> dict[str, Any]:
    if sys.platform != "darwin":
        return {}
    try:
        security = ctypes.CDLL(
            "/System/Library/Frameworks/Security.framework/Security"
        )
        core_foundation = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
    except OSError:
        return {}

    service_bytes = service.encode("utf-8")
    account_bytes = str(account or "").encode("utf-8")
    if not account_bytes:
        return {}
    value_length = ctypes.c_uint32()
    value_pointer = ctypes.c_void_p()
    item_ref = ctypes.c_void_p()
    security.SecKeychainFindGenericPassword.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    status = security.SecKeychainFindGenericPassword(
        None,
        len(service_bytes),
        service_bytes,
        len(account_bytes),
        account_bytes,
        ctypes.byref(value_length),
        ctypes.byref(value_pointer),
        ctypes.byref(item_ref),
    )
    if status != 0:
        return {}
    try:
        raw = ctypes.string_at(value_pointer, value_length.value).decode("utf-8")
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = {}
    finally:
        security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        security.SecKeychainItemFreeContent(None, value_pointer)
        core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
        core_foundation.CFRelease(item_ref)
    return value if isinstance(value, dict) else {}


def _secure_storage_label() -> str:
    if sys.platform == "win32":
        return "windows_dpapi"
    if sys.platform == "darwin":
        return "macos_keychain"
    return "environment"


def _request_json(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request_headers = {
        "Accept": "application/json",
        "User-Agent": f"DianAgent/{AGENT_VERSION}",
        **(headers or {}),
    }
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json; charset=utf-8"
        method = "POST"
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
            raw_bytes = response.read(MAX_OAUTH_RESPONSE_BYTES + 1)
            if len(raw_bytes) > MAX_OAUTH_RESPONSE_BYTES:
                raise ValueError("OceanEngine OAuth response is too large")
            try:
                raw = raw_bytes.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("OceanEngine OAuth response is not valid UTF-8") from error
    except HTTPError as error:
        # Keep the established HTTPError contract while releasing its response.
        error.close()
        raise
    value = json.loads(
        raw,
        parse_float=_parse_finite_json_float,
        parse_constant=_reject_nonfinite_json_constant,
    )
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
        if sys.platform == "darwin":
            value = _macos_keychain_load(MACOS_APP_SECRET_SERVICE)
            return str(value.get("app_secret") or "")
        if sys.platform != "win32" or not self.secret_path.exists():
            return ""
        value = _load_encrypted(self.secret_path)
        return str(value.get("app_secret") or "")

    def _load_tokens(self) -> dict[str, Any]:
        if sys.platform == "darwin":
            return _macos_keychain_load(MACOS_TOKEN_SERVICE)
        if sys.platform != "win32" or not self.token_path.exists():
            return {}
        try:
            return _load_encrypted(self.token_path)
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _store_secret(self, app_secret: str) -> None:
        value = {"app_secret": app_secret}
        if sys.platform == "darwin":
            _macos_keychain_store(MACOS_APP_SECRET_SERVICE, value)
            return
        if sys.platform == "win32":
            _store_encrypted(
                self.secret_path,
                value,
                "店策 Agent 巨量千川 App Secret",
            )
            return
        raise ValueError("当前系统不会把 App Secret 写入磁盘，请通过 OCEANENGINE_APP_SECRET 环境变量提供。")

    def _store_tokens(self, tokens: dict[str, Any]) -> None:
        if sys.platform == "darwin":
            _macos_keychain_store(MACOS_TOKEN_SERVICE, tokens)
            return
        if sys.platform == "win32":
            _store_encrypted(
                self.token_path,
                tokens,
                "店策 Agent 巨量千川 OAuth Token",
            )
            return
        raise ValueError("当前系统不支持安全保存 Token，请改用 Windows 或 macOS 本机 Agent。")

    def save_credentials(self, app_id: str, app_secret: str = "") -> None:
        app_id = str(app_id or "").strip()
        app_secret = str(app_secret or "").strip()
        if not app_id.isdigit() or not 10 <= len(app_id) <= 24:
            raise ValueError("App ID 格式不正确，请填写开放平台显示的数字 App ID。")
        if app_secret and not 8 <= len(app_secret) <= 256:
            raise ValueError("App Secret 格式不正确，请重新复制完整密钥。")
        if app_secret and sys.platform not in {"win32", "darwin"}:
            raise ValueError(
                "当前系统不会把 App Secret 写入磁盘，请通过 OCEANENGINE_APP_SECRET 环境变量提供。"
            )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            self.config_path,
            {
                "app_id": app_id,
                "updated_at": int(time.time()),
                "secret_storage": _secure_storage_label(),
            },
        )
        if app_secret:
            self._store_secret(app_secret)
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
                "account_name": str(account.get("account_name") or ""),
                "account_role": str(account.get("account_role") or ""),
                "valid": bool(account.get("valid", True)),
                "advertiser_count": len(account.get("advertiser_ids") or []),
                "account_hint": f"•••• {str(account.get('account_id') or '')[-4:]}"
                if account.get("account_id") else "",
            }
            for account in accounts
            if isinstance(account, dict)
        ]
        access_token = _stored_token(tokens.get("access_token"), "access_token")
        expires_at = _stored_expiry(tokens.get("expires_at"))
        refresh_token = _stored_token(tokens.get("refresh_token"), "refresh_token")
        refresh_expires_at = _stored_expiry(tokens.get("refresh_token_expires_at"))
        now = int(time.time())
        secret_saved = bool(self._load_secret())
        connected = bool(access_token) and expires_at > now
        refresh_available = bool(refresh_token) and refresh_expires_at > now and secret_saved
        return {
            "app_id": config["app_id"],
            "callback_url": PUBLIC_CALLBACK_URL,
            "secret_saved": secret_saved,
            "secret_storage": _secure_storage_label(),
            "connected": connected,
            "needs_refresh": bool(access_token) and not connected,
            "refresh_available": refresh_available,
            "account_count": len(public_accounts),
            "accounts": public_accounts,
            "expires_at": expires_at or None,
            "refresh_token_expires_at": refresh_expires_at or None,
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
            access_token = _stored_token(tokens.get("access_token"), "access_token")
            expires_at = _stored_expiry(tokens.get("expires_at"))
            if access_token and expires_at > int(time.time()) + 300:
                return access_token
            refresh_token = _stored_token(tokens.get("refresh_token"), "refresh_token")
            refresh_expires_at = _stored_expiry(tokens.get("refresh_token_expires_at"))
            app_secret = self._load_secret()
            if (
                not refresh_token
                or refresh_expires_at <= int(time.time())
                or not app_secret
            ):
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
            next_access_token = _required_token(data.get("access_token"), "access_token")
            raw_refresh_token = data.get("refresh_token")
            next_refresh_token = (
                _required_token(raw_refresh_token, "refresh_token")
                if raw_refresh_token is not None
                else refresh_token
            )
            if not next_access_token:
                raise ValueError("平台未返回新的 Access Token，请重新授权账号。")
            now = int(time.time())
            expires_in = _required_lifetime(data.get("expires_in"), "expires_in")
            refresh_expires_in = _required_lifetime(
                data.get("refresh_token_expires_in"), "refresh_token_expires_in"
            )
            tokens.update(
                {
                    "access_token": next_access_token,
                    "refresh_token": next_refresh_token,
                    "expires_at": now + expires_in,
                    "refresh_token_expires_at": now + refresh_expires_in,
                    "last_error": "",
                }
            )
            self._store_tokens(tokens)
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
            self._store_tokens(tokens)

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
            access_token = _required_token(data.get("access_token"), "access_token")
            refresh_token = _required_token(data.get("refresh_token"), "refresh_token")
            if not access_token or not refresh_token:
                raise ValueError("平台未返回完整 Token，请重新授权。")
            fallback_ids = _normalize_advertiser_ids(
                data.get("advertiser_ids"), "advertiser_ids"
            )
            if not fallback_ids and data.get("advertiser_id"):
                fallback_ids = _normalize_advertiser_ids(
                    [data["advertiser_id"]], "advertiser_id"
                )
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
            expires_in = _required_lifetime(data.get("expires_in"), "expires_in")
            refresh_expires_in = _required_lifetime(
                data.get("refresh_token_expires_in"), "refresh_token_expires_in"
            )
            token_record = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": now + expires_in,
                "refresh_token_expires_at": now + refresh_expires_in,
                "authorized_at": now,
                "accounts": accounts,
                "last_error": account_warning,
            }
            self._store_tokens(token_record)
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
