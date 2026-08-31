const assert = require("assert");
const policy = require("./promotion-plan-center.js");

assert.deepEqual(policy.promotionViewRoute("overview"), {
  view: "overview",
  target_id: "promotion-plan-center",
  filters: { mode: "all", plan_type: "all" },
});
assert.deepEqual(policy.promotionViewRoute("standard"), {
  view: "standard",
  target_id: "promotion-plan-center",
  filters: { mode: "standard", plan_type: "all" },
});
assert.deepEqual(policy.promotionViewRoute("full-domain"), {
  view: "full_domain",
  target_id: "promotion-plan-center",
  filters: { mode: "full_domain", plan_type: "all" },
});
assert.deepEqual(policy.promotionViewRoute("chengfang"), {
  view: "chengfang",
  target_id: "promotion-mode-workbench",
  filters: null,
});
assert.equal(policy.promotionViewRoute("unexpected").view, "overview");
const disposableRoute = policy.promotionViewRoute("standard");
disposableRoute.filters.mode = "all";
assert.equal(policy.promotionViewRoute("standard").filters.mode, "standard");

const partialReceipt = policy.deriveCollectionReceiptView({
  status: "partial",
  safe_to_claim_complete: false,
  platform_total: 120,
  collected_rows: 83,
  stable_id_rows: 82,
  mode_confirmed_rows: 80,
  warning_labels: ["分页采集尚未完成"],
});
assert.equal(partialReceipt.tone, "attention");
assert.match(partialReceipt.title, /覆盖范围待确认/);
assert.equal(partialReceipt.detail, "平台显示 120 条 · 已读取 83 条 · 稳定 ID 82/83 · 模式确认 80/83");
assert.equal(partialReceipt.warning, "分页采集尚未完成");
assert.doesNotMatch(partialReceipt.title, /全部|完整/);

const completeReceipt = policy.deriveCollectionReceiptView({
  status: "complete",
  safe_to_claim_complete: true,
  platform_total: 12,
  collected_rows: 12,
  stable_id_rows: 12,
  mode_confirmed_rows: 12,
});
assert.equal(completeReceipt.tone, "complete");
assert.equal(completeReceipt.title, "计划读取范围已确认");
assert.match(completeReceipt.warning, /不会开启.*生产写入授权/);

const missingReceipt = policy.deriveCollectionReceiptView({});
assert.equal(missingReceipt.tone, "unknown");
assert.equal(missingReceipt.detail, "平台总数未确认 · 已读取 0 条 · 稳定 ID 0/0 · 模式确认 0/0");

const receiptPayload = {
  collection_receipt: { status: "inconsistent", collected_rows: 3 },
  collection_receipts: [
    {
      scope_key: "acct-a|standard|product",
      scope: { account_key: "acct-a", promotion_mode: "standard", plan_type: "product" },
      status: "complete",
      safe_to_claim_complete: true,
      collected_rows: 1,
    },
  ],
};
const scopedReceipt = policy.selectCollectionReceipt(receiptPayload, {
  account: "acct-a", mode: "standard", plan_type: "product",
});
assert.equal(scopedReceipt.source, "scoped");
assert.equal(scopedReceipt.exact_scope, true);
assert.deepEqual(scopedReceipt.scope, { account: "acct-a", promotion_mode: "standard", plan_type: "product" });
assert.equal(scopedReceipt.receipt.status, "complete");
assert.equal(scopedReceipt.receipt.collected_rows, 1);
const broadReceipt = policy.selectCollectionReceipt(receiptPayload, {
  account: "acct-a", mode: "all", plan_type: "product",
});
assert.equal(broadReceipt.source, "global");
assert.equal(broadReceipt.exact_scope, false);
assert.deepEqual(broadReceipt.scope, { account: "acct-a", promotion_mode: "unknown", plan_type: "product" });
assert.equal(broadReceipt.receipt.status, "inconsistent");
const missingScopedReceipt = policy.selectCollectionReceipt(receiptPayload, {
  account: "acct-a", mode: "chengfang", plan_type: "live",
});
assert.equal(missingScopedReceipt.source, "scope_missing");
assert.equal(missingScopedReceipt.receipt.status, "inconsistent");

const productRecoveryContract = policy.buildScopeRecoveryContract(scopedReceipt);
assert.equal(productRecoveryContract.can_collect, true);
assert.equal(productRecoveryContract.account_key, "acct-a");
assert.equal(productRecoveryContract.promotion_mode, "standard");
assert.equal(productRecoveryContract.plan_type, "product");
assert.deepEqual(productRecoveryContract.page_ids, ["qianchuan_campaigns"]);
assert.deepEqual(productRecoveryContract.expected_page_types, ["campaigns", "qianchuan_campaigns"]);
assert.equal(productRecoveryContract.purpose, "promotion_plan_scope_recovery_product");
assert.equal(productRecoveryContract.platform_write_enabled, false);
assert.equal(productRecoveryContract.automatic_submit, false);
assert.equal(Object.prototype.hasOwnProperty.call(productRecoveryContract, "plan_id"), false, "recovery must never fabricate a plan ID");
const liveRecoveryContract = policy.buildScopeRecoveryContract({
  exact_scope: true,
  scope: { account: "acct-live", promotion_mode: "chengfang", plan_type: "live" },
});
assert.deepEqual(liveRecoveryContract.page_ids, ["qianchuan_live"]);
assert.deepEqual(liveRecoveryContract.expected_page_types, ["qianchuan_live"]);
assert.equal(liveRecoveryContract.purpose, "promotion_plan_scope_recovery_live");
assert.equal(policy.buildScopeRecoveryContract(broadReceipt).can_collect, false, "a broad scope cannot start recovery collection");
assert.equal(policy.selectScopeRecoveryAction([
  { automation_next_action: { code: "refresh_verified_scope", label: "刷新" } },
  { automation_next_action: { code: "read_plan_id", label: "补 ID" } },
  { automation_next_action: { code: "reselect_verified_scope", label: "核对账户" } },
]).code, "reselect_verified_scope", "identity mismatch must win over freshness and plan-id recovery");
assert.equal(policy.selectScopeRecoveryAction([
  { automation_next_action: { code: "refresh_verified_scope", label: "刷新" } },
  { automation_next_action: { code: "read_plan_id", label: "补 ID" } },
]).code, "read_plan_id");

const guardNow = Date.parse("2026-08-23T16:00:00+08:00");
const guardFilters = { account: "acct-selected", mode: "chengfang", plan_type: "live" };
const incompleteGuardPayload = {
  freshness_status: "stale",
  data_age_seconds: 3600,
  data_as_of_ms: guardNow - 60 * 60 * 1000,
  collection_receipts: [
    {
      scope: { account_key: "acct-selected", promotion_mode: "chengfang", plan_type: "live" },
      status: "partial",
      safe_to_claim_complete: false,
      platform_total: 1,
      collected_rows: 0,
    },
    {
      scope: { account_key: "acct-new", promotion_mode: "chengfang", plan_type: "live" },
      status: "complete",
      safe_to_claim_complete: true,
      platform_total: 1,
      collected_rows: 1,
    },
  ],
};
const oldUnlinkedGuard = policy.deriveUnlinkedAccountGuard(incompleteGuardPayload, {
  link_required: true,
  unlinked_accounts: [{ key: "acct-new", last_seen_at_ms: guardNow - 2 * 60 * 60 * 1000 }],
}, guardFilters, guardNow);
assert.equal(oldUnlinkedGuard.blocked, false, "an unrelated historical unlinked account must not permanently block a selected account");
const recentUnlinkedGuard = policy.deriveUnlinkedAccountGuard(incompleteGuardPayload, {
  link_required: true,
  unlinked_accounts: [{ key: "acct-new", last_seen_at_ms: guardNow - 60 * 1000 }],
}, guardFilters, guardNow);
assert.equal(recentUnlinkedGuard.blocked, true, "a recent related unlinked account must fail closed when the current receipt is incomplete");
assert.equal(recentUnlinkedGuard.account_key, "acct-new");

const healthyGuardPayload = {
  ...incompleteGuardPayload,
  freshness_status: "fresh",
  data_age_seconds: 30,
  data_as_of_ms: guardNow - 30 * 1000,
  collection_receipts: incompleteGuardPayload.collection_receipts.map((item, index) => index === 0
    ? { ...item, status: "complete", safe_to_claim_complete: true, collected_rows: 1 }
    : item),
};
assert.equal(policy.deriveUnlinkedAccountGuard(healthyGuardPayload, {
  link_required: true,
  unlinked_accounts: [{ key: "acct-new", last_seen_at_ms: guardNow - 60 * 1000 }],
}, guardFilters, guardNow).blocked, false, "a healthy current scope must ignore an older unlinked account");

const payload = {
  safe: true,
  rows: [
    {
      plan_key: "acct-a:plan-1",
      plan_id: "plan-1",
      plan_name: "直播守量计划",
      account_key: "acct-a",
      account_label: "主投放账户",
      promotion_mode: "chengfang",
      plan_type: "live",
      delivery_status: "投放中",
      budget: 500,
      spend: 300,
      roi: 2,
      orders: 8,
      quality_score: 90,
      stale: false,
      data_age_seconds: 15,
      learning_phase: "learning",
      learning_status_label: "学习中",
      platform_low_efficiency: "flagged",
      platform_low_efficiency_label: "平台标记低效",
      diagnostic_source: "official_api",
      diagnostic_source_label: "官方 API",
      eligible_for_local_binding: true,
      binding_blockers: [],
    },
    {
      plan_key: "acct-a:plan-2",
      plan_id: "plan-2",
      plan_name: "商品止损计划",
      account_key: "acct-a",
      account_label: "主投放账户",
      promotion_mode: "full_domain",
      plan_type: "product",
      delivery_status: "暂停",
      spend: 100,
      roi: 0.5,
      orders: 1,
      quality_score: 60,
      stale: false,
      data_age_seconds: 15,
      eligible_for_local_binding: false,
      binding_blockers: ["数据质量低于 70"],
    },
  ],
};

const binding = policy.createBinding(payload.rows[0], { profile: "protect" }, 1_700_000_000_000);
assert.equal(binding.profile, "protect");
assert.equal(binding.local_only, true);
assert.equal(binding.platform_write_enabled, false);
assert.ok(binding.features.includes("ai_pricing"));
assert.ok(!binding.features.includes("auto_budget"));

const view = policy.deriveView(payload, { mode: "chengfang", features: ["ai_pricing"] }, {
  [binding.plan_key]: binding,
});
assert.equal(view.rows.length, 1);
assert.equal(view.rows[0].binding_state, "bound");
assert.equal(view.summary.spend, 300);
assert.equal(view.summary.weighted_roi, 2);
assert.equal(view.summary.learning, 1);
assert.equal(view.summary.low_efficiency, 1);
assert.equal(view.rows[0].learning_phase, "learning");
assert.equal(view.rows[0].learning_status_label, "学习中");
assert.equal(view.rows[0].platform_low_efficiency, "flagged");
assert.equal(view.rows[0].diagnostic_source, "official_api");
assert.equal(view.rows[0].health.learning_status_label, "学习中");
assert.equal(view.rows[0].health.platform_low_efficiency_label, "平台标记低效");
assert.equal(view.rows[0].data_age_label, "15 秒前");
assert.equal(view.platform_write_enabled, false);
assert.equal(policy.deriveView({ rows: [{ plan_key: "empty", plan_name: "无指标计划" }] }).summary.spend, null);

const missingHealth = policy.normalizePlan({
  ...payload.rows[0],
  learning_phase: undefined,
  learning_status_label: undefined,
  platform_low_efficiency: undefined,
  platform_low_efficiency_label: undefined,
  diagnostic_source: undefined,
  diagnostic_source_label: undefined,
});
assert.equal(missingHealth.learning_phase, "unavailable");
assert.equal(missingHealth.platform_low_efficiency, "unknown");
assert.equal(missingHealth.diagnostic_source, "unavailable");
assert.equal(missingHealth.eligible_for_local_binding, true);

const ageExpired = policy.normalizePlan({
  ...payload.rows[0], stale: false, data_age_seconds: 1800, freshness_status: "fresh",
});
assert.equal(ageExpired.stale, true);
assert.equal(ageExpired.freshness_status, "stale");
assert.match(ageExpired.binding_blockers.join("；"), /过期/);
const futureCapture = policy.normalizePlan({
  ...payload.rows[0], stale: false, data_age_seconds: -20, freshness_status: "fresh",
});
assert.equal(futureCapture.freshness_status, "future");
assert.match(futureCapture.data_age_label, /未来/);

const pausedBindingView = policy.deriveView({ rows: [{
  ...payload.rows[0], stale: true, data_age_seconds: 1900, freshness_status: "stale",
}] }, {}, { [binding.plan_key]: binding });
assert.equal(pausedBindingView.rows[0].binding_state, "blocked");
assert.equal(pausedBindingView.rows[0].binding, null);
assert.equal(pausedBindingView.rows[0].saved_binding.plan_key, binding.plan_key);
assert.equal(pausedBindingView.rows[0].binding_paused, true);
assert.equal(pausedBindingView.summary.bound, 0);
assert.equal(pausedBindingView.summary.binding_paused, 1);
assert.equal(policy.deriveView({ rows: pausedBindingView.all_rows }, { binding: "bound" }, {
  [binding.plan_key]: binding,
}).rows.length, 0);

const mismatchedBinding = { ...binding, account_key: "acct-other" };
const mismatchView = policy.deriveView({ rows: [payload.rows[0]] }, {}, { [binding.plan_key]: mismatchedBinding });
assert.equal(mismatchView.rows[0].binding_state, "blocked");
assert.match(mismatchView.rows[0].binding_blockers.join("；"), /身份不一致/);

const freshnessView = policy.deriveView({
  rows: payload.rows,
  freshness_status: "stale",
  data_as_of_ms: 1_700_000_000_000,
  oldest_data_as_of_ms: 1_699_999_900_000,
  data_age_seconds: 1900,
  latest_data_age_seconds: 30,
});
assert.equal(freshnessView.freshness.status, "stale");
assert.equal(freshnessView.latest_data_age_seconds, 30);
assert.match(freshnessView.data_age_label, /分钟/);

const storeScopedView = policy.deriveView({ selected_store_key: "STORE-A", rows: [payload.rows[0]] });
assert.equal(storeScopedView.rows[0].store_key, "store-a");

const blockedView = policy.deriveView(payload, { binding: "blocked" }, {});
assert.equal(blockedView.rows.length, 1);
assert.equal(blockedView.rows[0].plan_id, "plan-2");
assert.equal(blockedView.rows[0].learning_phase, "unavailable");
assert.equal(blockedView.rows[0].platform_low_efficiency, "unknown");
assert.equal(blockedView.rows[0].diagnostic_source, "unavailable");
assert.equal(blockedView.summary.learning, 0);
assert.equal(blockedView.summary.low_efficiency, 0);
assert.throws(() => policy.createBinding(payload.rows[1], { profile: "protect" }), /数据质量/);
assert.throws(() => policy.createBinding({
  ...payload.rows[0], plan_key: "stale-plan", stale: true,
  binding_blockers: ["计划数据已过期，请重新同步"], eligible_for_local_binding: false,
}, { profile: "protect" }), /过期/);

const draft = policy.createBatchDraft({
  account_key: "acct-a",
  mode: "chengfang",
  plan_type: "product",
  target_ids: "product-1\nproduct-2\nproduct-1",
  budget: 1000,
  roi_target: 3,
  name_prefix: "秋季上新",
  star_material: true,
}, 1_700_000_000_000);
assert.equal(draft.items.length, 2);
assert.equal(draft.items[0].plan_name, "秋季上新-乘方商品-01");
assert.equal(draft.automatic_submit, false);
assert.equal(draft.platform_write_enabled, false);
assert.throws(() => policy.createBatchDraft({}), /账户/);
assert.throws(() => policy.createBatchDraft({
  account_key: "acct-a", mode: "chengfang", plan_type: "product",
  target_ids: Array.from({ length: 21 }, (_, index) => `p-${index}`),
  budget: 100, roi_target: 2, name_prefix: "批量",
}), /最多/);

console.log("promotion plan center tests passed");
