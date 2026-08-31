const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = __dirname;
const popupHtml = fs.readFileSync(path.join(root, "popup.html"), "utf8");
const popupJs = fs.readFileSync(path.join(root, "popup.js"), "utf8");
const background = fs.readFileSync(path.join(root, "background.js"), "utf8");
const sidepanel = fs.readFileSync(path.join(root, "sidepanel.js"), "utf8");
const welcomeHtml = fs.readFileSync(path.join(root, "welcome.html"), "utf8");
const welcomeJs = fs.readFileSync(path.join(root, "welcome.js"), "utf8");

assert.match(popupHtml, /id="repair-agent-button"[\s\S]*修复 Agent/);
assert.match(popupJs, /type: "repair-agent"/);
assert.match(background, /message\.type === "repair-agent"/);
assert.match(background, /welcome\.html#repair-agent/);
assert.doesNotMatch(background, /repair-agent"[\s\S]{0,300}fetch\(/, "修复入口不应伪装成浏览器能启动本机程序");
assert.match(welcomeHtml, /id="repair-agent-guide"[\s\S]*repair-agent-instruction/);
assert.match(welcomeJs, /location\.hash === "#repair-agent"/);
assert.match(welcomeJs, /function setRepairGuideVisible\(visible\)[\s\S]*guide\.hidden = !visible/);
assert.match(
  welcomeJs,
  /renderRepairInstruction\(health\.platform\);\s*setRepairGuideVisible\(location\.hash === "#repair-agent"\)/,
  "Agent 恢复后应隐藏修复卡；显式 repair hash 除外",
);
assert.match(
  welcomeJs,
  /catch(?: \(error\))? \{\s*setRepairGuideVisible\(true\);/,
  "健康检查失败时应立即展示修复卡",
);
assert.match(welcomeJs, /repair-agent-recheck/);
assert.match(welcomeJs, /repair_dian_agent\.command/);
assert.match(popupJs, /Agent 已运行，扩展未配对/);
assert.match(popupJs, /重新加载.*Repair Dian Agent/);
assert.match(popupJs, /open_extension_manager/);
assert.match(background, /open-extension-manager/);
assert.match(background, /chrome:\/\/extensions\//);
assert.match(background, /authenticated:\s*false/);
assert.match(background, /agent_extension_version_mismatch/);
assert.match(sidepanel, /function dashboardBridgeFailure/);
assert.match(sidepanel, /function dashboardBridgeReceiptFailure/);
assert.match(sidepanel, /Agent 与扩展版本不一致/);
assert.match(sidepanel, /Agent 已运行，扩展未配对/);
const bridgeReceiptFailureStart = sidepanel.indexOf("function dashboardBridgeReceiptFailure");
const bridgeReceiptFailureEnd = sidepanel.indexOf("function dashboardBridgeAccepted", bridgeReceiptFailureStart);
const bridgeReceiptFailureSandbox = {};
vm.runInNewContext(sidepanel.slice(bridgeReceiptFailureStart, bridgeReceiptFailureEnd), bridgeReceiptFailureSandbox);
assert.equal(
  bridgeReceiptFailureSandbox.dashboardBridgeReceiptFailure(
    { status: "fulfilled" },
    { ok: true, liveness_ok: true, authenticated: true, version_match: true },
  ),
  null,
);
const receiptFailure = bridgeReceiptFailureSandbox.dashboardBridgeReceiptFailure(
  { status: "rejected", reason: new Error("service worker stopped") },
  {},
);
assert.equal(receiptFailure.action, "open_extension_manager");
assert.equal(receiptFailure.title, "浏览器扩展后台不可用");
assert.match(receiptFailure.detail, /重新加载店策 Agent/);
assert.doesNotMatch(receiptFailure.detail, /本地 Agent 未启动/);
const failureFunctionStart = sidepanel.indexOf("function dashboardBridgeReceiptFailure");
const failureFunctionEnd = sidepanel.indexOf("function applyExperienceMode", failureFunctionStart);
const failureSandbox = {
  dashboardBridgeAuthenticationRecoverable(bridge = {}) {
    return bridge.liveness_ok === true
      && bridge.version_match === true
      && bridge.authenticated !== true
      && !["extension_origin_not_trusted", "extension_pairing_not_authorized"].includes(String(bridge.error_code || ""));
  },
};
vm.runInNewContext(sidepanel.slice(failureFunctionStart, failureFunctionEnd), failureSandbox);
assert.equal(
  failureSandbox.dashboardBridgeFailure({ liveness_ok: true, authenticated: true, version_match: false }).action,
  "open_extension_manager",
);
assert.equal(
  failureSandbox.dashboardBridgeFailure({ liveness_ok: true, authenticated: false, version_match: true }).action,
  "retry_connection",
);
assert.equal(
  failureSandbox.dashboardBridgeFailure({ liveness_ok: true, authenticated: false, version_match: true, error_code: "extension_origin_not_trusted" }).action,
  "repair_agent",
);
assert.equal(failureSandbox.dashboardBridgeFailure({ liveness_ok: false }).action, "repair_agent");
const blockReasonStart = sidepanel.indexOf("function agentWriteBlockReason");
const blockReasonEnd = sidepanel.indexOf("function markAgentWriteControl", blockReasonStart);
const blockReasonSandbox = { DEFAULT_AGENT_WRITE_BLOCK_MESSAGE: "本地 Agent 未连接，写入与执行已冻结；请先修复本地 Agent。" };
vm.runInNewContext(sidepanel.slice(blockReasonStart, blockReasonEnd), blockReasonSandbox);
const mismatchBlockReason = blockReasonSandbox.agentWriteBlockReason(
  failureSandbox.dashboardBridgeFailure({ liveness_ok: true, authenticated: true, version_match: false }),
);
assert.match(mismatchBlockReason, /版本不一致/);
assert.match(mismatchBlockReason, /扩展管理页重新加载/);
assert.doesNotMatch(mismatchBlockReason, /Agent 未连接/);
const recoveringBlockReason = blockReasonSandbox.agentWriteBlockReason(
  failureSandbox.dashboardBridgeFailure({ liveness_ok: true, authenticated: false, version_match: true }),
);
assert.match(recoveringBlockReason, /安全会话/);
assert.match(recoveringBlockReason, /自动复核/);
assert.doesNotMatch(recoveringBlockReason, /Agent 未连接/);
const unpairedBlockReason = blockReasonSandbox.agentWriteBlockReason(
  failureSandbox.dashboardBridgeFailure({ liveness_ok: true, authenticated: false, version_match: true, error_code: "extension_origin_not_trusted" }),
);
assert.match(unpairedBlockReason, /Agent 未连接/);
assert.match(
  blockReasonSandbox.agentWriteBlockReason(failureSandbox.dashboardBridgeFailure({ liveness_ok: false })),
  /本地 Agent 未连接/,
);
assert.match(sidepanel, /control\.title = currentAgentWriteBlockMessage\(\)/);
assert.match(sidepanel, /throw new Error\(currentAgentWriteBlockMessage\(\)\)/);
assert.match(
  sidepanel,
  /workspace-agent-label"\)\.textContent = ok \? "本地 Agent 已连接" : title/,
  "顶部状态必须沿用真实失败标题，不能把在线未配对或版本错配统一说成 Agent 未连接",
);
assert.match(sidepanel, /action === "open_extension_manager"[\s\S]{0,300}type: "open-extension-manager"/);

const dashboardStart = sidepanel.indexOf("async function loadDashboardOnce()");
const dashboardEnd = sidepanel.indexOf("\nfunction startDashboardLoad", dashboardStart);
assert.ok(dashboardStart >= 0 && dashboardEnd > dashboardStart, "single dashboard load function should exist");
const dashboardSource = sidepanel.slice(dashboardStart, dashboardEnd);

function fakeNode() {
  return {
    hidden: false,
    textContent: "",
    className: "",
    dataset: {},
    style: { setProperty() {} },
    classList: { add() {}, remove() {}, toggle() {} },
    querySelector() { return fakeNode(); },
    querySelectorAll() { return []; },
    replaceChildren() {},
    setAttribute() {},
    removeAttribute() {},
    focus() {},
  };
}

async function runDashboardConnectionCase(bridge, { rejectInsights = false, rejectBackground = false } = {}) {
  const nodes = new Map();
  const connectionCalls = [];
  const journeyCalls = [];
  const renderOrder = [];
  const renderNames = [
    "renderQianchuanAccounts", "renderSimpleAgentError", "renderOperationContext", "renderOnboarding",
    "renderPlans", "renderInventory", "renderOperations", "renderTodayFocus", "renderAlerts", "renderCoverage",
    "renderSettings", "renderIntegrations", "renderOceanEngineOAuth", "renderOceanEngineSync",
    "renderOceanEngineAccountCenter", "renderPromotionPlanConsole", "renderPromotionOperationLog",
    "renderConnectionGuide", "renderFullScan", "renderTrends", "renderHealthMonitor", "renderEffectiveness",
    "renderAutomationReadiness", "renderStopLossQueue", "renderStrategySimulation", "renderExecutionPreflight",
    "renderShadowExecution", "renderExecutionEffectiveness", "renderValueLedger", "renderChengfangReadiness",
    "renderChengfangUnavailable", "renderChengfangTrialConsole", "renderAutopilotCenter", "renderControlTaskCenter",
    "renderScheduleControl",
  ];
  const sandbox = {
    console,
    CHECKING_AGENT_WRITE_BLOCK_MESSAGE: "checking",
    dashboardLoadGeneration: 0,
    agentConnectionState: "checking",
    agentWriteBlockMessage: "",
    currentDashboardFailures: [],
    currentExtensionSettings: {},
    currentChengfangA2Pilot: null,
    currentControlTaskSummary: null,
    currentChengfangAgentRuntime: null,
    latestBrief: "",
    dashboardBridgeAuthenticationRecoverable(candidate = {}) {
      return candidate.liveness_ok === true
        && candidate.version_match === true
        && candidate.authenticated !== true
        && !["extension_origin_not_trusted", "extension_pairing_not_authorized"].includes(String(candidate.error_code || ""));
    },
    document: {
      activeElement: null,
      getElementById(id) {
        if (!nodes.has(id)) nodes.set(id, fakeNode());
        return nodes.get(id);
      },
    },
    chrome: {
      runtime: {
        async sendMessage() { return {}; },
      },
    },
    async dashboardRead(pathname) {
      if (pathname === "/insights" && rejectInsights) throw new Error("insights temporary failure");
      return {};
    },
    async dashboardBridgeReceiptRead() {
      return bridge;
    },
    async dashboardBackgroundRead() {
      if (rejectBackground) throw new Error("get-dashboard unavailable after retry");
      return { ok: true, dashboard: { settings: {}, fullScan: {} } };
    },
    async runDashboardReadPool(tasks) {
      return Promise.all(tasks.map(async (task) => {
        try {
          return { status: "fulfilled", value: await task() };
        } catch (reason) {
          return { status: "rejected", reason };
        }
      }));
    },
    dashboardLoadIsCurrent() { return true; },
    registerAgentWriteControls() {},
    showLoadingSkeleton() {},
    hideLoadingSkeleton() {},
    reconcileQianchuanScope() { return { accountKey: "" }; },
    renderProductCapability() { renderOrder.push("renderProductCapability"); return {}; },
    renderJourneyCommand(input) { renderOrder.push("renderJourneyCommand"); journeyCalls.push(input); },
    renderConnection(ok, title, detail, recovery) {
      renderOrder.push(`renderConnection:${ok}`);
      connectionCalls.push({ ok, title, detail, recovery });
    },
    async loadSystemStatus() {},
    async loadAiCenter() {},
    async loadOperatorMemory() {},
    async maybeReturnFromTargetedScan() {},
  };
  for (const name of renderNames) sandbox[name] = () => { renderOrder.push(name); };
  vm.runInNewContext(
    `${sidepanel.slice(bridgeReceiptFailureStart, failureFunctionEnd)}\n${dashboardSource}`,
    sandbox,
    { filename: "sidepanel-dashboard-connection.vm.js" },
  );
  await sandbox.loadDashboardOnce();
  return { connectionCalls, journeyCalls, nodes, renderOrder };
}

(async () => {
  const acceptedBridge = {
    ok: true,
    liveness_ok: true,
    authenticated: true,
    version_match: true,
  };
  const degraded = await runDashboardConnectionCase(acceptedBridge, { rejectInsights: true });
  assert.equal(degraded.connectionCalls.at(-1)?.ok, true, "an insights-only failure must not mark a verified Agent offline");
  assert.equal(degraded.journeyCalls.at(-1)?.online, true, "an insights-only failure must not make the journey offline");
  assert.equal(degraded.nodes.get("dashboard-load-warning")?.hidden, false, "the failed module must remain visible as a load warning");
  assert.match(degraded.nodes.get("dashboard-load-warning-detail")?.textContent || "", /经营建议/);
  assert.equal(
    degraded.renderOrder[0],
    "renderConnection:true",
    "a strict positive bridge verdict must reopen the global gate before any business renderer restores its own disabled state",
  );
  for (const businessRenderer of [
    "renderQianchuanAccounts", "renderProductCapability", "renderJourneyCommand", "renderPlans", "renderInventory",
    "renderOperations", "renderSettings", "renderPromotionPlanConsole", "renderConnectionGuide",
    "renderAutomationReadiness", "renderExecutionPreflight", "renderControlTaskCenter", "renderScheduleControl",
  ]) {
    const businessIndex = degraded.renderOrder.indexOf(businessRenderer);
    assert.ok(businessIndex > 0, `${businessRenderer} must run only after renderConnection(true)`);
  }

  const backgroundDegraded = await runDashboardConnectionCase(acceptedBridge, { rejectBackground: true });
  assert.equal(backgroundDegraded.connectionCalls.at(-1)?.ok, true, "get-dashboard failure must not override a complete bridge receipt");
  assert.equal(backgroundDegraded.journeyCalls.at(-1)?.online, true, "get-dashboard failure must remain a data-module degradation");
  assert.equal(backgroundDegraded.nodes.get("dashboard-load-warning")?.hidden, false);
  assert.match(
    backgroundDegraded.nodes.get("dashboard-load-warning-detail")?.textContent || "",
    /浏览器状态/,
    "get-dashboard failure must be listed as a failed module instead of an Agent outage",
  );

  for (const field of ["ok", "liveness_ok", "authenticated", "version_match"]) {
    for (const mode of ["false", "missing"]) {
      const rejectedBridge = { ...acceptedBridge };
      if (mode === "false") rejectedBridge[field] = false;
      else delete rejectedBridge[field];
      const rejected = await runDashboardConnectionCase(rejectedBridge);
      assert.equal(rejected.connectionCalls.at(-1)?.ok, false, `bridge.${field} ${mode} must fail closed`);
      assert.equal(rejected.journeyCalls.at(-1)?.online, false, `bridge.${field} ${mode} must keep the journey offline`);
    }
  }

  console.log("agent repair entry tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
