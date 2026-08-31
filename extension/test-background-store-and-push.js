"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const background = fs.readFileSync(path.join(__dirname, "background.js"), "utf8");
const scanPolicy = fs.readFileSync(path.join(__dirname, "scan-policy.js"), "utf8");
const blockStart = background.indexOf("async function storeAndPush");
const blockEnd = background.indexOf("\nasync function updateStatus", blockStart);
const conflictHelperStart = background.indexOf("function snapshotHasIdentityConflict");
const conflictHelperEnd = background.indexOf("\nfunction requireDoudianSnapshotIdentity", conflictHelperStart);
assert.ok(blockStart >= 0 && blockEnd > blockStart, "storeAndPush must remain independently testable");
assert.ok(conflictHelperStart >= 0 && conflictHelperEnd > conflictHelperStart, "identity conflict policy must remain independently testable");
const conflictHelperSource = background.slice(conflictHelperStart, conflictHelperEnd);
const storeAndPushSource = background.slice(blockStart, blockEnd);

function loadStoreAndPush(responseBody, { responseOk = true, responseStatus = 200 } = {}) {
  const calls = { catalogMutations: [], statuses: [] };
  let catalog = {};
  const context = {
    console,
    Date,
    BRIDGE_URL: "http://127.0.0.1:8765",
    SOURCE_PATTERNS: {
      qianchuan: ["*://qianchuan.jinritemai.com/*"],
      doudian: ["*://fxg.jinritemai.com/*"],
    },
    structuredClone: (value) => JSON.parse(JSON.stringify(value)),
    scanErrorCode: () => "BRIDGE_PUSH_FAILED",
    fetchWithTimeout: async () => ({
      ok: responseOk,
      status: responseStatus,
      async json() { return responseBody; },
    }),
    mutateLocalStorage: async (keys, mutator) => {
      calls.catalogMutations.push(Array.from(keys));
      const updates = await mutator({ catalog });
      if (updates.catalog) catalog = updates.catalog;
      return updates;
    },
    updateStatus: async (...args) => { calls.statuses.push(args); },
  };
  context.globalThis = context;
  vm.runInNewContext(scanPolicy, context, { filename: "scan-policy.vm.js" });
  vm.runInNewContext(
    `${conflictHelperSource}\n${storeAndPushSource}\nglobalThis.__storeAndPush = storeAndPush;`,
    context,
    { filename: "background-store-and-push.vm.js" },
  );
  return { storeAndPush: context.__storeAndPush, calls, catalog: () => catalog };
}

const snapshot = {
  source: "qianchuan",
  page_type: "campaigns",
  captured_at: 1_900_000_000_000,
  title: "Campaigns",
};

(async () => {
  for (const [label, responseBody, expectedCode] of [
    [
      "quarantined identity conflict",
      {
        ok: true,
        quarantined: true,
        accepted_for_current_data: false,
        error_code: "STORE_IDENTITY_CONFLICT",
        error: "snapshot quarantined",
      },
      "STORE_IDENTITY_CONFLICT",
    ],
    ["missing acceptance", { ok: true, accepted: true }, "SNAPSHOT_ACCEPTANCE_UNCONFIRMED"],
    [
      "missing accepted flag",
      { ok: true, quarantined: false, accepted_for_current_data: true },
      "SNAPSHOT_ACCEPTANCE_UNCONFIRMED",
    ],
    [
      "non-boolean quarantine claim",
      { ok: true, accepted: true, quarantined: "true", accepted_for_current_data: true },
      "SNAPSHOT_ACCEPTANCE_UNCONFIRMED",
    ],
    [
      "explicit rejection",
      { ok: true, accepted: true, quarantined: false, accepted_for_current_data: false },
      "SNAPSHOT_ACCEPTANCE_UNCONFIRMED",
    ],
    [
      "targeted response without an execution request",
      {
        ok: true,
        accepted: true,
        quarantined: false,
        accepted_for_current_data: false,
        targeted_execution_snapshot: true,
      },
      "SNAPSHOT_ACCEPTANCE_UNCONFIRMED",
    ],
  ]) {
    const harness = loadStoreAndPush(responseBody);
    const result = await harness.storeAndPush("qianchuan", snapshot);
    assert.equal(result.ok, false, label);
    assert.equal(result.error_code, expectedCode, label);
    assert.equal(harness.calls.catalogMutations.length, 0, `${label} must not update catalog`);
    assert.deepEqual(harness.catalog(), {}, `${label} must leave catalog unchanged`);
  }

  {
    const harness = loadStoreAndPush({
      ok: false,
      accepted: false,
      accepted_for_current_data: false,
      quarantined: true,
      forensic_saved: true,
      error_code: "STORE_IDENTITY_CONFLICT",
      error: "snapshot quarantined",
    }, { responseOk: false, responseStatus: 409 });
    const result = await harness.storeAndPush("qianchuan", snapshot);
    assert.equal(result.ok, false, "the canonical HTTP 409 quarantine response must fail closed");
    assert.equal(result.error_code, "STORE_IDENTITY_CONFLICT");
    assert.equal(result.status, 409);
    assert.equal(result.forensic_saved, true, "the 409 forensic receipt must survive client normalization");
    assert.equal(harness.calls.catalogMutations.length, 0);
  }

  {
    const harness = loadStoreAndPush({
      ok: true,
      accepted: true,
      quarantined: false,
      accepted_for_current_data: true,
    });
    const result = await harness.storeAndPush("qianchuan", {
      ...snapshot,
      identity_status: "conflict",
      identity_conflicts: ["qianchuan_advertiser_id"],
    });
    assert.equal(result.ok, false, "a conflict candidate must never become current data even on a malformed 200 acceptance");
    assert.equal(result.error_code, "STORE_IDENTITY_CONFLICT");
    assert.equal(harness.calls.catalogMutations.length, 0, "a conflict candidate must never update catalog");
    assert.deepEqual(harness.catalog(), {});
  }

  {
    const harness = loadStoreAndPush({
      ok: true,
      accepted: true,
      quarantined: false,
      accepted_for_current_data: true,
    });
    const result = await harness.storeAndPush("doudian", {
      page_type: "orders",
      identity_status: "resolved_by_bridge",
      identity_claims: [
        { kind: "douyin_shop_id", raw_id: "shop-a", confidence: "high", evidence_source: "data_attribute" },
        { kind: "douyin_shop_id", raw_id: "shop-b", confidence: "high", evidence_source: "data_attribute" },
      ],
    });
    assert.equal(result.ok, false, "a policy-derived conflict must override malformed current-data acceptance");
    assert.equal(result.error_code, "STORE_IDENTITY_CONFLICT");
    assert.equal(harness.calls.catalogMutations.length, 0, "a policy-derived conflict must remain forensic-only");
    assert.deepEqual(harness.catalog(), {});
  }

  {
    const harness = loadStoreAndPush({
      ok: true,
      accepted: true,
      quarantined: false,
      accepted_for_current_data: true,
      account: { key: "account-a" },
    });
    const result = await harness.storeAndPush("qianchuan", snapshot);
    assert.equal(result.ok, true, "an explicitly accepted normal response remains compatible");
    assert.equal(harness.calls.catalogMutations.length, 1);
    assert.equal(harness.catalog().qianchuan.campaigns.title, "Campaigns");
  }

  {
    const harness = loadStoreAndPush({
      ok: true,
      accepted: true,
      quarantined: false,
      accepted_for_current_data: false,
      targeted_execution_snapshot: true,
    });
    const result = await harness.storeAndPush("qianchuan", snapshot, {
      executionContext: {
        purpose: "execution_readback",
        action_id: "a".repeat(24),
        authorization_id: "b".repeat(32),
        readback_token: "c".repeat(64),
      },
    });
    assert.equal(result.ok, true, "an explicitly accepted targeted execution snapshot remains usable");
    assert.equal(harness.calls.catalogMutations.length, 0, "targeted execution snapshots must never update catalog");
  }

  {
    const harness = loadStoreAndPush({
      ok: true,
      accepted: true,
      quarantined: false,
      accepted_for_current_data: true,
      // A targeted response must explicitly carry its targeted marker.  A
      // response that omits it cannot fall through the ordinary scan branch.
    });
    const result = await harness.storeAndPush("qianchuan", snapshot, {
      executionContext: {
        purpose: "execution_readback",
        action_id: "d".repeat(24),
        authorization_id: "e".repeat(32),
        readback_token: "f".repeat(64),
      },
    });
    assert.equal(result.ok, false, "a targeted request must require the targeted response contract");
    assert.equal(result.error_code, "TARGETED_EXECUTION_ACCEPTANCE_UNCONFIRMED");
    assert.equal(harness.calls.catalogMutations.length, 0, "targeted requests must never update the ordinary catalog");
    assert.deepEqual(harness.catalog(), {});
  }

  {
    const harness = loadStoreAndPush({
      ok: true,
      accepted: true,
      quarantined: false,
      accepted_for_current_data: true,
    });
    const result = await harness.storeAndPush("qianchuan", snapshot, {
      executionContext: {
        purpose: "preconsume_baseline",
        action_id: "malformed",
        authorization_id: "e".repeat(32),
        readback_token: "f".repeat(64),
      },
    });
    assert.equal(result.ok, false, "a malformed targeted context must not be reinterpreted as an ordinary scan");
    assert.equal(result.error_code, "TARGETED_EXECUTION_ACCEPTANCE_UNCONFIRMED");
    assert.equal(harness.calls.catalogMutations.length, 0);
  }

  {
    const harness = loadStoreAndPush({
      ok: false,
      accepted: true,
      quarantined: false,
      accepted_for_current_data: true,
      error_code: "BRIDGE_REJECTED",
    });
    const result = await harness.storeAndPush("qianchuan", snapshot);
    assert.equal(result.ok, false, "an explicit bridge failure must not be overwritten by acceptance");
    assert.equal(harness.calls.catalogMutations.length, 0);
  }

  console.log("background storeAndPush acceptance tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
