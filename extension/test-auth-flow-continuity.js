"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const auth = require("./bridge-auth.js");

const EXTENSION_ID = "a".repeat(32);
const BRIDGE_URL = "http://127.0.0.1:8765";
const VERSION = JSON.parse(fs.readFileSync(path.join(__dirname, "manifest.json"), "utf8")).version;
const background = fs.readFileSync(path.join(__dirname, "background.js"), "utf8");
const sidepanel = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");

function response(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return payload; },
    clone() { return response(status, payload); },
  };
}

function memoryStorage() {
  const values = {};
  return {
    values,
    async get(key) { return { [key]: values[key] }; },
    async set(patch) { Object.assign(values, patch); },
    async remove(key) { delete values[key]; },
  };
}

function functionBlock(source, signature, nextSignature) {
  const start = source.indexOf(signature);
  const end = source.indexOf(nextSignature, start);
  assert.ok(start >= 0 && end > start, `${signature} block must exist`);
  return source.slice(start, end);
}

(async () => {
  let activeToken = "";
  let issuedSessions = 0;
  let pushCount = 0;
  let grantCount = 0;
  let authStatusCount = 0;
  const calls = [];

  // Deliberately model the Chromium case that caused the split: neither the
  // authenticated POST nor the following GET has Origin.  The explicit ID is
  // only a routing claim; the fake Agent still binds it to the signed token.
  const fetchImpl = async (url, options = {}) => {
    const headers = options.headers || {};
    calls.push({ url, method: options.method || "GET", headers: { ...headers }, body: options.body || "" });
    assert.equal(headers.Origin, undefined, "this regression must remain independent of Origin");
    const claimedId = String(headers[auth.EXTENSION_ID_HEADER] || "");
    if (url.endsWith("/auth/session")) {
      const requestedId = JSON.parse(options.body || "{}").extension_id;
      assert.equal(requestedId, EXTENSION_ID);
      assert.equal(claimedId, EXTENSION_ID);
      issuedSessions += 1;
      activeToken = `signed-current-extension-${issuedSessions}`;
      return response(200, {
        ok: true,
        access_token: activeToken,
        token_type: "DianAgent",
        expires_in: 600,
        expires_at: 2_000_000_600,
        install_id: "install-current",
        subject: EXTENSION_ID,
        extension_version: VERSION,
      });
    }
    if (claimedId !== EXTENSION_ID || headers["X-Dian-Agent-Token"] !== activeToken) {
      return response(401, {
        error: "agent_session_invalid",
        message: "The local Agent session belongs to another client.",
      });
    }
    if (url.endsWith("/stores/current-page-grant")) {
      grantCount += 1;
      return response(200, {
        ok: true,
        current_page_token: "b".repeat(64),
      });
    }
    if (url.endsWith("/push")) {
      pushCount += 1;
      const body = JSON.parse(options.body || "{}");
      assert.equal(body.source, "doudian");
      assert.equal(body.data?.page_type, "overview");
      assert.equal(
        body.current_page_context?.current_page_token,
        "b".repeat(64),
        "the accepted push must carry the one-time capability issued for this exact page",
      );
      return response(200, {
        ok: true,
        store: { key: "store-a" },
        account: null,
      });
    }
    if (url.endsWith("/auth/status")) {
      authStatusCount += 1;
      return response(200, {
        authentication_contract_version: 1,
        authenticated: true,
        client_kind: "browser_extension",
        session_subject: EXTENSION_ID,
        session_extension_version: VERSION,
        required_extension_version: VERSION,
      });
    }
    throw new Error(`unexpected Agent path: ${url}`);
  };

  const bridgeClient = auth.createClient({
    baseUrl: BRIDGE_URL,
    extensionId: EXTENSION_ID,
    extensionVersion: VERSION,
    storage: memoryStorage(),
    now: () => 2_000_000_000_000,
    fetchImpl,
  });

  // Run the real current-page synchronizer.  The content-script collection is
  // stubbed only at the page boundary; its bridge result comes from a real
  // authenticated /push call through the shared extension auth client.
  const purposeStart = background.indexOf("function normalizePurposeSyncOptions");
  const purposeEnd = background.indexOf("\nasync function runAuthorizedExecution", purposeStart);
  assert.ok(purposeStart >= 0 && purposeEnd > purposeStart, "current-page sync block must exist");
  const doudianTab = {
    id: 17,
    windowId: 3,
    title: "抖店经营概览",
    url: "https://fxg.jinritemai.com/ffa/mshop/homepage/index",
  };
  const syncSandbox = {
    console,
    URL,
    chrome: {
      tabs: {
        async get(tabId) { return tabId === doudianTab.id ? doudianTab : null; },
        async query() { return [doudianTab]; },
      },
      storage: {
        local: {
          async get() { return {}; },
          async set() {},
        },
      },
    },
    DianAgentScanPolicy: {
      isQianchuanUrl() { return false; },
      selectQianchuanSyncTab() { return { tab: null, matchedBy: "none" }; },
    },
    async querySourceTabs() { return []; },
    async inspectPlatformPage() {},
    async bridgeFetch(path, options) {
      return bridgeClient.authorizedFetch(path, options);
    },
    async responseJson(value) { return value.json(); },
    async collectFromTab(source, tab, reason, options) {
      assert.equal(source, "doudian");
      assert.equal(tab.id, doudianTab.id);
      assert.match(reason, /^manual-current-page-/);
      assert.equal(options?.deferPush, true, "current-page sync must remain candidate-only at the page boundary");
      return {
        ok: true,
        page_type: "overview",
        quality: { score: 100 },
        snapshot: { page_type: "overview", captured_at: 2_000_000_000_000 },
      };
    },
    async storeAndPush(source, snapshot, options) {
      assert.equal(options.currentPageToken, "b".repeat(64));
      return bridgeClient.fetchJson("/push", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
        body: JSON.stringify({
          source,
          data: snapshot,
          current_page_context: { current_page_token: options.currentPageToken },
        }),
      });
    },
  };
  vm.runInNewContext(
    `${background.slice(purposeStart, purposeEnd)}\nthis.__syncCurrentPage = syncCurrentPage;`,
    syncSandbox,
    { filename: "auth-flow-current-page-sync.vm.js" },
  );
  const syncResult = await syncSandbox.__syncCurrentPage("doudian", {
    targetTabId: doudianTab.id,
    purpose: "platform-assistant",
  });
  assert.equal(syncResult.page_type, "overview");
  assert.equal(grantCount, 1, "one current-page sync must consume exactly one Agent-issued grant");
  assert.equal(pushCount, 1, "the visible page success must represent exactly one accepted Agent push");

  // Run the real worker bridge verdict immediately after the accepted push.
  // This is the exact transition that previously changed from green in-page
  // status to a frozen workbench.
  const checkBridgeSource = functionBlock(
    background,
    "async function checkBridge(options = {})",
    "\nasync function getDashboard()",
  );
  const bridgeSandbox = {
    console,
    BRIDGE_URL,
    chrome: {
      runtime: {
        id: EXTENSION_ID,
        getManifest() { return { version: VERSION }; },
      },
    },
    async checkBridgeLiveness() { return { ok: true, data: { version: VERSION } }; },
    async checkActivationStatus() {
      return {
        ok: true,
        data: {
          agent_version: VERSION,
          required_extension_version: VERSION,
          installed_extension_version: VERSION,
          activation: { state: "active", ready: true, reload_required: false },
        },
      };
    },
    async reconcileRequiredExtensionVersion() {
      return {
        version_match: true,
        runtime_extension_version: VERSION,
        required_extension_version: VERSION,
        installed_extension_version: VERSION,
        reload_state: "matched",
      };
    },
    async fetchWithTimeout(url, options) {
      assert.match(url, /\/auth\/status$/);
      return bridgeClient.authorizedFetch("/auth/status", options);
    },
    async responseJson(value) { return value.json(); },
    async withTimeout(operation) { return operation; },
    async updateStatusBestEffort() {},
    async reportExtensionInstallSource() {},
  };
  vm.runInNewContext(
    `${checkBridgeSource}\nthis.__checkBridge = checkBridge;`,
    bridgeSandbox,
    { filename: "auth-flow-worker-verdict.vm.js" },
  );
  const initialReceipt = await bridgeSandbox.__checkBridge({ trigger: "page-sync-open-workbench" });
  const finalReceipt = await bridgeSandbox.__checkBridge({ trigger: "workbench-write-boundary" });
  for (const receipt of [initialReceipt, finalReceipt]) {
    assert.equal(receipt.ok, true);
    assert.equal(receipt.liveness_ok, true);
    assert.equal(receipt.authenticated, true);
    assert.equal(receipt.version_match, true);
  }
  assert.equal(authStatusCount, 2, "the workbench must authenticate both of its fail-closed gate checks");
  assert.equal(issuedSessions, 1, "the GET verification must reuse the page-sync session instead of splitting clients");

  const acceptanceSource = functionBlock(
    sidepanel,
    "function dashboardBridgeAccepted(backgroundFailure, bridge = {})",
    "\nfunction dashboardBridgeFailure(",
  );
  const acceptanceSandbox = {};
  vm.runInNewContext(acceptanceSource, acceptanceSandbox, {
    filename: "auth-flow-sidepanel-acceptance.vm.js",
  });
  assert.equal(acceptanceSandbox.dashboardBridgeAccepted(null, initialReceipt), true);
  assert.equal(acceptanceSandbox.dashboardBridgeAccepted(null, finalReceipt), true);
  assert.equal(
    !acceptanceSandbox.dashboardBridgeAccepted(null, finalReceipt),
    false,
    "a page sync followed by two authenticated worker receipts must not enter the frozen-workbench branch",
  );

  assert.ok(
    calls.every((call) => call.headers[auth.EXTENSION_ID_HEADER] === EXTENSION_ID),
    "session issue, page push and both workbench GETs must carry one canonical extension identity",
  );
  console.log("auth flow continuity tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
