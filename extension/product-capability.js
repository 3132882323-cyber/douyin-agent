(function initProductCapability(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.DianProductCapability = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function productCapabilityFactory() {
  "use strict";

  const LEVELS = Object.freeze({
    setup_required: "先完成准备",
    diagnosis_ready: "巡店诊断可用",
    plan_read_only: "投放只读可用",
    local_management: "本地计划管理可用",
    supervised_ready: "受控执行准备就绪",
    closed_loop: "结果回读已形成",
  });
  const STAGE_STATES = new Set(["ready", "attention", "blocked", "inactive"]);
  const READY_PREFLIGHT_STATES = new Set(["awaiting_reread", "ready_for_final_confirmation", "authorized"]);
  const BLOCKED_PREFLIGHT_STATES = new Set(["blocked", "expired", "stopped"]);
  const EVALUATED_EFFECT_STATES = new Set(["effective", "ineffective", "rollback_recommended"]);

  function text(value) {
    return String(value ?? "").trim();
  }

  function count(value) {
    const number = Number(value);
    return Number.isFinite(number) && number > 0 ? Math.floor(number) : 0;
  }

  function hasUsablePlanId(value) {
    const identifier = text(value);
    return Boolean(identifier) && !/^\[(?:已隐藏|标识无效|无数据|未知)\]$/.test(identifier);
  }

  function stage(id, label, status, summary, target, actionLabel, evidence = []) {
    return { id, label, status, summary, target, action_label: actionLabel, evidence };
  }

  function accountKey(value) {
    return text(value).toLowerCase();
  }

  function itemAccountKey(item = {}) {
    return accountKey(
      item.account_key
      || item.target_ref?.account_key
      || item.action?.target_ref?.account_key
      || item.session?.action?.target_ref?.account_key
      || item.promotion_context?.account_scope?.account_id
    );
  }

  function resolveCurrentAccount(inputs, onboarding, context, plans, rows) {
    const explicit = [
      inputs.selected_account_key,
      plans.selected_account_key,
      context.selected_account_key,
      context.account_key,
      context.account_scope?.account_id,
      onboarding.selected_account_key,
      onboarding.account_key,
    ].map(accountKey).find(Boolean);
    if (explicit) return explicit;
    const rowAccounts = [...new Set(rows.map(itemAccountKey).filter(Boolean))];
    return rowAccounts.length === 1 ? rowAccounts[0] : "";
  }

  function scopedItems(items, currentAccount) {
    if (!currentAccount) return [];
    return items.filter((item) => itemAccountKey(item) === currentAccount);
  }

  function derive(inputs = {}) {
    const onboarding = inputs.onboarding && typeof inputs.onboarding === "object" ? inputs.onboarding : {};
    const context = inputs.operation_context && typeof inputs.operation_context === "object" ? inputs.operation_context : {};
    const plans = inputs.plan_console && typeof inputs.plan_console === "object" ? inputs.plan_console : {};
    const automation = inputs.automation_readiness && typeof inputs.automation_readiness === "object" ? inputs.automation_readiness : {};
    const preflight = inputs.preflight && typeof inputs.preflight === "object" ? inputs.preflight : {};
    const effectiveness = inputs.effectiveness && typeof inputs.effectiveness === "object" ? inputs.effectiveness : {};
    const planSummary = plans.summary && typeof plans.summary === "object" ? plans.summary : {};
    const actionSummary = automation.summary && typeof automation.summary === "object" ? automation.summary : {};
    const dataReady = onboarding.store_confirmed === true && context.analysis_allowed === true;
    const allPlanRows = Array.isArray(plans.rows) ? plans.rows.filter((row) => row && typeof row === "object") : [];
    const currentAccount = resolveCurrentAccount(inputs, onboarding, context, plans, allPlanRows);
    const planRows = currentAccount ? allPlanRows.filter((row) => itemAccountKey(row) === currentAccount) : allPlanRows;
    const planTotal = planRows.length || (!currentAccount ? count(planSummary.total) : 0);
    const scopedRowsHaveFreshness = planRows.length > 0 && planRows.every((row) => row.stale === false);
    const stalePlans = planRows.length
      ? planRows.filter((row) => row.stale !== false).length
      : count(planSummary.stale);
    const freshPlans = planRows.length
      ? planRows.filter((row) => row.stale === false).length
      : Math.max(0, planTotal - stalePlans);
    const staleOnly = planTotal > 0 && freshPlans === 0 && stalePlans > 0;
    const missingIdPlans = planRows.filter((row) => !hasUsablePlanId(row.plan_id)).length;
    const planFresh = planTotal > 0 && (planRows.length ? scopedRowsHaveFreshness : text(plans.freshness_status) === "fresh" && stalePlans === 0);
    const planReady = dataReady && planTotal > 0 && planFresh;
    const rowsCarryBindingEvidence = planRows.some((row) => Object.prototype.hasOwnProperty.call(row, "eligible_for_local_binding"));
    const identityReady = rowsCarryBindingEvidence
      ? planRows.filter((row) => row.eligible_for_local_binding === true && row.stale === false).length
      : count(planSummary.binding_ready);
    const identityStageReady = planReady && Boolean(currentAccount) && identityReady > 0;
    const automationItems = Array.isArray(automation.items) ? automation.items.filter((item) => item && typeof item === "object") : [];
    const actionItemsHaveAccount = automationItems.some((item) => itemAccountKey(item));
    const actionItems = actionItemsHaveAccount && currentAccount ? scopedItems(automationItems, currentAccount) : automationItems;
    const preflightReady = actionItemsHaveAccount
      ? actionItems.filter((item) => text(item.status) === "preflight_ready").length
      : count(actionSummary.preflight_ready);
    const confirmable = actionItemsHaveAccount
      ? actionItems.filter((item) => text(item.status) === "confirmable").length
      : count(actionSummary.confirmable);
    const identityBlockers = [];
    if (!currentAccount) identityBlockers.push("尚未锁定当前千川账户");
    planRows.forEach((row) => {
      (Array.isArray(row?.binding_blockers) ? row.binding_blockers : []).forEach((blocker) => {
        const value = text(blocker);
        if (value && !identityBlockers.includes(value)) identityBlockers.push(value);
      });
    });
    const actionBlockers = [];
    actionItems.forEach((item) => {
      (Array.isArray(item?.blocked_reasons) ? item.blocked_reasons : []).forEach((blocker) => {
        const value = text(blocker?.message || blocker);
        if (value && !actionBlockers.includes(value)) actionBlockers.push(value);
      });
    });
    const preflightState = text(preflight.state);
    const preflightAccount = itemAccountKey(preflight);
    const preflightAccountMatches = !currentAccount || !preflightAccount || preflightAccount === currentAccount;
    const preflightActive = READY_PREFLIGHT_STATES.has(preflightState) && preflightAccountMatches;
    const preflightBlocked = BLOCKED_PREFLIGHT_STATES.has(preflightState);
    const actionReady = identityStageReady && !preflightBlocked && (preflightReady > 0 || preflightActive);
    const effectItems = scopedItems(
      Array.isArray(effectiveness.items) ? effectiveness.items.filter((item) => item && typeof item === "object") : [],
      currentAccount,
    );
    const evaluatedItems = effectItems.filter((item) => EVALUATED_EFFECT_STATES.has(text(item.status)));
    const evaluated = evaluatedItems.length;
    const effective = evaluatedItems.filter((item) => text(item.status) === "effective").length;
    const planStageSummary = planReady
      ? `当前账户已读取 ${planTotal} 个新鲜计划`
      : staleOnly
        ? `当前账户没有可用的新计划；发现 ${stalePlans} 条历史快照，请重新读取当前计划`
        : planTotal
          ? "计划已读取，但快照过期或缺少时效证据"
          : "尚未读取到当前账户的千川计划";
    const planEvidence = [
      stalePlans ? `历史快照：${stalePlans} 条，仅供复核，不进入自动投放` : planFresh ? "当前计划快照未过期" : "计划时效尚未验证",
      missingIdPlans ? `计划 ID 缺失：${missingIdPlans} 条，无法绑定或执行` : planRows.length ? "计划 ID 已识别" : "",
      Number.isFinite(Number(plans.data_age_seconds)) ? `最旧计划数据距今 ${Math.max(0, Math.floor(Number(plans.data_age_seconds) / 60))} 分钟` : "",
    ];
    const stages = [
      stage("data", "经营数据", dataReady ? "ready" : "blocked", dataReady ? "巡店诊断可以使用" : "还没有可用的经营数据", "scan-card", onboarding.store_confirmed ? "开始巡店" : "打开抖店并开始", [text(context.state_label)]),
      stage("plans", "计划识别", planReady ? "ready" : dataReady && planTotal ? "blocked" : dataReady ? "attention" : "inactive", planStageSummary, "promotion-plan-center", planTotal ? "重新读取当前计划" : "读取当前计划", planEvidence),
      stage("identity", "计划信息", identityStageReady ? "ready" : planReady ? "blocked" : "inactive", identityStageReady ? `${identityReady} 个计划信息完整` : planReady ? "账户、计划 ID、模式或数据质量仍需补齐" : "等待新鲜计划数据", "promotion-plan-center", "补齐计划信息", identityBlockers.slice(0, 3)),
      stage("action", "受控执行", actionReady ? "ready" : identityStageReady && confirmable > 0 && !preflightBlocked ? "attention" : identityStageReady ? "blocked" : "inactive", actionReady ? "已有动作可进入受控执行前检查" : preflightBlocked ? "当前执行前检查已阻断，不能继续授权" : "目前只能建议、预演或人工处理", "automation-section", "查看受控执行", [preflightBlocked ? `执行前检查：${preflightState}` : `可进入执行前检查：${preflightReady} 项`, ...actionBlockers.slice(0, 2)]),
      stage("readback", "结果回读", actionReady && evaluated > 0 ? "ready" : actionReady && effectItems.length > 0 ? "attention" : "inactive", actionReady && evaluated > 0 ? `当前账户已复核 ${evaluated} 个动作结果` : actionReady ? "还没有当前账户可验证的执行结果" : "等待受控执行完成", "promotion-operation-log", "查看操作与回读", currentAccount ? [`当前账户：${currentAccount.slice(-8)}`, `已评价 ${evaluated} 项，其中有效 ${effective} 项`] : ["尚未锁定当前千川账户"]),
    ];
    let level = "setup_required";
    if (actionReady && evaluated) level = "closed_loop";
    else if (actionReady) level = "supervised_ready";
    else if (identityStageReady) level = "local_management";
    else if (planReady) level = "plan_read_only";
    else if (dataReady) level = "diagnosis_ready";
    const next = stages.find((item) => item.status !== "ready") || stages[4];
    return normalize({
      level,
      level_label: LEVELS[level],
      stages,
      next_action: { stage_id: next.id, label: next.action_label, target: next.target, reason: next.summary },
      truth: {
        available_now: [dataReady && "巡店与经营诊断", planReady && "当前账户计划只读筛选", identityStageReady && "计划范围已准备", actionReady && "受控执行前检查", actionReady && evaluated && "当前账户执行结果复盘"].filter(Boolean),
        not_available_yet: [!planReady && "当前账户还没有新鲜、可用的计划快照", !identityStageReady && "自动托管所需计划信息尚未完整", !actionReady && "真实投放调整尚未进入短时效人工授权", !(actionReady && evaluated) && "尚无当前账户已验证的投放动作结果"].filter(Boolean),
        execution_enabled: actionReady && automation.execution_enabled === true && preflight.execution_enabled === true,
      },
      note: "草稿、建议和预演不会被计为真实执行。",
    });
  }

  function normalize(payload = {}) {
    const source = payload && typeof payload === "object" ? payload : {};
    let previousReady = true;
    const stages = (Array.isArray(source.stages) ? source.stages : []).slice(0, 5).map((stage, index) => {
      const value = stage && typeof stage === "object" ? stage : {};
      const requestedStatus = STAGE_STATES.has(text(value.status)) ? text(value.status) : "inactive";
      const status = previousReady ? requestedStatus : "inactive";
      if (status !== "ready") previousReady = false;
      const normalizedStage = {
        id: text(value.id) || `stage-${index + 1}`,
        label: text(value.label) || `第 ${index + 1} 步`,
        status,
        status_label: status === requestedStatus && text(value.status_label) ? text(value.status_label) : ({ ready: "可用", attention: "需复核", blocked: "待补齐", inactive: "等待前一步" }[status]),
        summary: status !== requestedStatus ? "等待前一步完成" : text(value.summary),
        evidence: (Array.isArray(value.evidence) ? value.evidence : []).map(text).filter(Boolean).slice(0, 5),
        target: text(value.target),
        action_label: text(value.action_label),
      };
      return normalizedStage;
    });
    const readyCount = stages.filter((stage) => stage.status === "ready").length;
    const next = source.next_action && typeof source.next_action === "object" ? source.next_action : {};
    const truth = source.truth && typeof source.truth === "object" ? source.truth : {};
    const level = ["setup_required", "diagnosis_ready", "plan_read_only", "local_management", "supervised_ready", "closed_loop"][Math.min(readyCount, 5)];
    return {
      level,
      level_label: LEVELS[level],
      stages,
      ready_count: readyCount,
      total_stages: stages.length || Number(source.total_stages) || 5,
      next_action: {
        stage_id: text(next.stage_id),
        label: text(next.label) || "查看准备项",
        target: text(next.target) || "connection-guide",
        reason: text(next.reason) || "先完成当前缺失项，再进入下一步。",
      },
      truth: {
        available_now: (Array.isArray(truth.available_now) ? truth.available_now : []).map(text).filter(Boolean),
        not_available_yet: (Array.isArray(truth.not_available_yet) ? truth.not_available_yet : []).map(text).filter(Boolean),
        execution_enabled: readyCount >= 4 && truth.execution_enabled === true,
      },
      note: text(source.note),
    };
  }

  return { LEVELS, normalize, derive };
});
