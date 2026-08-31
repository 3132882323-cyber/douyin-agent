const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");

assert.match(script, /let agentConnectionState = "checking"/);
assert.match(script, /function agentWriteGateOpen\(\)[\s\S]*?agentConnectionState === "online"/);
assert.match(script, /!agentWriteGateOpen\(\) && method !== "GET"/);

assert.match(script, /const DASHBOARD_READ_CONCURRENCY = 4/);
const dashboardStart = script.indexOf("async function loadDashboardOnce()");
const dashboardEnd = script.indexOf("\nfunction startDashboardLoad()", dashboardStart);
assert.ok(dashboardStart >= 0 && dashboardEnd > dashboardStart, "single dashboard load block should exist");
const dashboardBlock = script.slice(dashboardStart, dashboardEnd);
const generationStart = dashboardBlock.indexOf("const loadGeneration = ++dashboardLoadGeneration");
const settled = dashboardBlock.indexOf("await runDashboardReadPool");
const receiptRead = dashboardBlock.indexOf("await dashboardBridgeReceiptRead(loadGeneration)");
const finalReceiptRead = dashboardBlock.indexOf("await dashboardBridgeReceiptRead(loadGeneration)", receiptRead + 1);
const receiptAcceptanceGate = dashboardBlock.indexOf("if (!dashboardBridgeAccepted(", receiptRead);
const receiptFailureReturn = dashboardBlock.indexOf("return true;", receiptAcceptanceGate);
const staleGuard = dashboardBlock.indexOf("if (!dashboardLoadIsCurrent(loadGeneration)) return false", settled);
const firstRenderMutation = dashboardBlock.indexOf("currentDashboardFailures = failedModules");
assert.ok(generationStart >= 0 && generationStart < settled);
assert.ok(
  generationStart < receiptRead
    && receiptRead < receiptAcceptanceGate
    && receiptAcceptanceGate < receiptFailureReturn
    && receiptFailureReturn < settled,
  "connection receipt acceptance must complete and fail fast before any business read pool starts",
);
assert.ok(
  settled < finalReceiptRead && finalReceiptRead < firstRenderMutation,
  "the Agent receipt must be revalidated after business reads and before write-gate state can change",
);
assert.ok(settled < staleGuard && staleGuard < firstRenderMutation, "stale dashboard results must be rejected before state or DOM mutation");
assert.doesNotMatch(
  dashboardBlock.slice(settled, dashboardBlock.indexOf("]);", settled) + 3),
  /dashboardBridgeReceiptRead/,
  "the business pool must not duplicate or delay the independent connection receipt phase",
);
assert.match(dashboardBlock, /loadSystemStatus\(loadGeneration\)/);
assert.match(dashboardBlock, /loadAiCenter\(loadGeneration\)/);
assert.match(dashboardBlock, /loadOperatorMemory\(loadGeneration\)/);
assert.match(dashboardBlock, /renderQianchuanAccounts\(accounts, qianchuanScope\)/);
assert.ok(
  dashboardBlock.indexOf("renderQianchuanAccounts(accounts, qianchuanScope)")
    < dashboardBlock.indexOf("renderProductCapability("),
  "store/account reconciliation must run before capability and journey derivation",
);
assert.match(dashboardBlock, /selected_account_key:\s*qianchuanScope\.accountKey/);
assert.match(dashboardBlock, /selectedAccountKey:\s*qianchuanScope\.accountKey/);
assert.match(dashboardBlock, /maybeReturnFromTargetedScan\(extensionDashboard\.fullScan \|\| \{\}, loadGeneration\)/);
const strictBridgeVerdict = dashboardBlock.indexOf("const bridgeAcceptanceBlocked = !dashboardBridgeAccepted(");
const acceptedConnectionRender = dashboardBlock.indexOf("renderConnection(\n    true", strictBridgeVerdict);
const firstAcceptedBusinessRender = dashboardBlock.indexOf("renderQianchuanAccounts(accounts, qianchuanScope)", strictBridgeVerdict);
assert.ok(strictBridgeVerdict >= 0, "the dashboard must derive the strict bridge verdict before rendering business state");
assert.ok(
  finalReceiptRead < strictBridgeVerdict
    && strictBridgeVerdict < acceptedConnectionRender
    && acceptedConnectionRender < firstAcceptedBusinessRender,
  "a positive bridge verdict must restore the global connection gate before any business renderer recalculates disabled controls",
);

const bridgeStart = script.indexOf("async function bridgeFetch");
const bridgeEnd = script.indexOf("\nfunction renderConnection", bridgeStart);
const bridgeBlock = script.slice(bridgeStart, bridgeEnd);
assert.match(bridgeBlock, /const requestGeneration = dashboardLoadGeneration/);
assert.match(bridgeBlock, /requestGeneration === dashboardLoadGeneration/);
assert.match(bridgeBlock, /method !== "GET"[\s\S]*?setAgentAvailability\(false/);

const refreshStart = script.indexOf("async function refreshAll(");
const refreshEnd = script.indexOf("\nfunction qianchuanSyncAgeLabel", refreshStart);
assert.ok(refreshStart >= 0 && refreshEnd > refreshStart, "dashboard refresh function should exist");
const refreshBlock = script.slice(refreshStart, refreshEnd);
assert.doesNotMatch(
  refreshBlock,
  /renderConnection\(false,\s*"本地 Agent 未启动"/,
  "a dashboard rendering error must not be relabelled as an Agent liveness failure",
);
assert.doesNotMatch(
  refreshBlock,
  /renderSimpleAgentError\(/,
  "a dashboard rendering error must not open the Agent repair journey",
);
assert.match(
  refreshBlock,
  /dashboard-load-warning|工作台[^"`]*加载|renderDashboardLoadFailure/,
  "a dashboard rendering error should remain visible as a retryable load failure",
);

assert.match(script, /if \(!renderFullScan\(scan\)\) return/);
assert.match(script, /DianAgentScanPolicy\.shouldAcceptScanSnapshot\(latestFullScanSnapshot, scan\)/);

function installImmediateTestTimers(sandbox, { changeGenerationWhileWaiting = false } = {}) {
  let nextTimer = 0;
  const activeTimers = new Set();
  sandbox.setTimeout = (callback, delay = 0) => {
    const timer = ++nextTimer;
    activeTimers.add(timer);
    if (Number(delay) >= Number(sandbox.DASHBOARD_READ_TIMEOUT_MS)) {
      setImmediate(() => {
        if (!activeTimers.delete(timer)) return;
        callback();
      });
    } else {
      activeTimers.delete(timer);
      if (changeGenerationWhileWaiting) sandbox.dashboardLoadGeneration += 1;
      callback();
    }
    return timer;
  };
  sandbox.clearTimeout = (timer) => { activeTimers.delete(timer); };
}

function makeReadRuntime(responses, { generation = 7, changeGenerationWhileWaiting = false } = {}) {
  const queue = [...responses];
  const calls = [];
  const availability = [];
  const sandbox = {
    console,
    DASHBOARD_READ_RETRY_DELAY_MS: 1,
    DASHBOARD_READ_TIMEOUT_MS: 5000,
    DASHBOARD_READ_CONCURRENCY: 4,
    RECOVERABLE_AGENT_AUTH_ERROR_CODES: new Set([
      "agent_auth_timeout", "agent_session_timeout", "agent_session_required",
      "agent_session_invalid", "agent_session_expired",
    ]),
    dashboardLoadGeneration: generation,
    AbortController,
    agentWriteGateOpen() { return true; },
    currentAgentWriteBlockMessage() { return "blocked"; },
    setAgentAvailability(...args) { availability.push(args); },
    bridgeAuthClient: {
      async fetchJson(pathname, options) {
        calls.push({ pathname, options });
        const next = queue.shift();
        if (next instanceof Error) throw next;
        return next;
      },
    },
    chrome: { runtime: { async sendMessage() { return {}; } } },
  };
  installImmediateTestTimers(sandbox, { changeGenerationWhileWaiting });
  sandbox.dashboardLoadIsCurrent = (candidate) => candidate === sandbox.dashboardLoadGeneration;
  vm.runInNewContext(bridgeBlock, sandbox, { filename: "sidepanel-dashboard-read.vm.js" });
  return { sandbox, calls, availability };
}

function makeRuntimeMessageReadRuntime(
  responses,
  { generation = 7, messageType = "test-bridge", authProbeResponses = [] } = {},
) {
  const queue = [...responses];
  const authQueue = [...authProbeResponses];
  const calls = [];
  const authCalls = [];
  const sandbox = {
    console,
    DASHBOARD_READ_RETRY_DELAY_MS: 1,
    DASHBOARD_READ_TIMEOUT_MS: 5000,
    DASHBOARD_READ_CONCURRENCY: 4,
    RECOVERABLE_AGENT_AUTH_ERROR_CODES: new Set([
      "agent_auth_timeout", "agent_session_timeout", "agent_session_required",
      "agent_session_invalid", "agent_session_expired",
    ]),
    dashboardLoadGeneration: generation,
    AbortController,
    agentWriteGateOpen() { return true; },
    currentAgentWriteBlockMessage() { return "blocked"; },
    setAgentAvailability() {},
    bridgeAuthClient: {
      async fetchJson(pathname) {
        authCalls.push(pathname);
        const next = authQueue.shift();
        if (next instanceof Error) throw next;
        return next || {};
      },
    },
    chrome: {
      runtime: {
        id: "a".repeat(32),
      getManifest() { return { version: "4.14.8" }; },
        async sendMessage(message) {
          assert.equal(message.type, messageType);
          calls.push(message);
          const next = queue.shift();
          if (next instanceof Error) throw next;
          return next;
        },
      },
    },
  };
  installImmediateTestTimers(sandbox);
  sandbox.dashboardLoadIsCurrent = (candidate) => candidate === sandbox.dashboardLoadGeneration;
  vm.runInNewContext(bridgeBlock, sandbox, { filename: "sidepanel-dashboard-runtime-message.vm.js" });
  return { sandbox, calls, authCalls };
}

function errorWithStatus(message, status) {
  const error = new Error(message);
  if (status !== undefined) error.status = status;
  return error;
}

function topLevelFunctionSource(signature) {
  const start = script.indexOf(signature);
  assert.ok(start >= 0, `${signature} should exist`);
  const tail = script.slice(start + signature.length);
  const next = tail.search(/\n(?=(?:async )?function [A-Za-z_$])/);
  return script.slice(start, next < 0 ? script.length : start + signature.length + next);
}

function assertSupplementalReadSourceContracts() {
  const contracts = [
    ["async function loadSystemStatus(", "/system/status"],
    ["async function loadAiStatus(", "/ai/status"],
    ["async function loadAiContextPreview(", "/ai/context-preview"],
    ["async function loadOperatorMemory(", "/memory"],
  ];
  contracts.forEach(([signature, pathname]) => {
    const source = topLevelFunctionSource(signature);
    assert.ok(
      source.includes(`dashboardRead("${pathname}", generation)`),
      `${pathname} must inherit the dashboard's bounded five-second timeout and one-retry policy`,
    );
    assert.doesNotMatch(
      source,
      /bridgeFetch\(/,
      `${pathname} must not bypass the bounded dashboard read path`,
    );
  });
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function nextTurn() {
  await new Promise((resolve) => setImmediate(resolve));
}

async function assertDashboardReadPoolContract() {
  const poolSandbox = { DASHBOARD_READ_CONCURRENCY: 4 };
  vm.runInNewContext(
    topLevelFunctionSource("async function runDashboardReadPool("),
    poolSandbox,
    { filename: "sidepanel-dashboard-pool.vm.js" },
  );
  let active = 0;
  let maximumActive = 0;
  const completionOrder = [];
  const delays = [30, 2, 24, 4, 18, 6, 12, 8, 1];
  const tasks = delays.map((delay, index) => async () => {
    active += 1;
    maximumActive = Math.max(maximumActive, active);
    await new Promise((resolve) => setTimeout(resolve, delay));
    active -= 1;
    completionOrder.push(index);
    if (index === 5) throw new Error("task-five-failed");
    return `value-${index}`;
  });
  const results = await poolSandbox.runDashboardReadPool(tasks);
  assert.equal(maximumActive, 4, "the dashboard read pool must use exactly four workers under load");
  assert.notDeepEqual(completionOrder, delays.map((_, index) => index), "the fixture must complete out of order");
  assert.equal(results.length, tasks.length);
  results.forEach((result, index) => {
    if (index === 5) {
      assert.equal(result.status, "rejected");
      assert.match(result.reason?.message || "", /task-five-failed/);
      return;
    }
    assert.equal(result.status, "fulfilled");
    assert.equal(result.value, `value-${index}`, "pool results must retain input index order");
  });

  const businessGates = Array.from({ length: 4 }, () => deferred());
  const starts = [];
  const priorityRun = poolSandbox.runDashboardReadPool([
    async () => { starts.push("priority-task"); return "healthy"; },
    ...businessGates.map((gate, index) => async () => {
      starts.push(`business-${index}`);
      return gate.promise;
    }),
  ]);
  await nextTurn();
  assert.equal(starts[0], "priority-task", "the first pool factory must start before later hanging tasks");
  assert.ok(starts.includes("priority-task"), "the first pool factory must start immediately within the worker cap");
  businessGates.forEach((gate, index) => gate.resolve(`business-value-${index}`));
  const priorityResults = await priorityRun;
  assert.equal(priorityResults[0].status, "fulfilled");
  assert.equal(priorityResults[0].value, "healthy");
}

async function assertDashboardConnectionPhaseContract() {
  const healthyReceipt = {
    ok: true,
    liveness_ok: true,
    authenticated: true,
    version_match: true,
  };
  const offlineReceipt = {
    ok: false,
    liveness_ok: false,
    authenticated: false,
    version_match: true,
    error: "offline",
  };
  const receiptHealthy = (receipt = {}) => receipt.ok === true
    && receipt.liveness_ok === true
    && receipt.authenticated === true
    && receipt.version_match === true;

  async function runScenario(receipts) {
    const receiptQueue = [...receipts];
    const elements = new Map();
    const trace = {
      receiptCalls: 0,
      poolCalls: 0,
      connectionStates: [],
      journeyStates: [],
      writeGateOpened: false,
    };
    const sandbox = {
      console,
      dashboardLoadGeneration: 0,
      agentConnectionState: "checking",
      agentWriteBlockMessage: "",
      CHECKING_AGENT_WRITE_BLOCK_MESSAGE: "checking",
      currentChengfangAgentRuntime: null,
      currentChengfangA2Pilot: null,
      currentControlTaskSummary: null,
      currentExtensionSettings: {},
      latestBrief: "",
      document: {
        activeElement: null,
        getElementById(id) {
          if (!elements.has(id)) elements.set(id, { hidden: true, textContent: "", focus() {} });
          return elements.get(id);
        },
      },
      async dashboardBridgeReceiptRead() {
        trace.receiptCalls += 1;
        const next = receiptQueue.shift();
        if (next instanceof Error) throw next;
        return next;
      },
      dashboardLoadIsCurrent(generation) { return generation === sandbox.dashboardLoadGeneration; },
      dashboardBridgeReceiptFailure(result, receipt) {
        if (result?.status !== "fulfilled" || !receiptHealthy(receipt)) {
          return { title: "Agent offline", detail: "repair", headline: "connection blocked" };
        }
        return null;
      },
      dashboardBridgeAccepted(failure, receipt) { return !failure && receiptHealthy(receipt); },
      dashboardBridgeFailure() {
        return { title: "Agent offline", detail: "repair", headline: "connection blocked" };
      },
      async runDashboardReadPool(tasks) {
        trace.poolCalls += 1;
        return tasks.map(() => ({ status: "fulfilled", value: {} }));
      },
      renderConnection(online) {
        trace.connectionStates.push(online);
        if (online === true) trace.writeGateOpened = true;
      },
      renderJourneyCommand(state = {}) { trace.journeyStates.push(state.online); },
      reconcileQianchuanScope() { return { accountKey: "" }; },
      renderProductCapability() { return {}; },
      async maybeReturnFromTargetedScan() {},
    };
    const builtins = new Set(["Array", "Boolean", "Date", "JSON", "Math", "Number", "Object", "Promise", "String"]);
    for (const match of dashboardBlock.matchAll(/(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(/g)) {
      const name = match[1];
      if (!builtins.has(name) && !(name in sandbox)) sandbox[name] = () => undefined;
    }
    vm.runInNewContext(dashboardBlock, sandbox, { filename: "sidepanel-dashboard-two-phase.vm.js" });
    trace.result = await sandbox.loadDashboardOnce();
    return trace;
  }

  const initialOffline = await runScenario([offlineReceipt]);
  assert.equal(initialOffline.result, true, "a rejected initial receipt must finish through the repair-state render path");
  assert.equal(initialOffline.receiptCalls, 1);
  assert.equal(initialOffline.poolCalls, 0, "a rejected initial receipt must not start any business read");
  assert.deepEqual(initialOffline.connectionStates, [false]);
  assert.deepEqual(initialOffline.journeyStates, [false]);
  assert.equal(initialOffline.writeGateOpened, false);

  const finalOffline = await runScenario([healthyReceipt, offlineReceipt]);
  assert.equal(finalOffline.poolCalls, 1, "a positive initial receipt must allow the business pool to load");
  assert.equal(finalOffline.receiptCalls, 2, "the write boundary must revalidate the Agent after business reads");
  assert.ok(!finalOffline.connectionStates.includes(true), "a negative final receipt must never render the Agent online");
  assert.ok(!finalOffline.journeyStates.includes(true), "a negative final receipt must never render an online journey");
  assert.equal(finalOffline.writeGateOpened, false, "a negative final receipt must keep the write gate closed");

  const finalException = await runScenario([healthyReceipt, new Error("Agent exited during dashboard load")]);
  assert.equal(finalException.poolCalls, 1);
  assert.equal(finalException.receiptCalls, 2);
  assert.ok(!finalException.connectionStates.includes(true), "a final receipt exception must never render the Agent online");
  assert.ok(!finalException.journeyStates.includes(true), "a final receipt exception must never render an online journey");
  assert.equal(finalException.writeGateOpened, false, "a final receipt exception must keep the write gate closed");

  const fullyHealthy = await runScenario([healthyReceipt, healthyReceipt]);
  assert.equal(fullyHealthy.poolCalls, 1);
  assert.equal(fullyHealthy.receiptCalls, 2);
  assert.ok(fullyHealthy.connectionStates.includes(true), "two positive receipts must restore the connection gate");
  assert.ok(fullyHealthy.journeyStates.includes(true), "two positive receipts must restore the online journey");
  assert.equal(fullyHealthy.writeGateOpened, true, "two positive receipts must open the write gate");
}

async function assertDashboardSingleflightContract() {
  const firstRound = deferred();
  const trailingRound = deferred();
  const dirtyTrailingRound = deferred();
  let loadCount = 0;
  let activeLoads = 0;
  let maximumActiveLoads = 0;
  const sandbox = {
    dashboardLoadInFlight: null,
    dashboardLoadTrailing: null,
    loadDashboardOnce() {
      loadCount += 1;
      activeLoads += 1;
      maximumActiveLoads = Math.max(maximumActiveLoads, activeLoads);
      const operation = loadCount === 1
        ? firstRound.promise
        : loadCount === 2
          ? trailingRound.promise
          : loadCount === 3
            ? dirtyTrailingRound.promise
            : Promise.resolve("unexpected-extra-refresh");
      return operation.finally(() => { activeLoads -= 1; });
    },
  };
  vm.runInNewContext(
    `${topLevelFunctionSource("function startDashboardLoad()")}
${topLevelFunctionSource("function loadDashboard()")}`,
    sandbox,
    { filename: "sidepanel-dashboard-singleflight.vm.js" },
  );

  const first = sandbox.loadDashboard();
  await nextTurn();
  const joined = [sandbox.loadDashboard(), sandbox.loadDashboard(), sandbox.loadDashboard()];
  await nextTurn();
  assert.equal(loadCount, 1, "concurrent refreshes must join the active dashboard load");

  firstRound.resolve("round-one");
  await nextTurn();
  assert.equal(loadCount, 2, "a refresh requested during the first round must create one trailing load");

  const duringTrailing = [sandbox.loadDashboard(), sandbox.loadDashboard()];
  await nextTurn();
  assert.equal(loadCount, 2, "refreshes arriving during the trailing round must queue, never overlap it");
  trailingRound.resolve("round-two");
  await nextTurn();
  assert.equal(loadCount, 3, "mutations during the trailing round must coalesce into one final freshness load");
  assert.equal(maximumActiveLoads, 1, "dashboard refresh rounds must remain strictly serial");

  dirtyTrailingRound.resolve("round-three");
  await Promise.all([first, ...joined, ...duringTrailing]);
  await nextTurn();
  await nextTurn();
  assert.equal(loadCount, 3, "each active round may expose at most one coalesced pending refresh");
  assert.equal(maximumActiveLoads, 1, "singleflight must never run two dashboard loads concurrently");
  assert.equal(sandbox.dashboardLoadInFlight, null, "singleflight state must reset after the burst");
  assert.equal(sandbox.dashboardLoadTrailing, null, "the trailing marker must reset after the burst");
}

async function assertAgentAutoRecoveryContract() {
  let now = 10_000;
  let receiptCalls = 0;
  let refreshCalls = 0;
  const gate = deferred();
  const sandbox = {
    agentConnectionState: "offline",
    agentAutoRecoveryInFlight: null,
    agentAutoRecoveryLastAttemptAt: 0,
    AGENT_AUTO_RECOVERY_COOLDOWN_MS: 5000,
    Date: { now: () => now },
    dashboardRuntimeMessage() { receiptCalls += 1; return gate.promise; },
    dashboardBridgeReceiptNeedsConfirmation(receipt) { return receipt?.authenticated !== true; },
    async refreshAll() { refreshCalls += 1; },
  };
  vm.runInNewContext(
    topLevelFunctionSource("function maybeRecoverAgentOnRuntimeReady()"),
    sandbox,
    { filename: "sidepanel-agent-auto-recovery.vm.js" },
  );
  const first = sandbox.maybeRecoverAgentOnRuntimeReady();
  const joined = sandbox.maybeRecoverAgentOnRuntimeReady();
  assert.strictEqual(first, joined, "focus/runtime-ready bursts must share one recovery probe");
  gate.resolve({ authenticated: true });
  assert.equal(await first, true);
  await joined;
  assert.equal(receiptCalls, 1);
  assert.equal(refreshCalls, 1, "only a complete positive receipt may refresh the dashboard");
  assert.equal(sandbox.agentAutoRecoveryInFlight, null);

  assert.equal(await sandbox.maybeRecoverAgentOnRuntimeReady(), false);
  assert.equal(receiptCalls, 1, "the cooldown must suppress a focus loop");
  now += 5000;
  sandbox.agentConnectionState = "online";
  assert.equal(await sandbox.maybeRecoverAgentOnRuntimeReady(), false);
  assert.equal(receiptCalls, 1, "an online workbench must not probe on focus");
}

async function assertSupplementalReadsCannotHangSingleflight() {
  const hangingReads = Array.from({ length: 8 }, () => new Promise(() => undefined));
  const runtime = makeReadRuntime(hangingReads);
  Object.assign(runtime.sandbox, {
    document: { getElementById() { return { textContent: "" }; } },
    renderSystemStatus() {},
    renderAiStatus() {},
    renderAiContextPreview() {},
    renderOperatorMemory() {},
    setAiMessage() {},
  });
  vm.runInNewContext(
    [
      topLevelFunctionSource("async function loadSystemStatus("),
      topLevelFunctionSource("async function loadAiStatus("),
      topLevelFunctionSource("async function loadAiContextPreview("),
      topLevelFunctionSource("async function loadAiCenter("),
      topLevelFunctionSource("async function loadOperatorMemory("),
    ].join("\n"),
    runtime.sandbox,
    { filename: "sidepanel-supplemental-reads.vm.js" },
  );
  runtime.sandbox.dashboardLoadInFlight = null;
  runtime.sandbox.dashboardLoadTrailing = null;
  runtime.sandbox.loadDashboardOnce = () => Promise.all([
    runtime.sandbox.loadSystemStatus(7),
    runtime.sandbox.loadAiCenter(7),
    runtime.sandbox.loadOperatorMemory(7),
  ]);
  vm.runInNewContext(
    `${topLevelFunctionSource("function startDashboardLoad()")}
${topLevelFunctionSource("function loadDashboard()")}`,
    runtime.sandbox,
    { filename: "sidepanel-supplemental-singleflight.vm.js" },
  );

  let guardTimer;
  try {
    await Promise.race([
      runtime.sandbox.loadDashboard(),
      new Promise((_, reject) => {
        guardTimer = setTimeout(() => reject(new Error("supplemental dashboard reads did not settle")), 1000);
      }),
    ]);
  } finally {
    clearTimeout(guardTimer);
  }
  await nextTurn();

  assert.equal(runtime.calls.length, 8, "four hanging supplemental reads must each time out and retry exactly once");
  ["/system/status", "/ai/status", "/ai/context-preview", "/memory"].forEach((pathname) => {
    assert.equal(
      runtime.calls.filter((call) => call.pathname === pathname).length,
      2,
      `${pathname} must use the shared timeout and one-retry policy`,
    );
  });
  assert.equal(runtime.sandbox.dashboardLoadInFlight, null, "timed-out supplemental reads must release dashboard singleflight");
  assert.equal(runtime.sandbox.dashboardLoadTrailing, null, "timed-out supplemental reads must not leave a trailing refresh stuck");
}

(async () => {
  assertSupplementalReadSourceContracts();
  const healthyBridge = {
    ok: true,
    liveness_ok: true,
    authenticated: true,
    version_match: true,
  };
  const falseLiveness = {
    ok: false,
    liveness_ok: false,
    authenticated: false,
    version_match: true,
    error: "temporary liveness miss",
  };
  const recoveredBridgeReceipt = makeRuntimeMessageReadRuntime([
    falseLiveness,
    healthyBridge,
  ]);
  const recoveredReceipt = await recoveredBridgeReceipt.sandbox.dashboardBridgeReceiptRead(7);
  assert.equal(recoveredBridgeReceipt.calls.length, 2, "a negative liveness receipt must be confirmed once");
  assert.equal(recoveredReceipt.liveness_ok, true, "the positive confirmation must replace a transient negative receipt");

  const stillOffline = makeRuntimeMessageReadRuntime([
    falseLiveness,
    { ...falseLiveness, error: "confirmed offline" },
  ]);
  const failedReceipt = await stillOffline.sandbox.dashboardBridgeReceiptRead(7);
  assert.equal(stillOffline.calls.length, 2, "a persistent negative liveness receipt must still use only one confirmation");
  assert.equal(failedReceipt.liveness_ok, false, "two negative receipts must remain fail-closed");
  assert.equal(failedReceipt.error, "confirmed offline");

  const staleWorkerSession = {
    ok: false,
    liveness_ok: true,
    authenticated: false,
    version_match: true,
    error_code: "agent_session_invalid",
    error: "The local Agent session belongs to another client.",
  };
  const recoveredAuthentication = makeRuntimeMessageReadRuntime([
    staleWorkerSession,
    healthyBridge,
  ], {
    authProbeResponses: [{
      authenticated: true,
      client_kind: "browser_extension",
      session_subject: "a".repeat(32),
      session_extension_version: "4.14.8",
      required_extension_version: "4.14.8",
    }],
  });
  const recoveredAuthenticationReceipt = await recoveredAuthentication.sandbox.dashboardBridgeReceiptRead(7);
  assert.equal(recoveredAuthentication.calls.length, 2, "a stale worker session must receive one final worker confirmation");
  assert.deepEqual(recoveredAuthentication.authCalls, ["/auth/status"], "the live workbench must prime exactly one protected auth receipt");
  assert.equal(recoveredAuthenticationReceipt.authenticated, true);

  const mismatchedRuntime = makeRuntimeMessageReadRuntime([
    { ...staleWorkerSession, version_match: false },
    { ...staleWorkerSession, version_match: false },
  ], {
    authProbeResponses: [{ must_not_be_reached: true }],
  });
  const mismatchedRuntimeReceipt = await mismatchedRuntime.sandbox.dashboardBridgeReceiptRead(7);
  assert.equal(mismatchedRuntimeReceipt.version_match, false);
  assert.equal(mismatchedRuntime.authCalls.length, 0, "a version mismatch must never be treated as a recoverable auth session");

  const recoveredBackground = makeRuntimeMessageReadRuntime([
    errorWithStatus("background worker was reclaimed"),
    { ok: true, dashboard: { fullScan: { status: "idle" } } },
  ], { messageType: "get-dashboard" });
  const recoveredBackgroundData = await recoveredBackground.sandbox.dashboardBackgroundRead(7);
  assert.equal(recoveredBackground.calls.length, 2, "get-dashboard exceptions must be retried exactly once");
  assert.equal(recoveredBackgroundData.dashboard.fullScan.status, "idle");

  const recovered = makeReadRuntime([errorWithStatus("temporary transport loss"), { ok: true }]);
  assert.deepEqual(await recovered.sandbox.dashboardRead("/insights", 7), { ok: true });
  assert.equal(recovered.calls.length, 2, "a transient GET should be retried exactly once");
  assert.equal(recovered.availability.length, 0, "a read-only module failure must not mark the Agent offline");

  const exhausted = makeReadRuntime([
    errorWithStatus("temporary transport loss"),
    errorWithStatus("still unavailable"),
    { must_not_be_reached: true },
  ]);
  await assert.rejects(() => exhausted.sandbox.dashboardRead("/insights", 7), /still unavailable/);
  assert.equal(exhausted.calls.length, 2, "a persistent transient GET must stop after one retry");

  const clientError = makeReadRuntime([errorWithStatus("bad request", 400), { must_not_be_reached: true }]);
  await assert.rejects(() => clientError.sandbox.dashboardRead("/insights", 7), /bad request/);
  assert.equal(clientError.calls.length, 1, "HTTP 400 must not be retried");

  const stale = makeReadRuntime(
    [errorWithStatus("temporary transport loss"), { must_not_be_reached: true }],
    { changeGenerationWhileWaiting: true },
  );
  await assert.rejects(() => stale.sandbox.dashboardRead("/insights", 7), /temporary transport loss/);
  assert.equal(stale.calls.length, 1, "an obsolete dashboard generation must not issue a retry");

  assert.match(script, /const DASHBOARD_READ_TIMEOUT_MS = 5000/);
  const never = new Promise(() => undefined);
  const timedOut = makeReadRuntime([never, never]);
  const timeoutResults = await timedOut.sandbox.runDashboardReadPool([
    () => timedOut.sandbox.dashboardRead("/hung-dashboard-module", 7),
    async () => "neighbor-settled",
  ]);
  assert.equal(timedOut.calls.length, 2, "a hanging GET must time out and retry only once");
  assert.equal(timeoutResults[0].status, "rejected", "the pool must settle a twice-timed-out GET as rejected");
  assert.match(timeoutResults[0].reason?.message || "", /5\s*秒|超过/);
  assert.equal(timeoutResults[1].status, "fulfilled", "a hanging neighbor must not prevent the pool from settling other tasks");
  assert.equal(timeoutResults[1].value, "neighbor-settled");

  await assertDashboardReadPoolContract();
  await assertDashboardConnectionPhaseContract();
  await assertDashboardSingleflightContract();
  await assertAgentAutoRecoveryContract();
  await assertSupplementalReadsCannotHangSingleflight();

  console.log("sidepanel continuity tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
