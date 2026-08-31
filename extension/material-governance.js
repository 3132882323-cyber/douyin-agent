(function initMaterialGovernance(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.DianMaterialGovernance = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createMaterialGovernance() {
  "use strict";

  const SCHEMA_VERSION = 1;
  const RULE_VERSION = "dian-material-governance-v1";
  const RULES = Object.freeze([
    Object.freeze({
      id: "protect_verified_winner", label: "保护已验证胜出素材", bucket: "protect",
      description: "ROI 达标且至少形成 3 笔成交的素材优先进入保护池。",
      action: "保留原素材，生成单变量变体候选",
    }),
    Object.freeze({
      id: "retire_high_cost_zero_conversion", label: "高消耗零转化停测", bucket: "retire",
      description: "只有高质量证据、达到消耗门槛且仍为零成交或零 ROI 时才进入停测候选。",
      action: "生成停测候选，禁止自动删除",
    }),
    Object.freeze({
      id: "retest_hook", label: "钩子问题单变量复测", bucket: "retest",
      description: "点击率不足时只替换前 3 秒钩子，保持商品、人群、预算和时段一致。",
      action: "进入钩子复测池",
    }),
    Object.freeze({
      id: "retest_conversion", label: "承接问题单变量复测", bucket: "retest",
      description: "已有点击但未成交时保留钩子，只调整卖点、利益点或直播承接。",
      action: "进入承接复测池",
    }),
    Object.freeze({
      id: "test_unverified", label: "未验证素材小额测试", bucket: "test",
      description: "尚未测试或仅被平台标记为高潜的素材先进入可比的小额测试。",
      action: "进入小额测试池",
    }),
    Object.freeze({
      id: "observe_remaining", label: "其余素材继续观察", bucket: "observe",
      description: "证据不足或未命中前述规则时保持观察，不因播放量或单次波动下结论。",
      action: "等待完整转化窗口",
    }),
  ]);

  const BUCKET_LABELS = Object.freeze({
    protect: "保护复用",
    retire: "停测候选",
    retest: "单变量复测",
    test: "小额测试",
    observe: "继续观察",
  });

  function finiteNumber(value) {
    if (value === "" || value === null || value === undefined) return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function stableValue(value) {
    if (Array.isArray(value)) return value.map(stableValue);
    if (!value || typeof value !== "object") return value;
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, stableValue(value[key])]));
  }

  function fingerprint(value) {
    const text = JSON.stringify(stableValue(value));
    let hash = 2166136261;
    for (let index = 0; index < text.length; index += 1) {
      hash ^= text.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return `mat-${(hash >>> 0).toString(16).padStart(8, "0")}`;
  }

  function normalizePolicy(creative = {}) {
    const source = creative.governance_policy && typeof creative.governance_policy === "object"
      ? creative.governance_policy
      : {};
    const minSpend = finiteNumber(source.min_spend_for_action);
    const roiTarget = finiteNumber(source.roi_target);
    const minOrders = finiteNumber(source.min_orders_for_scale);
    return {
      min_spend_for_action: minSpend !== null && minSpend > 0 ? minSpend : null,
      roi_target: roiTarget !== null && roiTarget > 0 ? roiTarget : null,
      min_orders_for_scale: minOrders !== null && minOrders >= 1 ? Math.trunc(minOrders) : null,
      data_window: String(source.data_window || "current_page_filter"),
      data_window_label: String(source.data_window_label || "当前千川页面筛选范围"),
      source_label: String(source.source_label || "本地 Agent 经营设置与当前素材快照"),
      automatic_delete_enabled: false,
      platform_write_enabled: false,
    };
  }

  function ruleMatches(ruleId, item = {}, policy = {}) {
    const status = String(item.status || "");
    const level = String(item.level || "");
    const stage = String(item.funnel_stage || "");
    const confidence = String(item.confidence || "");
    const evidence = item.evidence && typeof item.evidence === "object" ? item.evidence : {};
    const spend = finiteNumber(evidence.spend);
    const roi = finiteNumber(evidence.roi);
    const orders = finiteNumber(evidence.orders);
    if (ruleId === "protect_verified_winner") {
      return policy.roi_target !== null && policy.min_orders_for_scale !== null
        && status === "可复制放量"
        && roi !== null && roi >= policy.roi_target
        && orders !== null && orders >= policy.min_orders_for_scale;
    }
    if (ruleId === "retire_high_cost_zero_conversion") {
      return policy.min_spend_for_action !== null
        && level === "high"
        && confidence === "high"
        && spend !== null && spend >= policy.min_spend_for_action
        && ((orders !== null && orders === 0) || (roi !== null && roi === 0));
    }
    if (ruleId === "retest_hook") return stage === "hook";
    if (ruleId === "retest_conversion") return stage === "conversion";
    if (ruleId === "test_unverified") return stage === "untested" || status === "高潜素材";
    return ruleId === "observe_remaining";
  }

  function classifyMaterials(creative = {}, policy = normalizePolicy(creative)) {
    const videos = Array.isArray(creative.videos) ? creative.videos.filter((item) => item && typeof item === "object") : [];
    const ruleHits = Object.fromEntries(RULES.map((rule) => [rule.id, []]));
    const items = videos.map((item, index) => {
      const matchedRule = RULES.find((rule) => ruleMatches(rule.id, item, policy)) || RULES[RULES.length - 1];
      const evidence = item.evidence && typeof item.evidence === "object" ? item.evidence : {};
      const classified = {
        source_index: index,
        source_id: String(item.id || ""),
        name: String(item.name || `第 ${index + 1} 条素材`).slice(0, 160),
        bucket: matchedRule.bucket,
        bucket_label: BUCKET_LABELS[matchedRule.bucket],
        rule_id: matchedRule.id,
        rule_label: matchedRule.label,
        action: matchedRule.action,
        confidence: String(item.confidence || "medium"),
        evidence: {
          spend: finiteNumber(evidence.spend),
          roi: finiteNumber(evidence.roi),
          orders: finiteNumber(evidence.orders),
          ctr: finiteNumber(evidence.ctr),
        },
      };
      ruleHits[matchedRule.id].push(classified);
      return classified;
    });
    const rules = RULES.map((rule, index) => ({
      ...rule,
      priority: index + 1,
      hit_count: ruleHits[rule.id].length,
      examples: ruleHits[rule.id].slice(0, 3).map((item) => item.name),
    }));
    const counts = Object.fromEntries(Object.keys(BUCKET_LABELS).map((bucket) => [bucket, items.filter((item) => item.bucket === bucket).length]));
    return { items, rules, counts, total: items.length };
  }

  function packageComparison(policy, previous = null) {
    const before = previous?.policy || {};
    const rows = [
      ["data_window", "统计窗口", before.data_window_label || "未配置", policy.data_window_label],
      ["min_spend_for_action", "停测消耗门槛", before.min_spend_for_action == null ? "未配置" : `¥${Number(before.min_spend_for_action).toFixed(2)}`, policy.min_spend_for_action == null ? "待补齐" : `¥${Number(policy.min_spend_for_action).toFixed(2)}`],
      ["roi_target", "胜出 ROI 目标", before.roi_target == null ? "未配置" : Number(before.roi_target).toFixed(2), policy.roi_target == null ? "待补齐" : Number(policy.roi_target).toFixed(2)],
      ["min_orders_for_scale", "最小成交样本", before.min_orders_for_scale == null ? "未配置" : `${before.min_orders_for_scale} 单`, policy.min_orders_for_scale == null ? "待补齐" : `${policy.min_orders_for_scale} 单`],
      ["automatic_delete_enabled", "自动删除", before.automatic_delete_enabled ? "开启" : "关闭", "关闭"],
    ];
    return rows.map(([key, label, oldValue, newValue]) => ({ key, label, before: oldValue, after: newValue, changed: oldValue !== newValue }));
  }

  function deriveGovernance(input = {}) {
    const creative = input.creative && typeof input.creative === "object" ? input.creative : {};
    const scope = input.scope && typeof input.scope === "object" ? input.scope : {};
    const applied = input.appliedPackage && typeof input.appliedPackage === "object" ? input.appliedPackage : null;
    const policy = normalizePolicy(creative);
    const classification = classifyMaterials(creative, policy);
    const scopeFingerprint = fingerprint({ store_key: String(scope.store_key || ""), account_key: String(scope.account_key || "") });
    const core = {
      schema_version: SCHEMA_VERSION,
      rule_version: RULE_VERSION,
      scope_fingerprint: scopeFingerprint,
      policy,
      rules: RULES.map((rule, index) => ({
        id: rule.id, priority: index + 1, label: rule.label, bucket: rule.bucket, action: rule.action,
      })),
      execution_kind: "classification_preview_only",
      automatic_delete_enabled: false,
      platform_write_enabled: false,
    };
    const configFingerprint = fingerprint(core);
    const previous = applied && applied.scope_fingerprint === scopeFingerprint ? applied : null;
    const changed = !previous || previous.config_fingerprint !== configFingerprint;
    const revision = previous ? Math.max(1, Number(previous.revision || 1)) + (changed ? 1 : 0) : 1;
    const blockers = [];
    if (!scope.store_key || !scope.account_key) blockers.push("先确认唯一店铺与千川账户作用域");
    if (creative.data_status !== "ready" || !classification.total) blockers.push("先同步包含素材明细的千川视频库");
    if (policy.min_spend_for_action === null) blockers.push("缺少停测消耗门槛");
    if (policy.roi_target === null) blockers.push("缺少素材胜出 ROI 目标");
    if (policy.min_orders_for_scale === null) blockers.push("缺少最小成交样本");
    const packageValue = {
      ...core,
      package_name: "本店素材治理",
      config_fingerprint: configFingerprint,
      revision,
      state: "local_draft",
      source_label: policy.source_label,
    };
    const changes = packageComparison(policy, previous);
    return {
      package: packageValue,
      previous,
      changed,
      already_applied: Boolean(previous) && !changed,
      can_apply: !blockers.length && changed,
      overwrite_warning: Boolean(previous) && changed,
      blockers,
      changes,
      changed_count: changes.filter((item) => item.changed).length,
      classification,
      affected_material_count: classification.total,
      protected_count: classification.counts.protect || 0,
      retire_candidate_count: classification.counts.retire || 0,
      retest_count: (classification.counts.retest || 0) + (classification.counts.test || 0),
      observe_count: classification.counts.observe || 0,
      first_match_notice: "规则按优先级从上到下首次命中；同一素材只进入一个池。",
      notice: previous && changed
        ? `保存后将把本机素材治理包从 v${previous.revision || 1} 更新为 v${revision}；不会修改或删除千川素材。`
        : previous
          ? `本机素材治理包 v${previous.revision || 1} 与当前规则一致。`
          : "首次保存只记录规则和阈值，不保存素材名称，也不会修改千川。",
    };
  }

  return Object.freeze({
    SCHEMA_VERSION,
    RULE_VERSION,
    RULES,
    BUCKET_LABELS,
    fingerprint,
    normalizePolicy,
    classifyMaterials,
    deriveGovernance,
  });
});
