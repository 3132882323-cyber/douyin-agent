const assert = require("assert");
const policy = require("./automation-policy-center.js");

{
  const matrix = policy.derivePolicyMatrix({
    schedule: { scope_bound: true },
    runtime: { profile_validation: { business_profile_ready: false }, shadow_evidence: {} },
  });
  assert.deepStrictEqual(matrix.map((item) => item.id), [
    "schedule", "auto_stop", "auto_budget", "chasing_restart", "smart_boost",
  ]);
  assert.strictEqual(matrix[0].state, "available");
  assert.strictEqual(matrix[1].state, "blocked");
  assert.strictEqual(matrix[2].state, "locked");
}

{
  const matrix = policy.derivePolicyMatrix({
    schedule: { scope_bound: true, active: { state: "verified" } },
    runtime: {
      profile_validation: { business_profile_ready: true },
      shadow_evidence: { validated_days: 7, useful_rate: 0.8, readback_success_rate: 0.99, critical_incidents: 0 },
    },
  });
  assert.strictEqual(matrix[0].state, "local_verified");
  assert.strictEqual(matrix[1].state, "shadow_available");
  assert.strictEqual(matrix[2].state, "design_ready");
  assert.ok(matrix.every((item) => item.next_step));
}

{
  const schedule = policy.normalizeSchedule({
    scope_bound: true,
    platform_write_enabled: false,
    latest: {
      revision_id: "schedule-1", revision: 1, state: "review_required", enabled: true,
      time_ranges: [{ start: "09:00", end: "12:00" }], platform_write_attempted: false,
    },
    next_events: [],
  });
  assert.strictEqual(schedule.state_label, "待人工复核");
  assert.deepStrictEqual(schedule.actions.map((item) => item.id), ["accept", "reject"]);
  assert.strictEqual(schedule.platform_write_enabled, false);
}

{
  const schedule = policy.normalizeSchedule({
    scope_bound: true,
    latest: {
      revision_id: "schedule-sim", state: "awaiting_readback", enabled: true,
      simulation_receipt: {
        events: [{ operation: "ENABLE", scheduled_at_ms: 123, platform_write_enabled: false }],
      },
    },
    next_events: [],
  });
  assert.strictEqual(schedule.next_events.length, 1, "simulation preview must be visible before readback");
  assert.deepStrictEqual(schedule.actions.map((item) => item.id), ["readback"]);
}

{
  const schedule = policy.normalizeSchedule({
    scope_bound: true,
    platform_write_enabled: true,
    latest: { revision_id: "unsafe", state: "approved" },
  });
  assert.strictEqual(schedule.unsafe, true);
  assert.strictEqual(schedule.platform_write_enabled, false);
  assert.match(schedule.notice, /不安全/);
}

console.log("automation policy center tests passed");
