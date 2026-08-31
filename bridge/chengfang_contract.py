"""Versioned, fail-closed Chengfang page and field contract registry.

No selector, route, API field, or policy rate belongs here until it has been
verified against an authorized account and recorded with review evidence.
"""

from __future__ import annotations

from typing import Any

try:
    from chengfang_official_contract import build_official_control_task_contract
except ModuleNotFoundError as error:
    if error.name != "chengfang_official_contract":
        raise
    from chengfang_public_fallback import build_official_control_task_contract

REGISTRY_SCHEMA_VERSION = 1
CHENGFANG_CONTRACT_VERSION = "unverified-2026-08"


def build_chengfang_contract_registry() -> dict[str, Any]:
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "contract_version": CHENGFANG_CONTRACT_VERSION,
        "status": "unverified",
        "verified": False,
        "pages": [],
        "fields": [],
        "api_contracts": [],
        # Machine-readable discovery record for the future official adapter.
        # It remains explicitly unverified and cannot populate api_contracts.
        "official_control_task_contract": build_official_control_task_contract(),
        # Public official documentation is useful for product discovery, but
        # it is not an account authorization or a field-level write contract.
        "official_capability_discovery": [
            {
                "capability": "create_chengfang_live_plan",
                "label": "新建乘方直播计划",
                "method": "POST",
                "endpoint": "/open_api/v1.0/qianchuan/overall_live/create/",
                "status": "documented_not_verified_for_account",
                "source": "https://open.oceanengine.com/tools/visual_debug.html?docId=1871954016096256",
            },
            {
                "capability": "create_chengfang_product_plan",
                "label": "新建乘方商品计划",
                "method": "POST",
                "endpoint": "/open_api/v1.0/qianchuan/overall_video/create/",
                "status": "documented_not_verified_for_account",
                "source": "https://open.oceanengine.com/tools/visual_debug.html?docId=1872485038037385",
            },
            {
                "capability": "upgrade_full_domain_plan_to_chengfang",
                "label": "全域计划升级为乘方计划",
                "method": "POST",
                "endpoint": "/open_api/v1.0/qianchuan/ad/overall_marketing/update/",
                "status": "documented_not_verified_for_account",
                "source": "https://open.oceanengine.com/tools/visual_debug.html?docId=1866761206705753",
            },
            {
                "capability": "read_full_domain_and_chengfang_plan_detail",
                "label": "获取全域与乘方投放计划详情",
                "method": "GET",
                "endpoint": "/open_api/v1.0/qianchuan/ad/detail/",
                "status": "documented_not_verified_for_account",
                "source": "https://open.oceanengine.com/tools/visual_debug.html?docId=1804362305657868",
            },
            {
                "capability": "create_full_domain_control_task",
                "label": "创建全域计划调控任务",
                "method": "POST",
                "endpoint": "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/create/",
                "status": "documented_not_verified_for_chengfang_write",
                "source": "https://open.oceanengine.com/tools/visual_debug.html?docId=1825827435645963",
            },
            {
                "capability": "list_full_domain_control_tasks",
                "label": "获取全域计划调控任务列表",
                "method": "GET",
                "status": "documented_not_verified_fields_pending",
                "source": "https://open.oceanengine.com/tools/visual_debug.html?docId=1824940765838411",
            },
            {
                "capability": "update_chengfang_budget",
                "label": "更新乘方与全域计划预算",
                "method": "POST",
                "endpoint": "/open_api/v1.0/qianchuan/uni_promotion/ad/budget/update/",
                "status": "documented_not_verified_for_account",
                "source": "https://open.oceanengine.com/tools/visual_debug.html?docId=1841395352172800",
            },
            {
                "capability": "update_chengfang_roi_goal",
                "label": "更新乘方与全域计划 ROI 目标",
                "method": "POST",
                "endpoint": "/open_api/v1.0/qianchuan/ad/roi2_goal/update/",
                "status": "documented_not_verified_for_account",
                "source": "https://open.oceanengine.com/tools/visual_debug.html?docId=1841394087572811",
            },
            {
                "capability": "update_chengfang_status",
                "label": "更新乘方与全域计划状态",
                "method": "POST",
                "endpoint": "/open_api/v1.0/qianchuan/uni_promotion/ad/status/update/",
                "status": "documented_not_verified_for_account",
                "source": "https://open.oceanengine.com/tools/visual_debug.html?docId=1804364027501580",
            },
        ],
        "selectors": [],
        "fingerprint_policy": {
            "required_evidence": ["authorized_account", "captured_at", "page_url_origin", "visible_labels", "reviewer"],
            "minimum_independent_samples": 2,
            "allow_unverified_match": False,
        },
        "write_capabilities": [],
        "blockers": [
            "尚未在授权乘方账户中验证页面结构。",
            "尚未建立经过复核的字段和页面指纹合同。",
            "官方写能力已发现，但当前应用权限、账户白名单、请求字段和回读合同尚未完成受控验收。",
        ],
    }


def assess_page_fingerprint(observation: Any, registry: Any = None) -> dict[str, Any]:
    active = registry if isinstance(registry, dict) else build_chengfang_contract_registry()
    observed = observation if isinstance(observation, dict) else {}
    # An empty registry can never recognize a page, regardless of supplied
    # labels.  This prevents visible text from silently becoming a selector.
    verified_pages = [item for item in active.get("pages", []) if isinstance(item, dict) and item.get("verified") is True]
    return {
        "matched": False,
        "verified": False,
        "contract_version": str(active.get("contract_version") or CHENGFANG_CONTRACT_VERSION),
        "observation_present": bool(observed),
        "verified_page_count": len(verified_pages),
        "reason": "NO_VERIFIED_PAGE_FINGERPRINT",
        "next_step": "登录授权乘方账户采集至少两个独立样本，经人工复核后登记页面指纹。",
    }
