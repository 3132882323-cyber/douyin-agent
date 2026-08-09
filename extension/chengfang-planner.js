(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.DianChengfangPlanner = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const GOALS = Object.freeze({
    profit: "保利润", stable: "稳规模", growth: "冲增长",
    new_product: "测新品", clearance: "清库存", live_burst: "直播爆发",
  });
  const REQUIRED_FIELDS = Object.freeze([
    "price", "product_cost", "commission_rate", "platform_fee_rate",
    "merchant_discount", "fulfillment_cost", "refund_rate", "advertising_cost",
  ]);
  const LIMITS = Object.freeze({ money: 100000000, rate: 100 });

  function parseField(raw, kind) {
    if (raw === "" || raw === null || raw === undefined) return { status: "missing", value: null };
    const value = Number(raw);
    if (!Number.isFinite(value)) return { status: "invalid", value: null, message: "请输入有效数字" };
    const max = kind === "rate" ? LIMITS.rate : LIMITS.money;
    if (value < 0 || value > max) return { status: "invalid", value: null, message: `请输入 0–${max} 之间的数值` };
    return { status: "present", value };
  }

  function calculate(input) {
    const parsed = {
      price: parseField(input.price, "money"),
      product_cost: parseField(input.product_cost, "money"),
      commission_rate: parseField(input.commission_rate, "rate"),
      platform_fee_rate: parseField(input.platform_fee_rate, "rate"),
      merchant_discount: parseField(input.merchant_discount, "money"),
      fulfillment_cost: parseField(input.fulfillment_cost, "money"),
      refund_rate: parseField(input.refund_rate, "rate"),
      advertising_cost: parseField(input.advertising_cost, "money"),
    };
    const missing = REQUIRED_FIELDS.filter((key) => parsed[key].status === "missing");
    const invalid = REQUIRED_FIELDS.filter((key) => parsed[key].status === "invalid");
    if (missing.length || invalid.length) return { status: invalid.length ? "invalid" : "incomplete", parsed, missing, invalid };

    const price = parsed.price.value;
    if (price <= 0) return { status: "invalid", parsed, missing: [], invalid: ["price"], reason: "售价必须大于 0" };
    const retainedRevenue = price * (1 - parsed.refund_rate.value / 100);
    const commission = retainedRevenue * parsed.commission_rate.value / 100;
    const platformFee = retainedRevenue * parsed.platform_fee_rate.value / 100;
    const nonAdCost = parsed.product_cost.value + commission + platformFee + parsed.merchant_discount.value + parsed.fulfillment_cost.value;
    const maxAdSpend = retainedRevenue - nonAdCost;
    if (maxAdSpend <= 0) {
      return { status: "loss_before_ads", parsed, retained_revenue: retainedRevenue, non_ad_cost: nonAdCost, max_ad_spend: maxAdSpend };
    }
    const breakEvenRoi = retainedRevenue / maxAdSpend;
    const contributionMargin = maxAdSpend - parsed.advertising_cost.value;
    return {
      status: "ready", parsed, retained_revenue: retainedRevenue, non_ad_cost: nonAdCost,
      advertising_cost: parsed.advertising_cost.value, contribution_margin: contributionMargin,
      max_ad_spend: maxAdSpend, break_even_roi: breakEvenRoi,
      assumptions: "单件测算；退款商品不保留收入；佣金与平台费用按退款后预计收入计提；未计入的费用会令结果偏乐观。",
    };
  }

  const QUALIFICATION_DIMENSIONS = Object.freeze({
    profit: { label: "利润空间", fields: ["contribution_margin"] },
    refund: { label: "退款风险", fields: ["refund_rate"] },
    inventory: { label: "库存覆盖", fields: ["inventory_days"] },
    fulfillment: { label: "履约能力", fields: ["fulfillment_rate"] },
    creative: { label: "素材供给", fields: ["creative_count"] },
    data_quality: { label: "数据质量", fields: ["data_completeness", "data_freshness_minutes"] },
  });

  function presentNumber(raw, kind = "money") {
    if (kind === "signed") {
      if (raw === "" || raw === null || raw === undefined) return null;
      const value = Number(raw);
      return Number.isFinite(value) && Math.abs(value) <= LIMITS.money ? value : null;
    }
    return parseField(raw, kind).status === "present" ? Number(raw) : null;
  }

  function assessQualification(input = {}) {
    const values = {
      contribution_margin: presentNumber(input.contribution_margin, "signed"), refund_rate: presentNumber(input.refund_rate, "rate"),
      inventory_days: presentNumber(input.inventory_days), fulfillment_rate: presentNumber(input.fulfillment_rate, "rate"),
      creative_count: presentNumber(input.creative_count), data_completeness: presentNumber(input.data_completeness, "rate"),
      data_freshness_minutes: presentNumber(input.data_freshness_minutes),
    };
    const dimensions = Object.entries(QUALIFICATION_DIMENSIONS).map(([key, config]) => {
      const missing = config.fields.filter((field) => values[field] === null);
      if (missing.length) return { key, label: config.label, status: "insufficient", score: null, missing, evidence: "缺少可验证数据" };
      let score = 0;
      if (key === "profit") score = values.contribution_margin > 0 ? 100 : 0;
      if (key === "refund") score = Math.max(0, 100 - values.refund_rate * 2);
      if (key === "inventory") score = Math.min(100, values.inventory_days / 14 * 100);
      if (key === "fulfillment") score = values.fulfillment_rate;
      if (key === "creative") score = Math.min(100, values.creative_count / 5 * 100);
      if (key === "data_quality") score = Math.max(0, Math.min(values.data_completeness, 100 - values.data_freshness_minutes / 3));
      return { key, label: config.label, status: "assessed", score: Math.round(score), missing: [], evidence: config.fields.map((field) => `${field}=${values[field]}`).join("；") };
    });
    const missing = [...new Set(dimensions.flatMap((item) => item.missing))];
    return { status: missing.length ? "incomplete" : "ready", score: missing.length ? null : Math.round(dimensions.reduce((sum, item) => sum + item.score, 0) / dimensions.length), dimensions, missing };
  }

  function identifyBottlenecks(input = {}) {
    const qualification = assessQualification(input);
    if (qualification.status !== "ready") return { status: "insufficient", primary: null, secondary: [], next_steps: qualification.missing.map((field) => `补齐 ${field}`) };
    const candidates = [];
    const add = (key, title, severity, evidence, level, action) => candidates.push({ key, title, severity, evidence, evidence_level: level, action });
    const n = (key) => Number(input[key]);
    if (n("contribution_margin") <= 0) add("profit", "利润空间不足", 100, `贡献毛利 ${n("contribution_margin").toFixed(2)}`, "confirmed", "先校准成本和目标，停止扩量判断");
    if (n("data_completeness") < 80) add("data_quality", "数据完整度不足", 95, `完整度 ${n("data_completeness")}%`, "confirmed", "补齐数据后再形成确定性建议");
    if (n("refund_rate") >= 30) add("refund", "退款风险偏高", 90, `退款率 ${n("refund_rate")}%`, "confirmed", "定位高退款商品与素材");
    if (n("inventory_days") < 7) add("inventory", "库存覆盖不足", 80, `库存覆盖 ${n("inventory_days")} 天`, "confirmed", "补库存或限制探索规模");
    if (n("fulfillment_rate") < 95) add("fulfillment", "履约能力不足", 70, `履约率 ${n("fulfillment_rate")}%`, "likely", "先修复发货与售后承接");
    if (n("creative_count") < 3) add("creative", "素材供给不足", 60, `可用素材 ${n("creative_count")} 条`, "likely", "先补充至少 3 条差异化素材");
    candidates.sort((a, b) => b.severity - a.severity);
    return { status: candidates.length ? "found" : "clear", primary: candidates[0] || null, secondary: candidates.slice(1, 3), next_steps: [] };
  }

  function buildShadowRecord({ goal, recommendation, evidence, observation_started_at } = {}) {
    const missing = [!GOALS[goal] && "goal", !String(recommendation || "").trim() && "recommendation", (!Array.isArray(evidence) || !evidence.length) && "evidence"].filter(Boolean);
    if (missing.length) return { status: "incomplete", record: null, missing };
    const created = Number(observation_started_at) || Date.now();
    return { status: "ready", record: { schema_version: 1, id: `cf-shadow-${created}`, local_only: true, execution_allowed: false, goal, recommendation: String(recommendation).trim(), evidence, created_at: created, readbacks: { "2h": null, "24h": null, "3d": null, "7d": null } } };
  }

  function buildScenarios(result) {
    if (!result || result.status !== "ready") return [];
    return [
      { key: "conservative", label: "保守", target_roi: result.break_even_roi * 1.2, spend_ratio: 0.75, risk: "可能牺牲成交规模；仍需观察素材与承接变化。" },
      { key: "baseline", label: "基准", target_roi: result.break_even_roi * 1.1, spend_ratio: 0.9, risk: "接近当前成本假设，费用遗漏或退款上升会压缩利润。" },
      { key: "aggressive", label: "进取", target_roi: result.break_even_roi * 1.03, spend_ratio: 1, risk: "安全垫很薄，仅适合小额验证，不代表平台能够实现。" },
    ].map((item) => ({ ...item, max_ad_spend: result.max_ad_spend * item.spend_ratio }));
  }

  function emptyDiagnostics(hasRealData) {
    if (hasRealData) return { eligibility: "待计算", bottleneck: "待诊断", shadow: "待生成" };
    return {
      eligibility: "先同步商品利润、库存、退款与内容供给，再计算乘方资格。",
      bottleneck: "缺少真实经营数据，当前不判断增长瓶颈。",
      shadow: "完成字段合同和连续数据采集后，才能生成只读影子建议。",
    };
  }

  return { GOALS, REQUIRED_FIELDS, QUALIFICATION_DIMENSIONS, parseField, calculate, buildScenarios, assessQualification, identifyBottlenecks, buildShadowRecord, emptyDiagnostics };
});
