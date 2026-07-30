"""Daily report rendering, persistence, and scheduler."""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from insights import build_action_center, build_insights, build_ops_manager, build_scan_receipt
from storage import _now_label, list_snapshots
import state

logger = logging.getLogger("dian-agent-http")


def _data_dir() -> Path:
    return state.DATA_DIR

REPORT_TEMPLATE_KEYS = {"default", "brief", "handover", "custom"}
DEFAULT_CUSTOM_REPORT_TEMPLATE = """# 店策 Agent 经营日志 - {{date}}

## 今日结论
{{headline}}
{{summary}}

## 今日重点
{{top_tasks}}

## 千川计划
{{plans}}

## 库存风险
{{inventory}}

## 数据状态
{{scan_status}}
"""

def _facade():
    import http_receiver as facade
    return facade


def load_agent_settings(*args, **kwargs):
    return _facade().load_agent_settings(*args, **kwargs)


def load_scan_status(*args, **kwargs):
    return _facade().load_scan_status(*args, **kwargs)


def _load_integration_secrets(*args, **kwargs):
    return _facade()._load_integration_secrets(*args, **kwargs)


def send_report_notifications(*args, **kwargs):
    return _facade().send_report_notifications(*args, **kwargs)


def _atomic_text_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _reports_dir() -> Path:
    return _data_dir() / "reports"


def _report_list(items: list[str], empty: str) -> str:
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, 1)) if items else f"- {empty}"


def _render_selected_report(
    template_key: str,
    custom_template: str,
    report_date: str,
    insights: dict[str, Any],
    action_center: dict[str, Any],
    ops: dict[str, Any],
    scan: dict[str, Any],
    scan_receipt: dict[str, Any],
) -> list[str] | None:
    if template_key == "default":
        return None
    top_tasks = [
        f"[{item['owner']}] {item['title']}：{item['action']}（验收：{item['acceptance']}）"
        for item in ops.get("today_top_actions", [])[:8]
    ]
    plans = [
        f"{item['plan']}：{item['suggestion']}（{item['reason']}）"
        for item in action_center.get("plan_recommendations", [])[:8]
    ]
    inventory = [
        f"{item['product']}：{item['title']}；{item['suggestion']}"
        for item in action_center.get("inventory_alerts", [])[:8]
    ]
    alerts = [
        f"{item['title']}：{item.get('action') or item.get('detail') or ''}"
        for item in insights.get("alerts", [])[:6]
    ]
    scan_status = (
        f"巡检 {scan.get('status', 'idle')}，成功 {scan.get('success', 0)} 页，"
        f"失败 {scan.get('failed', 0)} 页；体检覆盖率 {scan_receipt['summary']['coverage_rate']}%，"
        f"需复核 {scan_receipt['summary']['needs_review']} 页。"
    )
    context = {
        "date": report_date,
        "generated_at": _now_label(),
        "headline": str(insights.get("headline") or "暂无结论"),
        "summary": str(insights.get("summary") or "暂无摘要"),
        "top_tasks": _report_list(top_tasks, "暂无待办任务。"),
        "plans": _report_list(plans, "暂无千川调整建议。"),
        "inventory": _report_list(inventory, "暂无库存预警。"),
        "alerts": _report_list(alerts, "暂无其他异常。"),
        "scan_status": scan_status,
    }
    if template_key == "brief":
        return [
            f"# 店策 Agent 老板简报 - {report_date}",
            "",
            f"> 生成时间：{context['generated_at']}｜模式：只读建议",
            "",
            "## 一句话结论",
            "",
            f"- {context['headline']}",
            f"- {context['summary']}",
            "",
            "## 今天先做",
            "",
            context["top_tasks"],
            "",
            "## 需要关注",
            "",
            context["alerts"],
            "",
            "## 数据状态",
            "",
            f"- {scan_status}",
            "",
        ]
    if template_key == "handover":
        return [
            f"# 店策 Agent 运营交接日志 - {report_date}",
            "",
            f"> 交接生成时间：{context['generated_at']}｜所有执行动作需人工确认",
            "",
            "## 本班结论",
            "",
            f"- {context['headline']}",
            f"- {context['summary']}",
            "",
            "## 下一班优先事项",
            "",
            context["top_tasks"],
            "",
            "## 千川待处理",
            "",
            context["plans"],
            "",
            "## 库存待处理",
            "",
            context["inventory"],
            "",
            "## 数据交接",
            "",
            f"- {scan_status}",
            "",
        ]
    template = custom_template or DEFAULT_CUSTOM_REPORT_TEMPLATE
    for key, value in context.items():
        template = template.replace(f"{{{{{key}}}}}", str(value))
    return template.splitlines()


def generate_daily_report(report_date: str | None = None) -> dict[str, Any]:
    report_date = report_date or time.strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
        raise ValueError("report_date must be YYYY-MM-DD")
    insights = build_insights()
    action_center = build_action_center()
    ops = build_ops_manager()
    scan = load_scan_status()
    scan_receipt = build_scan_receipt()
    catalog = list_snapshots()
    report_path = _reports_dir() / f"{report_date}.md"

    # Build staleness map: source/page_type -> saved_at and age
    freshness_map: dict[str, dict[str, Any]] = {}
    for item in catalog:
        freshness_map[f"{item['source']}/{item['page_type']}"] = {
            "saved_at": item.get("saved_at", ""),
            "age_seconds": item.get("age_seconds", 0),
            "fresh": item.get("fresh", False),
        }

    # Identify stale data sources (>1 hour)
    stale_sources = [
        f"{key} (更新于 {info['saved_at']})"
        for key, info in freshness_map.items()
        if info["age_seconds"] > 3600
    ]

    lines = [
        f"# 店策 Agent 每日经营报告 - {report_date}",
        "",
        f"> 生成时间：{_now_label()}｜模式：只读建议",
        "",
    ]

    # Staleness warning section
    if stale_sources:
        lines.extend([
            "⚠️ **数据时效提醒**：以下数据源超过 1 小时未更新，建议重新同步后再做决策：",
            "",
        ])
        for src in stale_sources[:5]:
            lines.append(f"- {src}")
        lines.append("")

    lines.extend([
        "## 今日结论",
        "",
        f"- {insights['headline']}",
        f"- {insights['summary']}",
        f"- 已同步页面：{len(insights['coverage'])}；千川调整项：{len(action_center['plan_recommendations'])}；库存预警：{len(action_center['inventory_alerts'])}",
        f"- 自动巡检：{scan.get('status', 'idle')}；成功 {scan.get('success', 0)} 页，失败 {scan.get('failed', 0)} 页，低质量 {scan.get('low_quality', 0)} 页。",
        f"- 数据体检：{scan_receipt['readiness_label']}；覆盖率 {scan_receipt['summary']['coverage_rate']}%；需复核 {scan_receipt['summary']['needs_review']} 页。",
        "",
        "## 运营总管今日任务",
        "",
    ])
    for index, item in enumerate(ops["today_top_actions"][:8], 1):
        source_key = item.get("source", "")
        data_time = freshness_map.get(source_key, {}).get("saved_at", "未知")
        lines.extend([f"{index}. **[{item['owner']}] {item['title']}**：{item['action']}", f"   - 依据：{item['evidence']}｜验收：{item['acceptance']}｜数据时间：{data_time}"])
    lines.extend(["", "## 货架运营", ""])
    for item in action_center["shelf_analysis"]["recommendations"]:
        lines.append(f"- **{item['title']}**：{item['action']}（{item['evidence']}）")
    if not action_center["shelf_analysis"]["recommendations"]:
        lines.append("- 暂无货架专项建议。")
    lines.extend(["", "## 直播与内容运营", ""])
    for item in action_center["live_analysis"]["recommendations"]:
        lines.append(f"- **{item['title']}**：{item['action']}（{item['evidence']}）")
    if not action_center["live_analysis"]["recommendations"]:
        lines.append("- 暂无直播专项建议。")
    lines.extend([
        "",
        "## 千川计划调整建议",
        "",
    ])
    plans = action_center["plan_recommendations"]
    if plans:
        qianchuan_time = freshness_map.get("qianchuan/report", freshness_map.get("qianchuan/campaigns", {})).get("saved_at", "未知")
        for index, item in enumerate(plans[:10], 1):
            lines.extend([f"{index}. **{item['plan']}**：{item['suggestion']}", f"   - 依据：{item['reason']}｜数据时间：{qianchuan_time}"])
    else:
        lines.append("- 暂无可执行建议；请同步千川计划列表和报表页面。")
    lines.extend(["", "## 千川视频库与直播引流素材", ""])
    creative = action_center["creative_analysis"]
    summary = creative["summary"]
    lines.append(f"- 视频 {summary['total_videos']} 条；在投/有消耗 {summary['spending_videos']} 条；未测试 {summary['untested_videos']} 条；高风险 {summary['risky_videos']} 条；高潜 {summary['high_potential_videos']} 条。")
    for item in creative["recommendations"][:8]:
        lines.append(f"- **{item['title']}**：{item['action']}（{item['evidence']}）")
    lines.extend(["", "## 库存预警", ""])
    inventory = action_center["inventory_alerts"]
    if inventory:
        doudian_time = freshness_map.get("doudian/products", {}).get("saved_at", "未知")
        for index, item in enumerate(inventory[:15], 1):
            lines.append(f"{index}. **{item['product']}**：{item['title']}；{item['suggestion']}（数据时间：{doudian_time}）")
    else:
        lines.append("- 暂无库存预警，或尚未同步商品/库存页面。")
    lines.extend(["", "## 其他优先事项", ""])
    for index, item in enumerate(insights["alerts"][:8], 1):
        confidence_tag = f"[{item['confidence']}]" if item.get("confidence") else ""
        lines.append(f"{index}. **{item['title']}** {confidence_tag}：{item.get('action') or item.get('detail') or ''}")
    lines.extend(
        [
            "",
            "## 安全边界",
            "",
            "- 本报告来自已登录网页的本地脱敏快照，不等同于官方 API 数据。",
            "- 所有预算、启停和店铺变更建议必须在后台核对统计周期与归因口径后人工确认。",
            "",
        ]
    )
    template_key = str(action_center["settings"].get("report_template") or "default")
    selected_lines = _render_selected_report(
        template_key,
        str(action_center["settings"].get("custom_report_template") or ""),
        report_date,
        insights,
        action_center,
        ops,
        scan,
        scan_receipt,
    )
    if selected_lines is not None:
        lines = selected_lines
    _atomic_text_write(report_path, "\n".join(lines))
    _cleanup_old_reports(int(action_center["settings"]["report_retention_days"]))
    return {
        "date": report_date,
        "generated_at": _now_label(),
        "path": str(report_path),
        "headline": insights["headline"],
        "summary": action_center["summary"],
        "template": template_key,
        "stale_sources": stale_sources[:5],
        "content": "\n".join(lines),
    }


def _cleanup_old_reports(retention_days: int) -> None:
    cutoff = time.time() - retention_days * 86400
    reports_dir = _reports_dir()
    if not reports_dir.exists():
        return
    for path in reports_dir.glob("*.md"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            logger.exception("清理旧日报失败: %s", path)


def load_latest_report() -> dict[str, Any] | None:
    reports_dir = _reports_dir()
    if not reports_dir.exists():
        return None
    paths = sorted(reports_dir.glob("*.md"), reverse=True)
    if not paths:
        return None
    path = paths[0]
    try:
        return {"date": path.stem, "path": str(path), "content": path.read_text(encoding="utf-8")}
    except OSError:
        logger.exception("读取日报失败: %s", path)
        return None


def _daily_report_scheduler(stop_event: threading.Event) -> None:
    while not stop_event.wait(30):
        try:
            settings = load_agent_settings()
            if not settings["daily_report_enabled"]:
                continue
            now = datetime.now()
            if now.strftime("%H:%M") < settings["daily_report_time"]:
                continue
            target = _reports_dir() / f"{now:%Y-%m-%d}.md"
            if not target.exists():
                report = generate_daily_report(now.strftime("%Y-%m-%d"))
                if _load_integration_secrets().get("auto_send_reports"):
                    send_report_notifications(report)
                logger.info("已生成每日经营报告: %s", target)
        except Exception:
            logger.exception("生成定时日报失败")


