"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const background = fs.readFileSync(path.join(__dirname, "background.js"), "utf8");
const policyStart = background.indexOf("function parsedExtensionVersion");
const policyEnd = background.indexOf("\nconst FULL_SCAN_PAGES", policyStart);
const barrierStart = background.indexOf("function assertVersionReloadAllowsPrivilegedExecution");
const barrierEnd = background.indexOf("\nconst authorizedExecutionPromises", barrierStart);
const listenerStart = background.indexOf("chrome.runtime.onMessage.addListener");
assert.ok(policyStart >= 0 && policyEnd > policyStart, "reload hydration policy must remain testable");
assert.ok(barrierStart >= 0 && barrierEnd > barrierStart, "reload admission barrier must remain testable");
assert.ok(listenerStart >= 0, "runtime message listener must remain testable");

const policySource = background.slice(policyStart, policyEnd);
const barrierSource = background.slice(barrierStart, barrierEnd);
const listenerSource = background.slice(listenerStart);

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function memoryArea(values = {}, beforeGet = null) {
  return {
    values,
    async get(key) {
      if (beforeGet) await beforeGet;
      return { [key]: values[key] };
    },
    async set(patch) { Object.assign(values, patch); },
    async remove(key) { delete values[key]; },
  };
}

function createHarness({ localValues = {}, sessionValues = {}, hydrationGate = null, identityGate = null } = {}) {
  let listener = null;
  let identityResolutionCalls = 0;
  const sandbox = {
    console,
    Date,
    VERSION_RELOAD_GUARD_KEY: "dianAgentVersionReloadV1",
    VERSION_RELOAD_SESSION_KEY: "dianAgentVersionReloadSessionV1",
    VERSION_RELOAD_VERIFY_ALARM: "dian-agent-extension-reload-verify",
    VERSION_RECONCILE_ALARM: "dian-agent-extension-version-reconcile",
    VERSION_RELOAD_MAX_ATTEMPTS: 2,
    VERSION_RELOAD_COOLDOWN_MS: 45 * 1000,
    VERSION_RECONCILE_PERIOD_MINUTES: 5,
    versionReconcileInFlight: null,
    versionReloadTransitionPending: true,
    versionReloadStateHydrationPromise: null,
    activeReloadTrackedOperations: 0,
    scanRecoveryPromise: null,
    fullScanPromise: null,
    authorizedExecutionPromises: new Map(),
    executionJournalAdvancePromises: new Map(),
    executionReadbackRunPromises: new Map(),
    executionReadbackRecoveryPromise: null,
    storageMutationQueue: Promise.resolve(),
    TRUSTED_PLATFORM_MESSAGE_TYPES: new Set(["resolve-execution-identity"]),
    PAGE_SYNC_RECEIPTS_KEY: "pageSyncReceiptsV1",
    DEFAULT_SETTINGS: {},
    resolveExecutionIdentity: async () => {
      identityResolutionCalls += 1;
      if (identityGate) await identityGate;
      return { ok: true, resolved: true };
    },
    chrome: {
      runtime: {
        id: "extension-id",
        getManifest: () => ({ version: "4.14.8" }),
        onMessage: { addListener(callback) { listener = callback; } },
        reload() {},
        getURL: (value) => value,
      },
      storage: {
        local: memoryArea(localValues, hydrationGate),
        session: memoryArea(sessionValues, hydrationGate),
      },
      alarms: {
        async get() { return null; },
        create() {},
        async clear() { return true; },
      },
    },
    DianExecutionSenderPolicy: {
      isTrustedPageDataSender: () => true,
      isTrustedExtensionPageSender: () => true,
      isTrustedExecutionSender: () => true,
    },
  };
  sandbox.globalThis = sandbox;
  vm.runInNewContext(
    `${policySource}\n${barrierSource}\n` +
      "globalThis.__activeReloadWorkReasons = activeExtensionReloadWorkReasons;\n" +
      listenerSource,
    sandbox,
    { filename: "background-runtime-reload-gate.vm.js" },
  );
  assert.equal(typeof listener, "function");
  return {
    sandbox,
    identityResolutionCalls: () => identityResolutionCalls,
    dispatch(message) {
      let responded = false;
      let responseValue;
      const response = new Promise((resolve) => {
        const keepAlive = listener(
          message,
          { id: "extension-id", tab: { id: 17, url: "https://qianchuan.jinritemai.com/" } },
          (value) => {
            responded = true;
            responseValue = value;
            resolve(value);
          },
        );
        assert.equal(keepAlive, true);
      });
      return { response, responded: () => responded, responseValue: () => responseValue };
    },
  };
}

(async () => {
  {
    const gate = deferred();
    const harness = createHarness({
      hydrationGate: gate.promise,
      localValues: {
        dianAgentVersionReloadV1: {
          pair: "4.14.9|4.14.8",
          attempts: 1,
          last_requested_at: Date.now(),
        },
      },
    });
    const pending = harness.dispatch({ type: "resolve-execution-identity", source: "qianchuan" });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(pending.responded(), false, "identity resolution must await durable reload-state hydration");
    assert.equal(harness.identityResolutionCalls(), 0, "identity resolution must not start before hydration");
    gate.resolve();
    const blocked = await pending.response;
    assert.equal(blocked.ok, false);
    assert.equal(blocked.code, "EXTENSION_RELOAD_PENDING");
    assert.equal(harness.identityResolutionCalls(), 0, "durable reload pending must block identity resolution");
    assert.equal(harness.sandbox.activeReloadTrackedOperations, 0);

    const ordinary = await harness.dispatch({ type: "runtime-context-ping" }).response;
    assert.equal(ordinary.ok, true, "non-execution runtime messages remain available while reload is pending");
    assert.equal(ordinary.runtime_version, "4.14.8");
  }

  {
    const harness = createHarness();
    const allowed = await harness.dispatch({ type: "resolve-execution-identity", source: "qianchuan" }).response;
    assert.equal(allowed.ok, true);
    assert.equal(allowed.resolved, true);
    assert.equal(harness.identityResolutionCalls(), 1, "normal identity resolution must remain compatible");
    assert.equal(harness.sandbox.activeReloadTrackedOperations, 0);
  }

  {
    const identityGate = deferred();
    const harness = createHarness({ identityGate: identityGate.promise });
    const inFlight = harness.dispatch({ type: "resolve-execution-identity", source: "qianchuan" });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(harness.identityResolutionCalls(), 1);
    assert.equal(harness.sandbox.activeReloadTrackedOperations, 1);
    assert.ok(
      harness.sandbox.__activeReloadWorkReasons().includes("active_snapshot_sync"),
      "version reconciliation must see an admitted identity resolution as active work",
    );
    identityGate.resolve();
    assert.equal((await inFlight.response).ok, true);
    assert.equal(harness.sandbox.activeReloadTrackedOperations, 0);
  }

  assert.match(
    background,
    /message\.type === "resolve-execution-identity"[\s\S]{0,300}runReloadTrackedOperation\(\s*"execution_identity_resolution"[\s\S]{0,300}allowInheritedAdmission:\s*false/,
    "the runtime identity entry must use the non-inheritable reload gate",
  );
  assert.match(
    background,
    /message\.type === "run-authorized-execution"[\s\S]{0,700}await ensureVersionReloadStateHydrated\(\)[\s\S]{0,300}runAuthorizedExecution/,
    "the DOM-mutation execution entry must hydrate before shared privileged admission",
  );

  console.log("background runtime reload-gate tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
