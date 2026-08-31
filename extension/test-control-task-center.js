const assert = require("assert");
const policy = require("./control-task-center.js");

const baseSummary = {
  platform_write_enabled: false,
  automatic_platform_submit: false,
  notice: "当前仅本机模拟。",
  families: [
    { id: "status", task_count: 1, pending_count: 1, verified_count: 0, simulation_available: true },
    { id: "budget", task_count: 1, pending_count: 1, verified_count: 0, simulation_available: true },
    { id: "duration", task_count: 0, pending_count: 0, verified_count: 0, simulation_available: true },
  ],
  tasks: [
    {
      task_id: "task-budget",
      task_label: "任务预算 · ABC123",
      family: "budget",
      operation: "DECREASE",
      current_value: 1000,
      target_value: 900,
      state: "review_required",
      single_variable_only: true,
      change: { budget: 900 },
      platform_write_attempted: false,
    },
    {
      task_id: "task-status",
      task_label: "启停状态 · ABC123",
      family: "status",
      operation: "PAUSE",
      current_value: "ACTIVE",
      target_value: "PAUSED",
      state: "approved",
      single_variable_only: true,
      change: { status: "PAUSED" },
      platform_write_attempted: false,
    },
  ],
  importable_candidates: [{ candidate_id: "candidate-1", current_value: 1000, target_value: 900 }],
};

{
  const view = policy.deriveView(baseSummary, "budget");
  assert.strictEqual(view.selected_family, "budget");
  assert.strictEqual(view.tasks.length, 1);
  assert.strictEqual(view.tasks[0].current_label, "¥1000.00");
  assert.strictEqual(view.tasks[0].target_label, "¥900.00");
  assert.deepStrictEqual(view.tasks[0].actions.map((item) => item.id), ["accept", "reject"]);
  assert.strictEqual(view.primary_action.id, "import_candidate");
  assert.strictEqual(view.platform_write_enabled, false);
  assert.strictEqual(view.safe, true);
}

{
  const view = policy.deriveView(baseSummary, "status");
  assert.strictEqual(view.tasks[0].operation_label, "暂停");
  assert.strictEqual(view.tasks[0].current_label, "投放中");
  assert.strictEqual(view.tasks[0].target_label, "已暂停");
  assert.deepStrictEqual(view.tasks[0].actions.map((item) => item.id), ["simulate"]);
  assert.strictEqual(view.primary_action.id, "sync");
}

{
  const view = policy.deriveView({ ...baseSummary, scope_bound: true }, "duration");
  assert.strictEqual(view.manual_rehearsal_available, true);
  assert.strictEqual(view.primary_action.id, "open_builder");
  assert.match(view.primary_action.label, /时长/);
}

{
  const unsafe = policy.deriveView({
    ...baseSummary,
    platform_write_enabled: true,
  }, "budget");
  assert.strictEqual(unsafe.safe, false);
  assert.strictEqual(unsafe.platform_write_enabled, false);
  assert.match(unsafe.notice, /不安全/);
}

{
  const unsafe = policy.deriveView({
    ...baseSummary,
    tasks: [{
      ...baseSummary.tasks[0],
      change: { budget: 900, status: "PAUSED" },
    }],
  }, "budget");
  assert.strictEqual(unsafe.safe, false);
  assert.strictEqual(unsafe.unsafe_task_count, 1);
}

console.log("control task center tests passed");
