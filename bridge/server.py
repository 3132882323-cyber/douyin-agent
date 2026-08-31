"""店策 Agent MCP Server。

从本地 companion service 的快照目录读取脱敏数据。MCP 进程不连接浏览器、
不读取 Cookie，也不执行任何店铺或资金写操作。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

BRIDGE_DIR = str(Path(__file__).resolve().parent)
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

from ai_decision import DECISION_PROPOSAL_SCHEMA
from http_receiver import (
    STALE_SECONDS,
    _cached,
    _invalidate_cache,
    build_action_center,
    build_automation_readiness,
    build_execution_preflight_report,
    build_insights,
    build_inventory_alerts,
    build_live_analysis,
    build_ops_manager,
    build_plan_recommendations,
    build_qianchuan_creative_analysis,
    build_scan_receipt,
    build_shadow_execution_report,
    build_shelf_analysis,
    build_trends,
    check_selector_health,
    export_tasks,
    generate_daily_report,
    get_action_audit,
    get_effectiveness_report,
    get_feedback_stats,
    list_snapshots,
    list_qianchuan_accounts,
    load_agent_settings,
    load_data,
    load_latest_report,
    load_scan_status,
    save_agent_settings,
    update_task_state,
)

# These optional wrappers are the only integration seam between MCP and the AI
# subsystem.  Their contract is deliberately proposal-only: build a sanitized
# context pack, enqueue an untrusted proposal, or list queued proposals.  MCP
# never imports an authorization/execution primitive and remains usable while
# older local Agent builds are being upgraded.
try:
    from http_receiver import (
        get_ai_context_pack as _get_ai_context_pack,
        get_ai_proposals as _get_ai_proposals,
        submit_ai_proposal as _submit_ai_proposal,
    )
except ImportError:  # pragma: no cover - exercised through adapter behavior
    _get_ai_context_pack = None
    _get_ai_proposals = None
    _submit_ai_proposal = None

app = Server("dian-agent")

PAGE_TYPE_PROPERTY = {
    "type": "string",
    "description": "可选页面类型；留空返回该平台最新快照",
    "default": "",
}

TOOLS = [
    Tool(
        name="get_doudian_data",
        description="读取抖店网页版快照。支持经营概览、订单、商品、库存、售后、评价、直播、罗盘和资金页面。默认隐藏整页原始文本。",
        inputSchema={
            "type": "object",
            "properties": {
                "page_type": PAGE_TYPE_PROPERTY,
                "include_page_text": {"type": "boolean", "description": "是否包含脱敏后的整页文本", "default": False},
            },
            "required": [],
        },
    ),
    Tool(
        name="get_qianchuan_data",
        description="读取巨量千川网页版快照。支持账户、推广计划、投放报表、素材和精选联盟页面。默认隐藏整页原始文本。",
        inputSchema={
            "type": "object",
            "properties": {
                "page_type": PAGE_TYPE_PROPERTY,
                "include_page_text": {"type": "boolean", "description": "是否包含脱敏后的整页文本", "default": False},
            },
            "required": [],
        },
    ),
    Tool(
        name="list_cached_pages",
        description="列出本机已经同步的抖店/千川页面、数据时间、结构化字段数和质量评分",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_today_brief",
        description="根据本机最新网页快照生成今日经营简报、数据覆盖和优先处理事项；每项建议带证据来源",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_operation_insights",
        description="获取确定性异常诊断，包括数据过期、页面字段缺失、低 ROI、退款率和低库存提示",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_bridge_status",
        description="检查本地数据桥状态和各页面的数据新鲜度",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_ai_context_pack",
        description="读取供外部 AI 分析的脱敏聚合上下文；不包含 Cookie、Token、客户/订单明细或整页原文，且不能执行投放操作",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="submit_ai_proposal",
        description="验证并存入一条外部 AI 建议；建议始终不可信且不可执行，不授权、不确认也不修改浏览器或投放平台",
        inputSchema={
            "type": "object",
            "properties": {
                "proposal": {
                    **deepcopy(DECISION_PROPOSAL_SCHEMA),
                    "description": "DecisionProposalV1 建议对象，由本地规则再次执行权威校验",
                },
                "provider_id": {
                    "type": "string",
                    "description": "可选 AI Provider 标识，仅用于本地审计",
                    "default": "mcp",
                    "maxLength": 80,
                },
                "model": {
                    "type": "string",
                    "description": "可选模型标识，仅用于本地审计",
                    "default": "",
                    "maxLength": 120,
                },
                "context_hash": {
                    "type": "string",
                    "description": "建议所依据的脱敏上下文哈希",
                    "default": "",
                    "maxLength": 128,
                },
            },
            "required": ["proposal"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_ai_proposals",
        description="读取本机 AI 建议队列及校验状态；只读且所有建议均不可直接执行",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 50,
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_qianchuan_adjustments",
        description="按计划明细生成带账号、计划 ID、当前值、目标值和安全校验的千川操作草稿；不执行投放变更",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_qianchuan_automation_readiness",
        description="将千川建议分为可进入执行前检查、等待人工授权、暂时阻止和仅人工处理；用于自动化投放前置资格检查，不执行投放变更",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_qianchuan_execution_preflight",
        description="读取当前受监督执行前检查会话及账号、计划、预算、时效和质量闸门结果；只读，不执行投放变更",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_qianchuan_action_audit",
        description="读取用户在扩展中确认或撤销的千川操作方案记录；只读，不确认也不执行操作",
        inputSchema={"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100}}, "required": []},
    ),
    Tool(
        name="get_qianchuan_shadow_execution",
        description="读取千川影子执行报告，对比已确认方案、用户声明的人工操作和后续页面回读结果；不执行投放变更",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_qianchuan_creative_analysis",
        description="分析巨量千川视频库素材的消耗、ROI、成交、素材评估和测试覆盖，输出直播引流素材分层与优化建议",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_qianchuan_accounts",
        description="列出本机已经识别的巨量千川账号及当前选择的分析账号",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="select_qianchuan_account",
        description="选择后续千川分析使用的账号；仅修改本地分析设置，不登录或切换官方后台账号",
        inputSchema={"type": "object", "properties": {"account_key": {"type": "string"}}, "required": ["account_key"]},
    ),
    Tool(
        name="get_inventory_alerts",
        description="读取商品和库存快照，按缺货、极低库存和预计可售天数输出分级预警",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_action_center",
        description="一次获取千川计划调整建议、库存预警、阈值和风险汇总",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_shelf_analysis",
        description="获取抖音货架曝光、点击、成交漏斗、页面风险与可验收的优化建议",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_live_dashboard_analysis",
        description="获取店铺直播与千川直播大屏分析，定位观看、点击、成交和投放瓶颈",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_ops_manager",
        description="按货架商品、直播投放和内容三条业务链路汇总数据，输出今日优先任务、负责人和验收标准",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="update_operation_task",
        description="更新本地运营任务状态，不修改抖店或千川；状态支持 todo、doing、observing、done",
        inputSchema={"type": "object", "properties": {"task_id": {"type": "string"}, "status": {"type": "string", "enum": ["todo", "doing", "observing", "done"]}}, "required": ["task_id", "status"]},
    ),
    Tool(
        name="get_auto_scan_status",
        description="读取扩展最近一次无 API 全店自动巡检的进度、成功页面和失败原因",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_scan_receipt",
        description="读取最近一次巡查的数据体检单，包括页面覆盖率、质量分、失败原因、账号和单页重试目标",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_operation_trends",
        description="读取近 1 到 90 天脱敏历史快照，返回关键经营指标的趋势和变化幅度",
        inputSchema={"type": "object", "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7}, "source": {"type": "string", "default": ""}, "page_type": {"type": "string", "default": ""}}, "required": []},
    ),
    Tool(
        name="get_daily_report",
        description="读取最近一份本地每日经营报告",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="generate_daily_report",
        description="根据本地最新脱敏快照立即生成 Markdown 每日经营报告，不修改店铺或投放数据",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_agent_settings",
        description="读取千川 ROI 目标、消耗判断门槛、库存阈值和每日定时报告设置",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_health_monitor",
        description="对比历史基线检测数据异常波动，包括指标偏移、数据过期和页面质量下降",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_effectiveness_report",
        description="查看已完成建议的实际效果评估报告，包括有效率和改进统计",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_feedback_stats",
        description="查看用户对建议的点赞/点踩统计，用于了解哪些建议类型更受欢迎",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="export_tasks",
        description="将今日运营任务导出为结构化文本，支持 clipboard 和 markdown 格式",
        inputSchema={"type": "object", "properties": {"format": {"type": "string", "enum": ["clipboard", "markdown"], "default": "clipboard"}}, "required": []},
    ),
]

# The stdio MCP endpoint is intended for third-party AI clients.  Keep its
# public surface narrower than the local HTTP/UI surface: raw snapshots,
# reports, settings and task mutation tools remain local implementation
# details and cannot be reached by guessing an unlisted tool name.
AI_SAFE_TOOL_NAMES = frozenset({
    "get_ai_context_pack",
    "submit_ai_proposal",
    "get_ai_proposals",
})
_LEGACY_LOCAL_TOOLS = tuple(TOOLS)
TOOLS = [tool for tool in _LEGACY_LOCAL_TOOLS if tool.name in AI_SAFE_TOOL_NAMES]


def _text(value: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(value, ensure_ascii=False, indent=2))]


def _ai_proposal_only_response(value: Any) -> dict[str, Any]:
    """Seal every AI MCP response with a non-execution capability claim."""

    result = deepcopy(value) if isinstance(value, dict) else {"data": deepcopy(value)}
    result["mode"] = "proposal_only"
    result["execution_allowed"] = False
    result["can_execute"] = False
    result["execution_performed"] = False
    return result


def _ai_unavailable_response() -> dict[str, Any]:
    return _ai_proposal_only_response(
        {
            "ok": False,
            "error": {
                "code": "AI_BRIDGE_UNAVAILABLE",
                "message": "本地 AI 建议模块尚未就绪，请更新或重启本地 Agent。",
            },
        }
    )


def _ai_rejected_response() -> dict[str, Any]:
    return _ai_proposal_only_response(
        {
            "ok": False,
            "error": {
                "code": "AI_PROPOSAL_REJECTED",
                "message": "AI 建议未通过本地格式或安全校验，未进入建议队列。",
            },
        }
    )


def _public_snapshot(snapshot: dict[str, Any] | None, include_page_text: bool) -> dict[str, Any]:
    if not snapshot:
        return {"error": "暂无对应网页数据，请打开后台页面并在扩展中点击同步"}
    result = deepcopy(snapshot)
    data = result.get("data")
    if isinstance(data, dict) and not include_page_text:
        data.pop("page_text", None)
    age = max(0, int(time.time() - float(result.get("timestamp", 0))))
    result["age_seconds"] = age
    result["fresh"] = age < STALE_SECONDS
    return result


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    arguments = arguments or {}
    if name not in AI_SAFE_TOOL_NAMES:
        return _text(_ai_proposal_only_response({
            "ok": False,
            "error": {
                "code": "MCP_TOOL_NOT_ALLOWED",
                "message": "外部 AI 只能读取脱敏经营上下文并提交不可执行提案。",
            },
        }))
    if name in {"get_doudian_data", "get_qianchuan_data"}:
        source = "doudian" if name == "get_doudian_data" else "qianchuan"
        page_type = str(arguments.get("page_type") or "") or None
        include_page_text = bool(arguments.get("include_page_text", False))
        return _text(_public_snapshot(load_data(source, page_type), include_page_text))

    if name == "list_cached_pages":
        return _text({"snapshots": list_snapshots()})

    if name in {"get_today_brief", "get_operation_insights"}:
        return _text(build_insights())

    if name == "get_bridge_status":
        snapshots = list_snapshots()
        return _text(
            {
                "status": "ok",
                "mode": "proposal_only",
                "execution_enabled": False,
                "snapshot_count": len(snapshots),
                "fresh_count": sum(1 for item in snapshots if item["fresh"]),
                "sources": {
                    source: {
                        "pages": sum(1 for item in snapshots if item["source"] == source),
                        "has_fresh_data": any(item["source"] == source and item["fresh"] for item in snapshots),
                    }
                    for source in ("doudian", "qianchuan")
                },
            }
        )

    if name == "get_ai_context_pack":
        if _get_ai_context_pack is None:
            return _text(_ai_unavailable_response())
        try:
            return _text(_ai_proposal_only_response(_get_ai_context_pack()))
        except Exception:
            return _text(_ai_rejected_response())

    if name == "submit_ai_proposal":
        proposal = arguments.get("proposal")
        if not isinstance(proposal, dict) or _submit_ai_proposal is None:
            return _text(_ai_unavailable_response() if _submit_ai_proposal is None else _ai_rejected_response())
        try:
            result = _submit_ai_proposal(
                proposal,
                provider_id=str(arguments.get("provider_id") or "mcp")[:80],
                model=str(arguments.get("model") or "")[:120],
                context_hash=str(arguments.get("context_hash") or "")[:128],
            )
            return _text(_ai_proposal_only_response(result))
        except Exception:
            return _text(_ai_rejected_response())

    if name == "get_ai_proposals":
        if _get_ai_proposals is None:
            return _text(_ai_unavailable_response())
        try:
            limit = max(1, min(100, int(arguments.get("limit") or 50)))
            return _text(_ai_proposal_only_response(_get_ai_proposals(limit=limit)))
        except (TypeError, ValueError):
            return _text(_ai_rejected_response())
        except Exception:
            return _text(_ai_rejected_response())

    if name == "get_qianchuan_adjustments":
        return _text({"recommendations": _cached("plan_recs", build_plan_recommendations), "mode": "proposal_only", "execution_enabled": False})

    if name == "get_qianchuan_automation_readiness":
        return _text(build_automation_readiness())

    if name == "get_qianchuan_execution_preflight":
        return _text(build_execution_preflight_report())

    if name == "get_qianchuan_action_audit":
        return _text(get_action_audit(int(arguments.get("limit") or 100)))

    if name == "get_qianchuan_shadow_execution":
        return _text(build_shadow_execution_report())

    if name == "get_qianchuan_creative_analysis":
        return _text(_cached("creative_analysis", build_qianchuan_creative_analysis))

    if name == "get_qianchuan_accounts":
        return _text({"accounts": list_qianchuan_accounts(), "selected_account_key": load_agent_settings().get("qianchuan_account_key", "")})

    if name == "select_qianchuan_account":
        key = str(arguments.get("account_key") or "")
        if key not in {str(item.get("key")) for item in list_qianchuan_accounts()}:
            return _text({"error": "未知千川账号，请先在该账号页面同步一次"})
        return _text({"settings": save_agent_settings({"qianchuan_account_key": key})})

    if name == "get_inventory_alerts":
        return _text({"alerts": _cached("inv_alerts", build_inventory_alerts), "mode": "read_only"})

    if name == "get_action_center":
        return _text(build_action_center())

    if name == "get_shelf_analysis":
        return _text(build_shelf_analysis())

    if name == "get_live_dashboard_analysis":
        return _text(build_live_analysis())

    if name == "get_ops_manager":
        return _text(_cached("ops_manager", build_ops_manager))

    if name == "update_operation_task":
        result = update_task_state(str(arguments.get("task_id") or ""), str(arguments.get("status") or ""))
        _invalidate_cache()
        return _text(result)

    if name == "get_auto_scan_status":
        return _text(load_scan_status())

    if name == "get_scan_receipt":
        return _text(build_scan_receipt())

    if name == "get_operation_trends":
        return _text(build_trends(int(arguments.get("days") or 7), str(arguments.get("source") or "") or None, str(arguments.get("page_type") or "") or None))

    if name == "get_daily_report":
        return _text(load_latest_report() or {"error": "尚未生成日报"})

    if name == "generate_daily_report":
        return _text(generate_daily_report())

    if name == "get_agent_settings":
        return _text(load_agent_settings())

    if name == "get_health_monitor":
        return _text(check_selector_health())

    if name == "get_effectiveness_report":
        return _text(get_effectiveness_report())

    if name == "get_feedback_stats":
        return _text(get_feedback_stats())

    if name == "export_tasks":
        fmt = str(arguments.get("format") or "clipboard")
        return _text(export_tasks(fmt))

    return _text({"error": f"未知工具: {name}"})


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
