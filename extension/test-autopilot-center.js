const assert = require("assert");
const fs = require("fs");
const path = require("path");
const center = require("./autopilot-center.js");

const sidepanelHtml = fs.readFileSync(path.join(__dirname, "sidepanel.html"), "utf8");
const sidepanelJs = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");
assert.match(sidepanelHtml, /id="autopilot-center"/);
assert.match(sidepanelHtml, /id="autopilot-operating-heading"/);
assert.match(sidepanelHtml, /id="autopilot-recovery-steps"/);
assert.match(sidepanelHtml, /data-chengfang-evidence="natural_flow_ratio"/);
assert.match(sidepanelHtml, /data-chengfang-evidence="paid_order_ratio"/);
assert.match(sidepanelHtml, /<script src="autopilot-center\.js"><\/script>\s*<script src="control-task-center\.js"><\/script>\s*<script src="automation-policy-center\.js"><\/script>\s*<script src="oceanengine-account-center\.js"><\/script>\s*<script src="material-governance\.js"><\/script>\s*<script src="promotion-plan-center\.js"><\/script>\s*<script src="promotion-bulk-actions\.js"><\/script>\s*<script src="promotion-operation-log\.js"><\/script>\s*<script src="product-capability\.js"><\/script>\s*<script src="simple-experience\.js"><\/script>\s*<script src="journey-orchestrator\.js"><\/script>\s*<script src="sidepanel\.js"><\/script>/);
assert.match(sidepanelJs, /function renderAutopilotCenter\(\)/);
assert.match(sidepanelJs, /function renderAutopilotOperatingLoop\(/);
assert.match(sidepanelJs, /writeEnabled: false/);

const catalog = {
  stores: [{ key: "store-001", label: "测试店铺", state_label: "网页已同步" }],
  selected_store_key: "store-001",
  selected_account_key: "advertiser-ABC123",
};
const gate = {
  identity_ready: true,
  identity_conflict: false,
  metric_ready: true,
  data_ready: true,
  next_step: "继续观察",
};
const candidates = [
  { candidate_id: "down-1", plan_key: "plan-A10001", state: "shadow_candidate", current_value: 1000, target_value: 900, reasons: ["ROI_BELOW_BREAK_EVEN"] },
  { candidate_id: "up-1", plan_key: "plan-B10002", state: "shadow_candidate", current_value: 1000, target_value: 1100 },
];
const localPlan = {
  goal: "profit",
  inputs: {
    price: 100, product_cost: 35, commission_rate: 10, platform_fee_rate: 5,
    merchant_discount: 5, fulfillment_cost: 5, refund_rate: 10, advertising_cost: 25,
  },
  evidence: {
    natural_flow_ratio: 35, paid_order_ratio: 65,
    today_spend: 1200, actual_roi: 1.1, attributed_orders: 4, daily_realized_loss: 50,
  },
  boundaries: {
    daily_budget_cap: 5000, daily_loss_cap: 300, daily_action_cap: 3,
    cooldown_minutes: 30, single_adjustment_cap: 10, daily_adjustment_cap: 20,
    authorization_ttl_seconds: 60,
  },
};

assert.deepEqual(center.normalizeSettings({ mode: "unknown", max_batch: 99 }), {
  schema_version: 1,
  mode: "protect",
  max_batch: 5,
});

const firstUse = center.deriveCenterView({
  settings: { mode: "stabilize" },
  gate,
  catalog,
  runtime: { candidates, shadow_evidence: { validated_days: 2, useful_rate: 1, readback_success_rate: 1, critical_incidents: 0 } },
});
assert.equal(firstUse.selected_mode, "protect", "locked mode must fall back to protect");
assert.equal(firstUse.modes.find((item) => item.id === "stabilize").locked, true);
assert.equal(firstUse.draft_count, 1, "protect mode must exclude budget increase candidates");
assert.equal(firstUse.drafts[0].kind, "decrease");
assert.equal(firstUse.production_write_ready, false);

const shadowPassed = center.deriveCenterView({
  settings: { mode: "stabilize", max_batch: 3 },
  gate,
  catalog,
  runtime: {
    candidates,
    decision_automation: { running: true },
    shadow_evidence: { validated_days: 7, useful_rate: 70, readback_success_rate: 99, critical_incidents: 0 },
  },
});
assert.equal(shadowPassed.selected_mode, "stabilize");
assert.equal(shadowPassed.shadow.passed, true);
assert.equal(shadowPassed.draft_count, 2);
assert.equal(shadowPassed.primary_action.id, "review");

const unbound = center.deriveCenterView({ gate: {}, catalog: {}, runtime: {} });
assert.equal(unbound.status.label, "待准备");
assert.equal(unbound.primary_action.id, "prepare_store");
assert.equal(unbound.primary_action.label, "打开抖店并巡店");

const requestedScale = center.deriveCenterView({
  settings: { mode: "scale" },
  gate,
  catalog,
  runtime: {
    write_automation: { production_write_ready: true },
    shadow_evidence: { validated_days: 7, useful_rate: 1, readback_success_rate: 1, critical_incidents: 0 },
  },
  pilot: { live_execution_available: true, platform_write_enabled: true },
  writeEnabled: false,
});
assert.equal(requestedScale.selected_mode, "protect");
assert.equal(requestedScale.modes.find((item) => item.id === "scale").locked, true);

const draft = center.buildModeDraft(shadowPassed.settings, shadowPassed);
assert.equal(draft.execution_kind, "draft_only");
assert.equal(draft.platform_write_enabled, false);
assert.equal(draft.guardrails.per_plan_authorization_required, true);
assert.equal(draft.guardrails.readback_required, true);
assert.deepEqual(draft.candidate_ids.sort(), ["down-1", "up-1"]);

const packageRuntime = {
  ...shadowPassed,
  profile_validation: { business_profile_ready: true },
  policy: {
    daily_budget_cap: 5000,
    max_daily_loss: 300,
    max_single_adjustment_pct: 10,
    max_daily_adjustment_pct: 20,
    max_daily_actions: 3,
    cooldown_minutes: 30,
    authorization_ttl_seconds: 60,
  },
  a2_pilot: { plan_key: "plan-A10001" },
};
const packagePreview = center.strategyPackagePreview({
  view: shadowPassed,
  runtime: packageRuntime,
  pilot: packageRuntime.a2_pilot,
  gate,
  localPlan,
});
assert.equal(packagePreview.can_apply, true);
assert.equal(packagePreview.affected_plan_count, 2);
assert.equal(packagePreview.package.execution_kind, "draft_only");
assert.equal(packagePreview.package.platform_write_enabled, false);
assert.equal(packagePreview.package.source_label, "7 天影子反馈与经营边界");
assert.equal(packagePreview.package.revision, 1);
assert.match(packagePreview.package.config_fingerprint, /^cfg-[a-f0-9]{8}$/);
assert.match(packagePreview.package.scope_fingerprint, /^cfg-[a-f0-9]{8}$/);
assert.equal(JSON.stringify(packagePreview.package).includes("store-001"), false, "saved package must not contain raw store keys");
assert.equal(JSON.stringify(packagePreview.package).includes("advertiser-ABC123"), false, "saved package must not contain raw account keys");
assert.equal(packagePreview.package.business_strategy.stage, "profit_mature");
assert.equal(packagePreview.package.recovery_policy.platform_write_enabled, false);
assert.equal(Object.hasOwn(packagePreview.package.business_strategy, "price"), false, "strategy package must not persist raw unit inputs");

const packageApplied = center.strategyPackagePreview({
  view: shadowPassed,
  runtime: packageRuntime,
  pilot: packageRuntime.a2_pilot,
  gate,
  localPlan,
  appliedPackage: packagePreview.package,
});
assert.equal(packageApplied.already_applied, true);
assert.equal(packageApplied.can_apply, false);
assert.equal(packageApplied.changed_count, 0);
assert.equal(packageApplied.package.revision, 1);

const incompletePackage = center.strategyPackagePreview({
  view: shadowPassed,
  runtime: { profile_validation: { business_profile_ready: false }, policy: {} },
  pilot: { plan_key: "plan-A10001" },
  gate,
});
assert.equal(incompletePackage.can_apply, false);
assert.ok(incompletePackage.missing_guardrails.length >= 7);

const task = center.deriveRealtimeTask({}, {
  execution_kind: "simulation",
  candidates: [{ candidate_id: "candidate-1", plan_key: "plan-A10001", state: "awaiting_readback", platform_write_attempted: false }],
  executions: [{ execution_id: "execution-1", candidate_id: "candidate-1", state: "awaiting_readback", execution_kind: "simulation", platform_write_attempted: false }],
});
assert.equal(task.state_label, "等待模拟回读");
assert.deepEqual(task.steps.map((item) => item.state), ["complete", "complete", "complete", "current"]);
assert.equal(task.platform_write_attempted, false);

const businessStrategy = center.deriveBusinessStrategy(localPlan, gate);
assert.equal(businessStrategy.ready, true);
assert.equal(businessStrategy.stage, "profit_mature");
assert.equal(businessStrategy.gmv_weight, 35);
assert.equal(businessStrategy.profit_weight, 65);
assert.equal(businessStrategy.attribution_status, "declared");
assert.ok(businessStrategy.observation_roi_floor > businessStrategy.break_even_roi);

const recovery = center.deriveRecoveryPolicy({ localPlan, gate, businessStrategy });
assert.equal(recovery.ready, true);
assert.equal(recovery.state, "pause_candidate");
assert.equal(recovery.sample_spend_floor, 1000);
assert.equal(recovery.recheck_minutes, 30);
assert.equal(recovery.max_cycles, 3);
assert.equal(recovery.human_approval_required, true);
assert.equal(recovery.automatic_rebuild_enabled, false);
assert.equal(recovery.platform_write_enabled, false);
assert.deepEqual(recovery.steps.map((item) => item.state), ["complete", "current", "waiting", "waiting", "waiting"]);

const lossLocked = center.deriveRecoveryPolicy({
  localPlan: { ...localPlan, evidence: { ...localPlan.evidence, daily_realized_loss: 300 } },
  gate,
  businessStrategy,
});
assert.equal(lossLocked.state, "hard_stop");
assert.equal(lossLocked.steps[1].state, "failed");

const recoveryReadback = center.deriveRecoveryPolicy({
  localPlan,
  gate,
  businessStrategy,
  realtimeTask: { state: "awaiting_readback" },
});
assert.deepEqual(recoveryReadback.steps.map((item) => item.state), ["complete", "complete", "complete", "complete", "current"]);

console.log("autopilot-center tests passed");
