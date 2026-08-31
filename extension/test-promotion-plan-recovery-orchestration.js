const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const policy = require("./promotion-plan-center.js");
const sidepanel = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");
const blockStart = sidepanel.indexOf("function promotionPlanRecoveryError");
const blockEnd = sidepanel.indexOf("\nasync function runPromotionPlanCollectionRecovery", blockStart);
assert.ok(blockStart >= 0 && blockEnd > blockStart, "plan recovery orchestration block should exist");

function loadRecovery(selection, overrides = {}) {
  const calls = { messages: [], bridge: [], render: [], prepareStore: 0 };
  const context = {
    console,
    Date,
    setTimeout,
    clearTimeout,
    SCAN_TIMEOUT_MS: 2000,
    selectedStoreKey: overrides.selectedStoreKey === undefined ? "store-a" : overrides.selectedStoreKey,
    currentPromotionPlanRecovery: { selection },
    DianPromotionPlanCenter: policy,
    chrome: {
      runtime: {
        sendMessage: async (message) => {
          calls.messages.push(message);
          if (overrides.sendMessage) return overrides.sendMessage(message);
          if (message.type === "start-full-scan") return { ok: true, started: true, run_id: "run-1" };
          return {
            ok: true,
            dashboard: {
              fullScan: {
                run_id: "run-1",
                status: "completed",
                results: [{ id: selection.scope.plan_type === "live" ? "qianchuan_live" : "qianchuan_campaigns", ok: true }],
              },
            },
          };
        },
      },
    },
    bridgeFetch: async (endpoint) => {
      calls.bridge.push(endpoint);
      return { rows: [], collection_receipts: [] };
    },
    renderPromotionPlanConsole: (payload) => { calls.render.push(payload); },
  };
  context.ensureCurrentStoreForScan = async () => {
    calls.prepareStore += 1;
    if (overrides.ensureCurrentStoreForScan) {
      return overrides.ensureCurrentStoreForScan(context);
    }
    context.selectedStoreKey = String(overrides.preparedStoreKey || "");
    return context.selectedStoreKey;
  };
  vm.runInNewContext(
    `${sidepanel.slice(blockStart, blockEnd)}\n` +
      "globalThis.__recovery = { runPromotionPlanScopeRecovery, waitForPromotionPlanRecoveryScan };",
    context,
    { filename: "promotion-plan-recovery.vm.js" },
  );
  return { ...context.__recovery, calls };
}

(async () => {
  const liveSelection = {
    exact_scope: true,
    scope: { account: "acct-live", promotion_mode: "chengfang", plan_type: "live" },
  };
  const live = loadRecovery(liveSelection);
  const result = await live.runPromotionPlanScopeRecovery(policy.buildScopeRecoveryContract(liveSelection));
  assert.equal(result.contract.purpose, "promotion_plan_scope_recovery_live");
  assert.deepEqual(JSON.parse(JSON.stringify(live.calls.messages[0])), {
    type: "start-full-scan",
    scan_scope: "quick",
    store_key: "store-a",
    account_key: "acct-live",
    page_ids: ["qianchuan_live"],
    recovery_purpose: "promotion_plan_scope_recovery_live",
    expected_page_types: ["qianchuan_live"],
  });
  assert.equal(Object.prototype.hasOwnProperty.call(live.calls.messages[0], "plan_id"), false);
  assert.equal(live.calls.messages.some((message) => message.type === "run-authorized-execution"), false);
  assert.deepEqual(live.calls.bridge, ["/qianchuan/plan-console"]);
  assert.equal(live.calls.render.length, 1, "the plan console must be revalidated after collection");

  const productSelection = {
    exact_scope: true,
    scope: { account: "acct-product", promotion_mode: "full_domain", plan_type: "product" },
  };
  const product = loadRecovery(productSelection);
  await product.runPromotionPlanScopeRecovery(policy.buildScopeRecoveryContract(productSelection));
  assert.deepEqual(Array.from(product.calls.messages[0].page_ids), ["qianchuan_campaigns"]);
  assert.deepEqual(Array.from(product.calls.messages[0].expected_page_types), ["campaigns", "qianchuan_campaigns"]);

  const broadSelection = {
    exact_scope: false,
    scope: { account: "all", promotion_mode: "unknown", plan_type: "all" },
  };
  const broad = loadRecovery(broadSelection);
  await assert.rejects(
    broad.runPromotionPlanScopeRecovery(policy.buildScopeRecoveryContract(broadSelection)),
    (error) => error.code === "RECOVERY_SCOPE_REQUIRED",
  );
  assert.equal(broad.calls.messages.length, 0, "a broad scope must not open or collect any platform page");

  const preparedStore = loadRecovery(liveSelection, {
    selectedStoreKey: "",
    preparedStoreKey: "store-current-page",
  });
  await preparedStore.runPromotionPlanScopeRecovery(policy.buildScopeRecoveryContract(liveSelection));
  assert.equal(preparedStore.calls.prepareStore, 1, "a missing store scope should prepare the current Doudian page automatically");
  assert.equal(preparedStore.calls.messages[0].store_key, "store-current-page");

  const missingStore = loadRecovery(liveSelection, { selectedStoreKey: "", preparedStoreKey: "" });
  await assert.rejects(
    missingStore.runPromotionPlanScopeRecovery(policy.buildScopeRecoveryContract(liveSelection)),
    (error) => error.code === "STORE_SELECTION_UNCONFIRMED",
  );
  assert.equal(missingStore.calls.prepareStore, 1, "recovery should try the exact current page instead of opening a local store picker");
  assert.equal(missingStore.calls.messages.length, 0, "collection must remain fail-closed when current-page preparation cannot establish a store scope");

  console.log("promotion plan recovery orchestration tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
