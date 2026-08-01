const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(require.resolve("./content-qianchuan.js"), "utf8");
let listener;

class MockInput {
  constructor(value) {
    this._value = value;
    this.disabled = false;
    this.style = {};
  }
  get value() { return this._value; }
  set value(next) { this._value = next; }
  getClientRects() { return [1]; }
  getAttribute(name) { return name === "aria-label" ? "日预算" : ""; }
  dispatchEvent() {}
  focus() {}
  scrollIntoView() {}
  closest() { return row; }
}

const input = new MockInput("500");
let submitted = false;
const submitButton = {
  innerText: "保存",
  textContent: "保存",
  disabled: false,
  getClientRects: () => [1],
  getAttribute: () => null,
  click() { submitted = true; },
};
const pauseButton = {
  innerText: "暂停",
  textContent: "暂停",
  disabled: false,
  getClientRects: () => [1],
  getAttribute: () => null,
  click() {
    submitted = true;
    row.innerText = "计划ID plan_123 春季止损计划 日预算 500 已暂停";
  },
};
const row = {
  innerText: "计划ID plan_123 春季止损计划 日预算 500 投放中",
  getClientRects: () => [1],
  querySelectorAll: (selector) => {
    if (selector === "input") return [input];
    if (selector === "button") return [submitButton, pauseButton];
    if (selector.includes("button") || selector.includes("switch") || selector.includes("checkbox")) {
      return [submitButton, pauseButton];
    }
    return [];
  },
};
const context = {
  URLSearchParams,
  location: {
    href: "https://qianchuan.jinritemai.com/uni-prom?advertiser_id=12345678",
    pathname: "/uni-prom",
    search: "?advertiser_id=12345678",
    hash: "",
    hostname: "qianchuan.jinritemai.com",
  },
  document: {
    title: "巨量千川",
    body: { innerText: "春季止损计划" },
    documentElement: {},
    querySelectorAll(selector) {
      if (selector.includes("table-row") || selector.startsWith("tr,")) return [row];
      if (selector.includes("shopName")) return [{ innerText: "测试店铺", getClientRects: () => [1] }];
      if (selector.includes("[role='alert']") && submitted) {
        return [{ innerText: "修改成功", getClientRects: () => [1] }];
      }
      return [];
    },
  },
  chrome: {
    storage: { local: { get: async () => ({ settings: { privacyMode: true } }) } },
    runtime: {
      onMessage: { addListener(callback) { listener = callback; } },
      sendMessage: async () => ({ ok: true }),
    },
  },
  sessionStorage: { length: 0, key: () => null, getItem: () => null },
  localStorage: { length: 0, key: () => null, getItem: () => null },
  DianAgentExtractor: {
    collect: async () => ({ quality: { score: 90 } }),
    pseudonymizePlanIdentifier: (value) => value,
  },
  HTMLInputElement: MockInput,
  Event: class { constructor(type) { this.type = type; } },
  MutationObserver: class { observe() {} },
  Date: { now: () => fakeNow },
  setTimeout(callback, delay = 0) {
    fakeNow += Number(delay) || 0;
    callback();
    return 1;
  },
  clearTimeout() {},
  console,
};
let fakeNow = 1_700_000_000_000;
context.globalThis = context;
vm.runInNewContext(source, context, { filename: "content-qianchuan.js" });

function send(request, type = "qianchuan-supervised-submit") {
  return new Promise((resolve) => {
    listener({ type, request }, {}, resolve);
  });
}

(async () => {
  const accountKey = "acct_0aa8abcd"; // FNV-1a hash used by the content script for advertiser_id 12345678.
  const request = {
    operation_type: "adjust_budget",
    mode: "supervised_submit",
    account_key: accountKey,
    plan_id: "plan_123",
    plan_name: "春季止损计划",
    expected_current_value: 500,
    target_value: 400,
  };
  const probe = await send(request, "qianchuan-execution-probe");
  assert.equal(probe.ready, true);
  assert.equal(input.value, "500");
  const result = await send(request);
  assert.equal(result.ok, true);
  assert.equal(result.submitted, true);
  assert.equal(result.platform_success_observed, true);
  assert.equal(input.value, "400");

  input.value = "450";
  const mismatch = await send({
    operation_type: "adjust_budget",
    mode: "supervised_submit",
    account_key: accountKey,
    plan_id: "plan_123",
    plan_name: "春季止损计划",
    expected_current_value: 500,
    target_value: 400,
  });
  assert.equal(mismatch.ok, false);
  assert.match(mismatch.error, /当前预算一致/);
  assert.equal(input.value, "450");

  submitted = false;
  const pauseRequest = {
    operation_type: "pause_plan",
    mode: "supervised_submit",
    account_key: accountKey,
    plan_id: "plan_123",
    plan_name: "春季止损计划",
    expected_current_value: "投放中",
    target_value: "暂停",
  };
  const pauseProbe = await send(pauseRequest, "qianchuan-execution-probe");
  assert.equal(pauseProbe.ready, true);
  const pauseResult = await send(pauseRequest);
  assert.equal(pauseResult.ok, true);
  assert.equal(pauseResult.submitted, true);
  assert.equal(pauseResult.platform_success_observed, true);
  assert.equal(pauseResult.target_value, "暂停");

  // Bare「暂停」button label must not count as a paused status.
  submitted = false;
  row.innerText = "计划ID plan_123 春季止损计划 日预算 500 投放中 暂停";
  pauseButton.click = () => { submitted = true; /* status unchanged */ };
  const falsePause = await send(pauseRequest);
  assert.equal(falsePause.ok, false);
  assert.equal(falsePause.platform_mutation_attempted, true);
  assert.match(falsePause.error, /未读取到成功回执/);

  // Restore pause button success path for later cases.
  pauseButton.click = () => {
    submitted = true;
    row.innerText = "计划ID plan_123 春季止损计划 日预算 500 已暂停";
  };
  row.innerText = "计划ID plan_123 春季止损计划 日预算 500 投放中";
  submitted = false;
  const pauseButton2 = {
    innerText: "停用",
    textContent: "停用",
    disabled: false,
    getClientRects: () => [1],
    getAttribute: () => null,
    click() {},
  };
  row.querySelectorAll = (selector) => {
    if (selector === "input") return [input];
    if (selector === "button") return [submitButton, pauseButton, pauseButton2];
    if (selector.includes("button") || selector.includes("switch") || selector.includes("checkbox")) {
      return [submitButton, pauseButton, pauseButton2];
    }
    return [];
  };
  const dualPause = await send(pauseRequest, "qianchuan-execution-probe");
  assert.equal(dualPause.ok, false);
  assert.match(dualPause.error, /多个暂停/);

  // 「取消暂停」must not be treated as a pause control.
  row.innerText = "计划ID plan_123 春季止损计划 日预算 500 投放中";
  const cancelPause = {
    innerText: "取消暂停",
    textContent: "取消暂停",
    disabled: false,
    getClientRects: () => [1],
    getAttribute: () => null,
    click() {},
  };
  row.querySelectorAll = (selector) => {
    if (selector === "input") return [input];
    if (selector === "button") return [submitButton, cancelPause];
    if (selector.includes("button") || selector.includes("switch") || selector.includes("checkbox")) {
      return [submitButton, cancelPause];
    }
    return [];
  };
  const cancelOnly = await send(pauseRequest, "qianchuan-execution-probe");
  assert.equal(cancelOnly.ok, false);
  assert.match(cancelOnly.error, /未找到唯一暂停/);

  // Negated active wording must not pass the status gate.
  row.innerText = "计划ID plan_123 春季止损计划 日预算 500 未启用";
  row.querySelectorAll = (selector) => {
    if (selector === "input") return [input];
    if (selector === "button") return [submitButton, pauseButton];
    if (selector.includes("button") || selector.includes("switch") || selector.includes("checkbox")) {
      return [submitButton, pauseButton];
    }
    return [];
  };
  const negated = await send({
    ...pauseRequest,
    expected_current_value: "启用",
  }, "qianchuan-execution-probe");
  assert.equal(negated.ok, false);
  assert.match(negated.error, /投放状态与授权不一致|授权缺少已投放状态/);

  console.log("content-qianchuan executor tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
