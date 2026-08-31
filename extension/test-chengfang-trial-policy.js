const assert = require("assert");
const policy = require("./chengfang-trial-policy.js");

const planKey = "cf-plan-7f3c9d";
const baseA2 = {
  level: "A1_shadow",
  master_enabled: false,
  stop_active: true,
  stop_reason: "NOT_CONFIGURED",
  execution_kind: "simulation",
  platform_write_enabled: false,
  live_execution_available: false,
  plan_key: planKey,
  plan_whitelist: [],
  budget_cap: null,
  evidence_gate: { fresh: true },
  candidates: [{
    candidate_id: "cf-candidate-1",
    plan_key: planKey,
    state: "shadow_candidate",
    current_value: 1000,
    target_value: 900,
  }],
  executions: [],
};
const runtime = {
  scope_fingerprint: "0123456789abcdef0123456789abcdef",
  decision_automation: { running: true, platform_write_enabled: false },
  policy: {
    daily_budget_cap: 5000,
    max_daily_loss: 300,
    max_single_adjustment_pct: 10,
    max_daily_adjustment_pct: 20,
    max_daily_actions: 3,
    cooldown_minutes: 30,
    authorization_ttl_seconds: 60,
  },
  a2_pilot: baseA2,
};
const syncOn = { autoSync: true, intervalMinutes: 5 };
const config = { environment: "demo", plan_whitelist: [planKey], budget_cap: 1200 };

assert.deepEqual(policy.normalizeWhitelist([planKey, "ignored-second-plan"]), [planKey]);

const demo = policy.deriveView(runtime, config, syncOn);
assert.equal(demo.can_configure, true);
assert.equal(demo.platform_write_enabled, false);
assert.equal(demo.can_accept, false, "A2 must be enabled before accepting a candidate");
assert.equal(demo.steps.find((item) => item.id === "review").state, "current");

const candidateEffect = policy.deriveEffect({
  ...runtime,
  profile: { evidence: { actual_roi: 9.9, attributed_orders: 999 } },
});
assert.equal(candidateEffect.source, "candidate");
assert.equal(candidateEffect.current_budget, 1000);
assert.equal(candidateEffect.simulated_target, 900);
assert.equal(candidateEffect.budget_change, -100);
assert.equal(candidateEffect.budget_change_pct, -10);
assert.equal(candidateEffect.receipt.present, false);
assert.equal(candidateEffect.readback.present, false);
assert.deepEqual(candidateEffect.observation_nodes.map((item) => item.id), ["2h", "24h", "3d", "7d"]);
assert.deepEqual(candidateEffect.outcome_limits.roi, { proven: false, value: null, label: "本机模拟不能证明 ROI 变化" });
assert.deepEqual(candidateEffect.outcome_limits.orders, { proven: false, value: null, label: "本机模拟不能证明订单变化" });

const configuredRuntime = {
  ...runtime,
  a2_pilot: {
    ...baseA2,
    level: "A2_simulation",
    master_enabled: true,
    stop_active: false,
    plan_whitelist: [planKey],
    budget_cap: 1200,
  },
};
const configured = policy.deriveView(configuredRuntime, config, syncOn);
assert.equal(configured.can_accept, true);
assert.equal(configured.can_execute, false);
assert.equal(configured.workflow_state, "A2 模拟运行中");

const payload = policy.buildConfigurePayload(runtime, config, syncOn);
assert.equal(payload.ok, true);
assert.deepEqual(payload.payload, {
  master_enabled: true,
  execution_kind: "simulation",
  plan_whitelist: [planKey],
  budget_cap: 1200,
  confirm: true,
});

const alias = policy.deriveView(runtime, { ...config, plan_whitelist: ["CF-LIVE-001"] }, syncOn);
assert.equal(alias.can_configure, false);
assert.match(alias.blockers.join("；"), /唯一作用域加入白名单/);

const production = policy.deriveView(
  { ...runtime, a2_pilot: { ...baseA2, platform_write_enabled: true } },
  { ...config, environment: "production" },
  syncOn,
);
assert.equal(production.can_configure, false);
assert.equal(production.platform_write_enabled, false);
assert.match(production.blockers.join("；"), /生产环境保持只读锁定/);
assert.match(production.blockers.join("；"), /官方调控能力已发现.*应用权限、账户白名单与生产适配器尚未验收/);

const missingBoundary = policy.deriveView(
  { ...runtime, policy: { ...runtime.policy, max_daily_loss: null } },
  config,
  syncOn,
);
assert.equal(missingBoundary.can_configure, false);
assert.match(missingBoundary.blockers.join("；"), /边界尚未补齐/);

const overCap = policy.deriveView(runtime, { ...config, budget_cap: 6000 }, syncOn);
assert.equal(overCap.can_configure, false);
assert.match(overCap.blockers.join("；"), /不得高于经营建档/);

const syncOff = policy.deriveView(runtime, config, { autoSync: false, intervalMinutes: 5 });
assert.equal(syncOff.can_configure, false);
assert.match(syncOff.blockers.join("；"), /明确开启每 5 分钟同步/);

const stale = policy.deriveView({
  ...runtime,
  a2_pilot: { ...baseA2, evidence_gate: { fresh: false } },
}, config, syncOn);
assert.equal(stale.can_configure, false);
assert.match(stale.blockers.join("；"), /新鲜度门槛/);

const acceptedRuntime = {
  ...configuredRuntime,
  a2_pilot: {
    ...configuredRuntime.a2_pilot,
    candidates: [{ ...baseA2.candidates[0], state: "accepted_for_pilot" }],
  },
};
const accepted = policy.deriveView(acceptedRuntime, config, syncOn);
assert.equal(accepted.can_execute, true);
assert.equal(accepted.steps.find((item) => item.id === "simulation").state, "current");

const execution = {
  execution_id: "cf-exec-1",
  candidate_id: "cf-candidate-1",
  execution_kind: "simulation",
  state: "awaiting_readback",
  current_value: 1000,
  target_value: 900,
  platform_write_attempted: false,
  adapter_receipt: {
    ok: true,
    receipt_id: "cf-sim-receipt-1",
    execution_kind: "simulation",
    adapter_id: "chengfang_simulator_v1",
    platform_write_attempted: false,
    simulated_value: 900,
  },
};
const awaiting = policy.deriveView({
  ...configuredRuntime,
  a2_pilot: {
    ...configuredRuntime.a2_pilot,
    candidates: [{ ...baseA2.candidates[0], state: "awaiting_readback" }],
    executions: [execution],
  },
}, config, syncOn);
assert.equal(awaiting.can_readback, true);
assert.equal(awaiting.steps.find((item) => item.id === "readback").state, "current");
const awaitingEffect = policy.deriveEffect({
  ...configuredRuntime,
  a2_pilot: {
    ...configuredRuntime.a2_pilot,
    candidates: [baseA2.candidates[0]],
    executions: [execution],
  },
});
assert.equal(awaitingEffect.source, "execution");
assert.equal(awaitingEffect.receipt.receipt_id, "cf-sim-receipt-1");
assert.equal(awaitingEffect.receipt.ok, true);
assert.equal(awaitingEffect.receipt.execution_kind, "simulation");
assert.equal(awaitingEffect.receipt.platform_write_attempted, false);
assert.equal(awaitingEffect.readback.present, false);

const verified = policy.deriveView({
  ...configuredRuntime,
  a2_pilot: {
    ...configuredRuntime.a2_pilot,
    candidates: [{ ...baseA2.candidates[0], state: "verified" }],
    executions: [{
      ...execution,
      state: "verified",
      readback: {
        source: "simulation",
        execution_kind: "simulation",
        observed_value: 900,
        expected_value: 900,
        matched: true,
        platform_write_observed: false,
      },
    }],
  },
}, config, syncOn);
assert.equal(verified.steps.find((item) => item.id === "readback").state, "complete");
assert.equal(verified.effect.readback.present, true);
assert.equal(verified.effect.readback.observed_value, 900);
assert.equal(verified.effect.readback.matched, true);

const noEffectEvidence = policy.deriveEffect({
  profile: { evidence: { current_total_budget: 888, actual_roi: 8.8, attributed_orders: 88 } },
  a2_pilot: { candidates: [], executions: [] },
});
assert.equal(noEffectEvidence.source, "none");
assert.equal(noEffectEvidence.current_budget, null);
assert.equal(noEffectEvidence.simulated_target, null);
assert.equal(noEffectEvidence.budget_change, null);
assert.equal(noEffectEvidence.budget_change_pct, null);

const demoFixtureEffect = policy.deriveEffect({
  synthetic: true,
  demo_fixture: true,
  platform_write_attempted: false,
  a2_pilot: {
    synthetic: true,
    demo_fixture: true,
    candidates: [{
      synthetic: true,
      demo_fixture: true,
      candidate_id: "demo-candidate",
      state: "verified",
      current_value: 1000,
      target_value: 900,
      platform_write_attempted: false,
    }],
    executions: [{
      synthetic: true,
      demo_fixture: true,
      execution_id: "demo-execution",
      candidate_id: "demo-candidate",
      state: "verified",
      execution_kind: "simulation",
      current_value: 1000,
      target_value: 900,
      platform_write_attempted: false,
      adapter_receipt: {
        synthetic: true,
        demo_fixture: true,
        ok: true,
        receipt_id: "demo-receipt",
        execution_kind: "simulation",
        platform_write_attempted: false,
      },
      readback: {
        synthetic: true,
        demo_fixture: true,
        source: "demo_fixture",
        execution_kind: "simulation",
        observed_value: 900,
        expected_value: 900,
        matched: true,
        platform_write_observed: false,
        platform_write_attempted: false,
      },
    }],
  },
});
assert.equal(demoFixtureEffect.synthetic, true);
assert.equal(demoFixtureEffect.demo_fixture, true);
assert.equal(demoFixtureEffect.platform_write_attempted, false);
assert.equal(demoFixtureEffect.budget_change, -100);
assert.equal(demoFixtureEffect.readback.matched, true);
assert.match(demoFixtureEffect.evidence_notice, /SYNTHETIC \/ DEMO_FIXTURE/);
assert.match(demoFixtureEffect.evidence_notice, /不是实际投放效果/);
const demoFixtureView = policy.deriveView({
  synthetic: true,
  demo_fixture: true,
  policy: runtime.policy,
  scope_fingerprint: "demo_fixture:synthetic_scope:not_a_real_account",
  a2_pilot: {
    synthetic: true,
    demo_fixture: true,
    platform_write_enabled: false,
    evidence_gate: { fresh: true },
    candidates: [{ candidate_id: "demo-candidate", state: "verified", current_value: 1000, target_value: 900 }],
    executions: [{
      execution_id: "demo-execution",
      candidate_id: "demo-candidate",
      state: "verified",
      execution_kind: "simulation",
      current_value: 1000,
      target_value: 900,
      platform_write_attempted: false,
    }],
  },
}, config, syncOn);
assert.equal(demoFixtureView.synthetic_demo, true);
assert.equal(demoFixtureView.can_configure, false);
assert.equal(demoFixtureView.can_stop, false);
assert.match(demoFixtureView.environment_label, /SYNTHETIC/);
assert.match(demoFixtureView.workflow_state, /未持久化/);

const readyCalculation = { status: "ready", missing: [], invalid: [] };
const readyBoundaries = { status: "ready", missing: [], invalid: [] };
const readyEvidence = {
  inventory_days: 8,
  fulfillment_rate: 98,
  creative_count: 6,
  data_completeness: 95,
  data_freshness_minutes: 10,
  current_total_budget: 1000,
  actual_roi: 1.8,
  today_spend: 200,
  attributed_orders: 5,
  daily_realized_loss: 0,
};
const readyPathInput = {
  identityReady: true,
  identityConflict: false,
  dataReady: true,
  planKey,
  goal: "profit",
  calculation: readyCalculation,
  boundaries: readyBoundaries,
  evidence: readyEvidence,
  shadowRunning: false,
  candidate: null,
};

const missingEvidencePath = policy.deriveCandidatePath({ ...readyPathInput, evidence: {} });
assert.equal(missingEvidencePath.profile_ready, true);
assert.equal(missingEvidencePath.evidence_ready, false);
assert.equal(missingEvidencePath.evidence.completed_count, 0);
assert.equal(missingEvidencePath.next_action.id, "evidence");
assert.equal(missingEvidencePath.completed_count, 2, "binding and profile can finish before evidence");

assert.equal(policy.deriveCandidatePath({ ...readyPathInput, identityReady: false }).next_action.id, "bind_scope");
assert.equal(policy.deriveCandidatePath({ ...readyPathInput, identityConflict: true }).next_action.id, "resolve_binding");
assert.equal(policy.deriveCandidatePath({ ...readyPathInput, planKey: "" }).next_action.id, "sync_chengfang");
assert.equal(policy.deriveCandidatePath({ ...readyPathInput, goal: "" }).next_action.id, "goal");

const missingCostPath = policy.deriveCandidatePath({
  ...readyPathInput,
  calculation: { status: "missing", missing: ["product_cost"], invalid: [] },
});
assert.equal(missingCostPath.next_action.id, "costs");
assert.equal(missingCostPath.next_action.field, "product_cost");
assert.equal(missingCostPath.cost.completed_count, 7);

const missingBoundaryPath = policy.deriveCandidatePath({
  ...readyPathInput,
  boundaries: { status: "missing", missing: ["daily_budget_cap"], invalid: [] },
});
assert.equal(missingBoundaryPath.next_action.id, "boundaries");
assert.equal(missingBoundaryPath.next_action.field, "daily_budget_cap");
assert.equal(missingBoundaryPath.boundaries.completed_count, 9);

const staleEvidencePath = policy.deriveCandidatePath({
  ...readyPathInput,
  evidence: { ...readyEvidence, data_freshness_minutes: 31 },
});
assert.equal(staleEvidencePath.next_action.id, "evidence");
assert.equal(staleEvidencePath.next_action.field, "data_freshness_minutes");
assert.match(staleEvidencePath.next_action.detail, /30 分钟/);

const shadowPath = policy.deriveCandidatePath(readyPathInput);
assert.equal(shadowPath.next_action.id, "shadow");
const evaluatePath = policy.deriveCandidatePath({ ...readyPathInput, shadowRunning: true });
assert.equal(evaluatePath.next_action.id, "evaluate");
assert.equal(evaluatePath.next_action.label, "立即运行首次评估");
assert.match(evaluatePath.next_action.detail, /保持策略/);
const candidatePath = policy.deriveCandidatePath({
  ...readyPathInput,
  shadowRunning: true,
  candidate: { candidate_id: "candidate-ready", state: "shadow_candidate" },
});
assert.equal(candidatePath.ready, true);
assert.equal(candidatePath.completed_count, 4);
assert.equal(candidatePath.next_action.id, "candidate");

console.log("chengfang trial policy tests passed");
