"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const background = fs.readFileSync(path.join(__dirname, "background.js"), "utf8");
const scanPolicy = fs.readFileSync(path.join(__dirname, "scan-policy.js"), "utf8");
const admissionStart = background.indexOf("function clearDoudianScanContext");
const admissionEnd = background.indexOf("\nfunction mutateLocalStorage", admissionStart);
const scanStart = background.indexOf("async function scanOnePage");
const scanEnd = background.indexOf("\nasync function findQianchuanSeedTab", scanStart);
const conflictHelperStart = background.indexOf("function snapshotHasIdentityConflict");
const conflictHelperEnd = background.indexOf("\nfunction requireDoudianSnapshotIdentity", conflictHelperStart);
const liveScanStart = background.indexOf("async function rejectAfterForensicIdentityConflict");
const liveScanEnd = background.indexOf("\nfunction isMissingTabError", liveScanStart);
assert.ok(admissionStart >= 0 && admissionEnd > admissionStart, "snapshot admission helpers must remain testable");
assert.ok(scanStart >= 0 && scanEnd > scanStart, "full-scan page admission must remain testable");
assert.ok(conflictHelperStart >= 0 && conflictHelperEnd > conflictHelperStart, "shared conflict classifier must remain testable");
assert.ok(liveScanStart >= 0 && liveScanEnd > liveScanStart, "live full-scan path must remain testable");

const context = { globalThis: null };
context.globalThis = context;
vm.runInNewContext(scanPolicy, context, { filename: "scan-policy.vm.js" });
vm.runInNewContext(
  `const doudianScanContexts = new Map();\n${background.slice(admissionStart, admissionEnd)}\n` +
    "globalThis.__prepareScanSessionSnapshot = prepareScanSessionSnapshot;\n" +
    "globalThis.__setDoudianScanContext = (tabId, value) => doudianScanContexts.set(tabId, value);",
  context,
  { filename: "background-forensic-conflict-admission.vm.js" },
);

const conflict = {
  page_type: "orders",
  identity_status: "conflict",
  identity_conflicts: ["douyin_shop_id"],
  identity_claims: [
    { kind: "douyin_shop_id", raw_id: "shop-a", confidence: "high", evidence_source: "data_attribute" },
    { kind: "douyin_shop_id", raw_id: "shop-b", confidence: "high", evidence_source: "data_attribute" },
  ],
};
assert.equal(
  context.__prepareScanSessionSnapshot("doudian", conflict, {
    tab: { id: 31, url: "https://fxg.jinritemai.com/ffa/morder/order/list" },
  }),
  conflict,
  "an explicit identity conflict must reach the local Agent for forensic quarantine",
);
context.__setDoudianScanContext(31, {
  source: "doudian",
  store_key: "store-a",
  run_id: "scan-run-a",
  tab_id: 31,
  expires_at: Date.now() + 60_000,
  proof: { page_type: "overview", identity_source: "hmac_douyin_shop_id" },
});
assert.equal(
  context.__prepareScanSessionSnapshot("doudian", conflict, {
    tab: { id: 31, url: "https://fxg.jinritemai.com/ffa/morder/order/list" },
  }),
  conflict,
  "a run-scoped Doudian lease must not discard conflict evidence before Agent quarantine",
);
assert.throws(
  () => context.__prepareScanSessionSnapshot("doudian", {
    page_type: "orders",
    identity_status: "unresolved",
    identity_claims: [],
  }, {
    tab: { id: 31, url: "https://fxg.jinritemai.com/ffa/morder/order/list" },
  }),
  (error) => error?.code === "STORE_IDENTITY_UNRESOLVED",
  "an unresolved non-conflict snapshot must still fail before persistence",
);

const scanSource = background.slice(scanStart, scanEnd);
assert.doesNotMatch(
  scanSource,
  /if\s*\(response\.snapshot\.identity_status\s*===\s*"conflict"\)[\s\S]{0,500}throw identityError/,
  "the full-scan coordinator must not discard conflict evidence before /push",
);
assert.match(scanSource, /allowConflictForForensics:\s*true/);
assert.match(scanSource, /prepared\.reason !== "fresh_identity_claim"[\s\S]{0,120}prepared\.reason !== "identity_conflict"/);
assert.match(scanSource, /await storeAndPush\(page\.source, preparedSnapshot/);
assert.match(scanSource, /bridgeError\.forensic_saved = bridgeResult\?\.forensic_saved === true/);
assert.match(scanSource, /forensic_saved: lastError\?\.forensic_saved === true/);

async function runLiveConflictCase(response) {
  const calls = { pushes: [], collections: 0 };
  const sandbox = {
    globalThis: null,
    Date,
    DianAgentScanPolicy: {},
    doudianScanContexts: new Map(),
    navigateScanTab: async () => undefined,
    inspectPlatformPage: async () => undefined,
    sleep: async () => undefined,
    activatePageTab: async () => true,
    withTimeout: async (operation) => operation,
    collectFromTab: async () => {
      calls.collections += 1;
      return response;
    },
    isScanCancelled: () => false,
    isNonRetryableScanError: (error) => /IDENTITY_CONFLICT/.test(String(error?.code || "")),
    scanErrorCode: (error) => String(error?.code || "SCAN_FAILED"),
    requireDoudianSnapshotIdentity: () => ({ ok: true }),
    matchAccount: () => ({ ok: true }),
    chrome: { tabs: { async get() { return { url: "https://qianchuan.jinritemai.com/" }; } } },
    storeAndPush: async (source, snapshot, options) => {
      calls.pushes.push({ source, snapshot, options });
      return {
        ok: false,
        status: 409,
        quarantined: true,
        forensic_saved: true,
        error_code: "STORE_IDENTITY_CONFLICT",
        error: "snapshot quarantined",
      };
    },
  };
  sandbox.globalThis = sandbox;
  vm.runInNewContext(
    `${background.slice(conflictHelperStart, conflictHelperEnd)}\n` +
      `${background.slice(liveScanStart, liveScanEnd)}\n` +
      "globalThis.__scanOnePage = scanOnePage;",
    sandbox,
    { filename: "background-live-forensic-scan.vm.js" },
  );
  const result = await sandbox.__scanOnePage(
    41,
    {
      id: "qianchuan_campaigns",
      label: "千川计划",
      source: "qianchuan",
      url: "https://qianchuan.jinritemai.com/uni-prom/product",
      waitMs: 0,
      expectedPageType: "campaigns",
    },
    "test",
    { key: "account-a" },
    "store-a",
    "scan-run-a",
  );
  return { result, calls };
}

(async () => {
  for (const [label, pageType, quality] of [
    ["login required", "campaigns", { login_required: true }],
    ["page type mismatch", "overview", {}],
    ["pagination stalled", "campaigns", { pagination_stalled: true }],
  ]) {
    const live = await runLiveConflictCase({
      ok: true,
      page_type: pageType,
      quality,
      snapshot: {
        page_type: pageType,
        identity_status: "conflict",
        identity_conflicts: ["qianchuan_advertiser_id"],
        quality,
      },
    });
    assert.equal(live.calls.collections, 1, label);
    assert.equal(live.calls.pushes.length, 1, `${label} conflict evidence must reach storeAndPush first`);
    assert.equal(live.calls.pushes[0].options.expectedStoreKey, "store-a", label);
    assert.equal(live.calls.pushes[0].options.expectedAccountKey, "account-a", label);
    assert.equal(live.result.ok, false, label);
    assert.equal(live.result.error_code, "STORE_IDENTITY_CONFLICT", label);
    assert.equal(live.result.status, 409, label);
    assert.equal(live.result.quarantined, true, label);
    assert.equal(live.result.forensic_saved, true, label);
  }

  console.log("background live forensic conflict scan tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
