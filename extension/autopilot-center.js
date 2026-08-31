(function initAutopilotCenter(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.DianAutopilotCenter = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createAutopilotCenter() {
  "use strict";

  const SCHEMA_VERSION = 1;
  const MODE_DEFINITIONS = Object.freeze({
    protect: Object.freeze({
      id: "protect",
      label: "守钱",
      eyebrow: "首次托管",
      description: "先止损、再减少无效消耗，只整理降预算与高风险暂停候选。",
      action_families: Object.freeze(["budget_decrease_draft", "high_risk_pause_review", "anomaly_alert"]),
    }),
    stabilize: Object.freeze({
      id: "stabilize",
      label: "稳投",
      eyebrow: "影子验证后",
      description: "在稳定回读基础上生成小幅预算调节草稿，优先守住利润与波动边界。",
      action_families: Object.freeze(["budget_decrease_draft", "bounded_budget_adjustment_draft", "high_risk_pause_review"]),
    }),
    scale: Object.freeze({
      id: "scale",
      label: "放量",
      eyebrow: "生产验收后",
      description: "仅在官方写合同、白名单、回读与回滚全部验收后开放受控放量。",
      action_families: Object.freeze(["bounded_budget_increase", "bounded_roi_adjustment", "automatic_readback"]),
    }),
  });
  const BUSINESS_STAGE_DEFINITIONS = Object.freeze({
    cold_start: Object.freeze({
      id: "cold_start", label: "冷启动验证", gmv_weight: 60, profit_weight: 40,
      description: "先取得可信样本，再讨论扩量；预算和动作频率保持保守。",
      roi_buffer: 1.05,
    }),
    balanced: Object.freeze({
      id: "balanced", label: "稳态经营", gmv_weight: 50, profit_weight: 50,
      description: "规模与利润同权，优先减少波动和无效消耗。",
      roi_buffer: 1.10,
    }),
    controlled_growth: Object.freeze({
      id: "controlled_growth", label: "受控增长", gmv_weight: 60, profit_weight: 40,
      description: "仅在利润安全线之上生成小幅放量候选，并要求回读。",
      roi_buffer: 1.08,
    }),
    profit_mature: Object.freeze({
      id: "profit_mature", label: "利润经营", gmv_weight: 35, profit_weight: 65,
      description: "成熟阶段优先守住贡献毛利和退款后的真实利润。",
      roi_buffer: 1.15,
    }),
    inventory_clearance: Object.freeze({
      id: "inventory_clearance", label: "库存消化", gmv_weight: 70, profit_weight: 30,
      description: "在库存与亏损红线内提高出货权重，不牺牲单日止损。",
      roi_buffer: 1.03,
    }),
    live_pacing: Object.freeze({
      id: "live_pacing", label: "直播节奏", gmv_weight: 55, profit_weight: 45,
      description: "围绕开播时段控制流速，先看承接，再决定是否追加消耗。",
      roi_buffer: 1.08,
    }),
  });
  const BUSINESS_STAGE_BY_GOAL = Object.freeze({
    new_product: "cold_start",
    stable: "balanced",
    growth: "controlled_growth",
    profit: "profit_mature",
    clearance: "inventory_clearance",
    live_burst: "live_pacing",
  });
  const UNIT_ECONOMIC_FIELDS = Object.freeze([
    "price", "product_cost", "commission_rate", "platform_fee_rate",
    "merchant_discount", "fulfillment_cost", "refund_rate", "advertising_cost",
  ]);
  const TERMINAL_CANDIDATE_STATES = new Set([
    "verified", "rejected", "expired", "failed", "readback_failed", "cancelled", "canceled",
  ]);

  function finiteNumber(value) {
    if (value === "" || value === null || value === undefined) return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function ratio(value) {
    const number = finiteNumber(value);
    if (number === null || number < 0) return 0;
    return Math.min(1, number > 1 ? number / 100 : number);
  }

  function percent(value) {
    const number = finiteNumber(value);
    return number !== null && number >= 0 && number <= 100 ? number : null;
  }

  function roundNumber(value, digits = 2) {
    if (!Number.isFinite(value)) return null;
    const scale = 10 ** digits;
    return Math.round(value * scale) / scale;
  }

  function deriveUnitEconomics(localPlan = {}) {
    const inputs = localPlan.inputs && typeof localPlan.inputs === "object" ? localPlan.inputs : {};
    const values = Object.fromEntries(UNIT_ECONOMIC_FIELDS.map((field) => [field, finiteNumber(inputs[field])]));
    const missing = UNIT_ECONOMIC_FIELDS.filter((field) => values[field] === null);
    const invalid = UNIT_ECONOMIC_FIELDS.filter((field) => {
      const value = values[field];
      if (value === null) return false;
      if (["commission_rate", "platform_fee_rate", "refund_rate"].includes(field)) return value < 0 || value > 100;
      return value < 0;
    });
    if (values.price !== null && values.price <= 0) invalid.push("price");
    if (missing.length || invalid.length) {
      return { status: invalid.length ? "invalid" : "incomplete", values, missing, invalid: [...new Set(invalid)] };
    }
    const retainedRevenue = values.price * (1 - values.refund_rate / 100);
    const commission = retainedRevenue * values.commission_rate / 100;
    const platformFee = retainedRevenue * values.platform_fee_rate / 100;
    const nonAdCost = values.product_cost + commission + platformFee + values.merchant_discount + values.fulfillment_cost;
    const maxAdSpend = retainedRevenue - nonAdCost;
    if (maxAdSpend <= 0) {
      return {
        status: "loss_before_ads", values, missing: [], invalid: [],
        retained_revenue: roundNumber(retainedRevenue), non_ad_cost: roundNumber(nonAdCost), max_ad_spend: roundNumber(maxAdSpend),
      };
    }
    return {
      status: "ready", values, missing: [], invalid: [],
      retained_revenue: roundNumber(retainedRevenue),
      non_ad_cost: roundNumber(nonAdCost),
      max_ad_spend: roundNumber(maxAdSpend),
      break_even_roi: roundNumber(retainedRevenue / maxAdSpend, 3),
      contribution_margin: roundNumber(maxAdSpend - values.advertising_cost),
    };
  }

  function deriveBusinessStrategy(localPlan = {}, gate = {}) {
    const goal = String(localPlan.goal || "");
    const stageId = BUSINESS_STAGE_BY_GOAL[goal] || "";
    const stage = BUSINESS_STAGE_DEFINITIONS[stageId] || null;
    const economics = deriveUnitEconomics(localPlan);
    const evidence = localPlan.evidence && typeof localPlan.evidence === "object" ? localPlan.evidence : {};
    const naturalFlowRatio = percent(evidence.natural_flow_ratio);
    const paidOrderRatio = percent(evidence.paid_order_ratio);
    let attributionStatus = "missing";
    let attributionLabel = "归因占比待补齐";
    if (naturalFlowRatio !== null && paidOrderRatio !== null) {
      attributionStatus = naturalFlowRatio + paidOrderRatio > 110 ? "needs_review" : "declared";
      attributionLabel = attributionStatus === "needs_review"
        ? "自然流与付费归因口径需复核"
        : `自然流 ${naturalFlowRatio}% · 付费归因 ${paidOrderRatio}%`;
    } else if (naturalFlowRatio !== null || paidOrderRatio !== null) {
      attributionStatus = "partial";
      attributionLabel = naturalFlowRatio !== null ? `自然流 ${naturalFlowRatio}% · 付费待补` : `付费归因 ${paidOrderRatio}% · 自然流待补`;
    }
    const blockers = [];
    const warnings = [];
    if (!stage) blockers.push("先选择经营目标，才能映射经营阶段");
    if (economics.status !== "ready") blockers.push(economics.status === "loss_before_ads" ? "广告前已亏损，不能生成投放恢复线" : "先补齐 8 项单件经济模型");
    if (gate.identity_ready !== true || gate.identity_conflict === true) blockers.push("先确认唯一店铺和广告账户作用域");
    if (gate.metric_ready !== true || gate.data_ready !== true) blockers.push("先取得可信 ROI 口径和新鲜投放数据");
    if (attributionStatus === "missing") warnings.push("尚未区分自然流成交与付费归因，策略置信度降低");
    if (attributionStatus === "partial") warnings.push("自然流与付费归因只补齐了一项");
    if (attributionStatus === "needs_review") warnings.push("两项归因占比合计明显超过 100%，需核对是否存在重复归因");
    const observationRoiFloor = stage && economics.status === "ready"
      ? roundNumber(economics.break_even_roi * stage.roi_buffer, 2)
      : null;
    return {
      ready: !blockers.length,
      goal,
      stage: stage?.id || "unconfigured",
      stage_label: stage?.label || "经营阶段待确认",
      stage_description: stage?.description || "完成经营目标和成本建档后生成阶段策略。",
      gmv_weight: stage?.gmv_weight ?? null,
      profit_weight: stage?.profit_weight ?? null,
      economics,
      break_even_roi: economics.status === "ready" ? economics.break_even_roi : null,
      observation_roi_floor: observationRoiFloor,
      attribution_status: attributionStatus,
      attribution_label: attributionLabel,
      blockers,
      warnings,
      confidence: blockers.length ? "blocked" : warnings.length ? "medium" : "high",
      source_label: "经营目标、单件经济模型与本店归因口径",
      local_only: true,
      platform_write_enabled: false,
    };
  }

  function deriveRecoveryPolicy(input = {}) {
    const localPlan = input.localPlan && typeof input.localPlan === "object" ? input.localPlan : {};
    const runtime = input.runtime && typeof input.runtime === "object" ? input.runtime : {};
    const gate = input.gate && typeof input.gate === "object" ? input.gate : {};
    const business = input.businessStrategy || deriveBusinessStrategy(localPlan, gate);
    const evidence = localPlan.evidence && typeof localPlan.evidence === "object" ? localPlan.evidence : {};
    const dailyBudgetCap = policyValue(runtime, localPlan, "daily_budget_cap", "daily_budget_cap");
    const maxDailyLoss = policyValue(runtime, localPlan, "max_daily_loss", "daily_loss_cap");
    const cooldown = policyValue(runtime, localPlan, "cooldown_minutes", "cooldown_minutes");
    const dailyActionCap = policyValue(runtime, localPlan, "max_daily_actions", "daily_action_cap");
    const sampleSpendFloor = dailyBudgetCap === null
      ? null
      : roundNumber(Math.min(dailyBudgetCap, Math.max(100, dailyBudgetCap * 0.2)));
    const recheckMinutes = cooldown === null ? null : Math.max(30, Math.round(cooldown));
    const maxCycles = dailyActionCap === null ? null : Math.max(1, Math.min(3, Math.trunc(dailyActionCap)));
    const minOrders = 3;
    const pauseRoiFloor = business.break_even_roi;
    const resumeRoiFloor = business.observation_roi_floor;
    const todaySpend = finiteNumber(evidence.today_spend);
    const actualRoi = finiteNumber(evidence.actual_roi);
    const attributedOrders = finiteNumber(evidence.attributed_orders);
    const realizedLoss = finiteNumber(evidence.daily_realized_loss);
    const blockers = [...business.blockers];
    if (dailyBudgetCap === null) blockers.push("缺少单日预算上限");
    if (maxDailyLoss === null) blockers.push("缺少单日止损线");
    if (cooldown === null) blockers.push("缺少动作冷却时间");
    if (dailyActionCap === null) blockers.push("缺少单日动作次数上限");
    const uniqueBlockers = [...new Set(blockers)];
    let state = uniqueBlockers.length ? "blocked" : "observing";
    if (!uniqueBlockers.length && todaySpend !== null && actualRoi !== null && attributedOrders !== null) {
      if (realizedLoss !== null && maxDailyLoss !== null && realizedLoss >= maxDailyLoss) state = "hard_stop";
      else if (todaySpend < sampleSpendFloor || attributedOrders < minOrders) state = "collecting";
      else if (actualRoi < pauseRoiFloor) state = "pause_candidate";
      else if (actualRoi >= resumeRoiFloor) state = "healthy";
      else state = "watch";
    }
    const steps = [
      { id: "sample", label: "样本观察", state: "current" },
      { id: "pause", label: "暂停候选", state: "waiting" },
      { id: "cooldown", label: "冷却复检", state: "waiting" },
      { id: "recover", label: "恢复或重建", state: "waiting" },
      { id: "readback", label: "结果回读", state: "waiting" },
    ];
    if (["watch", "healthy", "pause_candidate", "hard_stop"].includes(state)) steps[0].state = "complete";
    if (state === "pause_candidate") steps[1].state = "current";
    if (state === "hard_stop") steps[1].state = "failed";
    const realtimeState = String(input.realtimeTask?.state || "");
    if (realtimeState === "verified_stop_loss") {
      steps[0].state = "complete";
      steps[1].state = "complete";
      steps[2].state = "current";
    } else if (realtimeState === "awaiting_readback") {
      steps.slice(0, 4).forEach((step) => { step.state = "complete"; });
      steps[4].state = "current";
    } else if (realtimeState === "verified") {
      steps.forEach((step) => { step.state = "complete"; });
    }
    const labels = {
      blocked: ["暂不可推演", "warning"],
      observing: ["等待当前样本", "warning"],
      collecting: ["继续积累样本", "warning"],
      pause_candidate: ["命中暂停候选", "danger"],
      hard_stop: ["触发单日止损", "danger"],
      watch: ["接近利润观察线", "warning"],
      healthy: ["当前未触发止损", "safe"],
    };
    const stateMeta = labels[state] || labels.observing;
    let decision = "完成经营建档后生成恢复规则。";
    if (state === "observing") decision = "等待当前消耗、ROI 和归因订单样本，不提前判断。";
    if (state === "collecting") decision = `继续采样：至少消耗 ¥${sampleSpendFloor.toFixed(2)} 且达到 ${minOrders} 笔归因订单。`;
    if (state === "pause_candidate") decision = "已满足高风险暂停候选条件；只生成草稿，等待人工复核。";
    if (state === "hard_stop") decision = "已达到单日亏损线；保持停止并要求人工解除，不进入自动恢复。";
    if (state === "watch") decision = "ROI 高于保本线但未达到恢复观察线，继续观察，不反复调参。";
    if (state === "healthy") decision = "当前样本高于恢复观察线，保持策略并继续回读。";
    const ruleSummary = sampleSpendFloor !== null && pauseRoiFloor !== null && resumeRoiFloor !== null && recheckMinutes !== null && maxCycles !== null
      ? `样本 ¥${sampleSpendFloor.toFixed(2)} · 暂停 ROI＜${pauseRoiFloor.toFixed(2)} · 恢复 ROI≥${resumeRoiFloor.toFixed(2)} · 冷却 ${recheckMinutes} 分钟 · 最多 ${maxCycles} 轮`
      : "关键阈值待补齐；不会使用猜测值启动恢复循环。";
    return {
      ready: !uniqueBlockers.length,
      state,
      state_label: stateMeta[0],
      tone: stateMeta[1],
      sample_spend_floor: sampleSpendFloor,
      pause_roi_floor: pauseRoiFloor,
      resume_roi_floor: resumeRoiFloor,
      recheck_minutes: recheckMinutes,
      max_cycles: maxCycles,
      min_orders: minOrders,
      max_daily_loss: maxDailyLoss,
      blockers: uniqueBlockers,
      warnings: business.warnings,
      steps,
      decision,
      rule_summary: ruleSummary,
      execution_kind: "shadow_recovery_draft",
      human_approval_required: true,
      automatic_rebuild_enabled: false,
      platform_write_enabled: false,
    };
  }

  function normalizeSettings(value = {}) {
    const source = value && typeof value === "object" ? value : {};
    const mode = Object.hasOwn(MODE_DEFINITIONS, source.mode) ? source.mode : "protect";
    const maxBatch = finiteNumber(source.max_batch);
    return {
      schema_version: SCHEMA_VERSION,
      mode,
      max_batch: Math.max(1, Math.min(5, Math.trunc(maxBatch === null ? 3 : maxBatch))),
    };
  }

  function shadowEvidence(runtime = {}) {
    const evidence = runtime.shadow_evidence && typeof runtime.shadow_evidence === "object"
      ? runtime.shadow_evidence
      : {};
    const validatedDays = Math.max(0, finiteNumber(evidence.validated_days) || 0);
    const usefulRate = ratio(evidence.useful_rate);
    const readbackSuccessRate = ratio(evidence.readback_success_rate);
    const criticalIncidents = Math.max(0, finiteNumber(evidence.critical_incidents) || 0);
    const passed = validatedDays >= 7
      && usefulRate >= 0.7
      && readbackSuccessRate >= 0.99
      && criticalIncidents === 0;
    return {
      validated_days: validatedDays,
      useful_rate: usefulRate,
      readback_success_rate: readbackSuccessRate,
      critical_incidents: criticalIncidents,
      passed,
      label: passed
        ? "7 天影子验证已通过"
        : `${Math.min(7, validatedDays)}/7 有效日 · 回读 ${Math.round(readbackSuccessRate * 100)}%`,
    };
  }

  function opaqueTail(value) {
    const text = String(value || "").replace(/[^a-z0-9]/gi, "");
    return text ? text.slice(-6).toUpperCase() : "待确认";
  }

  function catalogContext(catalog = {}) {
    const source = catalog && typeof catalog === "object" ? catalog : {};
    const stores = Array.isArray(source.stores)
      ? source.stores
      : Array.isArray(source.accounts) ? source.accounts : [];
    const selectedStoreKey = String(source.selected_store_key || "");
    const selectedAccountKey = String(source.selected_account_key || "");
    const selectedStore = stores.find((item) => String(item.key || "") === selectedStoreKey) || null;
    return {
      store_count: Number(source.store_count || stores.length || 0),
      selected_store_key: selectedStoreKey,
      selected_account_key: selectedAccountKey,
      selected_store: selectedStore,
      store_label: selectedStore?.label || "等待打开抖店",
      account_label: selectedAccountKey ? `匿名账户 ${opaqueTail(selectedAccountKey)}` : "等待选择千川账户",
      source_label: selectedStore?.state_label || (selectedStore?.channel === "official_api" ? "官方 API" : "本地网页快照"),
    };
  }

  function candidateKind(candidate = {}) {
    const actionType = String(candidate.action_type || candidate.kind || "").toLowerCase();
    const current = finiteNumber(candidate.current_value);
    const target = finiteNumber(candidate.target_value);
    if (actionType.includes("pause") || actionType.includes("stop")) return "pause";
    if (current !== null && target !== null && target < current) return "decrease";
    if (current !== null && target !== null && target > current) return "increase";
    return "review";
  }

  function normalizeCandidate(candidate = {}) {
    const current = finiteNumber(candidate.current_value);
    const target = finiteNumber(candidate.target_value);
    const delta = current !== null && target !== null ? target - current : null;
    const deltaPct = delta !== null && current > 0 ? delta / current : null;
    const kind = candidateKind(candidate);
    const labels = {
      pause: "高风险暂停建议",
      decrease: "降预算草稿",
      increase: "加预算草稿",
      review: "人工复核草稿",
    };
    return {
      candidate_id: String(candidate.candidate_id || candidate.id || ""),
      plan_key: String(candidate.plan_key || candidate.plan_id || ""),
      plan_label: `计划 ${opaqueTail(candidate.plan_key || candidate.plan_id)}`,
      state: String(candidate.state || "shadow_candidate"),
      current_value: current,
      target_value: target,
      delta,
      delta_pct: deltaPct,
      kind,
      kind_label: labels[kind],
      reasons: Array.isArray(candidate.reasons) ? candidate.reasons.slice(0, 4).map(String) : [],
      executable: false,
    };
  }

  function activeCandidates(runtime = {}, pilot = {}, mode = "protect", limit = 3) {
    const primary = Array.isArray(pilot.candidates) ? pilot.candidates : [];
    const fallback = Array.isArray(runtime.candidates) ? runtime.candidates : [];
    const seen = new Set();
    const combined = [...primary, ...fallback].reverse().filter((candidate) => {
      const id = String(candidate?.candidate_id || candidate?.id || JSON.stringify(candidate));
      if (seen.has(id)) return false;
      seen.add(id);
      return !TERMINAL_CANDIDATE_STATES.has(String(candidate?.state || "").toLowerCase());
    }).map(normalizeCandidate);
    const filtered = mode === "protect"
      ? combined.filter((candidate) => ["decrease", "pause", "review"].includes(candidate.kind))
      : combined;
    return filtered.slice(0, limit);
  }

  function stableValue(value) {
    if (Array.isArray(value)) return value.map(stableValue);
    if (!value || typeof value !== "object") return value;
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, stableValue(value[key])]));
  }

  function configFingerprint(value) {
    const text = JSON.stringify(stableValue(value));
    let hash = 2166136261;
    for (let index = 0; index < text.length; index += 1) {
      hash ^= text.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return `cfg-${(hash >>> 0).toString(16).padStart(8, "0")}`;
  }

  function policyValue(runtime, localPlan, policyField, boundaryField) {
    const runtimeValue = finiteNumber(runtime?.policy?.[policyField]);
    if (runtimeValue !== null) return runtimeValue;
    return finiteNumber(localPlan?.boundaries?.[boundaryField]);
  }

  function packageFieldLabel(key, value) {
    if (value === null || value === undefined) return "待补齐";
    if (["daily_budget_cap", "max_daily_loss"].includes(key)) return `¥${Number(value).toFixed(2)}`;
    if (["max_single_adjustment_pct", "max_daily_adjustment_pct"].includes(key)) return `${Number(value)}%`;
    if (key === "cooldown_minutes") return `${Number(value)} 分钟`;
    if (key === "authorization_ttl_seconds") return `${Number(value)} 秒`;
    if (key === "max_daily_actions") return `${Number(value)} 次/日`;
    if (key === "max_batch") return `${Number(value)} 条/批`;
    return String(value);
  }

  function strategyPackagePreview(input = {}) {
    const view = input.view && typeof input.view === "object" ? input.view : deriveCenterView(input);
    const runtime = input.runtime && typeof input.runtime === "object" ? input.runtime : {};
    const localPlan = input.localPlan && typeof input.localPlan === "object" ? input.localPlan : {};
    const pilot = input.pilot && typeof input.pilot === "object" ? input.pilot : runtime.a2_pilot || {};
    const applied = input.appliedPackage && typeof input.appliedPackage === "object" ? input.appliedPackage : null;
    const mode = Object.hasOwn(MODE_DEFINITIONS, view.selected_mode) ? view.selected_mode : "protect";
    const definition = MODE_DEFINITIONS[mode];
    const hasLocalPlan = input.localPlan && typeof input.localPlan === "object" && Object.keys(input.localPlan).length > 0;
    const businessStrategy = hasLocalPlan
      ? deriveBusinessStrategy(localPlan, input.gate || {})
      : view.business_strategy || deriveBusinessStrategy(localPlan, input.gate || {});
    const recoveryPolicy = hasLocalPlan
      ? deriveRecoveryPolicy({ localPlan, runtime, gate: input.gate || {}, businessStrategy, realtimeTask: view.realtime_task })
      : view.recovery_policy || deriveRecoveryPolicy({
        localPlan, runtime, gate: input.gate || {}, businessStrategy, realtimeTask: view.realtime_task,
      });
    const guardrails = {
      max_batch: view.settings?.max_batch || 3,
      daily_budget_cap: policyValue(runtime, localPlan, "daily_budget_cap", "daily_budget_cap"),
      max_daily_loss: policyValue(runtime, localPlan, "max_daily_loss", "daily_loss_cap"),
      max_single_adjustment_pct: policyValue(runtime, localPlan, "max_single_adjustment_pct", "single_adjustment_cap"),
      max_daily_adjustment_pct: policyValue(runtime, localPlan, "max_daily_adjustment_pct", "daily_adjustment_cap"),
      max_daily_actions: policyValue(runtime, localPlan, "max_daily_actions", "daily_action_cap"),
      cooldown_minutes: policyValue(runtime, localPlan, "cooldown_minutes", "cooldown_minutes"),
      authorization_ttl_seconds: policyValue(runtime, localPlan, "authorization_ttl_seconds", "authorization_ttl_seconds"),
    };
    const requiredGuardrails = Object.keys(guardrails).filter((key) => key !== "max_batch");
    const missing = requiredGuardrails.filter((key) => guardrails[key] === null);
    const planKeys = [...new Set([
      String(pilot.plan_key || ""),
      ...(view.drafts || []).map((item) => String(item.plan_key || "")),
    ].filter(Boolean))].slice(0, view.settings?.max_batch || 3);
    const scope = {
      store_key: String(view.catalog?.selected_store_key || ""),
      account_key: String(view.catalog?.selected_account_key || ""),
    };
    const scopeFingerprint = configFingerprint(scope);
    const evidenceBasis = view.shadow?.passed ? "shadow_validated" : "operator_boundaries";
    const core = {
      schema_version: 1,
      mode,
      max_batch: guardrails.max_batch,
      scope_fingerprint: scopeFingerprint,
      plan_keys: planKeys,
      guardrails,
      allowed_action_families: [...definition.action_families],
      evidence_basis: evidenceBasis,
      business_strategy: {
        stage: businessStrategy.stage,
        stage_label: businessStrategy.stage_label,
        gmv_weight: businessStrategy.gmv_weight,
        profit_weight: businessStrategy.profit_weight,
        break_even_roi: businessStrategy.break_even_roi,
        observation_roi_floor: businessStrategy.observation_roi_floor,
        attribution_status: businessStrategy.attribution_status,
      },
      recovery_policy: {
        state: recoveryPolicy.ready ? "configured" : "incomplete",
        sample_spend_floor: recoveryPolicy.sample_spend_floor,
        pause_roi_floor: recoveryPolicy.pause_roi_floor,
        resume_roi_floor: recoveryPolicy.resume_roi_floor,
        recheck_minutes: recoveryPolicy.recheck_minutes,
        max_cycles: recoveryPolicy.max_cycles,
        min_orders: recoveryPolicy.min_orders,
        max_daily_loss: recoveryPolicy.max_daily_loss,
        human_approval_required: true,
        automatic_rebuild_enabled: false,
        platform_write_enabled: false,
      },
      execution_kind: "draft_only",
      platform_write_enabled: false,
    };
    const fingerprint = configFingerprint(core);
    const sameScope = applied && String(applied.scope_fingerprint || "") === scopeFingerprint;
    const previous = sameScope ? applied : null;
    const comparisonFields = [
      ["mode", "托管责任", definition.label, previous?.mode_label || MODE_DEFINITIONS[previous?.mode]?.label || "未配置"],
      ["max_batch", "单批上限", packageFieldLabel("max_batch", guardrails.max_batch), packageFieldLabel("max_batch", previous?.guardrails?.max_batch)],
      ["daily_budget_cap", "单日预算上限", packageFieldLabel("daily_budget_cap", guardrails.daily_budget_cap), packageFieldLabel("daily_budget_cap", previous?.guardrails?.daily_budget_cap)],
      ["max_daily_loss", "单日止损线", packageFieldLabel("max_daily_loss", guardrails.max_daily_loss), packageFieldLabel("max_daily_loss", previous?.guardrails?.max_daily_loss)],
      ["max_single_adjustment_pct", "单次调整上限", packageFieldLabel("max_single_adjustment_pct", guardrails.max_single_adjustment_pct), packageFieldLabel("max_single_adjustment_pct", previous?.guardrails?.max_single_adjustment_pct)],
      ["max_daily_actions", "单日动作上限", packageFieldLabel("max_daily_actions", guardrails.max_daily_actions), packageFieldLabel("max_daily_actions", previous?.guardrails?.max_daily_actions)],
      ["cooldown_minutes", "动作冷却", packageFieldLabel("cooldown_minutes", guardrails.cooldown_minutes), packageFieldLabel("cooldown_minutes", previous?.guardrails?.cooldown_minutes)],
      ["business_stage", "经营阶段", businessStrategy.stage_label, previous?.business_strategy?.stage_label || "未配置"],
      ["recovery_policy", "关停恢复规则", recoveryPolicy.rule_summary, previous?.recovery_policy?.state === "configured"
        ? `样本 ¥${Number(previous.recovery_policy.sample_spend_floor || 0).toFixed(2)} · 暂停 ROI＜${Number(previous.recovery_policy.pause_roi_floor || 0).toFixed(2)} · 恢复 ROI≥${Number(previous.recovery_policy.resume_roi_floor || 0).toFixed(2)} · 冷却 ${Number(previous.recovery_policy.recheck_minutes || 0)} 分钟 · 最多 ${Number(previous.recovery_policy.max_cycles || 0)} 轮`
        : "未配置"],
    ];
    const changes = comparisonFields.map(([key, label, after, before]) => ({ key, label, before, after, changed: before !== after }));
    const blockers = [];
    if (!scope.store_key || !scope.account_key || input.gate?.identity_ready !== true || input.gate?.identity_conflict === true) blockers.push("先确认唯一店铺与千川账户作用域");
    if (runtime.profile_validation?.business_profile_ready !== true) blockers.push("先补齐经营目标、成本口径和经营边界");
    if (missing.length) blockers.push(`还缺 ${missing.length} 项技术护栏`);
    if (mode === "scale" && view.production_write_ready !== true) blockers.push("放量包必须等待生产写合同、回读与回滚全部验收");
    if (!planKeys.length) blockers.push("尚未取得本次可管理的计划范围");
    const changed = !previous || previous.config_fingerprint !== fingerprint;
    const revision = previous ? Math.max(1, Number(previous.revision || 1)) + (changed ? 1 : 0) : 1;
    const packageValue = {
      ...core,
      package_name: `${definition.label} · ${view.catalog?.store_label || "当前店铺"}`,
      mode_label: definition.label,
      config_fingerprint: fingerprint,
      revision,
      source_label: evidenceBasis === "shadow_validated" ? "7 天影子反馈与经营边界" : "经营建档与人工边界",
      state: "local_draft",
    };
    return {
      package: packageValue,
      previous,
      changed,
      changes,
      changed_count: changes.filter((item) => item.changed).length,
      missing_guardrails: missing,
      blockers,
      can_apply: !blockers.length && changed,
      already_applied: Boolean(previous) && !changed,
      overwrite_warning: Boolean(previous) && changed,
      affected_plan_count: planKeys.length,
      evidence_basis: evidenceBasis,
      evidence_label: packageValue.source_label,
      business_strategy: businessStrategy,
      recovery_policy: recoveryPolicy,
      notice: previous && changed
        ? `保存后将把当前本机策略包从 v${previous.revision || 1} 更新为 v${revision}；不会修改千川。`
        : previous
          ? `本机策略包 v${previous.revision || 1} 与当前预览一致。`
          : "首次保存只建立本机策略包，不会修改千川。",
    };
  }

  function deriveRealtimeTask(runtime = {}, pilotValue = {}) {
    const pilot = pilotValue && typeof pilotValue === "object" ? pilotValue : runtime.a2_pilot || {};
    const candidates = Array.isArray(pilot.candidates) && pilot.candidates.length
      ? pilot.candidates.filter((item) => item && typeof item === "object")
      : Array.isArray(runtime.candidates) ? runtime.candidates.filter((item) => item && typeof item === "object") : [];
    const candidate = candidates.length ? candidates[candidates.length - 1] : null;
    const executions = Array.isArray(pilot.executions) ? pilot.executions.filter((item) => item && typeof item === "object") : [];
    const execution = candidate?.candidate_id
      ? [...executions].reverse().find((item) => item.candidate_id === candidate.candidate_id) || null
      : executions.length ? executions[executions.length - 1] : null;
    const state = String(execution?.state || candidate?.state || "idle");
    const labels = {
      idle: "等待候选", shadow_candidate: "等待人工复核", accepted_for_pilot: "等待本机模拟",
      rejected: "人工已拒绝", executing: "本机模拟中", awaiting_readback: "等待模拟回读",
      verified: "模拟回读通过", verified_stop_loss: "止损回读通过", failed: "模拟失败",
      readback_failed: "回读失败", expired: "候选已过期",
    };
    const steps = [
      { id: "candidate", label: "生成候选", state: candidate ? "complete" : "current" },
      { id: "review", label: "人工复核", state: "waiting" },
      { id: "simulation", label: "本机模拟", state: "waiting" },
      { id: "readback", label: "结果回读", state: "waiting" },
    ];
    if (state === "shadow_candidate") steps[1].state = "current";
    if (state === "rejected") steps[1].state = "failed";
    if (["accepted_for_pilot", "executing", "awaiting_readback", "verified", "verified_stop_loss", "failed", "readback_failed"].includes(state)) {
      steps[1].state = "complete";
      steps[2].state = ["accepted_for_pilot", "executing"].includes(state) ? "current" : ["failed"].includes(state) ? "failed" : "complete";
    }
    if (["awaiting_readback", "verified", "verified_stop_loss", "readback_failed"].includes(state)) {
      steps[3].state = state === "awaiting_readback" ? "current" : state === "readback_failed" ? "failed" : "complete";
    }
    if (state === "expired") steps[1].state = "failed";
    const writeAttempted = execution?.platform_write_attempted === true || candidate?.platform_write_attempted === true;
    return {
      state,
      state_label: labels[state] || "等待复核",
      candidate_id: String(candidate?.candidate_id || execution?.candidate_id || ""),
      plan_label: candidate ? `计划 ${opaqueTail(candidate.plan_key)}` : "尚无计划任务",
      execution_id: String(execution?.execution_id || ""),
      execution_kind: String(execution?.execution_kind || pilot.execution_kind || "simulation"),
      platform_write_attempted: writeAttempted,
      steps,
      note: writeAttempted
        ? "检测到平台写入标记，必须立即停止并人工核查。"
        : candidate
          ? "当前链路仅生成候选、本机模拟和模拟回读，未写入千川。"
          : "完成首次影子评估后，这里会显示候选到回读的实时进度。",
    };
  }

  function productionReady(input = {}) {
    const runtime = input.runtime || {};
    const pilot = input.pilot || runtime.a2_pilot || {};
    return input.writeEnabled === true
      && runtime.write_automation?.production_write_ready === true
      && pilot.live_execution_available === true
      && pilot.platform_write_enabled === true;
  }

  function modeCards(settings, gate, shadow, productionIsReady) {
    const identityAndDataReady = gate.identity_ready === true
      && gate.identity_conflict !== true
      && gate.metric_ready === true
      && gate.data_ready === true;
    return Object.values(MODE_DEFINITIONS).map((definition) => {
      let locked = false;
      let lockedReason = "";
      if (definition.id === "stabilize" && (!identityAndDataReady || !shadow.passed)) {
        locked = true;
        lockedReason = !identityAndDataReady
          ? "先完成店铺、账户、指标口径和数据校验"
          : "需达到 7 天、有效反馈率 70%、回读成功率 99%、零重大事故";
      }
      if (definition.id === "scale" && !productionIsReady) {
        locked = true;
        lockedReason = "官方写合同、应用权限、账户白名单、回读与回滚尚未全部验收";
      }
      return { ...definition, locked, locked_reason: lockedReason, selected: false };
    });
  }

  function primaryAction(context = {}) {
    const pathAction = context.candidatePath?.next_action || {};
    if (!context.catalog.selected_store_key || context.gate.identity_conflict === true) {
      return { id: "prepare_store", label: "打开抖店并巡店", detail: "系统会自动采用当前抖店页面，不需要手动识别或绑定。" };
    }
    if (!context.catalog.selected_account_key || context.gate.identity_ready !== true) {
      return { id: "accounts", label: "选择千川账户", detail: "进入投放工作台，选择这次要管理的账户。" };
    }
    if (context.gate.metric_ready !== true || context.gate.data_ready !== true) {
      return { id: "sync", label: "同步并校验当前千川页", detail: context.gate.next_step || "先取得可信指标和新鲜数据。" };
    }
    if (["goal", "costs", "boundaries", "evidence"].includes(pathAction.id)) {
      return { id: "profile", label: pathAction.label || "补齐经营建档", detail: pathAction.detail || "先补齐经营边界。" };
    }
    if (["bind_scope", "resolve_binding"].includes(pathAction.id)) {
      return { id: "accounts", label: "选择投放范围", detail: "重新选择本次投放使用的千川账户。" };
    }
    if (pathAction.id === "sync_chengfang") {
      return { id: "sync", label: pathAction.label || "同步当前乘方页", detail: pathAction.detail || "同步最新数据。" };
    }
    if (context.drafts.length) {
      return { id: "review", label: `复核 ${context.drafts.length} 条托管草稿`, detail: "草稿不会自动提交，先核对理由和边界。" };
    }
    if (pathAction.id === "shadow" || context.runtime.decision_automation?.running !== true) {
      return { id: "shadow", label: "开启 A1 影子托管", detail: "只生成候选，不打开、填写或提交千川页面。" };
    }
    if (pathAction.id === "evaluate") {
      return { id: "shadow", label: "运行首次影子评估", detail: "符合确定性规则才生成候选。" };
    }
    return { id: "review", label: "查看托管观察", detail: "继续积累影子证据和候选反馈。" };
  }

  function deriveCenterView(input = {}) {
    const runtime = input.runtime && typeof input.runtime === "object" ? input.runtime : {};
    const pilot = input.pilot && typeof input.pilot === "object" ? input.pilot : runtime.a2_pilot || {};
    const gate = input.gate && typeof input.gate === "object" ? input.gate : {};
    const settings = normalizeSettings(input.settings);
    const catalog = catalogContext(input.catalog);
    const shadow = shadowEvidence(runtime);
    const productionIsReady = productionReady({ ...input, runtime, pilot });
    const modes = modeCards(settings, gate, shadow, productionIsReady);
    const requested = modes.find((item) => item.id === settings.mode) || modes[0];
    const selectedMode = requested.locked ? "protect" : requested.id;
    modes.forEach((item) => { item.selected = item.id === selectedMode; });
    const drafts = activeCandidates(runtime, pilot, selectedMode, settings.max_batch);
    const action = primaryAction({ catalog, gate, runtime, candidatePath: input.candidatePath, drafts });
    const running = runtime.decision_automation?.running === true;
    const realtimeTask = deriveRealtimeTask(runtime, pilot);
    const businessStrategy = deriveBusinessStrategy(input.localPlan || {}, gate);
    const recoveryPolicy = deriveRecoveryPolicy({
      localPlan: input.localPlan || {}, runtime, gate, businessStrategy, realtimeTask,
    });

    let status = { tone: "warning", label: "等待准备", detail: action.detail };
    if (gate.identity_conflict === true) status = { tone: "danger", label: "账户冲突", detail: "托管与写能力已全部阻止。" };
    else if (!catalog.selected_store_key || !catalog.selected_account_key) status = { tone: "warning", label: "待准备", detail: "打开抖店并选择千川账户。" };
    else if (gate.identity_ready !== true || gate.metric_ready !== true || gate.data_ready !== true) status = { tone: "warning", label: "待校验", detail: gate.next_step || "数据尚不能用于托管判断。" };
    else if (drafts.length) status = { tone: "active", label: `${drafts.length} 条待复核`, detail: "已生成不可执行草稿。" };
    else if (running) status = { tone: "safe", label: "影子托管中", detail: "正在观察，未触发动作时保持不动。" };
    else status = { tone: "ready", label: "可开始影子托管", detail: "准备条件已满足，真实写入仍关闭。" };

    const selectedDefinition = MODE_DEFINITIONS[selectedMode];
    const modeNote = requested.locked
      ? `${requested.label}模式尚未解锁，已保持“守钱”：${requested.locked_reason}。`
      : selectedDefinition.description;
    return {
      schema_version: SCHEMA_VERSION,
      settings: { ...settings, mode: selectedMode },
      requested_mode: settings.mode,
      selected_mode: selectedMode,
      selected_mode_label: selectedDefinition.label,
      mode_note: modeNote,
      modes,
      status,
      catalog,
      shadow,
      production_write_ready: productionIsReady,
      production_write_label: productionIsReady ? "受控写入已验收" : "真实写入关闭",
      drafts,
      draft_count: drafts.length,
      running,
      primary_action: action,
      next_step: action.detail,
      realtime_task: realtimeTask,
      business_strategy: businessStrategy,
      recovery_policy: recoveryPolicy,
      safety_notice: "所有批量动作先生成不可执行草稿；真实提交仍须逐计划授权、执行前复核与结果回读。",
    };
  }

  function buildModeDraft(settingsValue = {}, viewValue = {}) {
    const settings = normalizeSettings(settingsValue);
    const view = viewValue && typeof viewValue === "object" ? viewValue : {};
    const mode = Object.hasOwn(MODE_DEFINITIONS, view.selected_mode) ? view.selected_mode : settings.mode;
    const definition = MODE_DEFINITIONS[mode];
    return {
      schema_version: SCHEMA_VERSION,
      kind: "autopilot_strategy_draft",
      execution_kind: "draft_only",
      platform_write_enabled: false,
      mode,
      mode_label: definition.label,
      store_key: String(view.catalog?.selected_store_key || ""),
      account_key: String(view.catalog?.selected_account_key || ""),
      candidate_ids: (Array.isArray(view.drafts) ? view.drafts : []).slice(0, settings.max_batch).map((item) => String(item.candidate_id || "")).filter(Boolean),
      allowed_action_families: [...definition.action_families],
      business_stage: view.business_strategy?.stage || "unconfigured",
      recovery_policy: {
        enabled: view.recovery_policy?.ready === true,
        sample_spend_floor: view.recovery_policy?.sample_spend_floor ?? null,
        pause_roi_floor: view.recovery_policy?.pause_roi_floor ?? null,
        resume_roi_floor: view.recovery_policy?.resume_roi_floor ?? null,
        recheck_minutes: view.recovery_policy?.recheck_minutes ?? null,
        max_cycles: view.recovery_policy?.max_cycles ?? null,
        human_approval_required: true,
        automatic_rebuild_enabled: false,
      },
      guardrails: {
        max_batch: settings.max_batch,
        per_plan_authorization_required: true,
        preflight_required: true,
        readback_required: true,
        automatic_platform_submit: false,
      },
    };
  }

  return Object.freeze({
    SCHEMA_VERSION,
    MODE_DEFINITIONS,
    BUSINESS_STAGE_DEFINITIONS,
    normalizeSettings,
    shadowEvidence,
    catalogContext,
    normalizeCandidate,
    activeCandidates,
    configFingerprint,
    deriveUnitEconomics,
    deriveBusinessStrategy,
    deriveRecoveryPolicy,
    strategyPackagePreview,
    deriveRealtimeTask,
    deriveCenterView,
    buildModeDraft,
  });
});
