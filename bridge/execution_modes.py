"""Canonical account execution-mode labels and helpers."""

from __future__ import annotations

from typing import Any

EXECUTION_MODES = ("observe", "shadow", "supervised")

EXECUTION_MODE_LABELS = {
    "observe": "观察模式",
    "shadow": "影子模式",
    "supervised": "受监督执行",
}

EXECUTION_MODE_HINTS = {
    "observe": "只给建议，不启动页面执行",
    "shadow": "人工在千川改完后回插件核验",
    "supervised": "逐次口令授权后由插件提交单计划动作",
}


def normalize_execution_mode(value: Any, default: str = "observe") -> str:
    mode = str(value or default).strip().lower()
    return mode if mode in EXECUTION_MODES else default


def execution_mode_label(value: Any) -> str:
    mode = normalize_execution_mode(value)
    return EXECUTION_MODE_LABELS.get(mode, "未知模式")


def execution_mode_hint(value: Any) -> str:
    mode = normalize_execution_mode(value)
    return EXECUTION_MODE_HINTS.get(mode, "")
