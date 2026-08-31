const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");

function functionSource(signature) {
  const start = script.indexOf(signature);
  assert.ok(start >= 0, `${signature} should exist`);
  const tail = script.slice(start + signature.length);
  const next = tail.search(/\n(?=(?:async )?function [A-Za-z_$])/);
  return script.slice(start, next < 0 ? script.length : start + signature.length + next);
}

function createElement(tagName) {
  return {
    tagName,
    children: [],
    dataset: {},
    disabled: false,
    textContent: "",
    append(...children) { this.children.push(...children); },
    addEventListener(type, handler) { this[`on${type}`] = handler; },
  };
}

function createHarness(promptResult) {
  const elements = [];
  const requests = [];
  let reloads = 0;
  const sandbox = {
    console,
    selectedStoreKey: "store-selected",
    document: {
      createElement(tagName) {
        const element = createElement(tagName);
        elements.push(element);
        return element;
      },
    },
    window: { prompt() { return promptResult; } },
    markAgentWriteControl() {},
    appendCopyAction() {},
    async bridgeFetch(pathname, options) {
      requests.push({ pathname, options });
      return { ok: true };
    },
    async loadDashboard() { reloads += 1; },
  };
  vm.runInNewContext(functionSource("function planWorkbenchCard(item)"), sandbox, {
    filename: "plan-workbench-task-continuity.vm.js",
  });
  return {
    sandbox,
    elements,
    requests,
    reloadCount: () => reloads,
    render(item) {
      sandbox.planWorkbenchCard(item);
      return elements.find((element) => element.tagName === "button");
    },
  };
}

function observingItem(overrides = {}) {
  return {
    task_id: "canonical-plan-task",
    id: "noncanonical-card-id",
    task_updated_at: "2026-08-30T12:00:00+08:00",
    task_status: "observing",
    plan: "直播计划 A",
    contract_fingerprint: "fallback-fingerprint",
    task_contract: {
      contract_fingerprint: "contract-fingerprint",
      scope: { store_key: "store-from-contract" },
    },
    ...overrides,
  };
}

(async () => {
  for (const promptResult of [undefined, "   "]) {
    const harness = createHarness(promptResult);
    const button = harness.render(observingItem());
    await button.onclick();
    assert.equal(harness.requests.length, 0, "cancelled or blank completion notes must not issue a request");
    assert.equal(harness.reloadCount(), 0, "an aborted completion must not reload the dashboard");
    assert.equal(button.disabled, false, "the task button must be usable after an aborted completion");
  }

  const harness = createHarness("  已复查两小时，ROI 回升；继续按日复盘。  ");
  const button = harness.render(observingItem());
  await button.onclick();

  assert.equal(harness.requests.length, 1);
  assert.equal(harness.requests[0].pathname, "/tasks/update");
  assert.equal(harness.requests[0].options.method, "POST");
  assert.deepEqual(JSON.parse(harness.requests[0].options.body), {
    task_id: "canonical-plan-task",
    status: "done",
    contract_fingerprint: "contract-fingerprint",
    store_key: "store-from-contract",
    note: "已复查两小时，ROI 回升；继续按日复盘。",
  });
  assert.equal(harness.reloadCount(), 1, "a successful transition must preserve dashboard reload behavior");

  const fallbackHarness = createHarness("有结果");
  const fallbackButton = fallbackHarness.render(observingItem({
    task_contract: null,
    store_key: "",
  }));
  await fallbackButton.onclick();
  const fallbackPayload = JSON.parse(fallbackHarness.requests[0].options.body);
  assert.equal(fallbackPayload.contract_fingerprint, "fallback-fingerprint");
  assert.equal(fallbackPayload.store_key, "store-selected");

  console.log("plan workbench task continuity tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
