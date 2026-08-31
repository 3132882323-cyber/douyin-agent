const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(require.resolve("./scan-policy.js"), "utf8");
const context = { globalThis: null };
context.globalThis = context;
vm.runInNewContext(source, context, { filename: "scan-policy.js" });

const policy = context.DianAgentScanPolicy;
assert.ok(policy);

assert.strictEqual(
  policy.matchAccount(
    { key: "acct_platform_b", label: "同名旗舰店", identity_source: "platform_id" },
    { key: "acct_platform_a", label: "同名旗舰店", identity_source: "platform_id" },
  ).code,
  "ACCOUNT_MISMATCH",
);

assert.strictEqual(
  policy.matchAccount(
    { key: "acct_platform_a", label: "同名旗舰店", identity_source: "platform_id" },
    { key: "acct_label", label: "同名旗舰店", identity_source: "account_label" },
  ).code,
  "ACCOUNT_MISMATCH",
);

const storeMismatch = new Error("当前页面所属店铺与已选店铺不一致，已停止巡检且不会混合数据。");
assert.strictEqual(policy.errorCode(storeMismatch), "STORE_MISMATCH");
assert.strictEqual(policy.isNonRetryable(storeMismatch), true);

const unresolved = new Error("未识别当前千川账号。巡检已停止刷新，请确认账号后重试。");
assert.strictEqual(policy.isNonRetryable(unresolved), true);
assert.strictEqual(policy.errorCode(unresolved), "ACCOUNT_UNRESOLVED");
assert.strictEqual(policy.isNonRetryable(new Error("页面加载超时")), false);
assert.strictEqual(policy.errorCode(new Error("STORE_IDENTITY_CONFLICT: 候选数据未写入")), "STORE_IDENTITY_CONFLICT");
assert.strictEqual(policy.errorCode(new Error("PLAN_TABLE_UNRECOGNIZED: 页面结构错误")), "PLAN_TABLE_UNRECOGNIZED");
assert.strictEqual(policy.errorCode(new Error("PLAN_TABLE_LOADING: 计划业务行仍在加载")), "PLAN_TABLE_LOADING");
assert.strictEqual(policy.isNonRetryable(new Error("PLAN_TABLE_LOADING: 计划业务行仍在加载")), false);
assert.strictEqual(policy.errorCode(new Error("PLAN_IDENTIFIERS_UNRESOLVED: 计划 ID 缺失")), "PLAN_IDENTIFIERS_UNRESOLVED");
assert.strictEqual(policy.isNonRetryable(new Error("PLAN_IDENTIFIERS_UNRESOLVED: 计划 ID 缺失")), false);
assert.strictEqual(policy.errorCode(new Error("COLLECTION_CONTEXT_CHANGED: 店铺范围发生变化")), "COLLECTION_CONTEXT_CHANGED");
assert.strictEqual(policy.errorCode(new Error("页面识别为 materials，预期为 campaigns")), "PAGE_TYPE_MISMATCH");
assert.strictEqual(policy.errorCode(new Error("巡店页面已关闭")), "TAB_CLOSED");
assert.strictEqual(policy.shouldStopAfterResult({ ok: false, error_code: "STORE_IDENTITY_UNRESOLVED" }), true);
assert.strictEqual(policy.shouldStopAfterResult({ ok: false, error_code: "NETWORK_TRANSIENT" }), false);
assert.deepStrictEqual(Array.from(policy.resumePageIds({
  planned_page_ids: ["overview", "orders", "products"],
  attempted_page_ids: ["overview", "orders"],
  pending_page_ids: ["products"],
  results: [{ id: "overview", ok: true }, { id: "orders", ok: false }],
})), ["orders", "products"]);
assert.deepStrictEqual(Array.from(policy.resumePageIds({
  planned_page_ids: ["orders", "products"],
  attempted_page_ids: ["orders", "products"],
  results: [
    { id: "orders", ok: true, collection_complete: false, warning_code: "COLLECTION_TRUNCATED" },
    { id: "products", ok: true, collection_complete: true },
    { id: "outside_scope", ok: false },
  ],
})), ["orders"], "incomplete list pages are resumable, but pages outside the locked scope are not");

const loginAccess = policy.inspectPageAccess({
  href: "https://qianchuan.jinritemai.com/login", title: "巨量千川登录", readyState: "complete",
}, "qianchuan");
assert.strictEqual(loginAccess.code, "LOGIN_REQUIRED");
assert.strictEqual(policy.inspectPageAccess({
  href: "https://example.com/redirect", title: "redirect", readyState: "complete",
}, "doudian").code, "PAGE_SOURCE_MISMATCH");
assert.strictEqual(policy.inspectPageAccess({
  href: "https://fxg.jinritemai.com/ffa/morder/order/list", title: "订单管理", readyState: "complete",
}, "doudian").ok, true);

const identityRecovery = policy.buildRecoveryCheckpoint({
  status: "partial",
  planned_page_ids: ["overview", "orders"],
  attempted_page_ids: ["overview"],
  pending_page_ids: ["orders"],
  results: [{ id: "overview", ok: false, error_code: "STORE_IDENTITY_CONFLICT" }],
});
assert.strictEqual(identityRecovery.state, "blocked_identity");
assert.strictEqual(identityRecovery.automatic_resume, false);
assert.strictEqual(identityRecovery.action, "confirm_store_then_resume");
assert.deepStrictEqual(Array.from(identityRecovery.resume_page_ids), ["overview", "orders"]);

const unresolvedPageRecovery = policy.buildRecoveryCheckpoint({
  status: "partial",
  planned_page_ids: ["orders", "products"],
  attempted_page_ids: ["orders"],
  pending_page_ids: ["products"],
  results: [{ id: "orders", ok: false, error_code: "STORE_IDENTITY_UNRESOLVED" }],
});
assert.strictEqual(unresolvedPageRecovery.state, "blocked_identity");
assert.strictEqual(unresolvedPageRecovery.action, "confirm_store_then_resume");
assert.deepStrictEqual(
  Array.from(unresolvedPageRecovery.resume_page_ids),
  ["orders", "products"],
  "an identity-less current page must remain recoverable and pending instead of silently succeeding",
);

const interruptedRecovery = policy.buildRecoveryCheckpoint({
  status: "interrupted",
  interrupted: true,
  planned_page_ids: ["overview", "orders"],
  attempted_page_ids: ["overview"],
  pending_page_ids: ["orders"],
  results: [{ id: "overview", ok: true }],
});
assert.strictEqual(interruptedRecovery.state, "paused_interrupted");
assert.strictEqual(interruptedRecovery.error_code, "SCAN_INTERRUPTED");
assert.strictEqual(interruptedRecovery.automatic_resume, false);
assert.deepStrictEqual(Array.from(interruptedRecovery.resume_page_ids), ["orders"]);

const truncatedRecovery = policy.buildRecoveryCheckpoint({
  status: "partial",
  planned_page_ids: ["products"],
  attempted_page_ids: ["products"],
  results: [{ id: "products", ok: true, collection_complete: false, warning_code: "COLLECTION_TRUNCATED" }],
});
assert.strictEqual(truncatedRecovery.state, "partial_collection");
assert.strictEqual(truncatedRecovery.action, "narrow_filters_then_resume");

const incompletePlanIdentity = policy.inspectPlanIdentityCoverage({
  page_type: "campaigns",
  quality: { plan_identity_coverage: { eligible_rows: 5, identified_rows: 3 } },
});
assert.strictEqual(incompletePlanIdentity.complete, false);
assert.strictEqual(incompletePlanIdentity.code, "PLAN_IDENTIFIERS_UNRESOLVED");
assert.strictEqual(policy.inspectPlanIdentityCoverage({
  page_type: "campaigns",
  quality: { plan_identity_coverage: { eligible_rows: 5, identified_rows: 5 } },
}).complete, true);
assert.strictEqual(policy.inspectPlanIdentityCoverage({
  page_type: "materials",
  quality: { plan_identity_coverage: { eligible_rows: 5, identified_rows: 0 } },
}).applicable, false);

const targetedRecovery = policy.buildRecoveryCheckpoint({
  status: "partial",
  planned_page_ids: ["qianchuan_campaigns"],
  attempted_page_ids: ["qianchuan_campaigns"],
  results: [
    { id: "orders", ok: false, error_code: "LOGIN_REQUIRED" },
    { id: "qianchuan_campaigns", ok: true, collection_complete: false, warning_code: "PLAN_IDENTIFIERS_UNRESOLVED" },
  ],
});
assert.strictEqual(targetedRecovery.error_code, "PLAN_IDENTIFIERS_UNRESOLVED");
assert.strictEqual(targetedRecovery.state, "blocked_evidence");
assert.deepStrictEqual(Array.from(targetedRecovery.resume_page_ids), ["qianchuan_campaigns"]);

const cancelledRecovery = policy.buildRecoveryCheckpoint({
  status: "cancelled", planned_page_ids: ["overview"], pending_page_ids: ["overview"], results: [],
});
assert.strictEqual(cancelledRecovery.can_resume, false);
assert.deepStrictEqual(Array.from(cancelledRecovery.resume_page_ids), []);
assert.strictEqual(policy.canCancelScan({ run_id: "scan-running", status: "running" }), true);
assert.strictEqual(policy.canCancelScan({ run_id: "scan-complete", status: "completed" }), false);
assert.strictEqual(policy.acceptScanStatusTransition("completed", "cancelled"), false);
assert.strictEqual(policy.acceptScanStatusTransition("cancelled", "completed"), false);
assert.strictEqual(policy.acceptScanStatusTransition("running", "completed", { cancelRequested: true }), false);
assert.strictEqual(policy.acceptScanStatusTransition("running", "cancelled", { cancelRequested: true }), true);

const thirteenPages = [
  "overview", "orders", "products", "inventory", "refunds", "reviews", "shelf",
  "live", "short_video", "image_text", "search", "recommend_card", "funds",
];
const historicalResults = thirteenPages.map((id) => ({
  id,
  ok: id !== "funds",
  collection_complete: id !== "funds",
  error_code: id === "funds" ? "PAGE_LOAD_TIMEOUT" : "",
}));
const recoveredProgress = policy.reconcileRecoveryProgress({
  run_id: "scan_root_1",
  root_run_id: "scan_root_1",
  store_key: "store_a",
  account_key: "",
  planned_page_ids: thirteenPages,
  results: historicalResults,
}, [{ id: "funds", ok: true, collection_complete: true }], {
  storeKey: "store_a", accountKey: "",
}, thirteenPages);
assert.strictEqual(recoveredProgress.root_run_id, "scan_root_1");
assert.deepStrictEqual(Array.from(recoveredProgress.planned_page_ids), thirteenPages);
assert.deepStrictEqual(Array.from(recoveredProgress.pending_page_ids), []);
assert.strictEqual(recoveredProgress.results.length, 13);
assert.strictEqual(recoveredProgress.success, 13);
assert.strictEqual(recoveredProgress.failed, 0);
assert.strictEqual(recoveredProgress.coverage_complete, true);

const eighteenPages = [...thirteenPages, "qianchuan_video_library", "qianchuan_overview", "qianchuan_campaigns", "qianchuan_live", "qianchuan_live_dashboard"];
const adsRecovery = policy.reconcileRecoveryProgress({
  run_id: "scan_child_1",
  root_run_id: "scan_root_18",
  store_key: "store_a",
  account_key: "account_a",
  planned_page_ids: eighteenPages,
  results: eighteenPages.slice(0, -1).map((id) => ({ id, ok: true, collection_complete: true })),
}, [{ id: "qianchuan_live_dashboard", ok: true, collection_complete: true }], {
  storeKey: "store_a", accountKey: "account_a",
}, eighteenPages);
assert.strictEqual(adsRecovery.root_run_id, "scan_root_18");
assert.strictEqual(adsRecovery.results.length, 18);
assert.strictEqual(adsRecovery.coverage_complete, true);
assert.deepStrictEqual(
  Array.from(policy.mergeScopedResults(
    { store_key: "store_a", account_key: "acct_a", results: [{ id: "overview", ok: true }] },
    [{ id: "orders", ok: true }],
    { storeKey: "store_b", accountKey: "acct_a" },
    ["overview", "orders"],
  ), (item) => item.id),
  ["orders"],
);
assert.deepStrictEqual(
  Array.from(policy.mergeScopedResults(
    { store_key: "store_a", account_key: "acct_a", results: [{ id: "overview", ok: true }] },
    [{ id: "orders", ok: true }],
    { storeKey: "store_a", accountKey: "acct_a" },
    ["overview", "orders"],
  ), (item) => item.id),
  ["overview", "orders"],
);

const ranked = policy.rankSeedTabs([
  { id: 1, active: true, lastAccessed: 300 },
  { id: 2, active: false, lastAccessed: 100 },
  { id: 3, active: false, lastAccessed: 500 },
], 2);
assert.deepStrictEqual(Array.from(ranked, (tab) => tab.id), [2, 1, 3]);

assert.strictEqual(policy.shouldAcceptScanSnapshot(
  { run_id: "scan-new", revision: 9, heartbeat_at: 900 },
  { run_id: "scan-new", revision: 8, heartbeat_at: 1_000 },
), false, "an older revision must not overwrite newer scan progress");
assert.strictEqual(policy.shouldAcceptScanSnapshot(
  { run_id: "scan-new", revision: 9 },
  { run_id: "scan-old", revision: 9 },
), false, "equal revisions from different runs are ambiguous and must fail closed");
assert.strictEqual(policy.shouldAcceptScanSnapshot(
  { run_id: "scan-old", revision: 9 },
  { run_id: "scan-new", revision: 10 },
), true, "a higher persisted revision may advance to a new run");
assert.strictEqual(policy.shouldAcceptScanSnapshot(
  { run_id: "legacy-new", started_at: 200, heartbeat_at: 300 },
  { run_id: "legacy-old", started_at: 100, heartbeat_at: 400 },
), false, "legacy responses from an older run must not win on a later arrival");

assert.strictEqual(policy.isQianchuanUrl("https://qianchuan.jinritemai.com/dataV2/roi2-material-analysis"), true);
assert.strictEqual(policy.isQianchuanUrl("chrome-extension://workbench/sidepanel.html"), false);

const qianchuanTabs = [
  { id: 11, url: "https://qianchuan.jinritemai.com/home", active: false, lastAccessed: 100 },
  { id: 12, url: "https://qianchuan.jinritemai.com/dataV2/roi2-material-analysis", active: false, lastAccessed: 300 },
];
const recentSelection = policy.selectQianchuanSyncTab(
  qianchuanTabs,
  { id: 99, url: "chrome-extension://workbench/sidepanel.html", active: true },
  11,
  12,
);
assert.strictEqual(recentSelection.tab.id, 11);
assert.strictEqual(recentSelection.matchedBy, "recent");

const activeSelection = policy.selectQianchuanSyncTab(
  qianchuanTabs,
  { id: 12, url: qianchuanTabs[1].url, active: true },
  11,
  null,
);
assert.strictEqual(activeSelection.tab.id, 12);
assert.strictEqual(activeSelection.matchedBy, "active");

const seedSelection = policy.selectQianchuanSyncTab(
  qianchuanTabs,
  { id: 99, url: "chrome-extension://workbench/sidepanel.html", active: true },
  404,
  12,
);
assert.strictEqual(seedSelection.tab.id, 12);
assert.strictEqual(seedSelection.matchedBy, "seed");

const missingSelection = policy.selectQianchuanSyncTab([], null, 11, 12);
assert.strictEqual(missingSelection.tab, null);
assert.strictEqual(missingSelection.matchedBy, "none");

const discoveredRoutes = policy.resolveQianchuanRoutes(
  { text: "巨量千川", url: "https://qianchuan.jinritemai.com/uni-prom/overall?aavid=masked" },
  [
    { text: "投放工具", url: "https://qianchuan.jinritemai.com/campaign/settings" },
    { text: "商品全域推广", url: "https://qianchuan.jinritemai.com/brand_bid/promotion/standard" },
    { text: "直播全域推广", url: "https://qianchuan.jinritemai.com/uni-prom/live" },
    { text: "直播大屏", url: "https://qianchuan.jinritemai.com/board-next" },
  ],
);
assert.strictEqual(discoveredRoutes.qianchuan_campaigns, "https://qianchuan.jinritemai.com/brand_bid/promotion/standard");
assert.strictEqual(discoveredRoutes.qianchuan_live, "https://qianchuan.jinritemai.com/uni-prom/live");
assert.strictEqual(discoveredRoutes.qianchuan_live_dashboard, "https://qianchuan.jinritemai.com/board-next");
assert.notStrictEqual(discoveredRoutes.qianchuan_campaigns, "https://qianchuan.jinritemai.com/uni-prom/overall?aavid=masked");

const currentOnlyRoutes = policy.resolveQianchuanRoutes(
  { text: "巨量千川", url: "https://qianchuan.jinritemai.com/uni-prom/overall" },
  [],
);
assert.strictEqual(currentOnlyRoutes.qianchuan_campaigns, "");
assert.strictEqual(currentOnlyRoutes.qianchuan_live, "");

const textBeatsEarlierUrlFallback = policy.resolveQianchuanRoutes(
  { text: "巨量千川", url: "https://qianchuan.jinritemai.com/home" },
  [
    { text: "旧入口", url: "https://qianchuan.jinritemai.com/uni-prom/product" },
    { text: "商品推广", url: "https://qianchuan.jinritemai.com/brand_bid/promotion/standard" },
  ],
);
assert.strictEqual(textBeatsEarlierUrlFallback.qianchuan_campaigns, "https://qianchuan.jinritemai.com/brand_bid/promotion/standard");

const catalog = {
  selected_store_key: "store_a",
  stores: [
    { key: "store_a", selected: true },
    { key: "store_b", selected: false },
  ],
};
assert.deepStrictEqual(
  JSON.parse(JSON.stringify(policy.confirmSelectedStore(catalog, "store_a"))),
  { ok: true, storeKey: "store_a" },
);
assert.strictEqual(policy.confirmSelectedStore(catalog, "store_b").code, "STORE_SELECTION_MISMATCH");
assert.strictEqual(policy.confirmSelectedStore(catalog, "../store_a").code, "STORE_SELECTION_INVALID");
assert.strictEqual(policy.confirmSelectedStore({
  selected_store_key: "ghost",
  stores: catalog.stores.map((item) => ({ key: item.key })),
}, "ghost").code, "STORE_SELECTION_UNCONFIRMED");
assert.strictEqual(policy.confirmSelectedStore({ stores: [{ key: "store_a", selected: true }] }, "store_a").ok, true);
assert.strictEqual(policy.confirmSelectedStore({
  selected_store_key: "store_a",
  stores: [{ key: "store_b", selected: true }, { key: "store_a" }],
}, "store_a").code, "STORE_SELECTION_CONFLICT");

const verifiedOverview = {
  page_type: "overview",
  identity_status: "resolved_by_bridge",
  identity_claims: [{
    kind: "douyin_shop_id", raw_id: "778899", confidence: "high", evidence_source: "data_attribute",
  }],
};
assert.strictEqual(policy.inspectDoudianIdentityProof(verifiedOverview).ok, true);
assert.strictEqual(policy.inspectDoudianCurrentIdentityProof({
  page_type: "orders",
  identity_status: "resolved_by_bridge",
  identity_claims: [{
    kind: "douyin_shop_id", raw_id: "778899", confidence: "medium", evidence_source: "allowlisted_storage",
  }],
}).ok, true, "a freshly read current/active platform storage scalar remains a legitimate inner-page proof");
assert.strictEqual(policy.inspectDoudianCurrentIdentityProof({
  page_type: "overview",
  identity_status: "resolved_by_bridge",
  identity_claims: [{
    kind: "douyin_shop_id", raw_id: "778899", confidence: "high", evidence_source: "bootstrap_sec_shop_id",
  }],
}).ok, true, "a unique current Doudian bootstrap sec_shop_id is valid current-page proof");
assert.strictEqual(policy.inspectDoudianCurrentIdentityProof({
  page_type: "orders",
  identity_status: "resolved_by_bridge",
  identity_claims: [{
    kind: "douyin_shop_id", raw_id: "778899", confidence: "high", evidence_source: "overview_visible_text",
  }],
}).ok, false, "overview-only visible text must not be reused as proof for another route");
assert.strictEqual(policy.inspectDoudianCurrentIdentityProof({
  page_type: "orders",
  identity_status: "unresolved",
  identity_claims: [{
    kind: "douyin_shop_id", raw_id: "778899", confidence: "high", evidence_source: "data_attribute",
  }],
}).ok, false, "a contradictory unresolved status must not be reinterpreted as current identity proof");
assert.strictEqual(policy.inspectDoudianIdentityProof({
  page_type: "orders", identity_status: "unresolved", identity_claims: [],
}).reason, "bootstrap_page_required");
assert.strictEqual(policy.inspectDoudianIdentityProof({
  page_type: "overview", identity_status: "unresolved", identity_claims: [],
}).code, "STORE_IDENTITY_UNRESOLVED");
assert.strictEqual(policy.createDoudianScanContext("store_a", {
  now: 1000, ttlMs: 10000, runId: "scan_a", tabId: 7,
}), null, "selected store alone must never create an identity lease");
const scanContext = policy.createDoudianScanContext("store_a", {
  now: 1000,
  ttlMs: 10000,
  runId: "scan_a",
  tabId: 7,
  proof: {
    verified: true,
    store_key: "store_a",
    page_type: "overview",
    identity_source: "hmac_douyin_shop_id",
    verified_at: 1000,
  },
});
const unresolvedSnapshot = {
  page_type: "orders",
  identity_status: "unresolved",
  identity_claims: [],
  metrics: { fixture_owner: "store_b" },
};
const rejectedRelabel = policy.applyDoudianScanContext(unresolvedSnapshot, scanContext, {
  source: "doudian",
  tabUrl: "https://fxg.jinritemai.com/ffa/morder/order/list",
  runId: "scan_a",
  tabId: 7,
  now: 2000,
});
assert.strictEqual(rejectedRelabel.applied, false);
assert.strictEqual(rejectedRelabel.reason, "fresh_identity_required");
assert.strictEqual(rejectedRelabel.snapshot.metrics.fixture_owner, "store_b");
assert.strictEqual(rejectedRelabel.snapshot.store, undefined, "an old lease must never relabel an unresolved current snapshot");
assert.strictEqual(unresolvedSnapshot.store, undefined);

const realClaim = policy.applyDoudianScanContext({
  ...unresolvedSnapshot,
  identity_status: "resolved_by_bridge",
  identity_claims: [{
    kind: "douyin_shop_id", raw_id: "778899", confidence: "high", evidence_source: "data_attribute",
  }],
}, scanContext, {
  source: "doudian", tabUrl: "https://fxg.jinritemai.com/ffa/mshop/homepage/index",
  runId: "scan_a", tabId: 7, now: 2000,
});
assert.strictEqual(realClaim.applied, false);
assert.strictEqual(realClaim.reason, "fresh_identity_claim");
assert.strictEqual(realClaim.snapshot.store, undefined, "the bridge, not the old lease, must resolve the current claim");

const legacyPrehashedStoreIsNotProof = policy.applyDoudianScanContext({
  ...unresolvedSnapshot,
  store: { key: "store_a", identity_source: "scan_session" },
}, scanContext, {
  source: "doudian", tabUrl: "https://fxg.jinritemai.com/ffa/morder/order/list",
  runId: "scan_a", tabId: 7, now: 2000,
});
assert.strictEqual(legacyPrehashedStoreIsNotProof.reason, "fresh_identity_required");

const conflictSnapshot = policy.applyDoudianScanContext({ ...unresolvedSnapshot, identity_status: "conflict" }, scanContext, {
  source: "doudian", tabUrl: "https://fxg.jinritemai.com/ffa/mshop/homepage/index", runId: "scan_a", tabId: 7, now: 2000,
});
assert.strictEqual(conflictSnapshot.applied, false);
assert.strictEqual(conflictSnapshot.reason, "identity_conflict");
assert.strictEqual(policy.applyDoudianScanContext(unresolvedSnapshot, scanContext, {
  source: "qianchuan", tabUrl: "https://qianchuan.jinritemai.com/home", runId: "scan_a", tabId: 7, now: 2000,
}).applied, false);
assert.strictEqual(policy.applyDoudianScanContext(unresolvedSnapshot, scanContext, {
  source: "doudian", tabUrl: "https://fxg.jinritemai.com/ffa/mshop/homepage/index", runId: "scan_a", tabId: 7, now: 12000,
}).reason, "expired");
assert.strictEqual(policy.applyDoudianScanContext(unresolvedSnapshot, scanContext, {
  source: "doudian", tabUrl: "https://fxg.jinritemai.com/ffa/mshop/homepage/index", runId: "scan_b", tabId: 7, now: 2000,
}).reason, "run_mismatch");
assert.strictEqual(policy.applyDoudianScanContext(unresolvedSnapshot, scanContext, {
  source: "doudian", tabUrl: "https://fxg.jinritemai.com/ffa/mshop/homepage/index", runId: "scan_a", tabId: 8, now: 2000,
}).reason, "tab_mismatch");
assert.strictEqual(scanContext.expires_at, 11000, "using the lease must not extend its fixed expiry");

const fullScanLease = policy.createDoudianScanContext("store_a", {
  now: 1000,
  ttlMs: 900000,
  runId: "scan_full",
  tabId: 9,
  proof: {
    verified: true,
    store_key: "store_a",
    page_type: "overview",
    identity_source: "hmac_douyin_shop_id",
    verified_at: 1000,
  },
});
assert.strictEqual(fullScanLease.created_at, 1000);
assert.strictEqual(fullScanLease.expires_at, 901000);
assert.strictEqual(policy.applyDoudianScanContext(unresolvedSnapshot, fullScanLease, {
  source: "doudian", tabUrl: "https://fxg.jinritemai.com/ffa/fund-control/account-center",
  runId: "scan_full", tabId: 9, now: 900999,
}).reason, "fresh_identity_required");
assert.strictEqual(fullScanLease.expires_at, 901000, "processing a later page must not renew the lease");
assert.strictEqual(policy.applyDoudianScanContext(unresolvedSnapshot, fullScanLease, {
  source: "doudian", tabUrl: "https://fxg.jinritemai.com/ffa/fund-control/account-center",
  runId: "scan_full", tabId: 9, now: 901000,
}).reason, "expired");
const cappedLease = policy.createDoudianScanContext("store_a", {
  now: 1000,
  ttlMs: 3600000,
  runId: "scan_capped",
  tabId: 10,
  proof: {
    verified: true, store_key: "store_a", page_type: "overview",
    identity_source: "hmac_douyin_shop_id", verified_at: 1000,
  },
});
assert.strictEqual(cappedLease.expires_at, 901000, "a Doudian identity lease is hard-capped at 15 minutes");

console.log("scan-policy tests passed");
