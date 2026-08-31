const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const background = fs.readFileSync(require.resolve("./background.js"), "utf8");
const doudian = fs.readFileSync(require.resolve("./content-doudian.js"), "utf8");
const qianchuan = fs.readFileSync(require.resolve("./content-qianchuan.js"), "utf8");
const scanPolicy = fs.readFileSync(require.resolve("./scan-policy.js"), "utf8");

// A scan collects a candidate first; only the coordinator may validate and
// persist it. Passive page observers must not create a second snapshot.
assert.match(background, /deferPush:\s*true/);
assert.match(background, /expected_scope:/);
assert.match(background, /applyDoudianScanContext\(response\.snapshot/);
assert.doesNotMatch(doudian, /capture\("page-load"\)/);
assert.doesNotMatch(qianchuan, /capture\("page-load"\)/);

// Every lifecycle update is bound to one durable run and checkpoint, with
// failure + not-yet-attempted pages retained for restart recovery.
assert.match(background, /createScanRunId\(\)/);
assert.match(background, /planned_page_ids:/);
assert.match(background, /attempted_page_ids:/);
assert.match(background, /pending_page_ids:/);
assert.match(background, /recoverInterruptedScan\("dashboard-open"\)/);
assert.match(background, /SCAN_RECOVERY_ALARM/);
assert.match(background, /SCAN_PAUSED_FOR_USER/);
assert.match(background, /buildRecoveryCheckpoint/);
assert.match(background, /collection_complete/);
assert.match(background, /root_run_id:[ \t]*rootRunId/);
assert.match(background, /reconcileRecoveryProgress/);
assert.match(background, /execution_page_ids:/);
assert.match(background, /const scopedFinalResults = plannedPageIds\.map/);
assert.match(background, /result\.collection_complete === false/);
assert.match(background, /inspectPlanIdentityCoverage/);

const sidepanel = fs.readFileSync(require.resolve("./sidepanel.js"), "utf8");
assert.match(sidepanel, /const persistedPendingPageIds = new Set/);
assert.match(sidepanel, /persistedPendingPageIds\.has\(pageId\)/);
assert.match(sidepanel, /if \(scanPollInFlight\) return/);

// A selected store is not identity evidence. Every newly owned Doudian tab
// first proves itself on the overview page and receives one fixed, run-scoped
// lease. Every later snapshot still needs a fresh current-page identity claim;
// the lease may validate lifecycle scope but must never manufacture store scope.
assert.match(background, /establishDoudianScanContext/);
assert.match(background, /requiresIdentityProof:[ \t]*true/);
assert.match(background, /identity_bootstrap_\$\{tabId\}/);
assert.ok((background.match(/requireDoudianSnapshotIdentity\(/g) || []).length >= 3);
assert.match(background, /prepared\.reason !== "fresh_identity_claim"/);
assert.doesNotMatch(scanPolicy, /resolved_by_scan_session/);
assert.doesNotMatch(scanPolicy, /identity_source:\s*"scan_session"/);
assert.doesNotMatch(background, /bindDoudianScanContext/);

const admissionContext = { globalThis: null };
admissionContext.globalThis = admissionContext;
vm.runInNewContext(scanPolicy, admissionContext, { filename: "scan-policy.vm.js" });
const admissionStart = background.indexOf("function clearDoudianScanContext");
const admissionEnd = background.indexOf("\nfunction mutateLocalStorage", admissionStart);
assert.ok(admissionStart >= 0 && admissionEnd > admissionStart, "Doudian snapshot admission helpers should be extractable");
vm.runInNewContext(
  `const doudianScanContexts = new Map();\n${background.slice(admissionStart, admissionEnd)}\n` +
    "globalThis.__prepareScanSessionSnapshot = prepareScanSessionSnapshot;",
  admissionContext,
  { filename: "doudian-snapshot-admission.vm.js" },
);
assert.throws(
  () => admissionContext.__prepareScanSessionSnapshot("doudian", {
    page_type: "orders", identity_status: "unresolved", identity_claims: [],
  }, { tab: { id: 99, url: "https://fxg.jinritemai.com/ffa/morder/order/list" } }),
  (error) => error?.code === "STORE_IDENTITY_UNRESOLVED",
  "ordinary Doudian page-data without a current identity must fail before persistence",
);
const currentIdentitySnapshot = {
  page_type: "orders",
  identity_status: "resolved_by_bridge",
  identity_claims: [{
    kind: "douyin_shop_id", raw_id: "778899", confidence: "high", evidence_source: "data_attribute",
  }],
};
assert.strictEqual(
  admissionContext.__prepareScanSessionSnapshot("doudian", currentIdentitySnapshot, {
    tab: { id: 99, url: "https://fxg.jinritemai.com/ffa/morder/order/list" },
  }),
  currentIdentitySnapshot,
  "ordinary Doudian page-data with a fresh current claim remains accepted",
);

// Browser startup and alarms may preserve a checkpoint, but must never open
// platform pages or start a scan without a fresh user action.
assert.doesNotMatch(background, /alarm\.name === FULL_SCAN_ALARM\)\s*startFullScan/);
assert.doesNotMatch(background, /alarm\.name === ALARM_NAME[^\n]*syncAll/);
assert.match(background, /next\.autoSync = false/);
assert.match(background, /next\.autoFullScan = false/);

// Cancellation is run-scoped and closes the owned tab; the old global boolean
// previously allowed a cancelled old task to interfere with a new task.
assert.match(background, /cancelledFullScanRuns/);
assert.match(background, /chrome\.tabs\.remove\(activeFullScanTabId\)/);
assert.match(background, /acceptScanStatusTransition\(current\.status, patch\.status/);
assert.match(background, /canCancelScan\(\{ \.\.\.scan, run_id: runId \}\)/);
assert.doesNotMatch(background, /fullScanCancelled/);

// Retry inherits the original business scope and reports a busy task honestly.
assert.match(background, /String\(scan\.account_key \|\| ""\)/);
assert.match(background, /String\(scan\.store_key \|\| ""\)/);
assert.match(background, /started:\s*false,\s*code:\s*"SCAN_BUSY"/);
assert.match(sidepanel, /response\?\.ok !== true \|\| response\.started !== true/);

const receiptStart = sidepanel.indexOf("function scanReceiptFromStatus");
const receiptEnd = sidepanel.indexOf("\nfunction scanRecoveryAction", receiptStart);
assert.ok(receiptStart >= 0 && receiptEnd > receiptStart, "scan receipt implementation should be extractable");
const receiptContext = { globalThis: null };
receiptContext.globalThis = receiptContext;
vm.runInNewContext(fs.readFileSync(require.resolve("./scan-scope-policy.js"), "utf8"), receiptContext);
vm.runInNewContext(
  `${sidepanel.slice(receiptStart, receiptEnd)}\n` +
    "globalThis.__scanReceiptFromStatus = scanReceiptFromStatus;",
  receiptContext,
  { filename: "scan-receipt.vm.js" },
);
const incompleteTargetedReceipt = receiptContext.__scanReceiptFromStatus({
  status: "partial",
  scope: "quick",
  planned_page_ids: ["qianchuan_campaigns"],
  targeted_page_ids: ["qianchuan_campaigns"],
  attempted_page_ids: ["qianchuan_campaigns"],
  pending_page_ids: ["qianchuan_campaigns"],
  total: 1,
  success: 0,
  failed: 0,
  results: [{
    id: "qianchuan_campaigns",
    source: "qianchuan",
    ok: true,
    collection_complete: false,
    warning_code: "PLAN_IDENTIFIERS_UNRESOLVED",
    warning: "计划身份仅识别 0/1 行",
    quality: { score: 92, metric_count: 5, row_count: 1 },
  }],
});
assert.equal(incompleteTargetedReceipt.summary.total, 1);
assert.equal(incompleteTargetedReceipt.summary.success, 0, "an incomplete targeted page must not be counted as passed");
assert.equal(incompleteTargetedReceipt.summary.failed, 0);
assert.equal(incompleteTargetedReceipt.summary.needs_review, 1);
assert.equal(incompleteTargetedReceipt.results[0].needs_review, true);
assert.equal(incompleteTargetedReceipt.readiness, "attention");
assert.match(incompleteTargetedReceipt.warnings.join(" "), /PLAN_IDENTIFIERS_UNRESOLVED/);
assert.match(incompleteTargetedReceipt.warnings.join(" "), /计划身份仅识别 0\/1 行/);

console.log("scan lifecycle contract tests passed");
