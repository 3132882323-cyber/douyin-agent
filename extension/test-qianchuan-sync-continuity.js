const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");
const blockStart = script.indexOf("function normalizeQianchuanSyncOptions");
const blockEnd = script.indexOf("\ndocument.addEventListener(\"DOMContentLoaded\"", blockStart);
assert.ok(blockStart >= 0 && blockEnd > blockStart, "qianchuan sync implementation should be extractable");

function resultFor(storeKey, accountKey, pageType = "qianchuan_campaigns") {
  return {
    ok: true,
    result: {
      page_type: pageType,
      store: { key: storeKey },
      account: { key: accountKey, label: accountKey },
      quality: { row_count: 2 },
      tab: { id: 11 },
    },
  };
}

function createHarness(options = {}) {
  const pending = [];
  const writes = [];
  const ui = [];
  let dashboardLoads = 0;
  let dashboardFailures = 0;
  const sandbox = {
    Map,
    JSON,
    Date,
    Promise,
    Error,
    Set,
    LABELS: { qianchuan_campaigns: "商品推广" },
    selectedQianchuanAccount: "",
    selectedStoreKey: "",
    qianchuanFeatureDeferred: false,
    chrome: {
      runtime: {
        sendMessage(message) {
          return new Promise((resolve) => pending.push({ message, resolve }));
        },
      },
      storage: { local: { async set(value) {
        writes.push(value);
        if (options.storageError) throw options.storageError;
      } } },
    },
    document: { getElementById: () => ({ textContent: "" }) },
    setQianchuanSyncUi(state, detail) { ui.push({ state, detail }); },
    qianchuanSyncAgeLabel: () => "刚刚",
    restoreQianchuanSyncUi(success, attempt) {
      sandbox.latestQianchuanSyncUiReceipt = { success, attempt };
      ui.push({ state: "success", detail: "刚刚" });
    },
    renderDashboardLoadFailure() { dashboardFailures += 1; },
    async loadDashboard() {
      dashboardLoads += 1;
      if (options.dashboardError) throw options.dashboardError;
    },
  };
  const declarations = `
    const qianchuanSyncPromises = new Map();
    let qianchuanSyncGeneration = 0;
    let qianchuanSyncActiveCount = 0;
    let latestQianchuanSyncUiReceipt = { success: null, attempt: null };
    const QIANCHUAN_SYNC_ATTEMPT_KEY = "lastQianchuanManualSyncAttempt";
  `;
  vm.runInNewContext(`${declarations}\n${script.slice(blockStart, blockEnd)}`, sandbox);
  return { sandbox, pending, writes, ui, dashboardLoads: () => dashboardLoads, dashboardFailures: () => dashboardFailures };
}

test("different store/account sync contracts do not share an in-flight promise", async () => {
  const harness = createHarness();
  const first = harness.sandbox.syncRecentQianchuanPage({
    expectedStoreKey: "store-a",
    expectedAccountKey: "account-a",
    expectedPageTypes: ["qianchuan_campaigns"],
    purpose: "计划同步",
  });
  const second = harness.sandbox.syncRecentQianchuanPage({
    expectedStoreKey: "store-b",
    expectedAccountKey: "account-b",
    expectedPageTypes: ["qianchuan_campaigns"],
    purpose: "计划同步",
  });
  assert.equal(harness.pending.length, 2);
  assert.equal(harness.pending[0].message.expected_store_key, "store-a");
  assert.equal(harness.pending[1].message.expected_store_key, "store-b");

  harness.pending[0].resolve(resultFor("store-a", "account-a"));
  harness.pending[1].resolve(resultFor("store-b", "account-b"));
  const [firstResult, secondResult] = await Promise.all([first, second]);
  assert.equal(firstResult.store.key, "store-a");
  assert.equal(secondResult.store.key, "store-b");
  assert.equal(harness.dashboardLoads(), 1, "only the latest invocation may refresh shared UI state");
});

test("the exact same sync contract may share one in-flight request", async () => {
  const harness = createHarness();
  const options = {
    expectedStoreKey: "store-a",
    expectedAccountKey: "account-a",
    expectedPageTypes: ["qianchuan_campaigns"],
    purpose: "计划同步",
  };
  const first = harness.sandbox.syncRecentQianchuanPage(options);
  const second = harness.sandbox.syncRecentQianchuanPage(options);
  assert.equal(harness.pending.length, 1);
  harness.pending[0].resolve(resultFor("store-a", "account-a"));
  const [left, right] = await Promise.all([first, second]);
  assert.equal(left.account.key, "account-a");
  assert.equal(right.account.key, "account-a");
});

test("a successful transport response with the wrong scope is rejected", async () => {
  const harness = createHarness();
  const operation = harness.sandbox.syncRecentQianchuanPage({
    expectedStoreKey: "store-a",
    expectedAccountKey: "account-a",
    expectedPageTypes: ["qianchuan_campaigns"],
    purpose: "计划同步",
  });
  harness.pending[0].resolve(resultFor("store-b", "account-b"));
  await assert.rejects(operation, (error) => error.code === "SYNC_RESULT_STORE_MISMATCH");
  assert.equal(harness.dashboardLoads(), 0);
});

test("successful data sync is not reclassified when workbench refresh fails", async () => {
  const harness = createHarness({ dashboardError: new Error("dashboard unavailable") });
  const operation = harness.sandbox.syncRecentQianchuanPage({
    expectedStoreKey: "store-a",
    expectedAccountKey: "account-a",
    expectedPageTypes: ["qianchuan_campaigns"],
    purpose: "计划同步",
  });
  harness.pending[0].resolve(resultFor("store-a", "account-a"));
  const result = await operation;
  assert.equal(result.account.key, "account-a");
  assert.equal(harness.dashboardFailures(), 1);
  assert.ok(harness.writes.some((entry) => entry.lastQianchuanManualSync?.status === "success"));
  assert.equal(harness.writes.some((entry) => entry.lastQianchuanManualSyncAttempt?.status === "error"), false);
});

test("a failed receipt write cannot leave the floating sync UI stuck in progress", async () => {
  const harness = createHarness({ storageError: new Error("storage unavailable") });
  const operation = harness.sandbox.syncRecentQianchuanPage({ purpose: "计划同步" });
  harness.pending[0].resolve({ ok: false, code: "NO_PAGE", error: "请先打开千川计划页" });
  await assert.rejects(operation, /请先打开千川计划页/);
  assert.equal(harness.ui.at(-1)?.state, "error");
  assert.match(harness.ui.at(-1)?.detail || "", /请先打开千川计划页/);
});
