"""Operating-system storage for Feishu and DingTalk Webhook secrets.

The notification configuration file is deliberately a non-secret projection.
Webhook URLs are credentials and therefore use the same current-user DPAPI and
macOS login Keychain primitives as OceanEngine OAuth and AI provider keys.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from oceanengine_oauth import (
    MACOS_KEYCHAIN_ACCOUNT,
    _load_encrypted,
    _macos_keychain_load,
    _macos_keychain_store,
    _store_encrypted,
)


MACOS_INTEGRATION_SECRET_SERVICE = "com.dianagent.integrations.webhooks"
WINDOWS_INTEGRATION_SECRET_DESCRIPTION = "DianAgent notification Webhooks"


class IntegrationSecretStoreError(RuntimeError):
    """Raised without including a secret or a credential-bearing URL."""

    def __init__(self, message: str, *, outcome: str = "unchanged") -> None:
        super().__init__(message)
        self.outcome = outcome


class IntegrationSecretStore:
    """Persist one JSON record in the current OS user's protected store."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        install_id: str,
        install_root: str | Path | None = None,
    ):
        self.data_dir = Path(data_dir)
        normalized_install_id = str(install_id or "").lower()
        if not re.fullmatch(r"[0-9a-f]{32}", normalized_install_id):
            raise IntegrationSecretStoreError(
                "本机安装身份无效；通知密钥存储已停止初始化。"
            )
        root = Path(install_root) if install_root is not None else self.data_dir.parent
        try:
            normalized_root = os.path.normcase(str(root.expanduser().resolve(strict=False)))
        except OSError:
            raise IntegrationSecretStoreError(
                "本机安装目录无法规范化；通知密钥存储已停止初始化。"
            ) from None
        namespace = hashlib.sha256(
            f"{normalized_install_id}\0{normalized_root}".encode("utf-8")
        ).hexdigest()[:24]
        self.install_id = normalized_install_id
        self.install_root = Path(normalized_root)
        self.keychain_service = f"{MACOS_INTEGRATION_SECRET_SERVICE}.{namespace}"
        self.keychain_account = f"DianAgent:{normalized_install_id}:{namespace}"
        self.windows_path = self.data_dir / "integration-webhooks.dpapi"

    @staticmethod
    def label() -> str:
        if sys.platform == "win32":
            return "windows_dpapi"
        if sys.platform == "darwin":
            return "macos_keychain"
        return "unavailable"

    @staticmethod
    def _normalize(record: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise IntegrationSecretStoreError("通知密钥存储中的记录格式无效，已停止读取。")
        try:
            value = json.loads(
                json.dumps(record, ensure_ascii=False, allow_nan=False)
            )
        except (TypeError, ValueError, OverflowError):
            raise IntegrationSecretStoreError(
                "通知密钥记录无法安全序列化，已停止保存。"
            ) from None
        if not isinstance(value, dict):  # pragma: no cover - guarded above
            raise IntegrationSecretStoreError("通知密钥存储中的记录格式无效，已停止读取。")
        return value

    def _validate_identity(self, record: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize(record)
        if str(normalized.get("install_id") or "").lower() != self.install_id:
            raise IntegrationSecretStoreError(
                "通知密钥记录不属于当前安装；为避免跨实例误发，本次读取已停止。"
            )
        return normalized

    def _migrate_legacy_record(
        self,
        record: dict[str, Any],
        *,
        expected_revision: str,
    ) -> dict[str, Any]:
        normalized = self._normalize(record)
        revision = str(normalized.get("revision") or "").lower()
        legacy_install_id = str(normalized.get("install_id") or "").lower()
        if (
            not expected_revision
            or revision != expected_revision
            or (legacy_install_id and legacy_install_id != self.install_id)
        ):
            raise IntegrationSecretStoreError(
                "旧版通知密钥记录无法证明属于当前安装；本次迁移已停止。"
            )
        migrated = {**normalized, "install_id": self.install_id}
        self.store(migrated)
        return migrated

    def load(
        self,
        *,
        required: bool = False,
        legacy_revision: str = "",
    ) -> dict[str, Any]:
        """Load a record; a configured integration must set ``required``."""

        try:
            if sys.platform == "win32":
                if self.data_dir.is_symlink() or (
                    self.data_dir.exists() and not self.data_dir.is_dir()
                ):
                    raise OSError("protected record parent is not a trusted directory")
                if self.windows_path.is_symlink():
                    raise OSError("protected record path is a symbolic link")
                if not self.windows_path.exists():
                    if required:
                        raise FileNotFoundError("protected record is missing")
                    return {}
                if not self.windows_path.is_file():
                    raise OSError("protected record path is not a regular file")
                value = self._normalize(_load_encrypted(self.windows_path))
                if not value.get("install_id") and legacy_revision:
                    return self._migrate_legacy_record(
                        value, expected_revision=legacy_revision
                    )
                return self._validate_identity(value)
            if sys.platform == "darwin":
                value = self._normalize(
                    _macos_keychain_load(
                        self.keychain_service,
                        account=self.keychain_account,
                    )
                )
                if value:
                    return self._validate_identity(value)
                if legacy_revision:
                    legacy = self._normalize(
                        _macos_keychain_load(
                            MACOS_INTEGRATION_SECRET_SERVICE,
                            account=MACOS_KEYCHAIN_ACCOUNT,
                        )
                    )
                    if legacy:
                        return self._migrate_legacy_record(
                            legacy, expected_revision=legacy_revision
                        )
                if required and not value:
                    raise OSError("protected record is missing or Keychain is locked")
                return {}
            if required:
                raise OSError("this operating system has no supported secret store")
            return {}
        except IntegrationSecretStoreError:
            raise
        except Exception:
            # Native APIs and mocked backends can include credential material in
            # their errors.  Never propagate or log their original message.
            raise IntegrationSecretStoreError(
                "通知密钥存储不可用；为保护 Webhook，本次读取已停止。"
            ) from None

    def store(self, record: dict[str, Any]) -> None:
        """Store and read back a record before plaintext metadata is committed."""

        normalized = self._validate_identity(record)
        # Snapshot the exact protected value before starting a native write.
        # Some native APIs can commit and then fail during readback; reporting
        # that case as "not applied" would leave callers with the wrong state.
        previous = self.load(required=False)
        try:
            if sys.platform == "win32":
                if self.data_dir.is_symlink() or (
                    self.data_dir.exists() and not self.data_dir.is_dir()
                ):
                    raise OSError("protected record parent is not a trusted directory")
                if self.windows_path.is_symlink():
                    raise OSError("protected record path is a symbolic link")
                _store_encrypted(
                    self.windows_path,
                    normalized,
                    WINDOWS_INTEGRATION_SECRET_DESCRIPTION,
                )
            elif sys.platform == "darwin":
                _macos_keychain_store(
                    self.keychain_service,
                    normalized,
                    account=self.keychain_account,
                )
            else:
                raise OSError("this operating system has no supported secret store")
            verified = self.load(required=True)
            if verified != normalized:
                raise OSError("protected record verification mismatch")
        except Exception:
            rollback_verified = False
            if previous:
                try:
                    if sys.platform == "win32":
                        _store_encrypted(
                            self.windows_path,
                            previous,
                            WINDOWS_INTEGRATION_SECRET_DESCRIPTION,
                        )
                    elif sys.platform == "darwin":
                        _macos_keychain_store(
                            self.keychain_service,
                            previous,
                            account=self.keychain_account,
                        )
                    restored = self.load(required=True)
                    rollback_verified = restored == previous
                except Exception:
                    rollback_verified = False
            elif sys.platform == "win32":
                # The DPAPI backend is an Agent-owned file. A failed first
                # write can therefore be rolled back to a verified absence.
                # We do not guess at deleting a new macOS Keychain item: that
                # case is surfaced as explicitly unknown below.
                try:
                    if self.windows_path.is_symlink():
                        raise OSError("protected record path changed")
                    if self.windows_path.exists():
                        if not self.windows_path.is_file():
                            raise OSError("protected record path changed")
                        self.windows_path.unlink()
                    rollback_verified = self.load(required=False) == {}
                except Exception:
                    rollback_verified = False

            if rollback_verified:
                raise IntegrationSecretStoreError(
                    "通知密钥保存失败，原有记录已验证恢复。",
                    outcome="rolled_back",
                ) from None
            raise IntegrationSecretStoreError(
                "通知密钥保存结果无法确认；请先停止发送并重新读取配置。",
                outcome="unknown",
            ) from None
