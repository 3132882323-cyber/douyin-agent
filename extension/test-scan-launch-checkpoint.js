const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const background = fs.readFileSync(require.resolve("./background.js"), "utf8");
const start = background.indexOf("function startFullScan");
const end = background.indexOf("\nasync function recoverInterruptedScanUnlocked", start);
assert.ok(start >= 0 && end > start, "scan launch helpers should be extractable");

const checkpoint = background.indexOf("const initialCheckpointAccepted = await setFullScanState", background.indexOf("async function runFullScan"));
const rejected = background.indexOf("if (!initialCheckpointAccepted)", checkpoint);
const alarm = background.indexOf("chrome.alarms.create(SCAN_RECOVERY_ALARM", checkpoint);
const catalog = background.indexOf("`${BRIDGE_URL}/stores`", checkpoint);
assert.ok(checkpoint >= 0 && rejected > checkpoint && alarm > rejected && catalog > alarm,
  "an explicitly rejected initial checkpoint must stop before alarm and catalog work");
assert.equal((background.match(/await awaitFullScanLaunch\(operation\)/g) || []).length, 2,
  "manual and retry scan responses must both wait for initial checkpoint acceptance");

function loadRuntime(runFullScan) {
  const context = {
    Promise,
    fullScanPromise: null,
    activeFullScanRunId: "",
    activeFullScanTabId: null,
    SCAN_RECOVERY_ALARM: "scan-recovery",
    createScanRunId: () => "scan_test_1",
    assertVersionReloadAllowsNewWork: () => undefined,
    runFullScan,
    chrome: { alarms: { clear: async () => true } },
  };
  vm.runInNewContext(
    `${background.slice(start, end)}\n` +
      "globalThis.__scanLaunch = { startFullScan, awaitFullScanLaunch };",
    context,
    { filename: "scan-launch-checkpoint.vm.js" },
  );
  return context.__scanLaunch;
}

(async () => {
  {
    let signal;
    let finish;
    const completion = new Promise((resolve) => { finish = resolve; });
    const runtime = loadRuntime((...args) => {
      signal = args.at(-1);
      return completion;
    });
    const operation = runtime.startFullScan();
    let responseSettled = false;
    const responsePromise = runtime.awaitFullScanLaunch(operation).then((value) => {
      responseSettled = true;
      return value;
    });
    await Promise.resolve();
    assert.equal(responseSettled, false, "UI response must wait while the initial checkpoint is unresolved");
    signal(false);
    finish({ started: false, code: "SCAN_NOT_STARTED", run_id: operation.run_id, error: "rejected" });
    const response = await responsePromise;
    assert.deepEqual(JSON.parse(JSON.stringify(response)), {
      ok: false, started: false, code: "SCAN_NOT_STARTED", run_id: "scan_test_1", error: "rejected",
    });
  }

  {
    let signal;
    const runtime = loadRuntime((...args) => {
      signal = args.at(-1);
      return new Promise(() => undefined);
    });
    const operation = runtime.startFullScan();
    signal(true);
    const response = await runtime.awaitFullScanLaunch(operation);
    assert.deepEqual(JSON.parse(JSON.stringify(response)), {
      ok: true, started: true, run_id: "scan_test_1",
    });
  }

  {
    const launchError = new Error("storage unavailable");
    launchError.code = "SCAN_BOOTSTRAP_FAILED";
    const runtime = loadRuntime(() => Promise.reject(launchError));
    const operation = runtime.startFullScan();
    const response = await runtime.awaitFullScanLaunch(operation);
    assert.equal(response.ok, false);
    assert.equal(response.started, false);
    assert.equal(response.code, "SCAN_BOOTSTRAP_FAILED");
  }

  console.log("scan launch checkpoint tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
