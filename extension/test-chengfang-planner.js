const assert = require("node:assert/strict");
const planner = require("./chengfang-planner.js");

const complete = { price: "100", product_cost: "35", commission_rate: "10", platform_fee_rate: "0", merchant_discount: "5", fulfillment_cost: "5", refund_rate: "10", advertising_cost: "20" };
const ready = planner.calculate(complete);
assert.equal(ready.status, "ready");
assert.equal(ready.retained_revenue, 90);
assert.equal(ready.non_ad_cost, 54);
assert.equal(ready.max_ad_spend, 36);
assert.equal(ready.break_even_roi, 2.5);
assert.equal(ready.contribution_margin, 16);
assert.equal(planner.buildScenarios(ready).length, 3);

assert.equal(planner.calculate({ ...complete, platform_fee_rate: "" }).status, "incomplete");
assert.equal(planner.calculate({ ...complete, platform_fee_rate: "0" }).status, "ready");
assert.equal(planner.calculate({ ...complete, refund_rate: "101" }).status, "invalid");
assert.equal(planner.calculate({ ...complete, price: "0" }).status, "invalid");
assert.equal(planner.calculate({ ...complete, product_cost: "100" }).status, "loss_before_ads");
assert.deepEqual(planner.buildScenarios({ status: "incomplete" }), []);
assert.match(planner.emptyDiagnostics(false).shadow, /字段合同/);

const evidence = { contribution_margin: "16", refund_rate: "10", inventory_days: "21", fulfillment_rate: "98", creative_count: "6", data_completeness: "95", data_freshness_minutes: "20" };
const qualification = planner.assessQualification(evidence);
assert.equal(qualification.status, "ready");
assert.equal(typeof qualification.score, "number");
assert.equal(planner.assessQualification({ ...evidence, creative_count: "" }).score, null);
assert.match(planner.assessQualification({ ...evidence, creative_count: "" }).missing.join(","), /creative_count/);

const bottleneck = planner.identifyBottlenecks({ ...evidence, contribution_margin: "-1", refund_rate: "35", inventory_days: "2", creative_count: "1" });
assert.equal(bottleneck.primary.key, "profit");
assert.ok(bottleneck.secondary.length <= 2);
assert.equal(planner.identifyBottlenecks({ ...evidence, inventory_days: "" }).status, "insufficient");

const shadow = planner.buildShadowRecord({ goal: "profit", recommendation: "保持只读并修复利润", evidence: ["贡献毛利为负"], observation_started_at: 123 });
assert.equal(shadow.record.execution_allowed, false);
assert.deepEqual(Object.keys(shadow.record.readbacks), ["2h", "24h", "3d", "7d"]);
assert.equal(planner.buildShadowRecord({ goal: "profit" }).status, "incomplete");

const boundaries = { minimum_contribution_margin: "5", daily_budget_cap: "5000", daily_loss_cap: "300", refund_rate_ceiling: "25", inventory_days_floor: "7", single_adjustment_cap: "10" };
const validBoundaries = planner.validateBoundaries(boundaries);
assert.equal(validBoundaries.status, "ready");
assert.equal(validBoundaries.execution_allowed, false);
assert.equal(planner.validateBoundaries({ ...boundaries, daily_loss_cap: "" }).status, "incomplete");
assert.equal(planner.validateBoundaries({ ...boundaries, daily_budget_cap: "0" }).status, "invalid");
assert.equal(planner.validateBoundaries({ ...boundaries, refund_rate_ceiling: "101" }).status, "invalid");

const insufficientDecision = planner.buildDecisionBrief({ goal: "profit", calculation: ready, boundaries: validBoundaries, qualification, bottlenecks: { status: "clear", primary: null }, readiness: { identity_ready: false, metric_ready: false, data_ready: false } });
assert.equal(insufficientDecision.action, "先补齐店铺与账户身份");
assert.equal(insufficientDecision.execution_allowed, false);
const profitDecision = planner.buildDecisionBrief({ goal: "profit", calculation: { ...ready, contribution_margin: 1 }, boundaries: validBoundaries, qualification, bottlenecks: { status: "clear", primary: null }, readiness: { identity_ready: true, metric_ready: true, data_ready: true } });
assert.equal(profitDecision.level, "danger");
assert.match(profitDecision.action, /不扩量/);

const started = planner.buildShadowProgram({ enabled: true, started_at: 1000, days: [] }, 1000);
assert.equal(started.status, "active");
assert.equal(started.execution_allowed, false);
const dayOne = planner.appendDailyShadow(started, { date: "2026-08-10", goal: "profit", recommendation: "补齐真实数据", evidence: ["字段合同未验证"] }, 2000);
assert.equal(dayOne.days.length, 1);
assert.deepEqual(Object.keys(dayOne.days[0].readbacks), ["2h", "24h", "3d", "7d"]);
assert.equal(planner.appendDailyShadow(dayOne, { date: "2026-08-10", goal: "profit", recommendation: "重复", evidence: ["重复"] }, 3000).days.length, 1);
assert.equal(planner.buildShadowProgram({ enabled: false, days: dayOne.days }, 3000).days.length, 1);

console.log("chengfang local planning tests passed");
