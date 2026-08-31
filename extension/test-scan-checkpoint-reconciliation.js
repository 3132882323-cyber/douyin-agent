const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const background = fs.readFileSync(require.resolve("./background.js"), "utf8");
const blockStart = background.indexOf("const AUTHORITATIVE_SCAN_STATES");
const blockEnd = background.indexOf("\nasync function selectAnalysisAccount", blockStart);
assert.ok(blockStart >= 0 && blockEnd > blockStart, "scan checkpoint reconciliation block should be extractable");

function loadCheckpointRuntime(fetchWithTimeout) {
  const state = {
    fullScan: { status: "running", run_id: "scan_local_1", revision: 1, current: "old" },
  };
  const statusUpdates = [];
  const context = {
    console,
    BRIDGE_URL: "http://127.0.0.1:8765",
    fetchWithTimeout,
    isScanCancelled: () => false,
    acceptScanStatusTransition: () => true,
    updateStatus: async (...args) => { statusUpdates.push(args); },
    mutateLocalStorage: async (_keys, mutator) => {
      const updates = mutator({ fullScan: state.fullScan }) || {};
      if (updates.fullScan) state.fullScan = updates.fullScan;
      return updates;
    },
  };
  vm.runInNewContext(
    `${background.slice(blockStart, blockEnd)}\n` +
      "globalThis.__checkpoint = { setFullScanState, authoritativeScanCheckpoint, reconcileLocalScanCheckpoint };",
    context,
    { filename: "scan-checkpoint-reconciliation.vm.js" },
  );
  return { ...context.__checkpoint, state, statusUpdates };
}

(async () => {
  {
    let call = 0;
    const runtime = loadCheckpointRuntime(async (_url, options = {}) => {
      call += 1;
      if (call === 1) {
        assert.equal(options.method, "POST");
        return { ok: false, status: 400, json: async () => ({ error: "STALE_SCAN_REVISION" }) };
      }
      assert.equal(options.method, undefined, "an explicit rejection must trigger an authority GET");
      return {
        ok: true,
        status: 200,
        json: async () => ({ status: "completed", run_id: "scan_server_9", revision: 9, results: [] }),
      };
    });
    const accepted = await runtime.setFullScanState({ current: "next" }, "scan_local_1");
    assert.equal(accepted, false, "an explicitly rejected checkpoint is not reported as synchronized");
    assert.equal(runtime.state.fullScan.run_id, "scan_server_9");
    assert.equal(runtime.state.fullScan.revision, 9);
    assert.equal(runtime.state.fullScan.reconciliation_required, false);
    assert.equal(runtime.state.fullScan.reconciled_from, "agent");
  }

  {
    const runtime = loadCheckpointRuntime(async () => { throw new Error("response lost"); });
    const accepted = await runtime.setFullScanState({ current: "next" }, "scan_local_1");
    assert.equal(accepted, true, "a network ambiguity preserves the accepted local mutation");
    assert.equal(runtime.state.fullScan.run_id, "scan_local_1");
    assert.equal(runtime.state.fullScan.revision, 2);
    assert.equal(runtime.state.fullScan.current, "next");
    assert.equal(runtime.state.fullScan.reconciliation_required, true);
    assert.equal(runtime.state.fullScan.reconciliation_reason, "network_result_ambiguous");
  }

  {
    const runtime = loadCheckpointRuntime(async (_url, options = {}) => {
      const posted = JSON.parse(options.body);
      return { ok: true, status: 200, json: async () => ({ ok: true, scan: posted }) };
    });
    const accepted = await runtime.setFullScanState({ current: "next" }, "scan_local_1");
    assert.equal(accepted, true);
    assert.equal(runtime.state.fullScan.reconciliation_required, false);
    assert.equal(runtime.state.fullScan.reconciled_from, "agent");
    assert.equal(runtime.state.fullScan.revision, 2);
  }

  console.log("scan checkpoint reconciliation tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
