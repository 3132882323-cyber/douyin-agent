const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");
const blockStart = script.indexOf("function normalizeCoreRefreshPageIds");
const blockEnd = script.indexOf("\nasync function maybeReturnFromTargetedScan", blockStart);
assert.ok(blockStart >= 0 && blockEnd > blockStart, "core refresh implementation should be extractable");

function loadCoreRefresh() {
  const calls = { messages: [], navigation: [], details: [] };
  const detail = { set textContent(value) { calls.details.push(value); } };
  const doudianTab = {
    id: 17,
    status: "complete",
    url: "https://fxg.jinritemai.com/ffa/mshop/homepage/index",
  };
  const context = {
    console,
    Date,
    setTimeout(callback) { callback(); return 1; },
    QUICK_SCAN_PAGE_IDS: Object.freeze(["overview", "orders", "products", "shelf"]),
    selectedStoreKey: "store-key-a",
    selectedQianchuanAccount: "account-a",
    navigateToWorkspaceElement: (...args) => { calls.navigation.push(args); },
    loadDashboard: async () => undefined,
    document: {
      getElementById: (id) => id === "scan-detail" ? detail : { focus() {} },
    },
    chrome: {
      tabs: {
        async query() { return [doudianTab]; },
        async get(tabId) { return tabId === doudianTab.id ? doudianTab : null; },
        async update() { return doudianTab; },
        async create() { return doudianTab; },
      },
      runtime: {
        sendMessage: async (message) => {
          calls.messages.push(message);
          if (message.type === "sync-current-page") {
            return {
              ok: true,
              result: {
                store: { key: "store-key-a" },
                store_auto_confirmed: true,
                selected_store_key: "store-key-a",
              },
            };
          }
          return { ok: true, started: true, run_id: "core-run-1" };
        },
      },
    },
  };
  vm.runInNewContext(
    `${script.slice(blockStart, blockEnd)}\n` +
      "globalThis.__coreRefresh = { normalizeCoreRefreshPageIds, runQuickScan };",
    context,
    { filename: "connection-guide-core-refresh.vm.js" },
  );
  return { ...context.__coreRefresh, calls };
}

(async () => {
  const runtime = loadCoreRefresh();
  assert.deepEqual(
    Array.from(runtime.normalizeCoreRefreshPageIds(["orders", "bad-page", "orders", "products"])),
    ["orders", "products"],
  );
  assert.deepEqual(Array.from(runtime.normalizeCoreRefreshPageIds([])), ["overview", "orders", "products", "shelf"]);
  assert.deepEqual(Array.from(runtime.normalizeCoreRefreshPageIds([], { fallback: false })), []);

  const button = { disabled: false, textContent: "刷新" };
  await runtime.runQuickScan(button, {
    purpose: "refresh_core_data",
    pageIds: ["orders", "bad-page", "orders", "products"],
  });
  assert.deepEqual(JSON.parse(JSON.stringify(runtime.calls.messages[0])), {
    type: "sync-current-page",
    source_only: "doudian",
    target_tab_id: 17,
    purpose: "start-store-scan",
  });
  assert.deepEqual(JSON.parse(JSON.stringify(runtime.calls.messages[1])), {
    type: "start-full-scan",
    scan_scope: "quick",
    store_key: "store-key-a",
    account_key: "",
    page_ids: ["orders", "products"],
  });
  assert.equal(runtime.calls.messages.some((message) => message.type === "run-authorized-execution"), false);
  assert.equal(runtime.calls.navigation[0][1].title, "刷新核心经营数据");
  assert.match(runtime.calls.details.at(-1), /2 个核心页面/);

  const beforeEmptyRefresh = runtime.calls.messages.length;
  await assert.rejects(
    () => runtime.runQuickScan(button, { purpose: "refresh_core_data", pageIds: [] }),
    /精确待刷新页面/,
  );
  assert.equal(runtime.calls.messages.length, beforeEmptyRefresh, "missing exact scope must not expand into a four-page scan");

  assert.match(script, /action === "quick_scan" \|\| action === "refresh_core_data"/);
  assert.match(script, /operationalTruth\(currentConnectionGuide \|\| \{\}\)\.refreshPageIds/);
  assert.match(script, /currentActionId:\s*next\.id/);
  assert.match(script, /guideView\.operationalLabel/);
  assert.match(script, /guide\.dataset\.currentlyReady = String\(guideView\.currentlyReady\)/);

  console.log("connection guide core refresh UI tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
