const assert = require("assert");
const fs = require("fs");
const policy = require("./connection-guide-policy.js");

assert.deepStrictEqual(policy.guideView({ collapsed: true, next_upgrade: { id: "sync_qianchuan", optional: true } }), {
  collapsed: true,
  actionId: "sync_qianchuan",
  optional: true,
  deferred: false,
  operationalState: "",
  operationalLabel: "",
  currentlyReady: false,
});
assert.equal(policy.guideView({ next_upgrade: { id: "quick_scan" } }).collapsed, false);
assert.equal(policy.guideView({ next_upgrade: { id: "sync_qianchuan" } }, { qianchuanDeferred: true }).collapsed, true);
assert.equal(policy.guideView({ next_upgrade: { id: "sync_qianchuan" } }, { qianchuanDeferred: true }).deferred, true);
const connectedButStale = policy.guideView({
  level: "L3",
  level_label: "投放已连接",
  next_upgrade: { id: "refresh_core_data" },
  operational: { state: "refresh_required", state_label: "连接完成，数据待刷新", core_data_fresh: false },
});
assert.equal(connectedButStale.operationalLabel, "连接完成，数据待刷新");
assert.equal(connectedButStale.currentlyReady, false, "historically connected must not mean currently ready");
assert.equal(policy.guideView({
  operational: { state: "data_fresh", state_label: "当前数据可用于判断", core_data_fresh: true },
}).currentlyReady, true);
assert.equal(policy.automationSurface({ selectedAccountKey: "", itemCount: 5 }), "off");
assert.equal(policy.automationSurface({ selectedAccountKey: "", itemCount: 5, deferred: true }), "deferred");
assert.equal(policy.automationSurface({ selectedAccountKey: "account_v1_x", itemCount: 0 }), "no_plans");
assert.equal(policy.automationSurface({ selectedAccountKey: "account_v1_x", itemCount: 2 }), "candidates");
assert.equal(policy.automationStep("idle"), "proposal");
assert.equal(policy.automationStep("ready_for_final_confirmation"), "authorization");
assert.equal(policy.automationStep("verified"), "result");
assert.equal(policy.bindingReview({ selectedStoreKey: "store-a", unlinkedAccounts: [{ account_key: "account-a" }], currentActionId: "sync_qianchuan" }), true);
assert.equal(policy.bindingReview({ selectedStoreKey: "", unlinkedAccounts: [{ account_key: "account-a" }] }), false);
assert.equal(policy.bindingReview({ selectedStoreKey: "store-a", unlinkedAccounts: [] }), false);
assert.equal(policy.bindingReview({ selectedStoreKey: "store-a", unlinkedAccounts: [{ account_key: "account-a" }], currentActionId: "view_first_task" }), false);
assert.equal(policy.bindingReview({ selectedStoreKey: "store-a", unlinkedAccounts: [{ account_key: "account-a" }], currentActionId: "refresh_core_data" }), false);
assert.equal(policy.bindingReview({ selectedStoreKey: "store-a", unlinkedAccounts: [{ account_key: "account-a" }], currentActionId: "sync_qianchuan", qianchuanDeferred: true }), false);

// Guard against UI-only branches being pasted into the automation renderer with
// variables that only exist in renderPriorityReminder.
const sidepanel = fs.readFileSync(require.resolve("./sidepanel.js"), "utf8");
const readinessStart = sidepanel.indexOf("function renderAutomationReadiness");
const readinessEnd = sidepanel.indexOf("\nfunction ", readinessStart + 1);
const readinessSource = sidepanel.slice(readinessStart, readinessEnd);
assert.ok(readinessStart >= 0 && readinessEnd > readinessStart);
assert.ok(!readinessSource.includes("context.state"));
assert.ok(!readinessSource.includes("panel.hidden"));

console.log("connection guide policy tests passed");
