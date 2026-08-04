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
const row = {
  innerText: "计划ID plan_123 春季止损计划 日预算 500",
  getClientRects: () => [1],
  querySelectorAll: (selector) => selector === "input" ? [input] : selector === "button" ? [submitButton] : [],
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
      sendMessage: async () => ({ ok: true, account: { key: "adacct_test", identity_source: "hmac_qianchuan_advertiser_id" } }),
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
  setTimeout(callback, delay) { if (delay < 1000) callback(); return 1; },
  clearTimeout() {},
  console,
};
context.globalThis = context;
vm.runInNewContext(source, context, { filename: "content-qianchuan.js" });

function send(request, type = "qianchuan-supervised-submit") {
  return new Promise((resolve) => {
    listener({ type, request }, {}, resolve);
  });
}

(async () => {
  const accountKey = "adacct_test"; // Local bridge-resolved HMAC account key.
  const request = {
    operation_type: "adjust_budget",
    mode: "supervised_submit",
    account_key: accountKey,
    plan_id: "plan_123",
    plan_name: "春季止损计划",
    expected_current_value: 500,
    target_value: 400,
  };
  const budgetSubstringMismatch = await send({
    ...request,
    plan_id: "plan_12",
  }, "qianchuan-execution-probe");
  assert.equal(budgetSubstringMismatch.ready, false);
  assert.match(budgetSubstringMismatch.error, /未找到授权计划/);
  assert.equal(input.value, "500");

  const pauseSubstringMismatch = await send({
    operation_type: "pause_plan",
    mode: "supervised_submit",
    account_key: accountKey,
    plan_id: "plan_12",
    plan_name: "春季止损计划",
    expected_current_value: "投放中",
    target_value: "暂停",
  }, "qianchuan-execution-probe");
  assert.equal(pauseSubstringMismatch.ready, false);
  assert.match(pauseSubstringMismatch.error, /未找到授权计划/);

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
  console.log("content-qianchuan executor tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
