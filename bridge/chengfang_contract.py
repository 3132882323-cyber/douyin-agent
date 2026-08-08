"""Versioned, fail-closed Chengfang page and field contract registry.

No selector, route, API field, or policy rate belongs here until it has been
verified against an authorized account and recorded with review evidence.
"""

from __future__ import annotations

from typing import Any

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
            "尚未取得或验证官方写接口合同。",
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
