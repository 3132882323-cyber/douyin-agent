const assert = require("assert");
const bulk = require("./promotion-bulk-actions.js");

const plans = [
  {
    plan_key: "acct-a:plan-1", plan_id: "plan-1", plan_name: "直播计划 A",
    account_key: "acct-a", account_label: "主账户", promotion_mode: "chengfang",
    store_key: "store-a",
    plan_type: "live", delivery_status: "投放中", budget: 1000, quality_score: 90,
    stale: false,
    freshness_status: "fresh", data_age_seconds: 15, data_age_label: "15 秒前",
    learning_phase: "learning", learning_status_label: "学习中",
    platform_low_efficiency: "flagged", platform_low_efficiency_label: "平台标记低效",
    diagnostic_source: "official_api", diagnostic_source_label: "官方 API",
    collection_gate_state: "ready", collection_scope_key: "acct-a|chengfang|live",
    supervised_draft_ready: true,
    automation_blockers: [],
    automation_next_action: { code: "review_supervised_draft", label: "生成受监督草稿" },
    eligible_for_local_binding: true, binding_blockers: [], binding: { profile: "protect" },
  },
  {
    plan_key: "acct-b:plan-2", plan_id: "plan-2", plan_name: "商品计划 B",
    account_key: "acct-b", account_label: "副账户", promotion_mode: "full_domain",
    store_key: "store-b",
    plan_type: "product", delivery_status: "暂停", budget: 500, quality_score: 60,
    stale: false,
    collection_gate_state: "plan_not_ready", collection_scope_key: "acct-b|full_domain|product",
    supervised_draft_ready: false,
    automation_blockers: [{ code: "PLAN_BINDING_NOT_READY", message: "数据质量低于 70" }],
    eligible_for_local_binding: false, binding_blockers: ["数据质量低于 70"], binding: null,
  },
];

assert.deepStrictEqual(bulk.normalizeSelection(["a", "a", "", "b"]), ["a", "b"]);

const budgetDraft = bulk.buildBulkDraft(plans, {
  action: "decrease_budget",
  decrease_percent: 20,
  plan_keys: ["acct-a:plan-1", "acct-b:plan-2"],
}, 1_700_000_000_000);
assert.equal(budgetDraft.summary.total, 2);
assert.equal(budgetDraft.summary.ready, 1);
assert.equal(budgetDraft.summary.blocked, 1);
assert.equal(budgetDraft.summary.accounts, 2);
assert.equal(budgetDraft.summary.current_budget, 1000);
assert.equal(budgetDraft.summary.target_budget, 800);
assert.equal(budgetDraft.items[0].target_value, 800);
assert.equal(budgetDraft.platform_write_enabled, false);
assert.equal(budgetDraft.automatic_submit, false);
assert.match(budgetDraft.notice, /逐账户/);
assert.equal(budgetDraft.scoped_drafts.length, 2);
assert.equal(budgetDraft.restorable, false);
assert.equal(budgetDraft.account_groups[0].scope_key, "store-a::acct-a");
assert.equal(budgetDraft.items[0].data_age_label, "15 秒前");
assert.equal(budgetDraft.items[0].learning_status_label, "学习中");
assert.equal(budgetDraft.items[0].platform_low_efficiency, "flagged");

const accountADraft = bulk.draftForScope(budgetDraft, { store_key: "store-a", account_key: "acct-a" });
assert.ok(accountADraft);
assert.equal(accountADraft.items.length, 1);
assert.equal(accountADraft.items[0].plan_id, "plan-1");
assert.equal(accountADraft.action_label, "批量降预算预演");
assert.equal(accountADraft.summary.accounts, 1);
assert.equal(accountADraft.summary.current_budget, 1000);
assert.equal(accountADraft.account_groups.length, 1);
assert.equal(bulk.draftForScope(budgetDraft, { store_key: "store-b", account_key: "acct-a" }), null);
assert.equal(bulk.filterDraftsForScope([budgetDraft], { store_key: "store-a", account_key: "acct-a" }).length, 1);
assert.equal(bulk.filterDraftsForScope([budgetDraft], { store_key: "store-x", account_key: "acct-a" }).length, 0);

const explicitlyScoped = bulk.buildBulkDraft(plans, {
  action: "decrease_budget", decrease_percent: 20,
  plan_keys: ["acct-a:plan-1", "acct-b:plan-2"],
  store_key: "store-a", account_key: "acct-a",
}, 1_700_000_000_000);
assert.equal(explicitlyScoped.items[0].state, "ready_for_review");
assert.match(explicitlyScoped.items[1].blockers.join("；"), /当前广告账户/);
assert.match(explicitlyScoped.items[1].blockers.join("；"), /当前店铺/);

const singleScopeDraft = bulk.buildBulkDraft(plans.slice(0, 1), {
  action: "pause_plan", store_key: "store-a", account_key: "acct-a",
}, 1_700_000_000_001);
assert.equal(singleScopeDraft.restorable, true);
assert.equal(singleScopeDraft.scope_key, "store-a::acct-a");
assert.equal(singleScopeDraft.scoped_drafts[0].restorable, true);

const inferredStoreScopeDraft = bulk.buildBulkDraft([{ ...plans[0], store_key: "" }], {
  action: "pause_plan", store_key: "store-a", account_key: "acct-a",
}, 1_700_000_000_002);
assert.equal(inferredStoreScopeDraft.summary.ready, 1);
assert.equal(inferredStoreScopeDraft.items[0].store_key, "store-a");
assert.equal(inferredStoreScopeDraft.items[0].store_scope_source, "selected_account_scope");
assert.equal(inferredStoreScopeDraft.scope_key, "store-a::acct-a");

const staleDraft = bulk.buildBulkDraft([{ ...plans[0], plan_key: "stale", stale: true }], {
  action: "decrease_budget", decrease_percent: 20,
}, 1_700_000_000_000);
assert.equal(staleDraft.summary.ready, 0);
assert.match(staleDraft.items[0].blockers.join("；"), /过期/);

const ageExpiredDraft = bulk.buildBulkDraft([{
  ...plans[0], plan_key: "age-expired", stale: false, freshness_status: "fresh", data_age_seconds: 1800,
}], { action: "pause_plan" }, 1_700_000_000_000);
assert.equal(ageExpiredDraft.summary.ready, 0);
assert.equal(ageExpiredDraft.items[0].freshness_status, "stale");
assert.match(ageExpiredDraft.items[0].blockers.join("；"), /过期/);

const futureDraft = bulk.buildBulkDraft([{
  ...plans[0], plan_key: "future", stale: false, freshness_status: "fresh", data_age_seconds: -1,
}], { action: "pause_plan" }, 1_700_000_000_000);
assert.equal(futureDraft.summary.ready, 0);
assert.equal(futureDraft.items[0].freshness_status, "future");
assert.match(futureDraft.items[0].blockers.join("；"), /时间异常/);

const pausedBindingDraft = bulk.buildBulkDraft([{
  ...plans[0], plan_key: "paused-binding", stale: false,
  binding: null, saved_binding: { profile: "protect" }, binding_paused: true, binding_state: "blocked",
}], { action: "pause_plan" }, 1_700_000_000_000);
assert.equal(pausedBindingDraft.summary.ready, 0);
assert.match(pausedBindingDraft.items[0].blockers.join("；"), /绑定当前已暂停/);

const partialCollectionDraft = bulk.buildBulkDraft([{
  ...plans[0], plan_key: "partial-collection", supervised_draft_ready: false,
  collection_gate_state: "partial_collection",
  automation_blockers: [{ code: "PLAN_SCOPE_COVERAGE_INCOMPLETE", message: "当前计划范围尚未完整采集" }],
  automation_next_action: { code: "continue_scoped_collection", detail: "完成翻页后重试" },
}], { action: "pause_plan" }, 1_700_000_000_000);
assert.equal(partialCollectionDraft.summary.ready, 0);
assert.equal(partialCollectionDraft.items[0].collection_gate_state, "partial_collection");
assert.match(partialCollectionDraft.items[0].blockers.join("；"), /尚未完整采集/);

const scheduleDraft = bulk.buildBulkDraft(plans.slice(0, 1), {
  action: "schedule_plan", schedule_start: "18:00", schedule_end: "02:00",
}, 1_700_000_000_000);
assert.match(scheduleDraft.items[0].target_value, /跨天/);

assert.throws(() => bulk.buildBulkDraft([], { action: "pause_plan" }), /选择/);
assert.throws(() => bulk.buildBulkDraft(plans, { action: "decrease_budget", decrease_percent: 40 }), /5%/);
assert.throws(() => bulk.buildBulkDraft(plans, { action: "schedule_plan", schedule_start: "09:00", schedule_end: "09:00" }), /不同/);

console.log("promotion bulk actions tests passed");
