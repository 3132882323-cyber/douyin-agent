const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const background = fs.readFileSync(path.join(__dirname, "background.js"), "utf8");
const blockStart = background.indexOf("function normalizePurposeSyncOptions");
const blockEnd = background.indexOf("\nasync function runAuthorizedExecution", blockStart);
assert.ok(blockStart >= 0 && blockEnd > blockStart, "purpose-sync implementation block should exist");

function loadPurposeSync(overrides = {}) {
  const calls = { collect: [], store: [], storageSet: [], inspect: [], grants: [] };
  const activeTab = overrides.activeTab || {
    id: 17,
    windowId: 3,
    title: "千川计划",
    url: "https://qianchuan.jinritemai.com/uni-prom/overall?aavid=123",
  };
  const context = {
    console,
    URL,
    chrome: {
      tabs: {
        query: async () => [activeTab],
        get: async (tabId) => Number(tabId) === Number(activeTab.id) ? activeTab : null,
      },
      storage: {
        local: {
          get: async () => ({}),
          set: async (value) => { calls.storageSet.push(value); },
        },
      },
    },
    DianAgentScanPolicy: {
      isQianchuanUrl: (url) => String(url || "").includes("qianchuan.jinritemai.com"),
      selectQianchuanSyncTab: () => ({ tab: activeTab, matchedBy: "recent" }),
    },
    querySourceTabs: async () => [activeTab],
    inspectPlatformPage: async (tabId, source) => { calls.inspect.push({ tabId, source }); },
    collectFromTab: async (source, tab, reason, options) => {
      calls.collect.push({ source, tab, reason, options });
      return overrides.collectResponse || {
        ok: true,
        page_type: "campaigns",
        quality: { score: 92 },
        snapshot: { source: "qianchuan", page_type: "campaigns", captured_at: 1 },
      };
    },
    bridgeFetch: async (url, options) => {
      calls.grants.push({ url, options });
      return {
        ok: overrides.grantOk !== false,
        json: async () => overrides.grantResult || { current_page_token: "a".repeat(64) },
      };
    },
    responseJson: async (response) => response.json(),
    storeAndPush: async (source, snapshot, options) => {
      calls.store.push({ source, snapshot, options });
      return overrides.storeResult || {
        ok: true,
        accepted_for_current_data: true,
        account: { key: options.expectedAccountKey || "account-a", label: "账户 A" },
      };
    },
  };
  vm.runInNewContext(
    `${background.slice(blockStart, blockEnd)}\n` +
      "globalThis.__purposeSync = { normalizePurposeSyncOptions, syncCurrentPage };",
    context,
    { filename: "background-purpose-sync.vm.js" },
  );
  return { ...context.__purposeSync, calls };
}

(async () => {
  {
    const { normalizePurposeSyncOptions } = loadPurposeSync();
    const normalized = normalizePurposeSyncOptions({
      expectedPageTypes: [" Campaigns ", "campaigns", "qianchuan_live", "bad value!"],
      expectedAccountKey: " ACCOUNT-A ",
      expectedStoreKey: " STORE-A ",
      purpose: "Promotion Plan",
    });
    assert.deepEqual(Array.from(normalized.expectedPageTypes), ["campaigns", "qianchuan_live"]);
    assert.equal(normalized.expectedAccountKey, "account-a");
    assert.equal(normalized.expectedStoreKey, "store-a");
    assert.equal(normalized.purpose, "promotion-plan");
    assert.equal(normalizePurposeSyncOptions({ purpose: "素材同步" }).purpose, "素材同步");
  }

  {
    const { syncCurrentPage, calls } = loadPurposeSync({
      collectResponse: {
        ok: true,
        page_type: "live_dashboard",
        quality: { score: 95 },
        snapshot: { source: "qianchuan", page_type: "live_dashboard", captured_at: 1 },
      },
    });
    await assert.rejects(
      syncCurrentPage("qianchuan", { expectedPageTypes: ["campaigns", "qianchuan_live"], purpose: "promotion_plan" }),
      (error) => error.code === "PAGE_TYPE_MISMATCH" && /错误页面未保存/.test(error.message),
    );
    assert.equal(calls.collect.length, 1);
    assert.equal(calls.collect[0].options.deferPush, true);
    assert.equal(calls.store.length, 0, "wrong page type must be rejected before storeAndPush");
    assert.equal(calls.storageSet.length, 1, "a rejected attempt is recorded separately from a successful sync");
    assert.ok(calls.storageSet[0].lastSyncAttemptAt > 0);
    assert.equal(calls.storageSet[0].lastSuccessfulSync, undefined);
  }

  {
    const { syncCurrentPage, calls } = loadPurposeSync();
    const result = await syncCurrentPage("qianchuan", {
      expectedPageTypes: ["campaigns"],
      expectedAccountKey: "ACCOUNT-A",
      expectedStoreKey: "STORE-A",
      purpose: "promotion_plan",
    });
    assert.equal(result.page_type, "campaigns");
    assert.equal(result.purpose, "promotion_plan");
    assert.deepEqual(Array.from(result.expected_page_types), ["campaigns"]);
    assert.equal(result.account.key, "account-a");
    assert.equal(calls.store.length, 1);
    assert.equal(calls.store[0].options.expectedAccountKey, "account-a");
    assert.equal(calls.store[0].options.expectedStoreKey, "store-a");
    assert.equal(calls.storageSet.length, 2);
    assert.ok(calls.storageSet[0].lastSyncAttemptAt > 0);
    assert.ok(calls.storageSet[1].lastSuccessfulSync > 0);
    assert.equal(calls.storageSet[1].lastSuccessfulSync, calls.storageSet[1].lastSyncAttempt);
  }

  {
    const { syncCurrentPage, calls } = loadPurposeSync();
    const result = await syncCurrentPage("qianchuan");
    assert.equal(result.page_type, "campaigns", "legacy call without expectations should remain supported");
    assert.equal(result.purpose, "manual");
    assert.equal(calls.collect[0].options.deferPush, true);
    assert.equal(calls.store.length, 1);
  }

  {
    const { syncCurrentPage, calls } = loadPurposeSync({
      collectResponse: {
        ok: true,
        page_type: "unknown",
        quality: { score: 0 },
        snapshot: { source: "qianchuan", page_type: "unknown", captured_at: 1 },
      },
    });
    await assert.rejects(syncCurrentPage("qianchuan"), (error) => error.code === "PAGE_TYPE_UNRECOGNIZED");
    assert.equal(calls.store.length, 0);
  }

  {
    const { syncCurrentPage, calls } = loadPurposeSync({
      collectResponse: {
        ok: true,
        page_type: "campaigns",
        quality: { score: 95 },
        snapshot: { source: "qianchuan", page_type: "live_dashboard", captured_at: 1 },
      },
    });
    await assert.rejects(syncCurrentPage("qianchuan"), (error) => error.code === "PAGE_TYPE_CONFLICT");
    assert.equal(calls.store.length, 0, "conflicting wrapper and snapshot types must not be stored");
  }

  {
    const { syncCurrentPage, calls } = loadPurposeSync({
      storeResult: { ok: false, error_code: "ACCOUNT_SCOPE_MISMATCH", error: "当前账户与预期账户不一致" },
    });
    await assert.rejects(
      syncCurrentPage("qianchuan", { expectedAccountKey: "account-b" }),
      (error) => error.code === "ACCOUNT_SCOPE_MISMATCH",
    );
    assert.equal(calls.store[0].options.expectedAccountKey, "account-b");
    assert.equal(calls.storageSet.length, 1);
    assert.ok(calls.storageSet[0].lastSyncAttemptAt > 0);
    assert.equal(calls.storageSet[0].lastSuccessfulSync, undefined);
  }

  {
    const { syncCurrentPage, calls } = loadPurposeSync({
      storeResult: {
        ok: false,
        quarantined: true,
        accepted_for_current_data: false,
        error_code: "STORE_IDENTITY_CONFLICT",
        error: "冲突快照仅进入隔离区",
      },
    });
    await assert.rejects(
      syncCurrentPage("qianchuan"),
      (error) => error.code === "STORE_IDENTITY_CONFLICT",
    );
    assert.equal(calls.storageSet.length, 1, "quarantine records only the attempt");
    assert.ok(calls.storageSet[0].lastSyncAttemptAt > 0);
    assert.equal(calls.storageSet[0].lastSuccessfulSync, undefined, "quarantine must never advance success time");
  }

  {
    const { syncCurrentPage, calls } = loadPurposeSync({
      activeTab: {
        id: 23,
        windowId: 4,
        title: "抖店经营概览",
        url: "https://fxg.jinritemai.com/ffa/mshop/homepage/index",
      },
      collectResponse: {
        ok: true,
        page_type: "overview",
        quality: { score: 90 },
        snapshot: {
          source: "doudian",
          page_type: "overview",
          captured_at: Date.now(),
        },
      },
      storeResult: {
        ok: true,
        accepted: true,
        accepted_for_current_data: true,
        quarantined: false,
        store: { key: "store-a", label: "当前抖店" },
        store_auto_confirmed: true,
        selected_store_key: "store-a",
        selected_account_key: "",
      },
    });
    const result = await syncCurrentPage("doudian", { targetTabId: 23, purpose: "start_store_scan" });
    assert.equal(result.store_auto_confirmed, true);
    assert.equal(result.selected_store_key, "store-a");
    assert.equal(calls.grants.length, 1, "the active current-page flow must request a one-time store grant");
    assert.equal(calls.grants[0].url, "/stores/current-page-grant");
    assert.equal(calls.collect[0].options.deferPush, true);
    assert.equal(calls.store.length, 1, "the worker must attach the grant and validate the exact receipt");
    assert.equal(calls.store[0].options.currentPageToken, "a".repeat(64));
    assert.equal(calls.storageSet.length, 2);
    assert.ok(calls.storageSet[0].lastSyncAttemptAt > 0);
    assert.ok(calls.storageSet[1].lastSuccessfulSync > 0);
  }

  assert.match(background, /expectedPageTypes:\s*message\.expected_page_types/);
  assert.match(background, /expectedAccountKey:\s*message\.expected_account_key/);
  assert.match(background, /expectedStoreKey:\s*message\.expected_store_key/);
  assert.match(background, /purpose:\s*message\.purpose/);
  assert.match(background, /targetTabId:\s*Number\(message\.target_tab_id \|\| 0\)/);
  assert.match(background, /current_page_context:\s*\{ current_page_token:/);
  assert.match(background, /sendResponse\(\{ ok: false, code: error\.code \|\| "", error:/);

  console.log("background purpose sync tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
