"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const background = fs.readFileSync(path.join(__dirname, "background.js"), "utf8");
const policyStart = background.indexOf("function parsedExtensionVersion");
const policyEnd = background.indexOf("\nconst FULL_SCAN_PAGES", policyStart);
assert.ok(policyStart >= 0 && policyEnd > policyStart, "version recovery policy block must remain independently testable");
const policySource = background.slice(policyStart, policyEnd);
const barrierStart = background.indexOf("function assertVersionReloadAllowsPrivilegedExecution");
const barrierEnd = background.indexOf("\nconst authorizedExecutionPromises", barrierStart);
assert.ok(barrierStart >= 0 && barrierEnd > barrierStart, "privileged reload barrier must remain independently testable");
const barrierSource = background.slice(barrierStart, barrierEnd);

assert.match(background, /fetch\(`\$\{BRIDGE_URL\}\/activation\/status`/);
assert.match(background, /"X-Dian-Agent-Extension-Version": String\(runtimeVersion \|\| ""\)/);
assert.match(background, /VERSION_RECONCILE_ALARM/);
assert.match(background, /periodInMinutes: VERSION_RECONCILE_PERIOD_MINUTES/);
assert.match(background, /VERSION_RELOAD_VERIFY_ALARM/);
assert.match(background, /chrome\.storage\.session/);
assert.ok(
  background.indexOf("await checkActivationStatus(manifestVersion)")
    < background.indexOf("fetchWithTimeout(`${BRIDGE_URL}/auth/status`"),
  "public activation reconciliation must run before the authenticated session receipt",
);

function memoryArea(values = {}, { onSet = null } = {}) {
  return {
    values,
    async get(key) { return { [key]: values[key] }; },
    async set(patch) {
      Object.assign(values, patch);
      if (typeof onSet === "function") await onSet(patch);
    },
    async remove(key) { delete values[key]; },
  };
}

function activation(required, installed = required, state = "reload_required") {
  return {
    activation_contract_version: 1,
    agent_version: required,
    required_extension_version: required,
    installed_extension_version: installed,
    activation: {
      state,
      ready: state === "active",
      reload_required: state === "reload_required",
    },
  };
}

function createHarness({
  runtimeVersion = "4.13.5",
  updateUrl = "",
  now = 1_900_000_000_000,
  localValues = {},
  sessionValues = {},
  fullScanPromise = null,
  authorizedExecutionPromises = new Map(),
  executionJournalAdvancePromises = new Map(),
  executionReadbackRunPromises = new Map(),
  executionReadbackRecoveryPromise = null,
  storageMutationQueue = Promise.resolve(),
  onLocalSet = null,
  onSessionSet = null,
} = {}) {
  const local = memoryArea(localValues, { onSet: onLocalSet });
  const session = memoryArea(sessionValues, { onSet: onSessionSet });
  const alarms = new Map();
  let reloadCalls = 0;
  let clock = now;
  const sandbox = {
    console,
    Date: { now: () => clock },
    VERSION_RELOAD_GUARD_KEY: "dianAgentVersionReloadV1",
    VERSION_RELOAD_SESSION_KEY: "dianAgentVersionReloadSessionV1",
    VERSION_RELOAD_VERIFY_ALARM: "dian-agent-extension-reload-verify",
    VERSION_RECONCILE_ALARM: "dian-agent-extension-version-reconcile",
    VERSION_RELOAD_MAX_ATTEMPTS: 2,
    VERSION_RELOAD_COOLDOWN_MS: 45 * 1000,
    VERSION_RECONCILE_PERIOD_MINUTES: 5,
    versionReconcileInFlight: null,
    versionReloadTransitionPending: false,
    versionReloadStateHydrationPromise: null,
    activeReloadTrackedOperations: 0,
    scanRecoveryPromise: null,
    fullScanPromise,
    authorizedExecutionPromises,
    executionJournalAdvancePromises,
    executionReadbackRunPromises,
    executionReadbackRecoveryPromise,
    storageMutationQueue,
    chrome: {
      runtime: {
        getManifest: () => ({ version: runtimeVersion, ...(updateUrl ? { update_url: updateUrl } : {}) }),
        reload() { reloadCalls += 1; },
      },
      storage: { local, session },
      alarms: {
        async get(name) { return alarms.get(name) || null; },
        create(name, options) { alarms.set(name, { name, ...options }); },
        async clear(name) { return alarms.delete(name); },
      },
    },
  };
  vm.runInNewContext(
    `${policySource}\n${barrierSource}\n` +
      "globalThis.__hydrateReloadState = ensureVersionReloadStateHydrated;\n" +
      "globalThis.__assertReloadAllowsExecution = assertVersionReloadAllowsPrivilegedExecution;\n" +
      "globalThis.__assertReloadAllowsNewWork = assertVersionReloadAllowsNewWork;\n" +
      "globalThis.__runReloadTrackedOperation = runReloadTrackedOperation;",
    sandbox,
    { filename: "background-version-recovery.vm.js" },
  );
  return {
    sandbox,
    local,
    session,
    alarms,
    reloadCalls: () => reloadCalls,
    advance(ms) { clock += ms; },
  };
}

(async () => {
  {
    const now = 1_900_000_000_000;
    const harness = createHarness({
      now,
      sessionValues: {
        dianAgentVersionReloadSessionV1: {
          pair: "4.14.0|4.13.5",
          requested_at: now - 1_000,
          attempt: 1,
        },
      },
    });
    await harness.sandbox.__hydrateReloadState();
    assert.equal(harness.sandbox.versionReloadTransitionPending, true);
    assert.throws(
      () => harness.sandbox.__assertReloadAllowsNewWork("authorized_execution"),
      (error) => error.code === "EXTENSION_RELOAD_PENDING" && error.work_kind === "authorized_execution",
      "a fresh worker must restore the durable admission latch before any reconciliation call",
    );
    assert.equal(harness.reloadCalls(), 0);
  }

  {
    const now = 1_900_000_000_000;
    const harness = createHarness({
      now,
      localValues: {
        dianAgentVersionReloadV1: {
          pair: "4.14.0|4.13.5",
          attempts: 1,
          last_requested_at: now - 46_000,
        },
      },
      sessionValues: {},
    });
    await harness.sandbox.__hydrateReloadState();
    assert.equal(
      harness.sandbox.versionReloadTransitionPending,
      true,
      "a recent durable local reload guard must survive an empty session area",
    );
    assert.throws(
      () => harness.sandbox.__assertReloadAllowsExecution(),
      (error) => error.code === "EXTENSION_RELOAD_PENDING",
    );
  }

  {
    const harness = createHarness({
      runtimeVersion: "4.14.0",
      localValues: { dianAgentVersionReloadV1: { pair: "old", attempts: 2 } },
      sessionValues: { dianAgentVersionReloadSessionV1: { pair: "old", requested_at: 1 } },
    });
    harness.alarms.set("dian-agent-extension-reload-verify", { name: "dian-agent-extension-reload-verify" });
    const result = await harness.sandbox.reconcileRequiredExtensionVersion(
      activation("4.14.0", "4.14.0", "active"),
      "test-match",
    );
    assert.equal(result.version_match, true);
    assert.equal(result.reload_state, "matched");
    assert.equal(harness.reloadCalls(), 0);
    assert.equal(harness.local.values.dianAgentVersionReloadV1, undefined);
    assert.equal(harness.session.values.dianAgentVersionReloadSessionV1, undefined);
    assert.equal(harness.alarms.has("dian-agent-extension-reload-verify"), false);
  }

  {
    const now = 1_900_000_000_000;
    const harness = createHarness({
      now,
      sessionValues: {
        dianAgentVersionReloadSessionV1: {
          pair: "4.14.0|4.13.5",
          requested_at: now - 1_000,
          attempt: 1,
        },
      },
    });
    const result = await harness.sandbox.reconcileRequiredExtensionVersion(
      activation("4.14.0"),
      "fresh-worker-pending-reload",
    );
    assert.equal(result.reload_state, "reload_pending");
    assert.equal(harness.reloadCalls(), 0);
    assert.equal(harness.sandbox.versionReloadTransitionPending, true);
    assert.throws(
      () => harness.sandbox.__assertReloadAllowsExecution(),
      (error) => error.code === "EXTENSION_RELOAD_PENDING",
      "a durable pending reload must restore the privileged execution barrier in a fresh worker",
    );
  }

  {
    const now = 1_900_000_000_000;
    const guard = {
      pair: "4.14.0|4.13.5",
      attempts: 1,
      first_requested_at: now - 10_000,
      last_requested_at: now - 1_000,
    };
    const harness = createHarness({
      now,
      localValues: { dianAgentVersionReloadV1: guard },
      sessionValues: {
        dianAgentVersionReloadSessionV1: {
          pair: "4.14.0|4.13.5",
          requested_at: now - 1_000,
          attempt: 1,
        },
      },
      executionReadbackRecoveryPromise: Promise.resolve(),
    });
    const result = await harness.sandbox.reconcileRequiredExtensionVersion(
      activation("4.14.0"),
      "fresh-worker-pending-reload-busy",
    );
    assert.equal(result.reload_state, "reload_deferred_busy");
    assert.equal(result.reload_attempts, 1);
    assert.equal(harness.local.values.dianAgentVersionReloadV1.attempts, 1);
    assert.equal(harness.reloadCalls(), 0);
    assert.equal(harness.alarms.has("dian-agent-extension-reload-verify"), true);
    assert.equal(harness.sandbox.versionReloadTransitionPending, true);
    assert.throws(
      () => harness.sandbox.__assertReloadAllowsExecution(),
      (error) => error.code === "EXTENSION_RELOAD_PENDING",
      "busy deferral must preserve a durable reload barrier in a fresh worker",
    );
  }

  const durableLocal = {};
  {
    const harness = createHarness({ runtimeVersion: "4.13.5", localValues: durableLocal });
    const mismatch = activation("4.14.0");
    const [first, coalesced] = await Promise.all([
      harness.sandbox.reconcileRequiredExtensionVersion(mismatch, "manual"),
      harness.sandbox.reconcileRequiredExtensionVersion(mismatch, "alarm"),
    ]);
    assert.equal(first.reload_requested, true);
    assert.equal(coalesced.reload_requested, true);
    assert.equal(harness.reloadCalls(), 1, "overlapping checks must request one runtime reload");
    assert.equal(durableLocal.dianAgentVersionReloadV1.attempts, 1);
    assert.equal(harness.alarms.has("dian-agent-extension-reload-verify"), true);

    const pending = await harness.sandbox.reconcileRequiredExtensionVersion(mismatch, "duplicate-message");
    assert.equal(pending.reload_state, "reload_pending");
    assert.equal(harness.reloadCalls(), 1);
  }

  {
    const secondRuntime = createHarness({
      runtimeVersion: "4.13.5",
      now: 1_900_000_060_000,
      localValues: durableLocal,
    });
    const second = await secondRuntime.sandbox.reconcileRequiredExtensionVersion(
      activation("4.14.0"),
      "version-reload-verify-alarm",
    );
    assert.equal(second.reload_requested, true);
    assert.equal(second.reload_attempts, 2);
    assert.equal(secondRuntime.reloadCalls(), 1);
    assert.equal(durableLocal.dianAgentVersionReloadV1.attempts, 2);
  }

  {
    const blockedRuntime = createHarness({
      runtimeVersion: "4.13.5",
      now: 1_900_000_120_000,
      localValues: durableLocal,
    });
    const blocked = await blockedRuntime.sandbox.reconcileRequiredExtensionVersion(
      activation("4.14.0"),
      "version-reconcile-alarm",
    );
    assert.equal(blocked.reload_state, "reload_blocked");
    assert.equal(blocked.reload_blocked, true);
    assert.equal(blocked.reload_attempts, 2);
    assert.equal(blockedRuntime.reloadCalls(), 0, "the durable pair guard must prevent an infinite reload loop");
  }

  {
    const repairedPair = createHarness({
      runtimeVersion: "4.13.5",
      now: 1_900_000_180_000,
      localValues: durableLocal,
    });
    const nextVersion = await repairedPair.sandbox.reconcileRequiredExtensionVersion(
      activation("4.14.1"),
      "new-install",
    );
    assert.equal(nextVersion.reload_requested, true, "a genuinely new required/runtime pair gets its own bounded attempt budget");
    assert.equal(nextVersion.reload_attempts, 1);
    assert.equal(repairedPair.reloadCalls(), 1);
  }

  for (const [label, busyOptions, localPatch = {}] of [
    ["active-full-scan", { fullScanPromise: Promise.resolve() }],
    ["authorized-execution", { authorizedExecutionPromises: new Map([["authorization", Promise.resolve()]]) }],
    ["journal-advance", { executionJournalAdvancePromises: new Map([["action", Promise.resolve()]]) }],
    ["readback-run", { executionReadbackRunPromises: new Map([["action", Promise.resolve()]]) }],
    ["readback-recovery", { executionReadbackRecoveryPromise: Promise.resolve() }],
    ["persisted-running-scan", {}, { fullScan: { status: "running", run_id: "scan-1" } }],
  ]) {
    const guard = {
      pair: "4.14.0|4.13.5",
      attempts: 1,
      first_requested_at: 1_899_999_800_000,
      last_requested_at: 1_899_999_800_000,
    };
    const harness = createHarness({
      ...busyOptions,
      localValues: { dianAgentVersionReloadV1: guard, ...localPatch },
    });
    const result = await harness.sandbox.reconcileRequiredExtensionVersion(
      activation("4.14.0"),
      label,
    );
    assert.equal(result.reload_state, "reload_deferred_busy", label);
    assert.equal(result.reload_requested, false, label);
    assert.equal(result.reload_attempts, 1, label);
    assert.equal(harness.local.values.dianAgentVersionReloadV1.attempts, 1, `${label} must not consume an attempt`);
    assert.equal(harness.reloadCalls(), 0, `${label} must not reload`);
    assert.equal(harness.alarms.has("dian-agent-extension-reload-verify"), true, `${label} must schedule verification`);
    assert.equal(
      harness.sandbox.versionReloadTransitionPending,
      true,
      `${label} must preserve the unresolved durable reload barrier while existing work drains`,
    );
  }

  {
    let releaseFirst;
    let releaseSecond;
    const firstMutation = new Promise((resolve) => { releaseFirst = resolve; });
    const secondMutation = new Promise((resolve) => { releaseSecond = resolve; });
    const harness = createHarness({ storageMutationQueue: firstMutation });
    const reconciliation = harness.sandbox.reconcileRequiredExtensionVersion(
      activation("4.14.0"),
      "storage-drain",
    );
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(harness.reloadCalls(), 0, "reload must wait for the current storage mutation");
    assert.equal(harness.sandbox.versionReloadTransitionPending, true);
    harness.sandbox.storageMutationQueue = secondMutation;
    releaseFirst();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(harness.reloadCalls(), 0, "reload must also wait for a mutation appended while draining");
    releaseSecond();
    const result = await reconciliation;
    assert.equal(result.reload_state, "reload_requested");
    assert.equal(harness.reloadCalls(), 1);
  }

  {
    let harness;
    harness = createHarness({
      onLocalSet: async (patch) => {
        if (patch.dianAgentVersionReloadV1) {
          // Simulate an untracked sibling becoming active during an awaited
          // guard write.  The final busy recheck must still stop the reload.
          harness.sandbox.executionReadbackRecoveryPromise = Promise.resolve();
        }
      },
    });
    const result = await harness.sandbox.reconcileRequiredExtensionVersion(
      activation("4.14.0"),
      "final-busy-recheck",
    );
    assert.equal(result.reload_state, "reload_deferred_busy");
    assert.equal(result.reload_requested, false);
    assert.equal(harness.reloadCalls(), 0, "the final post-write busy check must prevent reload");
    assert.ok(result.reload_busy_reasons.includes("execution_readback_recovery"));
    assert.equal(harness.local.values.dianAgentVersionReloadV1, undefined, "a deferred reload must roll back its attempt guard");
    assert.equal(harness.session.values.dianAgentVersionReloadSessionV1, undefined, "a deferred reload must roll back its session latch");
    assert.equal(harness.sandbox.versionReloadTransitionPending, false, "deferral may reopen admission only after rollback");
  }

  {
    const barrierSandbox = createHarness().sandbox;
    await barrierSandbox.__hydrateReloadState();
    barrierSandbox.versionReloadTransitionPending = true;
    assert.throws(
      () => barrierSandbox.__assertReloadAllowsExecution(),
      (error) => error.code === "EXTENSION_RELOAD_PENDING",
      "a pending reload transition must close privileged execution entry",
    );
    barrierSandbox.versionReloadTransitionPending = false;
    assert.doesNotThrow(() => barrierSandbox.__assertReloadAllowsExecution());
    let ordinaryEntries = 0;
    barrierSandbox.versionReloadTransitionPending = true;
    await assert.rejects(
      barrierSandbox.__runReloadTrackedOperation("page_data_sync", async () => { ordinaryEntries += 1; }),
      (error) => error.code === "EXTENSION_RELOAD_PENDING",
    );
    assert.equal(ordinaryEntries, 0, "ordinary page-data must not start after the transition latch closes");
    assert.equal(barrierSandbox.activeReloadTrackedOperations, 0);
    barrierSandbox.versionReloadTransitionPending = false;
    assert.equal(
      await barrierSandbox.__runReloadTrackedOperation("page_data_sync", async () => { ordinaryEntries += 1; return "ok"; }),
      "ok",
    );
    assert.equal(ordinaryEntries, 1, "ordinary page-data remains unchanged when no reload is pending");
    assert.equal(barrierSandbox.activeReloadTrackedOperations, 0);
    for (const [label, pattern] of [
      ["authorized execution", /function runAuthorizedExecution\([^)]*\)[\s\S]{0,500}assertVersionReloadAllowsNewWork\("authorized_execution"\)/],
      ["full scan", /function startFullScan\([^)]*\)[\s\S]{0,500}assertVersionReloadAllowsNewWork\("full_scan"\)/],
      ["journal advance", /function advanceExecutionJournal\([^)]*\)[\s\S]{0,500}assertVersionReloadAllowsNewWork\("execution_journal_advance"\)/],
      ["readback run", /function runExecutionReadbackJob\([^)]*\)[\s\S]{0,500}assertVersionReloadAllowsNewWork\("execution_readback_run"\)/],
      ["readback recovery", /async function recoverExecutionReadbackJobs\([^)]*\)[\s\S]{0,500}assertVersionReloadAllowsNewWork\("execution_readback_recovery"\)/],
    ]) {
      assert.match(background, pattern, `${label} must enforce the shared reload admission barrier`);
    }
    assert.match(background, /runReloadTrackedOperation\("page_data_sync"/);
    assert.match(background, /runReloadTrackedOperation\("manual_sync"/);
    assert.match(background, /function recoverInterruptedScan\([^)]*\)[\s\S]{0,500}assertVersionReloadAllowsNewWork\("scan_recovery"\)/);
    assert.match(background, /function runManualExecutionReadback\([^)]*\)[\s\S]{0,700}assertVersionReloadAllowsNewWork\("manual_execution_readback"\)/);
  }

  for (const [label, options, status, expectedState] of [
    ["disk-not-updated", {}, activation("4.14.0", "4.13.5", "installed_version_mismatch"), "installation_repair_required"],
    ["store-managed", { updateUrl: "https://example.test/update" }, activation("4.14.0"), "browser_store_update_required"],
    ["agent-older", { runtimeVersion: "4.14.1" }, activation("4.14.0"), "agent_version_older"],
  ]) {
    const harness = createHarness(options);
    const result = await harness.sandbox.reconcileRequiredExtensionVersion(status, label);
    assert.equal(result.reload_state, expectedState, label);
    assert.equal(result.reload_blocked, true, label);
    assert.equal(harness.reloadCalls(), 0, `${label} must fail closed without runtime reload`);
  }

  console.log("background version recovery tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
