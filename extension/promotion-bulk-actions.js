(function initPromotionBulkActions(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.DianPromotionBulkActions = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function promotionBulkActionsFactory() {
  "use strict";

  const PLAN_FRESHNESS_LIMIT_SECONDS = 30 * 60;

  const ACTIONS = Object.freeze({
    pause_plan: {
      id: "pause_plan",
      label: "批量暂停预演",
      short_label: "暂停计划",
      field: "投放状态",
      risk: "high",
      description: "把投放中的计划整理为暂停候选，逐计划复核后再进入受监督执行。",
    },
    decrease_budget: {
      id: "decrease_budget",
      label: "批量降预算预演",
      short_label: "降低预算",
      field: "日预算",
      risk: "medium",
      description: "按统一比例计算目标预算，不改出价、时长或计划状态。",
    },
    schedule_plan: {
      id: "schedule_plan",
      label: "批量定时启停预演",
      short_label: "定时启停",
      field: "每日时段",
      risk: "medium",
      description: "生成每日启停时段草稿，跨天时段会明确标记。",
    },
  });

  function text(value) {
    return String(value ?? "").trim();
  }

  function finite(value) {
    if (value === null || value === undefined || text(value) === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function normalizeSelection(values) {
    const source = Array.isArray(values) ? values : [values];
    return [...new Set(source.map(text).filter(Boolean))];
  }

  function normalizeTime(value) {
    const match = text(value).match(/^([01]\d|2[0-3]):([0-5]\d)$/);
    return match ? `${match[1]}:${match[2]}` : "";
  }

  function normalizeScope(value = {}) {
    const source = value && typeof value === "object" ? value : {};
    const nested = source.scope && typeof source.scope === "object" ? source.scope : {};
    const storeKey = text(source.store_key ?? nested.store_key).toLowerCase();
    const accountKey = text(source.account_key ?? nested.account_key).toLowerCase();
    return {
      store_key: storeKey,
      account_key: accountKey,
      scope_key: `${storeKey || "unscoped"}::${accountKey || "account-unverified"}`,
      verified: Boolean(accountKey),
    };
  }

  function scopeMatches(candidateValue = {}, expectedValue = {}) {
    const candidate = normalizeScope(candidateValue);
    const expected = normalizeScope(expectedValue);
    if (!expected.account_key || candidate.account_key !== expected.account_key) return false;
    if (expected.store_key) return candidate.store_key === expected.store_key;
    return !candidate.store_key;
  }

  function actionInput(input = {}) {
    const action = ACTIONS[text(input.action)] ? text(input.action) : "";
    if (!action) throw new Error("请选择批量动作");
    const decreasePercent = finite(input.decrease_percent);
    const scheduleStart = normalizeTime(input.schedule_start);
    const scheduleEnd = normalizeTime(input.schedule_end);
    if (action === "decrease_budget" && (decreasePercent === null || decreasePercent < 5 || decreasePercent > 30)) {
      throw new Error("降预算比例必须在 5% 到 30% 之间");
    }
    if (action === "schedule_plan" && (!scheduleStart || !scheduleEnd || scheduleStart === scheduleEnd)) {
      throw new Error("请填写不同的开始与结束时间");
    }
    return {
      action,
      decrease_percent: action === "decrease_budget" ? decreasePercent : null,
      schedule_start: action === "schedule_plan" ? scheduleStart : "",
      schedule_end: action === "schedule_plan" ? scheduleEnd : "",
      scope: normalizeScope(input),
      require_store_scope: Boolean(text(input.store_key ?? input.scope?.store_key)),
      require_account_scope: Boolean(text(input.account_key ?? input.scope?.account_key)),
    };
  }

  function assessPlan(planValue = {}, normalizedInput = {}) {
    const plan = planValue && typeof planValue === "object" ? planValue : {};
    const blockers = [];
    const planKey = text(plan.plan_key);
    const planId = text(plan.plan_id);
    const accountKey = text(plan.account_key);
    const capturedStoreKey = text(plan.store_key || plan.store?.key || plan.binding?.store_key).toLowerCase();
    const accountMatchesRequestedScope = normalizedInput.require_account_scope
      && accountKey.toLowerCase() === normalizedInput.scope.account_key;
    const storeKey = capturedStoreKey || (
      normalizedInput.require_store_scope && accountMatchesRequestedScope
        ? normalizedInput.scope.store_key
        : ""
    );
    const planScope = normalizeScope({ store_key: storeKey, account_key: accountKey });
    const qualityScore = finite(plan.quality_score) ?? 0;
    const budget = finite(plan.budget);
    const dataAgeSeconds = finite(plan.data_age_seconds);
    const declaredFreshness = text(plan.freshness_status).toLowerCase();
    const stale = plan.stale !== false
      || ["stale", "missing", "future"].includes(declaredFreshness)
      || (dataAgeSeconds !== null && (dataAgeSeconds < 0 || dataAgeSeconds >= PLAN_FRESHNESS_LIMIT_SECONDS));
    const freshnessStatus = stale
      ? (declaredFreshness === "future" || (dataAgeSeconds !== null && dataAgeSeconds < 0) ? "future" : dataAgeSeconds === null && !text(plan.captured_at_ms) ? "missing" : "stale")
      : "fresh";
    const status = text(plan.delivery_status) || "状态待识别";
    if (!planKey) blockers.push("计划身份缺失");
    if (!planId) blockers.push("计划 ID 缺失");
    if (!accountKey) blockers.push("广告账户缺失");
    if (normalizedInput.require_account_scope && accountKey.toLowerCase() !== normalizedInput.scope.account_key) blockers.push("计划不属于当前广告账户，已隔离");
    if (normalizedInput.require_store_scope && storeKey !== normalizedInput.scope.store_key) blockers.push(storeKey ? "计划不属于当前店铺，已隔离" : "计划店铺范围无法核验");
    if (stale) blockers.push(freshnessStatus === "future" ? "计划采集时间异常，请重新同步" : "计划数据已过期，请重新同步");
    if (qualityScore < 70) blockers.push(`数据质量 ${qualityScore}，低于 70`);
    if (plan.eligible_for_local_binding !== true) blockers.push(...(Array.isArray(plan.binding_blockers) ? plan.binding_blockers : ["计划绑定条件未通过"]));
    if (!plan.binding) blockers.push("尚未建立本地托管绑定");
    if (plan.binding_paused || plan.binding_state === "blocked") blockers.push("本地绑定当前已暂停，请先重新同步并复核");
    if (plan.supervised_draft_ready !== true) {
      const collectionBlockers = Array.isArray(plan.automation_blockers)
        ? plan.automation_blockers.map((item) => text(item?.message || item)).filter(Boolean)
        : [];
      const nextAction = plan.automation_next_action && typeof plan.automation_next_action === "object"
        ? text(plan.automation_next_action.detail || plan.automation_next_action.label)
        : "";
      blockers.push(...(collectionBlockers.length
        ? collectionBlockers
        : [nextAction || "当前计划范围尚未通过完整采集与身份核验"]));
    }
    if (["pause_plan", "decrease_budget"].includes(normalizedInput.action) && status !== "投放中") blockers.push("仅对投放中计划生成该动作");
    if (normalizedInput.action === "decrease_budget" && (budget === null || budget <= 0)) blockers.push("当前预算缺失或无效");
    if (normalizedInput.action === "schedule_plan" && !["投放中", "暂停"].includes(status)) blockers.push("当前投放状态无法核验");

    let currentValue = status;
    let targetValue = "暂停";
    if (normalizedInput.action === "decrease_budget") {
      currentValue = budget;
      targetValue = budget === null ? null : Math.round(budget * (1 - normalizedInput.decrease_percent / 100) * 100) / 100;
    }
    if (normalizedInput.action === "schedule_plan") {
      currentValue = "未配置本地时段";
      const crossDay = normalizedInput.schedule_end <= normalizedInput.schedule_start;
      targetValue = `每日 ${normalizedInput.schedule_start}–${normalizedInput.schedule_end}${crossDay ? "（跨天）" : ""}`;
    }

    return {
      plan_key: planKey,
      plan_id: planId,
      plan_name: text(plan.plan_name) || "未命名计划",
      account_key: accountKey,
      account_label: text(plan.account_label) || "账户待识别",
      store_key: storeKey,
      store_scope_source: capturedStoreKey ? "plan_snapshot" : storeKey ? "selected_account_scope" : "unverified",
      scope_key: planScope.scope_key,
      scope_verified: planScope.verified,
      promotion_mode: text(plan.promotion_mode) || "unknown",
      plan_type: text(plan.plan_type) || "unknown",
      action: normalizedInput.action,
      operation_label: ACTIONS[normalizedInput.action].short_label,
      field: ACTIONS[normalizedInput.action].field,
      current_value: currentValue,
      target_value: targetValue,
      quality_score: qualityScore,
      freshness_status: freshnessStatus,
      data_age_seconds: dataAgeSeconds,
      data_age_label: text(plan.data_age_label) || "采集时间待确认",
      learning_phase: text(plan.learning_phase) || "unavailable",
      learning_status_label: text(plan.learning_status_label) || "暂不可读",
      platform_low_efficiency: text(plan.platform_low_efficiency) || "unknown",
      platform_low_efficiency_label: text(plan.platform_low_efficiency_label) || "暂不可读",
      diagnostic_source: text(plan.diagnostic_source) || "unavailable",
      diagnostic_source_label: text(plan.diagnostic_source_label) || "暂不可读",
      collection_gate_state: text(plan.collection_gate_state) || "missing",
      collection_scope_key: text(plan.collection_scope_key),
      supervised_draft_ready: plan.supervised_draft_ready === true,
      automation_next_action: plan.automation_next_action && typeof plan.automation_next_action === "object"
        ? { ...plan.automation_next_action }
        : null,
      blockers: [...new Set(blockers.map(text).filter(Boolean))],
      state: blockers.length ? "blocked" : "ready_for_review",
      local_only: true,
      platform_write_enabled: false,
    };
  }

  function buildBulkDraft(plansValue = [], input = {}, now = Date.now()) {
    const plans = Array.isArray(plansValue) ? plansValue : [];
    const selectedKeys = normalizeSelection(input.plan_keys?.length ? input.plan_keys : plans.map((plan) => plan?.plan_key));
    if (!selectedKeys.length) throw new Error("请先选择至少一个计划");
    if (selectedKeys.length > 50) throw new Error("一次最多预演 50 个计划");
    const normalizedInput = actionInput(input);
    const byKey = new Map(plans.map((plan) => [text(plan?.plan_key), plan]));
    const items = selectedKeys.map((key) => {
      const plan = byKey.get(key);
      return plan
        ? assessPlan(plan, normalizedInput)
        : {
            plan_key: key,
            plan_id: "",
            plan_name: "计划已不在当前数据中",
            account_key: "",
            account_label: "账户待识别",
            store_key: "",
            scope_key: "unscoped::account-unverified",
            scope_verified: false,
            action: normalizedInput.action,
            operation_label: ACTIONS[normalizedInput.action].short_label,
            field: ACTIONS[normalizedInput.action].field,
            current_value: null,
            target_value: null,
            quality_score: 0,
            freshness_status: "missing",
            data_age_seconds: null,
            data_age_label: "采集时间待确认",
            blockers: ["计划数据已变化，请重新选择"],
            state: "blocked",
            local_only: true,
            platform_write_enabled: false,
          };
    });
    const ready = items.filter((item) => item.state === "ready_for_review");
    const accountKeys = [...new Set(items.map((item) => item.account_key).filter(Boolean))];
    const currentBudget = normalizedInput.action === "decrease_budget"
      ? ready.reduce((sum, item) => sum + Number(item.current_value || 0), 0)
      : null;
    const targetBudget = normalizedInput.action === "decrease_budget"
      ? ready.reduce((sum, item) => sum + Number(item.target_value || 0), 0)
      : null;
    const createdAt = Number(now);
    const scopeGroups = new Map();
    items.forEach((item) => {
      const scope = normalizeScope(item);
      if (!scopeGroups.has(scope.scope_key)) scopeGroups.set(scope.scope_key, { scope, items: [] });
      scopeGroups.get(scope.scope_key).items.push(item);
    });
    const scopedDrafts = [...scopeGroups.values()].map((group, index) => {
      const scopedReady = group.items.filter((item) => item.state === "ready_for_review");
      const scopedCurrentBudget = normalizedInput.action === "decrease_budget"
        ? scopedReady.reduce((sum, item) => sum + Number(item.current_value || 0), 0)
        : null;
      const scopedTargetBudget = normalizedInput.action === "decrease_budget"
        ? scopedReady.reduce((sum, item) => sum + Number(item.target_value || 0), 0)
        : null;
      return {
        draft_id: `bulk-action-${createdAt}-${items.length}-scope-${index + 1}`,
        parent_draft_id: `bulk-action-${createdAt}-${items.length}`,
        scope: group.scope,
        scope_key: group.scope.scope_key,
        restorable: group.scope.verified,
        items: group.items,
        summary: {
          total: group.items.length,
          ready: scopedReady.length,
          blocked: group.items.length - scopedReady.length,
          accounts: group.scope.account_key ? 1 : 0,
          current_budget: scopedCurrentBudget === null ? null : Math.round(scopedCurrentBudget * 100) / 100,
          target_budget: scopedTargetBudget === null ? null : Math.round(scopedTargetBudget * 100) / 100,
        },
      };
    });
    const commonStoreKeys = [...new Set(scopedDrafts.map((group) => group.scope.store_key).filter(Boolean))];
    const rootScope = scopedDrafts.length === 1
      ? scopedDrafts[0].scope
      : {
          store_key: commonStoreKeys.length === 1 ? commonStoreKeys[0] : "",
          account_key: "",
          scope_key: `${commonStoreKeys.length === 1 ? commonStoreKeys[0] : "multi-store"}::multi-account`,
          verified: false,
        };
    return {
      schema_version: 1,
      draft_id: `bulk-action-${createdAt}-${items.length}`,
      created_at: createdAt,
      action: normalizedInput.action,
      action_label: ACTIONS[normalizedInput.action].label,
      action_description: ACTIONS[normalizedInput.action].description,
      risk_level: ACTIONS[normalizedInput.action].risk,
      parameters: {
        decrease_percent: normalizedInput.decrease_percent,
        schedule_start: normalizedInput.schedule_start,
        schedule_end: normalizedInput.schedule_end,
      },
      scope: rootScope,
      scope_key: rootScope.scope_key,
      restorable: scopedDrafts.length === 1 && rootScope.verified,
      scoped_drafts: scopedDrafts,
      items,
      account_groups: accountKeys.map((accountKey) => ({
        account_key: accountKey,
        account_label: items.find((item) => item.account_key === accountKey)?.account_label || "账户待识别",
        store_key: items.find((item) => item.account_key === accountKey)?.store_key || "",
        scope_key: normalizeScope(items.find((item) => item.account_key === accountKey) || {}).scope_key,
        total: items.filter((item) => item.account_key === accountKey).length,
        ready: ready.filter((item) => item.account_key === accountKey).length,
      })),
      summary: {
        total: items.length,
        ready: ready.length,
        blocked: items.length - ready.length,
        accounts: accountKeys.length,
        current_budget: currentBudget === null ? null : Math.round(currentBudget * 100) / 100,
        target_budget: targetBudget === null ? null : Math.round(targetBudget * 100) / 100,
      },
      local_only: true,
      platform_write_enabled: false,
      automatic_submit: false,
      requires_item_review: true,
      notice: accountKeys.length > 1
        ? "跨账户草稿已隔离为独立账户作用域；必须逐账户、逐计划复核，不会跨账户恢复或自动提交。"
        : "只生成本机预演草稿；必须逐计划复核，不会自动提交千川。",
    };
  }

  function draftForScope(draftValue = {}, scopeValue = {}) {
    const draft = draftValue && typeof draftValue === "object" ? draftValue : {};
    const expected = normalizeScope(scopeValue);
    if (!expected.verified) return null;
    const groups = Array.isArray(draft.scoped_drafts) ? draft.scoped_drafts : [draft];
    const match = groups.find((group) => group?.restorable !== false && scopeMatches(group.scope || group, expected));
    if (!match) return null;
    const { scoped_drafts: _scopedDrafts, ...base } = draft;
    return {
      ...base,
      ...match,
      scope: match.scope,
      scope_key: match.scope_key,
      restorable: true,
      items: match.items,
      account_groups: (Array.isArray(draft.account_groups) ? draft.account_groups : []).filter((group) => group.scope_key === match.scope_key),
      summary: { ...(draft.summary || {}), ...(match.summary || {}) },
      scoped_drafts: [match],
    };
  }

  function filterDraftsForScope(draftsValue = [], scopeValue = {}) {
    const drafts = Array.isArray(draftsValue) ? draftsValue : [];
    return drafts.map((draft) => draftForScope(draft, scopeValue)).filter(Boolean);
  }

  return {
    ACTIONS,
    normalizeSelection,
    normalizeScope,
    scopeMatches,
    assessPlan,
    buildBulkDraft,
    draftForScope,
    filterDraftsForScope,
  };
});
