const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(require.resolve("./scan-policy.js"), "utf8");
const context = { globalThis: null };
context.globalThis = context;
vm.runInNewContext(source, context, { filename: "scan-policy.js" });
const policy = context.DianAgentScanPolicy;

function completeResult(id) {
  return { id, ok: true, collection_complete: true, quality: { score: 90 } };
}

const plannedPageIds = [
  "overview", "orders", "products", "inventory", "refunds", "reviews", "shelf",
  "live", "short_video", "image_text", "search", "recommend_card", "funds",
];
const root = {
  run_id: "scan_root_state_machine",
  root_run_id: "scan_root_state_machine",
  status: "partial",
  scope: "full",
  store_key: "store_a",
  account_key: "",
  planned_page_ids: plannedPageIds,
  results: [
    ...plannedPageIds.slice(0, -1).map(completeResult),
    { id: "funds", ok: false, collection_complete: false, error_code: "PAGE_LOAD_TIMEOUT" },
  ],
};

// State 1: the root receipt is partial and only the failed page is runnable.
assert.deepStrictEqual(Array.from(policy.resumePageIds(root, plannedPageIds)), ["funds"]);
const partialCheckpoint = policy.buildRecoveryCheckpoint(root, plannedPageIds);
assert.strictEqual(partialCheckpoint.can_resume, true);
assert.deepStrictEqual(Array.from(partialCheckpoint.resume_page_ids), ["funds"]);

// State 2: a child attempt replaces only its page; it cannot shrink the root
// contract or discard the twelve historical successes.
const reconciled = policy.reconcileRecoveryProgress(
  root,
  [completeResult("funds")],
  { storeKey: "store_a", accountKey: "" },
  plannedPageIds,
);
assert.strictEqual(reconciled.root_run_id, root.run_id);
assert.deepStrictEqual(Array.from(reconciled.planned_page_ids), plannedPageIds);
assert.strictEqual(reconciled.results.length, 13);
assert.strictEqual(reconciled.attempted_page_ids.length, 13);
assert.strictEqual(reconciled.success, 13);
assert.strictEqual(reconciled.failed, 0);
assert.deepStrictEqual(Array.from(reconciled.pending_page_ids), []);
assert.strictEqual(reconciled.coverage_complete, true);

// State 3: the final root receipt is genuinely complete and has no further
// resume action. This is the state consumed by the analysis-ready receipt.
const completed = {
  ...root,
  status: "completed",
  results: reconciled.results,
  attempted_page_ids: reconciled.attempted_page_ids,
  pending_page_ids: reconciled.pending_page_ids,
  coverage_complete: reconciled.coverage_complete,
};
const completedCheckpoint = policy.buildRecoveryCheckpoint(completed, plannedPageIds);
assert.strictEqual(completedCheckpoint.state, "complete");
assert.strictEqual(completedCheckpoint.can_resume, false);
assert.deepStrictEqual(Array.from(completedCheckpoint.resume_page_ids), []);

// A result from another store is never allowed to complete this root run.
const crossStore = policy.reconcileRecoveryProgress(
  root,
  [completeResult("funds")],
  { storeKey: "store_b", accountKey: "" },
  plannedPageIds,
);
assert.strictEqual(crossStore.coverage_complete, false);
assert.deepStrictEqual(Array.from(crossStore.pending_page_ids), plannedPageIds.slice(0, -1));

console.log("scan recovery state-machine tests passed");
