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

console.log("chengfang local planning tests passed");
