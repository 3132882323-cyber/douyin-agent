"use strict";

const assert = require("assert");
const orchestrator = require("./journey-orchestrator.js");

function storeReady(overrides = {}) {
  return {
    online: true,
    lane: "store",
    onboarding: { store_confirmed: true, store_id: "store-a", store_name: "A 店" },
    connectionGuide: { operational: { state: "data_fresh", core_data_fresh: true } },
    operationContext: { analysis_allowed: true },
    scan: { status: "fresh" },
    ops: { items: [] },
    effectiveness: { items: [] },
    ...overrides,
  };
}

function adsReady(overrides = {}) {
  return {
    ...storeReady(),
    lane: "ads",
    selectedAccountKey: "acct-a",
    planConsole: {
      freshness_status: "fresh",
      rows: [{ account_key: "acct-a", stale: false, eligible_for_local_binding: true }],
    },
    preflight: { boundaries_ready: true, state: "idle" },
    ...overrides,
  };
}

// 1. Offline is the absolute top priority, even when another session is active.
const offline = orchestrator.deriveJourney(adsReady({
  online: false,
  operationContext: { state: "identity_conflict", analysis_allowed: false },
  preflight: { state: "executing", boundaries_ready: true },
}));
assert.equal(offline.state, "offline");
assert.equal(offline.primaryAction.id, "repair_agent");
assert.equal(offline.stages[0].status === "blocked" || offline.stages[0].status === "pending", true);

// 2. Identity conflict outranks an executing authorization.
const conflict = orchestrator.deriveJourney(adsReady({
  operationContext: { state: "identity_conflict", blockers: ["页面店铺与已确认店铺不一致"] },
  preflight: { state: "executing", boundaries_ready: true },
}));
assert.equal(conflict.state, "identity_blocked");
assert.equal(conflict.primaryAction.id, "start_store_scan");
assert.equal(conflict.primaryAction.kind, "scan");
assert.equal(conflict.primaryAction.targetId, "scan-card");
assert.equal(conflict.primaryAction.label, "重新打开并巡店");
assert.equal(conflict.blockers[0].code, "identity_conflict");
assert.match(conflict.blockers[0].message, /避免混用数据/);

// 3. Active readback outranks an unconfirmed store, as required by the state contract.
const readback = orchestrator.deriveJourney({
  online: true,
  lane: "ads",
  onboarding: { store_confirmed: false },
  preflight: { state: "awaiting_readback", account_key: "acct-active" },
});
assert.equal(readback.state, "readback_in_progress");
assert.equal(readback.primaryAction.targetId, "promotion-operation-log");

const unscopedAuthorization = orchestrator.deriveJourney({
  online: true,
  lane: "ads",
  onboarding: { store_confirmed: true },
  operationContext: { analysis_allowed: true },
  preflight: { state: "executing" },
});
assert.equal(unscopedAuthorization.state, "identity_blocked");
assert.equal(unscopedAuthorization.primaryAction.id, "start_store_scan");
assert.match(unscopedAuthorization.detail, /避免混用数据/);

// 4. A store must be confirmed before either lane can proceed.
const noStore = orchestrator.deriveJourney(storeReady({ onboarding: { store_confirmed: false } }));
assert.equal(noStore.state, "store_unconfirmed");
assert.equal(noStore.primaryAction.id, "start_store_scan");
assert.equal(noStore.primaryAction.kind, "scan");
assert.equal(noStore.primaryAction.targetId, "scan-card");
assert.equal(noStore.primaryAction.label, "打开抖店并开始");
assert.deepEqual(noStore.primaryAction.pageIds, ["overview", "orders", "products", "shelf"]);
assert.equal(noStore.stages.find((item) => item.id === "store").label, "打开抖店");

// 5. Explicit stale data wins over analysis_allowed=true.
const stale = orchestrator.deriveJourney(storeReady({
  scan: { status: "fresh", ready: true },
  onboarding: { store_confirmed: true, status: "completed", data_ready: true },
  connectionGuide: {
    level: "L3",
    operational: { state: "refresh_required", core_data_fresh: false, refresh_page_ids: ["overview", "products"] },
  },
}));
assert.equal(stale.state, "data_required");
assert.equal(stale.primaryAction.kind, "scan");
assert.equal(stale.primaryAction.id, "refresh_core_data");
assert.deepEqual(stale.primaryAction.pageIds, ["overview", "products"]);
assert.match(stale.detail, /过期/);

// 6. The store lane can finish without any Qianchuan account or plan data.
const storeComplete = orchestrator.deriveJourney(storeReady());
assert.equal(storeComplete.state, "complete");
assert.equal(storeComplete.lane, "store");
assert.equal(storeComplete.scope.accountKey, "");
assert.equal(storeComplete.stages.some((item) => item.id === "account"), false);

// 7. A concrete daily task becomes the single next action.
const taskReady = orchestrator.deriveJourney(storeReady({
  ops: { items: [{ id: "task-1", title: "修复缺货商品", state: "todo", action: "打开货架并补齐库存" }] },
}));
assert.equal(taskReady.state, "today_task_ready");
assert.equal(taskReady.primaryAction.id, "start_today_task");
assert.match(taskReady.title, /缺货/);
const productionTaskShape = orchestrator.deriveJourney(storeReady({
  ops: { all_tasks: [{ id: "task-2", title: "复核商品承接", status: "todo" }] },
}));
assert.equal(productionTaskShape.state, "today_task_ready");
assert.match(productionTaskShape.title, /商品承接/);

// 8. The ads lane fails closed when no account is selected.
const noAccount = orchestrator.deriveJourney(adsReady({ selectedAccountKey: "", planConsole: { rows: [] } }));
assert.equal(noAccount.state, "ads_account_blocked");
assert.equal(noAccount.primaryAction.id, "select_ads_account");

// 9. The selected account must have a fresh plan snapshot.
const stalePlans = orchestrator.deriveJourney(adsReady({
  planConsole: { freshness_status: "stale", rows: [{ account_key: "acct-a", stale: true, eligible_for_local_binding: true }] },
}));
assert.equal(stalePlans.state, "ads_plan_data_required");
assert.equal(stalePlans.primaryAction.kind, "sync_ads");

// 10. Fresh plans still cannot proceed without an identity binding.
const noPlanIdentity = orchestrator.deriveJourney(adsReady({
  planConsole: { freshness_status: "fresh", rows: [{ account_key: "acct-a", stale: false, eligible_for_local_binding: false }] },
}));
assert.equal(noPlanIdentity.state, "ads_plan_identity_blocked");
assert.equal(noPlanIdentity.primaryAction.targetId, "promotion-plan-center");

// 11. Account, plans and identity are insufficient without explicit execution boundaries.
const noBoundaries = orchestrator.deriveJourney(adsReady({ preflight: { state: "idle" } }));
assert.equal(noBoundaries.state, "ads_boundary_blocked");
assert.equal(noBoundaries.primaryAction.id, "configure_boundaries");

// 12. A fully gated ads task is actionable and exposes exactly one primary action.
const adsTask = orchestrator.deriveJourney(adsReady({
  ops: { tasks: [{ id: "ads-1", title: "下调高消耗计划预算", status: "todo" }] },
}));
assert.equal(adsTask.state, "today_task_ready");
assert.equal(adsTask.primaryAction.targetId, "today-task-center");
assert.equal(Object.prototype.hasOwnProperty.call(adsTask, "actions"), false);
assert.deepEqual(Object.keys(adsTask.primaryAction).sort(), ["disabled", "id", "kind", "label", "reason", "targetId"]);

// 13. Untagged or globally summarized rows must not be borrowed by a selected
// account. They may belong to a previous account or an all-account query.
const unscopedRows = orchestrator.deriveJourney(adsReady({
  planConsole: {
    selected_account_key: "acct-a",
    freshness_status: "fresh",
    summary: { total: 12 },
    rows: [{ stale: false, eligible_for_local_binding: true }],
  },
}));
assert.equal(unscopedRows.state, "ads_plan_data_required");

// 14. Endpoint snapshots that disagree on store/account identity stop at one
// recovery action instead of silently choosing whichever response arrived first.
const accountConflict = orchestrator.deriveJourney(adsReady({
  operationContext: { analysis_allowed: true, selected_account_key: "acct-b" },
}));
assert.equal(accountConflict.state, "identity_blocked");
assert.equal(accountConflict.primaryAction.id, "start_store_scan");
assert.match(accountConflict.detail, /避免混用数据/);
const storeConflict = orchestrator.deriveJourney(storeReady({
  connectionGuide: { store_id: "store-b", operational: { state: "data_fresh", core_data_fresh: true } },
}));
assert.equal(storeConflict.state, "identity_blocked");
assert.equal(storeConflict.primaryAction.id, "start_store_scan");
assert.match(storeConflict.detail, /避免混用数据/);

// 15. A terminal preflight from an old account cannot hijack the current
// selection. Only an active authorization/readback session participates.
const oldPreflight = orchestrator.deriveJourney(adsReady({
  preflight: { state: "idle", account_key: "acct-old", boundaries_ready: true },
}));
assert.notEqual(oldPreflight.state, "identity_blocked");
assert.equal(oldPreflight.scope.accountKey, "acct-a");

// 16. Public API stays DOM-free and understands product-facing lane names.
assert.equal(orchestrator.normalizeLane("千川投放"), "ads");
assert.equal(orchestrator.normalizeLane("店铺经营"), "store");
assert.equal(typeof orchestrator.deriveJourney, "function");

console.log("journey orchestrator tests passed");
