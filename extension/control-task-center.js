(function initControlTaskCenter(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.DianControlTaskCenter = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createControlTaskCenter() {
  "use strict";

  const FAMILY_DEFINITIONS = Object.freeze({
    status: Object.freeze({
      id: "status", label: "启停状态", short_label: "状态",
      description: "启用、暂停、结束分别复核；结束不会等同于删除。",
      contract_note: "本机可演练；正式状态写合同尚未对当前账户验真。",
    }),
    budget: Object.freeze({
      id: "budget", label: "任务预算", short_label: "预算",
      description: "预算升降单独建任务；首发生产边界只允许单任务降预算。",
      contract_note: "已记录公开合同，仍需账户权限与真实回读验收。",
    }),
    duration: Object.freeze({
      id: "duration", label: "投放时长", short_label: "时长",
      description: "延长或缩短投放窗口，不能与启停或预算在同一任务中提交。",
      contract_note: "仅本机演练；尚无经过验真的正式时长写合同。",
    }),
  });

  const STATE_LABELS = Object.freeze({
    review_required: "待人工复核",
    approved: "已批准，待模拟",
    awaiting_readback: "等待模拟回读",
    verified: "模拟回读通过",
    rejected: "人工已拒绝",
    blocked: "已阻止",
    readback_failed: "回读不一致",
  });

  const OPERATION_LABELS = Object.freeze({
    ENABLE: "启用",
    PAUSE: "暂停",
    CLOSE: "结束",
    DECREASE: "降低预算",
    INCREASE: "提高预算",
    SHORTEN: "缩短时长",
    EXTEND: "延长时长",
  });

  function finiteNumber(value) {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
    const result = Number(value);
    return Number.isFinite(result) ? result : null;
  }

  function normalizeFamily(value) {
    const family = String(value || "").toLowerCase();
    return Object.hasOwn(FAMILY_DEFINITIONS, family) ? family : "budget";
  }

  function familyValue(family, value) {
    if (value === null || value === undefined || value === "") return "待确认";
    if (family === "budget") {
      const number = finiteNumber(value);
      return number === null ? "待确认" : `¥${number.toFixed(2)}`;
    }
    if (family === "duration") {
      const number = finiteNumber(value);
      return number === null ? "待确认" : `${Math.trunc(number)} 分钟`;
    }
    const labels = {
      ACTIVE: "投放中", PAUSED: "已暂停", CLOSED: "已结束",
      OFFLINE_BALANCE: "余额不足", OFFLINE_BUDGET: "预算耗尽",
      OFFLINE_TIME: "超出投放时间", ROI2_DISABLE: "平台止投", FROZEN: "已冻结",
    };
    return labels[String(value || "")] || String(value || "待确认");
  }

  function taskActions(task = {}) {
    const state = String(task.state || "blocked");
    if (state === "review_required") return [
      { id: "accept", label: "批准本机演练", primary: true },
      { id: "reject", label: "拒绝", primary: false },
    ];
    if (state === "approved") return [{ id: "simulate", label: "运行本机模拟", primary: true }];
    if (state === "awaiting_readback") return [{ id: "readback", label: "核对模拟结果", primary: true }];
    return [];
  }

  function normalizeTask(task = {}) {
    const family = normalizeFamily(task.family);
    const state = String(task.state || "blocked");
    const blockers = Array.isArray(task.simulation_blockers) ? task.simulation_blockers.map(String) : [];
    const productionBlockers = Array.isArray(task.production_blockers) ? task.production_blockers.map(String) : [];
    return {
      task_id: String(task.task_id || ""),
      task_label: String(task.task_label || `${FAMILY_DEFINITIONS[family].label}任务`),
      family,
      family_label: FAMILY_DEFINITIONS[family].label,
      operation: String(task.operation || ""),
      operation_label: OPERATION_LABELS[String(task.operation || "")] || "待复核动作",
      state,
      state_label: STATE_LABELS[state] || "未知状态",
      tone: ["verified"].includes(state) ? "safe" : ["rejected", "blocked", "readback_failed"].includes(state) ? "danger" : "warning",
      current_label: familyValue(family, task.current_value),
      target_label: familyValue(family, task.target_value),
      reason_codes: Array.isArray(task.reason_codes) ? task.reason_codes.map(String).slice(0, 6) : [],
      blockers,
      production_blockers: productionBlockers,
      single_variable_only: task.single_variable_only === true && Object.keys(task.change || {}).length === 1,
      platform_write_attempted: task.platform_write_attempted === true,
      actions: taskActions(task),
      updated_at: finiteNumber(task.updated_at) || finiteNumber(task.created_at),
    };
  }

  function deriveView(summaryValue = {}, selectedFamilyValue = "budget") {
    const summary = summaryValue && typeof summaryValue === "object" ? summaryValue : {};
    const selectedFamily = normalizeFamily(selectedFamilyValue);
    const unsafeWriteClaim = summary.platform_write_enabled === true || summary.automatic_platform_submit === true;
    const reportedFamilies = Array.isArray(summary.families) ? summary.families : [];
    const tasks = (Array.isArray(summary.tasks) ? summary.tasks : []).map(normalizeTask);
    const families = Object.values(FAMILY_DEFINITIONS).map((definition) => {
      const reported = reportedFamilies.find((item) => item?.id === definition.id) || {};
      const familyTasks = tasks.filter((item) => item.family === definition.id);
      return {
        ...definition,
        selected: definition.id === selectedFamily,
        task_count: Math.max(0, finiteNumber(reported.task_count) || familyTasks.length),
        pending_count: Math.max(0, finiteNumber(reported.pending_count) || familyTasks.filter((item) => !["verified", "rejected", "blocked", "readback_failed"].includes(item.state)).length),
        verified_count: Math.max(0, finiteNumber(reported.verified_count) || familyTasks.filter((item) => item.state === "verified").length),
        production_available: false,
        simulation_available: reported.simulation_available !== false,
        contract_status: String(reported.contract_status || "not_verified"),
      };
    });
    const importableCandidates = Array.isArray(summary.importable_candidates)
      ? summary.importable_candidates.filter((item) => item && typeof item === "object")
      : [];
    const visibleTasks = tasks.filter((item) => item.family === selectedFamily);
    const selectedDefinition = FAMILY_DEFINITIONS[selectedFamily];
    const importCandidate = selectedFamily === "budget" ? importableCandidates[0] || null : null;
    let primaryAction = {
      id: "sync",
      label: "同步控制任务数据",
      detail: selectedFamily === "status"
        ? "读取任务当前状态后才能生成启停演练。"
        : selectedFamily === "duration"
          ? "读取任务当前投放窗口后才能生成时长演练。"
          : "先由 A1 影子判断生成降预算候选。",
      enabled: true,
    };
    if (["status", "duration"].includes(selectedFamily) && summary.scope_bound === true) {
      primaryAction = {
        id: "open_builder",
        label: `创建${selectedDefinition.short_label}演练`,
        detail: `使用当前已绑定计划作用域填写${selectedDefinition.label}的新旧值；只保存本机草稿。`,
        enabled: true,
      };
    }
    if (importCandidate) {
      primaryAction = {
        id: "import_candidate",
        label: "导入降预算候选",
        detail: `${familyValue("budget", importCandidate.current_value)} → ${familyValue("budget", importCandidate.target_value)}，仅生成本机任务。`,
        enabled: true,
        candidate_id: String(importCandidate.candidate_id || ""),
      };
    }
    const unsafeTasks = tasks.filter((item) => item.platform_write_attempted || !item.single_variable_only);
    return {
      schema_version: 1,
      selected_family: selectedFamily,
      selected_definition: selectedDefinition,
      families,
      tasks: visibleTasks,
      all_task_count: tasks.length,
      visible_task_count: visibleTasks.length,
      pending_count: tasks.filter((item) => !["verified", "rejected", "blocked", "readback_failed"].includes(item.state)).length,
      verified_count: tasks.filter((item) => item.state === "verified").length,
      importable_candidates: importableCandidates,
      scope_bound: summary.scope_bound === true,
      manual_rehearsal_available: summary.scope_bound === true && ["status", "duration"].includes(selectedFamily),
      primary_action: primaryAction,
      safe: !unsafeWriteClaim && !unsafeTasks.length,
      unsafe_write_claim: unsafeWriteClaim,
      unsafe_task_count: unsafeTasks.length,
      platform_write_enabled: false,
      automatic_platform_submit: false,
      notice: unsafeWriteClaim || unsafeTasks.length
        ? "检测到不安全的写入或多变量标记，控制任务中心已停止继续操作。"
        : String(summary.notice || "三类动作分开复核、模拟和回读；当前不会修改千川。"),
    };
  }

  return {
    FAMILY_DEFINITIONS,
    OPERATION_LABELS,
    STATE_LABELS,
    deriveView,
    familyValue,
    normalizeFamily,
    normalizeTask,
    taskActions,
  };
});
