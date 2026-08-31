const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");

function sourceBetween(startMarker, endMarker) {
  const start = script.indexOf(startMarker);
  const end = script.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `${startMarker} should be extractable`);
  return script.slice(start, end);
}

function loadQuickScan(response) {
  const calls = { dashboard: 0, details: [], messages: [] };
  const detail = { set textContent(value) { calls.details.push(value); }, get textContent() { return calls.details.at(-1) || ""; } };
  const doudianTab = {
    id: 17,
    status: "complete",
    url: "https://fxg.jinritemai.com/ffa/mshop/homepage/index",
  };
  const context = {
    Date,
    setTimeout(callback) { callback(); return 1; },
    QUICK_SCAN_PAGE_IDS: Object.freeze(["overview", "orders", "products", "shelf"]),
    selectedStoreKey: "store-key-a",
    navigateToWorkspaceElement: () => undefined,
    loadDashboard: async () => { calls.dashboard += 1; },
    document: { getElementById: (id) => id === "scan-detail" ? detail : { focus() {} } },
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
          return response;
        },
      },
    },
  };
  vm.runInNewContext(
    `${sourceBetween("function normalizeCoreRefreshPageIds", "\nasync function maybeReturnFromTargetedScan")}\n`
      + "globalThis.__quick = runQuickScan;",
    context,
    { filename: "scan-launch-quick.vm.js" },
  );
  return { run: context.__quick, calls };
}

function loadRecovery(response) {
  const calls = { dashboard: 0, messages: [] };
  const context = {
    selectedStoreKey: "store-a",
    selectedQianchuanAccount: "account-a",
    openAgentRepairGuide: async () => undefined,
    navigateToWorkspaceElement: () => undefined,
    document: { getElementById: () => null },
    loadDashboard: async () => { calls.dashboard += 1; },
    chrome: {
      tabs: { create: async () => undefined },
      runtime: { sendMessage: async (message) => { calls.messages.push(message); return response; } },
    },
  };
  vm.runInNewContext(
    `${sourceBetween("async function runScanRecovery", "\nfunction renderScanReceipt")}\n`
      + "globalThis.__recovery = runScanRecovery;",
    context,
    { filename: "scan-launch-recovery.vm.js" },
  );
  return { run: context.__recovery, calls };
}

(async () => {
  const busy = { ok: true, started: false, code: "SCAN_BUSY", run_id: "existing-run", message: "已有巡检正在运行，请等待当前进度" };

  {
    const runtime = loadQuickScan(busy);
    const button = { disabled: false, textContent: "快速巡店" };
    const result = await runtime.run(button, { purpose: "quick_scan", pageIds: ["overview"] });
    assert.equal(result.started, false);
    assert.equal(runtime.calls.dashboard, 2, "quick scan should reconcile the accepted store before attaching to the existing run");
    assert.deepEqual(JSON.parse(JSON.stringify(runtime.calls.messages[0])), {
      type: "sync-current-page",
      source_only: "doudian",
      target_tab_id: 17,
      purpose: "start-store-scan",
    });
    assert.equal(runtime.calls.messages[1]?.type, "start-full-scan");
    assert.match(runtime.calls.details.at(-1), /已有巡检正在运行/);
    assert.match(button.textContent, /巡检正在进行|等待/);
    assert.doesNotMatch(button.textContent, /已启动/);
    assert.equal(button.disabled, false);
  }

  {
    const runtime = loadRecovery(busy);
    const button = { textContent: "只重试这一页" };
    const result = await runtime.run({ kind: "retry_page" }, { id: "funds", error_code: "PAGE_LOAD_TIMEOUT" }, button);
    assert.equal(result.started, false);
    assert.equal(runtime.calls.dashboard, 1, "busy recovery should refresh the existing scan instead of claiming a retry");
    assert.match(button.textContent, /巡检正在运行|等待/);
    assert.doesNotMatch(button.textContent, /已启动本页重试|已重新启动巡检/);
  }

  {
    const runtime = loadRecovery({ ok: true, started: false, code: "SCAN_NOT_STARTED", message: "scan did not start" });
    await assert.rejects(
      runtime.run({ kind: "retry_scan" }, { id: "", scope: "full", error_code: "PAGE_LOAD_TIMEOUT" }, { textContent: "重试" }),
      (error) => error.code === "SCAN_NOT_STARTED",
    );
    assert.equal(runtime.calls.dashboard, 0);
  }

  console.log("scan launch UI truthfulness tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
