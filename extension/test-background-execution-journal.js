const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const policy = require("./execution-readback-policy.js");

const background = fs.readFileSync(path.join(__dirname, "background.js"), "utf8");

function sourceBetween(startMarker, endMarker) {
  const start = background.indexOf(startMarker);
  const end = background.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `${startMarker} should be extractable`);
  return background.slice(start, end);
}

const journalBlock = sourceBetween("async function loadExecutionReadbackJobs", "\nfunction qianchuanReadbackScopeMatches");
const recoveryBlock = sourceBetween("let executionReadbackRecoveryPromise", "\nasync function runAuthorizedExecution");
const authorizedBlock = sourceBetween("async function runAuthorizedExecution", "\nasync function storeAndPush");

const ACTION_ID = "a".repeat(24);
const AUTHORIZATION_ID = "b".repeat(32);
const REQUEST = Object.freeze({
  operation_type: "adjust_budget",
  mode: "supervised_submit",
  account_key: "account-a",
  page_type: "campaigns",
  plan_id: "plan-a",
  plan_name: "计划 A",
  expected_current_value: 500,
  target_value: 400,
  promotion_context: { account_scope: { store_id: "store-a", account_id: "account-a" } },
});

function response(ok, payload = {}) {
  return { ok, json: async () => payload };
}

function executionRequest(overrides = {}) {
  return {
    ...REQUEST,
    promotion_context: {
      ...REQUEST.promotion_context,
      account_scope: { ...REQUEST.promotion_context.account_scope },
    },
    ...overrides,
  };
}

function createRuntime(storage, server, options = {}) {
  const calls = {
    timeline: [],
    receiptPosts: 0,
    preflightReads: 0,
    previewPosts: 0,
    finalRereadPosts: 0,
    finalRereadBodies: [],
    consumePosts: 0,
    pageSubmits: 0,
    readbacks: 0,
    hardReloads: 0,
    receiptBodies: [],
    phaseRaces: 0,
    consumeBodies: [],
    collectCalls: [],
    pushCalls: [],
    tokenGenerations: 0,
    alarmCreates: [],
    alarmClears: 0,
    recoveryJobReads: 0,
  };
  const tab = { id: 17, windowId: 3, url: "https://qianchuan.jinritemai.com/uni-prom" };
  async function storageGet(keys) {
    if (typeof keys === "string") return { [keys]: storage[keys] };
    if (Array.isArray(keys)) return Object.fromEntries(keys.map((key) => [key, storage[key]]));
    return { ...storage };
  }
  const context = {
    console,
    BRIDGE_URL: "http://127.0.0.1:8765",
    EXECUTION_READBACK_STORAGE_KEY: "executionReadbackJobsV1",
    EXECUTION_READBACK_ALARM: "dian-agent-execution-readback",
    DianExecutionReadbackPolicy: policy,
    DianAgentScanPolicy: { selectQianchuanSyncTab: () => ({ tab, matchedBy: "recent" }) },
    querySourceTabs: async () => [tab],
    qianchuanReadbackScopeMatches: (record, scope) => Boolean(
      record
      && String(record.store_key || "").toLowerCase() === String(scope.store_key || "").toLowerCase()
      && String(record.account_key || "").toLowerCase() === String(scope.account_key || "").toLowerCase()
    ),
    inspectPlatformPage: async () => ({ ok: true }),
    reloadExecutionReadbackTab: async (selectedTab) => {
      calls.hardReloads += 1;
      return {
        ...selectedTab,
        readback_document_witness: options.reloadWitness || {
          document_instance_id: "document-final-0001",
          navigation_started_at_ms: 7_000,
        },
      };
    },
    collectFromTab: async (source, selectedTab, reason, collectOptions = {}) => {
      calls.collectCalls.push({ source, selectedTab, reason, options: collectOptions });
      return {
        ok: true,
        snapshot: {
          page_type: "qianchuan_plan_list",
          captured_at: Date.now(),
          quality: {
            targeted: true,
            scope: "target_plan",
            coverage_complete: false,
            collection_complete: false,
            target_plan_found: true,
          },
        },
      };
    },
    storeAndPush: async (source, snapshot, pushOptions = {}) => {
      calls.pushCalls.push({ source, snapshot, options: pushOptions });
      return { ok: true };
    },
    createExecutionReadbackToken: () => {
      calls.tokenGenerations += 1;
      return calls.tokenGenerations.toString(16).padStart(64, "0");
    },
    executionWitnessScopeHash: async () => "c".repeat(64),
    mutateLocalStorage: async (keys, mutator) => {
      const current = await storageGet(keys);
      const updates = await mutator(current) || {};
      Object.assign(storage, updates);
      const updatedJob = storage.executionReadbackJobsV1?.[ACTION_ID];
      if (options.raceAfterConsume && updatedJob?.journal_state === "awaiting_submission") {
        storage.executionReadbackJobsV1 = {
          ...storage.executionReadbackJobsV1,
          [ACTION_ID]: {
            ...updatedJob,
            journal_state: "submission_unknown",
            status_label: "a concurrent recovery already claimed the phase",
            updated_at_ms: Number(updatedJob.updated_at_ms || 0) + 1,
          },
        };
        calls.phaseRaces += 1;
      }
      const job = storage.executionReadbackJobsV1?.[ACTION_ID];
      if (job) calls.timeline.push(`persist:${job.journal_state || job.status}`);
      return updates;
    },
    bridgeFetch: async (url, init = {}) => {
      if (url.endsWith("/actions/preflight/preview")) {
        calls.previewPosts += 1;
        return response(true, {
          preview: {
            action_id: ACTION_ID,
            authorized_at_ms: Date.now() - 1_000,
            execution_request: executionRequest(),
          },
        });
      }
      if (url.endsWith("/actions/preflight/final-reread")) {
        calls.finalRereadPosts += 1;
        calls.finalRereadBodies.push(JSON.parse(init.body));
        return response(true, {
          preview: {
            action_id: ACTION_ID,
            execution_request: executionRequest(),
            execution_baseline: options.finalBaseline || {
              document_instance_id: "document-final-0001",
              navigation_started_at_ms: 7_000,
            },
          },
        });
      }
      if (url.endsWith("/actions/preflight/consume")) {
        calls.consumePosts += 1;
        calls.consumeBodies.push(JSON.parse(init.body));
        if (options.consumeGate) await options.consumeGate;
        return response(true, {
          grant: {
            action_id: ACTION_ID,
            execution_request: executionRequest({
              execution_attempt_id: AUTHORIZATION_ID,
              execute_before_ms: Date.now() + 60_000,
            }),
          },
        });
      }
      if (url.endsWith("/actions/preflight") && (!init.method || init.method === "GET")) {
        calls.preflightReads += 1;
        if (server.preflightAvailable === false) return response(false, { error: "preflight unavailable" });
        return response(true, {
          state: server.sessionState || "authorized",
          recoverable_readback_action_ids: server.recoverableActionIds || [],
          session: {
            action_id: server.actionId || ACTION_ID,
            authorization_id: server.authorizationId || AUTHORIZATION_ID,
            execution_receipt_recorded: server.receiptRecorded === true,
            execution_receipt_action_state: server.receiptActionState || "",
            authorization_consumed: server.authorizationConsumed === true,
            state: server.sessionState || "authorized",
          },
        });
      }
      if (url.includes("/actions/execution/readback-job?action_id=")) {
        calls.recoveryJobReads += 1;
        const requestedActionId = decodeURIComponent(url.split("action_id=")[1] || "");
        const recoveryJob = server.recoveryJobs?.[requestedActionId] || server.recoveryJob;
        return recoveryJob
          ? response(true, { job: recoveryJob })
          : response(false, { error: "no recoverable job" });
      }
      if (url.endsWith("/actions/execution/result")) {
        calls.receiptPosts += 1;
        calls.timeline.push("receipt-post");
        const receiptBody = JSON.parse(init.body);
        calls.receiptBodies.push(receiptBody);
        const submitted = receiptBody.result.submitted;
        if (server.receiptMode === "reject") return response(false, { error: "receipt offline", error_code: "BRIDGE_TEMPORARY" });
        if (server.receiptMode === "commit_then_throw") {
          server.receiptRecorded = true;
          server.receiptActionState = submitted === false ? "failed" : "executing";
          throw new Error("response lost after commit");
        }
        server.receiptRecorded = true;
        server.receiptActionState = submitted === false ? "failed" : "executing";
        return response(true, { ok: true });
      }
      throw new Error(`unexpected bridge request: ${url}`);
    },
    runExecutionReadbackJob: async (job) => {
      calls.readbacks += 1;
      calls.timeline.push(`readback:${job.journal_state || job.status}`);
      return { ...job, status: "verified", last_verification: { state: "verified", verified: true } };
    },
    chrome: {
      storage: { local: { get: storageGet } },
      alarms: {
        create(name, config) { calls.alarmCreates.push({ name, config }); },
        clear: async () => { calls.alarmClears += 1; return true; },
      },
      tabs: {
        sendMessage: async (_tabId, message) => {
          if (message.type === "qianchuan-execution-probe") {
            return {
              ok: true,
              ready: true,
              execution_witness: options.probeWitness || options.reloadWitness || {
                document_instance_id: "document-final-0001",
                navigation_started_at_ms: 7_000,
                promotion_mode: "standard",
                page_type: "campaigns",
              },
            };
          }
          calls.timeline.push("page-submit");
          calls.pageSubmits += 1;
          if (options.submitThrows) throw new Error("message port closed");
          return options.submitResult || {
            ok: true,
            submitted: true,
            platform_success_observed: false,
            plan_id: REQUEST.plan_id,
            target_value: REQUEST.target_value,
          };
        },
        update: async () => undefined,
      },
      windows: { update: async () => undefined },
    },
  };
  context.globalThis = context;
  vm.runInNewContext(
    `${journalBlock}\n${recoveryBlock}\n${authorizedBlock}\n`
      + "globalThis.__journalApi = { persistExecutionReadbackJob, advanceExecutionJournal, recoverExecutionReadbackJobs, runAuthorizedExecution };",
    context,
    { filename: "background-execution-journal.vm.js" },
  );
  return { ...context.__journalApi, calls };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function waitUntil(predicate, message) {
  for (let index = 0; index < 200; index += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.fail(message);
}

function actionId(index) {
  return Number(index).toString(16).padStart(24, "0").slice(-24);
}

function storedJob(id, status, updatedAtMs) {
  return {
    action_id: id,
    store_key: "store-a",
    account_key: "account-a",
    plan_id: `plan-${id}`,
    started_at_ms: 1,
    offsets_ms: [2_000, 5_000, 15_000, 30_000],
    next_attempt_index: status === "pending" ? 0 : 4,
    attempts: [],
    status,
    status_label: status,
    updated_at_ms: updatedAtMs,
  };
}

function assertPreconsumeBinding(calls) {
  assert.equal(calls.tokenGenerations, 1, "one preconsume baseline must mint one fresh token");
  assert.equal(calls.finalRereadBodies.length, 1);
  const readbackToken = calls.finalRereadBodies[0].readback_token;
  assert.match(readbackToken, /^[a-f0-9]{64}$/);
  assert.deepEqual(JSON.parse(JSON.stringify(calls.finalRereadBodies[0])), {
    authorization_id: AUTHORIZATION_ID,
    readback_token: readbackToken,
  });
  const executionContext = {
    purpose: "preconsume_baseline",
    action_id: ACTION_ID,
    authorization_id: AUTHORIZATION_ID,
    readback_token: readbackToken,
  };
  assert.equal(calls.collectCalls.length, 1);
  assert.equal(calls.collectCalls[0].options.deferPush, true);
  assert.equal(calls.collectCalls[0].options.targetPlanId, REQUEST.plan_id);
  assert.equal(calls.pushCalls.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(calls.pushCalls[0].options)), {
    expectedStoreKey: "store-a",
    expectedAccountKey: "account-a",
    executionContext,
  });
  return readbackToken;
}

(async () => {
  {
    const storage = {};
    const server = { receiptRecorded: false, preflightAvailable: true, receiptMode: "ok" };
    const runtime = createRuntime(storage, server, {
      reloadWitness: {
        document_instance_id: "document-new-from-hard-reload",
        navigation_started_at_ms: 8_000,
      },
      finalBaseline: {
        document_instance_id: "document-backend-baseline",
        navigation_started_at_ms: 8_000,
      },
    });
    await assert.rejects(
      runtime.runAuthorizedExecution(AUTHORIZATION_ID),
      /最终验收后页面文档又发生变化/,
    );
    assert.equal(runtime.calls.hardReloads, 1);
    assert.equal(runtime.calls.finalRereadPosts, 1);
    assertPreconsumeBinding(runtime.calls);
    assert.equal(runtime.calls.consumePosts, 0, "a witness different from the backend baseline must stop before consuming authorization");
    assert.equal(runtime.calls.pageSubmits, 0, "a witness different from the backend baseline must never dispatch");
    assert.equal(storage.executionReadbackJobsV1, undefined, "no execution journal is created for a rejected final witness");
  }

  {
    const storage = {};
    const server = { receiptRecorded: false, preflightAvailable: true, receiptMode: "reject" };
    const runtime = createRuntime(storage, server);
    const firstDispatch = runtime.runAuthorizedExecution(AUTHORIZATION_ID);
    const duplicateDispatch = runtime.runAuthorizedExecution(AUTHORIZATION_ID);
    assert.strictEqual(duplicateDispatch, firstDispatch, "the same authorization must share one in-flight execution");
    const dispatches = await Promise.allSettled([firstDispatch, duplicateDispatch]);
    assert.equal(dispatches.every((item) => item.status === "rejected" && item.reason?.code === "BRIDGE_TEMPORARY"), true);
    assert.equal(runtime.calls.previewPosts, 1, "single-flight must preview an authorization once");
    assert.equal(runtime.calls.finalRereadPosts, 1, "single-flight must perform one final hard reread");
    assertPreconsumeBinding(runtime.calls);
    assert.equal(runtime.calls.consumePosts, 1, "single-flight must consume an authorization once");
    assert.deepEqual(JSON.parse(JSON.stringify(runtime.calls.consumeBodies[0])), {
      authorization_id: AUTHORIZATION_ID,
      readback_context: {
        scope_hash: "c".repeat(64),
        document_instance_id: "document-final-0001",
        navigation_started_at_ms: 7_000,
        promotion_mode: "standard",
        page_type: "campaigns",
      },
    }, "consume must bind the exact document, mode, page, and scope needed for later readback reconstruction");
    assert.equal(runtime.calls.pageSubmits, 1, "single-flight must dispatch to the page once");
    const job = storage.executionReadbackJobsV1[ACTION_ID];
    assert.equal(job.status, "pending");
    assert.equal(job.journal_state, "awaiting_receipt");
    assert.equal(job.execution_receipt.submitted, true);
    assert.equal(runtime.calls.readbacks, 0, "readback must wait until the execution receipt is acknowledged");
    assert.ok(
      runtime.calls.timeline.indexOf("persist:awaiting_submission") < runtime.calls.timeline.indexOf("page-submit"),
      "the exact-scope journal must be durable before the page can submit",
    );

    // Simulate a reclaimed MV3 worker discovering that the first receipt POST
    // committed server-side even though its response was lost.
    job.started_at_ms = Date.now() - 10_000;
    server.receiptRecorded = true;
    server.receiptActionState = "executing";
    server.preflightAvailable = true;
    server.receiptMode = "reject";
    const restarted = createRuntime(storage, server);
    const recovered = await restarted.recoverExecutionReadbackJobs("worker-start");
    assert.equal(recovered.processed >= 1, true);
    assert.equal(restarted.calls.receiptPosts, 0, "restart should accept the exact recorded receipt instead of duplicating it");
    assert.equal(restarted.calls.readbacks, 1, "restart must continue into exact-scope readback after receipt recovery");
    assert.equal(storage.executionReadbackJobsV1[ACTION_ID].journal_state, "readback_pending");
  }

  {
    const pendingId = actionId(10_001);
    const uncertainId = actionId(10_002);
    const jobs = {
      [pendingId]: storedJob(pendingId, "pending", 1),
      [uncertainId]: storedJob(uncertainId, "uncertain", 2),
    };
    for (let index = 1; index <= 120; index += 1) {
      const id = actionId(index);
      jobs[id] = storedJob(id, "verified", 100_000 + index);
    }
    const storage = { executionReadbackJobsV1: jobs };
    const runtime = createRuntime(storage, { receiptMode: "ok" });
    const newestTerminalId = actionId(20_000);
    await runtime.persistExecutionReadbackJob(storedJob(newestTerminalId, "verified", 999_999));
    const retained = storage.executionReadbackJobsV1;
    assert.equal(retained[pendingId]?.status, "pending", "old pending journals must never be evicted by newer terminal history");
    assert.equal(retained[uncertainId]?.status, "uncertain", "old uncertain journals must never be evicted by newer terminal history");
    assert.equal(Object.keys(retained).length, 100, "terminal history may be trimmed to keep the bounded cache");
    assert.equal(
      Object.values(retained).filter((item) => ["pending", "uncertain"].includes(item.status)).length,
      2,
      "every unresolved journal must survive terminal-history trimming",
    );
  }

  {
    const consume = deferred();
    const storage = {};
    const server = { receiptRecorded: false, preflightAvailable: true, receiptMode: "ok" };
    const runtime = createRuntime(storage, server, { consumeGate: consume.promise });
    const owner = runtime.runAuthorizedExecution(AUTHORIZATION_ID);
    await waitUntil(() => runtime.calls.consumePosts === 1, "owner did not reach the consume boundary");
    storage.executionReadbackJobsV1[ACTION_ID].consume_intent_recovery_after_ms = 0;

    const recovered = await runtime.recoverExecutionReadbackJobs("owner-single-flight");
    assert.equal(recovered.pending, 1);
    assert.equal(runtime.calls.receiptPosts, 0, "recovery must not post the negative checkpoint while the authorization owner is active");
    assert.equal(runtime.calls.pageSubmits, 0, "the page stays untouched while the owner is paused at consume");
    assert.equal(storage.executionReadbackJobsV1[ACTION_ID].journal_state, "awaiting_consume");

    consume.resolve();
    const result = await owner;
    assert.equal(result.submitted, true);
    assert.equal(runtime.calls.pageSubmits, 1);
    assert.equal(runtime.calls.receiptPosts, 1);
    assert.deepEqual(runtime.calls.receiptBodies.map((item) => item.result.submitted), [true], "owner recovery must never fabricate a submitted=false receipt");
  }

  {
    const storage = {};
    const server = { receiptRecorded: false, preflightAvailable: true, receiptMode: "ok" };
    const runtime = createRuntime(storage, server, { raceAfterConsume: true });
    await assert.rejects(
      runtime.runAuthorizedExecution(AUTHORIZATION_ID),
      /执行断点状态发生竞争变化/,
    );
    assert.equal(runtime.calls.consumePosts, 1);
    assert.equal(runtime.calls.phaseRaces, 1);
    assert.equal(storage.executionReadbackJobsV1[ACTION_ID].journal_state, "submission_unknown");
    assert.equal(runtime.calls.pageSubmits, 0, "a phase owner change after consume must stop before page dispatch");
    assert.equal(runtime.calls.receiptPosts, 0, "a phase race must not claim a negative receipt");
  }

  {
    const storage = {};
    const server = {
      receiptRecorded: false,
      preflightAvailable: true,
      receiptMode: "ok",
      authorizationConsumed: true,
      sessionState: "authorization_consumed",
    };
    const runtime = createRuntime(storage, server);
    const base = policy.createJob({
      action_id: ACTION_ID,
      store_key: "store-a",
      account_key: "account-a",
      plan_id: "plan-a",
      started_at_ms: Date.now() - 10_000,
    });
    await runtime.persistExecutionReadbackJob({
      ...base,
      journal_state: "awaiting_consume",
      authorization_id: AUTHORIZATION_ID,
      consume_intent_recovery_after_ms: 0,
      pre_dispatch_negative_receipt: {
        authorization_id: AUTHORIZATION_ID,
        submitted: false,
        platform_success_observed: false,
        operation_type: REQUEST.operation_type,
        store_key: "store-a",
        account_key: REQUEST.account_key,
        plan_id: REQUEST.plan_id,
        target_value: REQUEST.target_value,
        definite_not_submitted: true,
        submission_phase: "pre_mutation",
        mutation_started: false,
        click_invoked: false,
        recovery_unverified: false,
      },
      updated_at_ms: Date.now() - 10_000,
    });
    const recovered = await runtime.recoverExecutionReadbackJobs("consume-intent-crash");
    const job = storage.executionReadbackJobsV1[ACTION_ID];
    assert.equal(recovered.processed >= 1, true);
    assert.equal(job.status, "failed");
    assert.equal(job.journal_state, "submission_failed");
    assert.equal(job.execution_receipt.definite_not_submitted, true);
    assert.equal(runtime.calls.pageSubmits, 0, "consume-intent recovery must never redispatch to the page");
    assert.equal(runtime.calls.receiptPosts, 1, "a consumed grant must resume from its durable pre-dispatch receipt");
    assert.equal(runtime.calls.readbacks, 0, "a proven pre-mutation interruption does not need platform readback");
  }

  {
    const storage = {};
    const server = { receiptRecorded: false, preflightAvailable: true, receiptMode: "ok" };
    const runtime = createRuntime(storage, server, {
      submitResult: {
        ok: false,
        submitted: false,
        error: "page rejected before click",
        definite_not_submitted: true,
        submission_phase: "pre_mutation",
        mutation_started: false,
        click_invoked: false,
        recovery_unverified: false,
      },
    });
    await assert.rejects(runtime.runAuthorizedExecution(AUTHORIZATION_ID), /page rejected before click/);
    const job = storage.executionReadbackJobsV1[ACTION_ID];
    assert.equal(job.status, "failed");
    assert.equal(job.journal_state, "submission_failed");
    assert.equal(job.execution_receipt.plan_id, REQUEST.plan_id);
    assert.equal(job.execution_receipt.target_value, REQUEST.target_value);
    assert.equal(runtime.calls.readbacks, 0, "literal submitted=false should terminate without readback");
  }

  {
    const storage = {};
    const server = { receiptRecorded: false, preflightAvailable: true, receiptMode: "ok" };
    const runtime = createRuntime(storage, server, { submitThrows: true });
    await assert.rejects(
      runtime.runAuthorizedExecution(AUTHORIZATION_ID),
      (error) => error.code === "SUBMISSION_RESPONSE_LOST",
    );
    const job = storage.executionReadbackJobsV1[ACTION_ID];
    assert.equal(job.journal_state, "submission_unknown");
    assert.equal(job.execution_receipt, undefined, "transport loss must never fabricate a submitted=false receipt");
    assert.equal(runtime.calls.receiptPosts, 0);
    assert.equal(runtime.calls.readbacks, 1);
  }

  {
    const storage = {};
    const server = {
      preflightAvailable: false,
      receiptMode: "ok",
      authorizationConsumed: true,
      sessionState: "authorization_consumed",
    };
    const runtime = createRuntime(storage, server);
    const firstProbe = await runtime.recoverExecutionReadbackJobs("storage-empty-agent-starting");
    assert.equal(firstProbe.reconstruction_probe_succeeded, false);
    assert.equal(runtime.calls.alarmCreates.length, 1, "an unavailable Agent must leave a reconstruction alarm behind");
    assert.equal(runtime.calls.alarmClears, 0, "unknown backend state must never clear the only recovery alarm");

    server.preflightAvailable = true;
    server.recoveryJob = {
      ...policy.createJob({
        action_id: ACTION_ID,
        store_key: "store-a",
        account_key: "account-a",
        plan_id: "plan-a",
        started_at_ms: Date.now() - 10_000,
      }),
      authorization_id: AUTHORIZATION_ID,
      journal_state: "readback_pending",
      reconstructed_from_agent: true,
    };
    const recovered = await runtime.recoverExecutionReadbackJobs("recovery-alarm");
    assert.equal(recovered.reconstruction_probe_succeeded, true);
    assert.equal(runtime.calls.recoveryJobReads, 1);
    assert.equal(storage.executionReadbackJobsV1[ACTION_ID].reconstructed_from_agent, true);
    assert.equal(runtime.calls.readbacks, 1, "the second alarm probe must resume read-only verification without a submit");
    assert.equal(runtime.calls.pageSubmits, 0);
  }

  {
    const terminalJob = {
      ...policy.createJob({
        action_id: ACTION_ID,
        store_key: "store-a",
        account_key: "account-a",
        plan_id: "plan-a",
        started_at_ms: Date.now() - 10_000,
      }),
      authorization_id: AUTHORIZATION_ID,
      journal_state: "readback_pending",
    };
    const storage = { executionReadbackJobsV1: { [ACTION_ID]: terminalJob } };
    const server = {
      preflightAvailable: true,
      receiptMode: "ok",
      sessionState: "completed",
      receiptActionState: "verified",
    };
    const runtime = createRuntime(storage, server);
    await runtime.recoverExecutionReadbackJobs("backend-terminal");
    assert.equal(storage.executionReadbackJobsV1[ACTION_ID].status, "verified");
    assert.equal(storage.executionReadbackJobsV1[ACTION_ID].last_verification.reconciled_from_agent, true);
    assert.equal(runtime.calls.readbacks, 0, "an exact backend terminal state must stop browser readback immediately");

    const mismatchedJob = {
      ...terminalJob,
      authorization_id: "c".repeat(32),
      updated_at_ms: Date.now() + 1,
    };
    const mismatchStorage = { executionReadbackJobsV1: { [ACTION_ID]: mismatchedJob } };
    const mismatchRuntime = createRuntime(mismatchStorage, server);
    await mismatchRuntime.recoverExecutionReadbackJobs("backend-terminal-mismatch");
    assert.equal(mismatchStorage.executionReadbackJobsV1[ACTION_ID].status, "pending", "another authorization must never unlock the local action");
  }

  {
    const storage = {};
    const archivedRecoveryJob = {
      ...policy.createJob({
        action_id: ACTION_ID,
        store_key: "store-a",
        account_key: "account-a",
        plan_id: "plan-a",
        started_at_ms: Date.now() - 10_000,
      }),
      authorization_id: AUTHORIZATION_ID,
      journal_state: "readback_pending",
      manual_reconcile_archived: true,
      plan_retry_blocked: true,
    };
    const server = {
      preflightAvailable: true,
      receiptMode: "ok",
      sessionState: "awaiting_reread",
      actionId: "d".repeat(24),
      authorizationId: "e".repeat(32),
      recoverableActionIds: [ACTION_ID],
      recoveryJobs: { [ACTION_ID]: archivedRecoveryJob },
    };
    const runtime = createRuntime(storage, server);
    const recovered = await runtime.recoverExecutionReadbackJobs("archived-action-with-new-singleton");
    assert.equal(recovered.reconstruction_probe_succeeded, true);
    assert.equal(runtime.calls.recoveryJobReads, 1, "archived actions must reconstruct even when another plan owns the singleton preflight");
    assert.equal(storage.executionReadbackJobsV1[ACTION_ID].manual_reconcile_archived, true);
    assert.equal(storage.executionReadbackJobsV1[ACTION_ID].status_label, "已从归档账本恢复只读验收；同一计划永久禁止自动重投");
    assert.equal(runtime.calls.pageSubmits, 0, "archived reconstruction is read-only and can never replay the platform write");
  }

  {
    const archivedUncertainJob = {
      ...storedJob(ACTION_ID, "uncertain", Date.now() - 10_000),
      authorization_id: AUTHORIZATION_ID,
      journal_state: "readback_uncertain",
      manual_reconcile_archived: true,
      plan_retry_blocked: true,
    };
    const storage = { executionReadbackJobsV1: { [ACTION_ID]: archivedUncertainJob } };
    const server = {
      preflightAvailable: true,
      receiptMode: "ok",
      sessionState: "awaiting_reread",
      actionId: "d".repeat(24),
      authorizationId: "e".repeat(32),
      recoverableActionIds: [ACTION_ID],
    };
    const runtime = createRuntime(storage, server);
    const recovered = await runtime.recoverExecutionReadbackJobs("archived-uncertain-with-new-singleton");
    assert.equal(recovered.pending, 0, "an exhausted archived readback must remain uncertain until a manual retry");
    assert.equal(runtime.calls.alarmCreates.length, 1, "the backend recoverable archive must keep the recovery alarm alive");
    assert.equal(runtime.calls.alarmClears, 0, "another singleton must not clear the archived action's recovery alarm");
    assert.equal(runtime.calls.pageSubmits, 0, "alarm recovery for an archived action is read-only and must never submit again");
  }

  {
    const notDue = {
      ...policy.createJob({
        action_id: ACTION_ID,
        store_key: "store-a",
        account_key: "account-a",
        plan_id: "plan-a",
        started_at_ms: Date.now() + 60_000,
      }),
      authorization_id: AUTHORIZATION_ID,
    };
    const storage = { executionReadbackJobsV1: { [ACTION_ID]: notDue } };
    const runtime = createRuntime(storage, {
      preflightAvailable: true,
      receiptMode: "ok",
      sessionState: "authorization_consumed",
      authorizationConsumed: true,
    });
    const recovered = await runtime.recoverExecutionReadbackJobs("pending-not-due");
    assert.equal(recovered.pending, 1);
    assert.equal(runtime.calls.readbacks, 0);
    assert.equal(runtime.calls.alarmCreates.length, 1, "a not-yet-due pending job must recreate a missing browser alarm");
  }

  {
    const storage = {};
    const server = { receiptRecorded: false, preflightAvailable: true, receiptMode: "ok" };
    const runtime = createRuntime(storage, server);
    const base = policy.createJob({
      action_id: ACTION_ID,
      store_key: "store-a",
      account_key: "account-a",
      plan_id: "plan-a",
      started_at_ms: Date.now() - 5_000,
    });
    await runtime.persistExecutionReadbackJob({ ...base, journal_state: "readback_pending", updated_at_ms: 200 });
    await runtime.persistExecutionReadbackJob({ ...base, journal_state: "awaiting_receipt", updated_at_ms: 300 });
    assert.equal(
      storage.executionReadbackJobsV1[ACTION_ID].journal_state,
      "readback_pending",
      "a stale receipt failure must not overwrite a newer readback state",
    );
  }

  assert.match(background, /recoverExecutionReadbackJobs\("worker-start"\)/);
  assert.match(background, /const executionJournalAdvancePromises = new Map\(\)/);
  console.log("background execution journal recovery tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
