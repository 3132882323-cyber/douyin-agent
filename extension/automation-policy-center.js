(function initAutomationPolicyCenter(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.DianAutomationPolicyCenter = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createAutomationPolicyCenter() {
  "use strict";

  const POLICY_DEFINITIONS = Object.freeze([
    Object.freeze({
      id: "schedule", label: "定时启停", stage: 1,
      description: "按每日时段生成启用与暂停状态任务。",
      evidence: "计划作用域、明确时区、无冲突时间段",
    }),
    Object.freeze({
      id: "auto_stop", label: "自动止损", stage: 2,
      description: "达到样本门槛且低于本店止损线时生成暂停候选。",
      evidence: "保本 ROI、最低消耗、订单样本、单日亏损线",
    }),
    Object.freeze({
      id: "auto_budget", label: "自动补预算", stage: 3,
      description: "ROI 达标且接近预算上限时生成受控补预算候选。",
      evidence: "7 天影子验证、预算余量、库存与履约、结果回读",
    }),
    Object.freeze({
      id: "chasing_restart", label: "追投复检", stage: 4,
      description: "低质追投先暂停，冷却复检后再决定恢复或重建。",
      evidence: "任务类型、关停原因、冷却窗口、复检 ROI",
    }),
    Object.freeze({
      id: "smart_boost", label: "起量流控", stage: 5,
      description: "运行/等待窗口交替，预算、时长与止损仍保持独立动作。",
      evidence: "生产写合同、逐项回读、硬止损与紧急停止",
    }),
  ]);

  const STATE_LABELS = Object.freeze({
    review_required: "待人工复核",
    approved: "待本机模拟",
    awaiting_readback: "等待模拟回读",
    verified: "本机时段演练已启用",
    rejected: "人工已拒绝",
    blocked: "配置已阻止",
    readback_failed: "回读失败",
  });

  function finiteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function derivePolicyMatrix(input = {}) {
    const schedule = input.schedule && typeof input.schedule === "object" ? input.schedule : {};
    const runtime = input.runtime && typeof input.runtime === "object" ? input.runtime : {};
    const shadow = runtime.shadow_evidence && typeof runtime.shadow_evidence === "object" ? runtime.shadow_evidence : {};
    const profileReady = runtime.profile_validation?.business_profile_ready === true;
    const scheduleVerified = schedule.active?.state === "verified";
    const shadowReady = finiteNumber(shadow.validated_days) >= 7
      && finiteNumber(shadow.useful_rate) >= 0.7
      && finiteNumber(shadow.readback_success_rate) >= 0.99
      && finiteNumber(shadow.critical_incidents) === 0;
    return POLICY_DEFINITIONS.map((definition) => {
      let state = "planned";
      let stateLabel = "待建设";
      let nextStep = "等待前序策略验收";
      let tone = "muted";
      if (definition.id === "schedule") {
        state = scheduleVerified ? "local_verified" : schedule.latest ? "in_progress" : schedule.scope_bound ? "available" : "blocked";
        stateLabel = scheduleVerified ? "本机已验证" : schedule.latest ? STATE_LABELS[schedule.latest.state] || "正在配置" : schedule.scope_bound ? "可开始" : "先绑定计划";
        nextStep = scheduleVerified ? "继续观察下一次时段事件" : schedule.latest ? "完成复核、模拟和回读" : "设置每日投放时段";
        tone = scheduleVerified ? "safe" : state === "blocked" ? "danger" : "warning";
      } else if (definition.id === "auto_stop") {
        state = profileReady ? "shadow_available" : "blocked";
        stateLabel = profileReady ? "可进入影子" : "缺经营边界";
        nextStep = profileReady ? "建立样本门槛与暂停候选，不自动提交" : "补齐利润、止损和证据口径";
        tone = profileReady ? "warning" : "danger";
      } else if (definition.id === "auto_budget") {
        state = shadowReady ? "design_ready" : "locked";
        stateLabel = shadowReady ? "可设计受控试验" : "等待 7 天影子";
        nextStep = shadowReady ? "只设计单计划补预算候选" : "先证明判断与回读稳定";
        tone = shadowReady ? "warning" : "muted";
      } else if (definition.id === "chasing_restart") {
        stateLabel = "等待止损闭环";
        nextStep = "先验证暂停、冷却与恢复规则";
      } else if (definition.id === "smart_boost") {
        stateLabel = "生产权限未开放";
        nextStep = "官方写合同与逐项回读验收后再建设";
      }
      return { ...definition, state, state_label: stateLabel, next_step: nextStep, tone };
    });
  }

  function normalizeSchedule(summaryValue = {}) {
    const summary = summaryValue && typeof summaryValue === "object" ? summaryValue : {};
    const latest = summary.latest && typeof summary.latest === "object" ? summary.latest : null;
    const active = summary.active && typeof summary.active === "object" ? summary.active : null;
    const previewEvents = Array.isArray(summary.next_events) && summary.next_events.length
      ? summary.next_events
      : Array.isArray(latest?.simulation_receipt?.events) ? latest.simulation_receipt.events : [];
    const nextEvents = previewEvents.filter((item) => item && typeof item === "object").slice(0, 8);
    const ranges = Array.isArray((active || latest)?.time_ranges) ? (active || latest).time_ranges : [];
    const state = String(latest?.state || (summary.scope_bound ? "empty" : "blocked"));
    const actions = state === "review_required"
      ? [{ id: "accept", label: "批准本机演练", primary: true }, { id: "reject", label: "拒绝", primary: false }]
      : state === "approved"
        ? [{ id: "simulate", label: "运行 48 小时模拟", primary: true }]
        : state === "awaiting_readback"
          ? [{ id: "readback", label: "核对模拟事件", primary: true }]
          : [];
    const unsafe = summary.platform_write_enabled === true
      || summary.automatic_platform_submit === true
      || latest?.platform_write_attempted === true;
    return {
      scope_bound: summary.scope_bound === true,
      state,
      state_label: STATE_LABELS[state] || (state === "empty" ? "尚未配置" : "等待绑定计划"),
      tone: unsafe || ["blocked", "readback_failed", "rejected"].includes(state) ? "danger" : active ? "safe" : "warning",
      latest,
      active,
      revision_id: String(latest?.revision_id || ""),
      revision: finiteNumber(latest?.revision) || 0,
      enabled: (active || latest)?.enabled === true,
      ranges,
      next_events: nextEvents,
      next_event: nextEvents[0] || null,
      actions,
      unsafe,
      platform_write_enabled: false,
      production_scheduler_enabled: false,
      notice: unsafe
        ? "检测到不安全的写入标记，定时启停已停止。"
        : String(summary.notice || "当前只生成本机时段演练，不会自动提交千川。"),
    };
  }

  return {
    POLICY_DEFINITIONS,
    STATE_LABELS,
    derivePolicyMatrix,
    normalizeSchedule,
  };
});
