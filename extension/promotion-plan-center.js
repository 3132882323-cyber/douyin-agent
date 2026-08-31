(function initPromotionPlanCenter(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.DianPromotionPlanCenter = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function promotionPlanCenterFactory() {
  "use strict";

  const PLAN_FRESHNESS_LIMIT_SECONDS = 30 * 60;

  const FEATURE_DEFINITIONS = Object.freeze([
    { id: "ai_pricing", label: "AI 托管调价", safety: "shadow" },
    { id: "schedule", label: "计划定时启停", safety: "draft" },
    { id: "auto_stop", label: "计划自动关停", safety: "draft" },
    { id: "auto_budget", label: "计划自动补预算", safety: "blocked" },
    { id: "scale", label: "一键起量", safety: "blocked" },
    { id: "ai_follow", label: "AI 托管追投", safety: "blocked" },
    { id: "follow_stop", label: "追投自动关停", safety: "blocked" },
    { id: "follow_budget", label: "追投自动补预算", safety: "blocked" },
    { id: "notify", label: "消息通知", safety: "local" },
  ]);

  const BINDING_PROFILES = Object.freeze({
    manual: {
      id: "manual",
      label: "人工复核",
      features: ["notify"],
      description: "只建立计划档案和本地提醒。",
    },
    protect: {
      id: "protect",
      label: "守钱托管",
      features: ["ai_pricing", "schedule", "auto_stop", "notify"],
      description: "生成影子调价、定时启停和自动关停草稿。",
    },
    stabilize: {
      id: "stabilize",
      label: "稳定经营",
      features: ["ai_pricing", "schedule", "auto_stop", "notify"],
      description: "以 ROI 稳定和观察窗口为先，不自动补预算或起量。",
    },
  });

  const PROMOTION_VIEW_ROUTES = Object.freeze({
    overview: Object.freeze({
      view: "overview",
      target_id: "promotion-plan-center",
      filters: Object.freeze({ mode: "all", plan_type: "all" }),
    }),
    standard: Object.freeze({
      view: "standard",
      target_id: "promotion-plan-center",
      filters: Object.freeze({ mode: "standard", plan_type: "all" }),
    }),
    full_domain: Object.freeze({
      view: "full_domain",
      target_id: "promotion-plan-center",
      filters: Object.freeze({ mode: "full_domain", plan_type: "all" }),
    }),
    chengfang: Object.freeze({
      view: "chengfang",
      target_id: "promotion-mode-workbench",
      filters: null,
    }),
  });

  function text(value) {
    return String(value ?? "").trim();
  }

  function finite(value) {
    if (value === null || value === undefined || String(value).trim() === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function normalizeMode(value) {
    const normalized = text(value).toLowerCase().replace(/-/g, "_");
    return ["standard", "full_domain", "chengfang", "suixintui", "unknown"].includes(normalized)
      ? normalized
      : "unknown";
  }

  function promotionViewRoute(value) {
    const view = text(value).toLowerCase().replace(/-/g, "_");
    const route = PROMOTION_VIEW_ROUTES[view] || PROMOTION_VIEW_ROUTES.overview;
    return {
      view: route.view,
      target_id: route.target_id,
      filters: route.filters ? { ...route.filters } : null,
    };
  }

  function deriveCollectionReceiptView(receipt = {}) {
    const labels = receipt?.labels && typeof receipt.labels === "object" ? receipt.labels : {};
    const collected = Math.max(0, finite(receipt?.collected_rows) ?? 0);
    const stableIds = Math.max(0, finite(receipt?.stable_id_rows) ?? 0);
    const confirmedModes = Math.max(0, finite(receipt?.mode_confirmed_rows) ?? 0);
    const platformTotalValue = finite(receipt?.platform_total);
    const platformTotal = platformTotalValue === null ? null : Math.max(0, platformTotalValue);
    const safeToClaimComplete = receipt?.safe_to_claim_complete === true;
    const warningLabels = Array.isArray(receipt?.warning_labels)
      ? receipt.warning_labels.map(text).filter(Boolean)
      : [];
    return {
      tone: safeToClaimComplete ? "complete" : collected ? "attention" : "unknown",
      safe_to_claim_complete: safeToClaimComplete,
      title: safeToClaimComplete
        ? (text(receipt?.status) === "confirmed_empty" ? "平台已确认暂无计划" : "计划读取范围已确认")
        : collected
          ? "已读取计划，覆盖范围待确认"
          : "尚未读取到可确认的计划",
      detail: [
        platformTotal === null ? "平台总数未确认" : `平台显示 ${platformTotal} 条`,
        `已读取 ${collected} 条`,
        `稳定 ID ${stableIds}/${collected}`,
        `模式确认 ${confirmedModes}/${collected}`,
      ].join(" · "),
      warning: safeToClaimComplete
        ? "只读采集凭证已通过；该凭证不会开启或代替千川生产写入授权。"
        : warningLabels[0] || text(labels.status) || "同步正确账户的计划列表并完成翻页后，再判断覆盖是否完整。",
    };
  }

  function selectCollectionReceipt(payload = {}, filters = {}) {
    const account = text(filters.account).toLowerCase() || "all";
    const mode = normalizeMode(filters.mode === "all" ? "all" : filters.mode);
    const planType = ["live", "product"].includes(text(filters.plan_type).toLowerCase())
      ? text(filters.plan_type).toLowerCase()
      : "all";
    const exactScope = account !== "all" && mode !== "unknown" && planType !== "all";
    const scope = { account, promotion_mode: mode, plan_type: planType };
    const globalReceipt = payload?.collection_receipt && typeof payload.collection_receipt === "object"
      ? payload.collection_receipt
      : {};
    if (!exactScope) {
      return { receipt: globalReceipt, source: "global", exact_scope: false, scope };
    }
    const receipts = Array.isArray(payload?.collection_receipts) ? payload.collection_receipts : [];
    const scoped = receipts.find((item) => {
      const scope = item?.scope && typeof item.scope === "object" ? item.scope : {};
      return text(scope.account_key).toLowerCase() === account
        && normalizeMode(scope.promotion_mode) === mode
        && text(scope.plan_type).toLowerCase() === planType;
    });
    return scoped
      ? { receipt: scoped, source: "scoped", exact_scope: true, scope }
      : { receipt: globalReceipt, source: "scope_missing", exact_scope: true, scope };
  }

  const SCOPE_RECOVERY_ACTION_PRIORITY = Object.freeze({
    reselect_verified_scope: 0,
    read_plan_id: 1,
    continue_scoped_collection: 2,
    refresh_verified_scope: 3,
    repair_plan_evidence: 4,
  });

  function selectScopeRecoveryAction(rows = []) {
    const candidates = (Array.isArray(rows) ? rows : [])
      .map((row) => row?.automation_next_action)
      .filter((item) => item && typeof item === "object" && text(item.code))
      .map((item) => ({
        code: text(item.code).toLowerCase(),
        label: text(item.label),
        detail: text(item.detail),
      }))
      .filter((item) => Object.prototype.hasOwnProperty.call(SCOPE_RECOVERY_ACTION_PRIORITY, item.code))
      .sort((left, right) => (
        SCOPE_RECOVERY_ACTION_PRIORITY[left.code] - SCOPE_RECOVERY_ACTION_PRIORITY[right.code]
      ));
    return candidates[0] || null;
  }

  function buildScopeRecoveryContract(selection = {}) {
    const scope = selection?.scope && typeof selection.scope === "object" ? selection.scope : {};
    const accountKey = text(scope.account || scope.account_key).toLowerCase();
    const promotionMode = normalizeMode(scope.promotion_mode || scope.mode);
    const planType = ["live", "product"].includes(text(scope.plan_type).toLowerCase())
      ? text(scope.plan_type).toLowerCase()
      : "";
    const target = planType === "live"
      ? {
          page_ids: ["qianchuan_live"],
          expected_page_types: ["qianchuan_live"],
          purpose: "promotion_plan_scope_recovery_live",
        }
      : planType === "product"
        ? {
            page_ids: ["qianchuan_campaigns"],
            expected_page_types: ["campaigns", "qianchuan_campaigns"],
            purpose: "promotion_plan_scope_recovery_product",
          }
        : { page_ids: [], expected_page_types: [], purpose: "" };
    const exactScope = selection?.exact_scope === true
      && accountKey !== ""
      && accountKey !== "all"
      && promotionMode !== "unknown"
      && Boolean(planType);
    return {
      schema_version: 1,
      operation: "read_only_scope_recovery",
      exact_scope: exactScope,
      can_collect: exactScope && target.page_ids.length === 1,
      account_key: accountKey,
      promotion_mode: promotionMode,
      plan_type: planType,
      page_ids: [...target.page_ids],
      expected_page_types: [...target.expected_page_types],
      purpose: target.purpose,
      verification_endpoint: "/qianchuan/plan-console",
      requires_store_confirmation: true,
      platform_write_enabled: false,
      automatic_submit: false,
    };
  }

  function timestampMs(value) {
    const numeric = finite(value);
    if (numeric !== null && numeric > 0) return numeric < 10_000_000_000 ? numeric * 1000 : numeric;
    const raw = text(value);
    if (!raw) return 0;
    const parsed = Date.parse(raw.includes("T") ? raw : raw.replace(" ", "T"));
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function deriveUnlinkedAccountGuard(payload = {}, catalog = {}, filters = {}, now = Date.now()) {
    const selection = selectCollectionReceipt(payload, filters);
    const receipt = selection.receipt && typeof selection.receipt === "object" ? selection.receipt : {};
    const receiptHealthy = selection.source === "scoped" && receipt.safe_to_claim_complete === true;
    const freshness = text(payload.freshness_status).toLowerCase();
    const dataAgeSeconds = finite(payload.data_age_seconds);
    const dataFresh = freshness === "fresh"
      && (dataAgeSeconds === null || (dataAgeSeconds >= 0 && dataAgeSeconds < PLAN_FRESHNESS_LIMIT_SECONDS));
    const currentHealthy = receiptHealthy && dataFresh;
    const currentDataAt = Math.max(
      0,
      timestampMs(payload.data_as_of_ms),
      ...(Array.isArray(payload.rows) ? payload.rows.map((row) => timestampMs(row?.captured_at_ms || row?.data_as_of_ms)) : [0]),
    );
    const receipts = Array.isArray(payload.collection_receipts) ? payload.collection_receipts : [];
    const planType = text(selection.scope?.plan_type).toLowerCase();
    const mode = normalizeMode(selection.scope?.promotion_mode);
    const unlinked = catalog?.link_required === true && Array.isArray(catalog?.unlinked_accounts)
      ? catalog.unlinked_accounts
      : [];
    const nowMs = timestampMs(now) || Date.now();
    const recentWindowMs = PLAN_FRESHNESS_LIMIT_SECONDS * 1000;
    const newerToleranceMs = 30 * 1000;
    const candidates = unlinked.map((account) => {
      const accountKey = text(account?.account_key || account?.key).toLowerCase();
      const lastSeenAt = timestampMs(account?.last_seen_at_ms || account?.last_seen);
      const recentlyActive = lastSeenAt > 0
        && lastSeenAt <= nowMs + 5 * 60 * 1000
        && nowMs - lastSeenAt <= recentWindowMs;
      const relatedReceipt = receipts.some((item) => {
        const scope = item?.scope && typeof item.scope === "object" ? item.scope : {};
        if (!accountKey || text(scope.account_key).toLowerCase() !== accountKey) return false;
        if (planType !== "all" && text(scope.plan_type).toLowerCase() !== planType) return false;
        if (mode !== "unknown" && normalizeMode(scope.promotion_mode) !== mode) return false;
        return Math.max(0, finite(item?.collected_rows) ?? 0, finite(item?.platform_total) ?? 0) > 0;
      });
      const clearlyNewer = currentDataAt > 0 && lastSeenAt > currentDataAt + newerToleranceMs;
      return {
        account,
        account_key: accountKey,
        last_seen_at_ms: lastSeenAt,
        recently_active: recentlyActive,
        related_to_scope: relatedReceipt,
        clearly_newer: clearlyNewer,
        should_block: recentlyActive && (
          (relatedReceipt && (!currentHealthy || clearlyNewer))
          || (!receiptHealthy && clearlyNewer)
        ),
      };
    }).filter((item) => item.should_block).sort((left, right) => right.last_seen_at_ms - left.last_seen_at_ms);
    const match = candidates[0] || null;
    return {
      blocked: Boolean(match),
      account: match?.account || null,
      account_key: match?.account_key || "",
      current_scope_healthy: currentHealthy,
      reason: match
        ? currentHealthy
          ? "检测到与当前范围相关、且晚于现有计划数据的新账户证据。"
          : "当前范围读取未完成，同时检测到最近活跃的未关联账户。"
        : "没有与当前读取动作相关的新未关联账户阻塞。",
    };
  }

  function normalizeLearningHealth(row = {}) {
    const labels = {
      learning: "学习中",
      learned: "学习完成",
      failed: "学习失败",
      none: "无学习期状态",
      unknown: "状态未知",
      unavailable: "暂不可读",
    };
    const raw = text(row.learning_phase).toLowerCase();
    const valid = Object.prototype.hasOwnProperty.call(labels, raw);
    const phase = valid ? raw : "unavailable";
    return {
      learning_phase: phase,
      learning_status_label: valid && text(row.learning_status_label)
        ? text(row.learning_status_label)
        : labels[phase],
    };
  }

  function normalizeLowEfficiencyHealth(row = {}) {
    const labels = {
      flagged: "平台标记低效",
      not_flagged_currently: "本次未命中",
      unknown: "暂不可读",
    };
    const raw = text(row.platform_low_efficiency).toLowerCase();
    const valid = Object.prototype.hasOwnProperty.call(labels, raw);
    const state = valid ? raw : "unknown";
    return {
      platform_low_efficiency: state,
      platform_low_efficiency_label: valid && text(row.platform_low_efficiency_label)
        ? text(row.platform_low_efficiency_label)
        : labels[state],
    };
  }

  function normalizeDiagnosticSource(row = {}) {
    const labels = {
      official_api: "官方 API",
      official_api_partial: "官方 API（部分不可用）",
      official_api_unknown: "官方 API（状态未知）",
      unknown: "来源未知",
      unavailable: "暂不可读",
    };
    const raw = text(row.diagnostic_source).toLowerCase();
    const valid = Object.prototype.hasOwnProperty.call(labels, raw);
    const source = valid ? raw : "unavailable";
    return {
      diagnostic_source: source,
      diagnostic_source_label: valid && text(row.diagnostic_source_label)
        ? text(row.diagnostic_source_label)
        : labels[source],
    };
  }

  function formatDataAge(value) {
    const seconds = finite(value);
    if (seconds === null) return "采集时间待确认";
    if (seconds < 0) return "采集时间异常（来自未来）";
    if (seconds < 60) return `${Math.floor(seconds)} 秒前`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
    return `${Math.floor(seconds / 86400)} 天前`;
  }

  function normalizeFreshness(row = {}) {
    const capturedAtMs = finite(row.captured_at_ms) ?? finite(row.data_as_of_ms) ?? 0;
    const dataAgeSeconds = finite(row.data_age_seconds);
    const declaredStatus = text(row.freshness_status).toLowerCase();
    const ageInvalid = dataAgeSeconds !== null && (dataAgeSeconds < 0 || dataAgeSeconds >= PLAN_FRESHNESS_LIMIT_SECONDS);
    const declaredStale = ["stale", "missing", "future"].includes(declaredStatus);
    const stale = row.stale !== false || declaredStale || ageInvalid;
    const status = stale
      ? (declaredStatus === "future" || (dataAgeSeconds !== null && dataAgeSeconds < 0) ? "future" : dataAgeSeconds === null && !capturedAtMs ? "missing" : "stale")
      : "fresh";
    const labels = {
      fresh: "数据新鲜",
      stale: "数据已过期",
      missing: "采集时间缺失",
      future: "采集时间异常",
    };
    return {
      captured_at_ms: capturedAtMs,
      data_as_of_ms: finite(row.data_as_of_ms) ?? capturedAtMs,
      data_age_seconds: dataAgeSeconds,
      data_age_label: formatDataAge(dataAgeSeconds),
      freshness_status: status,
      freshness_label: labels[status],
      stale,
    };
  }

  function normalizePlan(row = {}) {
    const planKey = text(row.plan_key || row.id || row.plan_id || row.plan_name);
    const blockers = Array.isArray(row.binding_blockers) ? row.binding_blockers.map(text).filter(Boolean) : [];
    const freshness = normalizeFreshness(row);
    const stale = freshness.stale;
    if (stale && !blockers.includes("计划数据已过期，请重新同步")) blockers.push("计划数据已过期，请重新同步");
    const learningHealth = normalizeLearningHealth(row);
    const lowEfficiencyHealth = normalizeLowEfficiencyHealth(row);
    const diagnosticSource = normalizeDiagnosticSource(row);
    return {
      ...row,
      ...learningHealth,
      ...lowEfficiencyHealth,
      ...diagnosticSource,
      ...freshness,
      plan_key: planKey,
      plan_id: text(row.plan_id),
      plan_name: text(row.plan_name) || "未命名计划",
      account_key: text(row.account_key),
      account_label: text(row.account_label) || "账户待识别",
      store_key: text(row.store_key || row.store?.key).toLowerCase(),
      promotion_mode: normalizeMode(row.promotion_mode),
      plan_type: ["live", "product"].includes(text(row.plan_type)) ? text(row.plan_type) : "unknown",
      delivery_status: text(row.delivery_status) || "状态待识别",
      budget: finite(row.budget),
      spend: finite(row.spend),
      roi: finite(row.roi),
      orders: finite(row.orders),
      quality_score: finite(row.quality_score) ?? 0,
      stale,
      binding_blockers: blockers,
      eligible_for_local_binding: row.eligible_for_local_binding === true && blockers.length === 0 && !stale,
      health: {
        learning_phase: learningHealth.learning_phase,
        learning_status_label: learningHealth.learning_status_label,
        platform_low_efficiency: lowEfficiencyHealth.platform_low_efficiency,
        platform_low_efficiency_label: lowEfficiencyHealth.platform_low_efficiency_label,
        diagnostic_source: diagnosticSource.diagnostic_source,
        diagnostic_source_label: diagnosticSource.diagnostic_source_label,
      },
    };
  }

  function normalizeFilters(filters = {}) {
    const features = Array.isArray(filters.features)
      ? [...new Set(filters.features.map(text).filter((item) => FEATURE_DEFINITIONS.some((feature) => feature.id === item)))]
      : [];
    return {
      account: text(filters.account) || "all",
      mode: text(filters.mode) || "all",
      plan_type: text(filters.plan_type) || "all",
      status: text(filters.status) || "all",
      binding: text(filters.binding) || "all",
      query: text(filters.query).toLowerCase(),
      features,
    };
  }

  function rowBinding(plan, bindings = {}) {
    const binding = bindings && typeof bindings === "object" ? bindings[plan.plan_key] : null;
    if (!binding || typeof binding !== "object") return null;
    return {
      ...binding,
      profile: BINDING_PROFILES[binding.profile] ? binding.profile : "manual",
      features: Array.isArray(binding.features) ? binding.features.filter((item) => FEATURE_DEFINITIONS.some((feature) => feature.id === item)) : [],
      local_only: true,
      platform_write_enabled: false,
      identity_matches: (!text(binding.plan_id) || text(binding.plan_id) === plan.plan_id)
        && (!text(binding.account_key) || text(binding.account_key) === plan.account_key),
    };
  }

  function deriveView(payload = {}, filters = {}, bindings = {}) {
    const normalizedFilters = normalizeFilters(filters);
    const payloadStoreKey = text(payload.store_key || payload.selected_store_key).toLowerCase();
    const allRows = (Array.isArray(payload.rows) ? payload.rows : []).map((row) => {
      const plan = normalizePlan({ ...row, store_key: text(row?.store_key || row?.store?.key) || payloadStoreKey });
      const savedBinding = rowBinding(plan, bindings);
      const bindingBlockers = [...plan.binding_blockers];
      if (savedBinding && !savedBinding.identity_matches) bindingBlockers.push("本地绑定与当前账户或计划身份不一致");
      const bindingAvailable = plan.eligible_for_local_binding && !plan.stale && (!savedBinding || savedBinding.identity_matches);
      const binding = bindingAvailable ? savedBinding : null;
      return {
        ...plan,
        binding_blockers: [...new Set(bindingBlockers)],
        eligible_for_local_binding: bindingAvailable,
        binding,
        saved_binding: savedBinding,
        binding_paused: Boolean(savedBinding && !bindingAvailable),
        binding_state: binding ? "bound" : bindingAvailable ? "unbound" : "blocked",
      };
    });
    const rows = allRows.filter((row) => {
      if (normalizedFilters.account !== "all" && row.account_key !== normalizedFilters.account) return false;
      if (normalizedFilters.mode !== "all" && row.promotion_mode !== normalizedFilters.mode) return false;
      if (normalizedFilters.plan_type !== "all" && row.plan_type !== normalizedFilters.plan_type) return false;
      if (normalizedFilters.status !== "all" && row.delivery_status !== normalizedFilters.status) return false;
      if (normalizedFilters.binding !== "all" && row.binding_state !== normalizedFilters.binding) return false;
      if (normalizedFilters.query) {
        const haystack = `${row.plan_name} ${row.plan_id} ${row.account_label}`.toLowerCase();
        if (!haystack.includes(normalizedFilters.query)) return false;
      }
      if (normalizedFilters.features.length && !normalizedFilters.features.every((feature) => row.binding?.features?.includes(feature))) return false;
      return true;
    });
    const spendRows = rows.filter((row) => row.spend !== null);
    const roiRows = rows.filter((row) => row.roi !== null && row.spend !== null && row.spend > 0);
    const totalSpend = spendRows.reduce((sum, row) => sum + row.spend, 0);
    const weightedRoi = roiRows.length && roiRows.reduce((sum, row) => sum + row.spend, 0) > 0
      ? roiRows.reduce((sum, row) => sum + row.roi * row.spend, 0) / roiRows.reduce((sum, row) => sum + row.spend, 0)
      : null;
    const accounts = new Map();
    (Array.isArray(payload.accounts) ? payload.accounts : []).forEach((account) => {
      const key = text(account.account_key || account.key);
      if (key) accounts.set(key, { key, label: text(account.account_label || account.label || account.display_name) || `账户 ${key.slice(-4)}` });
    });
    allRows.forEach((row) => {
      if (row.account_key && !accounts.has(row.account_key)) accounts.set(row.account_key, { key: row.account_key, label: row.account_label });
    });
    const declaredFreshnessStatus = text(payload.freshness_status).toLowerCase();
    const validFreshnessStatuses = ["fresh", "stale", "missing", "future"];
    const freshnessStatus = allRows.some((row) => row.freshness_status === "future") || declaredFreshnessStatus === "future"
      ? "future"
      : allRows.some((row) => row.stale) || declaredFreshnessStatus === "stale"
        ? "stale"
        : validFreshnessStatuses.includes(declaredFreshnessStatus)
          ? declaredFreshnessStatus
          : allRows.length ? "fresh" : "missing";
    const freshnessLabels = { fresh: "数据新鲜", stale: "数据已过期", missing: "采集时间缺失", future: "采集时间异常" };
    return {
      safe: payload.safe !== false,
      rows,
      all_rows: allRows,
      filters: normalizedFilters,
      accounts: [...accounts.values()],
      features: FEATURE_DEFINITIONS,
      profiles: Object.values(BINDING_PROFILES),
      summary: {
        visible: rows.length,
        total: allRows.length,
        spend: spendRows.length ? Math.round(totalSpend * 100) / 100 : null,
        weighted_roi: weightedRoi === null ? null : Math.round(weightedRoi * 100) / 100,
        orders: Math.round(rows.reduce((sum, row) => sum + (row.orders ?? 0), 0)),
        bound: rows.filter((row) => row.binding_state === "bound").length,
        binding_paused: rows.filter((row) => row.binding_paused).length,
        blocked: rows.filter((row) => row.binding_state === "blocked").length,
        stale: rows.filter((row) => row.stale).length,
        learning: rows.filter((row) => row.learning_phase === "learning").length,
        low_efficiency: rows.filter((row) => row.platform_low_efficiency === "flagged").length,
      },
      data_as_of_ms: finite(payload.data_as_of_ms) ?? 0,
      oldest_data_as_of_ms: finite(payload.oldest_data_as_of_ms) ?? 0,
      data_age_seconds: finite(payload.data_age_seconds),
      latest_data_age_seconds: finite(payload.latest_data_age_seconds),
      data_age_label: formatDataAge(payload.data_age_seconds),
      freshness_status: freshnessStatus,
      freshness_label: freshnessLabels[freshnessStatus],
      freshness: {
        status: freshnessStatus,
        label: freshnessLabels[freshnessStatus],
        can_manage: freshnessStatus === "fresh" && !allRows.some((row) => row.stale),
        data_as_of_ms: finite(payload.data_as_of_ms) ?? 0,
        oldest_data_as_of_ms: finite(payload.oldest_data_as_of_ms) ?? 0,
        data_age_seconds: finite(payload.data_age_seconds),
        latest_data_age_seconds: finite(payload.latest_data_age_seconds),
        data_age_label: formatDataAge(payload.data_age_seconds),
      },
      notice: text(payload.notice) || "计划中心只读取本机快照；绑定和规则只保存在本机。",
      platform_write_enabled: false,
      automatic_batch_submit: false,
    };
  }

  function createBinding(planValue = {}, options = {}, now = Date.now()) {
    const plan = normalizePlan(planValue);
    if (!plan.plan_key) throw new Error("计划身份缺失，无法建立本地绑定");
    if (!plan.eligible_for_local_binding) throw new Error(plan.binding_blockers[0] || "账户、计划 ID 或数据质量尚未通过绑定检查");
    const profile = BINDING_PROFILES[options.profile] ? options.profile : "manual";
    return {
      schema_version: 1,
      binding_id: `binding-${plan.plan_key}`,
      plan_key: plan.plan_key,
      plan_id: plan.plan_id,
      plan_name: plan.plan_name,
      account_key: plan.account_key,
      promotion_mode: plan.promotion_mode,
      plan_type: plan.plan_type,
      profile,
      profile_label: BINDING_PROFILES[profile].label,
      features: [...BINDING_PROFILES[profile].features],
      created_at: Number(now),
      updated_at: Number(now),
      local_only: true,
      platform_write_enabled: false,
      automatic_submit: false,
    };
  }

  function targetIds(value) {
    const source = Array.isArray(value) ? value : text(value).split(/[\s,，;；]+/);
    return [...new Set(source.map(text).filter(Boolean))];
  }

  function createBatchDraft(input = {}, now = Date.now()) {
    const accountKey = text(input.account_key);
    const mode = normalizeMode(input.mode);
    const planType = ["live", "product"].includes(text(input.plan_type)) ? text(input.plan_type) : "";
    const targets = targetIds(input.target_ids);
    const budget = finite(input.budget);
    const roiTarget = finite(input.roi_target);
    const namePrefix = text(input.name_prefix).slice(0, 30);
    const errors = [];
    if (!accountKey) errors.push("请选择已经核对的广告账户");
    if (!["full_domain", "chengfang", "suixintui"].includes(mode)) errors.push("请选择投放模式");
    if (!planType) errors.push("请选择直播或商品计划");
    if (!targets.length) errors.push("至少填写一个推广对象 ID");
    if (targets.length > 20) errors.push("一次最多生成 20 个本地草稿");
    if (budget === null || budget <= 0) errors.push("单计划预算必须大于 0");
    if (roiTarget === null || roiTarget <= 0) errors.push("ROI 目标必须大于 0");
    if (!namePrefix) errors.push("请填写计划名称前缀");
    if (errors.length) throw new Error(errors.join("；"));
    const createdAt = Number(now);
    const batchId = `batch-${createdAt}-${targets.length}`;
    const modeLabels = { full_domain: "全域", chengfang: "乘方", suixintui: "随心推" };
    const typeLabels = { live: "直播", product: "商品" };
    return {
      schema_version: 1,
      batch_id: batchId,
      account_key: accountKey,
      mode,
      mode_label: modeLabels[mode],
      plan_type: planType,
      plan_type_label: typeLabels[planType],
      budget,
      roi_target: roiTarget,
      long_term: input.long_term !== false,
      smart_coupon: input.smart_coupon === true,
      star_material: input.star_material === true,
      created_at: createdAt,
      local_only: true,
      platform_write_enabled: false,
      automatic_submit: false,
      items: targets.map((targetId, index) => ({
        draft_id: `${batchId}-${String(index + 1).padStart(2, "0")}`,
        target_id: targetId,
        plan_name: `${namePrefix}-${modeLabels[mode]}${typeLabels[planType]}-${String(index + 1).padStart(2, "0")}`,
        budget,
        roi_target: roiTarget,
        status: "local_draft",
      })),
      notice: "只生成本地草稿；未调用千川写接口，也不会自动提交。",
    };
  }

  return {
    FEATURE_DEFINITIONS,
    BINDING_PROFILES,
    PROMOTION_VIEW_ROUTES,
    PLAN_FRESHNESS_LIMIT_SECONDS,
    formatDataAge,
    promotionViewRoute,
    deriveCollectionReceiptView,
    selectCollectionReceipt,
    selectScopeRecoveryAction,
    buildScopeRecoveryContract,
    deriveUnlinkedAccountGuard,
    normalizeFreshness,
    normalizeFilters,
    normalizePlan,
    deriveView,
    createBinding,
    createBatchDraft,
  };
});
