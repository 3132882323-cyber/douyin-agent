const test = require("node:test");
const assert = require("node:assert/strict");

const policy = require("./execution-readback-policy.js");

const SCOPE = Object.freeze({ store_key: "store-a", account_key: "account-a", plan_id: "plan-1" });

test("execution readback retries use absolute 2/5/15/30 second checkpoints", () => {
  const job = policy.createJob({ action_id: "a".repeat(24), tab_id: 12, ...SCOPE }, 1_000);
  assert.deepEqual(job.offsets_ms, [2_000, 5_000, 15_000, 30_000]);
  assert.equal(policy.nextAttempt(job, 2_999).due, false);
  assert.equal(policy.nextAttempt(job, 3_000).due, true);
  const next = policy.recordAttempt(job, { collected: true, verification: { state: "executing" } }, 3_100);
  assert.equal(next.next_attempt_at_ms, 6_000);
  assert.equal(next.status, "pending");
});

test("verified readback stops all later attempts", () => {
  const job = policy.createJob({ action_id: "b".repeat(24), ...SCOPE }, 1_000);
  const done = policy.recordAttempt(job, {
    collected: true,
    verification: { state: "verified", verified: true },
  }, 3_100);
  assert.equal(done.status, "verified");
  assert.equal(policy.nextAttempt(done, 100_000).terminal, true);
});

test("original value remains locked until server explicitly marks retry safe", () => {
  let job = policy.createJob({ action_id: "c".repeat(24), ...SCOPE }, 1_000);
  for (let index = 0; index < 3; index += 1) {
    job = policy.recordAttempt(job, {
      collected: true,
      verification: { state: "executing", execution_unknown: true, original_value_confirmation_count: index + 1 },
    }, 3_000 + index * 5_000);
    assert.equal(job.status, "pending");
  }
  job = policy.recordAttempt(job, {
    collected: true,
    verification: { state: "failed", safe_to_retry: true, original_value_confirmation_count: 4 },
  }, 31_000);
  assert.equal(job.status, "failed");
  assert.match(job.status_label, /可重新生成方案/);
});

test("exhausted readbacks stay uncertain and locked", () => {
  let job = policy.createJob({ action_id: "d".repeat(24), ...SCOPE }, 1_000);
  for (let index = 0; index < 4; index += 1) {
    job = policy.recordAttempt(job, { error: "page still loading" }, 3_000 + index * 10_000);
  }
  assert.equal(job.status, "uncertain");
  assert.equal(policy.summarize({ [job.action_id]: job }).action_locked, true);
});

test("new readback jobs fail closed without store account and plan scope", () => {
  assert.throws(
    () => policy.createJob({ action_id: "e".repeat(24), store_key: "store-a", account_key: "account-a" }, 1_000),
    /\S/,
  );
});

test("legacy pending readback without scope is quarantined and remains locked", () => {
  const legacy = policy.normalizeJob({
    action_id: "f".repeat(24),
    status: "pending",
    started_at_ms: 1_000,
  });
  assert.equal(legacy.status, "uncertain");
  assert.equal(legacy.next_attempt_at_ms, 0);
  assert.equal(policy.summarize({ [legacy.action_id]: legacy }).action_locked, true);
});

test("missing exact scoped tab defers without consuming a readback attempt", () => {
  const job = policy.createJob({ action_id: "1".repeat(24), ...SCOPE }, 1_000);
  const deferred = policy.deferAttempt(job, {
    delay_ms: 60_000,
    error_code: "READBACK_SCOPE_TAB_UNAVAILABLE",
    error: "exact tab missing",
  }, 3_000);
  assert.equal(deferred.status, "pending");
  assert.equal(deferred.next_attempt_index, 0);
  assert.equal(deferred.attempts.length, 0);
  assert.equal(deferred.deferred_until_ms, 63_000);
  assert.equal(policy.nextAttempt(deferred, 62_999).due, false);
  assert.equal(policy.nextAttempt(deferred, 63_000).due, true);

  const attempted = policy.recordAttempt(deferred, { error: "page rejected" }, 63_100);
  assert.equal(attempted.next_attempt_index, 1);
  assert.equal(attempted.deferred_until_ms, 0);
});

test("manual reopen appends exactly one immediate attempt after automatic uncertainty", () => {
  let job = policy.createJob({ action_id: "2".repeat(24), ...SCOPE }, 1_000);
  for (let index = 0; index < 4; index += 1) {
    job = policy.recordAttempt(job, { error: `automatic attempt ${index + 1} uncertain` }, 3_000 + index * 10_000);
  }
  assert.equal(job.status, "uncertain");
  assert.equal(job.next_attempt_index, 4);

  const reopened = policy.reopenManualAttempt(job, 50_000);
  assert.equal(reopened.status, "pending");
  assert.equal(reopened.next_attempt_index, 4, "manual reopen must append rather than replay an old attempt index");
  assert.equal(reopened.offsets_ms.length, 5);
  assert.equal(reopened.manual_attempt_count, 1);
  assert.equal(policy.nextAttempt(reopened, 50_000).due, true);

  const verified = policy.recordAttempt(reopened, {
    collected: true,
    verification: { state: "verified", verified: true },
  }, 50_100);
  assert.equal(verified.status, "verified");
  assert.strictEqual(policy.reopenManualAttempt(verified, 60_000).status, "verified", "verified actions cannot be reopened");
});

test("manual request clears a scope deferral without consuming or duplicating its attempt", () => {
  const job = policy.createJob({ action_id: "3".repeat(24), ...SCOPE }, 1_000);
  const deferred = policy.deferAttempt(job, {
    delay_ms: 60_000,
    error_code: "READBACK_SCOPE_TAB_UNAVAILABLE",
  }, 3_000);
  const reopened = policy.reopenManualAttempt(deferred, 4_000);
  assert.equal(reopened.status, "pending");
  assert.equal(reopened.next_attempt_index, 0);
  assert.equal(reopened.attempts.length, 0);
  assert.equal(reopened.manual_attempt_count, 1);
  assert.equal(reopened.deferred_until_ms, 0);
  assert.equal(policy.nextAttempt(reopened, 4_000).due, true);
});
