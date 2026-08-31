const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");

function sourceBetween(start, end) {
  const from = script.indexOf(start);
  const to = script.indexOf(end, from + start.length);
  assert.ok(from >= 0 && to > from, `unable to extract ${start}`);
  return script.slice(from, to);
}

const policyContext = {};
vm.runInNewContext(`${sourceBetween("function scanRecoveryAction", "async function runScanRecovery")}
${sourceBetween("function observationWindowMinutes", "function taskCard")}
this.policies = { scanRecoveryAction, observationWindowMinutes, observationPrimaryAction };`, policyContext);
const { scanRecoveryAction, observationWindowMinutes, observationPrimaryAction } = policyContext.policies;

assert.match(script, /function observationPrimaryAction\(item = \{\}, nowMs = Date\.now\(\)\)/);
assert.match(script, /label: "查看观察进度"/);
assert.match(script, /label: "到期回读"/);
assert.match(script, /正在观察，不要重复调整/);
assert.match(script, /观察中·禁止重复执行/);
assert.match(script, /observationAction\.due \? "到期回读" : "等待观察"/);
assert.match(script, /观察窗口已结束；先同步同一商品或计划的最新数据，不要重复执行原动作/);
assert.match(script, /observationAction\?\.label \|\| \(next\.status === "doing" \? "继续处理" : "开始处理"\)/);
assert.doesNotMatch(script, /next\.status === "observing"[^\n]*"开始处理"/);
assert.match(script, /command\.state === "today_task_ready"/);
assert.match(script, /kind: "observation"/);
assert.match(script, /if \(selectedAction\.kind === "observation"\)/);
assert.equal(observationWindowMinutes("放量后观察 2–4 小时"), 120);
const observation = {
  status: "observing",
  updated_at: "2026-08-22T10:00:00.000Z",
  observation_window: "观察 2 小时",
  task_contract: { completion_contract: { readback_required: true }, result: { status: "pending" } },
};
assert.equal(observationPrimaryAction(observation, Date.parse("2026-08-22T11:00:00.000Z")).label, "查看观察进度");
assert.equal(observationPrimaryAction(observation, Date.parse("2026-08-22T12:00:00.000Z")).label, "到期回读");
assert.equal(observationPrimaryAction({ ...observation, task_contract: { result: { status: "effective" } } }, 0).due, true);

assert.match(script, /function scanRecoveryAction\(item = \{\}\)/);
for (const [code, label] of [
  ["AGENT_OFFLINE", "修复本地 Agent"],
  ["LOGIN_REQUIRED", "打开千川登录"],
  ["ACCOUNT_UNRESOLVED", "打开对应千川页面"],
  ["ACCOUNT_MISMATCH", "切换回已选账户"],
  ["STORE_MISMATCH", "重新打开并巡店"],
  ["AUTH_EXPIRED", "重新授权"],
  ["READ_PERMISSION_MISSING", "补充读取权限"],
  ["WRITE_PERMISSION_MISSING", "继续只读预演"],
  ["PAGE_CONTRACT_MISSING", "检查商品 ID / 库存列"],
  ["EXECUTION_RESULT_UNKNOWN", "同步当前计划状态"],
  ["READBACK_FAILED", "重新回读"],
  ["VALIDATION_BLOCKED", "补齐具体缺口"],
]) {
  assert.ok(script.includes(`"${code}"`), `missing recovery code ${code}`);
  assert.ok(script.includes(`"${label}"`), `missing recovery label ${label}`);
}
assert.equal(scanRecoveryAction({ error_code: "LOGIN_REQUIRED", source: "qianchuan" }).label, "打开千川登录");
assert.equal(scanRecoveryAction({ error_code: "ACCOUNT_MISMATCH" }).kind, "open_url");
assert.equal(scanRecoveryAction({ error_code: "ACCOUNT_UNRESOLVED" }).label, "打开对应千川页面");
assert.equal(scanRecoveryAction({ error_code: "STORE_MISMATCH" }).label, "重新打开并巡店");
assert.equal(scanRecoveryAction({ error_code: "STORE_MISMATCH" }).kind, "prepare_store");
assert.equal(scanRecoveryAction({ error_code: "NETWORK_TRANSIENT", id: "inventory" }).kind, "retry_page");
assert.equal(scanRecoveryAction({ error: "千川登录已失效，请先完成登录" }).label, "打开千川登录");
assert.equal(scanRecoveryAction({ error: "当前店铺尚未在本地目录中确认" }).label, "重新打开并巡店");
assert.equal(scanRecoveryAction({ error_code: "NETWORK_TRANSIENT" }).kind, "retry_scan");
assert.match(script, /recoveryButton\.dataset\.recoveryCode/);
assert.match(script, /failedRows\.length > 0/);
assert.match(script, /overallRecovery = scan\.status === "error"/);
assert.match(script, /button\.dataset\.overallRecovery === "true"/);
assert.match(script, /请按错误类型完成下方唯一恢复动作/);

console.log("sidepanel unique next-action UI tests passed");
