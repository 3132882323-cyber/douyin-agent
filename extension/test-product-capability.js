const assert = require("assert");
const fs = require("fs");
const path = require("path");
const capability = require("./product-capability.js");

const report = capability.normalize({
  level: "plan_read_only",
  stages: [
    { id: "data", label: "店铺与数据", status: "ready", evidence: ["店铺已确认"] },
    { id: "plans", label: "计划可见", status: "ready" },
    { id: "identity", label: "计划身份", status: "blocked", summary: "计划 ID 缺失" },
  ],
  next_action: { stage_id: "identity", label: "重新同步", target: "promotion-plan-center", reason: "计划数据已过期" },
  truth: { available_now: ["只读筛选"], not_available_yet: ["真实投放调整"], execution_enabled: false },
});

assert.equal(report.level, "plan_read_only");
assert.equal(report.ready_count, 2);
assert.equal(report.next_action.target, "promotion-plan-center");
assert.equal(report.truth.execution_enabled, false);
assert.match(report.truth.not_available_yet.join("；"), /真实投放/);

const cascaded = capability.normalize({
  level: "closed_loop",
  stages: [
    { id: "data", status: "blocked" },
    { id: "plans", status: "ready" },
    { id: "identity", status: "ready" },
  ],
  truth: { execution_enabled: true },
});
assert.equal(cascaded.level, "setup_required");
assert.deepEqual(cascaded.stages.map((item) => item.status), ["blocked", "inactive", "inactive"]);
assert.equal(cascaded.truth.execution_enabled, false);

const firstRun = capability.derive({
  onboarding: { store_confirmed: false },
  operation_context: { analysis_allowed: false },
});
assert.equal(firstRun.stages[0].label, "经营数据");
assert.equal(firstRun.stages[0].target, "scan-card");
assert.equal(firstRun.stages[0].action_label, "打开抖店并开始");
assert.equal(firstRun.next_action.target, "scan-card");
assert.equal(firstRun.next_action.label, "打开抖店并开始");

const stale = capability.derive({
  selected_account_key: "acct-a",
  onboarding: { store_confirmed: true },
  operation_context: { analysis_allowed: true, state_label: "可诊断" },
  plan_console: {
    rows: [{ account_key: "acct-a", plan_id: "", stale: true, eligible_for_local_binding: true, binding_blockers: ["计划数据已过期，请重新同步"] }],
    summary: { total: 1, binding_ready: 1, stale: 1 },
    freshness_status: "stale",
  },
  automation_readiness: { summary: { preflight_ready: 1 }, execution_enabled: true },
  preflight: { state: "blocked", execution_enabled: true },
  effectiveness: { items: [{ account_key: "acct-other", status: "effective" }], summary: { evaluated: 1 } },
});
assert.equal(stale.level, "diagnosis_ready");
assert.deepEqual(stale.stages.map((item) => item.status), ["ready", "blocked", "inactive", "inactive", "inactive"]);
assert.equal(stale.next_action.target, "promotion-plan-center");
assert.equal(stale.next_action.label, "重新读取当前计划");
assert.match(stale.stages[1].summary, /没有可用的新计划.*1 条历史快照.*重新读取当前计划/);
assert.match(stale.stages[1].evidence.join("；"), /历史快照：1 条/);
assert.match(stale.stages[1].evidence.join("；"), /计划 ID 缺失：1 条/);
assert.doesNotMatch(stale.truth.available_now.join("；"), /执行结果/);

const missingIdentity = capability.derive({
  selected_account_key: "acct-a",
  onboarding: { store_confirmed: true },
  operation_context: { analysis_allowed: true, state_label: "可诊断" },
  plan_console: {
    rows: [{ account_key: "acct-a", plan_id: "[已隐藏]", stale: false, eligible_for_local_binding: false }],
    summary: { total: 1, binding_ready: 0, stale: 0 },
    freshness_status: "fresh",
  },
});
assert.equal(missingIdentity.stages[1].status, "ready");
assert.match(missingIdentity.stages[1].evidence.join("；"), /计划 ID 缺失：1 条/);
assert.equal(missingIdentity.stages[2].status, "blocked");

function readyInputs(overrides = {}) {
  return {
    selected_account_key: "acct-a",
    onboarding: { store_confirmed: true },
    operation_context: { analysis_allowed: true, state_label: "可诊断" },
    plan_console: {
      rows: [{ account_key: "acct-a", stale: false, eligible_for_local_binding: true }],
      summary: { total: 1, binding_ready: 1, stale: 0 },
      freshness_status: "fresh",
    },
    automation_readiness: { summary: { preflight_ready: 1 }, execution_enabled: false },
    preflight: { state: "idle", execution_enabled: false },
    effectiveness: { items: [] },
    ...overrides,
  };
}

const blockedPreflight = capability.derive(readyInputs({
  preflight: { state: "blocked", execution_enabled: true },
  automation_readiness: { summary: { preflight_ready: 1 }, execution_enabled: true },
}));
assert.equal(blockedPreflight.level, "local_management");
assert.equal(blockedPreflight.stages[3].status, "blocked");
assert.equal(blockedPreflight.truth.execution_enabled, false);

const ambiguousAccount = capability.derive(readyInputs({
  selected_account_key: "",
  plan_console: {
    rows: [
      { account_key: "acct-a", stale: false, eligible_for_local_binding: true },
      { account_key: "acct-b", stale: false, eligible_for_local_binding: true },
    ],
    summary: { total: 2, binding_ready: 2, stale: 0 },
    freshness_status: "fresh",
  },
}));
assert.equal(ambiguousAccount.level, "plan_read_only");
assert.equal(ambiguousAccount.stages[2].status, "blocked");
assert.match(ambiguousAccount.stages[2].evidence.join("；"), /锁定当前千川账户/);

const crossAccountReadback = capability.derive(readyInputs({
  effectiveness: { items: [{ account_key: "acct-other", status: "effective" }], summary: { evaluated: 1 } },
}));
assert.equal(crossAccountReadback.level, "supervised_ready");
assert.equal(crossAccountReadback.stages[4].status, "inactive");
assert.doesNotMatch(crossAccountReadback.truth.available_now.join("；"), /结果复盘/);

const matchingReadback = capability.derive(readyInputs({
  effectiveness: { items: [{ account_key: "ACCT-A", status: "effective" }] },
}));
assert.equal(matchingReadback.level, "closed_loop");
assert.equal(matchingReadback.stages[4].status, "ready");
assert.match(matchingReadback.stages[4].summary, /当前账户/);

const summaryOnlyReadback = capability.derive(readyInputs({
  effectiveness: { items: [], summary: { evaluated: 99, effective: 99 } },
}));
assert.equal(summaryOnlyReadback.level, "supervised_ready");
assert.equal(summaryOnlyReadback.stages[4].status, "inactive");

const html = fs.readFileSync(path.join(__dirname, "sidepanel.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "sidepanel.css"), "utf8");
const sidepanel = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");
assert.match(html, /id="product-capability-card"[\s\S]*?id="product-capability-stages"[\s\S]*?id="product-capability-next"[^>]*data-workspace-target="scan-card"/);
assert.doesNotMatch(html, /id="product-capability-next"[^>]*data-workspace-target="connection-guide"/);
assert.match(html, /<script src="promotion-operation-log\.js"><\/script>\s*<script src="product-capability\.js"><\/script>\s*<script src="simple-experience\.js"><\/script>/);
assert.match(css, /\.product-capability-stage\.blocked/);
assert.match(sidepanel, /function renderProductCapability\(inputs = \{\}\)/);
assert.match(sidepanel, /const nextTarget = next\.target === "connection-guide" \? "scan-card" : next\.target/);
assert.match(sidepanel, /button\.dataset\.workspaceTarget = nextTarget/);
assert.match(sidepanel, /renderProductCapability\(\{[\s\S]*?selected_account_key:/);

console.log("product capability tests passed");
