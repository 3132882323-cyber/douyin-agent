const assert = require("assert");
const fs = require("fs");
const policy = require("./connection-guide-policy.js");

assert.deepStrictEqual(policy.guideView({ collapsed: true, next_upgrade: { id: "sync_qianchuan", optional: true } }), {
  collapsed: true,
  actionId: "sync_qianchuan",
  optional: true,
  deferred: false,
});
assert.equal(policy.guideView({ next_upgrade: { id: "quick_scan" } }).collapsed, false);
assert.equal(policy.guideView({ next_upgrade: { id: "sync_qianchuan" } }, { qianchuanDeferred: true }).collapsed, true);
assert.equal(policy.guideView({ next_upgrade: { id: "sync_qianchuan" } }, { qianchuanDeferred: true }).deferred, true);
assert.equal(policy.automationSurface({ selectedAccountKey: "", itemCount: 5 }), "off");
assert.equal(policy.automationSurface({ selectedAccountKey: "", itemCount: 5, deferred: true }), "deferred");
assert.equal(policy.automationSurface({ selectedAccountKey: "account_v1_x", itemCount: 0 }), "no_plans");
assert.equal(policy.automationSurface({ selectedAccountKey: "account_v1_x", itemCount: 2 }), "candidates");
assert.equal(policy.automationStep("idle"), "proposal");
assert.equal(policy.automationStep("ready_for_final_confirmation"), "authorization");
assert.equal(policy.automationStep("verified"), "result");

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
