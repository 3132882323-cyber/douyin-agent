(function initChengfangTrialPolicy(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.DianChengfangTrialPolicy = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createChengfangTrialPolicy() {
  "use strict";

  const CONFIG_VERSION = 2;
  const EVIDENCE_FIELDS = Object.freeze([
    "inventory_days", "fulfillment_rate", "creative_count", "data_completeness",
    "data_freshness_minutes", "current_total_budget", "actual_roi", "today_spend",
    "attributed_orders", "daily_realized_loss",
  ]);
  const FIELD_LABELS = Object.freeze({
    goal: "今天的经营目标",
    price: "商品售价", product_cost: "商品成本", commission_rate: "达人佣金率",
    platform_fee_rate: "平台费用率", merchant_discount: "商家优惠",
    fulfillment_cost: "履约成本", refund_rate: "预估退款率", advertising_cost: "广告消耗",
    minimum_contribution_margin: "最低单件贡献毛利", daily_budget_cap: "单日预算上限",
    daily_loss_cap: "单日最大可承受亏损", refund_rate_ceiling: "退款率预警线",
    inventory_days_floor: "库存覆盖底线", single_adjustment_cap: "单次预算调整上限",
    daily_adjustment_cap: "单日累计调整上限", daily_action_cap: "单日动作次数上限",
    cooldown_minutes: "动作冷却时间", authorization_ttl_seconds: "单次授权有效期",
    inventory_days: "库存覆盖", fulfillment_rate: "履约率", creative_count: "可用素材数",
    data_completeness: "数据完整度", data_freshness_minutes: "数据距今",
    current_total_budget: "当前乘方总预算", actual_roi: "当前综合 ROI",
    today_spend: "今日累计消耗", attributed_orders: "今日归因订单数",
    daily_realized_loss: "今日已实现亏损",
  });

  function finiteNumber(value) {
    if (value === "" || value === null || value === undefined) return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function normalizeWhitelist(value) {
    const items = Array.isArray(value) ? value : [value];
    const first = items.map((item) => String(item || "").trim().slice(0, 160)).find(Boolean);
    return first ? [first] : [];
  }

  function normalizeConfig(value = {}) {
    const source = value && typeof value === "object" ? value : {};
    return {
      schema_version: CONFIG_VERSION,
      environment: source.environment === "production" ? "production" : "demo",
      plan_whitelist: normalizeWhitelist(source.plan_whitelist || source.whitelist),
      budget_cap: finiteNumber(source.budget_cap),
    };
  }

  function fieldLabel(value) {
    const key = String(value || "").split(".").at(-1);
    return FIELD_LABELS[key] || key || "待补字段";
  }

  function evidenceValidation(evidenceValue = {}) {
    const evidence = evidenceValue && typeof evidenceValue === "object" ? evidenceValue : {};
    const missing = [];
    const invalid = [];
    EVIDENCE_FIELDS.forEach((key) => {
      const raw = evidence[key];
      if (raw === "" || raw === null || raw === undefined) {
        missing.push(key);
        return;
      }
      const value = Number(raw);
      if (!Number.isFinite(value) || value < 0) {
        invalid.push(key);
        return;
      }
      if (["fulfillment_rate", "data_completeness"].includes(key) && value > 100) invalid.push(key);
      if (["creative_count", "attributed_orders"].includes(key) && !Number.isInteger(value)) invalid.push(key);
    });
    const completeness = finiteNumber(evidence.data_completeness);
    const freshness = finiteNumber(evidence.data_freshness_minutes);
    const qualityBlockers = [];
    if (completeness !== null && completeness < 80) qualityBlockers.push("数据完整度需达到 80%");
    if (freshness !== null && freshness > 30) qualityBlockers.push("数据距今需不超过 30 分钟");
    const uniqueInvalid = [...new Set(invalid)];
    const qualityFields = [];
    if (completeness !== null && completeness < 80) qualityFields.push("data_completeness");
    if (freshness !== null && freshness > 30) qualityFields.push("data_freshness_minutes");
    return {
      ready: !missing.length && !invalid.length && !qualityBlockers.length,
      missing,
      invalid: uniqueInvalid,
      quality_blockers: qualityBlockers,
      quality_fields: qualityFields,
      completed_count: Math.max(0, EVIDENCE_FIELDS.length - missing.length - uniqueInvalid.length),
      total_count: EVIDENCE_FIELDS.length,
    };
  }

  function deriveCandidatePath(value = {}) {
    const source = value && typeof value === "object" ? value : {};
    const calculation = source.calculation && typeof source.calculation === "object" ? source.calculation : {};
    const boundaries = source.boundaries && typeof source.boundaries === "object" ? source.boundaries : {};
    const evidence = evidenceValidation(source.evidence);
    const goalReady = Boolean(source.goal);
    const costReady = calculation.status === "ready";
    const boundariesReady = boundaries.status === "ready";
    const profileReady = goalReady && costReady && boundariesReady;
    const planKeyReady = Boolean(String(source.planKey || ""));
    const bindingReady = source.identityReady === true && source.dataReady === true && planKeyReady && source.identityConflict !== true;
    const candidateState = String(source.candidate?.state || source.candidateState || "");
    const candidateReady = Boolean(source.candidate) && !["rejected", "expired", "failed", "readback_failed"].includes(candidateState);
    const shadowRunning = source.shadowRunning === true;
    const calculationMissing = Array.isArray(calculation.missing) ? calculation.missing : [];
    const calculationInvalid = Array.isArray(calculation.invalid) ? calculation.invalid : [];
    const boundaryMissing = Array.isArray(boundaries.missing) ? boundaries.missing : [];
    const boundaryInvalid = Array.isArray(boundaries.invalid) ? boundaries.invalid : [];
    const profileMissingCount = (goalReady ? 0 : 1) + calculationMissing.length + boundaryMissing.length;
    const profileInvalidCount = calculationInvalid.length + boundaryInvalid.length + (calculation.status === "loss_before_ads" ? 1 : 0);
    const steps = [
      {
        id: "binding",
        label: "作用域与乘方数据",
        state: bindingReady ? "complete" : "current",
        detail: source.identityConflict === true
          ? "店铺与千川账户证据冲突，需人工核对"
          : !source.identityReady
            ? "待绑定店铺与千川账户"
            : !planKeyReady
              ? "待确认超级策略作用域"
              : !source.dataReady
                ? "待同步可信乘方页面与指标口径"
                : "店铺、账户、策略与数据已确认",
      },
      {
        id: "profile",
        label: "经营建档",
        state: profileReady ? "complete" : bindingReady ? "current" : "waiting",
        detail: profileReady
          ? "目标、8 项成本和 10 项边界已补齐"
          : `${profileMissingCount ? `还缺 ${profileMissingCount} 项` : "字段已填写"}${profileInvalidCount ? `，${profileInvalidCount} 项需修正` : ""}`,
      },
      {
        id: "evidence",
        label: "当前投放证据",
        state: evidence.ready ? "complete" : bindingReady && profileReady ? "current" : "waiting",
        detail: evidence.ready
          ? "10 项证据完整，且完整度与时间达标"
          : evidence.quality_blockers.length
            ? evidence.quality_blockers.join("；")
            : evidence.invalid.length
              ? `${evidence.invalid.length} 项证据需修正`
              : `还缺 ${evidence.missing.length} 项当前证据`,
      },
      {
        id: "candidate",
        label: "A1 生成候选",
        state: candidateReady ? "complete" : bindingReady && profileReady && evidence.ready ? "current" : "waiting",
        detail: candidateReady
          ? "已生成可进入 A2 人工审核的候选"
          : shadowRunning
            ? "A1 已运行，等待立即评估或下一轮评估"
            : "准备完成后开启 A1 影子评估",
      },
    ];
    let nextAction = { id: "candidate", label: "查看 A2 候选", field: "", detail: "第一条候选已生成，可进入 A2 人工审核。" };
    if (!bindingReady) {
      if (source.identityConflict === true) nextAction = { id: "resolve_binding", label: "重新选择投放范围", field: "", detail: "当前页面与所选账户不一致；重新选择后再生成候选。" };
      else if (!source.identityReady) nextAction = { id: "bind_scope", label: "选择本次投放账户", field: "", detail: "选择要管理的千川账户后，系统会自动完成安全校验。" };
      else nextAction = { id: "sync_chengfang", label: "同步当前乘方页", field: "", detail: planKeyReady ? "打开正确的乘方页面后同步，确认指标口径和数据时间。" : "同步当前乘方页，确认超级策略作用域和指标口径。" };
    } else if (!goalReady) {
      nextAction = { id: "goal", label: "选择经营目标", field: "goal", detail: "先选择今天唯一的经营目标。" };
    } else if (!costReady) {
      nextAction = {
        id: "costs", label: "补齐利润与成本", field: calculationInvalid[0] || calculationMissing[0] || "price",
        detail: calculation.status === "loss_before_ads" ? "当前成本口径显示未投广告前已无正向空间，不会生成预算候选。" : `下一项：${fieldLabel(calculationInvalid[0] || calculationMissing[0] || "price")}`,
      };
    } else if (!boundariesReady) {
      nextAction = { id: "boundaries", label: "补齐经营边界", field: boundaryInvalid[0] || boundaryMissing[0] || "daily_budget_cap", detail: `下一项：${fieldLabel(boundaryInvalid[0] || boundaryMissing[0] || "daily_budget_cap")}` };
    } else if (!evidence.ready) {
      nextAction = {
        id: "evidence", label: "补齐当前证据", field: evidence.invalid[0] || evidence.missing[0] || evidence.quality_fields[0] || "current_total_budget",
        detail: evidence.quality_blockers[0] || `下一项：${fieldLabel(evidence.invalid[0] || evidence.missing[0] || "current_total_budget")}`,
      };
    } else if (!shadowRunning) {
      nextAction = { id: "shadow", label: "开启 A1 影子评估", field: "", detail: "开启后只生成候选，不会提交或填写千川。" };
    } else if (!candidateReady) {
      nextAction = { id: "evaluate", label: "立即运行首次评估", field: "", detail: candidateState ? "上一条候选已结束，重新评估最新证据；未触发规则时保持不动。" : "触发确定性边界时生成候选，否则记录保持策略。" };
    }
    return {
      ready: candidateReady,
      binding_ready: bindingReady,
      profile_ready: profileReady,
      evidence_ready: evidence.ready,
      shadow_running: shadowRunning,
      candidate_ready: candidateReady,
      steps,
      completed_count: steps.filter((item) => item.state === "complete").length,
      total_count: steps.length,
      next_action: nextAction,
      cost: {
        ready: costReady,
        completed_count: Math.max(0, 8 - new Set([...calculationMissing, ...calculationInvalid]).size),
        total_count: 8,
        missing: calculationMissing,
        invalid: calculationInvalid,
      },
      boundaries: {
        ready: boundariesReady,
        completed_count: Math.max(0, 10 - new Set([...boundaryMissing, ...boundaryInvalid]).size),
        total_count: 10,
        missing: boundaryMissing,
        invalid: boundaryInvalid,
      },
      evidence,
    };
  }

  function a2Summary(runtime = {}) {
    const nested = runtime.a2_pilot;
    return nested && typeof nested === "object" ? nested : runtime;
  }

  function policyBoundaries(runtime = {}) {
    const policy = runtime.policy && typeof runtime.policy === "object" ? runtime.policy : {};
    return {
      daily_budget_cap: finiteNumber(policy.daily_budget_cap),
      max_daily_loss: finiteNumber(policy.max_daily_loss),
      max_single_adjustment_pct: finiteNumber(policy.max_single_adjustment_pct),
      max_daily_adjustment_pct: finiteNumber(policy.max_daily_adjustment_pct),
      max_daily_actions: finiteNumber(policy.max_daily_actions),
      cooldown_minutes: finiteNumber(policy.cooldown_minutes),
      authorization_ttl_seconds: finiteNumber(policy.authorization_ttl_seconds),
    };
  }

  function latestCandidate(a2 = {}) {
    const candidates = Array.isArray(a2.candidates) ? a2.candidates : [];
    return candidates.length ? candidates[candidates.length - 1] : null;
  }

  function latestExecution(a2 = {}, candidate = null) {
    const executions = Array.isArray(a2.executions) ? a2.executions : [];
    if (!executions.length) return null;
    if (!candidate?.candidate_id) return executions[executions.length - 1];
    return [...executions].reverse().find((item) => item?.candidate_id === candidate.candidate_id) || null;
  }

  function rounded(value, digits = 4) {
    if (!Number.isFinite(value)) return null;
    const scale = 10 ** digits;
    return Math.round((value + Number.EPSILON) * scale) / scale;
  }

  function deriveEffect(runtimeValue = {}) {
    const runtime = runtimeValue && typeof runtimeValue === "object" ? runtimeValue : {};
    const a2 = a2Summary(runtime);
    const candidates = Array.isArray(a2.candidates) ? a2.candidates.filter((item) => item && typeof item === "object") : [];
    const executions = Array.isArray(a2.executions) ? a2.executions.filter((item) => item && typeof item === "object") : [];
    const execution = executions.length ? executions[executions.length - 1] : null;
    const candidate = execution?.candidate_id
      ? [...candidates].reverse().find((item) => item.candidate_id === execution.candidate_id) || null
      : candidates.length ? candidates[candidates.length - 1] : null;
    const currentBudget = finiteNumber(execution?.current_value ?? candidate?.current_value);
    const simulatedTarget = finiteNumber(execution?.target_value ?? candidate?.target_value);
    const budgetChange = currentBudget !== null && simulatedTarget !== null
      ? rounded(simulatedTarget - currentBudget, 2)
      : null;
    const budgetChangePct = currentBudget !== null && currentBudget !== 0 && simulatedTarget !== null
      ? rounded((simulatedTarget - currentBudget) / currentBudget * 100, 4)
      : null;
    const receipt = execution?.adapter_receipt && typeof execution.adapter_receipt === "object"
      ? execution.adapter_receipt
      : null;
    const readback = execution?.readback && typeof execution.readback === "object"
      ? execution.readback
      : null;
    const state = String(execution?.state || candidate?.state || "");
    const stateLabels = {
      shadow_candidate: "候选已生成",
      accepted_for_pilot: "等待本机模拟",
      executing: "本机模拟中",
      awaiting_readback: "等待模拟回读",
      verified: "模拟闭环已匹配",
      verified_stop_loss: "模拟闭环完成并止损",
      rejected: "候选已拒绝",
      expired: "候选已过期",
      failed: "本机模拟失败",
      readback_failed: "模拟回读不匹配",
    };
    const synthetic = runtime.synthetic === true && runtime.demo_fixture === true
      && a2.synthetic === true && a2.demo_fixture === true;
    return {
      synthetic,
      demo_fixture: synthetic,
      platform_write_attempted: execution?.platform_write_attempted === false ? false : null,
      source: execution ? "execution" : candidate ? "candidate" : "none",
      state,
      state_label: stateLabels[state] || "等待试运行数据",
      candidate_id: String(candidate?.candidate_id || execution?.candidate_id || ""),
      execution_id: String(execution?.execution_id || ""),
      current_budget: currentBudget,
      simulated_target: simulatedTarget,
      budget_change: budgetChange,
      budget_change_pct: budgetChangePct,
      receipt: {
        present: Boolean(receipt && Object.keys(receipt).length),
        ok: typeof receipt?.ok === "boolean" ? receipt.ok : null,
        receipt_id: String(receipt?.receipt_id || ""),
        execution_kind: String(receipt?.execution_kind || execution?.execution_kind || ""),
        adapter_id: String(receipt?.adapter_id || execution?.adapter_id || ""),
        platform_write_attempted: typeof receipt?.platform_write_attempted === "boolean"
          ? receipt.platform_write_attempted
          : typeof execution?.platform_write_attempted === "boolean"
            ? execution.platform_write_attempted
            : null,
      },
      readback: {
        present: Boolean(readback && Object.keys(readback).length),
        observed_value: finiteNumber(readback?.observed_value),
        expected_value: finiteNumber(readback?.expected_value),
        matched: typeof readback?.matched === "boolean" ? readback.matched : null,
        source: String(readback?.source || ""),
        execution_kind: String(readback?.execution_kind || ""),
        platform_write_observed: typeof readback?.platform_write_observed === "boolean"
          ? readback.platform_write_observed
          : null,
      },
      outcome_limits: {
        roi: { proven: false, value: null, label: "本机模拟不能证明 ROI 变化" },
        orders: { proven: false, value: null, label: "本机模拟不能证明订单变化" },
      },
      observation_nodes: ["2h", "24h", "3d", "7d"].map((id) => ({
        id,
        label: id === "2h" ? "2 小时" : id === "24h" ? "24 小时" : id === "3d" ? "3 天" : "7 天",
        state: "reserved",
        detail: `${id === "2h" ? "预留：动作状态与消耗回流" : id === "24h" ? "预留：首日 ROI 与订单" : id === "3d" ? "预留：连续表现" : "预留：完整经营效果复盘"}；当前无生产数据`,
      })),
      evidence_notice: synthetic
        ? "SYNTHETIC / DEMO_FIXTURE：以下数值全是合成演示，platform_write_attempted=false，不是实际投放效果。"
        : "数值仅来自当前 A2 候选、模拟执行回执与模拟回读；缺失值不会按 0 展示。",
    };
  }

  function workflowSteps(candidate, execution) {
    const state = String(execution?.state || candidate?.state || "");
    const steps = [
      { id: "candidate", label: "候选", state: "waiting", detail: "等待 A1 影子评估生成候选" },
      { id: "review", label: "人工审核", state: "waiting", detail: "确认候选与经营边界" },
      { id: "simulation", label: "本机模拟", state: "waiting", detail: "不请求、不填写、不提交平台" },
      { id: "readback", label: "模拟回读", state: "waiting", detail: "核对目标值和止损结果" },
    ];
    if (!candidate) return steps;
    steps[0] = { ...steps[0], state: "complete", detail: "已锁定当前策略的最新候选" };
    if (state === "shadow_candidate") {
      steps[1] = { ...steps[1], state: "current", detail: "等待接受或拒绝" };
    } else if (state === "rejected") {
      steps[1] = { ...steps[1], state: "blocked", detail: "候选已拒绝，等待下一条" };
    } else if (state === "expired") {
      steps[0] = { ...steps[0], state: "blocked", detail: "候选证据已过期" };
    } else {
      steps[1] = { ...steps[1], state: "complete", detail: "已由人工审核进入 A2 模拟" };
    }
    if (state === "accepted_for_pilot") {
      steps[2] = { ...steps[2], state: "current", detail: "等待执行本机模拟" };
    } else if (state === "executing") {
      steps[2] = { ...steps[2], state: "current", detail: "本机模拟适配器正在运行" };
    } else if (["awaiting_readback", "verified", "verified_stop_loss", "readback_failed"].includes(state)) {
      steps[2] = { ...steps[2], state: "complete", detail: "模拟完成，平台写入未发生" };
    } else if (state === "failed") {
      steps[2] = { ...steps[2], state: "blocked", detail: "模拟失败，A2 已安全停止" };
    }
    if (state === "awaiting_readback") {
      steps[3] = { ...steps[3], state: "current", detail: "等待模拟回读，不读取平台执行结果" };
    } else if (state === "verified") {
      steps[3] = { ...steps[3], state: "complete", detail: "模拟目标值已匹配" };
    } else if (state === "verified_stop_loss") {
      steps[3] = { ...steps[3], state: "complete", detail: "回读完成并触发止损，A2 已停止" };
    } else if (state === "readback_failed") {
      steps[3] = { ...steps[3], state: "blocked", detail: "回读不一致，A2 已安全停止" };
    }
    return steps;
  }

  function deriveView(runtimeValue = {}, configValue = {}, syncSettingsValue = {}) {
    const runtime = runtimeValue && typeof runtimeValue === "object" ? runtimeValue : {};
    const a2 = a2Summary(runtime);
    const config = normalizeConfig(configValue);
    const syncSettings = syncSettingsValue && typeof syncSettingsValue === "object" ? syncSettingsValue : {};
    const fiveMinuteSyncEnabled = syncSettings.autoSync === true
      && Number(syncSettings.intervalMinutes || 0) > 0
      && Number(syncSettings.intervalMinutes || 0) <= 5;
    const boundaries = policyBoundaries(runtime);
    const boundaryReady = [
      boundaries.daily_budget_cap,
      boundaries.max_daily_loss,
      boundaries.max_single_adjustment_pct,
      boundaries.max_daily_adjustment_pct,
      boundaries.max_daily_actions,
      boundaries.cooldown_minutes,
      boundaries.authorization_ttl_seconds,
    ].every((value) => value !== null);
    const planKey = String(a2.plan_key || "").trim();
    const requestedWhitelist = normalizeWhitelist(config.plan_whitelist);
    const exactWhitelistReady = Boolean(planKey && requestedWhitelist.length === 1 && requestedWhitelist[0] === planKey);
    const activeWhitelistReady = Boolean(planKey && Array.isArray(a2.plan_whitelist)
      && a2.plan_whitelist.length === 1 && a2.plan_whitelist[0] === planKey);
    const pilotBudgetCap = finiteNumber(config.budget_cap ?? a2.budget_cap);
    const candidate = latestCandidate(a2);
    const execution = latestExecution(a2, candidate);
    const state = String(execution?.state || candidate?.state || "");
    const evidenceFresh = a2.evidence_gate?.fresh === true;
    const masterEnabled = a2.master_enabled === true && a2.stop_active === false;
    const demo = config.environment === "demo";
    const blockers = [];
    if (!demo) blockers.push("官方调控能力已发现，但当前应用权限、账户白名单与生产适配器尚未验收；生产环境保持只读锁定");
    if (!fiveMinuteSyncEnabled) blockers.push("请明确开启每 5 分钟同步，避免 Agent 反复评估旧页面数据");
    if (!planKey) blockers.push("尚未取得当前店铺、账户与超级策略绑定后的唯一作用域");
    if (planKey && !exactWhitelistReady) blockers.push("请将当前策略的唯一作用域加入白名单，不可手工填写其他计划编号");
    if (!boundaryReady) blockers.push("预算、止损、频率、冷却和授权时效边界尚未补齐");
    if (!evidenceFresh) blockers.push("经营证据尚未达到 A2 新鲜度门槛，请先同步页面并刷新手工证据");
    if (pilotBudgetCap === null || pilotBudgetCap <= 0) blockers.push("请填写大于 0 的 A2 试运行预算上限");
    if (pilotBudgetCap !== null && boundaries.daily_budget_cap !== null && pilotBudgetCap > boundaries.daily_budget_cap) {
      blockers.push("A2 试运行预算上限不得高于经营建档中的单日预算上限");
    }
    const canConfigure = blockers.length === 0 && a2.platform_write_enabled !== true;
    const steps = workflowSteps(candidate, execution);
    const effect = deriveEffect(runtime);
    const syntheticDemo = effect.synthetic === true;
    const nextStep = !masterEnabled
      ? (blockers[0] || "开启 A2 模拟试运行；它不会修改乘方")
      : !candidate
        ? "A2 模拟已开启；立即运行一次 A1 影子评估生成候选"
        : state === "shadow_candidate"
          ? "人工审核候选；接受后才能进入本机模拟"
          : state === "accepted_for_pilot"
            ? evidenceFresh
              ? "执行本机模拟；不会请求或提交千川"
              : "经营证据已过期，A2 会阻止模拟执行；请先同步并重新生成候选"
            : state === "awaiting_readback"
              ? "完成模拟回读并核对止损结果"
              : ["verified", "verified_stop_loss"].includes(state)
                ? "本轮模拟闭环完成；等待新的影子候选"
                : ["rejected", "expired"].includes(state)
                  ? "本候选已结束；重新评估后等待下一条候选"
                  : ["failed", "readback_failed"].includes(state)
                    ? "A2 已安全停止；检查原因并重新配置"
                    : "等待本机 Agent 更新试运行状态";
    return {
      a2,
      config: { ...config, budget_cap: pilotBudgetCap },
      synthetic_demo: syntheticDemo,
      environment_label: syntheticDemo ? "SYNTHETIC · A2 合成演示" : demo ? "演示环境 · A2 本机模拟" : "生产环境 · 只读锁定",
      environment_tone: demo ? "demo" : "danger",
      platform_write_enabled: false,
      platform_write_label: syntheticDemo ? "platform_write_attempted=false · 不是实际投放" : "关闭 · 不会提交千川",
      authorization_scope: syntheticDemo ? "DEMO_FIXTURE · 不产生真实授权" : masterEnabled ? "A2 模拟主开关已开启" : "A1 影子评估 / A2 未开启",
      allowed_action_label: syntheticDemo ? "仅展示合成预算变化，不执行平台写入" : "仅模拟降低总预算，不执行平台写入",
      scope_fingerprint: String(runtime.scope_fingerprint || ""),
      plan_key: planKey,
      exact_whitelist_ready: exactWhitelistReady,
      active_whitelist_ready: activeWhitelistReady,
      five_minute_sync_enabled: fiveMinuteSyncEnabled,
      boundaries,
      boundary_ready: boundaryReady,
      budget_cap: pilotBudgetCap,
      candidate,
      execution,
      candidate_state: state,
      evidence_fresh: evidenceFresh,
      steps,
      effect,
      blockers,
      master_enabled: masterEnabled,
      stop_reason: String(a2.stop_reason || ""),
      can_configure: !syntheticDemo && canConfigure,
      can_accept: demo && masterEnabled && activeWhitelistReady && state === "shadow_candidate",
      can_reject: demo && state === "shadow_candidate",
      can_execute: demo && masterEnabled && activeWhitelistReady && fiveMinuteSyncEnabled
        && evidenceFresh && state === "accepted_for_pilot" && a2.execution_kind === "simulation",
      can_readback: demo && state === "awaiting_readback" && execution?.execution_kind === "simulation",
      can_stop: !syntheticDemo && masterEnabled,
      workflow_state: syntheticDemo ? "合成演示闭环完成 · 未持久化" : masterEnabled ? "A2 模拟运行中" : state ? "A2 已停止 / 保留记录" : "A1 等待候选",
      next_step: syntheticDemo ? "演示完成：候选、模拟执行和模拟回读均为合成数据；点击“返回真实数据”继续建档。" : nextStep,
      start_label: syntheticDemo ? "演示已完成（不是实际投放）" : !demo ? "生产环境只读锁定" : masterEnabled ? "A2 模拟试运行已开启" : "开启 A2 模拟试运行",
    };
  }

  function buildConfigurePayload(runtime, configValue, syncSettingsValue = {}) {
    const view = deriveView(runtime, configValue, syncSettingsValue);
    if (!view.can_configure) return { ok: false, blockers: view.blockers, payload: null };
    return {
      ok: true,
      blockers: [],
      payload: {
        master_enabled: true,
        execution_kind: "simulation",
        plan_whitelist: [view.plan_key],
        budget_cap: view.budget_cap,
        confirm: true,
      },
    };
  }

  return {
    CONFIG_VERSION,
    EVIDENCE_FIELDS,
    buildConfigurePayload,
    deriveCandidatePath,
    deriveEffect,
    deriveView,
    normalizeConfig,
    normalizeWhitelist,
  };
});
