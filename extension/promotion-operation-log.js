(function initPromotionOperationLog(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.DianPromotionOperationLog = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function promotionOperationLogFactory() {
  "use strict";

  const OPERATION_LABELS = Object.freeze({
    adjust_budget: "调整预算",
    restore_budget: "恢复预算",
    pause_plan: "暂停计划",
    adjust_bid: "调整出价",
    set_schedule: "定时启停",
    schedule_plan: "定时启停",
  });
  const STATE_LABELS = Object.freeze({
    draft: "待复核",
    confirmed: "已确认待执行",
    executing: "执行结果待确认 · 禁止重复提交",
    cancelled: "已撤销",
    succeeded: "已执行待回读",
    verified: "回读已验证",
    failed: "执行失败",
    expired: "已过期",
  });
  const STATE_TONES = Object.freeze({
    confirmed: "warning",
    executing: "danger",
    cancelled: "muted",
    succeeded: "warning",
    verified: "safe",
    failed: "danger",
    expired: "muted",
  });

  function text(value) {
    return String(value ?? "").trim();
  }

  function isCsvFormulaIgnorable(codePoint) {
    return codePoint <= 0x20
      || (codePoint >= 0x7f && codePoint <= 0xa0)
      || codePoint === 0xad
      || codePoint === 0x34f
      || codePoint === 0x61c
      || codePoint === 0x1680
      || (codePoint >= 0x115f && codePoint <= 0x1160)
      || (codePoint >= 0x17b4 && codePoint <= 0x17b5)
      || (codePoint >= 0x180b && codePoint <= 0x180f)
      || (codePoint >= 0x2000 && codePoint <= 0x200f)
      || (codePoint >= 0x2028 && codePoint <= 0x202f)
      || codePoint === 0x205f
      || (codePoint >= 0x2060 && codePoint <= 0x206f)
      || codePoint === 0x3000
      || codePoint === 0x3164
      || (codePoint >= 0xfe00 && codePoint <= 0xfe0f)
      || codePoint === 0xfeff
      || codePoint === 0xffa0
      || (codePoint >= 0x1bca0 && codePoint <= 0x1bca3)
      || (codePoint >= 0x1d173 && codePoint <= 0x1d17a)
      || (codePoint >= 0xe0000 && codePoint <= 0xe0fff);
  }

  function csvFormulaProbe(value) {
    const normalized = String(value || "").normalize("NFKC");
    let offset = 0;
    while (offset < normalized.length) {
      const codePoint = normalized.codePointAt(offset);
      if (!isCsvFormulaIgnorable(codePoint)) break;
      offset += codePoint > 0xffff ? 2 : 1;
    }
    return normalized.slice(offset);
  }

  function timeValue(value) {
    const number = Number(value);
    return Number.isFinite(number) && number > 0 ? number : 0;
  }

  function displayValue(value) {
    if (value === null || value === undefined || text(value) === "") return "--";
    return typeof value === "number" ? Number(value.toFixed(2)).toLocaleString("zh-CN") : text(value);
  }

  function normalizeAction(actionValue = {}) {
    const action = actionValue && typeof actionValue === "object" ? actionValue : {};
    const target = action.target_ref && typeof action.target_ref === "object" ? action.target_ref : {};
    const change = action.change && typeof action.change === "object" ? action.change : {};
    const evidence = action.evidence_ref && typeof action.evidence_ref === "object" ? action.evidence_ref : {};
    const operationType = text(action.operation_type) || "unknown";
    const state = text(action.state) || "draft";
    const timestamp = timeValue(action.state_updated_at_ms || action.created_at_ms);
    const targetName = text(target.name) || text(target.id) || "目标待识别";
    const accountLabel = text(target.account_label) || text(target.account_key) || "账户待识别";
    const field = text(change.field) || "目标值";
    const currentValue = displayValue(change.current_value);
    const targetValue = displayValue(change.target_value);
    return {
      raw: action,
      action_id: text(action.action_id),
      operation_type: operationType,
      operation_label: text(action.operation_label) || OPERATION_LABELS[operationType] || "其他动作",
      state,
      state_label: STATE_LABELS[state] || state || "待复核",
      state_tone: STATE_TONES[state] || "info",
      timestamp,
      target_name: targetName,
      target_id: text(target.id),
      account_label: accountLabel,
      account_key: text(target.account_key),
      field,
      current_value: currentValue,
      target_value: targetValue,
      change_label: `${field}：${currentValue} → ${targetValue}`,
      evidence_label: [text(evidence.source), text(evidence.page_type), evidence.quality_score === undefined ? "" : `质量 ${evidence.quality_score}`].filter(Boolean).join(" · ") || "证据待补齐",
      query_text: `${action.action_id || ""} ${operationType} ${action.operation_label || ""} ${state} ${targetName} ${target.id || ""} ${accountLabel} ${field}`.toLowerCase(),
    };
  }

  function normalizeFilters(filters = {}) {
    return {
      operation_type: text(filters.operation_type) || "all",
      state: text(filters.state) || "all",
      query: text(filters.query).toLowerCase(),
      date_from: text(filters.date_from),
      date_to: text(filters.date_to),
    };
  }

  function dateBoundary(value, end = false) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(text(value))) return 0;
    const parsed = new Date(`${value}T00:00:00`).getTime();
    return Number.isFinite(parsed) ? parsed + (end ? 86_400_000 - 1 : 0) : 0;
  }

  function deriveView(payload = {}, filters = {}) {
    const normalizedFilters = normalizeFilters(filters);
    const allRows = (Array.isArray(payload.actions) ? payload.actions : []).map(normalizeAction).sort((a, b) => b.timestamp - a.timestamp);
    const from = dateBoundary(normalizedFilters.date_from);
    const to = dateBoundary(normalizedFilters.date_to, true);
    const rows = allRows.filter((row) => {
      if (normalizedFilters.operation_type !== "all" && row.operation_type !== normalizedFilters.operation_type) return false;
      if (normalizedFilters.state !== "all" && row.state !== normalizedFilters.state) return false;
      if (normalizedFilters.query && !row.query_text.includes(normalizedFilters.query)) return false;
      if (from && row.timestamp < from) return false;
      if (to && row.timestamp > to) return false;
      return true;
    });
    return {
      safe: payload.execution_enabled !== true,
      rows,
      all_rows: allRows,
      filters: normalizedFilters,
      updated_at: text(payload.updated_at),
      summary: {
        total: allRows.length,
        visible: rows.length,
        confirmed: allRows.filter((row) => row.state === "confirmed").length,
        executed: allRows.filter((row) => ["succeeded", "verified"].includes(row.state)).length,
        cancelled: allRows.filter((row) => row.state === "cancelled").length,
      },
      platform_write_enabled: false,
      notice: "日志只记录本机动作生命周期；没有回读证据的动作不会显示为已验证。",
    };
  }

  function csvCell(value) {
    const string = text(value);
    // Spreadsheet applications may execute imported cells beginning with one
    // of these characters. Inspect a compatibility-normalized copy so hidden
    // controls, zero-width markers and full-width operators cannot bypass the
    // guard, while preserving the original audit value in the exported file.
    const inert = /^[=+\-@]/.test(csvFormulaProbe(string)) ? `'${string}` : string;
    // Quote every dynamic cell. Besides preserving commas/newlines, this keeps
    // attacker-controlled separators inside one cell when Excel is configured
    // to use a regional delimiter such as semicolon instead of comma.
    return `"${inert.replace(/"/g, '""')}"`;
  }

  function toCsv(rowsValue = []) {
    const rows = Array.isArray(rowsValue) ? rowsValue : [];
    const header = ["时间", "动作", "账户", "计划", "变更", "状态", "证据", "动作编号"];
    const lines = rows.map((row) => [
      row.timestamp ? new Date(row.timestamp).toLocaleString("zh-CN", { hour12: false }) : "",
      row.operation_label,
      row.account_label,
      row.target_name,
      row.change_label,
      row.state_label,
      row.evidence_label,
      row.action_id,
    ].map(csvCell).join(","));
    return `\uFEFF${[header.join(","), ...lines].join("\r\n")}`;
  }

  return { OPERATION_LABELS, STATE_LABELS, normalizeAction, normalizeFilters, deriveView, toCsv };
});
