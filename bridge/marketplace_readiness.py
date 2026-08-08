"""Fail-closed readiness assessment for a Doudian marketplace release."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

from deployment_mode import allowed_web_origins, resolve_deployment_policy
from doudian_marketplace import DoudianAppConfig, platform_adapter_status


PRODUCTION_TOKEN_BACKENDS = {"encrypted_sql", "cloud_kms"}
PRODUCTION_STATE_BACKENDS = {"encrypted_sql", "redis"}
_ICP_PATTERN = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9-]{4,64}(?:ICP(?:备|证))?[0-9A-Za-z-]*号?$", re.IGNORECASE)


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _https_url(name: str) -> tuple[str, bool]:
    value = os.environ.get(name, "").strip()
    parsed = urlparse(value)
    valid = parsed.scheme == "https" and bool(parsed.netloc)
    return value, valid


def _item(item_id: str, label: str, passed: bool, detail: str, *, group: str) -> dict[str, Any]:
    return {
        "id": item_id,
        "label": label,
        "group": group,
        "status": "ready" if passed else "blocked",
        "blocking": not passed,
        "detail": detail,
    }


def build_marketplace_readiness() -> dict[str, Any]:
    policy = resolve_deployment_policy()
    config = DoudianAppConfig.from_env()
    config_errors = config.validate(production=True)
    enterprise_name = os.environ.get("DIAN_AGENT_ENTERPRISE_NAME", "").strip()
    customer_service = os.environ.get("DIAN_AGENT_CUSTOMER_SERVICE", "").strip()
    icp = os.environ.get("DIAN_AGENT_ICP_FILING", "").strip()
    token_backend = os.environ.get("DIAN_AGENT_TOKEN_STORE_BACKEND", "").strip().lower()
    state_backend = os.environ.get("DIAN_AGENT_OAUTH_STATE_BACKEND", "").strip().lower()
    adapter = platform_adapter_status()
    privacy_url, privacy_ok = _https_url("DIAN_AGENT_PRIVACY_URL")
    terms_url, terms_ok = _https_url("DIAN_AGENT_TERMS_URL")
    deletion_url, deletion_ok = _https_url("DIAN_AGENT_DATA_DELETION_URL")
    support_url, support_ok = _https_url("DIAN_AGENT_SUPPORT_URL")
    items = [
        _item(
            "deployment_mode",
            "服务市场发行模式",
            policy.marketplace_release and policy.valid,
            f"当前模式：{policy.mode}；市场模式必须显式配置，不能由本地版自动推断。",
            group="runtime",
        ),
        _item(
            "browser_capabilities_disabled",
            "浏览器采集与 DOM 执行已关闭",
            not policy.browser_page_capture and not policy.browser_dom_execution,
            "服务市场核心链路只允许官方授权和 Open API。",
            group="runtime",
        ),
        _item(
            "cloud_web_origins",
            "HTTPS 工作台来源白名单",
            bool(allowed_web_origins()),
            "必须通过 DIAN_AGENT_ALLOWED_WEB_ORIGINS 配置一个或多个精确 HTTPS Origin。",
            group="runtime",
        ),
        _item(
            "enterprise_identity",
            "企业主体资料",
            bool(enterprise_name) and _truthy("DIAN_AGENT_ENTERPRISE_VERIFIED"),
            "需要企业名称，并由发布流程确认主体资料已审核。",
            group="qualification",
        ),
        _item(
            "service_provider_approval",
            "抖店软件服务商资格",
            _truthy("DIAN_AGENT_DOUDIAN_PROVIDER_APPROVED"),
            "只有平台实际批准后才能标记通过。",
            group="qualification",
        ),
        _item(
            "app_approval",
            "工具型应用与类目批准",
            _truthy("DIAN_AGENT_DOUDIAN_APP_APPROVED"),
            "需要平台分配正式应用并批准目标类目。",
            group="qualification",
        ),
        _item(
            "oauth_configuration",
            "OAuth 应用配置",
            not config_errors,
            "App Key、App Secret 和公网 HTTPS 回调必须由部署环境注入。",
            group="integration",
        ),
        _item(
            "api_contract",
            "Open API 合同已核对",
            _truthy("DIAN_AGENT_DOUDIAN_API_CONTRACT_APPROVED") and adapter["ready"],
            "必须同时具备平台合同审批，以及当前进程中已注入适配器的 contract/build/probe 证据。",
            group="integration",
        ),
        _item(
            "api_adapter",
            "Open API 适配器已联调",
            adapter["ready"],
            "环境变量不能证明适配器可用；必须由云端启动代码注入，并提供合同版本、构建 SHA-256 和部署探针结果。",
            group="integration",
        ),
        _item(
            "token_storage",
            "生产令牌加密存储",
            token_backend in PRODUCTION_TOKEN_BACKENDS,
            "生产环境只接受 encrypted_sql 或 cloud_kms；内存测试存储不能发布。",
            group="security",
        ),
        _item(
            "oauth_state_storage",
            "OAuth state 共享存储",
            state_backend in PRODUCTION_STATE_BACKENDS,
            "多实例生产环境必须使用 encrypted_sql 或 redis 保存一次性 state；进程内存不可发布。",
            group="security",
        ),
        _item(
            "authenticated_gateway",
            "租户与店铺绑定的认证网关",
            _truthy("DIAN_AGENT_AUTH_GATEWAY_CONFIGURED"),
            "现有本地 X-Dian-Agent 请求头不属于云端认证；必须接入 SSO、会话或 JWT 验证器。",
            group="security",
        ),
        _item(
            "privacy_policy",
            "隐私政策",
            privacy_ok,
            privacy_url or "未配置公网 HTTPS 隐私政策地址。",
            group="materials",
        ),
        _item(
            "terms_of_service",
            "用户协议",
            terms_ok,
            terms_url or "未配置公网 HTTPS 用户协议地址。",
            group="materials",
        ),
        _item(
            "data_deletion",
            "数据删除与注销说明",
            deletion_ok,
            deletion_url or "未配置公网 HTTPS 数据删除说明地址。",
            group="materials",
        ),
        _item(
            "support",
            "客服与售后",
            support_ok and bool(customer_service),
            support_url or "未配置客服联系方式和公网 HTTPS 支持页。",
            group="materials",
        ),
        _item(
            "icp_filing",
            "ICP备案",
            bool(icp and _ICP_PATTERN.fullmatch(icp)),
            icp or "未配置 ICP 备案号。",
            group="materials",
        ),
        _item(
            "security_review",
            "安全与隐私验收",
            _truthy("DIAN_AGENT_SECURITY_REVIEW_PASSED"),
            "需完成授权撤销、租户隔离、审计、限流、数据导出和删除测试。",
            group="security",
        ),
    ]
    blockers = [item for item in items if item["blocking"]]
    return {
        "schema_version": 1,
        "product": "dian-agent-doudian-marketplace",
        "ready_for_submission": not blockers,
        "ready_for_public_release": not blockers,
        "status": "ready" if not blockers else "blocked",
        "deployment": policy.public_status(),
        "oauth": config.public_status(production=True),
        "token_storage": {
            "backend": token_backend or "unconfigured",
            "production_safe": token_backend in PRODUCTION_TOKEN_BACKENDS,
        },
        "oauth_state_storage": {
            "backend": state_backend or "unconfigured",
            "production_safe": state_backend in PRODUCTION_STATE_BACKENDS,
        },
        "platform_adapter": adapter,
        "checklist": items,
        "blockers": [{"id": item["id"], "label": item["label"], "detail": item["detail"]} for item in blockers],
        "blocker_count": len(blockers),
        "note": "该状态只接受可核验部署配置；源码中存在适配骨架不代表已获平台审批或可以公开上架。",
    }


__all__ = ["PRODUCTION_STATE_BACKENDS", "PRODUCTION_TOKEN_BACKENDS", "build_marketplace_readiness"]
