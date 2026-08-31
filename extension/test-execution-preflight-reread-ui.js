const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const sidepanel = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");
const handlerStart = sidepanel.indexOf('document.getElementById("preflight-reread").addEventListener');
const handlerEnd = sidepanel.indexOf('\ndocument.getElementById("preflight-stop").addEventListener', handlerStart);
assert.ok(handlerStart >= 0 && handlerEnd > handlerStart, "preflight reread handler should be extractable");
const handlerSource = sidepanel.slice(handlerStart, handlerEnd);

let clickHandler = null;
let refreshCount = 0;
const messages = [];
const rereadButton = {
  disabled: false,
  textContent: "读取当前千川页并复核",
  addEventListener(type, callback) {
    assert.equal(type, "click");
    clickHandler = callback;
  },
};
const nextLabel = { textContent: "" };
const context = {
  currentPreflightState: "waiting_reread",
  currentPreflightSession: {
    action_id: "a".repeat(24),
    started_at_ms: 1_725_000_123_456,
  },
  currentPreflightAction: {
    page_type: "qianchuan_campaigns",
    account_key: "account-a",
    store_key: "store-a",
  },
  selectedQianchuanAccount: "fallback-account",
  selectedStoreKey: "fallback-store",
  document: {
    getElementById(id) {
      if (id === "preflight-reread") return rereadButton;
      if (id === "preflight-next") return nextLabel;
      throw new Error(`unexpected element: ${id}`);
    },
  },
  chrome: {
    runtime: {
      sendMessage: async (message) => {
        messages.push(message);
        return { ok: true, page_type: "campaigns" };
      },
    },
  },
  refreshExecutionPreflight: async () => { refreshCount += 1; },
  setTimeout: (callback) => { callback(); return 1; },
};
context.globalThis = context;
vm.runInNewContext(handlerSource, context, { filename: "execution-preflight-reread-ui.vm.js" });

function renderControlsFor(state, actionOverrides = {}, reportOverrides = {}) {
  const runtimeMessages = [];
  let refreshes = 0;
  const fakeNode = (tagName) => ({
    tagName,
    children: [],
    dataset: {},
    append(...children) { this.children.push(...children); },
    addEventListener(type, callback) { this[`on${type}`] = callback; },
  });
  const elements = {
    "execution-preflight": { hidden: false, className: "" },
    "preflight-state": { textContent: "" },
    "preflight-target": { textContent: "" },
    "preflight-checks": { replaceChildren() {} },
    "preflight-next": { textContent: "" },
    "archived-readback-hub": { hidden: true },
    "archived-readback-count": { textContent: "" },
    "archived-readback-list": { children: [], replaceChildren(...children) { this.children = children; } },
    "preflight-reread": { hidden: true, disabled: false },
    "preflight-authorize": { hidden: true, dataset: {} },
    "preflight-archive": { hidden: true, disabled: false, dataset: {} },
    "preflight-stop": { disabled: false },
  };
  const renderStart = sidepanel.indexOf("function renderExecutionPreflight");
  const renderEnd = sidepanel.indexOf("\nfunction renderExecutionEffectiveness", renderStart);
  assert.ok(renderStart >= 0 && renderEnd > renderStart);
  const renderContext = {
    currentPreflightState: "idle",
    currentPreflightSession: null,
    currentPreflightAction: null,
    document: {
      getElementById: (id) => elements[id],
      querySelectorAll: () => [],
      createElement: fakeNode,
    },
    DianConnectionGuidePolicy: { automationStep: () => "result" },
    chrome: {
      runtime: {
        sendMessage: async (message) => {
          runtimeMessages.push(message);
          return { ok: true, verification: { verified: false } };
        },
      },
    },
    refreshExecutionPreflight: async () => { refreshes += 1; },
  };
  renderContext.globalThis = renderContext;
  vm.runInNewContext(
    `${sidepanel.slice(renderStart, renderEnd)}\nglobalThis.__renderExecutionPreflight = renderExecutionPreflight;`,
    renderContext,
    { filename: "execution-preflight-state.vm.js" },
  );
  renderContext.__renderExecutionPreflight({
    state,
    session: { session_id: "session-1", action_id: "a".repeat(24) },
    action: { plan_name: "测试计划", operation_type: "adjust_budget", ...actionOverrides },
    checks: [],
    ...reportOverrides,
  });
  elements.__runtimeMessages = runtimeMessages;
  elements.__refreshes = () => refreshes;
  return elements;
}

async function runAuthorizeCase(executionResponse, refreshedState) {
  let authorizeHandler = null;
  let refreshes = 0;
  const runtimeMessages = [];
  const authorizeButton = {
    disabled: false,
    dataset: { confirmationText: "确认执行" },
    addEventListener(type, callback) {
      assert.equal(type, "click");
      authorizeHandler = callback;
    },
  };
  const status = { textContent: "" };
  const authorizeStart = sidepanel.indexOf('document.getElementById("preflight-authorize").addEventListener');
  const authorizeEnd = sidepanel.indexOf('\ndocument.getElementById("full-scan-button").addEventListener', authorizeStart);
  assert.ok(authorizeStart >= 0 && authorizeEnd > authorizeStart);
  const authorizeContext = {
    currentPreflightState: "ready_for_final_confirmation",
    currentPreflightSession: { session_id: "session-1" },
    document: {
      getElementById(id) {
        if (id === "preflight-authorize") return authorizeButton;
        if (id === "preflight-next") return status;
        throw new Error(`unexpected element: ${id}`);
      },
    },
    window: { prompt: () => "确认执行" },
    bridgeFetch: async () => ({
      preflight: { session: { authorization_id: "b".repeat(32) } },
    }),
    renderExecutionPreflight: () => undefined,
    chrome: {
      runtime: {
        sendMessage: async (message) => {
          runtimeMessages.push(message);
          if (executionResponse instanceof Error) throw executionResponse;
          return executionResponse;
        },
      },
    },
    refreshExecutionPreflight: async () => {
      refreshes += 1;
      authorizeContext.currentPreflightState = refreshedState;
    },
  };
  authorizeContext.globalThis = authorizeContext;
  vm.runInNewContext(sidepanel.slice(authorizeStart, authorizeEnd), authorizeContext, {
    filename: "execution-preflight-authorize.vm.js",
  });
  await authorizeHandler({ currentTarget: authorizeButton });
  return { refreshes, runtimeMessages, authorizeButton, status };
}

(async () => {
  assert.equal(typeof clickHandler, "function");
  await clickHandler({ currentTarget: rereadButton });
  assert.equal(messages.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(messages[0])), {
    type: "sync-current-qianchuan",
    expected_account_key: "account-a",
    expected_store_key: "store-a",
    expected_page_types: ["campaigns"],
    purpose: "execution_preflight_reread",
    require_hard_reload: true,
    session_started_at_ms: 1_725_000_123_456,
  });
  assert.equal(refreshCount, 1);
  assert.equal(rereadButton.disabled, false);
  assert.equal(rereadButton.textContent, "读取当前千川页并复核");

  context.currentPreflightState = "manual_reconcile_required";
  await clickHandler({ currentTarget: rereadButton });
  assert.deepEqual(JSON.parse(JSON.stringify(messages[1])), {
    type: "manual-execution-readback",
    action_id: "a".repeat(24),
  }, "manual reconcile must use read-only reconstruction/reread instead of the initial sync path");
  assert.equal(refreshCount, 2);

  for (const state of ["authorization_consumed", "manual_reconcile_required", "manual_reconcile_archived"]) {
    const controls = renderControlsFor(state, {
      manual_reconcile_archivable: state === "manual_reconcile_required",
      archive_confirmation_text: "确认归档未决动作 aaaaaaaa 并永久禁止该计划自动重投",
    });
    assert.equal(controls["preflight-reread"].hidden, false, `${state} must expose the independent reread action`);
    assert.equal(controls["preflight-stop"].disabled, true, `${state} must disable emergency stop after authorization consumption`);
    assert.equal(
      controls["preflight-archive"].hidden,
      state !== "manual_reconcile_required",
      "only an unresolved, unarchived action may expose the archive control",
    );
  }

  {
    const archivedActionId = "c".repeat(24);
    const controls = renderControlsFor("awaiting_reread", {}, {
      recoverable_readback_actions: [{
        action_id: archivedActionId,
        plan_id: "plan-archived",
        plan_name: "历史归档计划",
        account_key: "account-archived",
      }],
    });
    assert.equal(controls["archived-readback-hub"].hidden, false);
    assert.equal(controls["archived-readback-count"].textContent, "1 项");
    const row = controls["archived-readback-list"].children[0];
    const retryButton = row.children[1];
    assert.equal(retryButton.textContent, "继续只读核验");
    await retryButton.onclick();
    assert.deepEqual(JSON.parse(JSON.stringify(controls.__runtimeMessages)), [{
      type: "manual-execution-readback",
      action_id: archivedActionId,
    }]);
    assert.equal(
      controls.__runtimeMessages.some((message) => message.type === "run-authorized-execution"),
      false,
      "retrying an archived readback must never dispatch an authorized ad operation",
    );
    assert.equal(controls.__refreshes(), 1);
    assert.match(controls["preflight-next"].textContent, /未触发任何投放操作/);
    assert.equal(retryButton.disabled, false, "the archived readback retry should become available after refresh");
    assert.equal(retryButton.textContent, "继续只读核验");
  }

  {
    const archiveStart = sidepanel.indexOf('document.getElementById("preflight-archive").addEventListener');
    const archiveEnd = sidepanel.indexOf('\ndocument.getElementById("preflight-authorize").addEventListener', archiveStart);
    assert.ok(archiveStart >= 0 && archiveEnd > archiveStart, "manual reconcile archive handler should be extractable");
    let archiveHandler = null;
    const archiveRequests = [];
    const archiveButton = {
      disabled: false,
      textContent: "归档未决动作",
      dataset: { confirmationText: "确认归档未决动作 aaaaaaaa 并永久禁止该计划自动重投" },
      addEventListener(type, callback) { assert.equal(type, "click"); archiveHandler = callback; },
    };
    const archiveStatus = { textContent: "" };
    const archiveContext = {
      currentPreflightSession: { action_id: "a".repeat(24) },
      document: {
        getElementById(id) {
          if (id === "preflight-archive") return archiveButton;
          if (id === "preflight-next") return archiveStatus;
          throw new Error(`unexpected archive element: ${id}`);
        },
      },
      window: { prompt: () => archiveButton.dataset.confirmationText },
      bridgeFetch: async (url, init) => {
        archiveRequests.push({ url, body: JSON.parse(init.body) });
        return { preflight: { state: "manual_reconcile_archived" } };
      },
      renderExecutionPreflight: () => undefined,
    };
    archiveContext.globalThis = archiveContext;
    vm.runInNewContext(sidepanel.slice(archiveStart, archiveEnd), archiveContext, {
      filename: "execution-preflight-archive.vm.js",
    });
    await archiveHandler({ currentTarget: archiveButton });
    assert.deepEqual(JSON.parse(JSON.stringify(archiveRequests)), [{
      url: "/actions/preflight/manual-reconcile/archive",
      body: {
        action_id: "a".repeat(24),
        confirmation_text: "确认归档未决动作 aaaaaaaa 并永久禁止该计划自动重投",
        resolution_note: "用户在工作台显式归档；平台结果仍未知。",
      },
    }]);
    assert.equal(archiveButton.textContent, "归档未决动作");
  }

  const cases = [
    {
      label: "verified",
      response: { ok: true, result: { verification: { verified: true }, readback_job: { status: "verified" } } },
      refreshedState: "completed",
    },
    {
      label: "uncertain",
      response: { ok: true, result: { verification: { verified: false }, readback_job: { status: "uncertain" } } },
      refreshedState: "manual_reconcile_required",
    },
    {
      label: "error",
      response: { ok: false, error: "mock execution failed" },
      refreshedState: "manual_reconcile_required",
    },
  ];
  for (const testCase of cases) {
    const outcome = await runAuthorizeCase(testCase.response, testCase.refreshedState);
    assert.equal(outcome.refreshes, 1, `${testCase.label} execution must refresh canonical preflight state in finally`);
    assert.deepEqual(JSON.parse(JSON.stringify(outcome.runtimeMessages)), [{
      type: "run-authorized-execution",
      authorization_id: "b".repeat(32),
    }]);
  }
  console.log("execution preflight reread UI tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
