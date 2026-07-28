const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(require.resolve("./scan-policy.js"), "utf8");
const context = { globalThis: null };
context.globalThis = context;
vm.runInNewContext(source, context, { filename: "scan-policy.js" });

const policy = context.DianAgentScanPolicy;
assert.ok(policy);

assert.strictEqual(
  policy.matchAccount(
    { key: "acct_platform_b", label: "同名旗舰店", identity_source: "platform_id" },
    { key: "acct_platform_a", label: "同名旗舰店", identity_source: "platform_id" },
  ).code,
  "ACCOUNT_MISMATCH",
);

assert.strictEqual(
  policy.matchAccount(
    { key: "acct_platform_a", label: "同名旗舰店", identity_source: "platform_id" },
    { key: "acct_label", label: "同名旗舰店", identity_source: "account_label" },
  ).ok,
  true,
);

const unresolved = new Error("未识别当前千川账号。巡检已停止刷新，请确认账号后重试。");
assert.strictEqual(policy.isNonRetryable(unresolved), true);
assert.strictEqual(policy.errorCode(unresolved), "ACCOUNT_UNRESOLVED");
assert.strictEqual(policy.isNonRetryable(new Error("页面加载超时")), false);

console.log("scan-policy tests passed");
