const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const sidepanel = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");
const start = sidepanel.indexOf("const DOUDIAN_OVERVIEW_URL");
const end = sidepanel.indexOf("\nasync function runQuickScan", start);
assert.ok(start >= 0 && end > start, "simple store preparation implementation should exist");

function createHarness(options = {}) {
  const calls = { query: [], update: [], create: [], messages: [], selects: [], dashboard: 0 };
  let currentTab = options.activeTab || {
    id: 11,
    windowId: 2,
    status: "complete",
    url: "https://fxg.jinritemai.com/ffa/mshop/homepage/index",
  };
  let dashboardStoreKey = String(options.selectedStoreKey || "");
  const acceptedStoreKey = String(options.acceptedStoreKey || "store_abc123");
  const context = {
    console,
    selectedStoreKey: dashboardStoreKey,
    selectedQianchuanAccount: "",
    setTimeout: (callback) => { callback(); return 1; },
    clearTimeout: () => undefined,
    chrome: {
      tabs: {
        query: async (query) => {
          calls.query.push(query);
          if (query.active) return currentTab ? [currentTab] : [];
          return Array.isArray(options.openTabs) ? options.openTabs : [];
        },
        get: async (tabId) => Number(tabId) === Number(currentTab?.id) ? currentTab : null,
        update: async (tabId, patch) => {
          calls.update.push({ tabId, patch });
          currentTab = { ...currentTab, ...patch, id: tabId, status: "complete" };
          return currentTab;
        },
        create: async (patch) => {
          calls.create.push(patch);
          currentTab = { id: 99, windowId: 2, status: "complete", ...patch };
          return currentTab;
        },
      },
      runtime: {
        sendMessage: async (message) => {
          calls.messages.push(message);
          return options.syncResponse || {
            ok: true,
            result: {
              store: { key: acceptedStoreKey },
              store_auto_confirmed: options.autoConfirmed !== false,
              selected_store_key: options.autoConfirmed === false ? "" : acceptedStoreKey,
            },
          };
        },
      },
    },
    bridgeFetch: async (url, request) => {
      calls.selects.push({ url, request, body: JSON.parse(request.body) });
      dashboardStoreKey = acceptedStoreKey;
      return { ok: true };
    },
    loadDashboard: async () => {
      calls.dashboard += 1;
      context.selectedStoreKey = dashboardStoreKey || acceptedStoreKey;
    },
  };
  vm.runInNewContext(
    `${sidepanel.slice(start, end)}\n` +
      "globalThis.__flow = { ensureCurrentStoreForScan, simpleStorePreparationError };",
    context,
    { filename: "simple-store-preparation.vm.js" },
  );
  return { ...context.__flow, context, calls };
}

(async () => {
  {
    const harness = createHarness();
    const result = await harness.ensureCurrentStoreForScan();
    assert.equal(result, "store_abc123");
    assert.equal(harness.calls.messages.length, 1);
    assert.deepEqual(JSON.parse(JSON.stringify(harness.calls.messages[0])), {
      type: "sync-current-page",
      source_only: "doudian",
      target_tab_id: 11,
      purpose: "start-store-scan",
    });
    assert.equal(harness.calls.selects.length, 0, "new Agents select atomically from the exact receipt");
  }

  {
    const harness = createHarness({
      activeTab: { id: 8, windowId: 2, status: "complete", url: "https://example.com/" },
      autoConfirmed: false,
    });
    const result = await harness.ensureCurrentStoreForScan();
    assert.equal(result, "store_abc123");
    assert.equal(harness.calls.create.length, 1, "first use should open Doudian instead of showing a store picker");
    assert.equal(harness.calls.selects.length, 1, "older Agents use only the exact accepted receipt as fallback");
    assert.equal(harness.calls.selects[0].url, "/stores/select");
    assert.equal(harness.calls.selects[0].body.store_key, "store_abc123");
  }

  {
    const harness = createHarness({
      syncResponse: { ok: true, result: { store: null } },
    });
    await assert.rejects(
      harness.ensureCurrentStoreForScan(),
      /暂时不能用于经营判断/,
    );
    assert.equal(harness.calls.selects.length, 0, "an unresolved current page must fail closed");
  }

  {
    const harness = createHarness({
      activeTab: { id: 3, windowId: 2, status: "complete", url: "https://example.com/" },
      selectedStoreKey: "store_existing",
    });
    const result = await harness.ensureCurrentStoreForScan();
    assert.equal(result, "store_existing");
    assert.equal(harness.calls.create.length, 0);
    assert.equal(harness.calls.messages.length, 0, "an already prepared store can continue from another workspace tab");
  }

  console.log("simple store preparation tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
