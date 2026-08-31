const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = __dirname;
const html = fs.readFileSync(path.join(root, "sidepanel.html"), "utf8");
const js = fs.readFileSync(path.join(root, "sidepanel.js"), "utf8");

assert.match(html, /id="chengfang-production-write"/);
assert.match(html, /只允许单个乘方计划降低预算/);
assert.match(html, /预检不会写入/);
assert.match(html, /精确确认词/);
assert.match(html, /一次性短时授权/);
assert.match(html, /结果未知：生产写入已冻结/);
assert.match(html, /不要再次执行/);
assert.match(html, /无网页点击兜底/);
assert.match(html, /id="chengfang-production-cancel"/);
assert.match(html, /id="chengfang-production-stop"/);
assert.match(html, /id="chengfang-production-resume"/);
assert.match(html, /id="chengfang-production-readiness-steps"/);
assert.match(html, /id="chengfang-production-next"/);
assert.match(html, /当前店铺|完成 5 项准备/);
assert.match(html, /data-workspace-target="chengfang-production-write"/);
assert.match(html, /官方接口没有 CAS/);
assert.match(html, /请勿让其他投手同时修改该计划/);
assert.match(js, /取消只适用于尚未执行的操作/);

assert.match(js, /dashboardRead\("\/chengfang\/production-write", loadGeneration\)/);
assert.match(js, /bridgeFetch\("\/chengfang\/production-write\/prepare"/);
assert.match(js, /JSON\.stringify\(\{ target_key: draft\.target\.target_key, target_budget:/);
assert.match(js, /bridgeFetch\("\/chengfang\/production-write\/authorize"/);
assert.match(js, /operation_id: operation\.operation_id, confirmation_phrase: typed, confirm: true/);
assert.match(js, /typed !== phrase/);
assert.match(js, /bridgeFetch\("\/chengfang\/production-write\/execute"/);
assert.match(js, /JSON\.stringify\(\{ operation_id: operation\.operation_id \}\)/);
assert.match(js, /Date\.now\(\) > Number\(operation\.expires_at_ms\)/);
assert.match(js, /bridgeFetch\("\/chengfang\/production-write\/cancel"/);
assert.match(js, /operation_id: operation\.operation_id, confirm: true/);
assert.match(js, /!\["prepared", "authorized"\]\.includes\(phase\)/);
assert.match(js, /bridgeFetch\("\/chengfang\/production-write\/reconcile"/);
assert.match(js, /bridgeFetch\("\/chengfang\/production-write\/stop"/);
assert.match(js, /bridgeFetch\("\/chengfang\/production-write\/resume"/);
assert.match(js, /USER_REQUESTED_FROM_EXTENSION/);
assert.match(js, /confirmation_phrase: typed/);
assert.match(js, /typed !== phrase/);
assert.match(js, /killSwitchActive/);
assert.match(js, /execute\.disabled = phase !== "authorized" \|\| authorizationExpired \|\| unknown \|\| killSwitchActive/);
assert.match(js, /setChengfangProductionError\(error,[\s\S]*\{ freeze: true \}\)/);
assert.match(js, /Never offer a retry: freeze locally/);
assert.match(js, /simple-show-production/);
assert.match(js, /simple-show-oauth/);
assert.match(js, /operationControlsForm/);
assert.match(js, /operation\?\.matched === true && observedMatchesTarget/);
assert.match(js, /phase === "observed"/);
assert.match(js, /目标值已观察 · 请求归因未知/);
assert.doesNotMatch(js, /client_observation/);

assert.match(js, /rawIdentifierKeys = \["advertiser_id", "ad_id", "task_id", "task_ids", "control_tasks", "shop_id", "store_id", "account_id"\]/);
assert.match(js, /RAW_IDENTIFIER_EXPOSED/);
assert.match(js, /option\.textContent = chengfangProductionTargetLabel\(target, index\)/);
assert.match(js, /operationTarget\.plan_name/);
assert.match(js, /operationTarget\.local_short_code/);
assert.doesNotMatch(js, /option\.textContent\s*=\s*target\.target_key/);
assert.doesNotMatch(js, /document\.(querySelector|querySelectorAll)\([^\n]*(qianchuan|oceanengine|巨量|千川)[^\n]*\)[\s\S]{0,160}\.click\(/i);

const policyStart = js.indexOf("const CHENGFANG_PRODUCTION_BLOCKER_LABELS");
const policyEnd = js.indexOf("function trialMoney", policyStart);
assert.ok(policyStart > 0 && policyEnd > policyStart, "production write policy block should remain independently testable");
const sandbox = { module: { exports: {} }, console };
vm.runInNewContext(
  "let currentChengfangProductionWrite = { status: {}, targets: [], active: null, operation: null, kill_switch: {}, unsafe_target_response: false };\n"
    + js.slice(policyStart, policyEnd)
    + "\nmodule.exports = { normalizeChengfangProductionWrite, chengfangProductionState, chengfangProductionTargetLabel, chengfangProductionReadiness };",
  sandbox,
);
const productionPolicy = sandbox.module.exports;

const safe = productionPolicy.normalizeChengfangProductionWrite({
  status: { ready: true },
  targets: [{
    target_key: `cf_target_v1_${"a".repeat(32)}`,
    plan_name: "夏日直播计划",
    local_short_code: "CF-A1B2C3D4",
    affected_task_count: 2,
    current_total_budget: 1000,
    marketing_goal: "PRODUCT",
    scene: "SHOP",
    single_plan_bound: true,
    official_snapshot_verified: true,
  }],
  active: {
    operation_id: `cfop_${"b".repeat(32)}`,
    status: "authorized",
    current_value: 1000,
    target_value: 900,
    authorization_expires_at_ms: Date.now() + 60_000,
  },
});
assert.equal(safe.targets.length, 1);
assert.equal(safe.targets[0].plan_name, "夏日直播计划");
assert.equal(safe.targets[0].local_short_code, "CF-A1B2C3D4");
assert.equal(safe.targets[0].affected_task_count, 2);
assert.equal(safe.operation.current_budget, 1000);
assert.equal(safe.operation.target_budget, 900);
assert.equal(productionPolicy.chengfangProductionState(safe.operation, safe.status), "authorized");
const safeLabel = productionPolicy.chengfangProductionTargetLabel(safe.targets[0]);
assert.match(safeLabel, /夏日直播计划/);
assert.match(safeLabel, /CF-A1B2C3D4/);
assert.match(safeLabel, /影响 2 个任务/);
assert.doesNotMatch(safeLabel, /cf_target_v1_/);

const cleanedName = productionPolicy.normalizeChengfangProductionWrite({
  targets: [{
    target_key: `cf_target_v1_${"1".repeat(32)}`,
    plan_name: "<b>直播\u202e\n计划</b>",
    local_short_code: "CF-11223344",
    affected_task_count: 1,
    current_total_budget: 1000,
  }],
});
assert.equal(cleanedName.targets.length, 1);
assert.doesNotMatch(cleanedName.targets[0].plan_name, /[<>\u202e]/);

const currentGetShape = productionPolicy.normalizeChengfangProductionWrite({
  available: true,
  blockers: [],
  targets: [{
    target_key: `cf_target_v1_${"e".repeat(32)}`,
    plan_name: "商品计划",
    local_short_code: "CF-E1E2E3E4",
    affected_task_count: 1,
    current_total_budget: 800,
    marketing_goal: "GMV",
    scene: "FULL_DOMAIN",
  }],
  operations: [{
    operation_id: `cfop_${"f".repeat(32)}`,
    status: "prepared",
    current_value: 800,
    target_value: 720,
  }],
});
assert.equal(currentGetShape.status.ready, true);
assert.equal(currentGetShape.operation.state, "prepared");
assert.equal(currentGetShape.targets.length, 1);

const unsafe = productionPolicy.normalizeChengfangProductionWrite({
  targets: [{
    target_key: `cf_target_v1_${"c".repeat(32)}`,
    plan_name: "危险返回",
    local_short_code: "CF-C1C2C3C4",
    current_total_budget: 1000,
    advertiser_id: "raw-advertiser-id",
  }],
});
assert.equal(unsafe.targets.length, 0);
assert.equal(unsafe.unsafe_target_response, true);

const rawTaskList = productionPolicy.normalizeChengfangProductionWrite({
  targets: [{
    target_key: `cf_target_v1_${"2".repeat(32)}`,
    plan_name: "计划",
    local_short_code: "CF-22334455",
    current_total_budget: 1000,
    task_ids: ["raw-task-id"],
  }],
});
assert.equal(rawTaskList.targets.length, 0);
assert.equal(rawTaskList.unsafe_target_response, true);

const missingOpaqueCode = productionPolicy.normalizeChengfangProductionWrite({
  targets: [{
    target_key: `cf_target_v1_${"3".repeat(32)}`,
    plan_name: "计划",
    current_total_budget: 1000,
  }],
});
assert.equal(missingOpaqueCode.targets.length, 0);
assert.equal(missingOpaqueCode.unsafe_target_response, true);

const unknown = productionPolicy.normalizeChengfangProductionWrite({
  active: {
    operation_id: `cfop_${"d".repeat(32)}`,
    status: "unknown",
    manual_reconcile_required: true,
    adapter_receipt: { outcome: "unknown" },
  },
});
assert.equal(productionPolicy.chengfangProductionState(unknown.operation, unknown.status), "unknown");

const targetObserved = productionPolicy.normalizeChengfangProductionWrite({
  active: {
    operation_id: `cfop_${"9".repeat(32)}`,
    status: "observed",
    current_value: 1000,
    target_value: 900,
    matched: true,
    request_outcome: "causality_unknown",
    observed_state: "target",
    safety_resolution: "target_state_observed",
    official_readback: { observed_value: 900, matched: true },
  },
});
assert.equal(productionPolicy.chengfangProductionState(targetObserved.operation, targetObserved.status), "observed");
assert.equal(targetObserved.operation.observed_budget, 900);
assert.equal(targetObserved.operation.request_outcome, "causality_unknown");
assert.equal(targetObserved.operation.safety_resolution, "target_state_observed");

const stopped = productionPolicy.normalizeChengfangProductionWrite({
  available: false,
  blockers: ["PRODUCTION_WRITE_KILL_SWITCH_ACTIVE"],
  kill_switch: {
    active: true,
    reason: "USER_REQUESTED_FROM_EXTENSION",
    resume_confirmation_phrase: "确认恢复乘方生产写入",
  },
});
assert.equal(stopped.kill_switch.active, true);
assert.equal(stopped.kill_switch.resume_confirmation_phrase, "确认恢复乘方生产写入");
assert.deepEqual(Array.from(stopped.status.blockers), ["PRODUCTION_WRITE_KILL_SWITCH_ACTIVE"]);

const storeStep = productionPolicy.chengfangProductionReadiness({
  status: { blockers: ["STORE_NOT_SELECTED"] },
  targets: [],
  kill_switch: {},
}, "idle");
assert.equal(storeStep.action, "store");
assert.equal(storeStep.current_index, 0);
assert.equal(storeStep.steps[0].current, true);

const bindingStep = productionPolicy.chengfangProductionReadiness({
  status: { blockers: ["PRODUCTION_SCOPE_LEASE_REQUIRED"] },
  targets: [],
  kill_switch: {},
}, "idle");
assert.equal(bindingStep.action, "account");
assert.equal(bindingStep.current_index, 1);

const globalUnknown = productionPolicy.chengfangProductionReadiness({
  status: { blockers: ["STORE_NOT_SELECTED", "UNKNOWN_WRITE_REQUIRES_RECONCILIATION"] },
  targets: [],
  operation: { blockers: [], state: "unknown" },
  kill_switch: {},
}, "unknown");
assert.equal(globalUnknown.action, "reconcile");
assert.equal(globalUnknown.current_index, 4);

const stoppedStep = productionPolicy.chengfangProductionReadiness({
  status: { blockers: ["PRODUCTION_WRITE_KILL_SWITCH_ACTIVE"] },
  targets: [],
  kill_switch: { active: true },
}, "idle");
assert.equal(stoppedStep.action, "resume");

const readyStep = productionPolicy.chengfangProductionReadiness({
  status: { ready: true, blockers: [] },
  targets: [{ target_key: `cf_target_v1_${"a".repeat(32)}` }],
  kill_switch: {},
}, "idle");
assert.equal(readyStep.action, "select");
assert.equal(readyStep.steps.filter((step) => step.ready).length, 5);

console.log("chengfang controlled production write UI tests passed");
