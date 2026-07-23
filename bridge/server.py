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

from http_receiver import (
    STALE_SECONDS,
    build_action_center,
    build_insights,
    build_inventory_alerts,
    build_live_analysis,
    build_ops_manager,
    build_plan_recommendations,
    build_qianchuan_creative_analysis,
    build_shelf_analysis,
    build_trends,
    generate_daily_report,
    list_snapshots,
    load_agent_settings,
    load_data,
    load_latest_report,
    load_scan_status,
    update_task_state,
)

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
        name="get_qianchuan_adjustments",
        description="按计划明细生成千川预算、止损、素材优化和谨慎放量建议；只返回建议，不执行投放变更",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_qianchuan_creative_analysis",
        description="分析巨量千川视频库素材的消耗、ROI、成交、素材评估和测试覆盖，输出直播引流素材分层与优化建议",
        inputSchema={"type": "object", "properties": {}, "required": []},
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
        description="以运营总管视角汇总货架、直播、千川和库存，输出今日优先任务、负责人和验收标准",
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
]


def _text(value: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(value, ensure_ascii=False, indent=2))]


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
                "mode": "read_only",
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

    if name == "get_qianchuan_adjustments":
        return _text({"recommendations": build_plan_recommendations(), "mode": "read_only"})

    if name == "get_qianchuan_creative_analysis":
        return _text(build_qianchuan_creative_analysis())

    if name == "get_inventory_alerts":
        return _text({"alerts": build_inventory_alerts(), "mode": "read_only"})

    if name == "get_action_center":
        return _text(build_action_center())

    if name == "get_shelf_analysis":
        return _text(build_shelf_analysis())

    if name == "get_live_dashboard_analysis":
        return _text(build_live_analysis())

    if name == "get_ops_manager":
        return _text(build_ops_manager())

    if name == "update_operation_task":
        return _text(update_task_state(str(arguments.get("task_id") or ""), str(arguments.get("status") or "")))

    if name == "get_auto_scan_status":
        return _text(load_scan_status())

    if name == "get_operation_trends":
        return _text(build_trends(int(arguments.get("days") or 7), str(arguments.get("source") or "") or None, str(arguments.get("page_type") or "") or None))

    if name == "get_daily_report":
        return _text(load_latest_report() or {"error": "尚未生成日报"})

    if name == "generate_daily_report":
        return _text(generate_daily_report())

    if name == "get_agent_settings":
        return _text(load_agent_settings())

    return _text({"error": f"未知工具: {name}"})


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
